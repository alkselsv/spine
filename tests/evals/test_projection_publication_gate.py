from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from spine.evaluations.release_gates.projection_publication import (
    ProjectionLifecycleObservation,
    ProviderCompatibilityEvidence,
    ProjectionPublicationQualificationReport,
    ProjectionPublicationTarget,
    evaluate_projection_publication_gate,
    required_projection_publication_scenarios,
)


def _target() -> ProjectionPublicationTarget:
    return ProjectionPublicationTarget(
        commit_sha="9d75b1e0e8531ed3534806ab8fc5071901157ece",
        deployment_fingerprint=(
            "47be5f0ac4e031de25a6b864ed9874a6a5a5acbef9653dd54fc7892cbf0ea823"
        ),
        workspace_id="54400000-0000-4000-8000-00000000000a",
        environment_id="54400000-0000-4000-8000-00000000000e",
        authorization_generation=16,
        provider_profile_id="cognee-approved-production",
        provider_profile_digest=(
            "8935297e3def5e786f7e3d34db87d13c2106fbb7f82f8d695c747cee4fe1c84f"
        ),
    )


def _compatibility(
    target: ProjectionPublicationTarget,
    *,
    source_run_id: str = "issue562-physical-001",
    evidence_kind: str = "fresh_physical_run",
) -> ProviderCompatibilityEvidence:
    return ProviderCompatibilityEvidence(
        source_run_id=source_run_id,
        evidence_kind=evidence_kind,
        status="compatible",
        target=target,
        approval_reference="issue-562-operator-approval",
        run_authorization_reference="issue-562-run-authorization",
        physical_report_sha256="b" * 64,
        passed_physical_gate_ids=(
            "public_sdk",
            "structured_round_trip",
            "unsafe_evidence",
            "workspace_isolation",
            "namespace_isolation",
            "explicit_retrieval_context",
            "update_remove_tombstone",
            "rebuild_recovery_validation_diagnostics",
            "concurrency_failure_cleanup",
            "canonical_lifecycle_separation",
        ),
        production_approval=True,
    )


def test_missing_provider_compatibility_cannot_qualify_projection_publication() -> None:
    target = _target()

    result = evaluate_projection_publication_gate(
        target=target,
        q16_01_target=target,
        r4_target=target,
        provider_compatibility=None,
        observations=(),
    )

    assert result.status == "not_verified"
    assert result.reason_codes == ("provider_compatibility_missing",)
    assert result.target == target


def test_provider_evidence_for_another_target_is_not_compatible() -> None:
    target = _target()
    other_target = target.model_copy(
        update={"commit_sha": "a" * 40},
    )
    compatibility = _compatibility(
        other_target,
        source_run_id="issue507-composed-002",
        evidence_kind="existing_run_compatibility",
    )

    result = evaluate_projection_publication_gate(
        target=target,
        q16_01_target=target,
        r4_target=target,
        provider_compatibility=compatibility,
        observations=(),
    )

    assert result.status == "not_verified"
    assert result.reason_codes == ("provider_compatibility_incompatible",)


def test_exact_target_with_every_live_lifecycle_observation_passes() -> None:
    target = _target()
    compatibility = _compatibility(target)
    observations = tuple(
        ProjectionLifecycleObservation(
            scenario_id=scenario_id,
            status="passed",
            evidence_references=(f"evidence-{index}",),
        )
        for index, scenario_id in enumerate(
            required_projection_publication_scenarios(), start=1
        )
    )

    result = evaluate_projection_publication_gate(
        target=target,
        q16_01_target=target,
        r4_target=target,
        provider_compatibility=compatibility,
        observations=observations,
    )

    assert result.status == "pass"
    assert result.reason_codes == ()


def test_confirmed_lifecycle_failure_takes_precedence_over_missing_evidence() -> None:
    target = _target()
    compatibility = _compatibility(target)

    result = evaluate_projection_publication_gate(
        target=target,
        q16_01_target=target,
        r4_target=target,
        provider_compatibility=compatibility,
        observations=(
            ProjectionLifecycleObservation(
                scenario_id="activation_generation_cas",
                status="failed",
                evidence_references=("failed-cas-observation",),
            ),
        ),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("lifecycle_evidence_failed",)


def test_report_cannot_claim_pass_without_provider_compatibility() -> None:
    target = _target()

    with pytest.raises(ValidationError, match="decision does not match evidence"):
        ProjectionPublicationQualificationReport(
            ticket=562,
            observed_at=datetime(2026, 10, 24, 12, tzinfo=timezone.utc),
            target=target,
            q16_01_target=target,
            r4_target=target,
            provider_compatibility=None,
            observations=(),
            status="pass",
            reason_codes=(),
        )


def test_compatible_provider_evidence_requires_a_pinned_complete_physical_run() -> None:
    with pytest.raises(ValidationError, match="complete pinned physical run"):
        ProviderCompatibilityEvidence(
            source_run_id="issue507-composed-002",
            evidence_kind="existing_run_compatibility",
            status="compatible",
            target=_target(),
            approval_reference="issue-562-operator-approval",
        )
