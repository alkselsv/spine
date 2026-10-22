"""Trusted composition for producing threshold evidence from a production run."""

from __future__ import annotations

import hashlib
import json

from spine.application.production_runtime_readiness import TrustedProductionRuntimePreflight
from spine.evals.real_benchmark_runner import RealBenchmarkReportV3, RealBenchmarkRunner
from spine.evaluations.release_gates.thresholds import (
    RuntimeIdentityBinding,
    ThresholdEvidenceV2,
)


async def build_threshold_evidence(
    *,
    trusted_runtime: TrustedProductionRuntimePreflight,
    runner: RealBenchmarkRunner,
    report: RealBenchmarkReportV3,
    dataset_id: str,
    runtime_scope: str,
) -> ThresholdEvidenceV2:
    """Bind only a report emitted by the trusted production composition's runner."""
    if type(trusted_runtime) is not TrustedProductionRuntimePreflight:
        raise TypeError("threshold evidence requires trusted production runtime composition")
    if type(runner) is not RealBenchmarkRunner or not runner.owns_completed_report(report):
        raise ValueError("benchmark report is not the runner's accepted production result")
    runner_evidence = runner.production_qualification_evidence
    if (
        runner_evidence is None
        or not trusted_runtime.production_evidence.matches_evidence(runner_evidence)
    ):
        raise ValueError("benchmark and readiness evidence do not share a production composition")

    inspection = await trusted_runtime.inspect_runtime()
    if type(report) is not RealBenchmarkReportV3 or (
        report.structural_validation_status != "passed"
        or report.production_observation_status != "completed"
        or report.evaluation_status != "completed"
        or report.readiness_status != "ready"
        or report.baseline.baseline_status != "completed"
        or report.case_counts.observed != report.case_counts.total
        or any(
            getattr(report.case_counts, field) != 0
            for field in (
                "partial",
                "unavailable",
                "denied",
                "failed",
                "readiness_blocked",
                "mapping_unavailable",
            )
        )
    ):
        raise ValueError("benchmark report is not an accepted completed run")
    if (
        report.run.dataset_version != report.baseline.dataset_version
        or report.run.dataset_sha256 != report.baseline.dataset_sha256
    ):
        raise ValueError("benchmark run dataset digest does not match its baseline")

    profile_payload = json.dumps(
        inspection.profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    binding = RuntimeIdentityBinding(
        profile_identity=hashlib.sha256(profile_payload.encode("ascii")).hexdigest(),
        profile_version=inspection.profile.schema_version,
        deployment_fingerprint=inspection.profile.deployment_fingerprint,
        commit_sha=inspection.profile.commit_sha,
        runtime_pins=report.run.runtime_pins,
    )
    return ThresholdEvidenceV2(
        evidence_version=report.schema_version,
        run_id=report.run.run_id,
        dataset_id=dataset_id,
        dataset_version=report.run.dataset_version,
        dataset_digest=report.run.dataset_sha256,
        runtime_identity=binding.profile_identity,
        runtime_scope=runtime_scope,
        runtime_binding=binding,
        baseline=report.baseline,
    )


__all__ = ["build_threshold_evidence"]
