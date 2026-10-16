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
]
