from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = (
    ROOT / "docs/qualification/issue-560-q16-01-runtime-readiness-v1.json"
)
SUMMARY_PATH = ROOT / "docs/qualification/issue-560-q16-01-runtime-readiness.md"


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_issue560_report_is_exact_metadata_only_and_does_not_claim_pass() -> None:
    report = _report()

    assert report["schema_version"] == "q16-01-runtime-readiness:v1"
    assert report["issue"] == 560
    assert report["decision"] == "BLOCKED"
    assert len(report["target"]["commit_sha"]) == 40
    assert len(report["target"]["deployment_fingerprint"]) == 64
    assert report["privacy"] == {
        "metadata_only": True,
        "credentials_recorded": False,
        "network_locations_recorded": False,
        "content_recorded": False,
        "provider_payloads_recorded": False,
        "raw_errors_recorded": False,
    }


def test_issue560_report_preserves_each_readiness_dimension() -> None:
    report = _report()
    readiness = report["readiness"]

    assert readiness["configuration"] == "BLOCKED"
    assert readiness["reachability"] == "NOT_VERIFIED"
    assert readiness["authorization"] == "BLOCKED"
    assert readiness["runtime_provider_bindings"] == "PASS"
    assert readiness["qualification_authority_binding"] == "NOT_VERIFIED"
    assert readiness["qualification_target_binding"] == "NOT_VERIFIED"
    assert readiness["application_execution"] == "BLOCKED"
    assert readiness["qualification"] == "BLOCKED"
    assert readiness["provider_calls_observed"] == 0

    observations = report["authorization_observations"]
    assert observations["approved_scope"] == "INVALID_BOOTSTRAPPED_AUTHORITY"


def test_issue560_synthetic_run_does_not_elevate_partial_correlation() -> None:
    report = _report()
    run = report["synthetic_ask"]

    assert run["api_admission"] == "PASS"
    assert run["canonical_outbox"] == "PASS_DELIVERED"
    assert run["dispatcher_delivery"] == "PASS"
    assert run["temporal_admission"] == "PASS_RUNNING"
    assert run["answer_question_capability"] == "BLOCKED"
    assert run["canonical_terminal_readback"] == "BLOCKED_ADMITTED"
    assert run["same_execution_capture"] == "NOT_VERIFIED"
    assert run["attempt_count"] == 0


def test_issue560_artifacts_omit_protected_or_secret_fields() -> None:
    report_text = REPORT_PATH.read_text(encoding="utf-8").lower()
    summary_text = SUMMARY_PATH.read_text(encoding="utf-8").lower()
    forbidden = (
        '"credential"',
        '"dsn"',
        '"endpoint"',
        '"question"',
        '"answer"',
        '"payload"',
        '"source_object_id"',
        '"filename"',
        '"raw_exception"',
    )

    assert all(value not in report_text for value in forbidden)
    assert all(value not in summary_text for value in forbidden)
