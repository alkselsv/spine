"""Executable integrity checks for the issue #598 qualification record."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spine.scripts.google_live_qualification import main


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-598-google-qualification.json"
TARGET_COMMIT = "7970c7127bb215f955e0a70a40ce544e56be0407"


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _verification(report: dict[str, object]) -> dict[str, object]:
    verification = report["verification"]
    assert isinstance(verification, dict)
    return verification


def test_issue598_report_identifies_the_target_snapshot() -> None:
    report = _report()

    assert report["schema_version"] == "issue-598-google-qualification:v1"
    assert report["ticket"] == 598
    assert report["target_commit"] == TARGET_COMMIT


def test_issue598_report_records_offline_pass_results() -> None:
    results = _report()["results"]

    assert isinstance(results, dict)
    assert results["offline_connector_contracts"] == "PASS"
    assert results["postgresql_canonical_and_migration_contracts"] == "PASS"
    assert results["concrete_google_api_adapters"] == "PASS"
    assert results["live_harness_fail_closed_behavior"] == "PASS"


def test_issue598_report_keeps_live_and_production_fail_closed() -> None:
    report = _report()
    results = report["results"]

    assert isinstance(results, dict)
    assert report["status"] == "offline_pass_live_not_verified"
    assert report["decision"] == "NO_GO_FOR_LIVE_OR_PRODUCTION"
    assert results["my_drive_live_provider_behavior"] == "NOT_VERIFIED"
    assert results["sheets_live_provider_behavior"] == "NOT_VERIFIED"
    assert results["shared_drive_live_provider_behavior"] == "NOT_VERIFIED"
    assert results["production_readiness"] == "NOT_APPROVED"


def test_issue598_report_records_reproducible_repository_gates() -> None:
    verification = _verification(_report())

    assert verification["offline_result"] == "388 passed, 0 skipped, 0 failed"
    assert verification["adapter_harness_result"] == (
        "131 passed, 0 skipped, 0 failed"
    )
    assert verification["repository_result"] == "6013 passed, 7 skipped, 0 failed"
    assert verification["compileall_result"] == "passed"
    assert "tests/integration/postgresql/test_postgresql_unit_of_work.py" in (
        verification["offline_command"]
    )
    assert "tests/qualification/test_google_live_harness.py" in (
        verification["adapter_harness_command"]
    )


def test_issue598_report_records_exact_negative_preflight_receipt() -> None:
    verification = _verification(_report())

    assert verification["negative_preflight_command"].startswith(
        "env -u SPINE_GOOGLE_LIVE_OPT_IN "
    )
    assert verification["negative_preflight_result"] == "REFUSED_BEFORE_CONFIG"
    assert verification["negative_preflight_exit_code"] == 2
    assert verification["negative_preflight_provider_requests"] == 0
    assert verification["negative_preflight_report_written"] is False


def test_issue598_negative_preflight_probe_refuses_without_approved_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "missing-approved-config.json"
    report_path = tmp_path / "live-report.json"
    monkeypatch.delenv("SPINE_GOOGLE_LIVE_OPT_IN", raising=False)

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--composition",
            "unavailable_google_runtime:build_live_runner",
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 2
    assert not report_path.exists()
    assert capsys.readouterr().err == (
        "Google live qualification refused: approved opt-in or prerequisites "
        "are missing.\n"
    )


def test_issue598_evidence_binding_matches_target_and_recorded_results() -> None:
    report = _report()
    verification = _verification(report)
    values = (
        report["schema_version"],
        report["target_commit"],
        verification["offline_result"],
        verification["adapter_harness_result"],
        verification["repository_result"],
        verification["negative_preflight_result"],
        verification["negative_preflight_exit_code"],
        verification["negative_preflight_provider_requests"],
        verification["negative_preflight_report_written"],
    )
    payload = "".join(f"{str(value).lower()}\n" for value in values).encode()
    binding = report["evidence_binding"]

    assert isinstance(binding, dict)
    assert binding["scheme"] == "sha256-lines:v1"
    assert hashlib.sha256(payload).hexdigest() == binding["digest"]


def test_issue598_report_lists_delivered_children() -> None:
    assert set(_report()["delivered_children"]) == {
        *range(599, 615),
        652,
        *range(752, 757),
    }


def test_issue598_report_keeps_human_live_qualification_open() -> None:
    report = _report()

    assert report["remaining_child"] == {
        "ticket": 615,
        "status": "ready-for-human",
        "result": "NOT_VERIFIED",
    }


def test_issue598_report_preserves_remaining_live_prerequisites() -> None:
    report = _report()

    assert report["remaining_prerequisites"]
    assert report["unverified_live_behaviors"]


def test_issue598_report_is_metadata_only_and_does_not_claim_provider_access() -> None:
    report = _report()
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()

    assert report["privacy"] == {
        "synthetic_fakes_only": True,
        "credentials_accessed": False,
        "provider_network_calls": False,
        "provider_payloads_recorded": False,
        "source_content_recorded": False,
        "raw_provider_errors_recorded": False,
    }
    for forbidden in (
        "access_token",
        "bearer ",
        "client_secret",
        "refresh_token",
        '"source_content":',
        '"raw_provider_error":',
    ):
        assert forbidden not in serialized
