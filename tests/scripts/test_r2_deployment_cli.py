from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

from spine.domain.evaluations.artifact_attestation import ArtifactAttestation
from spine.domain.evaluations.r2_deployment_qualification import (
    R2CanaryObservation,
    R2CollectionFailureEvidence,
    R2ComponentObservation,
    R2DeploymentEvidence,
    R2DeploymentQualificationResult,
    R2LiveExecution,
    R2PrivacyDeclaration,
    R2ReadinessObservation,
    R2StartupObservation,
)
from spine.evaluations.release_gates.r2_deployment_qualification import (
    r2_composition_digest,
)
from spine.scripts.r2_deployment_qualification import main
from spine.scripts.r2_deployment_evidence import main as collect_main


HEAD_SHA = "a" * 40
DEPLOYMENT_FINGERPRINT = "b" * 64
NOW = datetime(2026, 11, 2, 16, tzinfo=UTC)


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


def _evidence() -> R2DeploymentEvidence:
    roles = (
        "api",
        "web",
        "qa_worker",
        "outbox_dispatcher",
        "postgresql",
        "temporal",
        "original_storage",
        "selected_provider",
    )
    dependencies = {
        "api": ("postgresql", "temporal", "original_storage", "selected_provider"),
        "web": ("api",),
        "qa_worker": ("postgresql", "temporal", "selected_provider"),
        "outbox_dispatcher": ("postgresql", "temporal", "selected_provider"),
        "postgresql": (),
        "temporal": ("postgresql",),
        "original_storage": (),
        "selected_provider": ("postgresql",),
    }
    evidence = R2DeploymentEvidence(
        target_commit=HEAD_SHA,
        deployment_fingerprint=DEPLOYMENT_FINGERPRINT,
        environment="development",
        captured_at=NOW,
        live_execution=R2LiveExecution(
            execution_id="issue-815-live:v1",
            principal_id="release-operator:alkselsv",
            environment="development",
            started_at=NOW - timedelta(minutes=5),
            completed_at=NOW,
        ),
        components=tuple(
            R2ComponentObservation(
                role=role,
                component_id=f"spine-{role.replace('_', '-')}",
                version="test-v1",
                artifact_digest=hashlib.sha256(role.encode("ascii")).hexdigest(),
                observation_digest=hashlib.sha256(
                    f"observation:{role}".encode("ascii")
                ).hexdigest(),
                deployment_fingerprint=DEPLOYMENT_FINGERPRINT,
                dependencies=dependencies[role],
                configured=True,
                reachable=True,
                fallback_selected=False,
            )
            for role in roles
        ),
        composition_digest="0" * 64,
        startup=R2StartupObservation(
            status="passed",
            authority_mutations=0,
            provider_calls=0,
        ),
        readiness=R2ReadinessObservation(
            status="passed",
            authenticated=True,
            scope_verified=True,
            authority_mutations=0,
            provider_calls=0,
        ),
        canary=R2CanaryObservation(
            status="passed",
            authenticated=True,
            scope_verified=True,
        ),
        privacy=R2PrivacyDeclaration(
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


def _arguments(tmp_path: Path, *, digest: str) -> list[str]:
    return [
        "--artifact",
        str(tmp_path / "evidence.json"),
        "--artifact-digest",
        digest,
        "--head-sha",
        HEAD_SHA,
        "--deployment-fingerprint",
        DEPLOYMENT_FINGERPRINT,
        "--output",
        str(tmp_path / "result.json"),
    ]


def test_cli_writes_reproducible_pass_result(tmp_path: Path) -> None:
    artifact = tmp_path / "evidence.json"
    artifact.write_text(_evidence().model_dump_json(indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    exit_code = main(
        _arguments(tmp_path, digest=digest),
        attestation_verifier=AcceptingAttestationVerifier(),
    )

    result = R2DeploymentQualificationResult.model_validate_json(
        (tmp_path / "result.json").read_bytes()
    )
    assert exit_code == 0
    assert result.status == "pass"
    assert result.artifact_digest == digest


def test_cli_writes_failed_result_and_returns_one(tmp_path: Path) -> None:
    artifact = tmp_path / "evidence.json"
    artifact.write_text(_evidence().model_dump_json(indent=2) + "\n", encoding="utf-8")

    exit_code = main(
        _arguments(tmp_path, digest="f" * 64),
        attestation_verifier=AcceptingAttestationVerifier(),
    )

    result = R2DeploymentQualificationResult.model_validate_json(
        (tmp_path / "result.json").read_bytes()
    )
    assert exit_code == 1
    assert result.status == "fail"
    assert result.reason_codes == ("artifact_digest_mismatch",)


def test_cli_refuses_missing_artifact_without_writing_result(tmp_path: Path) -> None:
    exit_code = main(
        _arguments(tmp_path, digest="f" * 64),
        attestation_verifier=AcceptingAttestationVerifier(),
    )

    assert exit_code == 2
    assert not (tmp_path / "result.json").exists()


def test_collector_cli_records_blocked_missing_runtime_configuration(
    tmp_path: Path,
) -> None:
    exit_code = collect_main(
        [
            "--compose-project",
            "issue815",
            "--runtime-env",
            str(tmp_path / ".env"),
            "--qualification-env",
            str(tmp_path / ".env.qualification"),
            "--api-base-url",
            "http://127.0.0.1:18080",
            "--web-base-url",
            "http://127.0.0.1:4175",
            "--head-sha",
            HEAD_SHA,
            "--deployment-fingerprint",
            DEPLOYMENT_FINGERPRINT,
            "--environment",
            "development",
            "--principal-id",
            "release-operator:alkselsv",
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )

    artifact = tmp_path / "evidence.json"
    failure = R2CollectionFailureEvidence.model_validate_json(
        artifact.read_bytes()
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    assert exit_code == 0
    assert failure.reason_code == "runtime_configuration_unavailable"

    verifier_exit = main(
        _arguments(tmp_path, digest=digest),
        attestation_verifier=AcceptingAttestationVerifier(),
    )
    result = R2DeploymentQualificationResult.model_validate_json(
        (tmp_path / "result.json").read_bytes()
    )
    assert verifier_exit == 1
    assert result.status == "blocked"
    assert result.reason_codes == ("runtime_configuration_unavailable",)
