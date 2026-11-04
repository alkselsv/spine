from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs/qualification/issue-839-gmail-history-cursor-recovery.json"


def test_issue839_delivery_evidence_records_safe_cursor_recovery() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == (
        "issue-839-gmail-history-cursor-recovery-evidence:v1"
    )
    assert report["issue"] == 839
    assert report["offline_result"] == "PASS"
    assert report["live_gmail_semantics"] == "NOT_VERIFIED"
    assert "stale_checkpoint_is_preserved_on_the_fenced_attempt" in report[
        "proved_transition"
    ]


def test_issue839_evidence_is_synthetic_and_names_postgresql_suite() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert "Synthetic" in report["fixture_policy"]
    assert "no Gmail credentials" in report["fixture_policy"]
    assert any("postgresql" in item.lower() for item in report["durability_evidence"])
    assert "live qualification" in report["unverified_prerequisite"]
