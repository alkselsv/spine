"""Canonical, disclosure-safe audit events for Q&A persistence transitions."""

from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.audit import (
    AuditEvent,
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
)


def answer_question_run_audit_event_id(
    workspace_id: UUID,
    environment_id: UUID,
    run_id: UUID,
    transition: str,
) -> UUID:
    """Return the stable audit identity for one run transition."""

    return uuid5(
        NAMESPACE_URL,
        f"spine:answer-question-run:v1:{workspace_id}:{environment_id}:{run_id}:{transition}",
    )


def answer_question_attempt_audit_event_id(
    workspace_id: UUID,
    environment_id: UUID,
    attempt_id: UUID,
    event_id: UUID,
    transition: str,
) -> UUID:
    """Return a globally stable audit identity for one scoped attempt event."""

    return uuid5(
        NAMESPACE_URL,
        (
            "spine:answer-question-attempt:v1:"
            f"{workspace_id}:{environment_id}:{attempt_id}:{event_id}:{transition}"
        ),
    )


def build_answer_question_transition_audit(
    *,
    registry: AuditEventRegistry,
    context: TrustedPersistenceContext,
    audit_event_id: UUID,
    object_type: str,
    object_id: UUID,
    transition: str,
    occurred_at: datetime,
    reason: str,
) -> AuditEvent:
    """Build an allowlisted event without answer text or provider diagnostics."""

    return registry.build_event(
        audit_event_id=audit_event_id,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        event_type="canonical.transition",
        schema_version=1,
        payload=CanonicalTransitionAuditPayload(transition=transition),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=occurred_at,
        outcome=AuditOutcome.COMMITTED,
        reason=reason,
        target=AuditObjectReference(
            object_type=object_type,
            object_id=object_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"{transition}:{object_id}",
    )


__all__ = [
    "answer_question_attempt_audit_event_id",
    "answer_question_run_audit_event_id",
    "build_answer_question_transition_audit",
]
