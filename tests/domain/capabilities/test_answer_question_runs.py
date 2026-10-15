from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.diagnostics import DiagnosticContext
from spine.domain.agents import AgentResult, AgentRunStatus
from spine.domain.capabilities.answer_question_execution import (
    AnswerQuestionExecutionPin,
    PinnedAgentVersion,
    PinnedConfigurationVersion,
    PinnedContextProfile,
    PinnedProjection,
    TrustedAnswerQuestionConfiguration,
)
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttempt,
    AnswerQuestionAttemptEvent,
    AnswerQuestionAttemptEventKind,
    AnswerQuestionAttemptState,
    AnswerQuestionOutcome,
    AnswerQuestionRun,
    AnswerQuestionRunState,
    AnswerQuestionUsage,
    AgentResultRef,
    SafeAttemptReason,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind, SchemaRef
from spine.domain.context_profiles import ContextProfileRef
from spine.domain.context_bundles import ContextBundleRef


def _id(value: int) -> UUID:
    return UUID(int=value)


def _pin(
    workspace_id: UUID = _id(1), environment_id: UUID = _id(2)
) -> AnswerQuestionExecutionPin:
    digest = "a" * 64
    common = {
        "workspace_id": workspace_id,
        "environment_id": environment_id,
        "configuration_generation": 3,
    }
    configuration = TrustedAnswerQuestionConfiguration(
        configuration_generation=3,
        capability_id=_id(3),
        input_schema=SchemaRef(name="answer_question.input", version="1"),
        output_schema=SchemaRef(name="answer_question.output", version="1"),
        agent_version=PinnedAgentVersion(
            **common,
            agent_version_id=_id(4),
            agent_id=_id(5),
            version="1",
            digest=digest,
        ),
        prompt_version=PinnedConfigurationVersion(
            **common,
            identity_id=_id(6),
            binding_key="qa.prompt",
            version="1",
            digest=digest,
        ),
        model_profile=PinnedConfigurationVersion(
            **common,
            identity_id=_id(7),
            binding_key="qa.model",
            version="1",
            digest=digest,
        ),
        context_profile=PinnedContextProfile(
            **common,
            binding_key="qa.context",
            profile=ContextProfileRef(
                profile_id=_id(8), profile_version=2, profile_digest=digest
            ),
        ),
        projection=PinnedProjection(
            **common,
            projection_snapshot_id=_id(9),
            snapshot_manifest_digest=digest,
            projection_activation_id=_id(10),
            activation_generation=4,
            projection_config_version_id=_id(11),
            projection_config_digest=digest,
            canonical_boundary_id=_id(12),
            canonical_boundary_digest=digest,
        ),
        semantic_support_policy=PinnedConfigurationVersion(
            **common,
            identity_id=_id(13),
            binding_key="qa.semantic-support",
            version="1",
            digest=digest,
        ),
        approved=True,
    )
    return AnswerQuestionExecutionPin.create(
        workspace_id=workspace_id,
        environment_id=environment_id,
        environment=EnvironmentKind.PRODUCTION,
        configuration=configuration,
    )


def _run() -> AnswerQuestionRun:
    return AnswerQuestionRun.start(
        run_id=_id(20),
        workspace_id=_id(1),
        environment_id=_id(2),
        environment=EnvironmentKind.PRODUCTION,
        subject=ActorRef(kind=ActorKind.HUMAN, id=_id(21)),
        idempotency_key="qa-command-1",
        command_digest="b" * 64,
        input_artifact_ids=(_id(22),),
        diagnostic_context=DiagnosticContext(
            trace_id=_id(23), correlation_id=_id(24), causation_id=_id(25)
        ),
        created_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )


def test_answer_question_run_completion_preserves_scope_subject_and_lineage() -> None:
    run = _run()

    completed = run.complete(
        outcome=AnswerQuestionOutcome.ANSWERED,
        final_attempt_id=_id(30),
        output_artifact_ids=(_id(31),),
        completed_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
    )

    assert completed.state is AnswerQuestionRunState.COMPLETED
    assert completed.workspace_id == _id(1)
    assert completed.environment_id == _id(2)
    assert completed.subject.id == _id(21)
    assert completed.input_artifact_ids == (_id(22),)
    assert completed.output_artifact_ids == (_id(31),)
    assert completed.final_attempt_id == _id(30)


def test_answer_question_attempt_completion_pins_context_versions_usage_and_safe_events() -> None:
    run = _run()
    started_at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=_id(30),
        run=run,
        attempt_number=1,
        execution_pin=_pin(),
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=started_at,
    )
    completed_event = AnswerQuestionAttemptEvent(
        event_id=_id(32),
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        environment=run.environment,
        kind=AnswerQuestionAttemptEventKind.COMPLETED,
        occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
    )

    completed = attempt.complete(
        state=AnswerQuestionAttemptState.SUCCEEDED,
        outcome=AnswerQuestionOutcome.ANSWERED,
        context_bundle_ref=ContextBundleRef(
            bundle_id=_id(33),
            bundle_version=1,
            bundle_digest="c" * 64,
        ),
        output_artifact_ids=(_id(31),),
        agent_result_ref=AgentResultRef(result_id=_id(34)),
        agent_result=AgentResult(
            status=AgentRunStatus.SUCCEEDED,
            output_artifact_ids=(_id(31),),
        ),
        usage=AnswerQuestionUsage(
            model_invocations=1,
            input_tokens=125,
            output_tokens=37,
            cached_input_tokens=20,
            cost=Decimal("0.004125"),
            currency="USD",
        ),
        terminal_event=completed_event,
    )

    assert completed.execution_pin == _pin()
    assert completed.context_bundle_ref is not None
    assert completed.context_bundle_ref.bundle_id == _id(33)
    assert completed.usage.cost == Decimal("0.004125")
    assert completed.events[-1] == completed_event
    assert completed.agent_result_ref == AgentResultRef(result_id=_id(34))


def test_answer_question_attempt_rejects_leaky_or_inconsistent_terminal_failure() -> None:
    attempt = AnswerQuestionAttempt.start(
        attempt_id=_id(30),
        run=_run(),
        attempt_number=1,
        execution_pin=_pin(),
        input_artifact_ids=(_id(22),),
        diagnostic_context=DiagnosticContext(trace_id=_id(23)),
        started_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="terminal event"):
        attempt.complete(
            state=AnswerQuestionAttemptState.FAILED,
            outcome=AnswerQuestionOutcome.FAILED,
            reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
            usage=AnswerQuestionUsage(),
            terminal_event=AnswerQuestionAttemptEvent(
                event_id=_id(35),
                workspace_id=attempt.workspace_id,
                environment_id=attempt.environment_id,
                environment=attempt.environment,
                kind=AnswerQuestionAttemptEventKind.COMPLETED,
                occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
            ),
        )

    with pytest.raises(ValidationError):
        AnswerQuestionAttemptEvent.model_validate(
            {
                "event_id": str(_id(35)),
                "kind": "failed",
                "occurred_at": "2026-10-15T00:01:00Z",
                "reason": "provider said secret customer text",
            }
        )

    failure_event = AnswerQuestionAttemptEvent(
        event_id=_id(36),
        workspace_id=attempt.workspace_id,
        environment_id=attempt.environment_id,
        environment=attempt.environment,
        kind=AnswerQuestionAttemptEventKind.FAILED,
        occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
        reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
    )
    with pytest.raises(ValidationError, match="safe failure reason"):
        attempt.complete(
            state=AnswerQuestionAttemptState.FAILED,
            outcome=AnswerQuestionOutcome.ANSWERED,
            reason=SafeAttemptReason.PROVIDER_UNAVAILABLE,
            usage=AnswerQuestionUsage(),
            terminal_event=failure_event,
        )


def test_answer_question_attempt_event_rejects_different_tenant_scope() -> None:
    attempt = AnswerQuestionAttempt.start(
        attempt_id=_id(30),
        run=_run(),
        attempt_number=1,
        execution_pin=_pin(),
        input_artifact_ids=(_id(22),),
        diagnostic_context=DiagnosticContext(trace_id=_id(23)),
        started_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )

    with pytest.raises(ValidationError, match="different scope"):
        attempt.complete(
            state=AnswerQuestionAttemptState.CANCELLED,
            outcome=AnswerQuestionOutcome.CANCELLED,
            reason=SafeAttemptReason.CANCELLED_BY_REQUEST,
            usage=AnswerQuestionUsage(),
            terminal_event=AnswerQuestionAttemptEvent(
                event_id=_id(37),
                workspace_id=_id(99),
                environment_id=attempt.environment_id,
                environment=attempt.environment,
                kind=AnswerQuestionAttemptEventKind.CANCELLED,
                occurred_at=datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc),
                reason=SafeAttemptReason.CANCELLED_BY_REQUEST,
            ),
        )
