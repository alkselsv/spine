"""Executable checks for the metadata-only Issue #634 qualification record."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
V1_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v1.json"
)
V2_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v2.json"
)
V3_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v3.json"
)


def _load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_nested_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            item
            for nested in value.values()
            for item in _collect_nested_strings(nested)
        ]
    if isinstance(value, list):
        return [
            item for nested in value for item in _collect_nested_strings(nested)
        ]
    return []


def _collect_nested_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [
            item
            for key, nested in value.items()
            for item in (key, *_collect_nested_keys(nested))
        ]
    if isinstance(value, list):
        return [item for nested in value for item in _collect_nested_keys(nested)]
    return []


def test_issue634_failed_pre_provider_run_makes_no_qualification_claim() -> None:
    report = _load_report(V1_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v1"
    assert report["ticket"] == 634
    assert re.fullmatch(r"[0-9a-f]{40}", report["target_commit"])
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["immutable"] is True
    assert report["content_free"] is True
    assert report["offline_profile_preflight"] == {
        "profile_document": "verified",
        "profile_repository": "resolved",
        "profile_reference_match": True,
        "provider_request_count": 0,
        "broker_request_count": 0,
    }
    projection = report["projection_attempt"]
    assert projection["outcome"] == "not_executed"
    assert projection["safe_reason_code"] == "cognee_runtime_binding_mismatch"
    assert projection["provider_request_count"] == 0
    assert projection["projection_consumer_invocation_count"] == 0
    assert sum(
        projection[key]
        for key in (
            "projection_receipt_count",
            "projection_snapshot_count",
            "projection_activation_count",
            "active_projection_count",
        )
    ) == 0
    assert report["application_canary"]["broker_request_count"] == 0
    assert report["application_canary"]["context_bundle_count"] == 0
    assert report["application_canary"]["context_bundle_issued"] is False


def test_issue634_report_records_all_remaining_conditions() -> None:
    report = _load_report(V1_REPORT_PATH)

    assert report["remaining_conditions"] == [
        "cognee_runtime_binding_resolution_not_verified",
        "reconciliation_not_verified",
        "runtime_readiness_not_verified",
        "provider_backed_projection_and_publication_not_verified",
        "authorized_context_broker_request_not_verified",
        "workspace_and_environment_isolation_not_verified",
        "current_policy_enforcement_not_verified",
        "publication_pin_not_verified",
        "source_revision_lineage_not_verified",
        "independent_final_evidence_review_required",
    ]


def test_issue634_failed_pre_provider_run_requires_independent_review() -> None:
    report = _load_report(V1_REPORT_PATH)

    assert report["independent_review"] == "required"


def test_issue634_cleanup_is_closed() -> None:
    report = _load_report(V1_REPORT_PATH)

    safety = report["safety"]
    assert safety["temporary_authority_revoked"] is True
    assert safety["runtime_connect_revoked"] is True
    assert safety["temporary_authentication_material_rotated"] is True
    assert safety["temporary_cognee_database_removed"] is True
    assert safety["task_owned_graph_resource_count"] == 0
    assert safety["target_stopped_after_capture"] is True
    assert safety["serving_activated"] is False
    assert safety["cutover_performed"] is False


def test_issue634_report_is_content_free() -> None:
    reports = tuple(
        _load_report(path)
        for path in (V1_REPORT_PATH, V2_REPORT_PATH, V3_REPORT_PATH)
    )

    forbidden = (
        "://",
        "api_key",
        "password",
        "credential",
        "connection_string",
        "filename",
        "source_text",
        "raw_payload",
        "provider_payload",
        "raw_error",
        "raw_exception",
    )
    for report in reports:
        for value in _collect_nested_strings(report):
            assert not any(token in value.lower() for token in forbidden)
            assert re.search(r"\b[^/\s]+\.(pdf|docx|xlsx|txt|md)\b", value, re.I) is None

    serialized_keys = "\n".join(
        key.lower()
        for report in reports
        for key in _collect_nested_keys(report)
    )
    assert not any(token in serialized_keys for token in forbidden)


def test_issue634_provider_timeout_run_records_blocked_decision() -> None:
    report = _load_report(V2_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v2"
    assert report["run_reference"] == "run:issue634-recovered-canary-002"
    assert report["target_commit"] == "de7fdf26f536004e5b4ba6919542cfa39b66a147"
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["independent_review"] == "required"


def test_issue634_provider_timeout_run_records_offline_preflight() -> None:
    report = _load_report(V2_REPORT_PATH)

    assert report["offline_profile_preflight"] == {
        "profile_document": "verified",
        "profile_repository": "resolved",
        "profile_reference_match": True,
        "provider_request_count": 0,
        "broker_request_count": 0,
    }


def test_issue634_provider_timeout_run_records_failed_projection() -> None:
    report = _load_report(V2_REPORT_PATH)

    projection = report["projection_attempt"]
    assert projection["outcome"] == "failed_closed"
    assert projection["safe_reason_code"] == "projection_request_physical_retryable"
    assert projection["provider_boundary_reached"] is True
    assert projection["provider_request_bounds_enforced"] is True
    assert projection["provider_internal_retry_limit"] == 2
    assert projection["pre_provider_setup_failure_count"] == 1
    assert projection["provider_backed_consumer_invocation_count"] == 1
    assert sum(
        projection[key]
        for key in (
            "projection_receipt_count",
            "projection_snapshot_count",
            "projection_activation_count",
            "active_projection_count",
            "source_projection_reference_count",
            "reconciliation_result_count",
        )
    ) == 0
    assert projection["publication_status"] == "unpublished"


def test_issue634_provider_timeout_run_skips_broker_without_publication() -> None:
    report = _load_report(V2_REPORT_PATH)

    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "publication_unavailable",
        "broker_request_count": 0,
        "context_bundle_count": 0,
        "context_bundle_issued": False,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
    }


def test_issue634_provider_timeout_run_cleanup_is_closed() -> None:
    report = _load_report(V2_REPORT_PATH)

    assert report["safety"] == {
        "synthetic_content_only": True,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
        "runtime_connect_revoked": True,
        "temporary_authentication_material_rotated": True,
        "temporary_cognee_database_removed": True,
        "task_owned_graph_resource_count": 0,
        "target_stopped_after_capture": True,
        "serving_activated": False,
        "cutover_performed": False,
    }


def test_issue634_repeated_timeout_run_records_blocked_decision() -> None:
    report = _load_report(V3_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v3"
    assert report["run_reference"] == "run:issue634-recovered-canary-003"
    assert report["target_commit"] == "b8f1dee821bc2c455f3487745873e8713ffda116"
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["independent_review"] == "required"


def test_issue634_repeated_timeout_run_records_offline_preparation() -> None:
    report = _load_report(V3_REPORT_PATH)

    assert report["offline_profile_preflight"] == {
        "profile_document": "verified",
        "profile_repository": "resolved",
        "profile_reference_match": True,
        "provider_request_count": 0,
        "broker_request_count": 0,
    }
    assert report["projection_attempt"]["pre_provider_setup_completed"] is True


def test_issue634_repeated_timeout_run_records_unpublished_projection() -> None:
    projection = _load_report(V3_REPORT_PATH)["projection_attempt"]

    assert projection["outcome"] == "failed_closed"
    assert projection["safe_reason_code"] == "projection_request_physical_retryable"
    assert projection["provider_boundary_reached"] is True
    assert projection["provider_profile_limits_validated"] is True
    assert projection["provider_internal_retry_limit"] == 2
    assert projection["provider_backed_consumer_invocation_count"] == 1
    assert projection["projection_run_count"] == 1
    assert sum(
        projection[key]
        for key in (
            "projection_receipt_count",
            "projection_snapshot_count",
            "projection_activation_count",
            "active_projection_count",
            "source_projection_reference_count",
            "reconciliation_result_count",
        )
    ) == 0
    assert projection["publication_status"] == "unpublished"


def test_issue634_repeated_timeout_run_skips_broker_without_publication() -> None:
    report = _load_report(V3_REPORT_PATH)

    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "publication_unavailable",
        "broker_request_count": 0,
        "context_bundle_count": 0,
        "context_bundle_issued": False,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
    }


def test_issue634_repeated_timeout_run_cleanup_is_closed() -> None:
    report = _load_report(V3_REPORT_PATH)

    assert report["safety"] == {
        "synthetic_content_only": True,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
        "runtime_connect_revoked": True,
        "temporary_authentication_material_rotated": True,
        "temporary_cognee_database_removed": True,
        "task_owned_graph_resource_count": 0,
        "sensitive_run_logs_removed": True,
        "target_stopped_after_capture": True,
        "serving_activated": False,
        "cutover_performed": False,
    }
