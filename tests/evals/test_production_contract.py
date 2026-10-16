from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

import spine.evals.offline_runner as offline_runner
from spine.domain.capabilities import (
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    LineageReference,
    QuestionSubject,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from spine.domain.context_bundles import ContextBundleRef
from spine.evals.offline_runner import OfflineEvaluationRunner
from spine.evals.production_contract import (
    PRODUCTION_CONTRACT_OBSERVATION_SEAM,
    ProductionContractApplicationFlow,
    ProductionContractCaseRequest,
    ProductionContractObservation,
    ProductionContractObservationBridge,
    ProductionContractObservationError,
)
from spine.evals.qa_r1 import ExpectedClaim, QACase, load_r1_dataset
from spine.memory.contracts import (
    ContextOutcome,
    ContextReasonCode,
    ContextResponse,
    ProjectionRetrievalResult,
    RetrievalResult,
)


def _question(case: QACase) -> AnswerQuestionInput:
    return AnswerQuestionInput(
        subject=QuestionSubject(text=case.question),
        scope=AnswerQuestionScope(
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
        ),
        correlation=RunAttemptCorrelation(
            run_id=uuid5(NAMESPACE_URL, f"r1:run:{case.case_id}"),
            attempt_id=uuid5(NAMESPACE_URL, f"r1:attempt:{case.case_id}"),
            trace_id=uuid5(NAMESPACE_URL, f"r1:trace:{case.case_id}"),
        ),
    )


def _safe_observation(case: QACase, *, denied: bool = False) -> ProductionContractObservation:
    question = _question(case)
    outcome = AnswerQuestionOutcome.DENIED if denied else AnswerQuestionOutcome.ABSTAINED
    reason = (
        AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED
        if denied
        else AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE
    )
    answer = AnswerQuestionOutput(
        correlation=question.correlation,
        scope=question.scope,
        outcome=outcome,
        reason=AnswerQuestionSafeReason(code=reason),
    )
    response = ContextResponse(
        outcome=ContextOutcome.DENIED if denied else ContextOutcome.INSUFFICIENT,
        reason_code=(
            ContextReasonCode.AUTHORIZATION_DENIED
            if denied
            else ContextReasonCode.INSUFFICIENT_EVIDENCE
        ),
    )
    disclosure = AnswerDisclosureRecord(
        request_id=uuid5(NAMESPACE_URL, f"r1:disclosure:{case.case_id}"),
        request_digest="a" * 64,
        workspace_id=question.scope.workspace_id,
        environment_id=question.scope.environment_id,
        run_id=question.correlation.run_id,
        attempt_id=question.correlation.attempt_id,
        outcome=outcome,
        safe_output=answer,
        created_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    )
    return ProductionContractObservation(
        case_id=case.case_id,
        question=question,
        context_response=response,
        context_bundle=None,
        projection_retrieval=ProjectionRetrievalResult(
            outcome="missing",
            command_identity="r1-observation",
            command_digest="b" * 64,
        ),
        retrieval=RetrievalResult(
            retrieval_strategy=case.retrieval_strategy,
            index_version="r1-production-contract-observation:v1",
        ),
        answer=answer,
        disclosure=disclosure,
    )


class SafeFlow(ProductionContractApplicationFlow):
    def __init__(self) -> None:
        self.requests: list[ProductionContractCaseRequest] = []

    async def execute(self, request: ProductionContractCaseRequest) -> ProductionContractObservation:
        self.requests.append(request)
        assert not hasattr(request, "expected_answer")
        assert not hasattr(request, "expected_evidence_ids")
        case = next(case for case in load_r1_dataset().cases if case.case_id == request.case_id)
        return _safe_observation(case, denied=request.case_id == "denied-acl")


@pytest.mark.asyncio
async def test_production_contract_mode_uses_observed_output_without_expected_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("production-contract mode must not call _expected_answer")

    monkeypatch.setattr(offline_runner, "_expected_answer", fail_if_called)
    flow = SafeFlow()
    report = await OfflineEvaluationRunner(
        execution_mode="production-contract",
        production_observer=ProductionContractObservationBridge(flow),
    ).run()

    assert len(flow.requests) == 12
    assert report.execution_mode == "production-contract"
    assert report.observation_seam == PRODUCTION_CONTRACT_OBSERVATION_SEAM
    assert report.qualification_scope == "offline-contract-only"
    observed = {item.case_id: item.observed_outcome for item in report.scenarios}
    assert observed["denied-acl"] == "denied"
    assert observed["absent-evidence-abstention"] == "abstained"
    assert len(report.safety_results) == 12
    assert all(result.status == "passed" for result in report.safety_results)
    grounded_safety = {result.case_id: result for result in report.grounded_answer_safety_results}
    assert len(grounded_safety) == 12
    assert grounded_safety["allowed-acl"].status == "failed"
    assert grounded_safety["allowed-acl"].violation_codes == (
        "answer_outcome_mismatch",
    )
    assert all("protected" not in failure.model_dump_json() for failure in report.failures)


@pytest.mark.asyncio
async def test_production_contract_grounded_safety_fails_independently_of_quality_evaluators() -> None:
    class PassingEvaluator:
        def evaluate(self, *, context: object) -> tuple[object, ...]:
            del context
            return ()

    case = next(
        case for case in load_r1_dataset().cases if case.case_id == "absent-evidence-abstention"
    )
    dataset = load_r1_dataset().model_copy(update={"cases": (case,)})

    class WrongOutcomeFlow(ProductionContractApplicationFlow):
        async def execute(
            self, request: ProductionContractCaseRequest
        ) -> ProductionContractObservation:
            del request
            return _safe_observation(case, denied=True)

    report = await OfflineEvaluationRunner(
        dataset=dataset,
        execution_mode="production-contract",
        production_observer=ProductionContractObservationBridge(WrongOutcomeFlow()),
        case_evaluator=PassingEvaluator(),  # type: ignore[arg-type]
    ).run()

    assert report.evaluations == ()
    assert report.scenarios[0].status == "failed"
    assert report.failures[0].component == "grounded-answer-safety"
    assert report.grounded_answer_safety_results[0].status == "failed"


@pytest.mark.asyncio
async def test_production_contract_observation_rejects_allowed_response_without_bundle() -> None:
    base = _safe_observation(load_r1_dataset().cases[0])
    observation = base.model_copy(
        update={
            "context_response": ContextResponse(
            outcome=ContextOutcome.ALLOWED,
            context_bundle_ref=ContextBundleRef(
                bundle_id=UUID(int=40), bundle_version=1, bundle_digest="b" * 64
            ),
            reason_code=ContextReasonCode.ALLOWED,
            )
        }
    )

    with pytest.raises(ProductionContractObservationError, match="context bundle"):
        observation.validate()


def test_answered_output_requires_an_accepted_disclosure_record() -> None:
    from tests.evals.test_unauthorized_retrieval_safety import _allowed_observation

    observation = _allowed_observation()
    object.__setattr__(observation.answer, "outcome", AnswerQuestionOutcome.ANSWERED)
    object.__setattr__(observation.answer, "reason", None)
    object.__setattr__(observation.disclosure, "outcome", AnswerQuestionOutcome.ANSWERED)
    object.__setattr__(observation.disclosure, "safe_output", observation.answer)

    with pytest.raises(ProductionContractObservationError, match="accepted disclosure"):
        observation.validate()


def test_production_contract_mapping_preserves_non_answer_outcome() -> None:
    case = load_r1_dataset().cases[1]
    question = _question(case)
    output = AnswerQuestionOutput(
        correlation=question.correlation,
        scope=question.scope,
        outcome=AnswerQuestionOutcome.FAILED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
        ),
    )

    mapped = offline_runner._answer_submission_from_output(output)

    assert mapped.outcome == "failed"


def test_production_contract_mapping_aligns_opaque_claim_id_to_exact_gold_label() -> None:
    from tests.domain.capabilities.test_answer_question_candidate import candidate_output

    candidate = candidate_output()
    answer = AnswerQuestionOutput(
        correlation=candidate.question.correlation,
        scope=candidate.question.scope,
        outcome=AnswerQuestionOutcome.ANSWERED,
        answer=candidate.answer,
        lineage=(
            LineageReference(
                kind="context_bundle",
                identifier=candidate.context_bundle.bundle.bundle_id,
                schema_version="context-bundle:v1",
            ),
        ),
    )
    base_case = load_r1_dataset().cases[0]
    case = base_case.model_copy(
        update={
            "expected_claims": (
                ExpectedClaim(
                    claim_id="known-label",
                    text=candidate.answer.claims[0].text,
                    evidence_ids=(base_case.expected_evidence_ids[0],),
                ),
            )
        }
    )

    mapped = offline_runner._answer_submission_from_output(answer, case=case)

    assert mapped.claims[0].claim_id == "known-label"
    assert mapped.claims[0].text == candidate.answer.claims[0].text


@pytest.mark.asyncio
async def test_production_contract_bridge_exception_is_safe_and_non_disclosing() -> None:
    class FailingFlow(ProductionContractApplicationFlow):
        async def execute(self, request: ProductionContractCaseRequest) -> ProductionContractObservation:
            del request
            raise RuntimeError("protected evidence must not be copied")

    report = await OfflineEvaluationRunner(
        execution_mode="production-contract",
        production_observer=ProductionContractObservationBridge(FailingFlow()),
    ).run()

    assert all(scenario.status == "failed" for scenario in report.scenarios)
    assert all(failure.component == "production-contract-observation" for failure in report.failures)
    assert all("protected" not in failure.model_dump_json() for failure in report.failures)
