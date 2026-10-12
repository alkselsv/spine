from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.infrastructure.object_storage import (
    DeletionApproval,
    IntegrityMismatch,
    InvalidReadGrant,
    ObjectProtected,
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    PreWriteRequestDigest,
    ReconciliationRequired,
    StorageError,
    UploadConflict,
    UploadLimitExceeded,
    UploadCommandState,
    UploadCommandStateName,
    WriteReceipt,
)

from .adapter import OriginalObjectStoreAdapter, OriginalObjectStoreFactory


WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("20000000-0000-4000-8000-000000000001")
OBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
RECEIPT_ID = UUID("40000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("60000000-0000-4000-8000-000000000001")
SOURCE_REVISION_ID = UUID("70000000-0000-4000-8000-000000000001")
CONTENT = b"synthetic original\n"


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def split_chunks(content: bytes, size: int) -> tuple[bytes, ...]:
    return tuple(
        content[offset : offset + size] for offset in range(0, len(content), size)
    )


def upload_command(
    content: bytes = CONTENT,
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID | None = None,
    idempotency_key: str = "upload-1",
    expected_sha256: str | None = None,
    expected_byte_length: int | None = None,
) -> OriginalUploadCommandInput:
    return OriginalUploadCommandInput(
        operation_name="original.upload",
        operation_schema_version=1,
        idempotency_key=idempotency_key,
        workspace_id=workspace_id,
        environment_id=environment_id,
        expected_sha256=expected_sha256 or hashlib.sha256(content).hexdigest(),
        expected_byte_length=(
            len(content) if expected_byte_length is None else expected_byte_length
        ),
        purpose="document.upload",
    )


def upload_state(
    *,
    upload_id: UUID = UPLOAD_ID,
    object_id: UUID = OBJECT_ID,
    receipt_id: UUID = RECEIPT_ID,
    state: UploadCommandStateName = UploadCommandStateName.STAGING,
) -> UploadCommandState:
    return UploadCommandState(
        issue8_receipt_id=receipt_id,
        request_digest=PreWriteRequestDigest(
            algorithm="sha256",
            algorithm_version="spine.command-digest.v1",
            digest_hex="a" * 64,
        ),
        upload_id=upload_id,
        object_id=object_id,
        state=state,
    )


async def finalized_object(
    adapter: OriginalObjectStoreAdapter,
    *,
    content: bytes = CONTENT,
    command: OriginalUploadCommandInput | None = None,
    state: UploadCommandState | None = None,
) -> tuple[UploadCommandState, ObservedContentIdentity, WriteReceipt]:
    command = command or upload_command(content)
    state = state or upload_state()
    active = await adapter.store.begin_or_resume_upload(command, state)
    observed = await adapter.store.write_chunks(
        active.upload_id,
        chunks(*split_chunks(content, adapter.max_write_chunk_bytes)),
    )
    receipt = await adapter.store.finalize_upload(active, observed)
    return active, observed, receipt


async def read_bytes(
    adapter: OriginalObjectStoreAdapter,
    receipt: WriteReceipt,
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID | None = None,
    max_bytes: int = 64,
) -> bytes:
    grant = adapter.grant_registry.issue(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_revision_id=SOURCE_REVISION_ID,
        object_reference=receipt.object_reference,
        purpose="read.original",
        authorization_decision_version="policy-v1",
        expires_at=adapter.now + timedelta(minutes=1),
    )
    lease = adapter.grant_registry.consume(
        grant,
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_revision_id=SOURCE_REVISION_ID,
        object_reference=receipt.object_reference,
        now=adapter.now,
    )
    return b"".join(
        [part async for part in adapter.store.open_bounded_read(lease, max_bytes=max_bytes)]
    )


async def test_same_generation_replay_returns_stable_result_without_second_object(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state, observed, first = await finalized_object(adapter)

    replay_state = await adapter.store.begin_or_resume_upload(upload_command(), upload_state())
    replay = await adapter.store.finalize_upload(upload_state(), observed)
    inventory, next_cursor = await adapter.store.reconciliation_inventory()

    assert replay_state.provisional_receipt == first
    assert replay == first
    assert [entry.object_reference for entry in inventory] == [first.object_reference]
    assert next_cursor is None
    assert first.finalized_at == datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


async def test_replay_ignores_display_metadata_excluded_from_request_identity(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(adapter)
    replay_command = upload_command().model_copy(
        update={
            "declared_filename": "renamed-display-value.txt",
            "declared_media_type": "application/octet-stream",
        }
    )

    replay = await adapter.store.begin_or_resume_upload(replay_command, upload_state())

    assert replay.provisional_receipt == receipt


async def test_different_generation_for_existing_object_is_typed_conflict(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, first = await finalized_object(adapter)
    replacement_state = upload_state(
        upload_id=UUID("20000000-0000-4000-8000-000000000002"),
        object_id=OBJECT_ID,
        receipt_id=UUID("40000000-0000-4000-8000-000000000002"),
    )
    command = upload_command(b"replacement", idempotency_key="upload-2")
    active = await adapter.store.begin_or_resume_upload(command, replacement_state)
    observed = await adapter.store.write_chunks(
        active.upload_id,
        chunks(*split_chunks(b"replacement", adapter.max_write_chunk_bytes)),
    )

    with pytest.raises(UploadConflict):
        await adapter.store.finalize_upload(active, observed)

    assert await read_bytes(adapter, first) == CONTENT


async def test_finalized_generation_rejects_different_observed_retry_without_replacement(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state, _, receipt = await finalized_object(adapter)
    different = ObservedContentIdentity(
        observed_sha256=hashlib.sha256(b"different").hexdigest(),
        observed_byte_length=len(b"different"),
    )

    with pytest.raises(UploadConflict):
        await adapter.store.finalize_upload(state, different)

    assert await read_bytes(adapter, receipt) == CONTENT


@pytest.mark.parametrize(
    ("retry_content", "conflicts"),
    [(CONTENT, False), (b"different", True)],
    ids=["same-bytes", "different-bytes"],
)
async def test_finalized_stream_retry_is_stable_without_replacing_bytes(
    original_object_store_factory: OriginalObjectStoreFactory,
    retry_content: bytes,
    conflicts: bool,
) -> None:
    adapter = original_object_store_factory()
    command = upload_command().model_copy(
        update={"expected_sha256": None, "expected_byte_length": None}
    )
    state, _, receipt = await finalized_object(adapter, command=command)

    retried_observation = await adapter.store.write_chunks(
        state.upload_id,
        chunks(*split_chunks(retry_content, adapter.max_write_chunk_bytes)),
    )
    if conflicts:
        with pytest.raises(UploadConflict):
            await adapter.store.finalize_upload(state, retried_observation)
    else:
        replay = await adapter.store.finalize_upload(state, retried_observation)
        assert replay == receipt

    assert await read_bytes(adapter, receipt) == CONTENT


@pytest.mark.parametrize(
    ("command", "content"),
    [
        (
            upload_command(expected_sha256="0" * 64),
            CONTENT,
        ),
        (
            upload_command(expected_byte_length=len(CONTENT) + 1),
            CONTENT,
        ),
    ],
    ids=["checksum", "length"],
)
async def test_integrity_mismatch_does_not_publish_finalized_object(
    original_object_store_factory: OriginalObjectStoreFactory,
    command: OriginalUploadCommandInput,
    content: bytes,
) -> None:
    adapter = original_object_store_factory()
    state = await adapter.store.begin_or_resume_upload(command, upload_state())
    observed = await adapter.store.write_chunks(
        state.upload_id,
        chunks(*split_chunks(content, adapter.max_write_chunk_bytes)),
    )

    with pytest.raises(IntegrityMismatch):
        await adapter.store.finalize_upload(state, observed)
    with pytest.raises(IntegrityMismatch):
        await adapter.store.finalize_upload(state, observed)

    inventory, _ = await adapter.store.reconciliation_inventory()
    assert inventory == ()


async def test_bounded_read_never_returns_more_than_requested_or_configured_chunk(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(adapter)
    grant = adapter.grant_registry.issue(
        workspace_id=WORKSPACE_ID,
        environment_id=None,
        source_revision_id=SOURCE_REVISION_ID,
        object_reference=receipt.object_reference,
        purpose="read.original",
        authorization_decision_version="policy-v1",
        expires_at=adapter.now + timedelta(minutes=1),
    )
    lease = adapter.grant_registry.consume(
        grant,
        workspace_id=WORKSPACE_ID,
        environment_id=None,
        source_revision_id=SOURCE_REVISION_ID,
        object_reference=receipt.object_reference,
        now=adapter.now,
    )

    parts = [part async for part in adapter.store.open_bounded_read(lease, max_bytes=7)]

    assert b"".join(parts) == CONTENT[:7]
    assert all(0 < len(part) <= adapter.read_chunk_bytes for part in parts)
    read_observations = [
        item for item in adapter.io_observations() if item.operation == "read"
    ]
    assert read_observations
    assert all(
        0 <= item.processed_bytes <= item.limit_bytes for item in read_observations
    )


async def test_verify_is_bounded_and_returns_observed_integrity(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(adapter)

    with pytest.raises(UploadLimitExceeded):
        await adapter.store.verify(
            receipt.object_reference,
            max_bytes=receipt.object_reference.byte_length - 1,
        )
    rejected_observation = adapter.io_observations()[-1]
    assert rejected_observation.operation == "verify"
    assert (
        0 <= rejected_observation.processed_bytes <= rejected_observation.limit_bytes
    )

    result = await adapter.store.verify(
        receipt.object_reference,
        max_bytes=receipt.object_reference.byte_length,
    )
    assert result.observed_digest == hashlib.sha256(CONTENT).hexdigest()
    assert result.observed_length == len(CONTENT)
    verified_observation = adapter.io_observations()[-1]
    assert verified_observation.operation == "verify"
    assert (
        0 <= verified_observation.processed_bytes <= verified_observation.limit_bytes
    )


async def test_write_limits_raise_typed_sanitized_error(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state = await adapter.store.begin_or_resume_upload(upload_command(), upload_state())
    oversized_chunk = b"x" * (adapter.max_write_chunk_bytes + 1)

    with pytest.raises(UploadLimitExceeded) as captured:
        await adapter.store.write_chunks(state.upload_id, chunks(oversized_chunk))

    assert captured.value.redacted()["category"] == "upload_limit_exceeded"
    assert CONTENT.decode().strip() not in repr(captured.value)


async def test_total_upload_limit_stops_stream_before_publication(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    content = b"x" * (adapter.max_object_bytes + 1)
    requested_chunks = 0

    async def over_limit_stream() -> AsyncIterator[bytes]:
        nonlocal requested_chunks
        for part in split_chunks(content, adapter.max_write_chunk_bytes):
            requested_chunks += 1
            yield part
        raise AssertionError("adapter consumed beyond the first over-limit chunk")
    state = await adapter.store.begin_or_resume_upload(
        upload_command(content), upload_state()
    )

    with pytest.raises(UploadLimitExceeded):
        await adapter.store.write_chunks(
            state.upload_id,
            over_limit_stream(),
        )

    inventory, _ = await adapter.store.reconciliation_inventory()
    assert inventory == ()
    assert requested_chunks == len(split_chunks(content, adapter.max_write_chunk_bytes))


async def test_failed_write_cannot_finalize_retained_prefix_without_resume(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    command = upload_command().model_copy(
        update={"expected_sha256": None, "expected_byte_length": None}
    )
    state = await adapter.store.begin_or_resume_upload(command, upload_state())

    with pytest.raises(UploadLimitExceeded):
        await adapter.store.write_chunks(
            state.upload_id,
            chunks(b"prefix", b"x" * (adapter.max_write_chunk_bytes + 1)),
        )

    retained_prefix = ObservedContentIdentity(
        observed_sha256=hashlib.sha256(b"prefix").hexdigest(),
        observed_byte_length=len(b"prefix"),
    )
    with pytest.raises(UploadConflict):
        await adapter.store.finalize_upload(state, retained_prefix)
    with pytest.raises(UploadConflict):
        await adapter.store.write_chunks(state.upload_id, chunks())

    inventory, _ = await adapter.store.reconciliation_inventory()
    assert inventory == ()


@pytest.mark.parametrize(
    "state_name",
    [
        UploadCommandStateName.INTERRUPTED,
        UploadCommandStateName.ABORTED,
        UploadCommandStateName.INTEGRITY_CONFLICT,
    ],
)
async def test_unknown_non_staging_state_cannot_create_upload(
    original_object_store_factory: OriginalObjectStoreFactory,
    state_name: UploadCommandStateName,
) -> None:
    adapter = original_object_store_factory()
    state = upload_state().transitioned(state_name)

    with pytest.raises(StorageError) as captured:
        await adapter.store.begin_or_resume_upload(upload_command(), state)

    assert captured.value.redacted()["category"] == "upload_not_found"
    inventory, _ = await adapter.store.reconciliation_inventory()
    assert inventory == ()


async def test_abort_is_stable_and_does_not_reopen_upload(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state = await adapter.store.begin_or_resume_upload(upload_command(), upload_state())
    first = await adapter.store.abort_upload(state)
    replay = await adapter.store.abort_upload(state)
    resumed = await adapter.store.begin_or_resume_upload(upload_command(), upload_state())
    inventory, _ = await adapter.store.reconciliation_inventory()

    assert first == replay == resumed
    assert first.state is UploadCommandStateName.ABORTED
    assert inventory == ()

    observed = ObservedContentIdentity(
        observed_sha256=hashlib.sha256(b"").hexdigest(),
        observed_byte_length=0,
    )
    with pytest.raises(ObjectProtected):
        await adapter.store.finalize_upload(first, observed)


async def test_interruption_resumes_exact_staging_identity(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state = await adapter.store.begin_or_resume_upload(upload_command(), upload_state())

    async def interrupted_stream() -> AsyncIterator[bytes]:
        yield CONTENT[: adapter.max_write_chunk_bytes]
        raise UploadLimitExceeded()

    with pytest.raises(UploadLimitExceeded):
        await adapter.store.write_chunks(state.upload_id, interrupted_stream())

    interrupted = state.transitioned(UploadCommandStateName.INTERRUPTED)
    resumed = await adapter.store.begin_or_resume_upload(upload_command(), interrupted)
    observed = await adapter.store.write_chunks(
        resumed.upload_id,
        chunks(
            *split_chunks(
                CONTENT[adapter.max_write_chunk_bytes :],
                adapter.max_write_chunk_bytes,
            )
        ),
    )
    receipt = await adapter.store.finalize_upload(resumed, observed)

    assert resumed.upload_id == state.upload_id
    assert receipt.upload_id == state.upload_id
    assert await read_bytes(adapter, receipt) == CONTENT


async def test_reconciliation_required_blocks_retry_finalize_and_abort(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state = upload_state(state=UploadCommandStateName.RECONCILIATION_REQUIRED)
    observed = ObservedContentIdentity(
        observed_sha256=hashlib.sha256(CONTENT).hexdigest(),
        observed_byte_length=len(CONTENT),
    )

    with pytest.raises(ReconciliationRequired):
        await adapter.store.begin_or_resume_upload(upload_command(), state)
    with pytest.raises(ReconciliationRequired):
        await adapter.store.finalize_upload(state, observed)
    with pytest.raises(ReconciliationRequired):
        await adapter.store.abort_upload(state)

    inventory, _ = await adapter.store.reconciliation_inventory()
    assert inventory == ()


async def test_trusted_reconciliation_recovers_proven_staging_and_finalization(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state = await adapter.store.begin_or_resume_upload(upload_command(), upload_state())
    await adapter.store.write_chunks(
        state.upload_id,
        chunks(*split_chunks(CONTENT, adapter.max_write_chunk_bytes)),
    )
    blocked = state.transitioned(UploadCommandStateName.RECONCILIATION_REQUIRED)

    interrupted = await adapter.store.reconcile_upload(upload_command(), blocked)

    assert interrupted.state is UploadCommandStateName.INTERRUPTED
    assert interrupted.provisional_receipt is None

    finalized_state, _, receipt = await finalized_object(
        adapter,
        command=upload_command(idempotency_key="finalized-reconciliation"),
        state=upload_state(
            upload_id=UUID("20000000-0000-4000-8000-000000000002"),
            object_id=UUID("30000000-0000-4000-8000-000000000002"),
            receipt_id=UUID("40000000-0000-4000-8000-000000000002"),
        ),
    )
    finalized_blocked = finalized_state.transitioned(
        UploadCommandStateName.RECONCILIATION_REQUIRED
    )

    recovered = await adapter.store.reconcile_upload(
        upload_command(idempotency_key="finalized-reconciliation"),
        finalized_blocked,
    )

    assert recovered.state is UploadCommandStateName.FINALIZED
    assert recovered.provisional_receipt == receipt


async def test_unprepared_deletion_is_rejected_without_state_change(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(adapter)
    approval = DeletionApproval(
        command_id=UUID("80000000-0000-4000-8000-000000000001"),
        approval_reference=UUID("90000000-0000-4000-8000-000000000001"),
    )

    with pytest.raises(ObjectProtected):
        await adapter.store.delete_controlled(receipt.object_reference, approval)
    assert await read_bytes(adapter, receipt) == CONTENT


async def test_wrong_deletion_approval_is_rejected_without_state_change(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(adapter)
    approval = DeletionApproval(
        command_id=UUID("80000000-0000-4000-8000-000000000001"),
        approval_reference=UUID("90000000-0000-4000-8000-000000000001"),
    )

    await adapter.store.prepare_deletion(receipt.object_reference, approval)
    wrong_approval = DeletionApproval(
        command_id=approval.command_id,
        approval_reference=UUID("90000000-0000-4000-8000-000000000002"),
    )
    with pytest.raises(ObjectProtected):
        await adapter.store.delete_controlled(receipt.object_reference, wrong_approval)
    assert await read_bytes(adapter, receipt) == CONTENT


async def test_approved_deletion_replay_is_stable(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(adapter)
    approval = DeletionApproval(
        command_id=UUID("80000000-0000-4000-8000-000000000001"),
        approval_reference=UUID("90000000-0000-4000-8000-000000000001"),
    )
    await adapter.store.prepare_deletion(receipt.object_reference, approval)

    deleted = await adapter.store.delete_controlled(receipt.object_reference, approval)
    replay = await adapter.store.delete_controlled(receipt.object_reference, approval)

    assert deleted.status == "deleted"
    assert replay.status == "already_absent"


async def test_identical_bytes_remain_independent_across_scopes(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, first = await finalized_object(adapter)
    other_workspace = UUID("10000000-0000-4000-8000-000000000002")
    second_state = upload_state(
        upload_id=UUID("20000000-0000-4000-8000-000000000002"),
        object_id=UUID("30000000-0000-4000-8000-000000000002"),
        receipt_id=UUID("40000000-0000-4000-8000-000000000002"),
    )
    _, _, second = await finalized_object(
        adapter,
        command=upload_command(
            workspace_id=other_workspace,
            idempotency_key="upload-2",
        ),
        state=second_state,
    )

    assert first.observed_content == second.observed_content
    assert first.object_reference != second.object_reference
    assert first.object_reference.object_id != second.object_reference.object_id
    assert first.object_reference.storage_generation != second.object_reference.storage_generation
    assert await read_bytes(adapter, first) == CONTENT
    assert await read_bytes(adapter, second, workspace_id=other_workspace) == CONTENT


async def test_upload_retry_is_bound_to_original_workspace_and_environment(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    command = upload_command(environment_id=ENVIRONMENT_ID)
    state = await adapter.store.begin_or_resume_upload(command, upload_state())
    foreign = command.model_copy(
        update={
            "workspace_id": UUID("10000000-0000-4000-8000-000000000002"),
            "environment_id": UUID("60000000-0000-4000-8000-000000000002"),
        }
    )

    with pytest.raises(StorageError) as captured:
        await adapter.store.begin_or_resume_upload(foreign, state)

    rendered = f"{captured.value!r} {captured.value.redacted()!r}"
    assert captured.value.redacted()["category"] == "upload_not_found"
    assert str(command.workspace_id) not in rendered
    assert str(command.environment_id) not in rendered


async def test_object_reference_is_opaque_and_contains_no_scope_or_storage_details(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(adapter)
    rendered = repr(receipt.object_reference)
    fields = receipt.object_reference.model_dump()
    inventory, _ = await adapter.store.reconciliation_inventory()
    diagnostic = repr(inventory)

    assert set(fields) == {
        "schema_version",
        "object_id",
        "storage_generation",
        "digest_algorithm",
        "digest_hex",
        "byte_length",
    }
    for forbidden in (
        "path",
        "bucket",
        "provider",
        "credential",
        "token",
        str(WORKSPACE_ID),
        str(ENVIRONMENT_ID),
        CONTENT.decode().strip(),
    ):
        assert forbidden not in rendered.lower()
        assert forbidden not in diagnostic.lower()


@pytest.mark.parametrize(
    ("workspace_id", "environment_id"),
    [
        (UUID("10000000-0000-4000-8000-000000000002"), ENVIRONMENT_ID),
        (WORKSPACE_ID, UUID("60000000-0000-4000-8000-000000000002")),
    ],
    ids=["foreign-workspace", "foreign-environment"],
)
async def test_foreign_scope_cannot_observe_existence_or_content(
    original_object_store_factory: OriginalObjectStoreFactory,
    workspace_id: UUID,
    environment_id: UUID,
) -> None:
    adapter = original_object_store_factory()
    _, _, receipt = await finalized_object(
        adapter,
        command=upload_command(environment_id=ENVIRONMENT_ID),
    )

    with pytest.raises(InvalidReadGrant) as captured:
        await read_bytes(
            adapter,
            receipt,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )

    rendered = repr(captured.value)
    assert str(WORKSPACE_ID) not in rendered
    assert str(ENVIRONMENT_ID) not in rendered
    assert CONTENT.decode().strip() not in rendered

    absent_reference = receipt.object_reference.model_copy(
        update={
            "object_id": UUID("30000000-0000-4000-8000-000000000099"),
            "storage_generation": UUID("50000000-0000-4000-8000-000000000099"),
        }
    )
    absent_receipt = receipt.model_copy(update={"object_reference": absent_reference})
    with pytest.raises(StorageError) as absent:
        await read_bytes(
            adapter,
            absent_receipt,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )

    assert absent.value.redacted() == captured.value.redacted()


async def test_errors_and_diagnostics_are_typed_and_contain_no_bytes_or_scope_ids(
    original_object_store_factory: OriginalObjectStoreFactory,
) -> None:
    adapter = original_object_store_factory()
    state = await adapter.store.begin_or_resume_upload(
        upload_command(expected_sha256="0" * 64), upload_state()
    )
    observed = await adapter.store.write_chunks(
        state.upload_id,
        chunks(*split_chunks(CONTENT, adapter.max_write_chunk_bytes)),
    )

    with pytest.raises(StorageError) as captured:
        await adapter.store.finalize_upload(state, observed)

    diagnostic = captured.value.redacted()
    rendered = f"{captured.value!r} {diagnostic!r}"
    assert diagnostic["category"] == "integrity_mismatch"
    assert CONTENT.decode().strip() not in rendered
    assert CONTENT.hex() not in rendered
    assert str(WORKSPACE_ID) not in rendered
