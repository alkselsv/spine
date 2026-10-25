"""Pure composition contracts for the final Q&A disclosure gate.

The candidate is an internal, untrusted value.  Only ``AcceptedGroundedAnswer``
may cross the accepted-output port; every other branch contains a safe terminal
``AnswerQuestionOutput`` and never the candidate content.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.capabilities.answer_outcomes import (
    AnswerOutcomeDecisionInput,
    AnswerOutcomeResolution,
    resolve_answer_outcome,
)
from spine.domain.capabilities.answer_question import (
    AnswerContent,
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
)
from spine.domain.capabilities.answer_evaluation import (
    AnswerEvaluationDecision,
    GroundedConflict,
    GroundedClaimAssessment,
    ObservedCoverage,
    PartialFallbackAuthorization,
    TargetCompletenessScope,
)
from spine.domain.capabilities.answer_question_execution import AnswerQuestionExecutionPin
from spine.domain.capabilities.answer_question_runs import AgentResultRef
from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import ContextBundleRef


ANSWER_DISCLOSURE_REQUEST_SCHEMA = "answer-disclosure-request:v3"
ANSWER_DISCLOSURE_DECISION_SCHEMA = "answer-disclosure-decision:v1"
ACCEPTED_GROUNDED_ANSWER_SCHEMA = "accepted-grounded-answer:v1"
ANSWER_DISCLOSURE_RECORD_SCHEMA = "answer-disclosure-record:v1"
ANSWER_DISCLOSURE_RECORD_V2_SCHEMA = "answer-disclosure-record:v2"
ANSWER_DISCLOSURE_RECORD_V3_SCHEMA = "answer-disclosure-record:v3"
ANSWER_EXECUTION_CHECKPOINT_SCHEMA = "answer-execution-checkpoint:v1"
_DIGEST = r"^[0-9a-f]{64}$"


def _id(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class DisclosureEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNAVAILABLE = "unavailable"


class DisclosureEligibilityReason(str, Enum):
    CURRENT_AUTHORITY_CONFIRMED = "current_authority_confirmed"
    CURRENT_AUTHORITY_REJECTED = "current_authority_rejected"
    CURRENT_AUTHORITY_UNAVAILABLE = "current_authority_unavailable"


class CurrentDisclosureEligibility(DefinitionModel):
    """Typed current-authority result required before answer disclosure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["current-disclosure-eligibility:v1"] = (
        "current-disclosure-eligibility:v1"
    )
    status: DisclosureEligibilityStatus
    validation_reference: UUID
    reason: DisclosureEligibilityReason
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    run_id: UUID | None = None
    attempt_id: UUID | None = None
    validation_fact_ids: tuple[UUID, ...] = ()
    dependency_ids: tuple[UUID, ...] = ()

    @property
    def can_disclose(self) -> bool:
        return self.status is DisclosureEligibilityStatus.ELIGIBLE

    @model_validator(mode="after")
    def validate_result(self) -> "CurrentDisclosureEligibility":
        _id(self.validation_reference, "validation_reference")
        for name in ("workspace_id", "environment_id", "run_id", "attempt_id"):
            value = getattr(self, name)
            if value is not None:
                _id(value, name)
        if any(identifier.int == 0 for identifier in self.dependency_ids):
            raise ValueError("disclosure dependency IDs must be non-zero")
        if any(identifier.int == 0 for identifier in self.validation_fact_ids):
            raise ValueError("disclosure validation fact IDs must be non-zero")
        if len(set(self.validation_fact_ids)) != len(self.validation_fact_ids):
            raise ValueError("disclosure validation fact IDs must be unique")
        if self.status is DisclosureEligibilityStatus.ELIGIBLE and not self.validation_fact_ids:
            raise ValueError("eligible disclosure requires validation fact IDs")
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("disclosure dependency IDs must be unique")
        expected = {
            DisclosureEligibilityStatus.ELIGIBLE: DisclosureEligibilityReason.CURRENT_AUTHORITY_CONFIRMED,
            DisclosureEligibilityStatus.INELIGIBLE: DisclosureEligibilityReason.CURRENT_AUTHORITY_REJECTED,
            DisclosureEligibilityStatus.UNAVAILABLE: DisclosureEligibilityReason.CURRENT_AUTHORITY_UNAVAILABLE,
        }
        if self.reason is not expected[self.status]:
            raise ValueError("disclosure eligibility status and reason are inconsistent")
        return self


class AnswerDisclosureValidation(DefinitionModel):
    """Immutable references proving every mandatory validation stage ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-validation:v1"] = (
        "answer-disclosure-validation:v1"
    )
    citation_validation_reference: UUID
    semantic_support_validation_reference: UUID
    evaluation_lineage_reference: UUID
    disclosure_eligibility_reference: UUID
    context_bundle_id: UUID
    context_bundle_version: int = Field(ge=1)
    context_bundle_digest: str = Field(pattern=_DIGEST)
    claim_ids: tuple[UUID, ...] = Field(min_length=1)
    citation_ids: tuple[UUID, ...] = Field(min_length=1)
    validation_fact_ids: tuple[UUID, ...] = ()
    dependency_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> "AnswerDisclosureValidation":
        references = (
            self.citation_validation_reference,
            self.semantic_support_validation_reference,
            self.evaluation_lineage_reference,
            self.disclosure_eligibility_reference,
            self.context_bundle_id,
        )
        if any(reference.int == 0 for reference in references):
            raise ValueError("disclosure validation references must be non-zero")
        if any(identifier.int == 0 for identifier in (*self.claim_ids, *self.citation_ids)):
            raise ValueError("disclosure validation identifiers must be non-zero")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("disclosure claim IDs must be unique")
        if len(set(self.citation_ids)) != len(self.citation_ids):
            raise ValueError("disclosure citation IDs must be unique")
        if any(identifier.int == 0 for identifier in (*self.validation_fact_ids, *self.dependency_ids)):
            raise ValueError("disclosure lineage identifiers must be non-zero")
        if len(set(self.validation_fact_ids)) != len(self.validation_fact_ids):
            raise ValueError("disclosure validation fact IDs must be unique")
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("disclosure dependency IDs must be unique")
        return self


class AcceptedGroundedAnswer(DefinitionModel):
    """The only content-bearing value allowed past the final gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["accepted-grounded-answer:v1"] = ACCEPTED_GROUNDED_ANSWER_SCHEMA
    answer: AnswerContent
    validation: AnswerDisclosureValidation
    accepted_at: datetime

    @model_validator(mode="after")
    def validate_answer(self) -> "AcceptedGroundedAnswer":
        _aware(self.accepted_at, "accepted_at")
        claim_ids = tuple(claim.claim_id for claim in self.answer.claims)
        citation_ids = tuple(citation.citation_id for citation in self.answer.citations)
        if claim_ids != self.validation.claim_ids or citation_ids != self.validation.citation_ids:
            raise ValueError("accepted answer validation lineage is incomplete")
        if any(
            (
                citation.context_bundle.bundle.bundle_id != self.validation.context_bundle_id
                or citation.context_bundle.bundle.bundle_version
                != self.validation.context_bundle_version
                or citation.context_bundle.bundle.bundle_digest
                != self.validation.context_bundle_digest
            )
            for citation in self.answer.citations
        ):
            raise ValueError("accepted answer uses a different context bundle")
        return self


class AnswerDisclosureRequest(DefinitionModel):
    """All typed facts the final gate is allowed to consume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-request:v3"] = ANSWER_DISCLOSURE_REQUEST_SCHEMA
    request_id: UUID
    decision: AnswerOutcomeDecisionInput
    current_eligibility: CurrentDisclosureEligibility | None = None
    target_scope: TargetCompletenessScope | None = None
    observed_coverage: ObservedCoverage | None = None
    partial_fallback: PartialFallbackAuthorization | None = None
    claim_assessments: tuple[GroundedClaimAssessment, ...] = ()
    conflicts: tuple[GroundedConflict, ...] = ()

    @property
    def command_digest(self) -> str:
        payload = self.model_copy(
            update={
                "decision": self.decision.model_copy(update={"disclosure_eligible": False})
            }
        ).model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def validate_candidate(self) -> "AnswerDisclosureRequest":
        _id(self.request_id, "request_id")
        if self.decision.candidate is not None:
            candidate = self.decision.candidate
            if candidate.question != self.decision.question:
                raise ValueError("candidate question does not match disclosure request")
        lineage = (self.target_scope, self.observed_coverage)
        if any(value is not None for value in lineage):
            if any(value is None for value in lineage):
                raise ValueError("answer evaluation lineage must be complete")
            assert self.target_scope is not None
            assert self.observed_coverage is not None
            if self.decision.evaluation is None:
                raise ValueError("answer evaluation lineage must include its decision")
            assessed_claim_ids = tuple(item.claim_id for item in self.claim_assessments)
            if (
                not assessed_claim_ids
                or len(set(assessed_claim_ids)) != len(assessed_claim_ids)
                or self.target_scope.boundary != self.observed_coverage.boundary
                or self.target_scope.boundary != self.decision.evaluation.boundary
                or self.target_scope.target_scope_id
                != self.decision.evaluation.target_scope_id
                or self.observed_coverage.observed_coverage_id
                != self.decision.evaluation.observed_coverage_id
                or any(item.boundary != self.target_scope.boundary for item in self.conflicts)
                or any(
                    item.boundary != self.target_scope.boundary
                    for item in self.claim_assessments
                )
                or not set(self.decision.evaluation.disclosable_claim_ids).issubset(
                    assessed_claim_ids
                )
                or any(
                    not set(item.claim_ids).issubset(assessed_claim_ids)
                    for item in self.conflicts
                )
                or (
                    self.partial_fallback is not None
                    and self.partial_fallback.boundary != self.target_scope.boundary
                )
            ):
                raise ValueError("answer evaluation lineage has mixed boundaries")
        return self


class AnswerDisclosureDecision(DefinitionModel):
    """Result of pure gate composition; safe branches never carry a candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-decision:v1"] = ANSWER_DISCLOSURE_DECISION_SCHEMA
    resolution: AnswerOutcomeResolution
    validation: AnswerDisclosureValidation | None = None
    accepted_answer: AcceptedGroundedAnswer | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "AnswerDisclosureDecision":
        if self.accepted_answer is not None:
            if self.resolution.outcome is not AnswerQuestionOutcome.ANSWERED:
                raise ValueError("only answered resolution can carry accepted content")
            if self.resolution.output is None or self.resolution.output.answer != self.accepted_answer.answer:
                raise ValueError("accepted answer does not match terminal resolution")
            if self.validation != self.accepted_answer.validation:
                raise ValueError("accepted answer validation does not match decision validation")
        elif self.resolution.outcome is AnswerQuestionOutcome.ANSWERED:
            raise ValueError("answered resolution requires accepted content")
        return self

    @property
    def output(self) -> AnswerQuestionOutput:
        if self.resolution.output is None:
            raise ValueError("disclosure decision is not terminal")
        return self.resolution.output


class AnswerDisclosureRecord(DefinitionModel):
    """Immutable persisted gate result without rejected candidate content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-disclosure-record:v1"] = ANSWER_DISCLOSURE_RECORD_SCHEMA
    request_id: UUID
    request_digest: str = Field(pattern=_DIGEST)
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID
    attempt_id: UUID
    outcome: AnswerQuestionOutcome
    validation: AnswerDisclosureValidation | None = None
    accepted_answer: AcceptedGroundedAnswer | None = None
    safe_output: AnswerQuestionOutput | None = None
    terminal_output: AnswerQuestionOutput | None = None
    created_at: datetime

    @property
    def output(self) -> AnswerQuestionOutput | None:
        """Return the exact persisted terminal output when available."""

        return self.terminal_output or self.safe_output

    @model_validator(mode="after")
    def validate_record(self) -> "AnswerDisclosureRecord":
        for name in (
            "request_id",
            "workspace_id",
            "environment_id",
            "run_id",
            "attempt_id",
        ):
            _id(getattr(self, name), name)
        _aware(self.created_at, "created_at")
        if (self.accepted_answer is None) == (self.safe_output is None):
            raise ValueError("a disclosure record must contain exactly one result payload")
        if self.accepted_answer is not None:
            if self.outcome is not AnswerQuestionOutcome.ANSWERED:
                raise ValueError("accepted disclosure record must be answered")
            if self.validation != self.accepted_answer.validation:
                raise ValueError("accepted disclosure validation does not match answer validation")
            payload_workspace_id = self.accepted_answer.answer.workspace_id
            payload_environment_id = self.accepted_answer.answer.environment_id
            payload_run_id = self.accepted_answer.answer.run_id
            payload_attempt_id = self.accepted_answer.answer.attempt_id
        else:
            if self.safe_output is None or self.safe_output.outcome is AnswerQuestionOutcome.ANSWERED:
                raise ValueError("safe disclosure record cannot contain answer content")
            if self.safe_output.outcome is not self.outcome:
                raise ValueError("safe disclosure outcome does not match record")
            payload_workspace_id = self.safe_output.scope.workspace_id
            payload_environment_id = self.safe_output.scope.environment_id
            payload_run_id = self.safe_output.correlation.run_id
            payload_attempt_id = self.safe_output.correlation.attempt_id
        if self.terminal_output is not None:
            terminal = self.terminal_output
            if (
                terminal.outcome is not self.outcome
                or terminal.scope.workspace_id != self.workspace_id
                or terminal.scope.environment_id != self.environment_id
                or terminal.correlation.run_id != self.run_id
                or terminal.correlation.attempt_id != self.attempt_id
                or (
                    self.accepted_answer is not None
                    and terminal.answer != self.accepted_answer.answer
                )
                or (
                    self.safe_output is not None
                    and terminal != self.safe_output
                )
            ):
                raise ValueError("terminal disclosure output does not match its record")
        if (
            payload_workspace_id != self.workspace_id
            or payload_environment_id != self.environment_id
            or payload_run_id != self.run_id
            or payload_attempt_id != self.attempt_id
        ):
            raise ValueError("disclosure result scope does not match its record")
        return self


class AnswerExecutionCheckpoint(DefinitionModel):
    """Non-content references needed to resume a saved disclosure safely."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["answer-execution-checkpoint:v1"] = ANSWER_EXECUTION_CHECKPOINT_SCHEMA
    execution_pin_digest: str = Field(pattern=_DIGEST)
    context_bundle_ref: ContextBundleRef | None = None
    candidate_input_artifact_id: UUID | None = None
    agent_result_ref: AgentResultRef | None = None
    output_artifact_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_execution_references(self) -> "AnswerExecutionCheckpoint":
        if self.candidate_input_artifact_id is not None:
            _id(self.candidate_input_artifact_id, "candidate_input_artifact_id")
        if any(identifier.int == 0 for identifier in self.output_artifact_ids):
            raise ValueError("execution checkpoint artifact IDs must be non-zero")
        if len(set(self.output_artifact_ids)) != len(self.output_artifact_ids):
            raise ValueError("execution checkpoint artifact IDs must be unique")
        if bool(self.output_artifact_ids) != (self.agent_result_ref is not None):
            raise ValueError("output artifacts require their persisted AgentResult reference")
        if self.output_artifact_ids and self.candidate_input_artifact_id is None:
            raise ValueError("output artifacts require their persisted candidate input")
        if self.candidate_input_artifact_id is not None and self.context_bundle_ref is None:
            raise ValueError("candidate input requires its persisted ContextBundle")
        return self


class AnswerDisclosureRecordV2(AnswerDisclosureRecord):
    """Disclosure v2 adds only references for retry-safe terminal recovery."""

    schema_version: Literal["answer-disclosure-record:v2"] = ANSWER_DISCLOSURE_RECORD_V2_SCHEMA
    execution_checkpoint: AnswerExecutionCheckpoint

    @model_validator(mode="after")
    def validate_checkpoint_identity(self) -> "AnswerDisclosureRecordV2":
        checkpoint = self.execution_checkpoint
        if self.accepted_answer is not None:
            validation = self.accepted_answer.validation
            bundle = checkpoint.context_bundle_ref
            if (
                bundle is None
                or bundle.bundle_id != validation.context_bundle_id
                or bundle.bundle_version != validation.context_bundle_version
                or bundle.bundle_digest != validation.context_bundle_digest
                or checkpoint.agent_result_ref is None
                or checkpoint.candidate_input_artifact_id is None
                or not checkpoint.output_artifact_ids
            ):
                raise ValueError("answered disclosure checkpoint is incomplete")
        return self

    def as_v1(self) -> AnswerDisclosureRecord:
        """Project the historical disclosure payload without recovery metadata."""

        return AnswerDisclosureRecord(
            request_id=self.request_id,
            request_digest=self.request_digest,
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            outcome=self.outcome,
            validation=self.validation,
            accepted_answer=self.accepted_answer,
            safe_output=self.safe_output,
            terminal_output=self.terminal_output,
            created_at=self.created_at,
        )


class AnswerDisclosureRecordV3(AnswerDisclosureRecordV2):
    """Disclosure v3 persists immutable completeness and conflict decisions."""

    schema_version: Literal["answer-disclosure-record:v3"] = (
        ANSWER_DISCLOSURE_RECORD_V3_SCHEMA
    )
    target_scope: TargetCompletenessScope
    observed_coverage: ObservedCoverage
    partial_fallback: PartialFallbackAuthorization | None = None
    claim_assessments: tuple[GroundedClaimAssessment, ...] = ()
    conflicts: tuple[GroundedConflict, ...] = ()
    evaluation: AnswerEvaluationDecision

    @model_validator(mode="after")
    def validate_evaluation_lineage(self) -> "AnswerDisclosureRecordV3":
        boundary = self.target_scope.boundary
        assessed_claim_ids = tuple(item.claim_id for item in self.claim_assessments)
        if (
            not assessed_claim_ids
            or len(set(assessed_claim_ids)) != len(assessed_claim_ids)
            or self.observed_coverage.boundary != boundary
            or self.evaluation.boundary != boundary
            or self.target_scope.target_scope_id != self.evaluation.target_scope_id
            or self.observed_coverage.observed_coverage_id
            != self.evaluation.observed_coverage_id
            or boundary.workspace_id != self.workspace_id
            or boundary.environment_id != self.environment_id
            or boundary.run_id != self.run_id
            or boundary.attempt_id != self.attempt_id
            or any(item.boundary != boundary for item in self.conflicts)
            or any(item.boundary != boundary for item in self.claim_assessments)
            or not set(self.evaluation.disclosable_claim_ids).issubset(
                assessed_claim_ids
            )
            or any(
                not set(item.claim_ids).issubset(assessed_claim_ids)
                for item in self.conflicts
            )
            or (
                self.partial_fallback is not None
                and self.partial_fallback.boundary != boundary
            )
        ):
            raise ValueError("persisted answer evaluation lineage is inconsistent")
        return self


def parse_answer_disclosure_record(payload: object) -> AnswerDisclosureRecord:
    """Restore either persisted disclosure schema while preserving v1 reads."""

    if isinstance(payload, dict):
        if payload.get("schema_version") == ANSWER_DISCLOSURE_RECORD_V3_SCHEMA:
            return AnswerDisclosureRecordV3.model_validate(payload)
        if payload.get("schema_version") == ANSWER_DISCLOSURE_RECORD_V2_SCHEMA:
            return AnswerDisclosureRecordV2.model_validate(payload)
    return AnswerDisclosureRecord.model_validate(payload)


def execution_checkpoint_for_disclosure(
    record: AnswerDisclosureRecord,
) -> AnswerExecutionCheckpoint | None:
    """Return recovery metadata only for the schema version that defines it."""

    if isinstance(record, AnswerDisclosureRecordV2):
        return record.execution_checkpoint
    return None


def compose_answer_disclosure(
    request: AnswerDisclosureRequest,
    *,
    accepted_at: datetime,
) -> AnswerDisclosureDecision:
    """Run the binary disclosure predicate after all validation facts exist."""

    validation = _validation_for(request)
    eligibility = request.current_eligibility
    if not _eligibility_matches_candidate(request):
        return AnswerDisclosureDecision(
            resolution=_safe_resolution(
                request,
                AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
                outcome=AnswerQuestionOutcome.FAILED,
            ),
            validation=validation,
        )
    if request.decision.candidate is not None and (
        eligibility is None
        or eligibility.status is DisclosureEligibilityStatus.UNAVAILABLE
    ):
        return AnswerDisclosureDecision(
            resolution=_safe_resolution(
                request,
                AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
                outcome=AnswerQuestionOutcome.FAILED,
            ),
            validation=validation,
        )
    eligible = eligibility is not None and eligibility.can_disclose
    decision = request.decision.model_copy(update={"disclosure_eligible": eligible})
    resolution = resolve_answer_outcome(decision)
    if resolution.retry_required:
        resolution = _safe_resolution(
            request,
            AnswerQuestionSafeReasonCode.INSUFFICIENT_SUPPORT,
        )
    if resolution.outcome is not AnswerQuestionOutcome.ANSWERED:
        return AnswerDisclosureDecision(resolution=resolution, validation=validation)

    candidate = request.decision.candidate
    evaluation = request.decision.evaluation
    citation_validation = request.decision.citation_validation
    semantic_support = request.decision.semantic_support
    if candidate is None or evaluation is None or citation_validation is None or semantic_support is None:
        return AnswerDisclosureDecision(
            resolution=_safe_resolution(request, AnswerQuestionSafeReasonCode.REJECTED_CANDIDATE),
            validation=validation,
        )
    assert eligibility is not None
    assert validation is not None
    return AnswerDisclosureDecision(
        resolution=resolution,
        validation=validation,
        accepted_answer=AcceptedGroundedAnswer(
            answer=candidate.answer,
            validation=validation,
            accepted_at=accepted_at,
        ),
    )


def _validation_for(request: AnswerDisclosureRequest) -> AnswerDisclosureValidation | None:
    candidate = request.decision.candidate
    eligibility = request.current_eligibility
    citation_validation = request.decision.citation_validation
    semantic_support = request.decision.semantic_support
    evaluation = request.decision.evaluation
    if (
        candidate is None
        or eligibility is None
        or citation_validation is None
        or semantic_support is None
        or evaluation is None
    ):
        return None
    return AnswerDisclosureValidation(
        citation_validation_reference=citation_validation.validation_reference,
        semantic_support_validation_reference=semantic_support.validation_reference,
        evaluation_lineage_reference=evaluation.evaluation_lineage_reference,
        disclosure_eligibility_reference=eligibility.validation_reference,
        context_bundle_id=candidate.context_bundle.bundle.bundle_id,
        context_bundle_version=candidate.context_bundle.bundle.bundle_version,
        context_bundle_digest=candidate.context_bundle.bundle.bundle_digest,
        claim_ids=tuple(claim.claim_id for claim in candidate.answer.claims),
        citation_ids=tuple(citation.citation_id for citation in candidate.answer.citations),
        validation_fact_ids=eligibility.validation_fact_ids,
        dependency_ids=eligibility.dependency_ids,
    )


def _eligibility_matches_candidate(request: AnswerDisclosureRequest) -> bool:
    candidate = request.decision.candidate
    eligibility = request.current_eligibility
    if candidate is None or eligibility is None:
        return True
    scope = candidate.answer
    return all(
        expected is None or expected == actual
        for expected, actual in (
            (eligibility.workspace_id, scope.workspace_id),
            (eligibility.environment_id, scope.environment_id),
            (eligibility.run_id, scope.run_id),
            (eligibility.attempt_id, scope.attempt_id),
        )
    )


def _safe_resolution(
    request: AnswerDisclosureRequest,
    reason: AnswerQuestionSafeReasonCode,
    *,
    outcome: AnswerQuestionOutcome = AnswerQuestionOutcome.ABSTAINED,
) -> AnswerOutcomeResolution:
    return AnswerOutcomeResolution(
        outcome=outcome,
        output=AnswerQuestionOutput(
            correlation=request.decision.question.correlation,
            scope=request.decision.question.scope,
            outcome=outcome,
            reason=AnswerQuestionSafeReason(code=reason),
        ),
    )


# Compatibility aliases used by callers that use the shorter product terms.
GroundedAnswer = AcceptedGroundedAnswer
AcceptedAnswer = AcceptedGroundedAnswer


__all__ = [
    "ACCEPTED_GROUNDED_ANSWER_SCHEMA",
    "ANSWER_DISCLOSURE_DECISION_SCHEMA",
    "ANSWER_DISCLOSURE_RECORD_SCHEMA",
    "ANSWER_DISCLOSURE_RECORD_V3_SCHEMA",
    "ANSWER_DISCLOSURE_REQUEST_SCHEMA",
    "AcceptedAnswer",
    "AcceptedGroundedAnswer",
    "AnswerDisclosureDecision",
    "AnswerDisclosureRequest",
    "AnswerDisclosureRecordV3",
    "AnswerDisclosureRecord",
    "AnswerDisclosureValidation",
    "CurrentDisclosureEligibility",
    "DisclosureEligibilityReason",
    "DisclosureEligibilityStatus",
    "GroundedAnswer",
    "compose_answer_disclosure",
]
