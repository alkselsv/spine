"""Durable, disclosure-safe records for governed ``answer_question`` runs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.capabilities.answer_question_execution import AnswerQuestionExecutionPin
from spine.domain.common import ActorRef, EnvironmentKind, ValidatedDefinitionModel
from spine.domain.context_bundles import ContextBundleRef


ANSWER_QUESTION_RUN_SCHEMA = "answer-question-run:v1"
ANSWER_QUESTION_ATTEMPT_SCHEMA = "answer-question-attempt:v1"
_DIGEST = r"^[0-9a-f]{64}$"


class DiagnosticIdentity(Protocol):
    trace_id: UUID
    correlation_id: UUID | None
    causation_id: UUID | None


class AgentResultValue(Protocol):
    status: object
    output_artifact_ids: tuple[UUID, ...]


def _require_id(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _unique_ids(value: tuple[UUID, ...], field_name: str) -> tuple[UUID, ...]:
    if any(identifier.int == 0 for identifier in value):
        raise ValueError(f"{field_name} must contain non-zero UUIDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must be unique")
    return value


class AnswerQuestionRunState(str, Enum):
    ADMITTED = "admitted"
    DISPATCH_PENDING = "dispatch_pending"
    RUNNING = "running"
    COMPLETED = "completed"


class AnswerQuestionAttemptState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SafeAttemptReason(str, Enum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ACCESS_DENIED = "access_denied"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    VALIDATION_FAILED = "validation_failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    CANCELLED_BY_REQUEST = "cancelled_by_request"
    INTERNAL_FAILURE = "internal_failure"


class AnswerQuestionAttemptEventKind(str, Enum):
    STARTED = "started"
    CONTEXT_BOUND = "context_bound"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AnswerQuestionDiagnosticContext(ValidatedDefinitionModel):
    """Persisted diagnostic identity; carries no protected diagnostic payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @field_validator("trace_id", "correlation_id", "causation_id")
    @classmethod
    def validate_id(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is not None:
            _require_id(value, info.field_name)
        return value

    @classmethod
    def from_identity(cls, value: DiagnosticIdentity) -> AnswerQuestionDiagnosticContext:
        return cls(
            trace_id=value.trace_id,
            correlation_id=value.correlation_id,
            causation_id=value.causation_id,
        )


class AnswerQuestionUsage(ValidatedDefinitionModel):
    """Exact aggregate usage; provider payloads and free-form labels are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    model_invocations: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def require_currency_for_cost(self) -> AnswerQuestionUsage:
        if (self.cost == 0) != (self.currency is None):
            raise ValueError("currency must be present exactly when cost is non-zero")
        return self


class AgentResultRef(ValidatedDefinitionModel):
    """Opaque reference to the runtime-owned, already-persisted AgentResult."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    result_type: Literal["agent_invocation"] = "agent_invocation"
    result_id: UUID
    schema_version: Literal[1] = 1

    @field_validator("result_id")
    @classmethod
    def validate_result_id(cls, value: UUID) -> UUID:
        return _require_id(value, "result_id")


class AnswerQuestionAttemptEvent(ValidatedDefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    event_id: UUID
    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    kind: AnswerQuestionAttemptEventKind
    occurred_at: datetime
    reason: SafeAttemptReason | None = None

    @field_validator("event_id", "workspace_id", "environment_id")
    @classmethod
    def validate_event_id(cls, value: UUID) -> UUID:
        return _require_id(value, "event_id")

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")

    @model_validator(mode="after")
    def validate_reason(self) -> AnswerQuestionAttemptEvent:
        if self.kind in {
            AnswerQuestionAttemptEventKind.FAILED,
            AnswerQuestionAttemptEventKind.CANCELLED,
        }:
            if self.reason is None:
                raise ValueError("failed and cancelled events require a safe reason")
        elif self.reason is not None:
            raise ValueError("non-failure events cannot carry a reason")
        return self


class AnswerQuestionRun(ValidatedDefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal[ANSWER_QUESTION_RUN_SCHEMA] = ANSWER_QUESTION_RUN_SCHEMA
    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    subject: ActorRef
    idempotency_key: str = Field(min_length=1, max_length=255)
    command_digest: str = Field(pattern=_DIGEST)
    input_artifact_ids: tuple[UUID, ...]
    output_artifact_ids: tuple[UUID, ...] = ()
    diagnostic_context: AnswerQuestionDiagnosticContext
    state: AnswerQuestionRunState = AnswerQuestionRunState.RUNNING
    outcome: AnswerQuestionOutcome | None = None
    final_attempt_id: UUID | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def start(
        cls,
        *,
        run_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        environment: EnvironmentKind,
        subject: ActorRef,
        idempotency_key: str,
        command_digest: str,
        input_artifact_ids: tuple[UUID, ...],
        diagnostic_context: DiagnosticIdentity,
        created_at: datetime,
    ) -> AnswerQuestionRun:
        return cls(
            run_id=run_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=environment,
            subject=subject,
            idempotency_key=idempotency_key,
            command_digest=command_digest,
            input_artifact_ids=input_artifact_ids,
            diagnostic_context=AnswerQuestionDiagnosticContext.from_identity(
                diagnostic_context
            ),
            created_at=created_at,
        )

    @classmethod
    def admit(
        cls,
        *,
        run_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        environment: EnvironmentKind,
        subject: ActorRef,
        idempotency_key: str,
        command_digest: str,
        input_artifact_ids: tuple[UUID, ...],
        diagnostic_context: DiagnosticIdentity,
        created_at: datetime,
    ) -> AnswerQuestionRun:
        """Create a durable run before dispatch or execution can begin."""

        return cls(
            run_id=run_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=environment,
            subject=subject,
            idempotency_key=idempotency_key,
            command_digest=command_digest,
            input_artifact_ids=input_artifact_ids,
            diagnostic_context=AnswerQuestionDiagnosticContext.from_identity(
                diagnostic_context
            ),
            state=AnswerQuestionRunState.ADMITTED,
            created_at=created_at,
        )

    def complete(
        self,
        *,
        outcome: AnswerQuestionOutcome,
        final_attempt_id: UUID,
        output_artifact_ids: tuple[UUID, ...] = (),
        completed_at: datetime,
    ) -> AnswerQuestionRun:
        if self.state is not AnswerQuestionRunState.RUNNING:
            raise ValueError("Q&A run is already terminal")
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "state": AnswerQuestionRunState.COMPLETED,
                "outcome": outcome,
                "final_attempt_id": final_attempt_id,
                "output_artifact_ids": output_artifact_ids,
                "completed_at": completed_at,
            }
        )

    @field_validator("run_id", "workspace_id", "environment_id", "final_attempt_id")
    @classmethod
    def validate_id(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is not None:
            _require_id(value, info.field_name)
        return value

    @field_validator("input_artifact_ids", "output_artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, value: tuple[UUID, ...], info: Any) -> tuple[UUID, ...]:
        return _unique_ids(value, info.field_name)

    @field_validator("created_at", "completed_at")
    @classmethod
    def validate_time(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is not None:
            _aware(value, info.field_name)
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> AnswerQuestionRun:
        terminal = self.state is AnswerQuestionRunState.COMPLETED
        if terminal != all(
            value is not None
            for value in (self.outcome, self.final_attempt_id, self.completed_at)
        ):
            raise ValueError("Q&A run terminal fields are inconsistent")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("Q&A run completion precedes creation")
        if self.outcome is AnswerQuestionOutcome.ANSWERED and not self.output_artifact_ids:
            raise ValueError("answered Q&A run requires output artifacts")
        return self


class AnswerQuestionAttempt(ValidatedDefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal[ANSWER_QUESTION_ATTEMPT_SCHEMA] = ANSWER_QUESTION_ATTEMPT_SCHEMA
    attempt_id: UUID
    run_id: UUID
    attempt_number: int = Field(ge=1)
    workspace_id: UUID
    environment_id: UUID
    environment: EnvironmentKind
    subject: ActorRef
    execution_pin: AnswerQuestionExecutionPin
    input_artifact_ids: tuple[UUID, ...]
    output_artifact_ids: tuple[UUID, ...] = ()
    context_bundle_ref: ContextBundleRef | None = None
    agent_result_ref: AgentResultRef | None = None
    diagnostic_context: AnswerQuestionDiagnosticContext
    usage: AnswerQuestionUsage = AnswerQuestionUsage()
    events: tuple[AnswerQuestionAttemptEvent, ...]
    state: AnswerQuestionAttemptState = AnswerQuestionAttemptState.RUNNING
    outcome: AnswerQuestionOutcome | None = None
    reason: SafeAttemptReason | None = None
    started_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def start(
        cls,
        *,
        attempt_id: UUID,
        run: AnswerQuestionRun,
        attempt_number: int,
        execution_pin: AnswerQuestionExecutionPin,
        input_artifact_ids: tuple[UUID, ...],
        diagnostic_context: DiagnosticIdentity,
        started_at: datetime,
    ) -> AnswerQuestionAttempt:
        return cls(
            attempt_id=attempt_id,
            run_id=run.run_id,
            attempt_number=attempt_number,
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            environment=run.environment,
            subject=run.subject,
            execution_pin=execution_pin,
            input_artifact_ids=input_artifact_ids,
            diagnostic_context=AnswerQuestionDiagnosticContext.from_identity(
                diagnostic_context
            ),
            events=(
                AnswerQuestionAttemptEvent(
                    event_id=attempt_id,
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    environment=run.environment,
                    kind=AnswerQuestionAttemptEventKind.STARTED,
                    occurred_at=started_at,
                ),
            ),
            started_at=started_at,
        )

    def complete(
        self,
        *,
        state: AnswerQuestionAttemptState,
        outcome: AnswerQuestionOutcome,
        usage: AnswerQuestionUsage,
        terminal_event: AnswerQuestionAttemptEvent,
        reason: SafeAttemptReason | None = None,
        context_bundle_ref: ContextBundleRef | None = None,
        output_artifact_ids: tuple[UUID, ...] = (),
        agent_result_ref: AgentResultRef | None = None,
        agent_result: AgentResultValue | None = None,
    ) -> AnswerQuestionAttempt:
        if self.state is not AnswerQuestionAttemptState.RUNNING:
            raise ValueError("Q&A attempt is already terminal")
        expected_event = {
            AnswerQuestionAttemptState.SUCCEEDED: AnswerQuestionAttemptEventKind.COMPLETED,
            AnswerQuestionAttemptState.FAILED: AnswerQuestionAttemptEventKind.FAILED,
            AnswerQuestionAttemptState.CANCELLED: AnswerQuestionAttemptEventKind.CANCELLED,
        }.get(state)
        if expected_event is None or terminal_event.kind is not expected_event:
            raise ValueError("Q&A attempt terminal event is inconsistent")
        if (agent_result_ref is None) != (agent_result is None):
            raise ValueError("AgentResult reference and validation value must be supplied together")
        if agent_result is not None:
            expected_statuses = {
                AnswerQuestionAttemptState.SUCCEEDED: {
                    "succeeded",
                    "needs_review",
                },
                AnswerQuestionAttemptState.FAILED: {"failed"},
                AnswerQuestionAttemptState.CANCELLED: {"cancelled"},
            }[state]
            status = getattr(agent_result.status, "value", agent_result.status)
            if (
                status not in expected_statuses
                or agent_result.output_artifact_ids != output_artifact_ids
            ):
                raise ValueError("AgentResult does not describe this Q&A attempt")
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "state": state,
                "outcome": outcome,
                "reason": reason,
                "context_bundle_ref": context_bundle_ref,
                "output_artifact_ids": output_artifact_ids,
                "agent_result_ref": agent_result_ref,
                "usage": usage,
                "events": (*self.events, terminal_event),
                "completed_at": terminal_event.occurred_at,
            }
        )

    @field_validator(
        "attempt_id", "run_id", "workspace_id", "environment_id"
    )
    @classmethod
    def validate_id(cls, value: UUID | None, info: Any) -> UUID | None:
        if value is not None:
            _require_id(value, info.field_name)
        return value

    @field_validator("input_artifact_ids", "output_artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, value: tuple[UUID, ...], info: Any) -> tuple[UUID, ...]:
        return _unique_ids(value, info.field_name)

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_time(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is not None:
            _aware(value, info.field_name)
        return value

    @model_validator(mode="after")
    def validate_lineage_and_lifecycle(self) -> AnswerQuestionAttempt:
        if (
            self.execution_pin.workspace_id != self.workspace_id
            or self.execution_pin.environment_id != self.environment_id
            or self.execution_pin.environment is not self.environment
        ):
            raise ValueError("Q&A attempt execution pin has a different scope")
        if not self.events or self.events[0].kind is not AnswerQuestionAttemptEventKind.STARTED:
            raise ValueError("Q&A attempt must begin with a started event")
        if any(
            event.workspace_id != self.workspace_id
            or event.environment_id != self.environment_id
            or event.environment is not self.environment
            for event in self.events
        ):
            raise ValueError("Q&A attempt event has a different scope")
        if any(
            later.occurred_at < earlier.occurred_at
            for earlier, later in zip(self.events, self.events[1:])
        ):
            raise ValueError("Q&A attempt events must be ordered")
        terminal = self.state is not AnswerQuestionAttemptState.RUNNING
        if terminal != all(
            value is not None for value in (self.outcome, self.completed_at)
        ):
            raise ValueError("Q&A attempt terminal fields are inconsistent")
        if terminal and self.events[-1].occurred_at != self.completed_at:
            raise ValueError("Q&A attempt terminal event is inconsistent")
        if self.state is AnswerQuestionAttemptState.SUCCEEDED:
            if self.outcome in {AnswerQuestionOutcome.FAILED, AnswerQuestionOutcome.CANCELLED}:
                raise ValueError("successful Q&A attempt has a failure outcome")
            if self.reason is not None:
                raise ValueError("successful Q&A attempt cannot carry a failure reason")
            if self.outcome is AnswerQuestionOutcome.ANSWERED and (
                self.context_bundle_ref is None
                or not self.output_artifact_ids
                or self.agent_result_ref is None
            ):
                raise ValueError("answered Q&A attempt has incomplete lineage")
        elif self.state is AnswerQuestionAttemptState.FAILED:
            if self.outcome is not AnswerQuestionOutcome.FAILED or self.reason is None:
                raise ValueError("failed Q&A attempt requires a safe failure reason")
            if self.events[-1].reason is not self.reason:
                raise ValueError("Q&A attempt failure reason differs from its event")
        elif self.state is AnswerQuestionAttemptState.CANCELLED:
            if self.outcome is not AnswerQuestionOutcome.CANCELLED or self.reason is None:
                raise ValueError("cancelled Q&A attempt requires a safe reason")
            if self.events[-1].reason is not self.reason:
                raise ValueError("Q&A attempt cancellation reason differs from its event")
        return self


__all__ = [
    "ANSWER_QUESTION_ATTEMPT_SCHEMA",
    "ANSWER_QUESTION_RUN_SCHEMA",
    "AgentResultRef",
    "AnswerQuestionAttempt",
    "AnswerQuestionAttemptEvent",
    "AnswerQuestionAttemptEventKind",
    "AnswerQuestionAttemptState",
    "AnswerQuestionDiagnosticContext",
    "AnswerQuestionOutcome",
    "AnswerQuestionRun",
    "AnswerQuestionRunState",
    "AnswerQuestionUsage",
    "SafeAttemptReason",
]
