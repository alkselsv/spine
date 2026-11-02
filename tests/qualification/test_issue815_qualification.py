from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-815-r2-preflight-v1.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r1-r2-deployment-qualification.md"
WORKFLOW_PATH = ROOT / ".github/workflows/r2-deployment-qualification.yml"


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_issue815_preflight_preserves_not_verified_release_boundary() -> None:
    report = _report()

    assert report["schema_version"] == "issue-815-r2-preflight:v1"
    assert report["issue"] == 815
    assert report["criterion"] == "R2"
    assert report["criterion_status"] == "NOT_VERIFIED"
    assert report["r17_status"] == "FAIL"
    assert report["release_decision"] == "NO-GO"
    assert report["reason_codes"] == [
        "live_report_unlinked",
        "approved_runtime_configuration_unavailable",
        "approved_provider_authorization_unavailable",
        "attested_live_artifact_not_produced",
    ]


def test_issue815_preflight_records_only_safe_capability_observations() -> None:
    report = _report()

    assert report["implementation_base_commit"] == (
        "2d7c447b9602562752e5797b7d569b7f78135bf6"
    )
    assert report["local_capabilities"] == {
        "docker_daemon": "AVAILABLE",
        "docker_version": "29.6.1",
        "runtime_env": "UNAVAILABLE",
        "qualification_env": "UNAVAILABLE",
        "provider_authorization": "UNAVAILABLE",
        "signed_live_artifact": "NOT_PRODUCED",
    }
    assert report["privacy"] == {
        "metadata_only": True,
        "credentials_recorded": False,
        "network_locations_recorded": False,
        "protected_content_recorded": False,
        "provider_payloads_recorded": False,
        "raw_errors_recorded": False,
    }


def test_issue815_runbook_requires_attestation_and_never_self_declared_pass() -> None:
    text = RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "spine-r2-deployment-verify" in text
    assert "spine-r2-deployment-evidence" in text
    assert "gh attestation verify" in text
    assert "R2 PASS" in text
    assert "self-declared" in text


def test_issue815_main_workflow_collects_and_attests_exact_live_artifact() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "if: github.ref == 'refs/heads/main'" in text
    assert "environment: r2-qualification" in text
    assert "secrets.SPINE_R2_RUNTIME_ENV" in text
    assert "secrets.SPINE_R2_QUALIFICATION_ENV" in text
    assert "spine-r2-deployment-evidence" in text
    assert "uses: actions/attest@v4" in text
    assert "subject-path: r2-deployment-evidence.json" in text
    assert "spine-r2-deployment-verify" in text
    assert "r2-deployment-qualification-result.json" in text
    assert "- staging" not in text
    assert "- production" not in text
