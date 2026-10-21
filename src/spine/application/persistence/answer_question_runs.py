"""Application persistence contract for durable Q&A runs and attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    OpaqueResultReference,
)
from spine.domain.agents import AgentResult
from spine.domain.artifacts import Artifact
from spine.domain.capabilities.answer_question_runs import (
    AgentResultRef,
    AnswerQuestionAttempt,
    AnswerQuestionAttemptState,
    AnswerQuestionRun,
    AnswerQuestionRunState,
    AnswerQuestionUsage,
)
from spine.domain.capabilities.answer_question import AnswerQuestionOutput
from spine.domain.capabilities.answer_outcomes import AnswerAuthorizationOutcome
from spine.domain.capabilities.answer_disclosure import AcceptedGroundedAnswer
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from spine.domain.capabilities.answer_question_execution import AnswerQuestionExecutionPin
from spine.domain.context_bundles import ContextBundle, ContextBundleRef
from spine.domain.disclosure import DisclosureDependencySet


ANSWER_QUESTION_RUN_OPERATION = PersistenceOperation("answer_question_run")
ANSWER_QUESTION_RUN_OPERATION_SCHEMA_VERSION = 1
ANSWER_QUESTION_RUN_RESULT_TYPE = "answer_question_run"


@dataclass(frozen=True, slots=True)
class AnswerQuestionRunQuery:
    """Trusted-scope filters for the persisted Q&A run read model."""

    subject_id: UUID | None = None
    session_id: UUID | None = None
    message_id: UUID | None = None
    outcome: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class AnswerQuestionRunPosition:
    created_at: datetime
    run_id: UUID


@dataclass(frozen=True, slots=True)
class AnswerQuestionRunPage:
    runs: tuple[AnswerQuestionRun, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class AnswerQuestionRunMessageLink:
    session_id: UUID
    message_id: UUID


@dataclass(frozen=True, slots=True)
class AnswerQuestionRunCommand:
    run: AnswerQuestionRun
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls,
        *,
        run: AnswerQuestionRun,
        idempotency_key: IdempotencyKey,
    ) -> AnswerQuestionRunCommand:
        if run.idempotency_key != idempotency_key.value:
            raise ValueError("Q&A run idempotency key is inconsistent")
        digest = digest_command(
            operation=ANSWER_QUESTION_RUN_OPERATION,
            operation_schema_version=ANSWER_QUESTION_RUN_OPERATION_SCHEMA_VERSION,
            payload={
                "run_id": run.run_id,
                "workspace_id": run.workspace_id,
                "environment_id": run.environment_id,
                "environment": run.environment,
                "subject": run.subject.model_dump(mode="python"),
                "input_artifact_ids": run.input_artifact_ids,
                "diagnostic_context": run.diagnostic_context.model_dump(mode="python"),
                "created_at": run.created_at,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            run=run.model_copy(update={"command_digest": digest.value}),
            idempotency_key=idempotency_key,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class AnswerQuestionRunMutationResult:
    run: AnswerQuestionRun
    replay: bool
    result_reference: OpaqueResultReference


@dataclass(frozen=True, slots=True)
class AnswerQuestionAttemptAllocation:
    """Immutable lineage for one intended domain attempt.

    The key is opaque and retry-stable. It must never contain a question,
    answer, retrieval payload, or another protected value.
    """

    run_id: UUID
    logical_execution_key: str
    execution_pin: AnswerQuestionExecutionPin
    input_artifact_ids: tuple[UUID, ...]
    cancellation: bool = False

    def __post_init__(self) -> None:
        key = self.logical_execution_key
        if (
            self.run_id.int == 0
            or not 1 <= len(key) <= 255
            or any(not (character.isalnum() or character in "._:-") for character in key)
            or type(self.cancellation) is not bool
            or any(identifier.int == 0 for identifier in self.input_artifact_ids)
            or len(set(self.input_artifact_ids)) != len(self.input_artifact_ids)
        ):
            raise ValueError("Q&A attempt allocation is invalid")


@dataclass(frozen=True, slots=True)
class AnswerQuestionAttemptAllocationResult:
    attempt: AnswerQuestionAttempt
    replay: bool


@dataclass(frozen=True, slots=True)
class AnswerQuestionAttemptTerminalResult:
    """Canonical terminal payload associated with one persisted attempt."""

    output: AnswerQuestionOutput
    accepted_answer: AcceptedGroundedAnswer | None
    disclosure_record: AnswerDisclosureRecord | None = None


@dataclass(frozen=True, slots=True)
class CanonicalQATerminalizationReceipt:
    """Ephemeral proof of one real production Q&A execution's persisted lineage."""

    run_id: UUID
    attempt_id: UUID
    attempt_number: int
    execution_pin: AnswerQuestionExecutionPin
    output: AnswerQuestionOutput
    context_bundle_ref: ContextBundleRef | None
    agent_result_ref: AgentResultRef | None
    agent_result: AgentResult | None
    output_artifact_ids: tuple[UUID, ...]
    candidate_input_artifact_id: UUID | None
    candidate_output_artifacts: tuple[Artifact, ...]
    disclosure_record: AnswerDisclosureRecord
    authorization: AnswerAuthorizationOutcome
    disclosure_dependencies: DisclosureDependencySet = DisclosureDependencySet()
    usage: AnswerQuestionUsage | None = None

    def __post_init__(self) -> None:
        if (
            self.run_id.int == 0
            or self.attempt_id.int == 0
            or self.attempt_number < 1
            or self.output.correlation.run_id != self.run_id
            or self.output.correlation.attempt_id != self.attempt_id
            or self.output.scope.workspace_id != self.execution_pin.workspace_id
            or self.output.scope.environment_id != self.execution_pin.environment_id
            or self.disclosure_record.workspace_id != self.execution_pin.workspace_id
            or self.disclosure_record.environment_id != self.execution_pin.environment_id
            or self.disclosure_record.run_id != self.run_id
            or self.disclosure_record.attempt_id != self.attempt_id
            or self.disclosure_record.outcome is not self.output.outcome
            or self.disclosure_record.output != self.output
            or (
                self.output.outcome.value == "denied"
                and self.authorization.value != "denied"
            )
            or (
                self.authorization.value == "denied"
                and self.output.outcome.value != "denied"
            )
        ):
            raise ValueError("canonical Q&A terminalization receipt identity is invalid")
        if (self.agent_result_ref is None) != (self.agent_result is None):
            raise ValueError("AgentResult reference and value must be supplied together")
        if self.agent_result is None:
            if self.output_artifact_ids or self.candidate_output_artifacts:
                raise ValueError("artifact lineage requires a persisted AgentResult")
        elif (
            self.agent_result.output_artifact_ids != self.output_artifact_ids
            or tuple(item.id for item in self.candidate_output_artifacts)
            != self.output_artifact_ids
        ):
            raise ValueError("AgentResult artifact lineage is inconsistent")
        if self.output_artifact_ids:
            if self.candidate_input_artifact_id is None or any(
                item.workspace_id != self.execution_pin.workspace_id
                or item.environment_id != self.execution_pin.environment_id
                or item.work_item_id != self.run_id
                or self.candidate_input_artifact_id not in item.upstream_artifact_ids
                for item in self.candidate_output_artifacts
            ):
                raise ValueError("candidate input artifact lineage is invalid")
        elif self.candidate_input_artifact_id is not None or self.candidate_output_artifacts:
            raise ValueError("candidate input cannot exist without output artifacts")
        if self.disclosure_record.accepted_answer is not None:
            validation = self.disclosure_record.validation
            if (
                validation is None
                or tuple(validation.dependency_ids)
                != tuple(item.dependency_id for item in self.disclosure_dependencies.dependencies)
            ):
                raise ValueError("accepted disclosure dependencies are inconsistent")


class AnswerQuestionRunRepository(Protocol):
    async def create_run(
        self, command: AnswerQuestionRunCommand
    ) -> AnswerQuestionRunMutationResult: ...

    async def complete_run(self, run: AnswerQuestionRun) -> AnswerQuestionRun: ...

    async def start_run(self, run: AnswerQuestionRun) -> AnswerQuestionRun: ...

    async def request_cancellation(
        self, *, run_id: UUID, requested_at: datetime
    ) -> AnswerQuestionRun: ...

    async def resolve_run(self, run_id: UUID) -> AnswerQuestionRun: ...

    async def resolve_run_in_scope(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
    ) -> AnswerQuestionRun: ...

    async def resolve_run_for_update(self, run_id: UUID) -> AnswerQuestionRun: ...

    async def resolve_run_by_key(self, idempotency_key: str) -> AnswerQuestionRun: ...

    async def start_attempt(
        self, attempt: AnswerQuestionAttempt
    ) -> AnswerQuestionAttempt: ...

    async def allocate_or_resume_attempt(
        self, allocation: AnswerQuestionAttemptAllocation
    ) -> AnswerQuestionAttemptAllocationResult: ...

    async def complete_attempt(
        self, attempt: AnswerQuestionAttempt
    ) -> AnswerQuestionAttempt: ...

    async def resolve_attempt(self, attempt_id: UUID) -> AnswerQuestionAttempt: ...

    async def list_attempts(self, run_id: UUID) -> tuple[AnswerQuestionAttempt, ...]: ...

    async def list_runs(
        self,
        *,
        query: AnswerQuestionRunQuery,
        limit: int,
        after: AnswerQuestionRunPosition | None,
    ) -> AnswerQuestionRunPage: ...

    async def resolve_message_link(
        self, run_id: UUID
    ) -> AnswerQuestionRunMessageLink | None: ...


def validate_answer_question_attempt_start(
    run: AnswerQuestionRun,
    attempt: AnswerQuestionAttempt,
) -> None:
    """Validate immutable run-to-attempt lineage before either adapter writes."""

    if (
        run.state is not AnswerQuestionRunState.RUNNING
        or attempt.state is not AnswerQuestionAttemptState.RUNNING
        or run.run_id != attempt.run_id
        or run.workspace_id != attempt.workspace_id
        or run.environment_id != attempt.environment_id
        or run.environment is not attempt.environment
        or run.subject != attempt.subject
        or run.input_artifact_ids != attempt.input_artifact_ids
    ):
        raise ValueError("Q&A attempt lineage is invalid.")


def validate_answer_question_attempt_completion(
    existing: AnswerQuestionAttempt,
    completed: AnswerQuestionAttempt,
) -> None:
    """Allow only the one running-to-terminal transition of an attempt."""

    immutable_fields = {
        "schema_version",
        "attempt_id",
        "run_id",
        "attempt_number",
        "logical_execution_key",
        "workspace_id",
        "environment_id",
        "environment",
        "subject",
        "execution_pin",
        "input_artifact_ids",
        "diagnostic_context",
        "started_at",
    }
    if (
        existing.state is not AnswerQuestionAttemptState.RUNNING
        or completed.state is AnswerQuestionAttemptState.RUNNING
        or any(getattr(existing, field) != getattr(completed, field) for field in immutable_fields)
        or completed.events[:-1] != existing.events
    ):
        raise ValueError("Q&A attempt lineage is immutable.")


def validate_answer_question_run_completion(
    existing: AnswerQuestionRun,
    final_attempt: AnswerQuestionAttempt,
    completed: AnswerQuestionRun,
) -> None:
    """Bind a terminal run to the exact terminal attempt and immutable run lineage."""

    immutable_fields = {
        "schema_version",
        "run_id",
        "workspace_id",
        "environment_id",
        "environment",
        "subject",
        "idempotency_key",
        "command_digest",
        "input_artifact_ids",
        "diagnostic_context",
        "created_at",
        "cancellation_requested_at",
    }
    if (
        existing.state is not AnswerQuestionRunState.RUNNING
        or completed.state is not AnswerQuestionRunState.COMPLETED
        or any(getattr(existing, field) != getattr(completed, field) for field in immutable_fields)
        or final_attempt.run_id != completed.run_id
        or final_attempt.attempt_id != completed.final_attempt_id
        or final_attempt.state is AnswerQuestionAttemptState.RUNNING
        or final_attempt.outcome is not completed.outcome
        or final_attempt.output_artifact_ids != completed.output_artifact_ids
        or final_attempt.completed_at != completed.completed_at
    ):
        raise ValueError("Q&A run completion lineage is invalid.")


def validate_answer_question_run_start(
    existing: AnswerQuestionRun,
    started: AnswerQuestionRun,
) -> None:
    """Bind the workflow's running snapshot to the admitted run identity."""

    immutable_fields = {
        "schema_version",
        "run_id",
        "workspace_id",
        "environment_id",
        "environment",
        "subject",
        "idempotency_key",
        "command_digest",
        "input_artifact_ids",
        "output_artifact_ids",
        "diagnostic_context",
        "created_at",
        "outcome",
        "final_attempt_id",
        "cancellation_requested_at",
        "completed_at",
    }
    if (
        existing.state not in {
            AnswerQuestionRunState.ADMITTED,
            AnswerQuestionRunState.DISPATCH_PENDING,
            AnswerQuestionRunState.RUNNING,
        }
        or started.state is not AnswerQuestionRunState.RUNNING
        or any(
            getattr(existing, field) != getattr(started, field)
            for field in immutable_fields
        )
    ):
        raise ValueError("Q&A run start lineage is invalid.")


def validate_answer_question_context_bundle(
    attempt: AnswerQuestionAttempt,
    bundle: ContextBundle,
) -> None:
    """Require the exact bundle produced for this scoped run attempt."""

    reference = attempt.context_bundle_ref
    if (
        reference is None
        or bundle.workspace_id != attempt.workspace_id
        or bundle.environment_id != attempt.environment_id
        or bundle.run_id != attempt.run_id
        or bundle.attempt_id != attempt.attempt_id
        or bundle.reference != reference
    ):
        raise ValueError("Q&A attempt ContextBundle lineage is invalid.")


__all__ = [
    "ANSWER_QUESTION_RUN_OPERATION",
    "ANSWER_QUESTION_RUN_OPERATION_SCHEMA_VERSION",
    "ANSWER_QUESTION_RUN_RESULT_TYPE",
    "AnswerQuestionRunCommand",
    "AnswerQuestionAttemptAllocation",
    "AnswerQuestionAttemptAllocationResult",
    "AnswerQuestionAttemptTerminalResult",
    "CanonicalQATerminalizationReceipt",
    "AnswerQuestionRunMutationResult",
    "AnswerQuestionRunMessageLink",
    "AnswerQuestionRunPage",
    "AnswerQuestionRunPosition",
    "AnswerQuestionRunQuery",
    "AnswerQuestionRunRepository",
    "validate_answer_question_attempt_completion",
    "validate_answer_question_attempt_start",
    "validate_answer_question_context_bundle",
    "validate_answer_question_run_completion",
    "validate_answer_question_run_start",
]
