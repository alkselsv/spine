from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid5

import pytest

from spine.application.answer_question_attempts import PersistedAnswerQuestionAttemptService
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionAttemptAllocation,
    CanonicalQATerminalizationReceipt,
)
from spine.application.persistence.ask_admission import QA_ASK_ADMISSION_OPERATION
from spine.application.persistence.errors import AnswerQuestionRunConflictError
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.agents import AgentResult, AgentRunStatus
from spine.domain.artifacts import Artifact
from spine.domain.capabilities import AcceptedGroundedAnswer, AnswerDisclosureValidation
from spine.domain.capabilities.answer_question import LineageReference
from spine.domain.capabilities.answer_question_runs import AgentResultRef
from spine.domain.common import ActorKind, ActorRef, SchemaRef
from spine.domain.disclosure import DisclosureDependencySet
from spine.application.qa_sessions import PersistedQASessionService
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecord,
    AnswerDisclosureRecordV2,
    AnswerExecutionCheckpoint,
    parse_answer_disclosure_record,
)
from tests.application.persistence.test_ask_admission import (
    NOW,
    RUN_ID,
    TRACE_ID,
    WORKSPACE_ID,
    ENVIRONMENT_ID,
    _command,
    _context,
    _persistence,
)
from tests.domain.capabilities.test_answer_question_runs import _pin


class _PersistedArtifactReader:
    def __init__(self, *, agent_result, artifacts) -> None:
        self.agent_result = agent_result
        self.artifacts = {artifact.id: artifact for artifact in artifacts}
        self.reads: list[tuple[str, UUID]] = []

    async def load_agent_result(self, result_id, **_scope):
        self.reads.append(("agent_result", result_id))
        return self.agent_result if result_id == UUID(int=930) else None

    async def load(self, artifact_id, **_scope):
        self.reads.append(("artifact", artifact_id))
        return self.artifacts.get(artifact_id)

    async def verify_lineage(self, artifact, **_scope):
        self.reads.append(("lineage", artifact.id))


def _answered_execution(admitted, attempt, pin, *, bundle=None):
    from tests.domain.capabilities.test_answer_question_candidate import candidate_output

    if bundle is None:
        from tests.domain.context_bundle.test_context_bundle_models import context_bundle

        source_bundle = context_bundle()
        bundle_values = source_bundle.model_dump(mode="python")
        identity_map = {
            UUID(int=1): WORKSPACE_ID,
            UUID(int=2): ENVIRONMENT_ID,
            UUID(int=8): admitted.run_id,
            UUID(int=9): attempt.attempt_id,
        }

        def replace_identity(value):
            if isinstance(value, UUID):
                return identity_map.get(value, value)
            if isinstance(value, dict):
                return {key: replace_identity(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return tuple(replace_identity(item) for item in value)
            if isinstance(value, list):
                return [replace_identity(item) for item in value]
            return value

        bundle_values = replace_identity(bundle_values)
        bundle_values["idempotency_key"] = "durable-answer-context"
        bundle = type(source_bundle).create(**bundle_values)

    candidate_values = candidate_output().model_dump(mode="python")
    question = candidate_values["question"]
    question["scope"] = {"workspace_id": WORKSPACE_ID, "environment_id": ENVIRONMENT_ID}
    question["correlation"].update(
        {"run_id": admitted.run_id, "attempt_id": attempt.attempt_id, "trace_id": TRACE_ID}
    )
    candidate_values["context_bundle"] = bundle.lineage_reference.model_dump(mode="python")
    answer = candidate_values["answer"]
    answer.update(
        {
            "workspace_id": WORKSPACE_ID,
            "environment_id": ENVIRONMENT_ID,
            "run_id": admitted.run_id,
            "attempt_id": attempt.attempt_id,
        }
    )
    for claim in answer["claims"]:
        claim.update(
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "run_id": admitted.run_id,
                "attempt_id": attempt.attempt_id,
            }
        )
    for relationship in answer["relationships"]:
        relationship.update(
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "run_id": admitted.run_id,
                "attempt_id": attempt.attempt_id,
            }
        )
    for citation in answer["citations"]:
        citation["context_bundle"] = bundle.lineage_reference.model_dump(mode="python")
        citation["evidence_reference"]["workspace_id"] = WORKSPACE_ID
        citation["evidence_reference"]["environment_id"] = ENVIRONMENT_ID
    from spine.domain.capabilities import AnswerContent

    accepted_answer_content = AnswerContent.model_validate(answer)
    dependencies = DisclosureDependencySet.from_context_bundle(bundle)
    validation = AnswerDisclosureValidation(
        citation_validation_reference=UUID(int=931),
        semantic_support_validation_reference=UUID(int=932),
        evaluation_lineage_reference=UUID(int=933),
        disclosure_eligibility_reference=UUID(int=934),
        context_bundle_id=bundle.bundle_id,
        context_bundle_version=bundle.bundle_version,
        context_bundle_digest=bundle.bundle_digest,
        claim_ids=tuple(claim.claim_id for claim in accepted_answer_content.claims),
        citation_ids=tuple(citation.citation_id for citation in accepted_answer_content.citations),
        dependency_ids=tuple(item.dependency_id for item in dependencies.dependencies),
    )
    accepted_answer = AcceptedGroundedAnswer(
        answer=accepted_answer_content,
        validation=validation,
        accepted_at=NOW,
    )
    output = AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=admitted.run_id, attempt_id=attempt.attempt_id, trace_id=TRACE_ID
        ),
        scope=AnswerQuestionScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID),
        outcome=AnswerQuestionOutcome.ANSWERED,
        answer=accepted_answer_content,
        lineage=(
            LineageReference(kind="context_bundle", identifier=bundle.bundle_id, schema_version="context-bundle:v1"),
        ),
    )
    record = AnswerDisclosureRecordV2(
        request_id=uuid5(admitted.run_id, "context-request"),
        request_digest="b" * 64,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        run_id=admitted.run_id,
        attempt_id=attempt.attempt_id,
        outcome=AnswerQuestionOutcome.ANSWERED,
        validation=validation,
        accepted_answer=accepted_answer,
        terminal_output=output,
        created_at=NOW,
        execution_checkpoint=AnswerExecutionCheckpoint(
            execution_pin_digest=pin.pin_digest,
            context_bundle_ref=bundle.reference,
            candidate_input_artifact_id=UUID(int=935),
            agent_result_ref=AgentResultRef(result_id=UUID(int=930)),
            output_artifact_ids=(UUID(int=937),),
        ),
    )
    restored = parse_answer_disclosure_record(record.model_dump(mode="json"))
    assert restored == record
    historical_projection = record.as_v1()
    assert historical_projection.schema_version == "answer-disclosure-record:v1"
    assert "execution_checkpoint" not in historical_projection.model_dump(mode="json")
    input_artifact = Artifact(
        id=UUID(int=935), workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID,
        work_item_id=admitted.run_id, produced_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=936)),
        artifact_schema=SchemaRef(name="qa.candidate-input", version="1"), content={"kind": "synthetic"},
    )
    output_artifact = Artifact(
        id=UUID(int=937), workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID,
        work_item_id=admitted.run_id, produced_by=ActorRef(kind=ActorKind.AGENT, id=UUID(int=938)),
        produced_by_agent_version_id=pin.agent_version.agent_version_id,
        artifact_schema=SchemaRef(name="qa.candidate-output", version="1"),
        content={"kind": "synthetic"}, upstream_artifact_ids=(input_artifact.id,),
    )
    result = AgentResult(status=AgentRunStatus.SUCCEEDED, output_artifact_ids=(output_artifact.id,), trace_ref="synthetic")
    return bundle, dependencies, accepted_answer, output, record, input_artifact, output_artifact, result


@pytest.mark.parametrize(
    ("outcome", "safe_reason", "authorization", "attempt_state"),
    [
        (
            AnswerQuestionOutcome.DENIED,
            AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
            AnswerAuthorizationOutcome.DENIED,
            "succeeded",
        ),
        (
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
            AnswerAuthorizationOutcome.ALLOWED,
            "succeeded",
        ),
        (
            AnswerQuestionOutcome.CANCELLED,
            AnswerQuestionSafeReasonCode.CANCELLED,
            AnswerAuthorizationOutcome.ALLOWED,
            "cancelled",
        ),
        (
            AnswerQuestionOutcome.FAILED,
            AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
            AnswerAuthorizationOutcome.ALLOWED,
            "failed",
        ),
    ],
)
@pytest.mark.parametrize("conflict", (False, True))
@pytest.mark.parametrize("recover_from_disclosure", (False, True))
@pytest.mark.asyncio
async def test_terminalization_commits_safe_attempt_run_and_message_together(
    outcome: AnswerQuestionOutcome,
    safe_reason: AnswerQuestionSafeReasonCode,
    authorization: AnswerAuthorizationOutcome,
    attempt_state: str,
    conflict: bool,
    recover_from_disclosure: bool,
) -> None:
    persistence, boundary = await _persistence()
    admission_context = _context(boundary, operation=QA_ASK_ADMISSION_OPERATION)
    async with persistence.uow_factory(admission_context) as uow:
        admitted = await uow.ask_admissions.admit(_command())
        run = await uow.answer_question_runs.resolve_run(admitted.run_id)
        await uow.answer_question_runs.start_run(run.begin_execution())
        pin = _pin(WORKSPACE_ID, ENVIRONMENT_ID)
        allocation = await uow.answer_question_runs.allocate_or_resume_attempt(
            AnswerQuestionAttemptAllocation(
                run_id=admitted.run_id,
                logical_execution_key="durable-ask:terminalize-denied",
                execution_pin=pin,
                input_artifact_ids=run.input_artifact_ids,
            )
        )
        attempt = allocation.attempt
        output = AnswerQuestionOutput(
            correlation=RunAttemptCorrelation(
                run_id=admitted.run_id,
                attempt_id=attempt.attempt_id,
                trace_id=TRACE_ID,
            ),
            scope=AnswerQuestionScope(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
            ),
            outcome=outcome,
            reason=AnswerQuestionSafeReason(
                code=safe_reason
            ),
        )
        record_type = AnswerDisclosureRecordV2 if recover_from_disclosure else AnswerDisclosureRecord
        record_fields = dict(
            request_id=uuid5(admitted.run_id, "context-request"),
            request_digest="a" * 64,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            run_id=admitted.run_id,
            attempt_id=attempt.attempt_id,
            outcome=outcome,
            safe_output=output,
            terminal_output=output,
            created_at=NOW,
        )
        if recover_from_disclosure:
            record = record_type(
                **record_fields,
                execution_checkpoint=AnswerExecutionCheckpoint(
                    execution_pin_digest=pin.pin_digest
                ),
            )
        else:
            record = record_type(**record_fields)
        await uow.answer_disclosures.save(record)
        await uow.commit()

    terminalizer = PersistedAnswerQuestionAttemptService(
        uow_factory=persistence.uow_factory,
        execution=object(),  # not used during finalization
        clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
        event_id_factory=lambda: UUID(int=901),
    )
    session_service = PersistedQASessionService(
        repository=object(),
        disclosure_eligibility=object(),
        clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
        command_id_factory=lambda: UUID(int=902),
    )
    receipt = CanonicalQATerminalizationReceipt(
        run_id=admitted.run_id,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        execution_pin=pin,
        output=output,
        context_bundle_ref=None,
        agent_result_ref=None,
        agent_result=None,
        output_artifact_ids=(),
        candidate_input_artifact_id=None,
        candidate_output_artifacts=(),
        disclosure_record=record,
        authorization=authorization,
    )

    if conflict:
        conflicting_outcome = (
            AnswerQuestionOutcome.ABSTAINED
            if outcome is AnswerQuestionOutcome.DENIED
            else AnswerQuestionOutcome.DENIED
        )
        async with persistence.uow_factory(
            _context(boundary, operation=PersistenceOperation("qa_sessions"))
        ) as uow:
            await session_service.complete_message_in_transaction(
                repository=uow.qa_sessions,
                context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
                session_id=admitted.session_id,
                message_id=admitted.message_id,
                answer=None,
                outcome=conflicting_outcome,
                cancelled=False,
                idempotency_key=IdempotencyKey("preexisting-terminal-message"),
            )
            await uow.commit()

    if conflict:
        with pytest.raises(AnswerQuestionRunConflictError):
            await terminalizer.terminalize(
                context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
                session_id=admitted.session_id,
                message_id=admitted.message_id,
                idempotency_key="durable-ask:terminalize-denied",
                sessions=session_service,
                receipt=None if recover_from_disclosure else receipt,
            )
    else:
        result = await terminalizer.terminalize(
            context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
            session_id=admitted.session_id,
            message_id=admitted.message_id,
            idempotency_key="durable-ask:terminalize-denied",
            sessions=session_service,
            receipt=None if recover_from_disclosure else receipt,
        )

    async with persistence.uow_factory(
        _context(boundary, operation=PersistenceOperation("qa_sessions"))
    ) as uow:
        run = await uow.answer_question_runs.resolve_run(RUN_ID)
        stored_attempt = await uow.answer_question_runs.resolve_attempt(attempt.attempt_id)
        message = await uow.qa_sessions.resolve_message(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            session_id=admitted.session_id,
            message_id=admitted.message_id,
        )
        await uow.commit()

    assert message is not None
    if conflict:
        assert run.state.value == "running"
        assert run.final_attempt_id is None
        assert stored_attempt.state.value == "running"
        assert message.state.value == "terminal"
        assert message.terminal_outcome is not outcome
    else:
        assert result.output == output
        assert run.state.value == "completed"
        assert run.final_attempt_id == attempt.attempt_id
        assert run.outcome is outcome
        assert stored_attempt.state.value == attempt_state
        assert stored_attempt.outcome is outcome
        assert message.state.value == "terminal"
        assert message.terminal_outcome is outcome

        replay = await terminalizer.terminalize(
            context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
            session_id=admitted.session_id,
            message_id=admitted.message_id,
            idempotency_key="durable-ask:terminalize-denied",
            sessions=session_service,
            receipt=None,
        )
        assert replay.output == output


@pytest.mark.parametrize("recover_from_disclosure", (False, True))
@pytest.mark.parametrize(
    "lineage_fault",
    (
        None,
        "missing_agent_result",
        "foreign_workspace",
        "other_attempt",
        "wrong_artifact",
        "stale_pin",
    ),
)
@pytest.mark.asyncio
async def test_answered_terminalization_reads_back_real_agent_and_artifact_lineage(
    recover_from_disclosure: bool,
    lineage_fault: str | None,
) -> None:
    persistence, boundary = await _persistence()
    admission_context = _context(boundary, operation=QA_ASK_ADMISSION_OPERATION)
    async with persistence.uow_factory(admission_context) as uow:
        admitted = await uow.ask_admissions.admit(_command())
        run = await uow.answer_question_runs.resolve_run(admitted.run_id)
        await uow.answer_question_runs.start_run(run.begin_execution())
        pin = _pin(WORKSPACE_ID, ENVIRONMENT_ID)
        allocation = await uow.answer_question_runs.allocate_or_resume_attempt(
            AnswerQuestionAttemptAllocation(
                run_id=admitted.run_id,
                logical_execution_key="durable-ask:answered-lineage",
                execution_pin=pin,
                input_artifact_ids=run.input_artifact_ids,
            )
        )
        attempt = allocation.attempt
        (
            bundle,
            dependencies,
            _accepted,
            output,
            record,
            input_artifact,
            output_artifact,
            agent_result,
        ) = _answered_execution(admitted, attempt, pin)
        stale_pin = None
        if lineage_fault == "stale_pin":
            from tests.application.test_answer_question_execution import configuration
            from spine.domain.capabilities.answer_question_execution import AnswerQuestionExecutionPin

            stale_pin = AnswerQuestionExecutionPin.create(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                environment=pin.environment,
                configuration=configuration(
                    workspace_id=WORKSPACE_ID,
                    environment_id=ENVIRONMENT_ID,
                ),
            )
            record = AnswerDisclosureRecordV2.model_validate(
                {
                    **record.model_dump(mode="python"),
                    "execution_checkpoint": {
                        **record.execution_checkpoint.model_dump(mode="python"),
                        "execution_pin_digest": stale_pin.pin_digest,
                    },
                }
            )
        await uow.context_bundles.add(bundle)
        await uow.answer_disclosures.save(record)
        await uow.commit()

    persisted_result = None if lineage_fault == "missing_agent_result" else agent_result
    if lineage_fault == "foreign_workspace":
        persisted_output_artifact = output_artifact.model_copy(update={"workspace_id": UUID(int=999)})
    else:
        persisted_output_artifact = output_artifact
    if lineage_fault == "wrong_artifact":
        persisted_result = agent_result.model_copy(
            update={"output_artifact_ids": (UUID(int=949),)}
        )
    artifact_reader = _PersistedArtifactReader(
        agent_result=persisted_result,
        artifacts=(input_artifact, persisted_output_artifact),
    )
    terminalizer = PersistedAnswerQuestionAttemptService(
        uow_factory=persistence.uow_factory,
        execution=object(),
        clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
        event_id_factory=lambda: UUID(int=939),
        artifacts=artifact_reader,
    )
    session_service = PersistedQASessionService(
        repository=object(),
        disclosure_eligibility=object(),
        clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
        command_id_factory=lambda: UUID(int=940),
    )
    receipt_pin = stale_pin if stale_pin is not None else pin
    receipt = CanonicalQATerminalizationReceipt(
        run_id=admitted.run_id,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        execution_pin=receipt_pin,
        output=output,
        context_bundle_ref=bundle.reference,
        agent_result_ref=AgentResultRef(result_id=UUID(int=930)),
        agent_result=agent_result,
        output_artifact_ids=agent_result.output_artifact_ids,
        candidate_input_artifact_id=input_artifact.id,
        candidate_output_artifacts=(output_artifact,),
        disclosure_record=record,
        authorization=AnswerAuthorizationOutcome.ALLOWED,
        disclosure_dependencies=dependencies,
    )

    if lineage_fault == "other_attempt":
        with pytest.raises(ValueError, match="identity"):
            replace(receipt, attempt_id=UUID(int=941))
    else:
        call = terminalizer.terminalize(
            context=_context(boundary, operation=PersistenceOperation("qa_sessions")),
            session_id=admitted.session_id,
            message_id=admitted.message_id,
            idempotency_key="durable-ask:answered-lineage",
            sessions=session_service,
            receipt=None if recover_from_disclosure else receipt,
        )
        if lineage_fault is None:
            terminal = await call
        else:
            with pytest.raises(AnswerQuestionRunConflictError):
                await call

    async with persistence.uow_factory(
        _context(boundary, operation=PersistenceOperation("qa_sessions"))
    ) as uow:
        run = await uow.answer_question_runs.resolve_run(admitted.run_id)
        stored_attempt = await uow.answer_question_runs.resolve_attempt(attempt.attempt_id)
        message = await uow.qa_sessions.resolve_message(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            session_id=admitted.session_id,
            message_id=admitted.message_id,
        )
        await uow.commit()

    assert message is not None
    if lineage_fault is None:
        assert terminal.output == output
        assert run.state.value == "completed"
        assert run.final_attempt_id == attempt.attempt_id
        assert run.output_artifact_ids == agent_result.output_artifact_ids
        assert stored_attempt.state.value == "succeeded"
        assert stored_attempt.context_bundle_ref == bundle.reference
        assert stored_attempt.agent_result_ref == AgentResultRef(result_id=UUID(int=930))
        assert message.state.value == "terminal"
        assert message.terminal_outcome is AnswerQuestionOutcome.ANSWERED
    else:
        assert run.state.value == "running"
        assert run.final_attempt_id is None
        assert stored_attempt.state.value == "running"
        assert message.state.value == "pending"
