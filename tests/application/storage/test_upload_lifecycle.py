from __future__ import annotations

import copy
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType
from uuid import UUID

import pytest

from spine.application.persistence import (
    CommandDigest,
    digest_command,
    EnvironmentScope,
    IdempotencyConflictError,
    IdempotencyKey,
    IdempotencyReplay,
    InvalidPersistenceContextError,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
    PersistenceOperation,
    PersistenceUnavailableError,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.storage import (
    OriginalUploadOrchestrator,
    OriginalUploadRecord,
    RecordingStorageOutcomeRecorder,
    UploadCleanupStatus,
)
from spine.domain.common import ContextOrigin
from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    IntegrityMismatch,
    ObjectProtected,
    ObjectUnavailable,
    OriginalUploadCommandInput,
    OriginalUploadCommandPayload,
    OriginalUploadCommandResult,
    Issue8ResultReference,
    PreWriteRequestDigest,
    ReconciliationRequired,
    UploadCommandStateName,
    UploadConflict,
)
from spine.infrastructure.object_storage.contracts import (
    ObservedContentIdentity,
    UploadCommandState,
    WriteReceipt,
)
from spine.infrastructure.object_storage.testing import (
    DeterministicOriginalObjectStoreFake,
)


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000094")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000094")
ACTOR_ID = UUID("30000000-0000-0000-0000-000000000094")
TRACE_ID = UUID("40000000-0000-0000-0000-000000000094")
ISSUER_ID = UUID("50000000-0000-0000-0000-000000000094")
RECEIPT_ID = UUID("60000000-0000-0000-0000-000000000094")
UPLOAD_ID = UUID("70000000-0000-0000-0000-000000000094")
OBJECT_ID = UUID("80000000-0000-0000-0000-000000000094")
GENERATION_ID = UUID("90000000-0000-0000-0000-000000000094")
OTHER_WORKSPACE_ID = UUID("11000000-0000-0000-0000-000000000094")
OTHER_ENVIRONMENT_ID = UUID("22000000-0000-0000-0000-000000000094")
OTHER_UPLOAD_ID = UUID("77000000-0000-0000-0000-000000000094")
OTHER_OBJECT_ID = UUID("88000000-0000-0000-0000-000000000094")
OTHER_GENERATION_ID = UUID("99000000-0000-0000-0000-000000000094")
FINALIZED_AT = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _StoredIdempotencyReceipt:
    receipt_id: UUID
    digest: CommandDigest
    result: OpaqueResultReference | None


@dataclass(slots=True)
class _FakeStore:
    receipts: dict[str, _StoredIdempotencyReceipt] = field(default_factory=dict)
    uploads: dict[UUID, OriginalUploadRecord] = field(default_factory=dict)
    commit_attempts: int = 0
    failing_commit_attempts: set[int] = field(default_factory=set)


class _FakeIdempotencyRepository:
    def __init__(
        self,
        receipts: dict[str, _StoredIdempotencyReceipt],
    ) -> None:
        self._receipts = receipts

    async def claim(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> OwnedIdempotencyClaim | IdempotencyReplay:
        existing = self._receipts.get(key.value)
        if existing is not None:
            if existing.digest != digest:
                raise IdempotencyConflictError(
                    "Idempotency key conflicts with existing command."
                )
            assert existing.result is not None
            return IdempotencyReplay(
                kind="replay",
                receipt_id=existing.receipt_id,
                operation=PersistenceOperation("original.upload"),
                operation_schema_version=operation_schema_version,
                key=key,
                result=existing.result,
            )
        receipt_id = RECEIPT_ID
        claim = OwnedIdempotencyClaim(
            kind="owned",
            receipt_id=receipt_id,
            operation=PersistenceOperation("original.upload"),
            operation_schema_version=operation_schema_version,
            key=key,
            digest=digest,
        )
        self._receipts[key.value] = _StoredIdempotencyReceipt(
            receipt_id=receipt_id,
            digest=digest,
            result=None,
        )
        return claim

    async def complete(
        self,
        claim: OwnedIdempotencyClaim,
        result: OpaqueResultReference,
    ) -> None:
        existing = self._receipts[claim.key.value]
        self._receipts[claim.key.value] = _StoredIdempotencyReceipt(
            receipt_id=existing.receipt_id,
            digest=existing.digest,
            result=result,
        )


class _FakeUploadRepository:
    def __init__(self, uploads: dict[UUID, OriginalUploadRecord]) -> None:
        self._uploads = uploads

    async def add(self, record: OriginalUploadRecord) -> None:
        if record.result.upload_id in self._uploads:
            raise IdempotencyConflictError("Upload result identity already exists.")
        self._uploads[record.result.upload_id] = record

    async def resolve(self, upload_id: UUID) -> OriginalUploadRecord | None:
        return self._uploads.get(upload_id)

    async def replace(
        self,
        record: OriginalUploadRecord,
        *,
        expected_version: int,
    ) -> None:
        current = self._uploads.get(record.result.upload_id)
        if current is None or current.record_version != expected_version:
            raise IdempotencyConflictError("Upload lifecycle version conflicts.")
        self._uploads[record.result.upload_id] = record


class _FakeUnitOfWork:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self._receipts = copy.deepcopy(store.receipts)
        self._uploads = copy.deepcopy(store.uploads)
        self.idempotency = _FakeIdempotencyRepository(self._receipts)
        self.original_uploads = _FakeUploadRepository(self._uploads)
        self._committed = False

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._committed:
            await self.rollback()

    async def commit(self) -> None:
        self._store.commit_attempts += 1
        if self._store.commit_attempts in self._store.failing_commit_attempts:
            raise PersistenceUnavailableError("Synthetic PostgreSQL rollback.")
        self._store.receipts = self._receipts
        self._store.uploads = self._uploads
        self._committed = True

    async def rollback(self) -> None:
        return None


def _context(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("document.upload.v1"),
        operation=PersistenceOperation("original.upload"),
        trace_id=TRACE_ID,
        acting_subject_id=ACTOR_ID,
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=ISSUER_ID,
            signature=b"trusted-test-context",
        ),
    )


def _command(**updates: object) -> OriginalUploadCommandInput:
    values: dict[str, object] = {
        "operation_name": "original.upload",
        "operation_schema_version": 1,
        "idempotency_key": "upload-94",
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "purpose": "document.upload.v1",
        "declared_filename": "untrusted.txt",
        "declared_media_type": "text/plain",
    }
    values.update(updates)
    return OriginalUploadCommandInput.model_validate(values)


class _FaultInjectingObjectStore:
    def __init__(self, wrapped: DeterministicOriginalObjectStoreFake) -> None:
        self._wrapped = wrapped
        self.fail_next_finalize = False
        self.fail_next_abort = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)

    async def finalize_upload(
        self,
        state: UploadCommandState,
        observed_content: ObservedContentIdentity,
    ) -> WriteReceipt:
        if self.fail_next_finalize:
            self.fail_next_finalize = False
            raise ObjectUnavailable()
        return await self._wrapped.finalize_upload(state, observed_content)

    async def abort_upload(self, state: UploadCommandState) -> UploadCommandState:
        if self.fail_next_abort:
            self.fail_next_abort = False
            raise ObjectUnavailable()
        return await self._wrapped.abort_upload(state)


class _FailingOutcomeRecorder:
    async def record(self, outcome: object) -> None:
        raise RuntimeError("diagnostic transport failed")


def _orchestrator(
    store: _FakeStore,
    outcome_recorder: RecordingStorageOutcomeRecorder | None = None,
) -> tuple[OriginalUploadOrchestrator, _FaultInjectingObjectStore]:
    identities = iter((UPLOAD_ID, OBJECT_ID))
    object_store = _FaultInjectingObjectStore(
        DeterministicOriginalObjectStoreFake(
            grant_registry=AuthorizedReadGrantRegistry(),
            generation_factory=lambda: GENERATION_ID,
            clock=lambda: FINALIZED_AT,
            max_chunk_bytes=1024,
            max_object_bytes=4096,
            read_chunk_bytes=64,
        )
    )
    return (
        OriginalUploadOrchestrator(
            uow_factory=lambda _context: _FakeUnitOfWork(store),
            object_store=object_store,  # type: ignore[arg-type]
            identity_factory=lambda: next(identities),
            outcome_recorder=outcome_recorder,
        ),
        object_store,
    )


@pytest.mark.asyncio
async def test_successful_upload_emits_safe_idempotent_storage_outcome() -> None:
    store = _FakeStore()
    recorder = RecordingStorageOutcomeRecorder()
    orchestrator, _ = _orchestrator(store, recorder)

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    result = await orchestrator.upload(_context(), _command(), chunks())

    assert result.replay is False
    assert len(recorder.outcomes) == 1
    emitted = recorder.outcomes[0]
    assert emitted.workspace_id == WORKSPACE_ID
    assert emitted.trace_id == TRACE_ID
    assert emitted.subject_id == UPLOAD_ID
    assert emitted.status.value == "succeeded"
    assert emitted.producer_deduplication_id.endswith(":accepted:attempt-1")


@pytest.mark.asyncio
async def test_outcome_failure_does_not_rollback_finalized_upload() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store, _FailingOutcomeRecorder())  # type: ignore[arg-type]

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    await orchestrator.upload(_context(), _command(), chunks())

    assert store.uploads[UPLOAD_ID].state.state is UploadCommandStateName.FINALIZED


@pytest.mark.asyncio
async def test_finalized_upload_replay_emits_a_replay_outcome() -> None:
    store = _FakeStore()
    recorder = RecordingStorageOutcomeRecorder()
    orchestrator, _ = _orchestrator(store, recorder)

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    await orchestrator.upload(_context(), _command(), chunks())
    await orchestrator.upload(_context(), _command(), chunks())

    assert len(recorder.outcomes) == 2
    assert recorder.outcomes[1].producer_deduplication_id.endswith(
        ":replay:attempt-1"
    )


@pytest.mark.asyncio
async def test_upload_without_expected_checksum_claims_before_stream_and_replays_same_bytes() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)
    command = _command()

    async def chunks() -> AsyncIterable[bytes]:
        assert store.receipts["upload-94"].result == OpaqueResultReference(
            result_type="original_upload",
            result_id=UPLOAD_ID,
            schema_version=1,
        )
        yield b"hello"

    first = await orchestrator.upload(_context(), command, chunks())
    second = await orchestrator.upload(_context(), command, chunks())

    assert first.replay is False
    assert second.replay is True
    assert second.result == first.result
    assert second.write_receipt == first.write_receipt
    assert first.write_receipt.observed_content.model_dump() == {
        "observed_sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        "observed_byte_length": 5,
    }
    assert first.result.upload_id == UPLOAD_ID
    assert first.result.object_id == OBJECT_ID
    assert first.result.request_digest.digest_hex == (
        "aeae08def2f08df9f22e0ebbb6c54aeb7d91d323050f71655023c3d8b1cad090"
    )
    assert first.write_receipt.object_reference.storage_generation == GENERATION_ID
    record = store.uploads[UPLOAD_ID]
    assert record.observed_content == first.write_receipt.observed_content
    assert record.state.state is UploadCommandStateName.FINALIZED
    assert set(type(first.write_receipt).model_fields) == {
        "schema_version",
        "upload_id",
        "object_reference",
        "observed_content",
        "verification",
        "storage_status",
        "finalized_at",
    }


@pytest.mark.asyncio
async def test_same_key_with_changed_semantic_field_conflicts_before_stream() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)
    streamed = False

    async def first_chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    async def conflicting_chunks() -> AsyncIterable[bytes]:
        nonlocal streamed
        streamed = True
        yield b"hello"

    await orchestrator.upload(_context(), _command(), first_chunks())

    with pytest.raises(IdempotencyConflictError):
        await orchestrator.upload(
            _context(),
            _command(expected_byte_length=6),
            conflicting_chunks(),
        )

    assert streamed is False


@pytest.mark.asyncio
async def test_same_key_replay_with_different_observed_bytes_preserves_finalized_object() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)

    async def original_chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    async def changed_chunks() -> AsyncIterable[bytes]:
        yield b"world"

    first = await orchestrator.upload(_context(), _command(), original_chunks())

    with pytest.raises(UploadConflict):
        await orchestrator.upload(_context(), _command(), changed_chunks())

    record = store.uploads[UPLOAD_ID]
    assert record.state.state is UploadCommandStateName.FINALIZED
    assert record.state.provisional_receipt == first.write_receipt


@pytest.mark.asyncio
async def test_context_scope_mismatch_is_denied_before_claim_or_stream() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)
    streamed = False

    async def chunks() -> AsyncIterable[bytes]:
        nonlocal streamed
        streamed = True
        yield b"protected"

    with pytest.raises(InvalidPersistenceContextError) as error:
        await orchestrator.upload(
            _context(workspace_id=OTHER_WORKSPACE_ID),
            _command(),
            chunks(),
        )

    assert streamed is False
    assert store.receipts == {}
    assert str(WORKSPACE_ID) not in str(error.value)


@pytest.mark.asyncio
async def test_same_key_and_bytes_remain_isolated_between_two_workspaces() -> None:
    stores = {
        WORKSPACE_ID: _FakeStore(),
        OTHER_WORKSPACE_ID: _FakeStore(),
    }
    identities = iter((UPLOAD_ID, OBJECT_ID, OTHER_UPLOAD_ID, OTHER_OBJECT_ID))
    generations = iter((GENERATION_ID, OTHER_GENERATION_ID))
    object_store = DeterministicOriginalObjectStoreFake(
        grant_registry=AuthorizedReadGrantRegistry(),
        generation_factory=lambda: next(generations),
        clock=lambda: FINALIZED_AT,
        max_chunk_bytes=1024,
        max_object_bytes=4096,
        read_chunk_bytes=64,
    )
    orchestrator = OriginalUploadOrchestrator(
        uow_factory=lambda context: _FakeUnitOfWork(
            stores[context.scope.workspace_id]
        ),
        object_store=object_store,
        identity_factory=lambda: next(identities),
    )

    async def chunks() -> AsyncIterable[bytes]:
        yield b"same bytes"

    first = await orchestrator.upload(_context(), _command(), chunks())
    second = await orchestrator.upload(
        _context(
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=OTHER_ENVIRONMENT_ID,
        ),
        _command(
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=OTHER_ENVIRONMENT_ID,
        ),
        chunks(),
    )

    assert first.result.upload_id == UPLOAD_ID
    assert second.result.upload_id == OTHER_UPLOAD_ID
    assert first.write_receipt.object_reference != second.write_receipt.object_reference
    assert first.write_receipt.observed_content == second.write_receipt.observed_content


@pytest.mark.asyncio
async def test_retry_after_failure_past_observed_hashing_reuses_stored_identity() -> None:
    store = _FakeStore()
    orchestrator, object_store = _orchestrator(store)
    object_store.fail_next_finalize = True

    async def original_chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    with pytest.raises(ObjectUnavailable):
        await orchestrator.upload(_context(), _command(), original_chunks())

    observed = store.uploads[UPLOAD_ID].observed_content
    assert observed is not None

    async def must_not_be_read_again() -> AsyncIterable[bytes]:
        raise AssertionError("stored observed identity must skip duplicate streaming")
        yield b"different"  # pragma: no cover

    recovered = await orchestrator.upload(
        _context(),
        _command(),
        must_not_be_read_again(),
    )

    assert recovered.replay is True
    assert recovered.write_receipt.observed_content == observed
    assert recovered.write_receipt.object_reference.storage_generation == GENERATION_ID


@pytest.mark.asyncio
async def test_interrupted_stream_resumes_without_reallocating_identities() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)

    async def interrupted_chunks() -> AsyncIterable[bytes]:
        yield b"hel"
        raise RuntimeError("synthetic private stream failure")

    with pytest.raises(ObjectUnavailable) as error:
        await orchestrator.upload(_context(), _command(), interrupted_chunks())

    assert "synthetic private" not in str(error.value)
    assert store.uploads[UPLOAD_ID].state.state is UploadCommandStateName.INTERRUPTED

    async def remaining_chunks() -> AsyncIterable[bytes]:
        yield b"lo"

    recovered = await orchestrator.upload(_context(), _command(), remaining_chunks())

    assert recovered.result.upload_id == UPLOAD_ID
    assert recovered.result.object_id == OBJECT_ID
    assert recovered.write_receipt.observed_content.observed_byte_length == 5
    assert store.uploads[UPLOAD_ID].state.state is UploadCommandStateName.FINALIZED


@pytest.mark.asyncio
async def test_expected_checksum_mismatch_is_terminal_integrity_conflict() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)
    command = _command(expected_sha256="0" * 64)

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    with pytest.raises(IntegrityMismatch):
        await orchestrator.upload(_context(), command, chunks())

    assert (
        store.uploads[UPLOAD_ID].state.state
        is UploadCommandStateName.INTEGRITY_CONFLICT
    )

    streamed = False

    async def forbidden_retry() -> AsyncIterable[bytes]:
        nonlocal streamed
        streamed = True
        yield b"hello"

    with pytest.raises(ObjectProtected):
        await orchestrator.upload(_context(), command, forbidden_retry())

    assert streamed is False


@pytest.mark.asyncio
async def test_postgresql_rollback_after_physical_finalization_requires_reconciliation() -> None:
    store = _FakeStore(failing_commit_attempts={3})
    orchestrator, _ = _orchestrator(store)

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    with pytest.raises(ReconciliationRequired):
        await orchestrator.upload(_context(), _command(), chunks())

    record = store.uploads[UPLOAD_ID]
    assert record.observed_content is not None
    assert record.state.state is UploadCommandStateName.RECONCILIATION_REQUIRED
    assert record.state.provisional_receipt is None

    streamed = False

    async def forbidden_retry() -> AsyncIterable[bytes]:
        nonlocal streamed
        streamed = True
        yield b"hello"

    with pytest.raises(ReconciliationRequired):
        await orchestrator.upload(_context(), _command(), forbidden_retry())

    assert streamed is False


@pytest.mark.asyncio
async def test_trusted_reconciliation_rehydrates_proven_finalization() -> None:
    store = _FakeStore(failing_commit_attempts={3})
    orchestrator, _ = _orchestrator(store)

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    with pytest.raises(ReconciliationRequired):
        await orchestrator.upload(_context(), _command(), chunks())

    reconciled = await orchestrator.reconcile(_context(), _command())

    assert reconciled.resolved is True
    assert reconciled.changed is True
    assert reconciled.state is UploadCommandStateName.FINALIZED
    assert reconciled.write_receipt is not None
    assert store.uploads[UPLOAD_ID].state.state is UploadCommandStateName.FINALIZED


@pytest.mark.asyncio
async def test_trusted_reconciliation_leaves_ambiguous_evidence_blocked() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)
    command = _command()

    # Seed a durable upload record in the blocked state without physical evidence.
    record = OriginalUploadRecord(
        record_version=1,
        command=command,
        result=OriginalUploadCommandResult(
            request_digest=PreWriteRequestDigest(
                algorithm="sha256",
                algorithm_version="spine.command-digest.v1",
                digest_hex="a" * 64,
            ),
            upload_id=UPLOAD_ID,
            object_id=OBJECT_ID,
            issue8_result_reference=Issue8ResultReference(receipt_id=RECEIPT_ID),
        ),
        state=UploadCommandState(
            issue8_receipt_id=RECEIPT_ID,
            request_digest=PreWriteRequestDigest(
                algorithm="sha256",
                algorithm_version="spine.command-digest.v1",
                digest_hex="a" * 64,
            ),
            upload_id=UPLOAD_ID,
            object_id=OBJECT_ID,
            state=UploadCommandStateName.RECONCILIATION_REQUIRED,
        ),
    )
    store.uploads[UPLOAD_ID] = record
    request_digest = digest_command(
        operation=PersistenceOperation("original.upload"),
        operation_schema_version=1,
        payload=OriginalUploadCommandPayload(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            purpose=command.purpose,
            expected_sha256=command.expected_sha256,
            expected_byte_length=command.expected_byte_length,
        ).model_dump(),
    )
    store.receipts[command.idempotency_key] = _StoredIdempotencyReceipt(
        receipt_id=RECEIPT_ID,
        digest=request_digest,
        result=OpaqueResultReference(
            result_type="original_upload",
            result_id=UPLOAD_ID,
            schema_version=1,
        ),
    )

    reconciled = await orchestrator.reconcile(_context(), command)

    assert reconciled.resolved is False
    assert reconciled.changed is False
    assert reconciled.state is UploadCommandStateName.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_postgresql_rollback_after_observed_hash_requires_reconciliation() -> None:
    store = _FakeStore(failing_commit_attempts={2})
    orchestrator, _ = _orchestrator(store)

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    with pytest.raises(ReconciliationRequired):
        await orchestrator.upload(_context(), _command(), chunks())

    record = store.uploads[UPLOAD_ID]
    assert record.observed_content is None
    assert record.state.state is UploadCommandStateName.RECONCILIATION_REQUIRED

    streamed = False

    async def forbidden_retry() -> AsyncIterable[bytes]:
        nonlocal streamed
        streamed = True
        yield b"hello"

    with pytest.raises(ReconciliationRequired):
        await orchestrator.upload(_context(), _command(), forbidden_retry())
    assert streamed is False


@pytest.mark.asyncio
async def test_cleanup_unknown_key_does_not_claim_or_allocate_upload() -> None:
    store = _FakeStore()
    orchestrator, _ = _orchestrator(store)

    with pytest.raises(IdempotencyConflictError):
        await orchestrator.cleanup_aborted(_context(), _command())

    assert store.receipts == {}
    assert store.uploads == {}


@pytest.mark.asyncio
async def test_abort_does_not_hide_physical_finalization_after_persistence_failure() -> None:
    store = _FakeStore(failing_commit_attempts={3, 4})
    orchestrator, _ = _orchestrator(store)

    async def chunks() -> AsyncIterable[bytes]:
        yield b"hello"

    with pytest.raises(ReconciliationRequired):
        await orchestrator.upload(_context(), _command(), chunks())
    assert store.uploads[UPLOAD_ID].state.state is UploadCommandStateName.STAGING

    with pytest.raises(ReconciliationRequired):
        await orchestrator.abort(_context(), _command())
    assert (
        store.uploads[UPLOAD_ID].state.state
        is UploadCommandStateName.RECONCILIATION_REQUIRED
    )


@pytest.mark.asyncio
async def test_abort_is_terminal_when_separate_cleanup_fails_then_retries() -> None:
    store = _FakeStore()
    orchestrator, object_store = _orchestrator(store)

    async def interrupted_chunks() -> AsyncIterable[bytes]:
        yield b"partial"
        raise RuntimeError("synthetic interruption")

    with pytest.raises(ObjectUnavailable):
        await orchestrator.upload(_context(), _command(), interrupted_chunks())

    aborted = await orchestrator.abort(_context(), _command())
    assert aborted.state is UploadCommandStateName.ABORTED
    assert aborted.cleanup_status is UploadCleanupStatus.PENDING

    object_store.fail_next_abort = True
    failed_cleanup = await orchestrator.cleanup_aborted(_context(), _command())
    assert failed_cleanup.cleanup_status is UploadCleanupStatus.FAILED
    assert failed_cleanup.cleanup_error_category is not None
    assert store.uploads[UPLOAD_ID].state.state is UploadCommandStateName.ABORTED

    async def forbidden_retry() -> AsyncIterable[bytes]:
        raise AssertionError("aborted uploads cannot stream again")
        yield b"ignored"  # pragma: no cover

    with pytest.raises(ObjectProtected):
        await orchestrator.upload(_context(), _command(), forbidden_retry())

    cleaned = await orchestrator.cleanup_aborted(_context(), _command())
    assert cleaned.cleanup_status is UploadCleanupStatus.COMPLETED
    assert cleaned.cleanup_error_category is None
    assert store.uploads[UPLOAD_ID].state.state is UploadCommandStateName.ABORTED

    replayed_abort = await orchestrator.abort(_context(), _command())
    assert replayed_abort.replay is True
    assert replayed_abort.cleanup_status is UploadCleanupStatus.COMPLETED
