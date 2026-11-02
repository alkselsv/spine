"""Canonical audit-writer adapter for private ASR receipts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.private_asr_corpus import (
    PrivateArtifactDeletionAuthorization,
    PrivateArtifactDeletionRequest,
    ScopedAuditReceiptPort,
)
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
            outcome=_audit_outcome(outcome),
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

    async def create_report_receipt(
        self, *, workspace_id: UUID, environment_id: UUID, report_id: UUID,
        retention_profile_ref: PrivateReference, context: TrustedPersistenceContext,
    ) -> PrivateReference:
        """Reserve a stable receipt identity; publication is appended by storage."""
        del retention_profile_ref
        context.validate_shape()
        if (
            not isinstance(context.scope, EnvironmentScope)
            or (context.scope.workspace_id, context.scope.environment_id)
            != (workspace_id, environment_id)
            or context.purpose.value != "asr_model_evaluation"
        ):
            raise ValueError("ASR report receipt scope is invalid.")
        return PrivateReference(
            reference_id=_report_publication_id(workspace_id, environment_id, report_id),
            reference_kind="private_asr_audit",
        )

    async def append_report_publication(
        self, transaction: object, *, report_id: UUID, receipt_ref: PrivateReference,
        context: TrustedPersistenceContext,
    ) -> None:
        """Append canonical success evidence to the report's UoW transaction."""
        if not isinstance(context.scope, EnvironmentScope):
            raise ValueError("ASR report receipt scope is invalid.")
        expected_id = _report_publication_id(
            context.scope.workspace_id, context.scope.environment_id, report_id
        )
        if receipt_ref != PrivateReference(
            reference_id=expected_id, reference_kind="private_asr_audit"
        ):
            raise ValueError("ASR report receipt identity is invalid.")
        event = self._events.build_event(
            audit_event_id=expected_id,
            workspace_id=context.scope.workspace_id,
            environment_id=context.scope.environment_id,
            target=AuditObjectReference(
                object_type="private_asr_report", object_id=report_id, schema_version=1
            ),
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="private_asr_report_publication"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=self._clock(),
            outcome=AuditOutcome.COMPLETED,
            reason="private_asr_report_published",
            producer_deduplication_id=f"private-asr-report:{report_id}",
        )
        await transaction.audit.append(event)  # type: ignore[attr-defined]
        await transaction.flush_audit_events()  # type: ignore[attr-defined]

    async def append_artifact_deletion(
        self,
        transaction: object,
        *,
        request: PrivateArtifactDeletionRequest,
        authorization: PrivateArtifactDeletionAuthorization,
        context: TrustedPersistenceContext,
    ) -> PrivateReference:
        """Append minimal deletion evidence in the artifact's commit UoW."""
        if (
            not isinstance(context.scope, EnvironmentScope)
            or (context.scope.workspace_id, context.scope.environment_id)
            != (request.workspace_id, request.environment_id)
            or authorization.request_digest != request.calculate_digest()
            or not authorization.authorized
        ):
            raise ValueError("ASR artifact deletion receipt is invalid.")
        event_id = uuid5(
            NAMESPACE_URL,
            f"spine:private-asr-artifact-deletion:v1:{request.workspace_id}:"
            f"{request.environment_id}:{request.artifact_id}:{request.action}",
        )
        event = self._events.build_event(
            audit_event_id=event_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            target=AuditObjectReference(
                object_type="private_asr_artifact",
                object_id=request.artifact_id,
                schema_version=1,
            ),
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="private_asr_artifact_deletion"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=request.requested_at,
            outcome=AuditOutcome.COMPLETED,
            reason=(
                "private_asr_artifact_expired"
                if request.action == "retention_expiration"
                else "private_asr_staging_discarded"
            ),
            producer_deduplication_id=(
                f"private-asr-artifact-deletion:{request.artifact_id}:{request.action}"
            ),
        )
        await transaction.audit.append(event)  # type: ignore[attr-defined]
        await transaction.flush_audit_events()  # type: ignore[attr-defined]
        return PrivateReference(reference_id=event_id, reference_kind="private_asr_audit")


def _report_publication_id(workspace_id: UUID, environment_id: UUID, report_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:private-asr-report-publication:v1:{workspace_id}:{environment_id}:{report_id}",
    )


def _audit_outcome(value: str) -> AuditOutcome:
    normalized = value.strip().lower()
    if normalized in {"denied", "rejected", "withdrawn"}:
        return AuditOutcome.REJECTED
    if normalized in {"replayed", "retry"}:
        return AuditOutcome.REPLAYED
    if normalized in {"accepted", "admitted"}:
        return AuditOutcome.ACCEPTED
    return AuditOutcome.COMPLETED


__all__ = ["PostgreSQLPrivateASRAuditReceipts"]
