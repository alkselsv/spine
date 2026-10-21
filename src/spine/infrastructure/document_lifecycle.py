"""Production composition for canonical document lifecycle commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.diagnostics import AuditEventRegistry
from spine.application.document_lifecycle import (
    CanonicalDocumentLifecycleCommandService,
    ReplaceDocumentCommand,
    ReplacementLifecycleEvidence,
    ReplacementProcessingIntent,
    ServerDocumentLifecycleComposer,
    SourceLifecycleOperationReceipt,
    TombstoneDocumentCommand,
)
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
    CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceOutboxPayload,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.source_lifecycle import SourceLifecycleCoordinator
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
)
from spine.domain.sources import (
    RevisionKind,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
)


@dataclass(frozen=True, slots=True)
class _UnavailableReplacementEvidence:
    async def prepare(self, **_values: object) -> ReplacementLifecycleEvidence:
        raise RuntimeError("Replacement upload evidence is unavailable.")


@dataclass(frozen=True, slots=True)
class _UnavailableReplacementProcessing:
    async def replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        intent: ReplacementProcessingIntent,
    ) -> object:
        del context, intent
        raise RuntimeError("Replacement processing is unavailable.")


@dataclass(frozen=True, slots=True)
class ProductionDocumentLifecycleCommandService:
    """Bind each authorized request context to the existing lifecycle use case.

    ``SourceLifecycleCoordinator`` deliberately owns no ambient request state. A
    short-lived application service therefore captures the already-authorized
    context for exactly one command instead of sharing mutable context between
    concurrent HTTP requests.
    """

    uow_factory: UnitOfWorkFactory
    outbox_events: OutboxEventRegistry
    audit_events: AuditEventRegistry

    async def replace(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
    ) -> SourceLifecycleOperationReceipt:
        return await self._service(context).replace(context=context, command=command)

    async def tombstone(
        self,
        *,
        context: TrustedPersistenceContext,
        command: TombstoneDocumentCommand,
    ) -> SourceLifecycleOperationReceipt:
        return await self._service(context).tombstone(context=context, command=command)

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
    ) -> SourceLifecycleOperationReceipt | None:
        return await self._service(context).status(
            context=context,
            operation_id=operation_id,
        )

    def _service(
        self, context: TrustedPersistenceContext
    ) -> CanonicalDocumentLifecycleCommandService:
        composer = ServerDocumentLifecycleComposer(
            uow_factory=self.uow_factory,
            replacement_evidence=_UnavailableReplacementEvidence(),
            canonical_command_factory=lambda **values: _canonical_command(
                outbox_events=self.outbox_events,
                audit_events=self.audit_events,
                **values,
            ),
        )
        return CanonicalDocumentLifecycleCommandService(
            composer=composer,
            coordinator=SourceLifecycleCoordinator(
                uow_factory=self.uow_factory,
                context_factory=lambda _command: context,
            ),
            processing=_UnavailableReplacementProcessing(),
            uow_factory=self.uow_factory,
        )


def _canonical_command(
    *,
    outbox_events: OutboxEventRegistry,
    audit_events: AuditEventRegistry,
    context: TrustedPersistenceContext,
    source: SourceObject,
    revision: SourceRevision,
    provenance: SourceRevisionProvenance,
    expected_current_revision_id: UUID,
    idempotency_key: IdempotencyKey,
    canonical_acceptance_id: UUID,
    admission_decision_id: UUID | None,
    access_policy_version_id: UUID | None,
    accepted_at: datetime,
) -> CanonicalAcceptanceCommand:
    transition = (
        "source.tombstoned"
        if revision.kind is RevisionKind.TOMBSTONE
        else "source.accepted"
    )
    reason = transition.replace(".", "_")
    audit_event_id = uuid5(
        NAMESPACE_URL,
        f"spine:document-lifecycle:audit:{canonical_acceptance_id}",
    )
    outbox_event_id = uuid5(
        NAMESPACE_URL,
        f"spine:document-lifecycle:outbox:{canonical_acceptance_id}",
    )
    audit = audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        event_type="canonical.transition",
        schema_version=1,
        payload=CanonicalTransitionAuditPayload(transition=transition),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=accepted_at,
        outcome=AuditOutcome.COMMITTED,
        reason=reason,
        target=AuditObjectReference(
            object_type="source_revision",
            object_id=revision.revision_id,
            schema_version=1,
        ),
        producer_deduplication_id=(
            f"document-lifecycle:{canonical_acceptance_id}"
        ),
    )
    outbox = outbox_events.build_intent(
        event_id=outbox_event_id,
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload=CanonicalAcceptanceOutboxPayload(
            canonical_acceptance_id=canonical_acceptance_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            accepted_generation=None,
            audit_event_id=audit_event_id,
            transition=transition,
        ),
        trace_id=context.trace_id,
        aggregate=OpaqueObjectReference(
            object_type="source_object",
            object_id=source.source_object_id,
            schema_version=1,
        ),
        producer_deduplication_id=(
            f"document-lifecycle:{canonical_acceptance_id}"
        ),
    )
    return CanonicalAcceptanceCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        admission_decision_id=admission_decision_id,
        access_policy_version_id=access_policy_version_id,
        expected_current_revision_id=expected_current_revision_id,
        idempotency_key=idempotency_key,
        canonical_acceptance_id=canonical_acceptance_id,
        audit_event_id=audit_event_id,
        outbox_event_id=outbox_event_id,
        accepted_at=accepted_at,
        audit_event=audit,
        outbox_intent=outbox,
        operation=context.operation,
    )


__all__ = ["ProductionDocumentLifecycleCommandService"]
