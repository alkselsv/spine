from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.domain.capabilities import (
    AnswerQuestionInput,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    QuestionSubject,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from spine.domain.context_bundles import ContextBundleEvidenceReference
from spine.domain.projections import ProjectionFreshness
from spine.evals.production_contract import (
    ProductionContractCaseRequest,
    ProductionContractObservation,
)
from spine.evals.qa_r1 import load_r1_dataset
from spine.evals.unauthorized_retrieval import (
    UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM,
    ProductionContractSafetyGate,
)
from spine.memory.contracts import (
    ContextOutcome,
    ContextReasonCode,
    ContextReference,
    ContextResponse,
    EvidenceReference,
    ProjectionChunk,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    RetrievalReadContext,
    RetrievalResult,
    StableLocator,
)

from tests.domain.context_bundle.test_context_bundle_models import context_bundle
from tests.evals.test_production_contract import _safe_observation


def _request(case_id: str = "allowed-acl") -> ProductionContractCaseRequest:
    case = next(case for case in load_r1_dataset().cases if case.case_id == case_id)
    if case_id == "allowed-acl":
        return ProductionContractCaseRequest(
            case_id=case_id,
            question=case.question,
            language=case.language,
            workspace_id=UUID(int=1),
            environment_id=UUID(int=2),
            principal_id=UUID(int=30),
            available_revision_ids=(UUID(int=21),),
            retrieval_strategy=case.retrieval_strategy,
        )
    return ProductionContractCaseRequest.from_case(case)


def _allowed_observation(
    *, run_id: UUID | None = None, attempt_id: UUID | None = None
) -> ProductionContractObservation:
    case = next(case for case in load_r1_dataset().cases if case.case_id == "allowed-acl")
    reference = ContextBundleEvidenceReference(
        evidence_id="evidence-1",
        schema_version="context-evidence-reference:v2",
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        projection_snapshot_id=UUID(int=4),
        activation_generation=1,
        source_object_id=UUID(int=20),
        source_revision_id=UUID(int=21),
        original_sha256="a" * 64,
        revision_digest="6" * 64,
        revision_number=1,
        processing_receipt_id=UUID(int=22),
        projected_kind="chunk",
        locator_contract_version="locator:v1",
        locator={"kind": "page", "value": "1"},
        locator_digest="b" * 64,
        evidence_digest="7" * 64,
    )
    question = AnswerQuestionInput(
        subject=QuestionSubject(text=case.question),
        scope=AnswerQuestionScope(workspace_id=UUID(int=1), environment_id=UUID(int=2)),
        correlation=RunAttemptCorrelation(
            run_id=run_id or uuid5(NAMESPACE_URL, "r1:allowed:run"),
            attempt_id=attempt_id or uuid5(NAMESPACE_URL, "r1:allowed:attempt"),
            trace_id=uuid5(NAMESPACE_URL, "r1:allowed:trace"),
        ),
    )
    bundle = context_bundle(
        publication_pin_id=UUID(int=25),
        run_id=question.correlation.run_id,
        attempt_id=question.correlation.attempt_id,
        evidence_references=(reference,),
    )
    read_context = RetrievalReadContext(
        workspace_id=bundle.workspace_id,
        environment_id=bundle.environment_id,
        publication_pin_id=bundle.publication_pin_id,
        publication_pin_digest="c" * 64,
        projection_snapshot_id=bundle.projection_snapshot.projection_snapshot_id,
        projection_activation_id=bundle.projection_activation.projection_activation_id,
        activation_generation=bundle.activation_generation,
        canonical_boundary_id=bundle.canonical_boundary.boundary_id,
        canonical_boundary_digest=bundle.canonical_boundary.manifest_digest,
        projection_run_id=bundle.projection_snapshot.projection_run_id,
        projection_candidate_id=bundle.projection_snapshot.projection_candidate_id,
        projection_partition_id=UUID(int=26),
        projection_receipt_id=UUID(int=27),
        physical_namespace="qa:active",
        physical_namespace_digest="d" * 64,
        source_object_ids=(reference.source_object_id,),
        source_revision_ids=(reference.source_revision_id,),
        provider_capability_reference="provider:qa",
        source_isolation_pre_retrieval_enforced=True,
    )
    projection_reference = EvidenceReference(
        evidence_id=reference.evidence_id,
        workspace_id=reference.workspace_id,
        environment_id=reference.environment_id,
        source_object_id=reference.source_object_id,
        source_revision_id=reference.source_revision_id,
        revision_digest=reference.revision_digest,
        revision_number=reference.revision_number,
        processing_receipt_id=reference.processing_receipt_id,
        locator_contract_version=reference.locator_contract_version,
        locator=StableLocator(kind="page", value="1"),
        evidence_digest=reference.evidence_digest,
    )
    projection = ProjectionRetrievalResult(
        outcome=ProjectionOutcome.SUCCEEDED,
        command_identity="retrieve-allowed",
        command_digest="e" * 64,
        chunks=(
            ProjectionChunk(
                chunk_id="chunk-1",
                text="protected evidence",
                evidence=(projection_reference,),
                read_context=read_context,
                read_contexts=(read_context,),
            ),
        ),
        evidence=(projection_reference,),
        retrieval_strategy=case.retrieval_strategy,
        logical_projection_version="logical:1",
        physical_projection_version="physical:1",
        read_context=read_context,
        read_contexts=(read_context,),
    )
    retrieval = RetrievalResult(
        outcome=ContextOutcome.ALLOWED,
        retrieval_strategy=case.retrieval_strategy,
        index_version=UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM,
        context=("protected evidence",),
        references=(
            ContextReference(
                source_id=str(reference.source_object_id),
                source_revision_id=reference.source_revision_id,
                workspace_id=reference.workspace_id,
                environment_id=reference.environment_id,
                evidence_id=reference.evidence_id,
                locator=reference.locator.model_dump(mode="json"),
            ),
        ),
    )
    answer = AnswerQuestionOutput(
        correlation=question.correlation,
        scope=question.scope,
        outcome=AnswerQuestionOutcome.ABSTAINED,
        reason=AnswerQuestionSafeReason(code=AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE),
    )
    return ProductionContractObservation(
        case_id=case.case_id,
        question=question,
        context_response=ContextResponse(
            outcome=ContextOutcome.ALLOWED,
            context_bundle_ref=bundle.reference,
            reason_code=ContextReasonCode.ALLOWED,
            publication_pin_id=bundle.publication_pin_id,
        ),
        context_bundle=bundle,
        projection_retrieval=projection,
        retrieval=retrieval,
        answer=answer,
        disclosure=AnswerDisclosureRecord(
            request_id=UUID(int=28),
            request_digest="f" * 64,
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            run_id=question.correlation.run_id,
            attempt_id=question.correlation.attempt_id,
            outcome=answer.outcome,
            safe_output=answer,
            created_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
        ),
    )


def test_safety_gate_accepts_authorized_current_bundle_and_safe_non_answer() -> None:
    result = ProductionContractSafetyGate().evaluate(
        request=_request(), observation=_allowed_observation()
    )

    assert result.status == "passed"
    assert result.violation_codes == ()
    assert result.seam == UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM
    assert result.protected_payload_disclosed is False


def test_safety_gate_accepts_denied_observation_without_source_disclosure() -> None:
    case = next(case for case in load_r1_dataset().cases if case.case_id == "denied-acl")
    observation = _safe_observation(case, denied=True)

    result = ProductionContractSafetyGate().evaluate(
        request=ProductionContractCaseRequest.from_case(case), observation=observation
    )

    assert result.status == "passed"
    assert result.protected_payload_disclosed is False


def test_safety_gate_rejects_answer_after_authorization_denial() -> None:
    case = next(case for case in load_r1_dataset().cases if case.case_id == "denied-acl")
    observation = _safe_observation(case, denied=True)
    object.__setattr__(observation.answer, "outcome", AnswerQuestionOutcome.ANSWERED)
    object.__setattr__(observation.answer, "reason", None)

    result = ProductionContractSafetyGate().evaluate(
        request=ProductionContractCaseRequest.from_case(case), observation=observation
    )

    assert result.status == "failed"
    assert "contract_violation" in result.violation_codes
    assert result.protected_payload_disclosed is True


def test_safety_gate_rejects_stale_authorized_bundle_without_failure_payload() -> None:
    observation = _allowed_observation()
    assert observation.context_bundle is not None
    stale_bundle = context_bundle(
        publication_pin_id=UUID(int=25),
        run_id=observation.question.correlation.run_id,
        attempt_id=observation.question.correlation.attempt_id,
        freshness=ProjectionFreshness.STALE,
        evidence_references=observation.context_bundle.evidence_references,
    )
    observation = observation.model_copy(update={"context_bundle": stale_bundle})

    result = ProductionContractSafetyGate().evaluate(
        request=_request(), observation=observation
    )

    assert result.status == "failed"
    assert result.protected_payload_disclosed is False
    assert "protected evidence" not in result.model_dump_json()


def test_safety_gate_rejects_provider_output_before_denial() -> None:
    case = next(case for case in load_r1_dataset().cases if case.case_id == "denied-acl")
    observation = _safe_observation(case, denied=True)
    retrieval = observation.retrieval.model_copy(update={"context": ("protected evidence",)})
    forged = observation.model_copy(update={"retrieval": retrieval})

    result = ProductionContractSafetyGate().evaluate(
        request=ProductionContractCaseRequest.from_case(case), observation=forged
    )

    assert result.status == "failed"
    assert result.protected_payload_disclosed is True
    assert "protected evidence" not in result.model_dump_json()
