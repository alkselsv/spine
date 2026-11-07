"""Fail-closed release gate for unsupported extracted requirements."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field

from spine.domain.capabilities import RequirementsExecutionConfiguration
from spine.domain.common import DefinitionModel

UNSUPPORTED_REQUIREMENTS_POLICY_VERSION = "unsupported-requirements-quality-gate:v1"
UNSUPPORTED_REQUIREMENTS_EVALUATOR_VERSION = "unsupported-requirements-evaluator:v2"


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


class UnsupportedRequirementsObservation(DefinitionModel):
    item_id: str = Field(min_length=1, max_length=256)
    source_case_id: str = Field(min_length=1, max_length=128)
    actual_class: UnsupportedRequirementsClass
    predicted_class: UnsupportedRequirementsClass
    strata: tuple[UnsupportedRequirementsStratum, ...] = ()


class UnsupportedRequirementsEvaluationIdentity(DefinitionModel):
    tested_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_dataset_id: Literal["russian-meeting-transcription-synthetic"]
    source_dataset_version: Literal["1.0.0"]
    source_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_schema_version: Literal["russian-transcription-eval:v1"]
    source_requirements_metric_version: str = Field(min_length=1, max_length=128)
    source_fake_model_version: str = Field(min_length=1, max_length=128)
    source_fake_config_version: str = Field(min_length=1, max_length=128)
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


class UnsupportedRequirementsClassResult(DefinitionModel):
    actual_class: UnsupportedRequirementsClass
    labelled_count: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    recall: UnsupportedRequirementsRate
    predicted_counts: dict[UnsupportedRequirementsClass, int]


class UnsupportedRequirementsStratumResult(DefinitionModel):
    stratum: UnsupportedRequirementsStratum
    labelled_count: int = Field(ge=0)
    item_ids: tuple[str, ...]
    overlapping_item_ids: tuple[str, ...]


class UnsupportedRequirementsEvalReport(DefinitionModel):
    schema_version: Literal["unsupported-requirements-report:v2"] = (
        "unsupported-requirements-report:v2"
    )
    policy_version: Literal["unsupported-requirements-quality-gate:v1"] = (
        "unsupported-requirements-quality-gate:v1"
    )
    evaluator_version: Literal["unsupported-requirements-evaluator:v2"] = (
        "unsupported-requirements-evaluator:v2"
    )
    identity: UnsupportedRequirementsEvaluationIdentity
    result: UnsupportedRequirementsGateResult
    labelled_item_count: int = Field(ge=0)
    observed_item_count: int = Field(ge=0)
    unsafe_supported_count: int = Field(ge=0)
    unsafe_supported_rate: UnsupportedRequirementsRate
    unsupported_safe_handling_recall: UnsupportedRequirementsRate
    supported_precision: UnsupportedRequirementsRate
    supported_recall: UnsupportedRequirementsRate
    clarification_needed_recall: UnsupportedRequirementsRate
    inconclusive_abstention_recall: UnsupportedRequirementsRate
    confusion_matrix: tuple[UnsupportedRequirementsConfusionCell, ...]
    per_class_results: tuple[UnsupportedRequirementsClassResult, ...]
    mandatory_strata: tuple[UnsupportedRequirementsStratumResult, ...]
    exclusions: tuple[str, ...] = ()
    execution_failures: tuple[str, ...] = ()
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


def evaluate_unsupported_requirements(
    *,
    observations: tuple[UnsupportedRequirementsObservation, ...],
    identity: UnsupportedRequirementsEvaluationIdentity,
    exclusions: tuple[str, ...] = (),
    execution_failures: tuple[str, ...] = (),
) -> UnsupportedRequirementsEvalReport:
    """Apply the approved policy to already-labelled #674 observations."""

    not_verified: list[str] = []
    item_ids = tuple(item.item_id for item in observations)
    if len(set(item_ids)) != len(item_ids):
        not_verified.append("duplicate_observation")
    if exclusions:
        not_verified.append("excluded_items")
    if execution_failures:
        not_verified.append("incomplete_execution")

    actual_counts = Counter(item.actual_class for item in observations)
    if len(observations) < _MINIMUM_TOTAL or any(
        actual_counts[classification] < minimum
        for classification, minimum in _MINIMUM_CLASS_COUNTS.items()
    ):
        not_verified.append("insufficient_labelled_coverage")

    stratum_results = tuple(
        _stratum_result(stratum=stratum, observations=observations)
        for stratum in UnsupportedRequirementsStratum
    )
    if any(item.labelled_count < _MINIMUM_STRATUM_COUNT for item in stratum_results):
        not_verified.append("missing_mandatory_stratum")

    confusion = Counter(
        (item.actual_class, item.predicted_class) for item in observations
    )
    confusion_matrix = tuple(
        UnsupportedRequirementsConfusionCell(
            actual_class=actual,
            predicted_class=predicted,
            count=confusion[(actual, predicted)],
        )
        for actual in UnsupportedRequirementsClass
        for predicted in UnsupportedRequirementsClass
    )
    per_class = tuple(
        _class_result(
            classification=classification,
            confusion=confusion,
            actual_count=actual_counts[classification],
        )
        for classification in UnsupportedRequirementsClass
    )

    non_supported = {
        UnsupportedRequirementsClass.UNSUPPORTED,
        UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
        UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION,
    }
    unsafe_supported_count = sum(
        item.actual_class in non_supported
        and item.predicted_class is UnsupportedRequirementsClass.SUPPORTED
        for item in observations
    )
    unsafe_denominator = sum(
        item.actual_class in non_supported for item in observations
    )
    unsupported_denominator = actual_counts[UnsupportedRequirementsClass.UNSUPPORTED]
    unsupported_safe = sum(
        item.actual_class is UnsupportedRequirementsClass.UNSUPPORTED
        and item.predicted_class is not UnsupportedRequirementsClass.SUPPORTED
        for item in observations
    )
    predicted_supported = sum(
        item.predicted_class is UnsupportedRequirementsClass.SUPPORTED
        for item in observations
    )
    true_supported = confusion[
        (UnsupportedRequirementsClass.SUPPORTED, UnsupportedRequirementsClass.SUPPORTED)
    ]
    true_clarification = confusion[
        (
            UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
            UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
        )
    ]
    true_inconclusive = confusion[
        (
            UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION,
            UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION,
        )
    ]
    metrics = {
        "unsafe_supported_rate": _rate(unsafe_supported_count, unsafe_denominator),
        "unsupported_safe_handling_recall": _rate(
            unsupported_safe, unsupported_denominator
        ),
        "supported_precision": _rate(true_supported, predicted_supported),
        "supported_recall": _rate(
            true_supported, actual_counts[UnsupportedRequirementsClass.SUPPORTED]
        ),
        "clarification_needed_recall": _rate(
            true_clarification,
            actual_counts[UnsupportedRequirementsClass.CLARIFICATION_NEEDED],
        ),
        "inconclusive_abstention_recall": _rate(
            true_inconclusive,
            actual_counts[UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION],
        ),
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
        identity=identity,
        result=result,
        labelled_item_count=len(observations),
        observed_item_count=len(observations),
        unsafe_supported_count=unsafe_supported_count,
        unsafe_supported_rate=metrics["unsafe_supported_rate"],
        unsupported_safe_handling_recall=metrics["unsupported_safe_handling_recall"],
        supported_precision=metrics["supported_precision"],
        supported_recall=metrics["supported_recall"],
        clarification_needed_recall=metrics["clarification_needed_recall"],
        inconclusive_abstention_recall=metrics["inconclusive_abstention_recall"],
        confusion_matrix=confusion_matrix,
        per_class_results=per_class,
        mandatory_strata=stratum_results,
        exclusions=exclusions,
        execution_failures=execution_failures,
        not_verified_reasons=tuple(dict.fromkeys(not_verified)),
        threshold_failures=tuple(threshold_failures),
    )


def _class_result(
    *,
    classification: UnsupportedRequirementsClass,
    confusion: Counter[
        tuple[UnsupportedRequirementsClass, UnsupportedRequirementsClass]
    ],
    actual_count: int,
) -> UnsupportedRequirementsClassResult:
    correct = confusion[(classification, classification)]
    return UnsupportedRequirementsClassResult(
        actual_class=classification,
        labelled_count=actual_count,
        correct_count=correct,
        recall=_rate(correct, actual_count),
        predicted_counts={
            predicted: confusion[(classification, predicted)]
            for predicted in UnsupportedRequirementsClass
        },
    )


def _stratum_result(
    *,
    stratum: UnsupportedRequirementsStratum,
    observations: tuple[UnsupportedRequirementsObservation, ...],
) -> UnsupportedRequirementsStratumResult:
    matching = tuple(item for item in observations if stratum in item.strata)
    overlapping = tuple(item.item_id for item in matching if len(item.strata) > 1)
    return UnsupportedRequirementsStratumResult(
        stratum=stratum,
        labelled_count=len(matching),
        item_ids=tuple(item.item_id for item in matching),
        overlapping_item_ids=overlapping,
    )


def _rate(numerator: int, denominator: int) -> UnsupportedRequirementsRate:
    return UnsupportedRequirementsRate(
        numerator=numerator,
        denominator=denominator,
        value=(Decimal(numerator) / Decimal(denominator) if denominator else None),
    )


__all__ = [
    "UNSUPPORTED_REQUIREMENTS_EVALUATOR_VERSION",
    "UNSUPPORTED_REQUIREMENTS_POLICY_VERSION",
    "UnsupportedRequirementsClass",
    "UnsupportedRequirementsEvalReport",
    "UnsupportedRequirementsEvaluationIdentity",
    "UnsupportedRequirementsGateResult",
    "UnsupportedRequirementsObservation",
    "UnsupportedRequirementsStratum",
    "evaluate_unsupported_requirements",
]
