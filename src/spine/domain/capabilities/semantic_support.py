"""Provider-neutral contracts for validating semantic claim support.

The request carries the exact authorized evidence content to a validator, while
the immutable result retains only claim/evidence/relationship lineage.  A
citation, rank, or confidence value is deliberately not an outcome of this
boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.capabilities.answer_question import (
    AnswerClaim,
    AnswerQuestionScope,
    Citation,
    ClaimEvidenceRelationship,
    RunAttemptCorrelation,
)
from spine.domain.common import DefinitionModel, ValidatedDefinitionModel, VersionIdentifier
from spine.domain.context_bundles import ContextBundleLineageRef


SEMANTIC_SUPPORT_POLICY_SCHEMA = "semantic-support-policy:v1"
SEMANTIC_SUPPORT_REQUEST_SCHEMA = "semantic-support-request:v1"
SEMANTIC_SUPPORT_ASSESSMENT_SCHEMA = "semantic-support-assessment:v1"
SEMANTIC_SUPPORT_RESULT_SCHEMA = "semantic-support-result:v1"
_DIGEST = r"^[0-9a-f]{64}$"
_MAX_EVIDENCE_CONTENT_LENGTH = 32_768


class SemanticSupportOutcome(str, Enum):
    """Closed validator outcomes used by the disclosure gate."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    INCONCLUSIVE = "inconclusive"
    VALIDATOR_UNAVAILABLE = "validator_unavailable"
    VALIDATOR_FAILED = "validator_failed"


class SemanticSupportReasonCode(str, Enum):
    """Disclosure-safe explanations without model/provider detail."""

    SUPPORTED_BY_EVIDENCE = "supported_by_evidence"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    INSUFFICIENT_OR_CONTRADICTORY_EVIDENCE = "insufficient_or_contradictory_evidence"
    VALIDATOR_UNAVAILABLE = "validator_unavailable"
    VALIDATOR_FAILED = "validator_failed"


def _expected_reason_code(outcome: SemanticSupportOutcome) -> SemanticSupportReasonCode:
    return {
        SemanticSupportOutcome.SUPPORTED: SemanticSupportReasonCode.SUPPORTED_BY_EVIDENCE,
        SemanticSupportOutcome.UNSUPPORTED: SemanticSupportReasonCode.UNSUPPORTED_CLAIM,
        SemanticSupportOutcome.INCONCLUSIVE: SemanticSupportReasonCode.INSUFFICIENT_OR_CONTRADICTORY_EVIDENCE,
        SemanticSupportOutcome.VALIDATOR_UNAVAILABLE: SemanticSupportReasonCode.VALIDATOR_UNAVAILABLE,
        SemanticSupportOutcome.VALIDATOR_FAILED: SemanticSupportReasonCode.VALIDATOR_FAILED,
    }[outcome]


class SemanticSupportPolicy(DefinitionModel):
    """Pinned identity and behavior mode for one semantic-support validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SEMANTIC_SUPPORT_POLICY_SCHEMA] = SEMANTIC_SUPPORT_POLICY_SCHEMA
    policy_id: UUID
    policy_version: VersionIdentifier
    policy_digest: str = Field(pattern=_DIGEST)
    mandatory: bool = True
    validator_contract_version: VersionIdentifier = "semantic-support:v1"

    @field_validator("policy_id")
    @classmethod
    def validate_policy_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("policy_id must be non-zero")
        return value


class SemanticSupportEvidence(DefinitionModel):
    """Exact authorized evidence supplied to the semantic validator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["semantic-support-evidence:v1"] = (
        "semantic-support-evidence:v1"
    )
    citation: Citation
    content: str = Field(min_length=1, max_length=_MAX_EVIDENCE_CONTENT_LENGTH)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence content must not be blank")
        return value


class SemanticSupportRequest(ValidatedDefinitionModel):
    """One claim and its complete attributed evidence graph for validation."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )

    schema_version: Literal[SEMANTIC_SUPPORT_REQUEST_SCHEMA] = (
        SEMANTIC_SUPPORT_REQUEST_SCHEMA
    )
    policy: SemanticSupportPolicy
    scope: AnswerQuestionScope
    correlation: RunAttemptCorrelation
    context_bundle: ContextBundleLineageRef
    claim: AnswerClaim
    relationships: tuple[ClaimEvidenceRelationship, ...] = Field(min_length=1)
    evidence: tuple[SemanticSupportEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> SemanticSupportRequest:
        if (
            (self.claim.workspace_id, self.claim.environment_id)
            != (self.scope.workspace_id, self.scope.environment_id)
            or (self.claim.run_id, self.claim.attempt_id)
            != (self.correlation.run_id, self.correlation.attempt_id)
        ):
            raise ValueError("semantic support claim lineage is inconsistent")
        if (
            (self.context_bundle.workspace_id, self.context_bundle.environment_id)
            != (self.scope.workspace_id, self.scope.environment_id)
            or (self.context_bundle.run_id, self.context_bundle.attempt_id)
            != (self.correlation.run_id, self.correlation.attempt_id)
        ):
            raise ValueError("semantic support context scope is inconsistent")
        if self.context_bundle != self._expected_bundle_lineage:
            raise ValueError("semantic support context bundle lineage is inconsistent")

        citation_ids = tuple(item.citation.citation_id for item in self.evidence)
        evidence_ids = tuple(item.citation.evidence_id for item in self.evidence)
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("semantic support citations must be unique")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("semantic support evidence identities must be unique")
        for item in self.evidence:
            citation = item.citation
            if citation.context_bundle != self.context_bundle:
                raise ValueError("semantic support evidence has mixed bundle lineage")
            if citation.answer_id != self._answer_id:
                raise ValueError("semantic support evidence belongs to another answer")

        relationship_ids = tuple(item.relationship_id for item in self.relationships)
        if len(set(relationship_ids)) != len(relationship_ids):
            raise ValueError("semantic support relationships must be unique")
        attributed_citation_ids: set[UUID] = set()
        for relationship in self.relationships:
            if (
                relationship.answer_id != self._answer_id
                or relationship.workspace_id != self.scope.workspace_id
                or relationship.environment_id != self.scope.environment_id
                or relationship.run_id != self.correlation.run_id
                or relationship.attempt_id != self.correlation.attempt_id
                or relationship.claim_id != self.claim.claim_id
            ):
                raise ValueError("semantic support relationship lineage is inconsistent")
            attributed_citation_ids.update(relationship.citation_ids)
        if attributed_citation_ids != set(citation_ids):
            raise ValueError("every semantic support citation must be attributed")
        return self

    @property
    def _answer_id(self) -> UUID:
        return self.claim.location.answer_id

    @property
    def _expected_bundle_lineage(self) -> ContextBundleLineageRef:
        first = self.evidence[0].citation.context_bundle
        return first


class SemanticSupportAssessment(DefinitionModel):
    """Provider response whose identities must exactly match the request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SEMANTIC_SUPPORT_ASSESSMENT_SCHEMA] = (
        SEMANTIC_SUPPORT_ASSESSMENT_SCHEMA
    )
    claim_id: UUID
    outcome: SemanticSupportOutcome
    reason_code: SemanticSupportReasonCode
    evidence_ids: tuple[str, ...] = ()
    relationship_ids: tuple[UUID, ...] = ()

    @field_validator("claim_id")
    @classmethod
    def validate_claim_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("claim_id must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_assessment(self) -> SemanticSupportAssessment:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("semantic support evidence identities must be unique")
        if any(identifier.int == 0 for identifier in self.relationship_ids):
            raise ValueError("semantic support relationship identifiers must be non-zero")
        if len(set(self.relationship_ids)) != len(self.relationship_ids):
            raise ValueError("semantic support relationships must be unique")
        is_provider_failure = self.outcome in {
            SemanticSupportOutcome.VALIDATOR_UNAVAILABLE,
            SemanticSupportOutcome.VALIDATOR_FAILED,
        }
        if not is_provider_failure and (not self.evidence_ids or not self.relationship_ids):
            raise ValueError("a semantic assessment must retain evidence lineage")
        expected_reason = _expected_reason_code(self.outcome)
        if self.reason_code is not expected_reason:
            raise ValueError("semantic assessment reason does not match outcome")
        return self


class SemanticSupportResult(DefinitionModel):
    """Immutable, content-free validation record used by disclosure policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SEMANTIC_SUPPORT_RESULT_SCHEMA] = (
        SEMANTIC_SUPPORT_RESULT_SCHEMA
    )
    validation_record_id: UUID
    policy: SemanticSupportPolicy
    scope: AnswerQuestionScope
    correlation: RunAttemptCorrelation
    context_bundle: ContextBundleLineageRef
    answer_id: UUID
    claim_id: UUID
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    citation_ids: tuple[UUID, ...] = Field(min_length=1)
    relationship_ids: tuple[UUID, ...] = Field(min_length=1)
    outcome: SemanticSupportOutcome
    reason_code: SemanticSupportReasonCode
    disclosure_eligible: bool
    qna_failure: bool

    @field_validator("validation_record_id", "answer_id", "claim_id")
    @classmethod
    def validate_ids(cls, value: UUID, info: Any) -> UUID:
        if value.int == 0:
            raise ValueError(f"{info.field_name} must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_result_policy(self) -> SemanticSupportResult:
        if self.context_bundle.workspace_id != self.scope.workspace_id:
            raise ValueError("semantic support result workspace lineage is inconsistent")
        if self.context_bundle.environment_id != self.scope.environment_id:
            raise ValueError("semantic support result environment lineage is inconsistent")
        if (
            self.context_bundle.run_id != self.correlation.run_id
            or self.context_bundle.attempt_id != self.correlation.attempt_id
        ):
            raise ValueError("semantic support result attempt lineage is inconsistent")
        expected_eligibility = self.outcome is SemanticSupportOutcome.SUPPORTED
        if self.disclosure_eligible != expected_eligibility:
            raise ValueError("only supported semantic results can be disclosure eligible")
        expected_failure = self.policy.mandatory and self.outcome in {
            SemanticSupportOutcome.VALIDATOR_UNAVAILABLE,
            SemanticSupportOutcome.VALIDATOR_FAILED,
        }
        if self.qna_failure != expected_failure:
            raise ValueError("semantic support failure mapping is inconsistent")
        expected_reason = _expected_reason_code(self.outcome)
        if self.reason_code is not expected_reason:
            raise ValueError("semantic result reason does not match outcome")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("semantic support result evidence identities must be unique")
        if len(set(self.citation_ids)) != len(self.citation_ids):
            raise ValueError("semantic support result citation identities must be unique")
        if len(set(self.relationship_ids)) != len(self.relationship_ids):
            raise ValueError("semantic support result relationships must be unique")
        return self
