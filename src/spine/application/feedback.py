"""Application seam for administrator feedback on immutable Q&A references."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.diagnostics.audit import (
    AuditEventRegistry,
    AuditObjectReference,
    AuditOutcome,
    AuditWriter,
)
from spine.application.diagnostics.context import DiagnosticContext
from spine.application.diagnostics.events import (
    DiagnosticEventRegistry,
    DiagnosticSeverity,
    DiagnosticSink,
    FeedbackDiagnosticPayload,
    emit_diagnostic_safely,
)
from spine.application.feedback_errors import FeedbackTargetNotFound, InvalidFeedbackRequest
from spine.domain.audit import FeedbackAuditPayload
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.feedback import FeedbackOutcome, FeedbackRecord, FeedbackTargetKind


FEEDBACK_OPERATION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class FeedbackRequester:
    subject_id: UUID
    authorization_generation: int = 1

    def __post_init__(self) -> None:
        if self.subject_id.int == 0 or self.authorization_generation < 1:
            raise ValueError("feedback requester identity is invalid")


@dataclass(frozen=True, slots=True)
class FeedbackTargetReference:
    """Content-free target identity resolved by the owning Q&A authority."""

    workspace_id: UUID
    environment_id: UUID
    target_kind: FeedbackTargetKind
    target_id: UUID
    run_id: UUID
    message_id: UUID | None = None
    citation_id: UUID | None = None

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "environment_id",
            "target_id",
            "run_id",
            "message_id",
            "citation_id",
        ):
            value = getattr(self, name)
            if value is not None and value.int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if self.target_kind is FeedbackTargetKind.CITATION and self.citation_id != self.target_id:
            raise ValueError("citation target must identify its exact citation")


class FeedbackTargetResolver(Protocol):
    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        requester: FeedbackRequester,
        target_kind: FeedbackTargetKind,
        target_id: UUID,
        run_id: UUID,
        message_id: UUID | None,
        citation_id: UUID | None,
    ) -> FeedbackTargetReference | None: ...


@dataclass(frozen=True, slots=True)
class FeedbackSubmissionResult:
    command_id: UUID
    feedback_id: UUID
    feedback_version: int
    target_kind: FeedbackTargetKind
    target_id: UUID
    run_id: UUID
    message_id: UUID | None
    citation_id: UUID | None
    outcome: FeedbackOutcome
    replay: bool
    audit_event_id: UUID | None = None


class FeedbackRepository(Protocol):
    async def create(
        self,
        record: FeedbackRecord,
        *,
        command_id: UUID,
        idempotency_key: IdempotencyKey,
        digest: CommandDigest,
    ) -> FeedbackSubmissionResult: ...


class FeedbackService(Protocol):
    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: FeedbackRequester,
        run_id: UUID,
        message_id: UUID | None,
        target_kind: FeedbackTargetKind,
        target_id: UUID,
        citation_id: UUID | None,
        outcome: FeedbackOutcome,
        comment: str | None,
        reason: str | None,
        idempotency_key: IdempotencyKey,
    ) -> FeedbackSubmissionResult: ...


@dataclass(frozen=True, slots=True)
class PersistedFeedbackService:
    repository: FeedbackRepository
    target_resolver: FeedbackTargetResolver
    audit_writer: AuditWriter
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    feedback_id_factory: Callable[[], UUID] | None = None
    command_id_factory: Callable[[], UUID] = uuid4
    diagnostic_registry: DiagnosticEventRegistry | None = None
    diagnostic_sink: DiagnosticSink | None = None

    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: FeedbackRequester,
        run_id: UUID,
        message_id: UUID | None,
        target_kind: FeedbackTargetKind,
        target_id: UUID,
        citation_id: UUID | None,
        outcome: FeedbackOutcome,
        comment: str | None,
        reason: str | None,
        idempotency_key: IdempotencyKey,
    ) -> FeedbackSubmissionResult:
        workspace_id, environment_id = _scope(context)
        target = await self.target_resolver.resolve(
            workspace_id=workspace_id,
            environment_id=environment_id,
            requester=requester,
            target_kind=target_kind,
            target_id=target_id,
            run_id=run_id,
            message_id=message_id,
            citation_id=citation_id,
        )
        if target is None or not _target_matches(
            target,
            workspace_id=workspace_id,
            environment_id=environment_id,
            target_kind=target_kind,
            target_id=target_id,
            run_id=run_id,
            message_id=message_id,
            citation_id=citation_id,
        ):
            raise FeedbackTargetNotFound()

        try:
            record = FeedbackRecord(
                feedback_id=(
                    self.feedback_id_factory()
                    if self.feedback_id_factory is not None
                    else _stable_feedback_id(workspace_id, environment_id, idempotency_key)
                ),
                workspace_id=workspace_id,
                environment_id=environment_id,
                subject_id=requester.subject_id,
                trace_id=context.trace_id,
                run_id=run_id,
                message_id=message_id,
                target_kind=target_kind,
                target_id=target_id,
                citation_id=citation_id,
                outcome=outcome,
                comment=comment,
                reason=reason,
                created_at=self.clock(),
            )
        except (TypeError, ValueError):
            raise InvalidFeedbackRequest() from None
        audit_event_id = await self._record_audit(context, record)
        result = await self.repository.create(
            record,
            command_id=self.command_id_factory(),
            idempotency_key=idempotency_key,
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=FEEDBACK_OPERATION_SCHEMA_VERSION,
                payload={
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                    "subject_id": requester.subject_id,
                    "run_id": run_id,
                    "message_id": message_id,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "citation_id": citation_id,
                    "outcome": outcome,
                    "comment": comment,
                    "reason": reason,
                },
            ),
        )
        if not result.replay:
            await self._record_diagnostic(context, result)
        return replace(result, audit_event_id=audit_event_id)

    async def _record_audit(
        self,
        context: TrustedPersistenceContext,
        record: FeedbackRecord,
    ) -> UUID:
        scope = context.scope
        registry = AuditEventRegistry.with_default_families()
        event_id = uuid5(
            NAMESPACE_URL,
            f"spine:feedback-audit:v1:{record.feedback_id}",
        )
        event = registry.build_event(
            audit_event_id=event_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            event_type="feedback.recorded",
            schema_version=1,
            payload=FeedbackAuditPayload(feedback_type=record.outcome.value),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=self.clock(),
            outcome=AuditOutcome.RECORDED,
            reason="feedback_recorded",
            target=AuditObjectReference(
                object_type=record.target_kind.value,
                object_id=record.target_id,
                schema_version=1,
            ),
            producer_deduplication_id=f"feedback:{record.feedback_id}",
        )
        return await self.audit_writer.append(event)

    async def _record_diagnostic(
        self,
        context: TrustedPersistenceContext,
        result: FeedbackSubmissionResult,
    ) -> None:
        if self.diagnostic_registry is None or self.diagnostic_sink is None:
            return
        event = self.diagnostic_registry.build_event(
            event_type="feedback.recorded",
            schema_version=1,
            severity=DiagnosticSeverity.INFO,
            occurred_at=self.clock(),
            diagnostic_context=DiagnosticContext(trace_id=context.trace_id),
            workspace_id=context.scope.workspace_id,
            environment_id=context.scope.environment_id,
            payload=FeedbackDiagnosticPayload(
                feedback_id=result.feedback_id,
                target_kind=result.target_kind,
                outcome=result.outcome,
                run_id=result.run_id,
                message_id=result.message_id,
            ),
        )
        await emit_diagnostic_safely(self.diagnostic_sink, event)


def _target_matches(
    target: FeedbackTargetReference,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    target_kind: FeedbackTargetKind,
    target_id: UUID,
    run_id: UUID,
    message_id: UUID | None,
    citation_id: UUID | None,
) -> bool:
    return (
        target.workspace_id == workspace_id
        and target.environment_id == environment_id
        and target.target_kind is target_kind
        and target.target_id == target_id
        and target.run_id == run_id
        and target.message_id == message_id
        and target.citation_id == citation_id
    )


def _stable_feedback_id(
    workspace_id: UUID,
    environment_id: UUID,
    idempotency_key: IdempotencyKey,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:feedback:v1:{workspace_id}:{environment_id}:{idempotency_key.value}",
    )


def _scope(context: TrustedPersistenceContext) -> tuple[UUID, UUID]:
    try:
        context.validate_shape()
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Feedback requires an environment scope")
        return scope.workspace_id, scope.environment_id
    except InvalidPersistenceContextError:
        raise
    except Exception:
        raise InvalidPersistenceContextError("Persistence context is invalid.") from None


__all__ = [
    "FEEDBACK_OPERATION_SCHEMA_VERSION",
    "FeedbackRepository",
    "FeedbackRequester",
    "FeedbackService",
    "FeedbackSubmissionResult",
    "FeedbackTargetNotFound",
    "FeedbackTargetReference",
    "FeedbackTargetResolver",
    "InvalidFeedbackRequest",
    "PersistedFeedbackService",
]
