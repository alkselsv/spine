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
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.processing_commands import (
    OrchestratedProcessingCommandService,
    ProcessingCommandRejected,
    ProcessingCommandResult,
    ProcessingCommandStatus,
    ReprocessProcessingCommand,
    ReprocessingAuthorityDecision,
    RetryProcessingCommand,
)
from spine.application.document_lifecycle import ReplacementProcessingIntent
from spine.domain.ingestion import IngestionRunState
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


class _Projections:
    def __init__(self) -> None:
        self.active_publication = SimpleNamespace(
            projection_kind="qa",
            projection_run_id=identifier("active-publication-run"),
            publication_generation=3,
        )

    async def resolve_active_publication(self, projection_kind):
        assert projection_kind == "qa"
        return self.active_publication


class _Uow:
    def __init__(self, ingestion, sources, canonical, projections=None) -> None:
        self.ingestion = ingestion
        self.sources = sources
        self.canonical = canonical
        self.projections = projections or _Projections()

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
        self.run_digest = None

    async def create_run(self, command):
        self.run_attempts += 1
        if self.run_digest is not None and self.run_digest != command.digest:
            raise ConstraintConflictError("ingestion run idempotency conflict")
        self.run_digest = command.digest
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

    async def resolve_binding(self, item_id):
        if self.binding is not None and self.binding.ingestion_run_source_id == item_id:
            return self.binding
        return None

    async def resolve_processing_receipt(self, item_id):
        del item_id
        return None

    async def list_bindings_for_source_revision(self, source_object_id, source_revision_id):
        del source_object_id, source_revision_id
        return ()

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
                association_id=identifier("receipt-association"),
                source_object_id=identifier("source"),
                revision_id=revision_id,
                original_reference=self.reference,
                original_receipt_kind="issue6.write_receipt",
                original_receipt_id=identifier("original-receipt"),
                original_sha256="a" * 64,
                byte_length=4,
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
            canonical_acceptance_schema_version=2,
            canonical_acceptance_id=acceptance_id,
            accepted_generation=7,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            processing_admission_id=identifier("opaque-admission"),
            access_policy_version_id=identifier("processing-policy"),
            accepted_at=NOW,
        )

    async def resolve_processing_admission(self, admission_id):
        return SimpleNamespace(
            processing_admission_id=admission_id,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            receipt_association_id=identifier("receipt-association"),
            verified_byte_length=4,
            lifecycle_eligibility_reference=identifier("lifecycle"),
            lifecycle_eligibility_generation=3,
            retention_applicability_reference=identifier("retention"),
            retention_applicability_generation=4,
            mode=ProcessingAdmissionMode.PROCESSOR_REQUIRED,
        )


class _OpaqueCanonical(_ReplacementCanonical):
    async def resolve_processing_admission(self, admission_id):
        admission = await super().resolve_processing_admission(admission_id)
        admission.mode = ProcessingAdmissionMode.OPAQUE_ACCEPTANCE
        return admission

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


class _Authority:
    def __init__(self, *, opaque: bool) -> None:
        self.opaque = opaque
        self.calls = []

    async def authorize(self, *, context, command, processing_mode):
        self.calls.append((context, command, processing_mode))
        return ReprocessingAuthorityDecision(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            source_object_id=command.source_object_id,
            source_revision_id=command.source_revision_id,
            canonical_acceptance_id=command.canonical_acceptance_id,
            canonical_accepted_generation=7,
            processing_admission_id=command.processing_admission_id,
            receipt_association_id=command.receipt_association_id,
            verified_byte_length=command.verified_byte_length,
            processing_policy_version_id=identifier("processing-policy"),
            lifecycle_eligibility_reference=identifier("lifecycle"),
            lifecycle_eligibility_generation=3,
            retention_applicability_reference=identifier("retention"),
            retention_applicability_generation=4,
            process_only_authorization_id=(
                identifier("opaque-process-only-authorization") if self.opaque else None
            ),
            process_only_authorization_version=("v1" if self.opaque else None),
            process_only_authorization_digest=("b" * 64 if self.opaque else None),
            authorization_audit_event_id=identifier("authorization-audit"),
            authorized_at=NOW,
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
            canonical_acceptance_id=identifier("canonical-acceptance"),
            canonical_accepted_generation=7,
            processing_admission_id=identifier("processing-admission"),
            receipt_association_id=identifier("receipt-association"),
            verified_byte_length=4,
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
                canonical_acceptance_id=identifier("canonical-acceptance"),
                canonical_accepted_generation=7,
                processing_admission_id=identifier("opaque-admission"),
                receipt_association_id=identifier("receipt-association"),
                verified_byte_length=4,
                idempotency_key="opaque-reprocess",
                processing_profile_reference="document-processing:r1",
            ),
        )

    assert ingestion.created_items == []
    assert reader_calls == []
    assert parser_calls == []


@pytest.mark.asyncio
async def test_authorized_opaque_reprocess_pins_exact_process_only_lineage() -> None:
    ingestion = _ResumeIngestion()
    ingestion.fail_binding_once = False
    authority = _Authority(opaque=True)
    context = trusted_context()
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion,
            _ReplacementSources(),
            _OpaqueCanonical(),
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=authority,
        clock=lambda: NOW,
    )

    await service.reprocess(
        context=context,
        command=ReprocessProcessingCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            expected_current_revision_id=identifier("revision"),
            canonical_acceptance_id=identifier("canonical-acceptance"),
            canonical_accepted_generation=7,
            processing_admission_id=identifier("opaque-admission"),
            receipt_association_id=identifier("receipt-association"),
            verified_byte_length=4,
            idempotency_key="authorized-opaque-reprocess",
            processing_profile_reference="document-processing:r1",
        ),
    )

    assert ingestion.lineage is not None
    assert ingestion.lineage.schema_version == 2
    assert ingestion.lineage.canonical_acceptance_id == identifier(
        "canonical-acceptance"
    )
    assert ingestion.lineage.processing_admission_id == identifier(
        "opaque-admission"
    )
    assert ingestion.lineage.receipt_association_id == identifier(
        "receipt-association"
    )
    assert ingestion.lineage.process_only_authorization_id == identifier(
        "opaque-process-only-authorization"
    )
    assert len(authority.calls) == 1
    first_decision = await authority.authorize(
        context=context,
        command=authority.calls[0][1],
        processing_mode=ProcessingAdmissionMode.OPAQUE_ACCEPTANCE,
    )
    assert first_decision.matches_lineage(ingestion.lineage)
    assert not first_decision.model_copy(
        update={"retention_applicability_generation": 5}
    ).matches_lineage(ingestion.lineage)


@pytest.mark.asyncio
async def test_ordinary_reprocess_preserves_ordinary_admission_and_process_only_output() -> None:
    ingestion = _ResumeIngestion()
    ingestion.fail_binding_once = False
    context = trusted_context()
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion, _ReplacementSources(), _ReplacementCanonical()
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=_Authority(opaque=False),
        clock=lambda: NOW,
    )

    await service.reprocess(
        context=context,
        command=ReprocessProcessingCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            expected_current_revision_id=identifier("revision"),
            canonical_acceptance_id=identifier("canonical-acceptance"),
            canonical_accepted_generation=7,
            processing_admission_id=identifier("opaque-admission"),
            receipt_association_id=identifier("receipt-association"),
            verified_byte_length=4,
            idempotency_key="ordinary-reprocess",
            processing_profile_reference="document-processing:r1",
        ),
    )

    assert ingestion.lineage.process_only_authorization_id is None
    assert ingestion.lineage.schema_version == 2
    assert ingestion.lineage.trigger_kind == "reprocess"


@pytest.mark.asyncio
async def test_reprocess_accepts_historical_revision_without_moving_current_head_or_publication() -> None:
    ingestion = _ResumeIngestion()
    ingestion.fail_binding_once = False
    authority = _Authority(opaque=True)
    context = trusted_context()
    canonical = _OpaqueCanonical()
    canonical.current.current_revision_id = identifier("new-current-revision")
    projections = _Projections()
    publication_before = await projections.resolve_active_publication("qa")
    publication_snapshot = (
        publication_before.projection_run_id,
        publication_before.publication_generation,
    )
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion,
            _ReplacementSources(),
            canonical,
            projections,
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=authority,
        clock=lambda: NOW,
    )

    await service.reprocess(
        context=context,
        command=ReprocessProcessingCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            expected_current_revision_id=identifier("new-current-revision"),
            canonical_acceptance_id=identifier("canonical-acceptance"),
            canonical_accepted_generation=7,
            processing_admission_id=identifier("opaque-admission"),
            receipt_association_id=identifier("receipt-association"),
            verified_byte_length=4,
            idempotency_key="historical-opaque-reprocess",
            processing_profile_reference="document-processing:r1",
        ),
    )

    assert canonical.current.current_revision_id == identifier("new-current-revision")
    publication_after = await projections.resolve_active_publication("qa")
    assert (
        publication_after.projection_run_id,
        publication_after.publication_generation,
    ) == publication_snapshot
    assert ingestion.lineage.source_revision_id == identifier("revision")


@pytest.mark.asyncio
async def test_reprocess_ignores_binding_for_different_original_receipt() -> None:
    ingestion = _ResumeIngestion()
    ingestion.fail_binding_once = False
    ingestion.binding = SimpleNamespace(
        ingestion_run_source_id=identifier("unrelated-item"),
        original_receipt_id=identifier("substituted-original-receipt"),
        original_receipt_digest="a" * 64,
    )

    async def list_bindings(source_object_id, source_revision_id):
        del source_object_id, source_revision_id
        return (ingestion.binding,)

    ingestion.list_bindings_for_source_revision = list_bindings
    context = trusted_context()
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion, _ReplacementSources(), _OpaqueCanonical()
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=_Authority(opaque=True),
        clock=lambda: NOW,
    )

    await service.reprocess(
        context=context,
        command=ReprocessProcessingCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            expected_current_revision_id=identifier("revision"),
            canonical_acceptance_id=identifier("canonical-acceptance"),
            canonical_accepted_generation=7,
            processing_admission_id=identifier("opaque-admission"),
            receipt_association_id=identifier("receipt-association"),
            verified_byte_length=4,
            idempotency_key="receipt-bound-reprocess",
            processing_profile_reference="document-processing:r1",
        ),
    )

    assert ingestion.lineage.original_receipt_id == identifier("original-receipt")


@pytest.mark.asyncio
async def test_reprocess_parent_receipt_cannot_bypass_exact_receipt_association() -> None:
    class SubstitutedReceiptIngestion(_ResumeIngestion):
        async def resolve_processing_receipt_by_id(self, receipt_id):
            return SimpleNamespace(
                processing_receipt_id=receipt_id,
                ingestion_run_source_id=identifier("substituted-item"),
            )

        async def resolve_item(self, item_id):
            if item_id == identifier("substituted-item"):
                return SimpleNamespace(
                    ingestion_run_source_id=item_id,
                    ingestion_run_id=identifier("substituted-run"),
                )
            return await super().resolve_item(item_id)

        async def resolve_binding(self, item_id):
            if item_id == identifier("substituted-item"):
                return SimpleNamespace(
                    ingestion_run_source_id=item_id,
                    source_object_id=identifier("source"),
                    source_revision_id=identifier("revision"),
                    original_receipt_id=identifier("substituted-original-receipt"),
                    original_receipt_digest="a" * 64,
                    original_reference=ObjectReference(
                        schema_version=1,
                        object_id=identifier("substituted-object"),
                        storage_generation=identifier("substituted-generation"),
                        digest_algorithm="sha256",
                        digest_hex="a" * 64,
                        byte_length=4,
                    ),
                )
            return await super().resolve_binding(item_id)

        async def derive_run_state(self, run_id):
            del run_id
            return SimpleNamespace(state=IngestionRunState.RUNNING)

    ingestion = SubstitutedReceiptIngestion()
    context = trusted_context()
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion, _ReplacementSources(), _OpaqueCanonical()
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=_Authority(opaque=True),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.reprocess(
            context=context,
            command=ReprocessProcessingCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                expected_current_revision_id=identifier("revision"),
                canonical_acceptance_id=identifier("canonical-acceptance"),
                canonical_accepted_generation=7,
                processing_admission_id=identifier("opaque-admission"),
                receipt_association_id=identifier("receipt-association"),
                verified_byte_length=4,
                parent_processing_receipt_id=identifier("substituted-receipt"),
                idempotency_key="parent-receipt-substitution",
                processing_profile_reference="document-processing:r1",
            ),
        )


@pytest.mark.asyncio
async def test_reprocess_rejects_cross_workspace_authority_without_creating_item() -> None:
    ingestion = _ResumeIngestion()
    context = trusted_context()
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion, _ReplacementSources(), _OpaqueCanonical()
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=_Authority(opaque=True),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.reprocess(
            context=context,
            command=ReprocessProcessingCommand(
                workspace_id=identifier("foreign-workspace"),
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                expected_current_revision_id=identifier("revision"),
                canonical_acceptance_id=identifier("canonical-acceptance"),
                canonical_accepted_generation=7,
                processing_admission_id=identifier("opaque-admission"),
                receipt_association_id=identifier("receipt-association"),
                verified_byte_length=4,
                idempotency_key="cross-workspace-reprocess",
                processing_profile_reference="document-processing:r1",
            ),
        )

    assert ingestion.created_items == []


@pytest.mark.asyncio
async def test_reprocess_replay_recovers_unknown_commit_before_lineage() -> None:
    ingestion = _ResumeIngestion()
    authority = _Authority(opaque=True)
    context = trusted_context()
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion,
            _ReplacementSources(),
            _OpaqueCanonical(),
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=authority,
        clock=lambda: NOW,
    )
    command = ReprocessProcessingCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=identifier("source"),
        source_revision_id=identifier("revision"),
        expected_current_revision_id=identifier("revision"),
        canonical_acceptance_id=identifier("canonical-acceptance"),
        canonical_accepted_generation=7,
        processing_admission_id=identifier("opaque-admission"),
        receipt_association_id=identifier("receipt-association"),
        verified_byte_length=4,
        idempotency_key="recover-opaque-reprocess",
        processing_profile_reference="document-processing:r1",
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        await service.reprocess(context=context, command=command)
    recovered = await service.reprocess(context=context, command=command)

    assert recovered.replay is True
    assert ingestion.lineage is not None
    assert ingestion.lineage.command_digest == ingestion.created_runs[0].requested_scope_digest
    assert len(ingestion.created_items) == 1
    assert len(authority.calls) == 2


@pytest.mark.asyncio
async def test_reprocess_replay_rejects_changed_exact_evidence() -> None:
    ingestion = _ResumeIngestion()
    ingestion.fail_binding_once = False
    authority = _Authority(opaque=True)
    context = trusted_context()
    service = _ResumingProcessingService(
        uow_factory=lambda _: _Uow(
            ingestion,
            _ReplacementSources(),
            _OpaqueCanonical(),
        ),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        reprocessing_authority=authority,
        clock=lambda: NOW,
    )
    command = ReprocessProcessingCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=identifier("source"),
        source_revision_id=identifier("revision"),
        expected_current_revision_id=identifier("revision"),
        canonical_acceptance_id=identifier("canonical-acceptance"),
        canonical_accepted_generation=7,
        processing_admission_id=identifier("opaque-admission"),
        receipt_association_id=identifier("receipt-association"),
        verified_byte_length=4,
        idempotency_key="conflicting-opaque-reprocess",
        processing_profile_reference="document-processing:r1",
    )
    await service.reprocess(context=context, command=command)

    with pytest.raises(ConstraintConflictError):
        await service.reprocess(
            context=context,
            command=command.model_copy(
                update={"receipt_association_id": identifier("substituted-receipt")}
            ),
        )

    assert ingestion.lineage is not None
    assert ingestion.lineage.receipt_association_id == identifier(
        "receipt-association"
    )


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
