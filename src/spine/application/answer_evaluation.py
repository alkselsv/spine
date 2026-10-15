"""Deterministic disclosure evaluation for one Q&A answer candidate."""

from __future__ import annotations

from spine.domain.capabilities.answer_evaluation import (
    ApprovedPrecedenceRule,
    AnswerEvaluationDecision,
    AnswerEvaluationSafeReason,
    AnswerEvaluationStatus,
    AttemptProjectionBoundary,
    CoverageEvaluationStatus,
    CoverageMemberStatus,
    DeterministicDerivation,
    GroundedConflict,
    GroundedClaimAssessment,
    ObservedCoverage,
    PartialFallbackAuthorization,
    TargetCompletenessScope,
)
from spine.domain.capabilities.answer_question import (
    AnswerClaimKind,
    AnswerContent,
    ClaimEvidenceRelationshipKind,
)


def evaluate_answer_candidate(
    *,
    answer: AnswerContent,
    target_scope: TargetCompletenessScope,
    observed_coverage: ObservedCoverage,
    claim_assessments: tuple[GroundedClaimAssessment, ...],
    partial_fallback: PartialFallbackAuthorization | None = None,
    conflicts: tuple[GroundedConflict, ...] = (),
    precedence_rules: tuple[ApprovedPrecedenceRule, ...] = (),
    derivations: tuple[DeterministicDerivation, ...] = (),
) -> AnswerEvaluationDecision:
    """Evaluate facts supplied by authoritative stages without doing their I/O."""

    claim_ids = tuple(claim.claim_id for claim in answer.claims)
    assessments = {assessment.claim_id: assessment for assessment in claim_assessments}
    claims_are_grounded = (
        _answer_matches_boundary(answer, target_scope.boundary)
        and len(assessments) == len(claim_assessments)
        and set(assessments) == set(claim_ids)
        and all(
            assessment.grounded
            and assessment.current_authorized
            and bool(assessment.validation_references)
            for assessment in assessments.values()
        )
    )
    derivations_are_valid = _derivations_are_valid(
        answer=answer,
        boundary=target_scope.boundary,
        derivations=derivations,
    )
    coverage_status = _coverage_status(target_scope, observed_coverage)
    if not claims_are_grounded or not derivations_are_valid:
        return AnswerEvaluationDecision(
            answer_status=AnswerEvaluationStatus.REJECTED,
            coverage_status=coverage_status,
            safe_reason=AnswerEvaluationSafeReason.CLAIM_REJECTED,
        )
    conflict_by_id = {conflict.conflict_id: conflict for conflict in conflicts}
    rules_by_conflict = {rule.conflict_id: rule for rule in precedence_rules}
    conflicts_are_valid = (
        len(conflict_by_id) == len(conflicts)
        and len(rules_by_conflict) == len(precedence_rules)
        and all(
            conflict.boundary == target_scope.boundary
            and set(conflict.claim_ids).issubset(claim_ids)
            for conflict in conflicts
        )
        and all(
            rule.boundary == target_scope.boundary
            and rule.conflict_id in conflict_by_id
            and rule.winning_claim_id in conflict_by_id[rule.conflict_id].claim_ids
            for rule in precedence_rules
        )
        and (not precedence_rules or set(rules_by_conflict) == set(conflict_by_id))
    )
    if not conflicts_are_valid:
        return AnswerEvaluationDecision(
            answer_status=AnswerEvaluationStatus.REJECTED,
            coverage_status=coverage_status,
            safe_reason=AnswerEvaluationSafeReason.CLAIM_REJECTED,
        )
    selected_claim_ids = claim_ids
    if precedence_rules:
        conflict_claim_ids = {
            claim_id for conflict in conflicts for claim_id in conflict.claim_ids
        }
        winning_claim_ids = {rule.winning_claim_id for rule in precedence_rules}
        selected_claim_ids = tuple(
            claim_id
            for claim_id in claim_ids
            if claim_id not in conflict_claim_ids or claim_id in winning_claim_ids
        )
    if coverage_status is not CoverageEvaluationStatus.COMPLETE:
        partial_allowed = (
            coverage_status is CoverageEvaluationStatus.INSUFFICIENT
            and partial_fallback is not None
            and partial_fallback.boundary == target_scope.boundary
            and all(
                assessment.independently_useful
                and assessment.stable_under_incomplete_coverage
                for assessment in assessments.values()
            )
        )
        if partial_allowed:
            return AnswerEvaluationDecision(
                answer_status=(
                    AnswerEvaluationStatus.GROUNDED_CONFLICT
                    if conflicts and not precedence_rules
                    else AnswerEvaluationStatus.PARTIAL
                ),
                coverage_status=coverage_status,
                safe_reason=(
                    AnswerEvaluationSafeReason.GROUNDED_CONFLICT
                    if conflicts and not precedence_rules
                    else AnswerEvaluationSafeReason.PARTIAL_AUTHORIZED
                ),
                disclosable_claim_ids=selected_claim_ids,
                partial_fallback_applied=True,
            )
        return AnswerEvaluationDecision(
            answer_status=AnswerEvaluationStatus.REJECTED,
            coverage_status=coverage_status,
            safe_reason=(
                AnswerEvaluationSafeReason.COVERAGE_NOT_PROVEN
                if coverage_status is CoverageEvaluationStatus.UNPROVEN
                else AnswerEvaluationSafeReason.INCOMPLETE_AUTHORIZED_COVERAGE
            ),
        )
    if conflicts and not precedence_rules:
        return AnswerEvaluationDecision(
            answer_status=AnswerEvaluationStatus.GROUNDED_CONFLICT,
            coverage_status=CoverageEvaluationStatus.COMPLETE,
            safe_reason=AnswerEvaluationSafeReason.GROUNDED_CONFLICT,
            disclosable_claim_ids=claim_ids,
        )
    if precedence_rules:
        return AnswerEvaluationDecision(
            answer_status=AnswerEvaluationStatus.COMPLETE,
            coverage_status=CoverageEvaluationStatus.COMPLETE,
            safe_reason=AnswerEvaluationSafeReason.PRECEDENCE_APPLIED,
            disclosable_claim_ids=selected_claim_ids,
        )
    return AnswerEvaluationDecision(
        answer_status=AnswerEvaluationStatus.COMPLETE,
        coverage_status=CoverageEvaluationStatus.COMPLETE,
        safe_reason=AnswerEvaluationSafeReason.COMPLETE,
        disclosable_claim_ids=claim_ids,
    )


def _derivations_are_valid(
    *,
    answer: AnswerContent,
    boundary: AttemptProjectionBoundary,
    derivations: tuple[DeterministicDerivation, ...],
) -> bool:
    derived_claim_ids = {
        claim.claim_id for claim in answer.claims if claim.kind is AnswerClaimKind.DERIVED
    }
    by_claim = {derivation.claim_id: derivation for derivation in derivations}
    if len(by_claim) != len(derivations) or set(by_claim) != derived_claim_ids:
        return False
    for claim_id, derivation in by_claim.items():
        expected_inputs = {
            citation_id
            for relationship in answer.relationships
            if relationship.claim_id == claim_id
            and relationship.kind is ClaimEvidenceRelationshipKind.DERIVATION_INPUT
            for citation_id in relationship.citation_ids
        }
        actual_inputs = {item.citation_id for item in derivation.inputs}
        if derivation.boundary != boundary or actual_inputs != expected_inputs:
            return False
    return True


def _answer_matches_boundary(
    answer: AnswerContent,
    boundary: AttemptProjectionBoundary,
) -> bool:
    if (
        answer.workspace_id,
        answer.environment_id,
        answer.run_id,
        answer.attempt_id,
    ) != (
        boundary.workspace_id,
        boundary.environment_id,
        boundary.run_id,
        boundary.attempt_id,
    ):
        return False
    return all(
        citation.evidence_reference.projection_snapshot_id
        == boundary.projection_snapshot_id
        and citation.evidence_reference.activation_generation
        == boundary.activation_generation
        for citation in answer.citations
    )


def _coverage_status(
    target_scope: TargetCompletenessScope,
    observed_coverage: ObservedCoverage,
) -> CoverageEvaluationStatus:
    if (
        observed_coverage.target_scope_id != target_scope.target_scope_id
        or observed_coverage.boundary != target_scope.boundary
    ):
        return CoverageEvaluationStatus.UNPROVEN
    target_members = {
        (member.source_object_id, member.source_revision_id)
        for member in target_scope.members
    }
    observed_members = {
        (member.source_object_id, member.source_revision_id): member
        for member in observed_coverage.members
    }
    if set(observed_members) != target_members or any(
        member.status is not CoverageMemberStatus.REPRESENTED
        for member in observed_members.values()
    ):
        return CoverageEvaluationStatus.INSUFFICIENT
    if observed_coverage.proof_reference is None or any(
        member.proof_reference is None for member in observed_members.values()
    ):
        return CoverageEvaluationStatus.UNPROVEN
    return CoverageEvaluationStatus.COMPLETE


__all__ = ["evaluate_answer_candidate"]
