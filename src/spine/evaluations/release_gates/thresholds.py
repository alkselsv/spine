"""Versioned Product Owner thresholds for the existing R1 release gate.

This module is deliberately pure: it evaluates pinned metadata and mandatory
safety outcomes, and never runs an evaluator or benchmark itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from typing import Literal

from pydantic import Field, PrivateAttr, model_validator

from spine.domain.common import DefinitionModel
from spine.evals.real_baseline import R1BaselineReport
from spine.evals.real_benchmark_runner import RuntimeVersionPins
from spine.evals.real_qa_r1 import REAL_R1_DATASET_CASE_COUNT


PolicyState = Literal["draft", "proposed", "approved"]
MetricStatus = Literal[
    "passed", "failed", "not_applicable", "insufficient_evidence", "not_approved", "unavailable"
]
GateStatus = Literal["passed", "failed", "not_approved", "unavailable"]
R1_PRODUCT_OWNER_GITHUB_ACTOR_ID = 167317615
ReasonCode = Literal[
    "mandatory_safety_failed",
    "mandatory_safety_conflict",
    "mandatory_safety_missing",
    "mandatory_safety_unavailable",
    "policy_not_approved",
    "dataset_pin_mismatch",
    "runtime_pin_mismatch",
    "population_incomplete",
    "metric_population_mismatch",
    "failed_observation",
    "partial_observation",
    "evidence_missing",
    "evaluator_pin_mismatch",
    "statistic_unavailable",
    "evidence_unavailable",
    "zero_eligible_population",
    "denominator_unavailable",
    "coverage_below_minimum",
    "threshold_met",
    "threshold_not_met",
]


class PolicyApproval(DefinitionModel):
    """Attributable Product Owner approval for one exact policy version."""

    approval_id: str = Field(min_length=1, max_length=128)
    approver_id: str = Field(min_length=1, max_length=128)
    authority: str = Field(min_length=1, max_length=64)
    approval_version: str = Field(min_length=1, max_length=128)
    approved_at: datetime
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class MetricThreshold(DefinitionModel):
    """Threshold semantics and population requirements for one metric."""

    metric: str = Field(min_length=1, max_length=128)
    evidence_kind: Literal["metric", "observer_latency", "worker_latency", "operational"]
    direction: Literal["minimum", "maximum"]
    comparison: Literal["gte", "gt", "lte", "lt"]
    statistic: Literal["weighted_mean", "median", "p95", "value"]
    denominator: Literal["measured_denominator", "metric_specific"]
    expected_eligible_count: int = Field(ge=0)
    applicability: Literal["required", "optional"]
    minimum_coverage: float = Field(ge=0, le=1)
    threshold: float | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_non_finite_numbers(cls, values: object) -> object:
        if isinstance(values, dict):
            for key in ("threshold", "minimum_coverage"):
                value = values.get(key)
                if value is not None and not math.isfinite(float(value)):
                    raise ValueError(f"{key} must be finite")
        return values

    @model_validator(mode="after")
    def validate_comparison_direction(self) -> "MetricThreshold":
        if (self.direction == "minimum") != (self.comparison in {"gte", "gt"}):
            raise ValueError("threshold comparison must agree with metric direction")
        return self


class ThresholdPolicy(DefinitionModel):
    """Immutable, versioned policy with all compatibility pins explicit."""

    policy_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    metric_evaluator_versions: tuple[tuple[str, str], ...]
    runtime_identity: str = Field(min_length=1, max_length=128)
    runtime_scope: str = Field(min_length=1, max_length=128)
    metrics: tuple[MetricThreshold, ...] = Field(min_length=1)
    state: PolicyState = "draft"
    approval: PolicyApproval | None = None

    @model_validator(mode="after")
    def validate_unique_pins(self) -> "ThresholdPolicy":
        keys = [key for key, _ in self.metric_evaluator_versions]
        if len(keys) != len(set(keys)):
            raise ValueError("metric evaluator pins must be unique")
        metric_names = [metric.metric for metric in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("policy metrics must be unique")
        if not any(metric.applicability == "required" for metric in self.metrics):
            raise ValueError("policy requires at least one required metric")
        if any(metric not in keys for metric in metric_names):
            raise ValueError("each quality metric requires a pinned evaluator version")
        return self


class RuntimeIdentityBinding(DefinitionModel):
    """Exact trusted profile and accepted-run pins used by threshold policy v2."""

    schema_version: Literal["r1-threshold-runtime-binding:v1"] = (
        "r1-threshold-runtime-binding:v1"
    )
    profile_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_version: str = Field(min_length=1, max_length=128)
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    runtime_pins: RuntimeVersionPins


class ThresholdPolicyV2(ThresholdPolicy):
    """Successor policy that binds its thresholds to one exact production runtime."""

    schema_version: Literal["r1-threshold-policy:v2"] = "r1-threshold-policy:v2"
    runtime_binding: RuntimeIdentityBinding

    @model_validator(mode="after")
    def validate_runtime_identity(self) -> "ThresholdPolicyV2":
        if self.runtime_identity != self.runtime_binding.profile_identity:
            raise ValueError("policy runtime identity must match its trusted profile binding")
        return self


class ThresholdEvidenceV2(DefinitionModel):
    """Baseline metadata bound to a trusted profile and accepted benchmark run."""

    schema_version: Literal["r1-threshold-evidence:v2"] = "r1-threshold-evidence:v2"
    evidence_version: str = Field(min_length=1, max_length=128)
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_identity: str = Field(min_length=1, max_length=128)
    runtime_scope: str = Field(min_length=1, max_length=128)
    runtime_binding: RuntimeIdentityBinding
    baseline: R1BaselineReport

    @model_validator(mode="after")
    def validate_baseline_dataset_identity(self) -> "ThresholdEvidenceV2":
        if (self.dataset_version, self.dataset_digest) != (
            self.baseline.dataset_version,
            self.baseline.dataset_sha256,
        ):
            raise ValueError("evidence dataset identity must match its baseline")
        if self.runtime_identity != self.runtime_binding.profile_identity:
            raise ValueError("evidence runtime identity must match its trusted profile binding")
        return self


_VERIFIED_APPROVAL_ISSUER = object()


class VerifiedThresholdApproval(DefinitionModel):
    """Typed approval evidence emitted by the trusted GitHub decision verifier."""

    schema_version: Literal["r1-threshold-approval:v1"] = "r1-threshold-approval:v1"
    decision_id: str = Field(min_length=1, max_length=128)
    record_reference: str = Field(pattern=r"^https://github\.com/alkselsv/spine/issues/557#issuecomment-[0-9]+$")
    actor_id: int = Field(gt=0)
    authority_reference: str = Field(pattern=r"^https://github\.com/alkselsv/spine/issues/17$")
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_binding: RuntimeIdentityBinding
    release_scope: str = Field(min_length=1, max_length=128)
    decision_status: Literal["approved"]
    decided_at: datetime
    verification_contract: Literal["github-r1-threshold-decision-verifier:v1"] = (
        "github-r1-threshold-decision-verifier:v1"
    )
    source_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    _issuer: object | None = PrivateAttr(default=None)

    def _issued_by_verifier(self) -> bool:
        return self._issuer is _VERIFIED_APPROVAL_ISSUER


def _issue_verified_threshold_approval(**values: object) -> VerifiedThresholdApproval:
    """Mark approval evidence issued after the trusted GitHub verification."""
    approval = VerifiedThresholdApproval(**values)
    approval._issuer = _VERIFIED_APPROVAL_ISSUER
    return approval


class ThresholdEvidence(DefinitionModel):
    """Metadata-only baseline plus runtime and dataset identity."""

    evidence_version: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_identity: str = Field(min_length=1, max_length=128)
    runtime_scope: str = Field(min_length=1, max_length=128)
    baseline: R1BaselineReport

    @model_validator(mode="after")
    def validate_baseline_dataset_identity(self) -> "ThresholdEvidence":
        if (self.dataset_version, self.dataset_digest) != (
            self.baseline.dataset_version,
            self.baseline.dataset_sha256,
        ):
            raise ValueError("evidence dataset identity must match its baseline")
        return self


class MandatorySafetyResult(DefinitionModel):
    gate_id: str = Field(min_length=1, max_length=128)
    status: Literal["passed", "failed", "unavailable"]


class MetricGateResult(DefinitionModel):
    metric: str
    status: MetricStatus
    reason_code: ReasonCode
    evidence_version: str | None = None
    evaluator_version: str | None = None
    policy_version: str
    applied_threshold: float | None = None
    direction: Literal["minimum", "maximum"] | None = None
    comparison: Literal["gte", "gt", "lte", "lt"] | None = None
    statistic: Literal["weighted_mean", "median", "p95", "value"] | None = None
    denominator_kind: Literal["eligible_count", "measured_denominator", "metric_specific"] | None = None
    denominator: int | None = None
    eligible_count: int | None = None
    coverage: float | None = None
    observed_value: float | None = None


class ThresholdGateResult(DefinitionModel):
    schema_version: Literal["r1-approved-threshold-gate:v2"] = "r1-approved-threshold-gate:v2"
    status: GateStatus
    policy_id: str
    policy_version: str
    policy_digest: str
    evidence_version: str | None = None
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    runtime_identity: str
    runtime_scope: str
    runtime_binding: RuntimeIdentityBinding | None = None
    approval_decision_id: str | None = None
    approval_reference: str | None = None
    approval_actor_id: int | None = None
    approval_source_snapshot_digest: str | None = None
    metrics: tuple[MetricGateResult, ...]
    reason_codes: tuple[ReasonCode, ...] = ()


@dataclass(frozen=True, slots=True)
class _MetricObservation:
    value: float | None
    denominator: int
    eligible_count: int
    coverage: float | None
    metric_version: str | None
    unavailable_code: ReasonCode | None


def policy_digest(policy: ThresholdPolicy) -> str:
    """Hash immutable policy semantics, excluding its activation metadata."""
    values = policy.model_dump(mode="json", exclude={"state", "approval"})
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _metric_observation(
    policy: ThresholdPolicy, policy_metric: MetricThreshold, evidence: ThresholdEvidence
) -> _MetricObservation:
    baseline = evidence.baseline
    if policy_metric.evidence_kind == "metric":
        aggregate = next((item for item in baseline.metrics if item.metric == policy_metric.metric), None)
        if aggregate is None:
            return _MetricObservation(None, 0, 0, None, None, "evidence_missing")
        expected_version = dict(policy.metric_evaluator_versions).get(policy_metric.metric)
        if aggregate.version != expected_version:
            return _MetricObservation(None, aggregate.measured_denominator, aggregate.eligible_count, None, aggregate.version, "evaluator_pin_mismatch")
        if aggregate.applicability == "not_applicable":
            return _MetricObservation(None, 0, 0, None, aggregate.version, None)
        value = aggregate.value if policy_metric.statistic == "weighted_mean" else None
        if value is None:
            return _MetricObservation(None, aggregate.measured_denominator, aggregate.eligible_count, None, aggregate.version, "statistic_unavailable")
        coverage = (aggregate.eligible_count - aggregate.unavailable_count) / aggregate.eligible_count if aggregate.eligible_count else None
        return _MetricObservation(value, aggregate.measured_denominator, aggregate.eligible_count, coverage, aggregate.version, None)
    if policy_metric.evidence_kind in {"observer_latency", "worker_latency"}:
        aggregate = baseline.observer_latency if policy_metric.evidence_kind == "observer_latency" else baseline.worker_latency
        expected_version = dict(policy.metric_evaluator_versions).get(policy_metric.metric)
        if aggregate.version != expected_version:
            return _MetricObservation(None, aggregate.measured_denominator, aggregate.measured_denominator, None, aggregate.version, "evaluator_pin_mismatch")
        value = aggregate.median if policy_metric.statistic == "median" else aggregate.p95 if policy_metric.statistic == "p95" else None
        coverage = 1.0 if aggregate.coverage_state == "complete" else None
        return _MetricObservation(value, aggregate.measured_denominator, aggregate.measured_denominator, coverage, aggregate.version, None if value is not None else "evidence_unavailable")
    operational = next((item for item in baseline.operational_measurements if item.name == policy_metric.metric), None)
    if operational is None or operational.status != "measured" or operational.value is None:
        return _MetricObservation(
            None,
            0 if operational is None or operational.measured_denominator is None else operational.measured_denominator,
            0 if operational is None or operational.eligible_count is None else operational.eligible_count,
            None,
            None,
            "evidence_unavailable",
        )
    if dict(policy.metric_evaluator_versions).get(policy_metric.metric) != baseline.metric_version:
        return _MetricObservation(None, operational.measured_denominator or 0, operational.eligible_count or 0, None, baseline.metric_version, "evaluator_pin_mismatch")
    coverage = (
        (operational.eligible_count - operational.unavailable_count) / operational.eligible_count
        if operational.eligible_count and operational.unavailable_count is not None
        else 1.0 if operational.coverage_state == "complete" else None
    )
    return _MetricObservation(
        operational.value,
        operational.measured_denominator or 0,
        operational.eligible_count or 0,
        coverage,
        baseline.metric_version,
        None,
    )


def evaluate_threshold_policy(
    *,
    policy: ThresholdPolicy | ThresholdPolicyV2,
    evidence: ThresholdEvidence | ThresholdEvidenceV2,
    safety_results: tuple[MandatorySafetyResult, ...],
    required_safety_gates: tuple[str, ...],
    verified_approval: VerifiedThresholdApproval | None = None,
) -> ThresholdGateResult:
    """Evaluate hard safety first, then compatible approved thresholds."""
    result_base = {
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "policy_digest": policy_digest(policy),
        "evidence_version": evidence.evidence_version,
        "dataset_id": evidence.dataset_id,
        "dataset_version": evidence.dataset_version,
        "dataset_digest": evidence.dataset_digest,
        "runtime_identity": evidence.runtime_identity,
        "runtime_scope": evidence.runtime_scope,
    }
    if type(policy) is ThresholdPolicyV2 and type(evidence) is ThresholdEvidenceV2:
        result_base["runtime_binding"] = evidence.runtime_binding
    safety = {item.gate_id: item.status for item in safety_results}
    duplicate_safety_ids = len(safety) != len(safety_results)
    safety_missing = not required_safety_gates or any(gate not in safety for gate in required_safety_gates)
    safety_failed = any(item.status == "failed" for item in safety_results)
    safety_unavailable = any(item.status == "unavailable" for item in safety_results)
    if safety_missing or safety_failed or safety_unavailable or duplicate_safety_ids:
        code = "mandatory_safety_failed" if safety_failed else "mandatory_safety_conflict" if duplicate_safety_ids else "mandatory_safety_missing" if safety_missing else "mandatory_safety_unavailable"
        status: GateStatus = "failed" if safety_failed else "unavailable"
        results = tuple(MetricGateResult(metric=item.metric, status="unavailable", reason_code=code, policy_version=policy.version) for item in policy.metrics)
        return ThresholdGateResult(**result_base, status=status, metrics=results, reason_codes=(code,))

    approval_matches = (
        type(policy) is ThresholdPolicyV2
        and type(evidence) is ThresholdEvidenceV2
        and type(verified_approval) is VerifiedThresholdApproval
        and verified_approval._issued_by_verifier()
        and verified_approval.decision_status == "approved"
        and verified_approval.actor_id == R1_PRODUCT_OWNER_GITHUB_ACTOR_ID
        and verified_approval.authority_reference
        == "https://github.com/alkselsv/spine/issues/17"
        and verified_approval.record_reference.startswith(
            "https://github.com/alkselsv/spine/issues/557#issuecomment-"
        )
        and policy.state == "approved"
        and verified_approval.policy_id == policy.policy_id
        and verified_approval.policy_version == policy.version
        and verified_approval.policy_digest == policy_digest(policy)
        and verified_approval.dataset_id == policy.dataset_id == evidence.dataset_id
        and verified_approval.dataset_version == policy.dataset_version == evidence.dataset_version
        and verified_approval.dataset_digest == policy.dataset_digest == evidence.dataset_digest
        and verified_approval.runtime_binding == policy.runtime_binding == evidence.runtime_binding
        and verified_approval.release_scope == policy.runtime_scope
        and verified_approval.decided_at.tzinfo is not None
        and all(metric.threshold is not None for metric in policy.metrics)
    )
    if not approval_matches:
        results = tuple(MetricGateResult(metric=item.metric, status="not_approved", reason_code="policy_not_approved", policy_version=policy.version, applied_threshold=None, comparison=None, statistic=None, denominator_kind=None) for item in policy.metrics)
        return ThresholdGateResult(**result_base, status="not_approved", metrics=results, reason_codes=("policy_not_approved",))

    result_base.update(
        {
            "approval_decision_id": verified_approval.decision_id,
            "approval_reference": verified_approval.record_reference,
            "approval_actor_id": verified_approval.actor_id,
            "approval_source_snapshot_digest": verified_approval.source_snapshot_digest,
        }
    )

    incompatibilities = []
    if (evidence.dataset_id, evidence.dataset_version, evidence.dataset_digest) != (policy.dataset_id, policy.dataset_version, policy.dataset_digest):
        incompatibilities.append("dataset_pin_mismatch")
    if (evidence.runtime_identity, evidence.runtime_scope) != (policy.runtime_identity, policy.runtime_scope):
        incompatibilities.append("runtime_pin_mismatch")
    if (
        type(policy) is not ThresholdPolicyV2
        or type(evidence) is not ThresholdEvidenceV2
        or policy.runtime_binding != evidence.runtime_binding
        or policy.runtime_binding.runtime_pins != evidence.runtime_binding.runtime_pins
    ):
        incompatibilities.append("runtime_pin_mismatch")
    if (
        evidence.baseline.baseline_status != "completed"
        or evidence.baseline.eligible_case_count != REAL_R1_DATASET_CASE_COUNT
    ):
        incompatibilities.append("population_incomplete")
    if evidence.baseline.failure_clusters:
        incompatibilities.append("failed_observation")
    if incompatibilities:
        results = tuple(
            MetricGateResult(
                metric=item.metric,
                status="unavailable",
                reason_code=incompatibilities[0],
                policy_version=policy.version,
                applied_threshold=item.threshold,
                direction=item.direction,
                comparison=item.comparison,
                statistic=item.statistic,
                denominator_kind=item.denominator,
            )
            for item in policy.metrics
        )
        return ThresholdGateResult(**result_base, status="unavailable", metrics=results, reason_codes=tuple(incompatibilities))

    results: list[MetricGateResult] = []
    for item in policy.metrics:
        observation = _metric_observation(policy, item, evidence)
        value = observation.value
        denominator = observation.denominator
        eligible = observation.eligible_count
        coverage = observation.coverage
        reported_denominator = observation.denominator
        if eligible != item.expected_eligible_count:
            metric_status: MetricStatus = "unavailable"
            reason_code: ReasonCode = "metric_population_mismatch"
        elif observation.unavailable_code is not None:
            metric_status: MetricStatus = "unavailable"
            reason_code = observation.unavailable_code
        elif eligible == 0:
            metric_status = "not_applicable"
            reason_code = "zero_eligible_population"
        elif value is None or denominator == 0:
            metric_status = "insufficient_evidence"
            reason_code = "denominator_unavailable"
        elif coverage is None or coverage < item.minimum_coverage:
            metric_status = "insufficient_evidence"
            reason_code = "coverage_below_minimum"
        elif coverage < 1:
            metric_status = "insufficient_evidence"
            reason_code = "partial_observation"
        elif item.comparison == "gte":
            passed = value >= item.threshold
            metric_status, reason_code = ("passed", "threshold_met") if passed else ("failed", "threshold_not_met")
        elif item.comparison == "gt":
            passed = value > item.threshold
            metric_status, reason_code = ("passed", "threshold_met") if passed else ("failed", "threshold_not_met")
        elif item.comparison == "lte":
            passed = value <= item.threshold
            metric_status, reason_code = ("passed", "threshold_met") if passed else ("failed", "threshold_not_met")
        else:
            passed = value < item.threshold
            metric_status, reason_code = ("passed", "threshold_met") if passed else ("failed", "threshold_not_met")
        results.append(
            MetricGateResult(
                metric=item.metric,
                status=metric_status,
                    reason_code=reason_code,
                evidence_version=evidence.evidence_version,
                    evaluator_version=observation.metric_version,
                policy_version=policy.version,
                applied_threshold=item.threshold,
                direction=item.direction,
                comparison=item.comparison,
                statistic=item.statistic,
                denominator_kind=item.denominator,
                denominator=reported_denominator,
                eligible_count=eligible,
                coverage=coverage,
                observed_value=value,
            )
        )
    required_results = tuple(
        result
        for definition, result in zip(policy.metrics, results, strict=True)
        if definition.applicability == "required"
    )
    required_statuses = {item.status for item in required_results}
    overall: GateStatus = (
        "failed"
        if "failed" in required_statuses
        else "unavailable"
        if required_statuses != {"passed"}
        else "passed"
    )
    reasons = tuple(
        sorted({item.reason_code for item in required_results if item.status != "passed"})
    )
    return ThresholdGateResult(**result_base, status=overall, metrics=tuple(results), reason_codes=reasons)


__all__ = [
    "MandatorySafetyResult", "MetricGateResult", "MetricThreshold", "PolicyApproval",
    "RuntimeIdentityBinding", "ThresholdEvidence", "ThresholdEvidenceV2",
    "ThresholdGateResult", "ThresholdPolicy", "ThresholdPolicyV2",
    "VerifiedThresholdApproval", "R1_PRODUCT_OWNER_GITHUB_ACTOR_ID",
    "evaluate_threshold_policy", "policy_digest",
]
