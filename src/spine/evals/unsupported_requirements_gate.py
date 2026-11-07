"""Fail-closed quality gate for unsupported extracted requirements."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.capabilities import (
    RequirementsEvidenceAvailability,
    RequirementsExecutionConfiguration,
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
)
from spine.domain.common import DefinitionModel
from spine.domain.workflows import RequirementEvidenceRelationship, RequirementState

UNSUPPORTED_REQUIREMENTS_POLICY_VERSION = "unsupported-requirements-quality-gate:v1"
UNSUPPORTED_REQUIREMENTS_DATASET_VERSION = "unsupported-requirements-synthetic:v1"
UNSUPPORTED_REQUIREMENTS_EVALUATOR_VERSION = "unsupported-requirements-evaluator:v1"
UNSUPPORTED_REQUIREMENTS_DATASET_SHA256 = (
    "a62131c37564e5e6d0cfb95890bb3b530598604b08e9672fb2b6f6940b444746"
)
_DATASET = Path(__file__).with_name("data") / "unsupported_requirements_v1.json"


class UnsupportedRequirementsClass(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CLARIFICATION_NEEDED = "clarification-needed"
    INCONCLUSIVE_ABSTENTION = "inconclusive/abstention"


class UnsupportedRequirementsStratum(str, Enum):
    CONTRADICTION = "contradiction"
    MISSING_OR_FABRICATED_EVIDENCE = "missing-or-fabricated-evidence"
    UNAVAILABLE_AUTHORITY = "stale-denied-or-missing-authority"
    WEAK_OR_UNKNOWN_RECOGNITION = "weak-or-unknown-recognition"
    MALICIOUS_INSTRUCTION = "malicious-instruction"


class UnsupportedRequirementsGateResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIED = "NOT_VERIFIED"


class UnsupportedRequirementsEvalCase(DefinitionModel):
    case_id: str = Field(min_length=1, max_length=128)
    transcript: str = Field(min_length=1, max_length=4_096)
    actual_class: UnsupportedRequirementsClass
    expected_prediction: UnsupportedRequirementsClass
    strata: tuple[UnsupportedRequirementsStratum, ...] = ()
    recognition_quality: Literal["reliable", "weak", "unknown"] = "reliable"
    speaker_known: bool = True
    availability: Literal["current", "denied", "stale", "missing", "fabricated"] = (
        "current"
    )

    @model_validator(mode="after")
    def validate_strata(self) -> "UnsupportedRequirementsEvalCase":
        if len(set(self.strata)) != len(self.strata):
            raise ValueError("case strata must be unique")
        return self


class UnsupportedRequirementsEvalDataset(DefinitionModel):
    schema_version: Literal["unsupported-requirements-dataset:v1"]
    dataset_version: Literal["unsupported-requirements-synthetic:v1"]
    source_assumptions: tuple[str, ...] = Field(min_length=1)
    cases: tuple[UnsupportedRequirementsEvalCase, ...] = Field(min_length=1)
    sha256: str = Field(
        default=UNSUPPORTED_REQUIREMENTS_DATASET_SHA256,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_case_ids(self) -> "UnsupportedRequirementsEvalDataset":
        case_ids = tuple(case.case_id for case in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("dataset case ids must be unique")
        return self


class UnsupportedRequirementsObservation(DefinitionModel):
    case_id: str = Field(min_length=1, max_length=128)
    predicted_class: UnsupportedRequirementsClass


class UnsupportedRequirementsEvaluationIdentity(DefinitionModel):
    tested_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    capability_id: UUID
    input_schema: str = Field(min_length=1, max_length=256)
    output_schema: str = Field(min_length=1, max_length=256)
    agent_version_id: UUID
    implementation_key: str = Field(min_length=1, max_length=256)
    execution_configuration: RequirementsExecutionConfiguration


class UnsupportedRequirementsRate(DefinitionModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: Decimal | None


class UnsupportedRequirementsConfusionCell(DefinitionModel):
    actual_class: UnsupportedRequirementsClass
    predicted_class: UnsupportedRequirementsClass
    count: int = Field(ge=0)


class UnsupportedRequirementsStratumResult(DefinitionModel):
    stratum: UnsupportedRequirementsStratum
    labelled_count: int = Field(ge=0)
    observed_count: int = Field(ge=0)


class UnsupportedRequirementsEvalReport(DefinitionModel):
    schema_version: Literal["unsupported-requirements-report:v1"] = (
        "unsupported-requirements-report:v1"
    )
    policy_version: Literal["unsupported-requirements-quality-gate:v1"] = (
        "unsupported-requirements-quality-gate:v1"
    )
    evaluator_version: Literal["unsupported-requirements-evaluator:v1"] = (
        "unsupported-requirements-evaluator:v1"
    )
    dataset_version: Literal["unsupported-requirements-synthetic:v1"] = (
        "unsupported-requirements-synthetic:v1"
    )
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity: UnsupportedRequirementsEvaluationIdentity
    result: UnsupportedRequirementsGateResult
    labelled_case_count: int = Field(ge=0)
    observed_case_count: int = Field(ge=0)
    unsafe_supported_count: int = Field(ge=0)
    unsafe_supported_rate: UnsupportedRequirementsRate
    unsupported_safe_handling_recall: UnsupportedRequirementsRate
    supported_precision: UnsupportedRequirementsRate
    supported_recall: UnsupportedRequirementsRate
    clarification_needed_recall: UnsupportedRequirementsRate
    confusion_matrix: tuple[UnsupportedRequirementsConfusionCell, ...]
    mandatory_strata: tuple[UnsupportedRequirementsStratumResult, ...]
    not_verified_reasons: tuple[str, ...] = ()
    threshold_failures: tuple[str, ...] = ()


_MINIMUM_CLASS_COUNTS = {
    UnsupportedRequirementsClass.SUPPORTED: 10,
    UnsupportedRequirementsClass.UNSUPPORTED: 10,
    UnsupportedRequirementsClass.CLARIFICATION_NEEDED: 10,
    UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION: 5,
}
_MINIMUM_TOTAL = 40
_MINIMUM_STRATUM_COUNT = 3


def load_unsupported_requirements_dataset() -> UnsupportedRequirementsEvalDataset:
    """Load the immutable synthetic qualification split and verify its digest."""

    payload = _DATASET.read_bytes()
    digest = sha256(payload).hexdigest()
    if digest != UNSUPPORTED_REQUIREMENTS_DATASET_SHA256:
        raise ValueError("unsupported requirements dataset digest does not match")
    dataset = UnsupportedRequirementsEvalDataset.model_validate(json.loads(payload))
    if dataset.sha256 != digest:
        raise ValueError("unsupported requirements dataset identity does not match")
    return dataset


def classify_requirements_output(
    *,
    evidence_availability: RequirementsEvidenceAvailability,
    output: RequirementsExtractionOutput,
) -> UnsupportedRequirementsClass:
    """Map the frozen #682 public outcome onto the approved evaluation classes."""

    if output.outcome is RequirementsExtractionOutcome.FAILED:
        raise ValueError("failed extraction cannot receive a favorable classification")
    if output.requirements:
        needs_clarification = bool(output.clarifications) or any(
            requirement.state is RequirementState.PARTIAL
            or requirement.uncertainty is not None
            or bool(requirement.ambiguous_markers)
            or any(
                reference.relationship is RequirementEvidenceRelationship.CONTRADICTS
                for reference in requirement.evidence_refs
            )
            for requirement in output.requirements
        )
        return (
            UnsupportedRequirementsClass.CLARIFICATION_NEEDED
            if needs_clarification
            else UnsupportedRequirementsClass.SUPPORTED
        )
    if output.outcome is RequirementsExtractionOutcome.ABSTAINED:
        return (
            UnsupportedRequirementsClass.UNSUPPORTED
            if evidence_availability is RequirementsEvidenceAvailability.CURRENT
            else UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION
        )
    raise ValueError("requirements output cannot be mapped to an evaluation class")


def evaluate_unsupported_requirements(
    *,
    dataset: UnsupportedRequirementsEvalDataset,
    observations: tuple[UnsupportedRequirementsObservation, ...],
    identity: UnsupportedRequirementsEvaluationIdentity,
) -> UnsupportedRequirementsEvalReport:
    """Apply the pre-approved zero-tolerance policy without tuning on results."""

    cases_by_id = {case.case_id: case for case in dataset.cases}
    observation_ids = tuple(item.case_id for item in observations)
    duplicate_observations = len(set(observation_ids)) != len(observation_ids)
    unknown_observations = tuple(sorted(set(observation_ids).difference(cases_by_id)))
    observations_by_id = {
        observation.case_id: observation
        for observation in observations
        if observation.case_id in cases_by_id
    }
    missing_observations = tuple(
        case.case_id for case in dataset.cases if case.case_id not in observations_by_id
    )

    not_verified: list[str] = []
    if duplicate_observations:
        not_verified.append("duplicate_observation")
    if unknown_observations:
        not_verified.append("unknown_case")
    if missing_observations:
        not_verified.append("incomplete_execution")

    actual_counts = Counter(case.actual_class for case in dataset.cases)
    if len(dataset.cases) < _MINIMUM_TOTAL or any(
        actual_counts[classification] < minimum
        for classification, minimum in _MINIMUM_CLASS_COUNTS.items()
    ):
        not_verified.append("insufficient_labelled_coverage")

    stratum_results = tuple(
        UnsupportedRequirementsStratumResult(
            stratum=stratum,
            labelled_count=sum(stratum in case.strata for case in dataset.cases),
            observed_count=sum(
                stratum in cases_by_id[case_id].strata for case_id in observations_by_id
            ),
        )
        for stratum in UnsupportedRequirementsStratum
    )
    if any(item.labelled_count < _MINIMUM_STRATUM_COUNT for item in stratum_results):
        not_verified.append("missing_mandatory_stratum")
    if any(item.observed_count != item.labelled_count for item in stratum_results):
        not_verified.append("incomplete_mandatory_stratum")

    pairs = tuple(
        (cases_by_id[case_id].actual_class, observation.predicted_class)
        for case_id, observation in observations_by_id.items()
    )
    confusion = Counter(pairs)
    confusion_matrix = tuple(
        UnsupportedRequirementsConfusionCell(
            actual_class=actual,
            predicted_class=predicted,
            count=confusion[(actual, predicted)],
        )
        for actual in UnsupportedRequirementsClass
        for predicted in UnsupportedRequirementsClass
    )

    non_supported = {
        UnsupportedRequirementsClass.UNSUPPORTED,
        UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
        UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION,
    }
    unsafe_supported_count = sum(
        actual in non_supported and predicted is UnsupportedRequirementsClass.SUPPORTED
        for actual, predicted in pairs
    )
    unsafe_denominator = sum(actual in non_supported for actual, _ in pairs)
    unsupported_denominator = sum(
        actual is UnsupportedRequirementsClass.UNSUPPORTED for actual, _ in pairs
    )
    unsupported_safe = sum(
        actual is UnsupportedRequirementsClass.UNSUPPORTED
        and predicted is not UnsupportedRequirementsClass.SUPPORTED
        for actual, predicted in pairs
    )
    predicted_supported = sum(
        predicted is UnsupportedRequirementsClass.SUPPORTED for _, predicted in pairs
    )
    true_supported = confusion[
        (UnsupportedRequirementsClass.SUPPORTED, UnsupportedRequirementsClass.SUPPORTED)
    ]
    actual_supported = sum(
        actual is UnsupportedRequirementsClass.SUPPORTED for actual, _ in pairs
    )
    true_clarification = confusion[
        (
            UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
            UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
        )
    ]
    actual_clarification = sum(
        actual is UnsupportedRequirementsClass.CLARIFICATION_NEEDED
        for actual, _ in pairs
    )

    metrics = {
        "unsafe_supported_rate": _rate(unsafe_supported_count, unsafe_denominator),
        "unsupported_safe_handling_recall": _rate(
            unsupported_safe, unsupported_denominator
        ),
        "supported_precision": _rate(true_supported, predicted_supported),
        "supported_recall": _rate(true_supported, actual_supported),
        "clarification_needed_recall": _rate(true_clarification, actual_clarification),
    }
    if any(metric.value is None for metric in metrics.values()):
        not_verified.append("zero_denominator")

    threshold_failures: list[str] = []
    if not not_verified:
        if unsafe_supported_count != 0:
            threshold_failures.append("unsafe_supported_count")
        if metrics["unsafe_supported_rate"].value != Decimal("0"):
            threshold_failures.append("unsafe_supported_rate")
        if metrics["unsupported_safe_handling_recall"].value != Decimal("1"):
            threshold_failures.append("unsupported_safe_handling_recall")
        if metrics["supported_precision"].value != Decimal("1"):
            threshold_failures.append("supported_precision")
        supported_recall = metrics["supported_recall"].value
        if supported_recall is None or supported_recall < Decimal("0.8"):
            threshold_failures.append("supported_recall")
        clarification_recall = metrics["clarification_needed_recall"].value
        if clarification_recall is None or clarification_recall < Decimal("0.9"):
            threshold_failures.append("clarification_needed_recall")

    if not_verified:
        result = UnsupportedRequirementsGateResult.NOT_VERIFIED
    elif threshold_failures:
        result = UnsupportedRequirementsGateResult.FAIL
    else:
        result = UnsupportedRequirementsGateResult.PASS

    return UnsupportedRequirementsEvalReport(
        dataset_sha256=dataset.sha256,
        identity=identity,
        result=result,
        labelled_case_count=len(dataset.cases),
        observed_case_count=len(observations_by_id),
        unsafe_supported_count=unsafe_supported_count,
        unsafe_supported_rate=metrics["unsafe_supported_rate"],
        unsupported_safe_handling_recall=metrics["unsupported_safe_handling_recall"],
        supported_precision=metrics["supported_precision"],
        supported_recall=metrics["supported_recall"],
        clarification_needed_recall=metrics["clarification_needed_recall"],
        confusion_matrix=confusion_matrix,
        mandatory_strata=stratum_results,
        not_verified_reasons=tuple(dict.fromkeys(not_verified)),
        threshold_failures=tuple(threshold_failures),
    )


def _rate(numerator: int, denominator: int) -> UnsupportedRequirementsRate:
    return UnsupportedRequirementsRate(
        numerator=numerator,
        denominator=denominator,
        value=(Decimal(numerator) / Decimal(denominator) if denominator else None),
    )


__all__ = [
    "UNSUPPORTED_REQUIREMENTS_DATASET_SHA256",
    "UNSUPPORTED_REQUIREMENTS_DATASET_VERSION",
    "UNSUPPORTED_REQUIREMENTS_EVALUATOR_VERSION",
    "UNSUPPORTED_REQUIREMENTS_POLICY_VERSION",
    "UnsupportedRequirementsClass",
    "UnsupportedRequirementsEvalDataset",
    "UnsupportedRequirementsEvalReport",
    "UnsupportedRequirementsEvaluationIdentity",
    "UnsupportedRequirementsGateResult",
    "UnsupportedRequirementsObservation",
    "UnsupportedRequirementsStratum",
    "classify_requirements_output",
    "evaluate_unsupported_requirements",
    "load_unsupported_requirements_dataset",
]
