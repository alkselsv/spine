"""Canonical audit-writer adapter for private ASR receipts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.private_asr_corpus import ScopedAuditReceiptPort
from spine.domain.audit import AuditObjectReference, AuditOutcome, CommandAuditPayload
from spine.domain.evaluations.private_asr_corpus import PrivateReference


class PostgreSQLPrivateASRAuditReceipts(ScopedAuditReceiptPort):
    """Persist content-free ASR receipts through Spine's canonical UoW audit writer."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        events: AuditEventRegistry,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._events = events
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def receipt(
        self,
        *,
        operation: str,
        source_revision_id: UUID | None,
        outcome: str,
        context: TrustedPersistenceContext,
    ) -> PrivateReference:
        context.validate_shape()
        if not isinstance(context.scope, EnvironmentScope):
            raise ValueError("ASR audit receipt requires an environment scope.")
        if context.purpose.value != "asr_model_evaluation":
            raise ValueError("ASR audit receipt purpose is invalid.")
        # Scope and operation make a stable retry identity without retaining the
        # source revision identifier or any content-bearing payload.
        identity = (
            f"spine:private-asr-audit:v1:{context.scope.workspace_id}:"
            f"{context.scope.environment_id}:{context.trace_id}:{operation}:{outcome}:"
            f"{source_revision_id or 'none'}"
        )
        event_id = uuid5(NAMESPACE_URL, identity)
        event = self._events.build_event(
            audit_event_id=event_id,
            workspace_id=context.scope.workspace_id,
            environment_id=context.scope.environment_id,
            target=(
                AuditObjectReference(
                    object_type="source_revision", object_id=source_revision_id,
                    schema_version=1,
                ) if source_revision_id is not None else None
            ),
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="private_asr_storage"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=self._clock(),
            outcome=AuditOutcome.RECORDED,
            reason="private_asr_storage_receipt",
            producer_deduplication_id=(
                f"private-asr:{context.trace_id}:{operation}:{outcome}:"
                f"{source_revision_id or 'none'}"
            ),
        )
        async with self._uow_factory(context) as uow:
            await uow.audit.append(event)
            await uow.commit()
        return PrivateReference(reference_id=event_id, reference_kind="private_asr_audit")


__all__ = ["PostgreSQLPrivateASRAuditReceipts"]
