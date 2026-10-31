"""Versioned evidence contract for governed private-real ASR evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.evaluations.private_asr_corpus import (
    CorpusSplit,
    PrivateReference,
    QualificationSufficiency,
)


PRIVATE_REAL_ASR_EVALUATION_SCHEMA = "asr-candidate-qualification:private-real:v1"


class PrivateCaseDisposition(str, Enum):
    DENIED = "denied"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"
    UNANNOTATED = "unannotated"
    UNSCORABLE = "unscorable"
    MEASURED = "measured"


class PrivateEvaluationStatus(str, Enum):
    NOT_VERIFIED = "NOT_VERIFIED"


class PrivateEvaluationFailureKind(str, Enum):
    DENIED = "denied"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"


class PrivateCandidateConfiguration(DefinitionModel):
    """Frozen provider-neutral identity supplied to the injected runtime factory."""

    candidate_id: VersionIdentifier
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    processing_profile_reference: VersionIdentifier
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PrivateCaseOutcome(DefinitionModel):
    """Private case-level disposition with no transcript or content field."""

    record_ref: PrivateReference
    split: CorpusSplit
    disposition: PrivateCaseDisposition
    word_errors: StrictInt | None = Field(default=None, ge=0)
    reference_words: StrictInt | None = Field(default=None, ge=0)
    character_errors: StrictInt | None = Field(default=None, ge=0)
    reference_characters: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_measurement(self) -> "PrivateCaseOutcome":
        metrics = (self.word_errors, self.reference_words, self.character_errors, self.reference_characters)
        if (self.disposition is PrivateCaseDisposition.MEASURED) != all(value is not None for value in metrics):
            raise ValueError("only measured cases may include complete error counts")
        if self.word_errors is not None and self.reference_words == 0:
            raise ValueError("word errors require a non-empty reference denominator")
        if self.character_errors is not None and self.reference_characters == 0:
            raise ValueError("character errors require a non-empty reference denominator")
        return self


class PrivateEvaluationDenominators(DefinitionModel):
    eligible: StrictInt = Field(ge=0)
    admitted: StrictInt = Field(ge=0)
    verified: StrictInt = Field(ge=0)
    annotated: StrictInt = Field(ge=0)
    scorable: StrictInt = Field(ge=0)
    measured: StrictInt = Field(ge=0)
    independent_lineages: StrictInt = Field(ge=0)
    denied: StrictInt = Field(ge=0)
    revoked: StrictInt = Field(ge=0)
    unavailable: StrictInt = Field(ge=0)
    unsupported_metrics: tuple[VersionIdentifier, ...] = ()
    insufficient_coverage: tuple[VersionIdentifier, ...] = ()

    @model_validator(mode="after")
    def validate_progression(self) -> "PrivateEvaluationDenominators":
        if self.measured > self.scorable or self.scorable > self.annotated:
            raise ValueError("evaluation denominators are out of sequence")
        if self.annotated > self.verified or self.verified > self.admitted:
            raise ValueError("evaluation denominators are out of sequence")
        if self.admitted > self.eligible:
            raise ValueError("admitted denominator exceeds eligible cases")
        if self.independent_lineages > self.eligible:
            raise ValueError("independent lineages exceed eligible cases")
        return self


class PrivateEvaluationRefusalOutcome(DefinitionModel):
    """In-memory, content-free refusal evidence; never a report/export artifact."""

    schema_version: Literal["private-asr-run-refusal:v1"] = "private-asr-run-refusal:v1"
    kind: PrivateEvaluationFailureKind
    stage: VersionIdentifier
    eligible: StrictInt = Field(ge=0)
    admitted: StrictInt = Field(ge=0)
    verified: StrictInt = Field(ge=0)
    annotated: StrictInt = Field(ge=0)
    denied: StrictInt = Field(ge=0)
    revoked: StrictInt = Field(ge=0)
    unavailable: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_single_refusal(self) -> "PrivateEvaluationRefusalOutcome":
        if self.admitted > self.eligible or self.verified > self.admitted or self.annotated > self.verified:
            raise ValueError("refusal coverage counters are out of sequence")
        counts = (self.denied, self.revoked, self.unavailable)
        if sum(counts) != 1:
            raise ValueError("refusal must record exactly one terminal outcome")
        expected = {
            PrivateEvaluationFailureKind.DENIED: (1, 0, 0),
            PrivateEvaluationFailureKind.REVOKED: (0, 1, 0),
            PrivateEvaluationFailureKind.UNAVAILABLE: (0, 0, 1),
        }[self.kind]
        if counts != expected:
            raise ValueError("refusal counts do not match outcome kind")
        return self


class PrivateRealASREvaluationReport(DefinitionModel):
    """Restricted, immutable detailed report; write only through private storage."""

    schema_version: Literal[PRIVATE_REAL_ASR_EVALUATION_SCHEMA] = PRIVATE_REAL_ASR_EVALUATION_SCHEMA
    report_id: UUID
    dataset_id: UUID
    manifest_version: VersionIdentifier
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_id: VersionIdentifier
    evaluator_version: VersionIdentifier
    normalizer_version: VersionIdentifier
    candidate_id: VersionIdentifier
    candidate_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    generated_at: datetime
    outcomes: tuple[PrivateCaseOutcome, ...]
    denominators: PrivateEvaluationDenominators
    measurement_limitations: tuple[VersionIdentifier, ...]
    coverage_disposition: VersionIdentifier
    status: PrivateEvaluationStatus = PrivateEvaluationStatus.NOT_VERIFIED
    sufficiency: QualificationSufficiency = QualificationSufficiency.NOT_VERIFIED
    failure_code: VersionIdentifier | None = None
    audit_receipt_ref: PrivateReference
    retention_profile_ref: PrivateReference
    protocol_decision_ref: PrivateReference | None = None

    @model_validator(mode="after")
    def validate_private_report(self) -> "PrivateRealASREvaluationReport":
        if self.report_id.int == 0 or self.dataset_id.int == 0:
            raise ValueError("report and dataset identities must be non-zero")
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("report scope identities must be non-zero")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("report timestamp must be timezone-aware")
        if self.sufficiency is not QualificationSufficiency.NOT_VERIFIED:
            raise ValueError("private runner cannot approve protocol sufficiency")
        if self.status is not PrivateEvaluationStatus.NOT_VERIFIED:
            raise ValueError("offline runner evidence cannot qualify a real candidate")
        if len({item.record_ref.reference_id for item in self.outcomes}) != len(self.outcomes):
            raise ValueError("private report case identities must be unique")
        return self


__all__ = [
    "PRIVATE_REAL_ASR_EVALUATION_SCHEMA", "PrivateCaseDisposition", "PrivateCaseOutcome",
    "PrivateEvaluationDenominators", "PrivateEvaluationStatus", "PrivateRealASREvaluationReport",
    "PrivateCandidateConfiguration",
    "PrivateEvaluationFailureKind", "PrivateEvaluationRefusalOutcome",
]
