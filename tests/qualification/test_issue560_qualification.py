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


def test_issue560_report_is_exact_metadata_only_and_reports_current_blocker() -> None:
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

    assert readiness["configuration"] == "PASS"
    assert readiness["reachability"] == "BLOCKED"
    assert readiness["authorization"] == "PASS"
    assert readiness["runtime_provider_bindings"] == "PASS"
    assert readiness["qualification_authority_binding"] == "PASS"
    assert readiness["qualification_target_binding"] == "PASS"
    assert readiness["application_execution"] == "BLOCKED"
    assert readiness["qualification"] == "BLOCKED"
    assert readiness["provider_calls_observed"] == 0

    observations = report["authorization_observations"]
    assert observations["approved_scope_a"] == "PASS_200"
    assert observations["approved_scope_b"] == "PASS_200"
    assert observations["cross_workspace_source"] == (
        "PASS_NOT_FOUND_NO_IDENTITY_DISCLOSURE"
    )
    assert observations["stale_authorization_generation"] == (
        "PASS_DENIED_PRE_ATTEMPT"
    )

    provisioning = report["fixture_provisioning"]
    assert provisioning["mode"] == "explicit_operator_profile_before_runtime_start"
    assert provisioning["idempotent_replay"] == "PASS"
    assert provisioning["partial_failure_retry_contract"] == "PASS"
    assert provisioning["immutable_conflict_detection"] == "PASS"
    assert provisioning["stale_generation_fixture"] == "PASS"
    assert provisioning["generation_after_advance"] == (
        provisioning["generation_before_advance"] + 1
    )
    assert provisioning["generation_after_advance_retry"] == provisioning[
        "generation_after_advance"
    ]
    assert provisioning["counts_unchanged_after_runtime_verification"] is True

    manifest = report["manifest"]
    assert manifest["capture_service_profile_id"] == "api-qa-capture-v1"
    assert manifest["capture_worker_workload_id"] == "spine-qa-worker"
    assert manifest["capture_observer_workload_id"] == "spine-qa-observer"


def test_issue560_provider_blocker_does_not_elevate_partial_correlation() -> None:
    report = _report()
    run = report["synthetic_ask"]

    assert run["api_admission"] == "BLOCKED"
    assert run["canonical_outbox"] == "BLOCKED"
    assert run["dispatcher_delivery"] == "BLOCKED_PROVIDER_REACHABILITY"
    assert run["temporal_admission"] == "NOT_VERIFIED"
    assert run["answer_question_capability"] == "NOT_VERIFIED"
    assert run["canonical_terminal_readback"] == "NOT_VERIFIED"
    assert run["same_execution_capture"] == "NOT_VERIFIED"
    assert run["attempt_count"] == 0

    stale = report["stale_generation_probe"]
    assert stale["api_admission"] == "PASS_GENERATION_15"
    assert stale["worker_authorization"] == "PASS_DENIED_AT_GENERATION_16"
    assert stale["attempt_count"] == 0
    assert stale["provider_calls_observed"] == 0
    assert stale["protected_source_disclosure"] is False


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
