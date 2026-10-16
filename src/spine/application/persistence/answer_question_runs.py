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
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttempt,
    AnswerQuestionAttemptState,
    AnswerQuestionRun,
    AnswerQuestionRunState,
)
from spine.domain.context_bundles import ContextBundle


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


class AnswerQuestionRunRepository(Protocol):
    async def create_run(
        self, command: AnswerQuestionRunCommand
    ) -> AnswerQuestionRunMutationResult: ...

    async def complete_run(self, run: AnswerQuestionRun) -> AnswerQuestionRun: ...

    async def resolve_run(self, run_id: UUID) -> AnswerQuestionRun: ...

    async def resolve_run_by_key(self, idempotency_key: str) -> AnswerQuestionRun: ...

    async def start_attempt(
        self, attempt: AnswerQuestionAttempt
    ) -> AnswerQuestionAttempt: ...

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
]
