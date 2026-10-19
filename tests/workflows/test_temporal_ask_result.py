from uuid import UUID

from spine.application.authorized_ask import AuthorizedAskResult
from spine.domain.capabilities import (
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    RunAttemptCorrelation,
)
from spine.workflows.temporal.definitions import (
    AskAdmissionActivityResult,
    SafeAskActivityResult,
    _deserialize_ask_admission_activity_result,
    _serialize_ask_admission_activity_result,
)


def _result() -> AuthorizedAskResult:
    output = AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=UUID(int=4), attempt_id=UUID(int=5), trace_id=UUID(int=6)
        ),
        scope=AnswerQuestionScope(workspace_id=UUID(int=1), environment_id=UUID(int=2)),
        outcome=AnswerQuestionOutcome.FAILED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
        ),
    )
    return AuthorizedAskResult(
        command_id=UUID(int=7),
        session_id=UUID(int=8),
        message_id=UUID(int=9),
        output=output,
        accepted_answer=None,
    )


def test_temporal_activity_result_v2_contains_only_content_free_terminal_ack() -> None:
    serialized = _serialize_ask_admission_activity_result(_result())

    assert serialized == {
        "schema_version": "spine.qa.ask-activity-result:v2",
        "command_id": str(UUID(int=7)),
        "session_id": str(UUID(int=8)),
        "message_id": str(UUID(int=9)),
        "run_id": str(UUID(int=4)),
        "attempt_id": str(UUID(int=5)),
        "outcome": "failed",
        "replay": False,
    }
    assert "output" not in serialized
    assert "accepted_answer" not in serialized
    assert isinstance(_deserialize_ask_admission_activity_result(serialized), SafeAskActivityResult)


def test_temporal_activity_result_v1_history_remains_decodable() -> None:
    result = _result()
    historical = AskAdmissionActivityResult(
        command_id=result.command_id,
        session_id=result.session_id,
        message_id=result.message_id,
        output=result.output,
        accepted_answer=None,
        replay=False,
    ).model_dump(mode="json")

    restored = _deserialize_ask_admission_activity_result(historical)

    assert isinstance(restored, AuthorizedAskResult)
    assert restored.output == result.output
