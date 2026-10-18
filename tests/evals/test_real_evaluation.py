from __future__ import annotations

from uuid import UUID

import pytest

from spine.evals.production_contract import ProductionContractObservationError
from spine.evals.qa_r1 import load_r1_dataset
from spine.evals.real_evaluation import (
    FinalizedRealObservation,
    RealEvaluationAdapter,
    RealEvaluationCase,
    RealEvaluationContractError,
    RealEvaluationEvidenceProvenance,
    RealEvaluationPopulation,
    RealEvaluationRuntimeIdentity,
    RealCaseEvaluationResult,
    RealMetricResult,
    aggregate_real_evaluations,
    evaluate_real_case,
)
from spine.evals.real_qa_r1 import (
    DOCXLocator,
    REAL_R1_DATASET_DIGEST,
    PDFLocator,
    REAL_R1_CASE_SCHEMA_VERSION,
    RealQACase,
    RealRuntimeRevisionBinding,
    RealRuntimeCaseResolution,
    XLSXLocator,
    runtime_revision_bindings_digest,
)
from tests.evals.test_production_contract import _safe_observation
from tests.evals.test_unauthorized_retrieval_safety import _allowed_observation


WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000101")
ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-000000000201")
SOURCE_OBJECT_IDS = tuple(UUID(int=value) for value in (401, 402, 403))
SOURCE_REVISION_IDS = tuple(UUID(int=value) for value in (501, 502, 503))
SHA256 = "a" * 64


def _runtime_identity(observation) -> RealEvaluationRuntimeIdentity:
    return RealEvaluationRuntimeIdentity.from_observation(
        observation,
        model_version="model-profile:test:v1",
        prompt_version="prompt:test:v1",
        validation_policy_version="real-gold-validation:v1",
    )


def _binding(position: int, *, locator_kind: str) -> RealRuntimeRevisionBinding:
    return RealRuntimeRevisionBinding(
        manifest_sha256="b" * 64,
        dataset_source_id=f"source:opaque:{locator_kind}",
        dataset_revision_id=f"revision:opaque:{locator_kind}",
        upload_identity=f"approved.{locator_kind}",
        ingestion_intent_id=f"intent:{locator_kind}",
        owner_approval_ref="approval:test-fixture",
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_object_id=SOURCE_OBJECT_IDS[position],
        source_revision_id=SOURCE_REVISION_IDS[position],
        canonical_acceptance_id=UUID(int=601 + position),
        original_sha256=SHA256,
    )


def test_real_evaluation_case_preserves_opaque_identity_and_exact_locator_variants() -> None:
    locators = (
        PDFLocator(
            kind="pdf_page",
            schema_version="locator:pdf-page-char:v1",
            page=7,
            char_start=11,
            char_end=29,
        ),
        DOCXLocator(
            kind="docx_paragraph",
            schema_version="locator:docx-paragraph:v1",
            paragraph=3,
            char_start=2,
            char_end=19,
            heading="Approved heading",
        ),
        XLSXLocator(
            kind="xlsx_cell_range",
            schema_version="locator:xlsx-cell:v1",
            sheet="Estimate 2026",
            row=14,
            cell_range="C14:F14",
        ),
    )
    citations = tuple(
        {
            "evidence_id": f"evidence:opaque:{position}",
            "source_id": f"source:opaque:{locator.kind}",
            "source_revision_id": f"revision:opaque:{locator.kind}",
            "excerpt": f"Synthetic excerpt {position}",
            "locator": locator,
        }
        for position, locator in enumerate(locators)
    )
    case = RealQACase.model_validate(
        {
            "case_schema_version": REAL_R1_CASE_SCHEMA_VERSION,
            "case_id": "case:opaque:preservation",
            "question": "Synthetic question?",
            "language": "en",
            "expected_outcome": "answered",
            "expected_answer": "Synthetic answer.",
            "expected_claims": (
                {
                    "claim_id": "claim:opaque:1",
                    "text": "Synthetic answer.",
                    "evidence_ids": tuple(item["evidence_id"] for item in citations[:2]),
                },
            ),
            "expected_evidence_ids": tuple(item["evidence_id"] for item in citations),
            "citations": citations,
            "source_ids": tuple(item["source_id"] for item in citations),
            "source_revision_ids": tuple(item["source_revision_id"] for item in citations),
            "available_source_revision_ids": tuple(
                item["source_revision_id"] for item in citations
            ),
            "question_type": "fact_lookup",
            "difficulty": "hard",
            "synthetic": False,
        }
    )

    evaluation_case = RealEvaluationCase.from_case(
        case,
        dataset_version="0.3.9-user-approved",
        dataset_sha256=REAL_R1_DATASET_DIGEST,
        bindings=tuple(
            _binding(position, locator_kind=locator.kind)
            for position, locator in enumerate(locators)
        ),
    )

    assert evaluation_case.schema_version == "r1-real-evaluation-case:v1"
    assert evaluation_case.case_id == "case:opaque:preservation"
    assert tuple(item.dataset_source_id for item in evaluation_case.provenance) == case.source_ids
    assert tuple(item.dataset_revision_id for item in evaluation_case.provenance) == case.source_revision_ids
    assert tuple(item.canonical_source_object_id for item in evaluation_case.provenance) == SOURCE_OBJECT_IDS
    assert tuple(item.canonical_source_revision_id for item in evaluation_case.provenance) == SOURCE_REVISION_IDS
    assert tuple(item.locator for item in evaluation_case.provenance) == locators
    assert evaluation_case.expected_citation_count == 3
    assert evaluation_case.expected_claim_evidence_count == 2


def _synthetic_resolution() -> RealRuntimeCaseResolution:
    case = load_r1_dataset().cases[1]
    bindings = tuple(
        RealRuntimeRevisionBinding(
            manifest_sha256="b" * 64,
            dataset_source_id=f"dataset-source:{position}",
            dataset_revision_id=f"dataset-revision:{position}",
            upload_identity=f"approved.synthetic.{position}",
            ingestion_intent_id=f"intent:synthetic:{position}",
            owner_approval_ref="approval:test-fixture",
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            source_object_id=UUID(int=700 + position),
            source_revision_id=revision_id,
            canonical_acceptance_id=UUID(int=800 + position),
            original_sha256=SHA256,
        )
        for position, revision_id in enumerate(case.available_revision_ids, start=1)
    )
    from spine.evals.production_contract import ProductionContractCaseRequest

    return RealRuntimeCaseResolution(
        request=ProductionContractCaseRequest.from_case(case),
        manifest_sha256="b" * 64,
        binding_sha256=runtime_revision_bindings_digest(bindings),
        bindings=bindings,
        authorization_generation=1,
    )


def test_gold_provider_is_inaccessible_until_observation_is_validated_and_finalized() -> None:
    case = load_r1_dataset().cases[1]
    observation = _safe_observation(case)
    resolution = _synthetic_resolution()

    finalized = FinalizedRealObservation.from_observation(
        observation=observation,
        resolution=resolution,
    )

    class GoldProvider:
        dataset_version = "0.3.9-user-approved"
        dataset_sha256 = REAL_R1_DATASET_DIGEST

        def __init__(self) -> None:
            self.calls: list[str] = []

        def load_case(self, finalized: FinalizedRealObservation) -> RealQACase:
            case_id = finalized.observation.case_id
            self.calls.append(case_id)
            return RealQACase(
                case_schema_version=REAL_R1_CASE_SCHEMA_VERSION,
                case_id=case_id,
                question="Synthetic abstention question?",
                language="en",
                expected_outcome="abstained",
                expected_reason="insufficient_evidence",
                source_ids=tuple(binding.dataset_source_id for binding in resolution.bindings),
                source_revision_ids=tuple(
                    binding.dataset_revision_id for binding in resolution.bindings
                ),
                available_source_revision_ids=tuple(
                    binding.dataset_revision_id for binding in resolution.bindings
                ),
                question_type="abstention",
                difficulty="medium",
                synthetic=False,
            )

    provider = GoldProvider()
    joined = RealEvaluationAdapter().join_gold(finalized, gold=provider)

    assert provider.calls == [case.case_id]
    assert joined.case_id == case.case_id
    assert joined.expected_outcome == "abstained"

    provider.calls.clear()
    with pytest.raises(RealEvaluationContractError, match="finalized"):
        RealEvaluationAdapter().join_gold(observation, gold=provider)  # type: ignore[arg-type]
    assert provider.calls == []


def test_finalization_rejects_observation_provenance_mismatch_before_gold_join() -> None:
    case = load_r1_dataset().cases[1]
    observation = _safe_observation(case).model_copy(update={"case_id": "another:case"})

    with pytest.raises(ProductionContractObservationError, match="case identity"):
        FinalizedRealObservation.from_observation(
            observation=observation,
            resolution=_synthetic_resolution(),
        )


def test_finalization_rejects_an_observation_from_another_workspace() -> None:
    case = load_r1_dataset().cases[1]
    resolution = _synthetic_resolution()
    foreign_request = resolution.request.model_copy(
        update={"workspace_id": UUID("00000000-0000-0000-0000-000000009999")}
    )

    with pytest.raises(ProductionContractObservationError, match="scope"):
        FinalizedRealObservation.from_observation(
            observation=_safe_observation(case),
            resolution=resolution.model_copy(update={"request": foreign_request}),
        )


def _answered_observation_with_exact_pdf_locator():
    from spine.domain.capabilities import (
        AcceptedGroundedAnswer,
        AnswerClaim,
        AnswerClaimLocation,
        AnswerContent,
        AnswerDisclosureValidation,
        AnswerQuestionOutcome,
        ClaimEvidenceRelationship,
        ClaimEvidenceRelationshipKind,
        LineageReference,
    )
    from spine.domain.capabilities.answer_question import Citation
    from spine.domain.context_bundles import ContextBundle, EvidenceLocator
    from spine.memory.contracts import ContextReference, StableLocator

    observation = _allowed_observation()
    assert observation.context_bundle is not None
    old_reference = observation.context_bundle.evidence_references[0]
    new_reference = old_reference.model_copy(
        update={
            "locator_contract_version": "pdf-page-extracted-codepoint:v1",
            "locator": EvidenceLocator(kind="page", value="page:1:0-1"),
        }
    )
    bundle_values = observation.context_bundle.model_dump(
        mode="python", exclude={"bundle_digest"}
    )
    bundle_values["evidence_references"] = (new_reference,)
    bundle = ContextBundle.create(**bundle_values)

    old_projection_reference = observation.projection_retrieval.evidence[0]
    projection_reference = old_projection_reference.model_copy(
        update={
            "locator_contract_version": "pdf-page-extracted-codepoint:v1",
            "locator": StableLocator(kind="page", value="page:1:0-1"),
        }
    )
    projection = observation.projection_retrieval.model_copy(
        update={
            "evidence": (projection_reference,),
            "chunks": tuple(
                chunk.model_copy(update={"evidence": (projection_reference,)})
                for chunk in observation.projection_retrieval.chunks
            ),
        }
    )
    retrieval = observation.retrieval.model_copy(
        update={
            "references": (
                ContextReference(
                    source_id=str(new_reference.source_object_id),
                    source_revision_id=new_reference.source_revision_id,
                    workspace_id=new_reference.workspace_id,
                    environment_id=new_reference.environment_id,
                    evidence_id="evidence-1",
                    locator=new_reference.locator.model_dump(mode="json"),
                ),
            ),
        }
    )

    answer_id = UUID(int=901)
    claim_id = UUID(int=902)
    citation_id = UUID(int=903)
    text = "Synthetic supported claim."
    citation = Citation(
        citation_id=citation_id,
        answer_id=answer_id,
        context_bundle=bundle.lineage_reference,
        evidence_reference=new_reference,
    )
    claim = AnswerClaim(
        claim_id=claim_id,
        workspace_id=bundle.workspace_id,
        environment_id=bundle.environment_id,
        run_id=bundle.run_id,
        attempt_id=bundle.attempt_id,
        text=text,
        location=AnswerClaimLocation(
            answer_id=answer_id,
            start_offset=0,
            end_offset=len(text),
        ),
    )
    relationship = ClaimEvidenceRelationship(
        relationship_id=UUID(int=904),
        answer_id=answer_id,
        workspace_id=bundle.workspace_id,
        environment_id=bundle.environment_id,
        run_id=bundle.run_id,
        attempt_id=bundle.attempt_id,
        claim_id=claim_id,
        kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
        citation_ids=(citation_id,),
    )
    content = AnswerContent(
        answer_id=answer_id,
        workspace_id=bundle.workspace_id,
        environment_id=bundle.environment_id,
        run_id=bundle.run_id,
        attempt_id=bundle.attempt_id,
        text=text,
        claims=(claim,),
        citations=(citation,),
        relationships=(relationship,),
    )
    validation = AnswerDisclosureValidation(
        citation_validation_reference=UUID(int=905),
        semantic_support_validation_reference=UUID(int=906),
        evaluation_lineage_reference=UUID(int=907),
        disclosure_eligibility_reference=UUID(int=908),
        context_bundle_id=bundle.bundle_id,
        context_bundle_version=bundle.bundle_version,
        context_bundle_digest=bundle.bundle_digest,
        claim_ids=(claim_id,),
        citation_ids=(citation_id,),
        validation_fact_ids=(UUID(int=909),),
    )
    answer = observation.answer.model_copy(
        update={
            "outcome": AnswerQuestionOutcome.ANSWERED,
            "answer": content,
            "reason": None,
            "lineage": (
                LineageReference(
                    kind="context_bundle",
                    identifier=bundle.bundle_id,
                    schema_version="context-bundle:v1",
                ),
            ),
        }
    )
    disclosure = observation.disclosure.model_copy(
        update={
            "outcome": AnswerQuestionOutcome.ANSWERED,
            "validation": validation,
            "accepted_answer": AcceptedGroundedAnswer(
                answer=content,
                validation=validation,
                accepted_at=observation.disclosure.created_at,
            ),
            "safe_output": None,
        }
    )
    return observation.model_copy(
        update={
            "context_response": observation.context_response.model_copy(
                update={"context_bundle_ref": bundle.reference}
            ),
            "context_bundle": bundle,
            "projection_retrieval": projection,
            "retrieval": retrieval,
            "answer": answer,
            "disclosure": disclosure,
        }
    )


def _real_metric_scenario():
    observation = _answered_observation_with_exact_pdf_locator()
    binding = RealRuntimeRevisionBinding(
        manifest_sha256="b" * 64,
        dataset_source_id="source:opaque:pdf",
        dataset_revision_id="revision:opaque:pdf",
        upload_identity="approved.pdf",
        ingestion_intent_id="intent:pdf",
        owner_approval_ref="approval:test-fixture",
        workspace_id=observation.question.scope.workspace_id,
        environment_id=observation.question.scope.environment_id,
        source_object_id=UUID(int=20),
        source_revision_id=UUID(int=21),
        canonical_acceptance_id=UUID(int=910),
        original_sha256=SHA256,
    )
    from spine.evals.production_contract import ProductionContractCaseRequest

    resolution = RealRuntimeCaseResolution(
        request=ProductionContractCaseRequest(
            case_id=observation.case_id,
            question=observation.question.subject.text,
            language="en",
            workspace_id=observation.question.scope.workspace_id,
            environment_id=observation.question.scope.environment_id,
            principal_id=UUID(int=30),
            available_revision_ids=(UUID(int=21),),
            retrieval_strategy=observation.retrieval.retrieval_strategy,
        ),
        manifest_sha256="b" * 64,
        binding_sha256=runtime_revision_bindings_digest((binding,)),
        bindings=(binding,),
        authorization_generation=1,
    )
    finalized = FinalizedRealObservation.from_observation(
        observation=observation,
        resolution=resolution,
    )
    locator = PDFLocator(
        kind="pdf_page",
        schema_version="locator:pdf-page-char:v1",
        page=1,
        char_start=0,
        char_end=1,
    )
    evaluation_case = RealEvaluationCase(
        dataset_version="0.3.9-user-approved",
        dataset_sha256=REAL_R1_DATASET_DIGEST,
        case_id=observation.case_id,
        expected_outcome="answered",
        expected_answer="Synthetic supported claim.",
        expected_claims=(
            {
                "claim_id": "claim:gold:1",
                "text": "Synthetic supported claim.",
                "evidence_ids": ("evidence-1",),
            },
            *tuple(
                {
                    "claim_id": f"claim:gold:{position}",
                    "text": f"Synthetic absent claim {position}.",
                    "evidence_ids": (f"evidence:claim:{position}",),
                }
                for position in range(2, 159)
            ),
        ),
        provenance=(
            RealEvaluationEvidenceProvenance(
                evidence_id="evidence-1",
                dataset_source_id=binding.dataset_source_id,
                dataset_revision_id=binding.dataset_revision_id,
                canonical_source_object_id=binding.source_object_id,
                canonical_source_revision_id=binding.source_revision_id,
                locator=locator,
            ),
            *tuple(
                RealEvaluationEvidenceProvenance(
                    evidence_id=f"evidence:claim:{position}",
                    dataset_source_id=binding.dataset_source_id,
                    dataset_revision_id=binding.dataset_revision_id,
                    canonical_source_object_id=binding.source_object_id,
                    canonical_source_revision_id=binding.source_revision_id,
                    locator=locator.model_copy(
                        update={"char_start": position, "char_end": position + 1}
                    ),
                )
                for position in range(2, 159)
            ),
            *tuple(
                RealEvaluationEvidenceProvenance(
                    evidence_id=f"evidence:supplementary:{position}",
                    dataset_source_id=binding.dataset_source_id,
                    dataset_revision_id=binding.dataset_revision_id,
                    canonical_source_object_id=binding.source_object_id,
                    canonical_source_revision_id=binding.source_revision_id,
                    locator=locator.model_copy(
                        update={
                            "char_start": 200 + position,
                            "char_end": 201 + position,
                        }
                    ),
                )
                for position in range(1, 20)
            ),
        ),
    )

    result = evaluate_real_case(
        finalized,
        evaluation_case,
        runtime_identity=_runtime_identity(observation),
        recall_k=2,
    )

    return observation, finalized, evaluation_case, result


def test_real_metrics_count_all_citations_and_only_explicit_claim_evidence() -> None:
    _, _, evaluation_case, result = _real_metric_scenario()

    citation_recall = result.metric("qa.citation_recall")
    assert citation_recall.score == 1 / 177
    assert citation_recall.eligible_count == 177
    assert citation_recall.matched_count == 1
    groundedness = result.metric("qa.groundedness")
    assert groundedness.score == 1 / 158
    assert groundedness.eligible_count == 158
    assert groundedness.matched_count == 1
    assert evaluation_case.expected_claim_evidence_count == 158
    assert evaluation_case.expected_citation_count - evaluation_case.expected_claim_evidence_count == 19
    assert result.provenance == evaluation_case.provenance


def _partial_aggregate_scenario():
    _, _, _, result = _real_metric_scenario()
    citation_recall = result.metric("qa.citation_recall")

    recall_over_approved_set = citation_recall.model_copy(
        update={
            "score": 158 / 177,
            "eligible_count": 177,
            "matched_count": 158,
        }
    )
    answered = result.model_copy(
        update={
            "metrics": tuple(
                recall_over_approved_set
                if metric.evaluator_key == "qa.citation_recall"
                else metric
                for metric in result.metrics
            )
        }
    )
    abstention_metric = RealMetricResult(
        evaluator_key="qa.answer_outcome",
        evaluator_version="v1",
        status="measured",
        score=1.0,
        eligible_count=1,
        matched_count=1,
    )
    abstentions = tuple(
        RealCaseEvaluationResult(
            case_id=f"private-abstention-{position}",
            dataset_version=answered.dataset_version,
            dataset_sha256=answered.dataset_sha256,
            runtime_identity=answered.runtime_identity,
            expected_outcome="abstained",
            observed_outcome="abstained",
            observation_status="complete",
            metrics=(abstention_metric,),
        )
        for position in range(13)
    )
    unavailable = RealCaseEvaluationResult(
        case_id="private-unavailable",
        dataset_version=answered.dataset_version,
        dataset_sha256=answered.dataset_sha256,
        runtime_identity=answered.runtime_identity,
        expected_outcome="answered",
        observed_outcome="failed",
        observation_status="unavailable",
        evaluation_status="unavailable",
    )

    summary = aggregate_real_evaluations(
        (answered, *abstentions, unavailable),
        evaluation_failure_count=1,
    )
    return summary


def test_real_aggregate_keeps_quality_separate_from_unavailable_observations() -> None:
    summary = _partial_aggregate_scenario()

    summary_recall = summary.metric("qa.citation_recall")
    assert summary_recall.eligible_count == 177
    assert summary_recall.matched_count == 158
    assert summary_recall.score == 158 / 177
    abstention_slice = summary.outcome_slice("abstained")
    assert abstention_slice.status == "measured"
    assert abstention_slice.eligible_count == 13
    assert abstention_slice.score == 1.0
    denied_slice = summary.outcome_slice("denied")
    assert denied_slice.status == "not_applicable"
    assert denied_slice.eligible_count == 0
    assert denied_slice.score is None
    assert summary.unavailable_observation_count == 1
    assert summary.evaluation_failure_count == 1


def test_real_aggregate_represents_evaluator_failure_without_quality_scores() -> None:
    failed_summary = aggregate_real_evaluations(
        (),
        evaluation_failure_count=2,
        dataset_version="0.3.9-user-approved",
        dataset_sha256=REAL_R1_DATASET_DIGEST,
    )
    assert failed_summary.evaluation_status == "failed"
    assert failed_summary.case_count == 0
    assert all(
        item.status == "not_applicable" and item.eligible_count == 0
        for item in failed_summary.outcome_slices
    )


def test_real_aggregate_serialization_does_not_disclose_private_values() -> None:
    summary = _partial_aggregate_scenario()
    serialized = summary.model_dump_json()
    for protected_value in (
        "private-abstention",
        "private-unavailable",
        "case:opaque",
        "source:opaque",
        "revision:opaque",
        "evidence-1",
        "Synthetic supported claim",
    ):
        assert protected_value not in serialized


def test_real_case_rejects_missing_expected_evidence_reference() -> None:
    with pytest.raises(ValueError, match="outside the expected citation set"):
        RealEvaluationCase(
            dataset_version="0.3.9-user-approved",
            dataset_sha256=REAL_R1_DATASET_DIGEST,
            case_id="private-missing-evidence",
            expected_outcome="answered",
            expected_answer="Synthetic answer.",
            expected_claims=(
                {
                    "claim_id": "claim:missing",
                    "text": "Synthetic answer.",
                    "evidence_ids": ("evidence:missing",),
                },
            ),
            provenance=(),
        )


def test_finalization_rejects_inaccessible_out_of_scope_reference() -> None:
    observation, finalized, _, _ = _real_metric_scenario()
    reference = observation.retrieval.references[0].model_copy(
        update={"workspace_id": UUID("00000000-0000-0000-0000-000000009999")}
    )
    inaccessible = observation.model_copy(
        update={
            "retrieval": observation.retrieval.model_copy(
                update={"references": (reference,)}
            )
        }
    )

    with pytest.raises(ProductionContractObservationError):
        FinalizedRealObservation.from_observation(
            observation=inaccessible,
            resolution=finalized.resolution,
        )


def test_real_evaluation_marks_stale_locator_as_failed_evidence() -> None:
    observation, finalized, evaluation_case, _ = _real_metric_scenario()
    stale = evaluation_case.model_copy(
        update={
            "provenance": (
                evaluation_case.provenance[0].model_copy(
                    update={
                        "locator": evaluation_case.provenance[0].locator.model_copy(
                            update={"page": 2}
                        )
                    }
                ),
                *evaluation_case.provenance[1:],
            )
        }
    )

    result = evaluate_real_case(
        finalized,
        stale,
        runtime_identity=_runtime_identity(observation),
    )

    assert result.metric("qa.locator_validity").failure_counts == {
        "locator_mismatch": 1,
        "unsupported_claim": 158,
    }


def test_real_evaluation_rejects_fabricated_canonical_provenance() -> None:
    observation, finalized, evaluation_case, _ = _real_metric_scenario()

    mismatched = evaluation_case.model_copy(
        update={
            "provenance": (
                evaluation_case.provenance[0].model_copy(
                    update={"canonical_source_revision_id": UUID(int=9999)}
                ),
                *evaluation_case.provenance[1:],
            )
        }
    )
    with pytest.raises(RealEvaluationContractError, match="verified bindings") as error:
        evaluate_real_case(
            finalized,
            mismatched,
            runtime_identity=_runtime_identity(observation),
        )
    assert "evidence-1" not in str(error.value)


def test_count_shaped_results_cannot_impersonate_the_approved_population() -> None:
    observation = _answered_observation_with_exact_pdf_locator()
    runtime_identity = _runtime_identity(observation)
    outcome_metric = RealMetricResult(
        evaluator_key="qa.answer_outcome",
        evaluator_version="v1",
        status="measured",
        score=1.0,
        eligible_count=1,
        matched_count=1,
    )
    answered = tuple(
        RealCaseEvaluationResult(
            case_id=f"private-answered-{position}",
            dataset_version="0.3.9-user-approved",
            dataset_sha256=REAL_R1_DATASET_DIGEST,
            runtime_identity=runtime_identity,
            expected_outcome="answered",
            observed_outcome="answered",
            expected_citation_count=177 if position == 0 else 0,
            expected_claim_evidence_count=158 if position == 0 else 0,
            observation_status="complete",
            metrics=(outcome_metric,),
        )
        for position in range(136)
    )
    locator = PDFLocator(
        kind="pdf_page",
        schema_version="locator:pdf-page-char:v1",
        page=1,
        char_start=0,
        char_end=1,
    )
    provenance = tuple(
        RealEvaluationEvidenceProvenance(
            evidence_id=f"approved-evidence-{position}",
            dataset_source_id="approved-source",
            dataset_revision_id="approved-revision",
            canonical_source_object_id=UUID(int=20),
            canonical_source_revision_id=UUID(int=21),
            locator=locator.model_copy(
                update={"char_start": position, "char_end": position + 1}
            ),
        )
        for position in range(177)
    )
    claim_evidence = tuple(
        (f"approved-claim-{position}", f"approved-evidence-{position}")
        for position in range(158)
    )
    answered = (
        answered[0].model_copy(
            update={
                "provenance": provenance,
                "expected_claim_evidence": claim_evidence,
            }
        ),
        *answered[1:],
    )
    abstained = tuple(
        RealCaseEvaluationResult(
            case_id=f"private-abstained-{position}",
            dataset_version="0.3.9-user-approved",
            dataset_sha256=REAL_R1_DATASET_DIGEST,
            runtime_identity=runtime_identity,
            expected_outcome="abstained",
            observed_outcome="abstained",
            observation_status="complete",
            metrics=(outcome_metric,),
        )
        for position in range(13)
    )

    count_shaped = aggregate_real_evaluations((*answered, *abstained))
    incomplete = aggregate_real_evaluations((*answered[:-1], *abstained))
    population = RealEvaluationPopulation(
        dataset_version="0.3.9-user-approved",
        dataset_sha256=REAL_R1_DATASET_DIGEST,
        case_outcomes={
            result.case_id: result.expected_outcome
            for result in (*answered, *abstained)
        },
        citation_keys=tuple(
            (answered[0].case_id, item.evidence_id) for item in provenance
        ),
        claim_evidence_keys=tuple(
            (answered[0].case_id, claim_id, evidence_id)
            for claim_id, evidence_id in claim_evidence
        ),
    )
    identity_complete = aggregate_real_evaluations(
        (*answered, *abstained), population=population
    )

    assert count_shaped.evaluation_status == "partial"
    assert count_shaped.case_count == 149
    assert count_shaped.outcome_slice("denied").status == "not_applicable"
    assert incomplete.evaluation_status == "partial"
    assert identity_complete.evaluation_status == "completed"


@pytest.mark.parametrize(
    ("outcome", "reason_code", "observation_status"),
    (
        ("abstained", "no_usable_evidence", "complete"),
        ("denied", "authorization_denied", "complete"),
        ("clarification_required", "ambiguous_question", "complete"),
        ("failed", "mandatory_processing_failed", "unavailable"),
        ("cancelled", "cancelled", "unavailable"),
    ),
)
def test_real_evaluator_preserves_all_non_answer_terminal_outcomes(
    outcome: str,
    reason_code: str,
    observation_status: str,
) -> None:
    from spine.domain.capabilities import (
        AnswerQuestionOutcome,
        AnswerQuestionSafeReason,
        AnswerQuestionSafeReasonCode,
        ClarificationRequest,
    )

    case = load_r1_dataset().cases[1]
    observation = _safe_observation(case)
    outcome_value = AnswerQuestionOutcome(outcome)
    reason = AnswerQuestionSafeReason(code=AnswerQuestionSafeReasonCode(reason_code))
    answer_updates: dict[str, object] = {
        "outcome": outcome_value,
        "reason": reason,
    }
    if outcome == "clarification_required":
        answer_updates["clarification"] = ClarificationRequest(
            prompt="Please clarify the synthetic question."
        )
    answer = observation.answer.model_copy(update=answer_updates)
    observation = observation.model_copy(
        update={
            "answer": answer,
            "disclosure": observation.disclosure.model_copy(
                update={"outcome": outcome_value, "safe_output": answer}
            ),
        }
    )
    finalized = FinalizedRealObservation.from_observation(
        observation=observation,
        resolution=_synthetic_resolution(),
    )
    evaluation_case = RealEvaluationCase(
        dataset_version="0.3.9-user-approved",
        dataset_sha256=REAL_R1_DATASET_DIGEST,
        case_id=case.case_id,
        expected_outcome="abstained",
    )

    result = evaluate_real_case(
        finalized,
        evaluation_case,
        runtime_identity=_runtime_identity(observation),
    )

    assert result.observed_outcome == outcome
    assert result.observation_status == observation_status
    if observation_status == "unavailable":
        assert result.evaluation_status == "unavailable"
        assert result.metrics == ()
    else:
        expected_score = 1.0 if outcome == "abstained" else 0.0
        assert result.metric("qa.answer_outcome").score == expected_score
