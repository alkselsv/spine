from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.answer_question_runs import (
    validate_answer_question_context_bundle,
)
from spine.domain.agents import AgentResult, AgentRunStatus
from spine.domain.capabilities.answer_question_runs import (
    AgentResultRef,
    AnswerQuestionAttempt,
    AnswerQuestionAttemptEvent,
    AnswerQuestionAttemptEventKind,
    AnswerQuestionAttemptState,
    AnswerQuestionOutcome,
    AnswerQuestionUsage,
)
from tests.domain.capabilities.test_answer_question_runs import _pin, _run
from tests.domain.context_bundle.test_context_bundle_models import context_bundle


def test_context_bundle_lineage_belongs_to_exact_answer_question_attempt() -> None:
    run = _run()
    attempt_id = UUID(int=30)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=attempt_id,
        run=run,
        attempt_number=1,
        execution_pin=_pin(),
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    bundle = context_bundle(
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        run_id=run.run_id,
        attempt_id=attempt_id,
    )
    completed = attempt.complete(
        state=AnswerQuestionAttemptState.SUCCEEDED,
        outcome=AnswerQuestionOutcome.ANSWERED,
        context_bundle_ref=bundle.reference,
        output_artifact_ids=(UUID(int=31),),
        agent_result_ref=AgentResultRef(result_id=UUID(int=34)),
        agent_result=AgentResult(
            status=AgentRunStatus.SUCCEEDED,
            output_artifact_ids=(UUID(int=31),),
        ),
        usage=AnswerQuestionUsage(model_invocations=1),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=UUID(int=32),
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            environment=run.environment,
            kind=AnswerQuestionAttemptEventKind.COMPLETED,
            occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
        ),
    )

    validate_answer_question_context_bundle(completed, bundle)

    with pytest.raises(ValueError, match="lineage"):
        validate_answer_question_context_bundle(
            completed,
            bundle.model_copy(update={"run_id": UUID(int=99)}),
        )
