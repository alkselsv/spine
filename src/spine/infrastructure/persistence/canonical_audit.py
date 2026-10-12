"""Durable failed canonical-acceptance attempt auditing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.audit import AuditEvent, AuditOutcome, CommandAuditPayload


_FAILED_ATTEMPT_REASONS = frozenset(
    {
        "canonical_authority_conflict",
        "canonical_evidence_ineligible",
        "canonical_order_or_cas_conflict",
    }
)


class UnitOfWorkCanonicalAcceptanceFailedAttemptAudit:
    """Append one failed-attempt event after the rejected UoW has rolled back."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        context_factory: Callable[
            [CanonicalAcceptanceCommand], TrustedPersistenceContext
        ],
        event_factory: Callable[
            [CanonicalAcceptanceCommand, str, TrustedPersistenceContext], AuditEvent
        ],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._event_factory = event_factory

    async def record_failed_cas(
        self, command: CanonicalAcceptanceCommand, reason_code: str
    ) -> None:
        context = self._context_factory(command)
        event = self._event_factory(command, reason_code, context)
        async with self._uow_factory(context) as uow:
            await uow.audit.append(event)
            await uow.commit()


def build_canonical_failed_attempt_audit_event(
    command: CanonicalAcceptanceCommand,
    reason_code: str,
    context: TrustedPersistenceContext,
) -> AuditEvent:
    """Build the allowlisted, content-free failed-attempt event."""

    if reason_code not in _FAILED_ATTEMPT_REASONS:
        raise ValueError("Canonical failed-attempt reason is not allowlisted.")
    event_id = uuid5(
        NAMESPACE_URL,
        f"spine:canonical-failed-attempt:v1:{command.idempotency_key.value}:{reason_code}",
    )
    return AuditEventRegistry.with_default_families().build_event(
        audit_event_id=event_id,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="canonical_acceptance"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime.now(timezone.utc),
        outcome=AuditOutcome.REJECTED,
        reason=reason_code,
        producer_deduplication_id=(
            f"canonical-failed-attempt:{command.idempotency_key.value}:{reason_code}"
        ),
    )


__all__ = [
    "UnitOfWorkCanonicalAcceptanceFailedAttemptAudit",
    "build_canonical_failed_attempt_audit_event",
]
