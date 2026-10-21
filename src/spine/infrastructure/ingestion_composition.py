"""Production composition for durable document ingestion."""

from __future__ import annotations

from collections.abc import AsyncIterable, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from spine.application.batch_ingestion import (
    BatchDocumentIngestionOrchestrator,
    BatchDocumentInput,
    BatchDocumentUploadCommand,
)
from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    AccessPolicyActivationService,
    PolicyAuthoritySnapshot,
    access_policy_document_hash,
)
from spine.application.batch_ingestion_api import (
    BatchIngestionSnapshot,
    OrchestratedBatchDocumentIngestionService,
)
from spine.application.diagnostics import AuditEventRegistry
from spine.application.document_ingestion import (
    DocumentIngestionOrchestrator,
    DocumentUploadCommand,
    IngestionRequestPayload,
    PROCESSING_REQUEST_EVENT_TYPE,
    PROCESSING_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
)
from spine.application.ingestion import (
    BoundedContentValidator,
    OpaqueGrantContentValidationReader,
)
from spine.application.ingestion_api import (
    IngestionStatusSnapshot,
    IngestionSubmission,
    OrchestratedDocumentIngestionService,
)
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
    CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceOutboxPayload,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.storage import OriginalUploadOrchestrator
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
    CommandAuditPayload,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources import (
    AdmissionDecision,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
)
from spine.domain.sources.access_policy import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    EligibilityDisposition,
    InitialPolicyEligibilityDecision,
    PolicyBenefitClassification,
    PolicyOriginKind,
    PrincipalKind,
)
from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    FilesystemOriginalObjectStore,
    FilesystemStorageProfile,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.parsed_artifacts import (
    PostgreSQLParsedArtifactRepository,
)
from spine.ingestion.parsers import parse_document


def register_ingestion_outbox_events(registry: OutboxEventRegistry) -> None:
    registry.register(
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload_type=CanonicalAcceptanceOutboxPayload,
    )
    registry.register(
        event_type="access_policy.initial.activated",
        schema_version=1,
        payload_type=InitialPolicyActivatedPayload,
    )
    registry.register(
        event_type=PROCESSING_REQUEST_EVENT_TYPE,
        schema_version=PROCESSING_REQUEST_SCHEMA_VERSION,
        payload_type=IngestionRequestPayload,
    )
    registry.register(
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload_type=ProjectionRequestPayload,
    )


class InitialPolicyActivatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    access_policy_version_id: UUID
    policy_generation: int = 1


@dataclass(frozen=True, slots=True)
class _RequestPolicyAuthority:
    snapshot: PolicyAuthoritySnapshot

    async def resolve(self, canonical_human_id: UUID) -> PolicyAuthoritySnapshot:
        if canonical_human_id != self.snapshot.canonical_human_id:
            raise ValueError("Current policy authority is unavailable.")
        return self.snapshot

    async def classify_policy_change(
        self,
        requester: PolicyAuthoritySnapshot,
        current_policy_version_id: UUID,
        proposed_policy: AccessPolicyVersion,
    ) -> PolicyBenefitClassification:
        del requester, current_policy_version_id, proposed_policy
        return PolicyBenefitClassification.SELF_BENEFITING

    async def are_independent_humans(
        self,
        requester: PolicyAuthoritySnapshot,
        approver: PolicyAuthoritySnapshot,
    ) -> bool:
        return requester.canonical_human_id != approver.canonical_human_id


@dataclass(frozen=True, slots=True)
class _InitialUploadPolicyEnsurer:
    session_factory: object
    uow_factory: object
    request_context: TrustedPersistenceContext
    service_principal_id: UUID
    qa_service_principal_id: UUID
    outbox_events: OutboxEventRegistry
    audit_events: AuditEventRegistry
    authority_alias_issuer: str
    authority_alias_subject: str

    async def ensure(
        self,
        *,
        request: DocumentUploadCommand,
        source: SourceObject,
        context: TrustedPersistenceContext,
        activated_at: datetime,
    ) -> UUID:
        subject_id = self.request_context.acting_subject_id
        if subject_id is None:
            raise ValueError("Initial upload policy requires an interactive administrator.")
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            current = await uow.access_policies.resolve_current(source.source_object_id)
            if current is not None:
                if (
                    request.access_policy_version_id is not None
                    and request.access_policy_version_id
                    != current.access_policy_version_id
                ):
                    raise ValueError("Requested access policy is not current.")
                await uow.commit()
                return current.access_policy_version_id
            await uow.rollback()

        generation = await self._authorization_generation(request, subject_id)
        snapshot_reference = uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    "spine:initial-upload-policy-authority:v1",
                    str(subject_id),
                    str(request.workspace_id),
                    str(request.environment_id),
                    str(generation),
                )
            ),
        )
        snapshot = PolicyAuthoritySnapshot(
            canonical_human_id=subject_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            membership_generation=generation,
            authority_generation=generation,
            service_authority_generation=generation,
            is_policy_administrator=True,
            snapshot_reference=snapshot_reference,
        )
        command = self._command(
            request=request,
            source=source,
            context=context,
            subject_id=subject_id,
            generation=generation,
            snapshot_reference=snapshot_reference,
            activated_at=activated_at,
        )
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            result = await AccessPolicyActivationService(
                uow.access_policies,
                _RequestPolicyAuthority(snapshot),
            ).activate(command)
            await uow.commit()
        return result.current.access_policy_version_id

    async def _authorization_generation(
        self, request: DocumentUploadCommand, subject_id: UUID
    ) -> int:
        session = self.session_factory()  # type: ignore[operator]
        async with session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                        "set_config('spine.environment_id', :environment_id, true)"
                    ),
                    {
                        "workspace_id": str(request.workspace_id),
                        "environment_id": str(request.environment_id),
                    },
                )
                row = (
                    await session.execute(
                        text(
                            "SELECT acting_subject_id, authorization_generation "
                            "FROM spine.resolve_current_authorization_snapshot("
                            ":issuer, :subject, :workspace_id, :environment_id)"
                        ),
                        {
                            "issuer": self.authority_alias_issuer,
                            "subject": self.authority_alias_subject,
                            "workspace_id": str(request.workspace_id),
                            "environment_id": str(request.environment_id),
                        },
                    )
                ).mappings().one_or_none()
        if (
            row is None
            or row.acting_subject_id != subject_id
            or not isinstance(row.authorization_generation, int)
            or row.authorization_generation < 1
        ):
            raise ValueError("Current policy authority is unavailable.")
        return row.authorization_generation

    def _command(
        self,
        *,
        request: DocumentUploadCommand,
        source: SourceObject,
        context: TrustedPersistenceContext,
        subject_id: UUID,
        generation: int,
        snapshot_reference: UUID,
        activated_at: datetime,
    ) -> AccessPolicyActivationCommand:
        def stable(name: str) -> UUID:
            return uuid5(
                NAMESPACE_URL,
                f"spine:initial-upload-policy:v1:{source.source_object_id}:{name}",
            )

        origin_hash = hashlib.sha256(b"spine.initial-upload-policy:r1").hexdigest()
        policy = AccessPolicyVersion(
            access_policy_version_id=stable("policy"),
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            source_object_id=source.source_object_id,
            version=1,
            predecessor_policy_version_id=None,
            document_schema_version="access-policy.v1",
            document_hash="0" * 64,
            origin_kind=PolicyOriginKind.ELIGIBLE_TEMPLATE,
            origin_reference=stable("template"),
            origin_version=1,
            origin_hash=origin_hash,
            created_by_canonical_human_id=subject_id,
            created_at=activated_at,
            grants=(
                AccessGrant(
                    access_grant_id=stable("human-read"),
                    principal_kind=PrincipalKind.HUMAN,
                    principal_id=subject_id,
                    permission=AccessPermission.READ_CONTENT,
                ),
                AccessGrant(
                    access_grant_id=stable("service-process"),
                    principal_kind=PrincipalKind.SERVICE,
                    principal_id=self.service_principal_id,
                    permission=AccessPermission.PROCESS_CONTENT,
                    purpose=request.purpose,
                    operation="canonical_acceptance",
                ),
                AccessGrant(
                    access_grant_id=stable("service-retrieve"),
                    principal_kind=PrincipalKind.SERVICE,
                    principal_id=self.qa_service_principal_id,
                    permission=AccessPermission.PROCESS_CONTENT,
                    purpose="answer_question",
                    operation="retrieve_context",
                ),
            ),
        )
        policy = policy.model_copy(
            update={"document_hash": access_policy_document_hash(policy)}
        )
        audit_id = stable("audit")
        outbox_id = stable("outbox")
        activation_id = stable("activation")
        eligibility = InitialPolicyEligibilityDecision(
            decision_id=stable("eligibility"),
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            source_object_id=source.source_object_id,
            policy_version_id=policy.access_policy_version_id,
            policy_hash=policy.document_hash,
            origin_kind=policy.origin_kind,
            origin_reference=policy.origin_reference,
            origin_version=policy.origin_version,
            origin_hash=policy.origin_hash,
            membership_generation=generation,
            authority_generation=generation,
            service_authority_generation=generation,
            disposition=EligibilityDisposition.ELIGIBLE,
            reason_code="eligible_template",
            evaluator_contract_version="initial-policy.v1",
            authority_snapshot_reference=snapshot_reference,
            audit_event_id=stable("eligibility-audit"),
            decided_at=activated_at,
        )
        audit = self.audit_events.build_event(
            audit_event_id=audit_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="access_policy.activate"),
            origin=ContextOrigin.WORKER,
            acting_subject_id=None,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=activated_at,
            outcome=AuditOutcome.ACCEPTED,
            reason="initial_policy_activated",
            target=AuditObjectReference(
                object_type="source_object",
                object_id=source.source_object_id,
                schema_version=1,
            ),
            producer_deduplication_id=f"initial-policy:{source.source_object_id}",
        )
        outbox = self.outbox_events.build_intent(
            event_id=outbox_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            event_type="access_policy.initial.activated",
            schema_version=1,
            payload=InitialPolicyActivatedPayload(
                source_object_id=source.source_object_id,
                access_policy_version_id=policy.access_policy_version_id,
            ),
            trace_id=context.trace_id,
            aggregate=OpaqueObjectReference(
                object_type="source_object",
                object_id=source.source_object_id,
                schema_version=1,
            ),
            producer_deduplication_id=f"initial-policy:{source.source_object_id}",
        )
        return AccessPolicyActivationCommand(
            policy=policy,
            requester_canonical_human_id=subject_id,
            activator_canonical_human_id=subject_id,
            expected_policy_version_id=None,
            expected_policy_generation=0,
            expected_cas_version=0,
            activation_id=activation_id,
            idempotency_key=IdempotencyKey(
                f"initial-upload-policy-{source.source_object_id}"
            ),
            audit_event_id=audit_id,
            outbox_event_id=outbox_id,
            activated_at=activated_at,
            audit_event=audit,
            outbox_intent=outbox,
            eligibility=eligibility,
        )


@dataclass(frozen=True, slots=True)
class ProductionIngestionComposition:
    """The two HTTP adapters backed by one durable ingestion implementation."""

    document_service: "ProductionDocumentIngestionService"
    batch_service: "ProductionBatchDocumentIngestionService"


@dataclass(frozen=True, slots=True)
class _SharedIngestion:
    session_factory: object
    uow_factory: object
    boundary: TrustedContextBoundary
    service_principal_id: UUID
    qa_service_principal_id: UUID
    uploader: OriginalUploadOrchestrator
    reader: OpaqueGrantContentValidationReader
    validator: BoundedContentValidator
    parsed_artifacts: PostgreSQLParsedArtifactRepository
    outbox_events: OutboxEventRegistry
    audit_events: AuditEventRegistry
    authority_alias_issuer: str
    authority_alias_subject: str

    def context_factory(
        self,
        source: TrustedPersistenceContext,
        *,
        purpose: str = "document.ingestion.v1",
    ):
        scope = source.scope
        if not isinstance(scope, EnvironmentScope):
            raise ValueError("document ingestion requires an environment scope")

        def issue(operation: PersistenceOperation, trace_id: UUID) -> TrustedPersistenceContext:
            return self.boundary.worker(
                scope=scope,
                service_principal_id=self.service_principal_id,
                purpose=PersistencePurpose(purpose),
                operation=operation,
                trace_id=trace_id,
            )

        return issue

    def orchestrator(
        self,
        context: TrustedPersistenceContext,
        *,
        purpose: str = "document.ingestion.v1",
    ) -> DocumentIngestionOrchestrator:
        context_factory = self.context_factory(context, purpose=purpose)
        return DocumentIngestionOrchestrator(
            uow_factory=self.uow_factory,  # type: ignore[arg-type]
            uploader=self.uploader,
            validation_reader=self.reader,
            validator=self.validator,
            parsed_artifacts=self.parsed_artifacts,
            context_factory=context_factory,
            access_policy_ensurer=_InitialUploadPolicyEnsurer(
                session_factory=self.session_factory,
                uow_factory=self.uow_factory,
                request_context=context,
                service_principal_id=self.service_principal_id,
                qa_service_principal_id=self.qa_service_principal_id,
                outbox_events=self.outbox_events,
                audit_events=self.audit_events,
                authority_alias_issuer=self.authority_alias_issuer,
                authority_alias_subject=self.authority_alias_subject,
            ),
            canonical_command_factory=lambda **values: _canonical_command(
                outbox_events=self.outbox_events,
                audit_events=self.audit_events,
                **values,
            ),
            outbox_events=self.outbox_events,
            parser=parse_document,
        )


@dataclass(frozen=True, slots=True)
class ProductionDocumentIngestionService:
    shared: _SharedIngestion

    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: DocumentUploadCommand,
        chunks: AsyncIterable[bytes],
    ) -> IngestionSubmission:
        service = OrchestratedDocumentIngestionService(
            orchestrator=self.shared.orchestrator(context, purpose=command.purpose),
            uow_factory=self.shared.uow_factory,  # type: ignore[arg-type]
        )
        return await service.submit(context=context, command=command, chunks=chunks)

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        item_id: UUID,
    ) -> IngestionStatusSnapshot | None:
        service = OrchestratedDocumentIngestionService(
            orchestrator=self.shared.orchestrator(context),
            uow_factory=self.shared.uow_factory,  # type: ignore[arg-type]
        )
        return await service.status(context=context, run_id=run_id, item_id=item_id)


@dataclass(frozen=True, slots=True)
class ProductionBatchDocumentIngestionService:
    shared: _SharedIngestion

    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: BatchDocumentUploadCommand,
        items: Sequence[BatchDocumentInput],
    ) -> BatchIngestionSnapshot:
        orchestrator = BatchDocumentIngestionOrchestrator(
            uow_factory=self.shared.uow_factory,  # type: ignore[arg-type]
            item_processor=self.shared.orchestrator(
                context,
                purpose=command.items[0].purpose,
            ),
            context_factory=self.shared.context_factory(context),
        )
        service = OrchestratedBatchDocumentIngestionService(
            orchestrator=orchestrator,
            uow_factory=self.shared.uow_factory,  # type: ignore[arg-type]
        )
        return await service.submit(context=context, command=command, items=items)

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
    ) -> BatchIngestionSnapshot | None:
        orchestrator = BatchDocumentIngestionOrchestrator(
            uow_factory=self.shared.uow_factory,  # type: ignore[arg-type]
            item_processor=self.shared.orchestrator(context),
            context_factory=self.shared.context_factory(context),
        )
        service = OrchestratedBatchDocumentIngestionService(
            orchestrator=orchestrator,
            uow_factory=self.shared.uow_factory,  # type: ignore[arg-type]
        )
        return await service.status(context=context, run_id=run_id)


def build_production_ingestion(
    *,
    session_factory: object,
    uow_factory: object,
    context_verifier: object,
    persistence_boundary: TrustedContextBoundary,
    service_principal_id: UUID,
    qa_service_principal_id: UUID,
    storage_root: str | Path,
    outbox_events: OutboxEventRegistry,
    audit_events: AuditEventRegistry,
    max_bytes: int,
    authority_alias_issuer: str,
    authority_alias_subject: str,
) -> ProductionIngestionComposition:
    grants = AuthorizedReadGrantRegistry()
    object_store = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(
            root=storage_root,
            max_chunk_bytes=max_bytes,
            max_object_bytes=max_bytes,
        ),
        grant_registry=grants,
    )
    shared = _SharedIngestion(
        session_factory=session_factory,
        uow_factory=uow_factory,
        boundary=persistence_boundary,
        service_principal_id=service_principal_id,
        qa_service_principal_id=qa_service_principal_id,
        uploader=OriginalUploadOrchestrator(
            uow_factory=uow_factory,  # type: ignore[arg-type]
            object_store=object_store,
            identity_factory=uuid4,
        ),
        reader=OpaqueGrantContentValidationReader(
            object_store=object_store,
            grant_registry=grants,
        ),
        validator=BoundedContentValidator(
            max_bytes=max_bytes,
            max_chunk_bytes=max_bytes,
            producer="admission:r1",
        ),
        parsed_artifacts=PostgreSQLParsedArtifactRepository(
            session_factory,  # type: ignore[arg-type]
            context_factory=lambda workspace_id, environment_id: persistence_boundary.worker(
                scope=EnvironmentScope(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                ),
                service_principal_id=service_principal_id,
                purpose=PersistencePurpose("document.ingestion.v1"),
                operation=PersistenceOperation("parsed_artifact"),
                trace_id=uuid4(),
            ),
            context_verifier=context_verifier,  # type: ignore[arg-type]
        ),
        outbox_events=outbox_events,
        audit_events=audit_events,
        authority_alias_issuer=authority_alias_issuer,
        authority_alias_subject=authority_alias_subject,
    )
    return ProductionIngestionComposition(
        document_service=ProductionDocumentIngestionService(shared),
        batch_service=ProductionBatchDocumentIngestionService(shared),
    )


def _canonical_command(
    *,
    outbox_events: OutboxEventRegistry,
    audit_events: AuditEventRegistry,
    request: DocumentUploadCommand,
    source: SourceObject,
    revision: SourceRevision,
    provenance: SourceRevisionProvenance,
    admission: AdmissionDecision,
    context: TrustedPersistenceContext,
    accepted_at: datetime,
) -> CanonicalAcceptanceCommand:
    acceptance_id = uuid5(NAMESPACE_URL, f"spine:ingestion:acceptance:{revision.revision_id}")
    audit_id = uuid5(NAMESPACE_URL, f"spine:ingestion:audit:{revision.revision_id}")
    event_id = uuid5(NAMESPACE_URL, f"spine:ingestion:outbox:{revision.revision_id}")
    audit = audit_events.build_event(
        audit_event_id=audit_id,
        workspace_id=request.workspace_id,
        environment_id=request.environment_id,
        event_type="canonical.transition",
        schema_version=1,
        payload=CanonicalTransitionAuditPayload(transition="source.accepted"),
        origin=ContextOrigin.WORKER,
        acting_subject_id=None,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=accepted_at,
        outcome=AuditOutcome.COMMITTED,
        reason="source_accepted",
        target=AuditObjectReference(
            object_type="source_revision",
            object_id=revision.revision_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"document-ingestion:{revision.revision_id}",
    )
    outbox = outbox_events.build_intent(
        event_id=event_id,
        workspace_id=request.workspace_id,
        environment_id=request.environment_id,
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload=CanonicalAcceptanceOutboxPayload(
            canonical_acceptance_id=acceptance_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            accepted_generation=None,
            audit_event_id=audit_id,
            transition="source.accepted",
        ),
        trace_id=context.trace_id,
        aggregate=OpaqueObjectReference(
            object_type="source_object",
            object_id=source.source_object_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"document-ingestion:{revision.revision_id}",
    )
    return CanonicalAcceptanceCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        admission_decision_id=admission.decision_id,
        access_policy_version_id=request.access_policy_version_id,
        expected_current_revision_id=request.expected_current_revision_id,
        idempotency_key=IdempotencyKey(f"canonical:{request.idempotency_key}"),
        canonical_acceptance_id=acceptance_id,
        audit_event_id=audit_id,
        outbox_event_id=event_id,
        accepted_at=accepted_at,
        audit_event=audit,
        outbox_intent=outbox,
        operation=context.operation,
    )


__all__ = [
    "ProductionBatchDocumentIngestionService",
    "ProductionDocumentIngestionService",
    "ProductionIngestionComposition",
    "build_production_ingestion",
    "register_ingestion_outbox_events",
]
