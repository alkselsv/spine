from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-557-threshold-qualification.json"
SUMMARY_PATH = ROOT / "docs/qualification/issue-557-threshold-qualification.md"


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_issue557_missing_baseline_keeps_policy_and_gate_non_positive() -> None:
    report = _report()

    assert report["schema_version"] == "r1-threshold-qualification:v1"
    assert report["issue"] == 557
    assert report["baseline"]["status"] == "unavailable"
    assert report["product_owner_decision"] == {
        "status": "decision_required",
        "decision_id": None,
        "record_reference": None,
        "actor_id": None,
        "policy_id": None,
        "policy_version": None,
        "policy_digest": None,
    }
    assert report["release_gate_qualification"]["status"] == "NOT_VERIFIED"
    assert report["release_gate_qualification"]["gate_invoked"] is False
    assert report["release_gate_qualification"]["gate_result"] is None
    assert report["release_gate_qualification"]["reason_codes"] == [
        "accepted_real_baseline_unavailable",
        "product_owner_decision_required",
        "approved_threshold_policy_unavailable",
        "mandatory_safety_evidence_unavailable",
    ]


def test_issue557_package_reports_every_metric_without_inventing_observations() -> None:
    report = _report()
    metrics = report["metrics"]

    assert {item["metric"] for item in metrics} == {
        "retrieval.recall_at_k",
        "retrieval.mrr",
        "qa.groundedness",
        "qa.answer_outcome",
        "qa.abstention_quality",
        "qa.denial_quality",
        "qa.citation_precision",
        "qa.citation_recall",
        "qa.locator_validity",
        "observer.latency",
        "worker.latency",
        "operational.cost",
        "projection.indexing_lag",
        "operational.freshness",
        "operational.usefulness",
    }
    denial = next(item for item in metrics if item["metric"] == "qa.denial_quality")
    assert denial["applicability"] == "not_applicable"
    assert denial["eligible_count"] == 0
    assert denial["measured_denominator"] == 0
    assert denial["unavailable_count"] == 0
    assert denial["coverage"] is None
    assert denial["value"] is None

    observed = [item for item in metrics if item["metric"] != "qa.denial_quality"]
    assert all(item["status"] == "unavailable" for item in observed)
    assert all(item["numerator"] is None for item in observed)
    assert all(item["measured_denominator"] is None for item in observed)
    assert all(item["unavailable_count"] is None for item in observed)
    assert all(item["coverage"] is None for item in observed)
    assert all(item["value"] is None for item in observed)
    assert all(item["threshold"] is None for item in metrics)


def test_issue557_preserves_immutable_safety_gates_as_unverified() -> None:
    report = _report()
    safety = report["mandatory_safety"]

    assert {item["gate_id"] for item in safety} == {
        "workspace_environment_authorization",
        "access_policy_and_rls",
        "disclosure_safety",
        "current_source_revision",
        "provenance_and_lineage",
        "citation_locator_validity",
        "grounded_answer_safety",
    }
    assert all(item["mandatory"] is True for item in safety)
    assert all(item["status"] == "unavailable" for item in safety)
    assert report["release_gate_qualification"]["safety_evaluated_first"] is True


def test_issue557_artifacts_are_metadata_only_and_document_independence() -> None:
    report = _report()
    summary = SUMMARY_PATH.read_text(encoding="utf-8")
    serialized = json.dumps(report, sort_keys=True).lower()
    forbidden = (
        '"question"',
        '"answer"',
        '"filename"',
        '"source_id"',
        '"case_id"',
        '"citation_excerpt"',
        '"credential"',
        '"provider_payload"',
    )

    assert report["privacy"] == {
        "metadata_only": True,
        "private_benchmark_content_recorded": False,
        "case_or_source_identifiers_recorded": False,
        "credentials_recorded": False,
        "provider_payloads_recorded": False,
    }
    assert all(value not in serialized for value in forbidden)
    assert "Status: **NOT_VERIFIED**" in summary
    assert "`decision_required`" in summary
    assert "Full independence amendment" in summary
    assert "не запускался" in summary
