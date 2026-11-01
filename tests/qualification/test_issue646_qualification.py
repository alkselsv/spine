"""Executable offline qualification evidence for issue #646."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-646-offline-requirements.json"
REQUIRED_SCENARIOS = {
    "browser_start_to_exact_acceptance_and_reload",
    "lost_start_and_command_response",
    "idempotency_conflict",
    "duplicate_outbox_and_temporal_delivery",
    "worker_restart_and_retry",
    "partial_extraction",
    "unavailable_or_revoked_evidence",
    "cancellation",
    "explicitly_recoverable_failure",
    "postgresql_lineage_and_replay",
    "unsupported_requirements_quality_gate",
}


def _load_report() -> dict[str, Any]:
    value = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _python_test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_issue646_report_resolves_every_offline_recovery_scenario() -> None:
    report = _load_report()

    assert report["schema_version"] == "offline-requirements-qualification:v1"
    assert report["ticket"] == 646
    assert report["functional_offline_result"] == "PASS"
    scenarios = report["scenarios"]
    assert {scenario["scenario"] for scenario in scenarios} == REQUIRED_SCENARIOS

    for scenario in scenarios:
        assert scenario["result"] in {"PASS", "NOT_VERIFIED"}
        assert scenario["reason"]
        assert scenario["evidence"]
        for evidence in scenario["evidence"]:
            path = ROOT / evidence["file"]
            assert path.is_file(), evidence["file"]
            reference = evidence["reference"]
            if path.suffix == ".py":
                assert reference in _python_test_names(path), evidence
            else:
                assert reference in path.read_text(encoding="utf-8"), evidence


def test_issue646_keeps_unsupported_quality_separate_from_asr_accuracy() -> None:
    report = _load_report()
    gate = report["unsupported_requirements_gate"]

    assert report["overall_qualification_result"] == "NOT_VERIFIED"
    assert gate == {
        "result": "NOT_VERIFIED",
        "threshold_approved": False,
        "threshold": None,
        "owner": "Product/Project Owner",
        "reason_code": "UNSUPPORTED_REQUIREMENTS_THRESHOLD_NOT_APPROVED",
        "asr_metrics_are_substitute": False,
        "dataset": "russian-meeting-transcription-synthetic@1.0.0",
        "behavior_contract_issue": 682,
    }


def test_workflow_browser_uses_only_control_plane_api_and_no_qa_runtime() -> None:
    workflow_sources = tuple((ROOT / "web/src/features/workflows").glob("*.ts*"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in workflow_sources)
    lowered = source.lower()

    assert "temporal" not in lowered
    assert "/api/v1/qa" not in lowered
    assert "qa_session" not in lowered
    assert "q&a session" not in lowered


def test_issue646_names_future_live_prerequisites_without_claiming_them() -> None:
    report = _load_report()
    live = report["future_opt_in_qualification"]

    assert live["result"] == "NOT_VERIFIED"
    assert live["claimed_by_default_ci"] is False
    assert set(live["prerequisites"]) == {
        "approved isolated PostgreSQL target and migrations",
        "reachable Temporal service and registered requirements worker",
        "approved provider credentials and model configuration",
        "authorized live source revisions and disclosure policy",
    }
