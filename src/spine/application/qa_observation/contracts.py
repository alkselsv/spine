"""Versioned, gold-blind contracts for observing one admitted Q&A execution."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.application.workflows.ask_admission_dispatch import AskAdmissionWorkflowInput
from spine.application.persistence.ask_admission import AskCaptureSubscription
from spine.domain.capabilities import (
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
)
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecord,
    AnswerDisclosureRecordV2,
)
from spine.domain.common import ActorRef, ValidatedDefinitionModel
from spine.domain.context_bundles import ContextBundle
from spine.memory.contracts import (
    ContextResponse,
    ProjectionRetrievalResult,
    RetrievalResult,
)

EXECUTION_IDENTITY_SCHEMA = "r1-qa-admitted-execution-identity:v1"
ADMITTED_RUN_IDENTITY_SCHEMA = "r1-qa-admitted-run-identity:v1"
TERMINAL_WAIT_SCHEMA = "r1-qa-terminal-wait:v1"
TERMINAL_RUN_WAIT_SCHEMA = "r1-qa-terminal-run-wait:v1"
CAPTURE_SUBSCRIPTION_SCHEMA = "r1-qa-observer-subscription:v1"
CAPTURE_ENVELOPE_SCHEMA = "r1-qa-observer-capture:v1"
OBSERVER_WORKFLOW_ROUTING_SCHEMA = "spine.qa.ask-admission-observer-routing:v1"


class ExecutionWaitState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    TERMINAL = "terminal"
    UNAVAILABLE = "unavailable"


class SafeWaitUnavailableReason(str, Enum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AUTHORIZATION_CHANGED = "authorization_changed"
    READBACK_UNAVAILABLE = "readback_unavailable"
    CORRELATION_MISMATCH = "correlation_mismatch"
    INCOMPLETE_TERMINAL_STATE = "incomplete_terminal_state"


class AdmittedExecutionIdentity(ValidatedDefinitionModel):
    """The complete identity tuple for one admitted activity attempt."""

    schema_version: Literal[EXECUTION_IDENTITY_SCHEMA] = EXECUTION_IDENTITY_SCHEMA
    command_id: UUID
    run_id: UUID
    session_id: UUID
    message_id: UUID
    workspace_id: UUID
    environment_id: UUID
    requester: ActorRef
    attempt_id: UUID
    attempt_number: int = Field(ge=1, le=10_000)

    @field_validator(
        "command_id",
        "run_id",
        "session_id",
        "message_id",
        "workspace_id",
        "environment_id",
        "attempt_id",
    )
    @classmethod
    def require_non_zero_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("execution identity identifiers must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_requester(self) -> "AdmittedExecutionIdentity":
        if self.requester.id.int == 0:
            raise ValueError("requester identity must be non-zero")
        return self

    def matches(self, other: "AdmittedExecutionIdentity") -> bool:
        """Return whether every admitted execution and attempt field agrees."""

        return type(other) is AdmittedExecutionIdentity and self == other


class AdmittedRunIdentity(ValidatedDefinitionModel):
    """Canonical admitted identity before execution resolves an attempt."""

    schema_version: Literal[ADMITTED_RUN_IDENTITY_SCHEMA] = ADMITTED_RUN_IDENTITY_SCHEMA
    command_id: UUID
    run_id: UUID
    session_id: UUID
    message_id: UUID
    workspace_id: UUID
    environment_id: UUID
    requester: ActorRef

    @field_validator(
        "command_id", "run_id", "session_id", "message_id", "workspace_id", "environment_id"
    )
    @classmethod
    def require_non_zero_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("admitted run identity identifiers must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_requester(self) -> "AdmittedRunIdentity":
        if self.requester.id.int == 0:
            raise ValueError("requester identity must be non-zero")
        return self

    def matches_execution(self, other: AdmittedExecutionIdentity) -> bool:
        return (
            type(other) is AdmittedExecutionIdentity
            and self.command_id == other.command_id
            and self.run_id == other.run_id
            and self.session_id == other.session_id
            and self.message_id == other.message_id
            and self.workspace_id == other.workspace_id
            and self.environment_id == other.environment_id
            and self.requester == other.requester
        )

    def with_attempt(
        self, *, attempt_id: UUID, attempt_number: int
    ) -> AdmittedExecutionIdentity:
        return AdmittedExecutionIdentity(
            command_id=self.command_id,
            run_id=self.run_id,
            session_id=self.session_id,
            message_id=self.message_id,
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            requester=self.requester,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
        )


class TerminalWaitResult(ValidatedDefinitionModel):
    """Bounded, identity-correlated status readback for one admitted run."""

    schema_version: Literal[TERMINAL_WAIT_SCHEMA] = TERMINAL_WAIT_SCHEMA
    identity: AdmittedExecutionIdentity
    state: ExecutionWaitState
    outcome: AnswerQuestionOutcome | None = None
    unavailable_reason: SafeWaitUnavailableReason | None = None

    @model_validator(mode="after")
    def validate_state_payload(self) -> "TerminalWaitResult":
        if self.state is ExecutionWaitState.TERMINAL:
            if self.outcome is None or self.unavailable_reason is not None:
                raise ValueError("terminal state requires exactly one Q&A outcome")
        elif self.state is ExecutionWaitState.UNAVAILABLE:
            if self.unavailable_reason is None or self.outcome is not None:
                raise ValueError("unavailable state requires a safe reason only")
        elif self.outcome is not None or self.unavailable_reason is not None:
            raise ValueError("pending and running states have no terminal payload")
        return self


class TerminalRunWaitResult(ValidatedDefinitionModel):
    """Canonical run readback that resolves its attempt from persisted status."""

    schema_version: Literal[TERMINAL_RUN_WAIT_SCHEMA] = TERMINAL_RUN_WAIT_SCHEMA
    identity: AdmittedRunIdentity
    state: ExecutionWaitState
    outcome: AnswerQuestionOutcome | None = None
    unavailable_reason: SafeWaitUnavailableReason | None = None
    execution_identity: AdmittedExecutionIdentity | None = None

    @model_validator(mode="after")
    def validate_state_payload(self) -> "TerminalRunWaitResult":
        if self.state is ExecutionWaitState.TERMINAL:
            if (
                self.outcome is None
                or self.unavailable_reason is not None
                or self.execution_identity is None
                or not self.identity.matches_execution(self.execution_identity)
            ):
                raise ValueError("terminal run state requires its canonical attempt identity")
        elif self.state is ExecutionWaitState.UNAVAILABLE:
            if (
                self.unavailable_reason is None
                or self.outcome is not None
                or self.execution_identity is not None
            ):
                raise ValueError("unavailable run state requires a safe reason only")
        elif (
            self.outcome is not None
            or self.unavailable_reason is not None
            or self.execution_identity is not None
        ):
            raise ValueError("pending and running states have no terminal payload")
        return self


CaptureSubscription = AskCaptureSubscription


class ObserverRoutedAskAdmissionWorkflowInput(ValidatedDefinitionModel):
    """Versioned wrapper for routing IDs; legacy Ask Admission v1 stays intact.

    Protected capture values travel only through ``CaptureChannel`` and never
    enter this workflow input contract.
    """

    schema_version: Literal[OBSERVER_WORKFLOW_ROUTING_SCHEMA] = (
        OBSERVER_WORKFLOW_ROUTING_SCHEMA
    )
    ask_admission: AskAdmissionWorkflowInput
    capture_subscription: CaptureSubscription


class SameExecutionCapture(ValidatedDefinitionModel):
    """Typed values emitted by one authorized Context Broker execution."""

    question: AnswerQuestionInput
    context_response: ContextResponse
    context_bundle: ContextBundle | None
    projection_retrieval: ProjectionRetrievalResult
    retrieval: RetrievalResult
    answer: AnswerQuestionOutput
    disclosure: AnswerDisclosureRecordV2 | AnswerDisclosureRecord

    @model_validator(mode="after")
    def validate_execution_lineage(self) -> "SameExecutionCapture":
        correlation = self.question.correlation
        if (
            self.answer.scope != self.question.scope
            or self.answer.correlation != correlation
        ):
            raise ValueError("capture answer does not match its question")
        if (
            self.disclosure.workspace_id != self.question.scope.workspace_id
            or self.disclosure.environment_id != self.question.scope.environment_id
            or self.disclosure.run_id != correlation.run_id
            or self.disclosure.attempt_id != correlation.attempt_id
            or self.disclosure.outcome is not self.answer.outcome
        ):
            raise ValueError("capture disclosure lineage does not match its answer")
        if self.context_bundle is not None and (
            self.context_bundle.workspace_id != self.question.scope.workspace_id
            or self.context_bundle.environment_id != self.question.scope.environment_id
            or self.context_bundle.run_id != correlation.run_id
            or self.context_bundle.attempt_id != correlation.attempt_id
        ):
            raise ValueError("capture context bundle does not match its execution")
        return self


class CaptureEnvelope(ValidatedDefinitionModel):
    """Same-execution typed capture and an attempt-local duration sample."""

    schema_version: Literal[CAPTURE_ENVELOPE_SCHEMA] = CAPTURE_ENVELOPE_SCHEMA
    capture_id: UUID
    subscription_id: UUID
    identity: AdmittedExecutionIdentity
    capture: SameExecutionCapture
    worker_execution_elapsed_ns: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Monotonic elapsed duration measured inside this worker attempt; "
            "it is not observer_elapsed_ms and has no cross-process timestamp."
        ),
    )

    @field_validator("capture_id", "subscription_id")
    @classmethod
    def require_non_zero_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("capture routing identifiers must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_same_execution(self) -> "CaptureEnvelope":
        question = self.capture.question
        correlation = question.correlation
        answer = self.capture.answer
        if (
            question.scope.workspace_id != self.identity.workspace_id
            or question.scope.environment_id != self.identity.environment_id
            or correlation.run_id != self.identity.run_id
            or correlation.attempt_id != self.identity.attempt_id
            or answer.scope.workspace_id != self.identity.workspace_id
            or answer.scope.environment_id != self.identity.environment_id
            or answer.correlation.run_id != self.identity.run_id
            or answer.correlation.attempt_id != self.identity.attempt_id
        ):
            raise ValueError("capture does not match execution identity")
        if self.capture.disclosure.run_id != self.identity.run_id:
            raise ValueError("capture disclosure does not match execution identity")
        if self.capture.disclosure.attempt_id != self.identity.attempt_id:
            raise ValueError(
                "capture disclosure attempt does not match execution identity"
            )
        return self
