"""Pure deterministic disclosure evaluation for one Q&A answer candidate."""

from __future__ import annotations

from itertools import combinations
from typing import Literal
from uuid import UUID

from spine.domain.common import ValidatedDefinitionModel
from spine.domain.capabilities.answer_evaluation import (
    ApprovedPrecedenceRule,
    AnswerDisclosureStatus,
    AnswerEvaluationDecision,
    AnswerEvaluationSafeReason,
    AnswerPartialityStatus,
    AttemptProjectionBoundary,
    ClaimAuthorizationStatus,
    ClaimGroundingStatus,
    CoverageEvaluationStatus,
    CoverageMemberStatus,
    DeterministicDerivation,
    GroundedConflict,
    GroundedConflictStatus,
    GroundedClaimAssessment,
    MissingCoverageImpact,
    ObservedCoverage,
    PartialFallbackAuthorization,
    PartialClaimUtility,
    TargetCompletenessScope,
)
from spine.domain.capabilities.answer_question import (
    AnswerClaimKind,
    AnswerContent,
    ClaimEvidenceRelationshipKind,
)


class AnswerEvaluationRequest(ValidatedDefinitionModel):
    """All authoritative immutable facts consumed by one evaluation."""

    schema_version: Literal["answer-evaluation-request:v1"] = (
        "answer-evaluation-request:v1"
    )
    answer: AnswerContent
    target_scope: TargetCompletenessScope
    observed_coverage: ObservedCoverage
    claim_assessments: tuple[GroundedClaimAssessment, ...]
    partial_fallback: PartialFallbackAuthorization | None = None
    conflicts: tuple[GroundedConflict, ...] = ()
    precedence_rules: tuple[ApprovedPrecedenceRule, ...] = ()
    derivations: tuple[DeterministicDerivation, ...] = ()
    evaluation_lineage_reference: UUID


def evaluate_answer_candidate(
    request: AnswerEvaluationRequest,
) -> AnswerEvaluationDecision:
    """Evaluate facts supplied by authoritative stages without doing their I/O."""

    answer = request.answer
    target_scope = request.target_scope
    observed_coverage = request.observed_coverage
    claim_assessments = request.claim_assessments
    partial_fallback = request.partial_fallback
    conflicts = request.conflicts
    precedence_rules = request.precedence_rules
    derivations = request.derivations
    evaluation_lineage_reference = request.evaluation_lineage_reference
    claim_ids = tuple(claim.claim_id for claim in answer.claims)
    assessments = {assessment.claim_id: assessment for assessment in claim_assessments}
    claims_are_grounded = (
        _answer_matches_boundary(answer, target_scope.boundary)
        and len(assessments) == len(claim_assessments)
        and set(assessments) == set(claim_ids)
        and all(
            _claim_assessment_is_valid(answer, target_scope.boundary, assessment)
            for assessment in claim_assessments
        )
    )
    derivations_are_valid = _derivations_are_valid(
        answer=answer,
        boundary=target_scope.boundary,
        derivations=derivations,
        assessments=assessments,
    )
    coverage_status = _coverage_status(target_scope, observed_coverage)
    if not claims_are_grounded or not derivations_are_valid:
        return _decision(
            boundary=target_scope.boundary,
            target_scope=target_scope,
            observed_coverage=observed_coverage,
            evaluation_lineage_reference=evaluation_lineage_reference,
            disclosure_status=AnswerDisclosureStatus.REJECTED,
            coverage_status=coverage_status,
            safe_reason=AnswerEvaluationSafeReason.CLAIM_REJECTED,
        )
    conflict_by_id = {conflict.conflict_id: conflict for conflict in conflicts}
    rules_by_conflict = {rule.conflict_id: rule for rule in precedence_rules}
    contradiction_pairs = {
        frozenset((relationship.claim_id, relationship.related_claim_id))
        for relationship in answer.relationships
        if relationship.kind is ClaimEvidenceRelationshipKind.CONTRADICTION
        and relationship.related_claim_id is not None
    }
    detected_pairs = {
        frozenset(pair)
        for conflict in conflicts
        for pair in combinations(conflict.claim_ids, 2)
    }
    precedence_winners = {rule.winning_claim_id for rule in precedence_rules}
    precedence_losers = {
        claim_id
        for rule in precedence_rules
        for conflict in (conflict_by_id.get(rule.conflict_id),)
        if conflict is not None
        for claim_id in conflict.claim_ids
        if claim_id != rule.winning_claim_id
    }
    conflicts_are_valid = (
        len(conflict_by_id) == len(conflicts)
        and len(rules_by_conflict) == len(precedence_rules)
        and len(detected_pairs) == len(conflicts)
        and detected_pairs == contradiction_pairs
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
        and not precedence_winners.intersection(precedence_losers)
    )
    if not conflicts_are_valid:
        return _decision(
            boundary=target_scope.boundary,
            target_scope=target_scope,
            observed_coverage=observed_coverage,
            evaluation_lineage_reference=evaluation_lineage_reference,
            disclosure_status=AnswerDisclosureStatus.REJECTED,
            coverage_status=coverage_status,
            safe_reason=AnswerEvaluationSafeReason.CLAIM_REJECTED,
        )
    selected_claim_ids = claim_ids
    if precedence_rules:
        selected_claim_ids = tuple(
            claim_id
            for claim_id in claim_ids
            if claim_id not in precedence_losers
        )
    if coverage_status is not CoverageEvaluationStatus.COMPLETE:
        partial_allowed = (
            coverage_status is CoverageEvaluationStatus.INSUFFICIENT
            and partial_fallback is not None
            and partial_fallback.boundary == target_scope.boundary
            and all(
                assessment.partial_utility
                is PartialClaimUtility.INDEPENDENTLY_USEFUL
                and assessment.missing_coverage_impact
                is MissingCoverageImpact.IMMATERIAL
                for assessment in assessments.values()
            )
        )
        if partial_allowed:
            return _decision(
                boundary=target_scope.boundary,
                target_scope=target_scope,
                observed_coverage=observed_coverage,
                evaluation_lineage_reference=evaluation_lineage_reference,
                disclosure_status=AnswerDisclosureStatus.ACCEPTED,
                coverage_status=coverage_status,
                partiality_status=AnswerPartialityStatus.PARTIAL,
                conflict_status=(
                    GroundedConflictStatus.PRESENTED
                    if conflicts and not precedence_rules
                    else GroundedConflictStatus.RESOLVED_BY_PRECEDENCE
                    if precedence_rules
                    else GroundedConflictStatus.NONE
                ),
                safe_reason=(
                    AnswerEvaluationSafeReason.GROUNDED_CONFLICT
                    if conflicts and not precedence_rules
                    else AnswerEvaluationSafeReason.PARTIAL_AUTHORIZED
                ),
                disclosable_claim_ids=selected_claim_ids,
            )
        return _decision(
            boundary=target_scope.boundary,
            target_scope=target_scope,
            observed_coverage=observed_coverage,
            evaluation_lineage_reference=evaluation_lineage_reference,
            disclosure_status=AnswerDisclosureStatus.REJECTED,
            coverage_status=coverage_status,
            safe_reason=(
                AnswerEvaluationSafeReason.COVERAGE_NOT_PROVEN
                if coverage_status is CoverageEvaluationStatus.UNPROVEN
                else AnswerEvaluationSafeReason.INCOMPLETE_AUTHORIZED_COVERAGE
            ),
        )
    if conflicts and not precedence_rules:
        return _decision(
            boundary=target_scope.boundary,
            target_scope=target_scope,
            observed_coverage=observed_coverage,
            evaluation_lineage_reference=evaluation_lineage_reference,
            disclosure_status=AnswerDisclosureStatus.ACCEPTED,
            coverage_status=CoverageEvaluationStatus.COMPLETE,
            partiality_status=AnswerPartialityStatus.EXHAUSTIVE,
            conflict_status=GroundedConflictStatus.PRESENTED,
            safe_reason=AnswerEvaluationSafeReason.GROUNDED_CONFLICT,
            disclosable_claim_ids=claim_ids,
        )
    if precedence_rules:
        return _decision(
            boundary=target_scope.boundary,
            target_scope=target_scope,
            observed_coverage=observed_coverage,
            evaluation_lineage_reference=evaluation_lineage_reference,
            disclosure_status=AnswerDisclosureStatus.ACCEPTED,
            coverage_status=CoverageEvaluationStatus.COMPLETE,
            partiality_status=AnswerPartialityStatus.EXHAUSTIVE,
            conflict_status=GroundedConflictStatus.RESOLVED_BY_PRECEDENCE,
            safe_reason=AnswerEvaluationSafeReason.PRECEDENCE_APPLIED,
            disclosable_claim_ids=selected_claim_ids,
        )
    return _decision(
        boundary=target_scope.boundary,
        target_scope=target_scope,
        observed_coverage=observed_coverage,
        evaluation_lineage_reference=evaluation_lineage_reference,
        disclosure_status=AnswerDisclosureStatus.ACCEPTED,
        coverage_status=CoverageEvaluationStatus.COMPLETE,
        partiality_status=AnswerPartialityStatus.EXHAUSTIVE,
        conflict_status=GroundedConflictStatus.NONE,
        safe_reason=AnswerEvaluationSafeReason.COMPLETE,
        disclosable_claim_ids=claim_ids,
    )


def _derivations_are_valid(
    *,
    answer: AnswerContent,
    boundary: AttemptProjectionBoundary,
    derivations: tuple[DeterministicDerivation, ...],
    assessments: dict[UUID, GroundedClaimAssessment],
) -> bool:
    derived_claim_ids = {
        claim.claim_id for claim in answer.claims if claim.kind is AnswerClaimKind.DERIVED
    }
    by_claim = {derivation.claim_id: derivation for derivation in derivations}
    if len(by_claim) != len(derivations) or set(by_claim) != derived_claim_ids:
        return False
    for claim_id, derivation in by_claim.items():
        assessment = assessments.get(claim_id)
        if assessment is None:
            return False
        expected_inputs = {
            citation_id
            for relationship in answer.relationships
            if relationship.claim_id == claim_id
            and relationship.kind is ClaimEvidenceRelationshipKind.DERIVATION_INPUT
            for citation_id in relationship.citation_ids
        }
        actual_inputs = {item.citation_id for item in derivation.inputs}
        validation_by_citation = {
            item.citation_id: item
            for item in assessment.evidence_validations
        }
        if (
            derivation.boundary != boundary
            or actual_inputs != expected_inputs
            or any(
                validation_by_citation.get(item.citation_id) is None
                or validation_by_citation[
                    item.citation_id
                ].semantic_support_validation_reference
                != item.grounding_validation_reference
                for item in derivation.inputs
            )
        ):
            return False
    return True


def _claim_assessment_is_valid(
    answer: AnswerContent,
    boundary: AttemptProjectionBoundary,
    assessment: GroundedClaimAssessment,
) -> bool:
    expected_citation_ids = {
        citation_id
        for relationship in answer.relationships
        if relationship.claim_id == assessment.claim_id
        and relationship.kind is not ClaimEvidenceRelationshipKind.CONTRADICTION
        for citation_id in relationship.citation_ids
    }
    actual_citation_ids = {
        validation.citation_id for validation in assessment.evidence_validations
    }
    return bool(
        assessment.boundary == boundary
        and assessment.grounding_status is ClaimGroundingStatus.GROUNDED
        and assessment.authorization_status is ClaimAuthorizationStatus.CURRENT
        and actual_citation_ids == expected_citation_ids
    )


def _decision(
    *,
    boundary: AttemptProjectionBoundary,
    target_scope: TargetCompletenessScope,
    observed_coverage: ObservedCoverage,
    evaluation_lineage_reference: UUID,
    disclosure_status: AnswerDisclosureStatus,
    coverage_status: CoverageEvaluationStatus,
    safe_reason: AnswerEvaluationSafeReason,
    partiality_status: AnswerPartialityStatus = AnswerPartialityStatus.NONE,
    conflict_status: GroundedConflictStatus = GroundedConflictStatus.NONE,
    disclosable_claim_ids: tuple[UUID, ...] = (),
) -> AnswerEvaluationDecision:
    return AnswerEvaluationDecision(
        boundary=boundary,
        target_scope_id=target_scope.target_scope_id,
        observed_coverage_id=observed_coverage.observed_coverage_id,
        evaluation_lineage_reference=evaluation_lineage_reference,
        disclosure_status=disclosure_status,
        coverage_status=coverage_status,
        partiality_status=partiality_status,
        conflict_status=conflict_status,
        safe_reason=safe_reason,
        disclosable_claim_ids=disclosable_claim_ids,
    )


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
