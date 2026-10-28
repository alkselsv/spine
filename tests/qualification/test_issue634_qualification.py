"""Executable checks for the metadata-only Issue #634 qualification record."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v1.json"
)


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
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

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
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

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


def test_issue634_cleanup_is_closed() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    safety = report["safety"]
    assert safety["temporary_authority_revoked"] is True
    assert safety["runtime_connect_revoked"] is True
    assert safety["temporary_authentication_material_rotated"] is True
    assert safety["temporary_cognee_database_removed"] is True
    assert safety["task_owned_graph_resource_count"] == 0
    assert safety["target_stopped_after_capture"] is True
    assert safety["serving_activated"] is False
    assert safety["cutover_performed"] is False
    assert report["independent_review"] == "required"


def test_issue634_report_is_content_free() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

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
    for value in _collect_nested_strings(report):
        assert not any(token in value.lower() for token in forbidden)
        assert re.search(r"\b[^/\s]+\.(pdf|docx|xlsx|txt|md)\b", value, re.I) is None

    serialized_keys = "\n".join(key.lower() for key in _collect_nested_keys(report))
    assert not any(token in serialized_keys for token in forbidden)
