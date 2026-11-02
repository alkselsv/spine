from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

from spine.domain.evaluations.r2_deployment_qualification import (
    R2CanaryObservation,
    R2CollectionFailureEvidence,
    R2ComponentObservation,
    R2DeploymentEvidence,
    R2LiveExecution,
    R2PrivacyDeclaration,
    R2ReadinessObservation,
    R2StartupObservation,
)
from spine.domain.evaluations.artifact_attestation import ArtifactAttestation
from spine.evaluations.release_gates.artifact_attestation import (
    ArtifactAttestationVerificationError,
)
from spine.evaluations.release_gates.r2_deployment_qualification import (
    qualify_r2_deployment_artifact,
    r2_composition_digest,
)


HEAD_SHA = "a" * 40
DEPLOYMENT_FINGERPRINT = "b" * 64
NOW = datetime(2026, 11, 2, 16, tzinfo=UTC)
REQUIRED_COMPONENTS = (
    "api",
    "web",
    "qa_worker",
    "outbox_dispatcher",
    "postgresql",
    "temporal",
    "original_storage",
    "selected_provider",
)
DEPENDENCIES = {
    "api": ("postgresql", "temporal", "original_storage", "selected_provider"),
    "web": ("api",),
    "qa_worker": ("postgresql", "temporal", "selected_provider"),
    "outbox_dispatcher": ("postgresql", "temporal", "selected_provider"),
    "postgresql": (),
    "temporal": ("postgresql",),
    "original_storage": (),
    "selected_provider": ("postgresql",),
}


class AcceptingAttestationVerifier:
    def verify(
        self,
        *,
        artifact_path: Path,
        artifact_digest: str,
        expected_head_sha: str,
    ) -> ArtifactAttestation:
        del artifact_path
        return ArtifactAttestation(
            artifact_digest=artifact_digest,
            head_sha=expected_head_sha,
            repository="alkselsv/spine",
            workflow_ref="alkselsv/spine/.github/workflows/test.yml@refs/heads/main",
            run_invocation_uri=(
                "https://github.com/alkselsv/spine/actions/runs/41466361716/attempts/1"
            ),
        )


class RejectingAttestationVerifier:
    def verify(
        self,
        *,
        artifact_path: Path,
        artifact_digest: str,
        expected_head_sha: str,
    ) -> ArtifactAttestation:
        del artifact_path, artifact_digest, expected_head_sha
        raise ArtifactAttestationVerificationError("untrusted")


def _component(role: str) -> R2ComponentObservation:
    return R2ComponentObservation(
        role=role,
        component_id=f"spine-{role.replace('_', '-')}",
        version="test-v1",
        artifact_digest=hashlib.sha256(role.encode("ascii")).hexdigest(),
        observation_digest=hashlib.sha256(
            f"observation:{role}".encode("ascii")
        ).hexdigest(),
        deployment_fingerprint=DEPLOYMENT_FINGERPRINT,
        dependencies=DEPENDENCIES[role],
        configured=True,
        reachable=True,
        fallback_selected=False,
    )


def _evidence(
    *,
    live_execution: R2LiveExecution | None = None,
    components: tuple[R2ComponentObservation, ...] | None = None,
    readiness: R2ReadinessObservation | None = None,
    canary: R2CanaryObservation | None = None,
    privacy: R2PrivacyDeclaration | None = None,
) -> R2DeploymentEvidence:
    evidence = R2DeploymentEvidence(
        target_commit=HEAD_SHA,
        deployment_fingerprint=DEPLOYMENT_FINGERPRINT,
        environment="development",
        captured_at=NOW,
        live_execution=live_execution
        or R2LiveExecution(
            execution_id="issue-815-live:v1",
            principal_id="release-operator:alkselsv",
            environment="development",
            started_at=NOW - timedelta(minutes=5),
            completed_at=NOW,
        ),
        components=components
        if components is not None
        else tuple(_component(role) for role in REQUIRED_COMPONENTS),
        composition_digest="0" * 64,
        startup=R2StartupObservation(
            status="passed",
            authority_mutations=0,
            provider_calls=0,
        ),
        readiness=readiness
        or R2ReadinessObservation(
            status="passed",
            authenticated=True,
            scope_verified=True,
            authority_mutations=0,
            provider_calls=0,
        ),
        canary=canary
        or R2CanaryObservation(
            status="passed",
            authenticated=True,
            scope_verified=True,
        ),
        privacy=privacy
        or R2PrivacyDeclaration(
            metadata_only=True,
            credentials_recorded=False,
            network_locations_recorded=False,
            protected_content_recorded=False,
            provider_payloads_recorded=False,
            raw_errors_recorded=False,
        ),
    )
    return R2DeploymentEvidence.model_validate(
        {**evidence.model_dump(), "composition_digest": r2_composition_digest(evidence)}
    )


def _write_artifact(
    tmp_path: Path,
    evidence: R2DeploymentEvidence | R2CollectionFailureEvidence,
) -> tuple[Path, str]:
    artifact = tmp_path / "r2-deployment-evidence.json"
    artifact.write_text(evidence.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return artifact, hashlib.sha256(artifact.read_bytes()).hexdigest()


def _qualify(
    tmp_path: Path,
    evidence: R2DeploymentEvidence | R2CollectionFailureEvidence,
    *,
    expected_digest: str | None = None,
    expected_head_sha: str = HEAD_SHA,
    expected_deployment_fingerprint: str = DEPLOYMENT_FINGERPRINT,
    attestation_verifier=None,
):
    artifact, observed_digest = _write_artifact(tmp_path, evidence)
    return qualify_r2_deployment_artifact(
        artifact_path=artifact,
        expected_digest=expected_digest or observed_digest,
        expected_head_sha=expected_head_sha,
        expected_deployment_fingerprint=expected_deployment_fingerprint,
        attestation_verifier=(
            attestation_verifier or AcceptingAttestationVerifier()
        ),
    )


def test_complete_live_deployment_observation_passes(tmp_path: Path) -> None:
    result = _qualify(tmp_path, _evidence())

    assert result.status == "pass"
    assert result.reason_codes == ()
    assert result.head_sha == HEAD_SHA
    assert result.deployment_fingerprint == DEPLOYMENT_FINGERPRINT


def test_trusted_observed_startup_failure_fails(tmp_path: Path) -> None:
    evidence = R2CollectionFailureEvidence(
        target_commit=HEAD_SHA,
        deployment_fingerprint=DEPLOYMENT_FINGERPRINT,
        environment="development",
        captured_at=NOW,
        live_execution=R2LiveExecution(
            execution_id="issue-815-live:failed-startup",
            principal_id="release-operator:alkselsv",
            environment="development",
            started_at=NOW - timedelta(minutes=1),
            completed_at=NOW,
        ),
        reason_code="deployment_start_failed",
        privacy=R2PrivacyDeclaration(
            metadata_only=True,
            credentials_recorded=False,
            network_locations_recorded=False,
            protected_content_recorded=False,
            provider_payloads_recorded=False,
            raw_errors_recorded=False,
        ),
    )

    result = _qualify(tmp_path, evidence)

    assert result.status == "fail"
    assert result.reason_codes == ("deployment_start_failed",)


def test_missing_required_component_blocks_qualification(tmp_path: Path) -> None:
    result = _qualify(
        tmp_path,
        _evidence(
            components=tuple(
                _component(role)
                for role in REQUIRED_COMPONENTS
                if role != "temporal"
            )
        ),
    )

    assert result.status == "blocked"
    assert result.reason_codes == ("required_component_missing",)


def test_component_bound_to_another_deployment_fails(tmp_path: Path) -> None:
    components = tuple(
        R2ComponentObservation.model_validate(
            {
                **_component(role).model_dump(),
                "deployment_fingerprint": "c" * 64,
            }
        )
        if role == "api"
        else _component(role)
        for role in REQUIRED_COMPONENTS
    )

    result = _qualify(tmp_path, _evidence(components=components))

    assert result.status == "fail"
    assert result.reason_codes == ("component_binding_mismatch",)


def test_unlinked_observation_is_not_verified(tmp_path: Path) -> None:
    evidence = R2DeploymentEvidence.model_validate(
        {**_evidence().model_dump(), "live_execution": None}
    )

    result = _qualify(tmp_path, evidence)

    assert result.status == "not_verified"
    assert result.reason_codes == ("live_report_unlinked",)


def test_untrusted_artifact_attestation_is_not_verified(tmp_path: Path) -> None:
    result = _qualify(
        tmp_path,
        _evidence(),
        attestation_verifier=RejectingAttestationVerifier(),
    )

    assert result.status == "not_verified"
    assert result.reason_codes == ("artifact_attestation_untrusted",)


def test_failed_readiness_is_fail_not_blocked(tmp_path: Path) -> None:
    result = _qualify(
        tmp_path,
        _evidence(
            readiness=R2ReadinessObservation(
                status="failed",
                authenticated=True,
                scope_verified=True,
                authority_mutations=0,
                provider_calls=0,
            )
        ),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("readiness_failed",)


def test_unavailable_readiness_blocks_qualification(tmp_path: Path) -> None:
    result = _qualify(
        tmp_path,
        _evidence(
            readiness=R2ReadinessObservation(
                status="unavailable",
                authenticated=False,
                scope_verified=False,
                authority_mutations=0,
                provider_calls=0,
            )
        ),
    )

    assert result.status == "blocked"
    assert result.reason_codes == ("readiness_unavailable",)


def test_readiness_provider_call_fails_qualification(tmp_path: Path) -> None:
    result = _qualify(
        tmp_path,
        _evidence(
            readiness=R2ReadinessObservation(
                status="passed",
                authenticated=True,
                scope_verified=True,
                authority_mutations=0,
                provider_calls=1,
            )
        ),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("readiness_provider_call_observed",)


def test_startup_provider_call_fails_qualification(tmp_path: Path) -> None:
    evidence = R2DeploymentEvidence.model_validate(
        {
            **_evidence().model_dump(),
            "startup": {
                "status": "passed",
                "authority_mutations": 0,
                "provider_calls": 1,
            },
        }
    )

    result = _qualify(tmp_path, evidence)

    assert result.status == "fail"
    assert result.reason_codes == ("startup_provider_call_observed",)


def test_unavailable_startup_observation_blocks_qualification(
    tmp_path: Path,
) -> None:
    evidence = R2DeploymentEvidence.model_validate(
        {
            **_evidence().model_dump(),
            "startup": {
                "status": "unavailable",
                "authority_mutations": 0,
                "provider_calls": 0,
            },
        }
    )

    result = _qualify(tmp_path, evidence)

    assert result.status == "blocked"
    assert result.reason_codes == ("startup_observation_unavailable",)


def test_changed_component_graph_fails_qualification(tmp_path: Path) -> None:
    api = _component("api")
    changed_api = R2ComponentObservation.model_validate(
        {**api.model_dump(), "dependencies": ("postgresql",)}
    )
    evidence = _evidence(
        components=(
            changed_api,
            *(_component(role) for role in REQUIRED_COMPONENTS if role != "api"),
        )
    )

    result = _qualify(tmp_path, evidence)

    assert result.status == "fail"
    assert result.reason_codes == ("component_composition_mismatch",)


def test_privacy_violation_fails_qualification(tmp_path: Path) -> None:
    result = _qualify(
        tmp_path,
        _evidence(
            privacy=R2PrivacyDeclaration(
                metadata_only=False,
                credentials_recorded=True,
                network_locations_recorded=False,
                protected_content_recorded=False,
                provider_payloads_recorded=False,
                raw_errors_recorded=False,
            )
        ),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("unsafe_evidence_artifact",)


def test_artifact_digest_mismatch_fails_qualification(tmp_path: Path) -> None:
    result = _qualify(tmp_path, _evidence(), expected_digest="d" * 64)

    assert result.status == "fail"
    assert result.reason_codes == ("artifact_digest_mismatch",)


def test_exact_target_commit_and_deployment_are_required(tmp_path: Path) -> None:
    result = _qualify(
        tmp_path,
        _evidence(),
        expected_head_sha="e" * 40,
        expected_deployment_fingerprint="f" * 64,
    )

    assert result.status == "fail"
    assert result.reason_codes == (
        "target_commit_mismatch",
        "deployment_fingerprint_mismatch",
    )
