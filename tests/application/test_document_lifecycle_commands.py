from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.document_lifecycle import (
    CanonicalDocumentLifecycleCommandService,
    ExistingUploadIntent,
    LifecycleOperationKind,
    LifecycleOperationStatus,
    ReplaceDocumentCommand,
    ServerDocumentLifecycleComposer,
    TombstoneDocumentCommand,
)
from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.errors import IdempotencyConflictError
from spine.domain.sources import IdentityMode, RevisionKind, SourceObject


WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
SOURCE = UUID("30000000-0000-4000-8000-000000000001")
CURRENT = UUID("40000000-0000-4000-8000-000000000001")
REPLACEMENT = UUID("50000000-0000-4000-8000-000000000001")
RECEIPT = UUID("60000000-0000-4000-8000-000000000001")
RUN = UUID("70000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _context():
    return SimpleNamespace(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        operation=PersistenceOperation("document_lifecycle"),
        trace_id=UUID("80000000-0000-4000-8000-000000000001"),
    )


def _replace(key: str = "replace-1", *, reference: str = "staged:one"):
    return ReplaceDocumentCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        expected_current_revision_id=CURRENT,
        upload_intent=ExistingUploadIntent(
            upload_identity="handbook",
            declared_media_type="text/markdown",
            declared_filename="handbook.md",
            storage_request_reference=reference,
        ),
        idempotency_key=key,
    )


class _Composer:
    def __init__(self) -> None:
        self.requests = []

    async def compose_replacement(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            kind="replacement",
            operation_id=kwargs["operation_id"],
            command_digest=kwargs["command_digest"],
        )

    async def compose_tombstone(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            kind="tombstone",
            operation_id=kwargs["operation_id"],
            command_digest=kwargs["command_digest"],
        )


class _FailingReplacementComposer(_Composer):
    async def compose_replacement(self, **kwargs):
        self.requests.append(kwargs)
        raise RuntimeError("storage finalization unavailable")


class _Coordinator:
    def __init__(self, *, revision_kind: RevisionKind = RevisionKind.CONTENT) -> None:
        self.commands = []
        self.revision_kind = revision_kind

    async def apply(self, command):
        self.commands.append(command)
        operation_id = command.operation_id
        acceptance = SimpleNamespace(
            canonical_acceptance_id=operation_id,
            source_object_id=SOURCE,
            source_revision_id=(REPLACEMENT if self.revision_kind is RevisionKind.CONTENT else UUID("50000000-0000-4000-8000-000000000002")),
            predecessor_revision_id=CURRENT,
            idempotency_receipt_id=RECEIPT,
            accepted_at=NOW,
        )
        return SimpleNamespace(
            replay=False,
            acceptance=SimpleNamespace(
                acceptance=acceptance,
                revision=SimpleNamespace(kind=self.revision_kind),
            ),
        )


class _FailingCoordinator(_Coordinator):
    async def apply(self, command):
        self.commands.append(command)
        raise RuntimeError("canonical acceptance unavailable")


class _Processing:
    def __init__(self, *, fail: bool = False) -> None:
        self.intents = []
        self.fail = fail

    async def replacement(self, *, context, intent):
        del context
        self.intents.append(intent)
        if self.fail:
            raise RuntimeError("provider diagnostics")
        return SimpleNamespace(run_id=RUN)


class _EmptyCanonical:
    async def resolve_acceptance(self, operation_id):
        del operation_id
        return None


class _EmptySources:
    async def resolve_revision(self, revision_id):
        del revision_id
        return None

    async def list_provenance(self, revision_id):
        del revision_id
        return ()


class _NoStatusUow:
    canonical = _EmptyCanonical()
    sources = _EmptySources()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        del args

    async def rollback(self):
        return None

    async def commit(self):
        return None


class _ReplayCanonical:
    def __init__(self, acceptance):
        self.acceptance = acceptance

    async def resolve_acceptance(self, operation_id):
        if operation_id == self.acceptance.canonical_acceptance_id:
            return self.acceptance
        return None


class _ReplaySources:
    def __init__(self, revision, provenance):
        self.revision = revision
        self.provenance = provenance

    async def resolve_revision(self, revision_id):
        return self.revision if revision_id == self.revision.revision_id else None

    async def list_provenance(self, revision_id):
        return (self.provenance,) if revision_id == self.revision.revision_id else ()


class _ReplayUow(_NoStatusUow):
    def __init__(self, acceptance, revision, provenance):
        self.canonical = _ReplayCanonical(acceptance)
        self.sources = _ReplaySources(revision, provenance)


class _NoLineageIngestion:
    async def resolve_process_only_lineage(self, operation_id):
        del operation_id
        return None


class _StatusUow(_NoStatusUow):
    def __init__(self, acceptance, revision, provenance):
        self.canonical = _ReplayCanonical(acceptance)
        self.sources = _ReplaySources(revision, provenance)
        self.ingestion = _NoLineageIngestion()


class _ComposerSources:
    def __init__(self, source):
        self.source = source

    async def resolve_source(self, source_object_id):
        return self.source if source_object_id == self.source.source_object_id else None


class _ComposerCanonical:
    async def resolve_current(self, source_object_id):
        return SimpleNamespace(
            source_object_id=source_object_id,
            current_revision_id=CURRENT,
        )


class _ComposerUow(_NoStatusUow):
    def __init__(self, source):
        self.sources = _ComposerSources(source)
        self.canonical = _ComposerCanonical()


@pytest.mark.asyncio
async def test_replacement_returns_canonical_receipt_and_dispatches_internal_replacement() -> None:
    composer = _Composer()
    coordinator = _Coordinator()
    processing = _Processing()
    service = CanonicalDocumentLifecycleCommandService(
        composer=composer,
        coordinator=coordinator,
        processing=processing,
        uow_factory=lambda _context: _NoStatusUow(),
        clock=lambda: NOW,
    )

    receipt = await service.replace(context=_context(), command=_replace())

    assert receipt.status is LifecycleOperationStatus.PROCESSING_DISPATCHED
    assert receipt.accepted_revision_id == REPLACEMENT
    assert receipt.processing_run_id == RUN
    assert receipt.idempotency_receipt_id == RECEIPT
    assert processing.intents[0].trigger_kind == "replacement"
    assert processing.intents[0].source_revision_id == REPLACEMENT
    assert processing.intents[0].idempotency_key == f"replacement-processing:{REPLACEMENT}"
    assert composer.requests[0]["command_digest"] == coordinator.commands[0].command_digest


@pytest.mark.asyncio
async def test_replacement_processing_failure_preserves_acceptance_for_reconciliation() -> None:
    service = CanonicalDocumentLifecycleCommandService(
        composer=_Composer(),
        coordinator=_Coordinator(),
        processing=_Processing(fail=True),
        uow_factory=lambda _context: _NoStatusUow(),
        clock=lambda: NOW,
    )

    receipt = await service.replace(context=_context(), command=_replace())

    assert receipt.accepted_revision_id == REPLACEMENT
    assert receipt.status is LifecycleOperationStatus.RECONCILIATION_REQUIRED
    assert receipt.processing_run_id is None
    assert receipt.safe_failure_code == "processing_unavailable"
    assert receipt.retryable is True


@pytest.mark.asyncio
async def test_replacement_storage_failure_creates_no_canonical_or_processing_effect() -> None:
    composer = _FailingReplacementComposer()
    coordinator = _Coordinator()
    processing = _Processing()
    service = CanonicalDocumentLifecycleCommandService(
        composer=composer,
        coordinator=coordinator,
        processing=processing,
        uow_factory=lambda _context: _NoStatusUow(),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="storage finalization unavailable"):
        await service.replace(context=_context(), command=_replace())

    assert coordinator.commands == []
    assert processing.intents == []


@pytest.mark.asyncio
async def test_replacement_canonical_failure_creates_no_processing_effect() -> None:
    coordinator = _FailingCoordinator()
    processing = _Processing()
    service = CanonicalDocumentLifecycleCommandService(
        composer=_Composer(),
        coordinator=coordinator,
        processing=processing,
        uow_factory=lambda _context: _NoStatusUow(),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="canonical acceptance unavailable"):
        await service.replace(context=_context(), command=_replace())

    assert len(coordinator.commands) == 1
    assert processing.intents == []


@pytest.mark.asyncio
async def test_tombstone_completes_without_processing_operation() -> None:
    processing = _Processing()
    service = CanonicalDocumentLifecycleCommandService(
        composer=_Composer(),
        coordinator=_Coordinator(revision_kind=RevisionKind.TOMBSTONE),
        processing=processing,
        uow_factory=lambda _context: _NoStatusUow(),
        clock=lambda: NOW,
    )
    command = TombstoneDocumentCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        expected_current_revision_id=CURRENT,
        deletion_reason="source_deleted",
        idempotency_key="delete-1",
    )

    receipt = await service.tombstone(context=_context(), command=command)

    assert receipt.status is LifecycleOperationStatus.COMPLETED
    assert receipt.processing_run_id is None
    assert receipt.completed_at == NOW
    assert processing.intents == []


@pytest.mark.asyncio
async def test_tombstone_same_key_replays_without_rechecking_the_old_current_head() -> None:
    command = TombstoneDocumentCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        expected_current_revision_id=CURRENT,
        deletion_reason="source_deleted",
        idempotency_key="delete-replay",
    )
    operation_id = CanonicalDocumentLifecycleCommandService._operation_id(
        command, LifecycleOperationKind.TOMBSTONE
    )
    accepted_revision = UUID("50000000-0000-4000-8000-000000000002")
    acceptance = SimpleNamespace(
        canonical_acceptance_id=operation_id,
        source_object_id=SOURCE,
        source_revision_id=accepted_revision,
        predecessor_revision_id=CURRENT,
        idempotency_receipt_id=RECEIPT,
        accepted_at=NOW,
        admission_decision_id=None,
    )
    revision = SimpleNamespace(
        revision_id=accepted_revision,
        kind=RevisionKind.TOMBSTONE,
    )
    provenance = SimpleNamespace(
        provenance_id=UUID("90000000-0000-4000-8000-000000000004"),
        event_digest=CanonicalDocumentLifecycleCommandService.command_digest(command).value,
        producer_kind="lifecycle",
        producer_reference="document-lifecycle",
        observer_service="document-lifecycle",
    )
    acceptance.provenance_id = provenance.provenance_id
    composer = _Composer()
    service = CanonicalDocumentLifecycleCommandService(
        composer=composer,
        coordinator=_Coordinator(revision_kind=RevisionKind.TOMBSTONE),
        processing=_Processing(),
        uow_factory=lambda _context: _ReplayUow(acceptance, revision, provenance),
        clock=lambda: NOW,
    )

    receipt = await service.tombstone(context=_context(), command=command)

    assert receipt.replay is True
    assert receipt.accepted_revision_id == accepted_revision
    assert composer.requests == []


@pytest.mark.asyncio
async def test_existing_operation_with_changed_command_digest_conflicts_before_composition() -> None:
    original = _replace(key="replace-replay", reference="staged:one")
    changed = _replace(key="replace-replay", reference="staged:changed")
    operation_id = CanonicalDocumentLifecycleCommandService._operation_id(
        original, LifecycleOperationKind.REPLACEMENT
    )
    acceptance = SimpleNamespace(
        canonical_acceptance_id=operation_id,
        source_object_id=SOURCE,
        source_revision_id=REPLACEMENT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        predecessor_revision_id=CURRENT,
        idempotency_receipt_id=RECEIPT,
        accepted_at=NOW,
        provenance_id=UUID("90000000-0000-4000-8000-000000000005"),
        admission_decision_id=UUID("90000000-0000-4000-8000-000000000006"),
    )
    revision = SimpleNamespace(revision_id=REPLACEMENT, kind=RevisionKind.CONTENT)
    provenance = SimpleNamespace(
        provenance_id=acceptance.provenance_id,
        event_digest=CanonicalDocumentLifecycleCommandService.command_digest(original).value,
        producer_kind="lifecycle",
        producer_reference="document-lifecycle",
        observer_service="document-lifecycle",
    )
    composer = _Composer()
    service = CanonicalDocumentLifecycleCommandService(
        composer=composer,
        coordinator=_Coordinator(),
        processing=_Processing(),
        uow_factory=lambda _context: _ReplayUow(acceptance, revision, provenance),
        clock=lambda: NOW,
    )

    with pytest.raises(IdempotencyConflictError):
        await service.replace(context=_context(), command=changed)

    assert composer.requests == []


@pytest.mark.asyncio
async def test_status_reconciles_a_missing_replacement_processing_operation() -> None:
    operation_id = UUID("90000000-0000-4000-8000-000000000007")
    acceptance = SimpleNamespace(
        canonical_acceptance_id=operation_id,
        source_object_id=SOURCE,
        source_revision_id=REPLACEMENT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        predecessor_revision_id=CURRENT,
        idempotency_receipt_id=RECEIPT,
        accepted_at=NOW,
    )
    revision = SimpleNamespace(revision_id=REPLACEMENT, kind=RevisionKind.CONTENT)
    provenance = SimpleNamespace(
        provenance_id=UUID("90000000-0000-4000-8000-000000000008"),
        producer_kind="lifecycle",
        producer_reference="document-lifecycle",
        observer_service="document-lifecycle",
    )
    acceptance.provenance_id = provenance.provenance_id
    processing = _Processing()
    service = CanonicalDocumentLifecycleCommandService(
        composer=_Composer(),
        coordinator=_Coordinator(),
        processing=processing,
        uow_factory=lambda _context: _StatusUow(acceptance, revision, provenance),
        clock=lambda: NOW,
    )

    receipt = await service.status(context=_context(), operation_id=operation_id)

    assert receipt is not None
    assert receipt.status is LifecycleOperationStatus.PROCESSING_DISPATCHED
    assert receipt.processing_run_id == RUN
    assert processing.intents[0].source_revision_id == REPLACEMENT


@pytest.mark.asyncio
async def test_status_does_not_reconcile_an_ordinary_content_acceptance() -> None:
    operation_id = UUID("90000000-0000-4000-8000-000000000009")
    provenance = SimpleNamespace(
        provenance_id=UUID("90000000-0000-4000-8000-00000000000a"),
        producer_kind="upload",
        producer_reference="document-upload",
        observer_service="document-ingestion",
    )
    acceptance = SimpleNamespace(
        canonical_acceptance_id=operation_id,
        source_object_id=SOURCE,
        source_revision_id=REPLACEMENT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        predecessor_revision_id=CURRENT,
        provenance_id=provenance.provenance_id,
        idempotency_receipt_id=RECEIPT,
        accepted_at=NOW,
    )
    revision = SimpleNamespace(revision_id=REPLACEMENT, kind=RevisionKind.CONTENT)
    processing = _Processing()
    service = CanonicalDocumentLifecycleCommandService(
        composer=_Composer(),
        coordinator=_Coordinator(),
        processing=processing,
        uow_factory=lambda _context: _StatusUow(acceptance, revision, provenance),
        clock=lambda: NOW,
    )

    receipt = await service.status(context=_context(), operation_id=operation_id)

    assert receipt is None
    assert processing.intents == []


def test_changed_replacement_data_changes_server_command_digest() -> None:
    first = CanonicalDocumentLifecycleCommandService.command_digest(_replace(reference="staged:one"))
    changed = CanonicalDocumentLifecycleCommandService.command_digest(_replace(reference="staged:two"))

    assert isinstance(first, CommandDigest)
    assert first != changed
    assert first.operation == PersistenceOperation("document_lifecycle")


@pytest.mark.asyncio
async def test_tombstone_composer_builds_immutable_content_free_revision() -> None:
    source = SourceObject(
        source_object_id=SOURCE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity="handbook",
        created_at=NOW,
    )

    def canonical_factory(**values):
        return CanonicalAcceptanceCommand.create(
            source=values["source"],
            revision=values["revision"],
            provenance=values["provenance"],
            expected_current_revision_id=values["expected_current_revision_id"],
            idempotency_key=values["idempotency_key"],
            canonical_acceptance_id=values["canonical_acceptance_id"],
            admission_decision_id=values["admission_decision_id"],
            access_policy_version_id=values["access_policy_version_id"],
            audit_event_id=UUID("90000000-0000-4000-8000-000000000001"),
            outbox_event_id=UUID("90000000-0000-4000-8000-000000000002"),
            accepted_at=values["accepted_at"],
        )

    composer = ServerDocumentLifecycleComposer(
        uow_factory=lambda _context: _ComposerUow(source),
        replacement_evidence=SimpleNamespace(),
        canonical_command_factory=canonical_factory,
    )
    command = TombstoneDocumentCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        expected_current_revision_id=CURRENT,
        deletion_reason="source_deleted",
        idempotency_key="delete-1",
    )
    operation_id = UUID("90000000-0000-4000-8000-000000000003")

    lifecycle = await composer.compose_tombstone(
        context=_context(),
        command=command,
        operation_id=operation_id,
        command_digest=CanonicalDocumentLifecycleCommandService.command_digest(command),
        accepted_at=NOW,
    )

    assert lifecycle.revision.kind is RevisionKind.TOMBSTONE
    assert lifecycle.revision.original_reference is None
    assert lifecycle.revision.deletion_reason == "source_deleted"
    assert lifecycle.canonical_acceptance.expected_current_revision_id == CURRENT
    assert lifecycle.canonical_acceptance.admission_decision_id is None
    assert lifecycle.receipt_association is None
