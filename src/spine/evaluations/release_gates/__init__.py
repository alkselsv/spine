"""Quality, compatibility, security, and cost deployment gates."""

from spine.evaluations.release_gates.applicability import (
    ApplicabilityFailure,
    ApplicabilityManifest,
    ApplicabilityReport,
    DEFAULT_R1_APPLICABILITY_MANIFEST,
    ExecutionEvidence,
    build_r1_applicability_report,
    classify_changed_files,
    run_r1_applicability_gate,
    validate_applicability_manifest,
)
from spine.evaluations.release_gates.thresholds import (
    MandatorySafetyResult,
    MetricGateResult,
    MetricThreshold,
    PolicyApproval,
    ThresholdEvidence,
    ThresholdGateResult,
    ThresholdPolicy,
    evaluate_threshold_policy,
    policy_digest,
)

__all__ = [
    "ApplicabilityFailure",
    "ApplicabilityManifest",
    "ApplicabilityReport",
    "DEFAULT_R1_APPLICABILITY_MANIFEST",
    "ExecutionEvidence",
    "build_r1_applicability_report",
    "classify_changed_files",
    "run_r1_applicability_gate",
    "validate_applicability_manifest",
    "MandatorySafetyResult",
    "MetricGateResult",
    "MetricThreshold",
    "PolicyApproval",
    "ThresholdEvidence",
    "ThresholdGateResult",
    "ThresholdPolicy",
    "evaluate_threshold_policy",
    "policy_digest",
]
