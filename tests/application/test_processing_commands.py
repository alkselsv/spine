from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.processing_commands import (
    OrchestratedProcessingCommandService,
    ProcessingCommandRejected,
    ProcessingCommandResult,
    ProcessingCommandStatus,
    ReprocessProcessingCommand,
    RetryProcessingCommand,
)
from spine.application.document_lifecycle import ReplacementProcessingIntent
from spine.domain.sources import ProcessingAdmissionMode, RevisionKind
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")


def identifier(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"processing-command-test:{value}")


def trusted_context() -> object:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=identifier("issuer"),
        secret=b"processing-command-test-secret-32-bytes",
    )
    return boundary.worker(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        service_principal_id=identifier("service"),
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("document_processing"),
        trace_id=identifier("trace"),
    )


class _Ingestion:
    def __init__(self) -> None:
        self.created_runs = []
        self.created_items = []

    async def create_run(self, command):
        self.created_runs.append(command.run)
        return SimpleNamespace(
            value=command.run.model_copy(
                update={"idempotency_receipt_id": identifier("run-receipt")}
            ),
            replay=False,
        )

    async def create_item(self, item):
        self.created_items.append(item)
        return item


class _Sources:
    async def resolve_source(self, source_object_id):
        return SimpleNamespace(source_object_id=source_object_id)

    async def resolve_revision(self, revision_id):
        return SimpleNamespace(
            revision_id=revision_id,
            source_object_id=identifier("source"),
            kind=RevisionKind.CONTENT,
            original_sha256="a" * 64,
        )


class _Canonical:
    async def resolve_current(self, source_object_id):
        return SimpleNamespace(
            source_object_id=source_object_id,
            current_revision_id=identifier("different-current-revision"),
        )

    async def resolve_acceptance(self, acceptance_id):
        return SimpleNamespace(
            canonical_acceptance_id=acceptance_id,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            accepted_at=NOW,
        )


class _Uow:
    def __init__(self, ingestion, sources, canonical) -> None:
        self.ingestion = ingestion
        self.sources = sources
        self.canonical = canonical

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def rollback(self):
        return None

    async def commit(self):
        return None


class _ResumeIngestion(_Ingestion):
    def __init__(self) -> None:
        super().__init__()
        self.lineage = None
        self.binding = None
        self.run_attempts = 0
        self.fail_binding_once = True

    async def create_run(self, command):
        self.run_attempts += 1
        self.created_runs.append(command.run)
        return SimpleNamespace(
            value=command.run.model_copy(
                update={"idempotency_receipt_id": identifier("run-receipt")}
            ),
            replay=self.run_attempts > 1,
        )

    async def resolve_process_only_lineage(self, operation_id):
        if self.lineage is not None and self.lineage.operation_id == operation_id:
            return self.lineage
        return None

    async def resolve_item(self, item_id):
        return next(
            (item for item in self.created_items if item.ingestion_run_source_id == item_id),
            None,
        )

    async def bind_existing_revision(self, command):
        if self.fail_binding_once:
            self.fail_binding_once = False
            raise RuntimeError("injected crash after run and item commit")
        self.binding = command.binding.model_copy(
            update={
                "binding_command_digest": command.digest.value,
                "idempotency_receipt_id": identifier("binding-receipt"),
            }
        )
        return SimpleNamespace(value=self.binding, replay=False)

    async def resolve_run_head(self, run_id):
        del run_id
        return None

    async def append_transition(self, transition):
        return transition

    async def create_process_only_lineage(self, lineage):
        self.lineage = lineage
        return lineage


class _ReplacementSources:
    def __init__(self) -> None:
        self.reference = ObjectReference(
            schema_version=1,
            object_id=identifier("object"),
            storage_generation=identifier("generation"),
            digest_algorithm="sha256",
            digest_hex="a" * 64,
            byte_length=4,
        )

    async def resolve_source(self, source_object_id):
        return SimpleNamespace(source_object_id=source_object_id)

    async def resolve_revision(self, revision_id):
        return SimpleNamespace(
            revision_id=revision_id,
            source_object_id=identifier("source"),
            kind=RevisionKind.CONTENT,
            original_sha256="a" * 64,
        )

    async def resolve_receipt_associations(self, revision_id):
        return (
            SimpleNamespace(
                source_object_id=identifier("source"),
                revision_id=revision_id,
                original_reference=self.reference,
                original_receipt_kind="issue6.write_receipt",
                original_receipt_id=identifier("original-receipt"),
                original_sha256="a" * 64,
            ),
        )


class _ReplacementCanonical:
    def __init__(self) -> None:
        self.current = SimpleNamespace(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            current_revision_id=identifier("revision"),
            current_revision_kind=RevisionKind.CONTENT,
            acceptance_id=identifier("canonical-acceptance"),
        )

    async def resolve_current(self, source_object_id):
        return self.current

    async def resolve_acceptance_read(self, acceptance_id):
        return SimpleNamespace(
            is_current=True,
            current=self.current,
            processing_admission=None,
            acceptance=SimpleNamespace(
                canonical_acceptance_schema_version=1,
                canonical_acceptance_id=acceptance_id,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
            ),
        )

    async def resolve_acceptance(self, acceptance_id):
        return SimpleNamespace(
            canonical_acceptance_id=acceptance_id,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            accepted_at=NOW,
        )


class _OpaqueCanonical(_ReplacementCanonical):
    async def resolve_acceptance_read(self, acceptance_id):
        resolved = await super().resolve_acceptance_read(acceptance_id)
        return SimpleNamespace(
            is_current=True,
            current=self.current,
            acceptance=resolved.acceptance.model_copy(
                update={
                    "canonical_acceptance_schema_version": 2,
                    "processing_admission_id": identifier("opaque-admission"),
                }
            )
            if hasattr(resolved.acceptance, "model_copy")
            else SimpleNamespace(
                canonical_acceptance_schema_version=2,
                canonical_acceptance_id=acceptance_id,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                processing_admission_id=identifier("opaque-admission"),
            ),
            processing_admission=SimpleNamespace(
                processing_admission_id=identifier("opaque-admission"),
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                mode=ProcessingAdmissionMode.OPAQUE_ACCEPTANCE,
            ),
        )


class _ResumingProcessingService(OrchestratedProcessingCommandService):
    async def _process(self, **kwargs):
        self.__dict__.setdefault("resumed_processes", []).append(kwargs)

    async def status(self, *, context, operation_id):
        del context
        return ProcessingCommandResult(
            operation_id=operation_id,
            run_id=identifier("status-run"),
            item_id=identifier("status-item"),
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            trigger_kind="replacement",
            status=ProcessingCommandStatus.PROCESSING,
            replay=False,
            processing_receipt_id=None,
            failure_ids=(),
            next_action="poll",
        )


def test_reprocess_command_rejects_malformed_profile_reference() -> None:
    with pytest.raises(ValueError):
        ReprocessProcessingCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            expected_current_revision_id=identifier("revision"),
            idempotency_key="reprocess-1",
            processing_profile_reference="not a profile",
        )


@pytest.mark.asyncio
async def test_opaque_reprocess_is_rejected_before_item_parser_or_byte_read() -> None:
    ingestion = _Ingestion()
    parser_calls: list[object] = []
    reader_calls: list[object] = []

    class Reader:
        async def read_finalized(self, **kwargs):
            reader_calls.append(kwargs)
            yield b"opaque"

    service = OrchestratedProcessingCommandService(
        uow_factory=lambda _: _Uow(
            ingestion,
            _ReplacementSources(),
            _OpaqueCanonical(),
        ),
        validation_reader=Reader(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: parser_calls.append((args, kwargs)),
        context_factory=lambda operation, trace_id: trusted_context(),
        outbox_events=OutboxEventRegistry(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.reprocess(
            context=trusted_context(),
            command=ReprocessProcessingCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                expected_current_revision_id=identifier("revision"),
                idempotency_key="opaque-reprocess",
                processing_profile_reference="document-processing:r1",
            ),
        )

    assert ingestion.created_items == []
    assert reader_calls == []
    assert parser_calls == []


@pytest.mark.asyncio
async def test_retry_rejects_stale_current_revision_before_creating_a_process_item() -> None:
    ingestion = _Ingestion()
    sources = _Sources()
    canonical = _Canonical()
    context = trusted_context()
    service = OrchestratedProcessingCommandService(
        uow_factory=lambda _: _Uow(ingestion, sources, canonical),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.retry(
            context=context,
            command=RetryProcessingCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                expected_current_revision_id=identifier("revision"),
                idempotency_key="retry-1",
                parent_failure_id=identifier("failure"),
            ),
        )

    assert len(ingestion.created_runs) == 1
    assert ingestion.created_items == []


@pytest.mark.asyncio
async def test_retry_with_digit_prefixed_operation_id_uses_valid_trigger_reference() -> None:
    ingestion = _Ingestion()
    context = trusted_context()
    service = OrchestratedProcessingCommandService(
        uow_factory=lambda _: _Uow(ingestion, _Sources(), _Canonical()),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.retry(
            context=context,
            command=RetryProcessingCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                expected_current_revision_id=identifier("revision"),
                idempotency_key="issue561-original_storage-retry-issue561-r4-008",
                parent_failure_id=identifier("failure"),
            ),
        )

    assert len(ingestion.created_runs) == 1
    assert ingestion.created_runs[0].trigger_reference.startswith("operation:")


@pytest.mark.asyncio
async def test_replacement_processing_uses_internal_trigger_and_fails_closed_on_stale_head() -> None:
    ingestion = _Ingestion()
    context = trusted_context()
    service = OrchestratedProcessingCommandService(
        uow_factory=lambda _: _Uow(ingestion, _Sources(), _Canonical()),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.replacement(
            context=context,
            intent=ReplacementProcessingIntent(
                lifecycle_operation_id=identifier("canonical-acceptance"),
                canonical_acceptance_id=identifier("canonical-acceptance"),
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                idempotency_key=f"replacement-processing:{identifier('revision')}",
            ),
        )

    assert ingestion.created_runs[0].trigger_kind == "replacement"
    assert ingestion.created_items == []


@pytest.mark.asyncio
async def test_replacement_replay_repairs_a_run_without_process_only_lineage() -> None:
    ingestion = _ResumeIngestion()
    context = trusted_context()
    current_time = [NOW]
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion,
            _ReplacementSources(),
            _ReplacementCanonical(),
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        clock=lambda: current_time[0],
    )
    intent = ReplacementProcessingIntent(
        lifecycle_operation_id=identifier("canonical-acceptance"),
        canonical_acceptance_id=identifier("canonical-acceptance"),
        source_object_id=identifier("source"),
        source_revision_id=identifier("revision"),
        idempotency_key=f"replacement-processing:{identifier('revision')}",
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        await service.replacement(context=context, intent=intent)

    current_time[0] = NOW + timedelta(minutes=5)
    result = await service.replacement(context=context, intent=intent)

    assert result.replay is True
    assert ingestion.lineage is not None
    assert ingestion.lineage.trigger_kind == "replacement"
    assert len(ingestion.created_items) == 1
    assert len(service.resumed_processes) == 1


@pytest.mark.asyncio
async def test_replacement_rejects_mismatched_lifecycle_and_acceptance_identity() -> None:
    ingestion = _Ingestion()
    context = trusted_context()
    service = OrchestratedProcessingCommandService(
        uow_factory=lambda _: _Uow(ingestion, _Sources(), _Canonical()),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.replacement(
            context=context,
            intent=ReplacementProcessingIntent(
                lifecycle_operation_id=identifier("different-lifecycle-operation"),
                canonical_acceptance_id=identifier("canonical-acceptance"),
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                idempotency_key=f"replacement-processing:{identifier('revision')}",
            ),
        )

    assert ingestion.created_runs == []
