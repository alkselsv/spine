from datetime import datetime, timezone
from uuid import UUID

from spine.application.qa_completeness import completeness_presentation
from spine.domain.capabilities import (
    AnswerCompletenessPresentation,
    AnswerQuestionOutcome,
    AnswerQuestionOutput,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    RunAttemptCorrelation,
)
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecordV3,
    AnswerExecutionCheckpoint,
    parse_answer_disclosure_record,
)
from spine.domain.capabilities.answer_evaluation import (
    AnswerDisclosureStatus,
    AnswerEvaluationDecision,
    AnswerEvaluationSafeReason,
    AnswerPartialityStatus,
    AttemptProjectionBoundary,
    CoverageEvaluationStatus,
    CoverageMemberStatus,
    ClaimAuthorizationStatus,
    CanonicalViewMember,
    ClaimEvidenceValidation,
    ClaimGroundingStatus,
    GroundedConflictStatus,
    GroundedClaimAssessment,
    MissingCoverageImpact,
    ObservedCoverage,
    ObservedCoverageMember,
    TargetCompletenessScope,
    TargetScopeMember,
    PartialClaimUtility,
)


def _record() -> AnswerDisclosureRecordV3:
    workspace_id = UUID(int=1)
    environment_id = UUID(int=2)
    run_id = UUID(int=3)
    attempt_id = UUID(int=4)
    target_scope_id = UUID(int=5)
    coverage_id = UUID(int=6)
    source_id = UUID(int=7)
    revision_id = UUID(int=8)
    boundary = AttemptProjectionBoundary(
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=run_id,
        attempt_id=attempt_id,
        projection_snapshot_id=UUID(int=9),
        activation_generation=2,
        canonical_boundary_id=UUID(int=10),
    )
    target = TargetCompletenessScope(
        target_scope_id=target_scope_id,
        boundary=boundary,
        derivation_reference=UUID(int=11),
        canonical_view_reference=UUID(int=23),
        canonical_view_digest="d" * 64,
        canonical_view_generation=1,
        canonical_view_members=(
            CanonicalViewMember(
                source_object_id=source_id,
                source_revision_id=revision_id,
                revision_kind="content",
                accepted_generation=1,
                cas_version=1,
            ),
        ),
        source_constraint_digest="a" * 64,
        members=(
            TargetScopeMember(
                source_object_id=source_id,
                source_revision_id=revision_id,
                authorization_decision_reference=UUID(int=12),
            ),
        ),
    )
    coverage = ObservedCoverage(
        observed_coverage_id=coverage_id,
        target_scope_id=target_scope_id,
        boundary=boundary,
        proof_reference=UUID(int=13),
        members=(
            ObservedCoverageMember(
                source_object_id=source_id,
                source_revision_id=revision_id,
                status=CoverageMemberStatus.UNKNOWN,
            ),
        ),
    )
    evaluation = AnswerEvaluationDecision(
        boundary=boundary,
        target_scope_id=target_scope_id,
        observed_coverage_id=coverage_id,
        evaluation_lineage_reference=UUID(int=14),
        disclosure_status=AnswerDisclosureStatus.REJECTED,
        coverage_status=CoverageEvaluationStatus.UNPROVEN,
        partiality_status=AnswerPartialityStatus.NONE,
        conflict_status=GroundedConflictStatus.NONE,
        safe_reason=AnswerEvaluationSafeReason.COVERAGE_NOT_PROVEN,
    )
    claim_assessment = GroundedClaimAssessment(
        boundary=boundary,
        claim_id=UUID(int=17),
        grounding_status=ClaimGroundingStatus.GROUNDED,
        authorization_status=ClaimAuthorizationStatus.CURRENT,
        partial_utility=PartialClaimUtility.INDEPENDENTLY_USEFUL,
        missing_coverage_impact=MissingCoverageImpact.IMMATERIAL,
        evidence_validations=(
            ClaimEvidenceValidation(
                citation_id=UUID(int=18),
                citation_validation_reference=UUID(int=19),
                semantic_support_validation_reference=UUID(int=20),
                authorization_validation_reference=UUID(int=21),
            ),
        ),
        partial_validation_reference=UUID(int=22),
    )
    output = AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=run_id,
            attempt_id=attempt_id,
            trace_id=UUID(int=15),
        ),
        scope=AnswerQuestionScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        outcome=AnswerQuestionOutcome.ABSTAINED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.INCOMPLETE_AUTHORIZED_COVERAGE
        ),
    )
    return AnswerDisclosureRecordV3(
        request_id=UUID(int=16),
        request_digest="b" * 64,
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=run_id,
        attempt_id=attempt_id,
        outcome=AnswerQuestionOutcome.ABSTAINED,
        safe_output=output,
        terminal_output=output,
        created_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
        execution_checkpoint=AnswerExecutionCheckpoint(
            execution_pin_digest="c" * 64
        ),
        target_scope=target,
        observed_coverage=coverage,
        claim_assessments=(claim_assessment,),
        evaluation=evaluation,
    )


def test_v3_disclosure_json_round_trip_preserves_evaluation_lineage() -> None:
    record = _record()

    restored = parse_answer_disclosure_record(record.model_dump(mode="json"))

    assert isinstance(restored, AnswerDisclosureRecordV3)
    assert restored == record
    assert restored.target_scope.boundary.attempt_id == record.attempt_id
    assert restored.observed_coverage.boundary.projection_snapshot_id == UUID(int=9)


def test_completeness_projection_exposes_only_safe_codes_and_opaque_ids() -> None:
    presentation = completeness_presentation(_record())

    assert isinstance(presentation, AnswerCompletenessPresentation)
    payload = presentation.model_dump(mode="json")
    assert payload["coverage_status"] == "unproven"
    assert payload["serving_decision"] == "withhold"
    assert "members" not in payload
    assert "count" not in " ".join(payload)
    assert "source" not in " ".join(payload)


def test_current_denial_hides_protected_completeness_lineage() -> None:
    record = _record()
    denied_output = record.safe_output.model_copy(
        update={
            "outcome": AnswerQuestionOutcome.DENIED,
            "reason": AnswerQuestionSafeReason(
                code=AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED
            ),
        }
    )
    accepted_evaluation = record.evaluation.model_copy(
        update={
            "disclosure_status": AnswerDisclosureStatus.ACCEPTED,
            "coverage_status": CoverageEvaluationStatus.COMPLETE,
            "partiality_status": AnswerPartialityStatus.EXHAUSTIVE,
            "safe_reason": AnswerEvaluationSafeReason.COMPLETE,
            "disclosable_claim_ids": (UUID(int=17),),
        }
    )
    represented_coverage = record.observed_coverage.model_copy(
        update={
            "members": tuple(
                member.model_copy(update={"status": CoverageMemberStatus.REPRESENTED})
                for member in record.observed_coverage.members
            )
        }
    )
    denied = AnswerDisclosureRecordV3.model_validate(
        {
            **record.model_dump(mode="python"),
            "outcome": AnswerQuestionOutcome.DENIED,
            "safe_output": denied_output,
            "terminal_output": denied_output,
            "observed_coverage": represented_coverage,
            "evaluation": accepted_evaluation,
        }
    )

    presentation = completeness_presentation(denied)

    assert presentation is None
