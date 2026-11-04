"""Top-level acceptance gate for the provider-neutral source specification."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-655-provider-neutral-sources.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-provider-neutral-sources.md"

ACCEPTANCE_CRITERIA = {
    "shared_canonical_path",
    "google_v1_compatibility",
    "immutable_scoped_refs_only_connections",
    "explicit_capability_outcomes",
    "typed_observation_representations",
    "durable_checkpoint_after_acceptance",
    "conservative_lifecycle_classification",
    "identity_assurance_controls_continuity",
    "revocation_denies_without_history_erasure",
    "safe_operator_api_and_ui",
    "connection_is_not_business_authority",
}


def _test_names(path: Path) -> set[str]:
    if path.suffix in {".ts", ".tsx"}:
        return set(
            re.findall(
                r"(?:it|test)\(\s*['\"](test_[a-zA-Z0-9_]+)['\"]",
                path.read_text(encoding="utf-8"),
            )
        )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_issue655_report_maps_every_acceptance_criterion_to_executable_evidence() -> None:
    report = _report()

    assert report["schema_version"] == "issue-655-provider-neutral-sources:v1"
    assert report["ticket"] == 655
    assert report["implementation_status"] == "implemented"
    assert report["qualification_status"] == "offline_qualified"
    assert set(report["acceptance_criteria"]) == ACCEPTANCE_CRITERIA

    for criterion in report["acceptance_criteria"].values():
        assert criterion["status"] == "PASS"
        assert criterion["evidence"]
        for node_id in criterion["evidence"]:
            relative_path, test_name = node_id.split("::", 1)
            assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue655_report_keeps_live_provider_qualification_explicitly_open() -> None:
    report = _report()

    assert report["live_qualification"] == {
        "google": "NOT_VERIFIED",
        "yandex_disk": "NOT_VERIFIED",
        "bitrix24": "NOT_VERIFIED",
        "mounted_filesystem": "NOT_VERIFIED",
    }
    assert report["provider_live_gates"] == {
        "google": {"ticket": 615, "state": "OPEN", "outcome": "NOT_VERIFIED"},
        "yandex_disk": {
            "ticket": 678,
            "state": "CLOSED",
            "outcome": "NOT_VERIFIED",
        },
        "bitrix24": {"ticket": 679, "state": "OPEN", "outcome": "NOT_VERIFIED"},
        "mounted_filesystem": {
            "ticket": 680,
            "state": "OPEN",
            "outcome": "NOT_VERIFIED",
        },
    }
    assert report["open_live_gates"] == [615, 679, 680]
    assert report["production_ready"] is False


def test_issue655_report_records_safe_offline_evidence_only() -> None:
    report = _report()

    assert report["privacy"] == {
        "synthetic_fakes_only": True,
        "credentials_recorded": False,
        "signed_urls_recorded": False,
        "protected_names_recorded": False,
        "content_excerpts_recorded": False,
        "raw_provider_errors_recorded": False,
    }

    lowered = REPORT_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("access_token", "refresh_token", "bearer ", "signed-secret"):
        assert forbidden not in lowered


def test_issue655_runbook_executes_the_acceptance_gate_and_child_qualification() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "tests/qualification/test_issue655_provider_neutral_sources.py" in runbook
    assert "tests/qualification/test_issue677_cross_provider_offline.py" in runbook
    assert "tests/qualification/test_issue614_google_offline_qualification.py" in runbook
    assert "uv run pytest -q" in runbook
    assert "NOT_VERIFIED" in runbook
