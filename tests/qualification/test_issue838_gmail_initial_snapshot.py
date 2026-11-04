from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs/qualification/issue-838-gmail-initial-snapshot.json"


def test_issue838_delivery_evidence_records_offline_protocol_without_live_claim() -> (
    None
):
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == ("issue-838-gmail-initial-snapshot-evidence:v1")
    assert report["issue"] == 838
    assert report["offline_result"] == "PASS"
    assert report["live_gmail_semantics"] == "NOT_VERIFIED"
    assert report["proved_transition"] == [
        "captured_history_baseline",
        "complete_snapshot_pages",
        "durable_snapshot_effect_receipts",
        "complete_intervening_history_drain",
        "single_terminal_cursor_commit",
    ]


def test_issue838_evidence_is_synthetic_and_names_postgresql_crash_suite() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    evidence = report["durability_evidence"]

    assert "Synthetic" in report["fixture_policy"]
    assert "no Gmail credentials" in report["fixture_policy"]
    assert any("postgresql" in item.lower() for item in evidence)
    assert "live qualification" in report["unverified_prerequisite"]
