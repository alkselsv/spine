from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureEnvelope,
    CaptureSubscription,
    ExecutionWaitState,
    ObserverRoutedAskAdmissionWorkflowInput,
    SafeWaitUnavailableReason,
    TerminalWaitResult,
)
from spine.application.workflows.ask_admission_dispatch import AskAdmissionWorkflowInput
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecordV2,
    AnswerExecutionCheckpoint,
)
from spine.domain.common import ActorKind, ActorRef
from tests.application.qa_observation.factories import (
    capture_envelope,
    execution_identity,
    same_execution_capture,
)


def test_execution_identity_does_not_match_cross_scope_substitution() -> None:
    identity = execution_identity()
    substituted = identity.model_copy(
        update={"workspace_id": UUID("90000000-0000-0000-0000-000000000471")}
    )

    assert identity.matches(substituted) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_id", UUID("91000000-0000-0000-0000-000000000471")),
        ("run_id", UUID("92000000-0000-0000-0000-000000000471")),
        ("session_id", UUID("93000000-0000-0000-0000-000000000471")),
        ("message_id", UUID("94000000-0000-0000-0000-000000000471")),
        ("workspace_id", UUID("95000000-0000-0000-0000-000000000471")),
        ("environment_id", UUID("96000000-0000-0000-0000-000000000471")),
        (
            "requester",
            ActorRef(
                kind=ActorKind.HUMAN,
                id=UUID("97000000-0000-0000-0000-000000000471"),
            ),
        ),
        ("attempt_id", UUID("98000000-0000-0000-0000-000000000471")),
        ("attempt_number", 2),
    ],
)
def test_execution_identity_rejects_every_correlated_field_substitution(
    field: str, value: object
) -> None:
    identity = execution_identity()

    assert identity.matches(identity.model_copy(update={field: value})) is False


def test_execution_identity_is_frozen_and_rejects_unknown_fields() -> None:
    identity = execution_identity()

    with pytest.raises(ValidationError):
        AdmittedExecutionIdentity.model_validate(
            identity.model_dump(mode="python") | {"question": "protected"}
        )


def test_capture_envelope_round_trips_as_a_typed_same_execution_capture() -> None:
    envelope = capture_envelope(worker_execution_elapsed_ns=123_456)

    parsed = CaptureEnvelope.model_validate(envelope.model_dump(mode="python"))

    assert parsed == envelope
    assert parsed.worker_execution_elapsed_ns == 123_456
    assert parsed.capture.answer.outcome is AnswerQuestionOutcome.DENIED


def test_capture_envelope_json_round_trip_preserves_v2_disclosure_record() -> None:
    envelope = capture_envelope(worker_execution_elapsed_ns=123_456)
    historical = envelope.capture.disclosure
    current = AnswerDisclosureRecordV2(
        **historical.model_dump(mode="python", exclude={"schema_version"}),
        execution_checkpoint=AnswerExecutionCheckpoint(
            execution_pin_digest="d" * 64,
        ),
    )
    envelope = envelope.model_copy(
        update={
            "capture": envelope.capture.model_copy(
                update={"disclosure": current}
            )
        }
    )

    parsed = CaptureEnvelope.model_validate_json(envelope.model_dump_json())

    assert isinstance(parsed.capture.disclosure, AnswerDisclosureRecordV2)
    assert parsed.capture.disclosure == current


def test_capture_envelope_accepts_missing_worker_sample_without_fabricating_zero() -> (
    None
):
    envelope = capture_envelope()

    assert envelope.worker_execution_elapsed_ns is None


def test_capture_envelope_rejects_negative_worker_duration() -> None:
    with pytest.raises(ValidationError):
        capture_envelope(worker_execution_elapsed_ns=-1)


def test_capture_envelope_rejects_an_attempt_from_another_execution() -> None:
    capture = same_execution_capture()
    with pytest.raises(ValidationError):
        capture.model_copy(
            update={
                "question": capture.question.model_copy(
                    update={
                        "correlation": capture.question.correlation.model_copy(
                            update={
                                "attempt_id": UUID(
                                    "e0000000-0000-0000-0000-000000000471"
                                )
                            }
                        )
                    }
                )
            }
        )


@pytest.mark.parametrize(
    ("state", "outcome", "reason"),
    [
        (ExecutionWaitState.PENDING, None, None),
        (ExecutionWaitState.RUNNING, None, None),
        (ExecutionWaitState.TERMINAL, AnswerQuestionOutcome.FAILED, None),
        (ExecutionWaitState.TERMINAL, AnswerQuestionOutcome.CANCELLED, None),
        (
            ExecutionWaitState.UNAVAILABLE,
            None,
            SafeWaitUnavailableReason.READBACK_UNAVAILABLE,
        ),
    ],
)
def test_terminal_wait_states_preserve_status_and_q_and_a_outcome(
    state: ExecutionWaitState,
    outcome: AnswerQuestionOutcome | None,
    reason: SafeWaitUnavailableReason | None,
) -> None:
    result = TerminalWaitResult(
        identity=execution_identity(),
        state=state,
        outcome=outcome,
        unavailable_reason=reason,
    )

    assert result.state is state
    assert result.outcome is outcome
    assert result.unavailable_reason is reason


def test_subscription_rejects_naive_expiry_timestamp() -> None:
    with pytest.raises(ValidationError):
        CaptureSubscription(
            capture_id=UUID("c0000000-0000-0000-0000-000000000471"),
            subscription_id=UUID("d0000000-0000-0000-0000-000000000471"),
            expires_at=datetime(2026, 10, 18),
        )


def test_legacy_workflow_input_without_authority_generation_fails_closed() -> None:
    subscription = CaptureSubscription(
        capture_id=UUID("c0000000-0000-0000-0000-000000000471"),
        subscription_id=UUID("d0000000-0000-0000-0000-000000000471"),
        expires_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
    )

    assert subscription.schema_version == "r1-qa-observer-subscription:v1"
    with pytest.raises(ValidationError):
        AskAdmissionWorkflowInput(
            command_id=UUID("10000000-0000-0000-0000-000000000471"),
            workspace_id=UUID("50000000-0000-0000-0000-000000000471"),
            environment_id=UUID("60000000-0000-0000-0000-000000000471"),
            session_id=UUID("30000000-0000-0000-0000-000000000471"),
            message_id=UUID("40000000-0000-0000-0000-000000000471"),
            run_id=UUID("20000000-0000-0000-0000-000000000471"),
            trace_id=UUID("a0000000-0000-0000-0000-000000000471"),
        )


def test_observer_route_uses_compatible_separate_workflow_contract() -> None:
    identity = execution_identity()
    ask_input = AskAdmissionWorkflowInput(
        command_id=identity.command_id,
        workspace_id=identity.workspace_id,
        environment_id=identity.environment_id,
        session_id=identity.session_id,
        message_id=identity.message_id,
        run_id=identity.run_id,
        trace_id=UUID("a0000000-0000-0000-0000-000000000471"),
        authorization_generation=1,
    )
    routing = ObserverRoutedAskAdmissionWorkflowInput(
        ask_admission=ask_input,
        capture_subscription=CaptureSubscription(
            capture_id=UUID("c0000000-0000-0000-0000-000000000471"),
            subscription_id=UUID("d0000000-0000-0000-0000-000000000471"),
            expires_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        ),
    )

    parsed = ObserverRoutedAskAdmissionWorkflowInput.model_validate(
        routing.model_dump(mode="python")
    )

    assert parsed == routing
    assert routing.schema_version == "spine.qa.ask-admission-observer-routing:v1"
    assert "capture" not in routing.model_dump(mode="python")
