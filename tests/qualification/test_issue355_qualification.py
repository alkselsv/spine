from __future__ import annotations

import json
from pathlib import Path

from spine.evaluations.release_gates.applicability import DEFAULT_R1_APPLICABILITY_MANIFEST


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT = PROJECT_ROOT / "docs/qualification/issue-355-gate-applicability-report.json"


def test_issue355_report_is_machine_readable_and_versioned() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == "r1-gate-applicability-qualification:v1"
    assert report["ticket"] == 355
    assert report["policy_version"] == DEFAULT_R1_APPLICABILITY_MANIFEST.schema_version
    assert report["manifest_id"] == DEFAULT_R1_APPLICABILITY_MANIFEST.manifest_id
    assert report["status"] == "qualified_offline"
    assert report["production_approval"] is False


def test_issue355_report_covers_every_supported_manifest_change_class() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    rule_by_class = {
        rule.change_class: rule for rule in DEFAULT_R1_APPLICABILITY_MANIFEST.change_classes
    }

    assert set(report["coverage"]) == set(rule_by_class)
    for change_class, rule in rule_by_class.items():
        assert report["coverage"][change_class] == list(rule.suite_ids)


def test_issue355_mandatory_offline_suite_is_not_waivable() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["mandatory_offline_suite"] == {
        "suite_id": "r1-offline-safety",
        "suite_version": "v1",
        "always_selected": True,
        "evidence_id": "offline-regression-gate:v1",
    }
