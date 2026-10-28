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
V4_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v4.json"
)
V5_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v5.json"
)
V6_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v6.json"
)
V7_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v7.json"
)
V8_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v8.json"
)
V9_REPORT_PATH = (
    PROJECT_ROOT
    / "docs/qualification/issue-634-recovered-application-canary-v9.json"
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
        for path in (
            V1_REPORT_PATH,
            V2_REPORT_PATH,
            V3_REPORT_PATH,
            V4_REPORT_PATH,
            V5_REPORT_PATH,
        )
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


def test_issue634_repeated_timeout_run_records_authorization_bounds() -> None:
    report = _load_report(V3_REPORT_PATH)

    assert report["authorization_reference"] == (
        "session:issue634-recovered-canary-003"
    )
    assert report["authorization_publication"] == "not_authorized"
    assert report["authorization_record"] == {
        "record_timing": "before_provider_request",
        "target_commit": "b8f1dee821bc2c455f3487745873e8713ffda116",
        "target_reference": "spine634canary003",
        "provider_destination": "api.vsellm.ru/v1",
        "provider_profile": "cognee-approved-production",
        "budget_usd": "1.00",
        "concurrency_limit": 1,
        "provider_request_limit": 1000,
        "input_token_limit": 1000000,
        "output_token_limit": 100000,
        "request_timeout_seconds": 120,
        "provider_internal_retry_limit": 2,
        "run_deadline_seconds": 3600,
        "hard_expiry": "2026-10-28T15:30:00Z",
        "projection_consumer_invocation_limit": 1,
        "context_broker_request_limit": 1,
        "synthetic_data_only": True,
        "serving_and_cutover_authorized": False,
    }


def test_issue634_repeated_timeout_run_records_exact_remaining_conditions() -> None:
    report = _load_report(V3_REPORT_PATH)

    assert report["remaining_conditions"] == [
        "provider_backed_projection_completion_within_authorized_bounds_not_verified",
        "reconciliation_not_verified",
        "provider_backed_projection_and_publication_not_verified",
        "authorized_context_broker_request_not_verified",
        "workspace_and_environment_isolation_not_verified",
        "current_policy_enforcement_not_verified",
        "publication_pin_not_verified",
        "source_revision_lineage_not_verified",
        "independent_final_evidence_review_required",
    ]


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


def test_issue634_add_timeout_run_records_blocked_decision() -> None:
    report = _load_report(V4_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v4"
    assert report["run_reference"] == "run:issue634-recovered-canary-004"
    assert report["target_commit"] == "fb80cf52b24d392e9dc557b26ab1eb54149b5351"
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["independent_review"] == "required"


def test_issue634_add_timeout_run_records_authorization() -> None:
    report = _load_report(V4_REPORT_PATH)

    assert report["authorization_reference"] == (
        "session:issue634-recovered-canary-004"
    )
    assert report["authorization_publication"] == "not_authorized"
    assert report["authorization_record"] == {
        "record_timing": "before_provider_request",
        "target_commit": "fb80cf52b24d392e9dc557b26ab1eb54149b5351",
        "target_reference": "spine634canary004",
        "provider_destination": "api.vsellm.ru/v1",
        "provider_profile": "cognee-approved-production",
        "budget_usd": "1.00",
        "concurrency_limit": 1,
        "provider_request_limit": 1000,
        "input_token_limit": 1000000,
        "output_token_limit": 100000,
        "request_timeout_seconds": 120,
        "provider_internal_retry_limit": 2,
        "run_deadline_seconds": 3600,
        "hard_expiry": "2026-10-28T15:30:00Z",
        "projection_consumer_invocation_limit": 1,
        "context_broker_request_limit": 1,
        "synthetic_data_only": True,
        "serving_and_cutover_authorized": False,
    }


def test_issue634_add_timeout_run_records_preflight() -> None:
    report = _load_report(V4_REPORT_PATH)

    assert report["offline_profile_preflight"] == {
        "profile_document": "verified",
        "profile_repository": "resolved",
        "profile_reference_match": True,
        "provider_request_count": 0,
        "broker_request_count": 0,
    }


def test_issue634_add_timeout_run_records_pre_consumer_setup() -> None:
    report = _load_report(V4_REPORT_PATH)

    assert report["pre_consumer_runner_setup"] == {
        "initial_attempt_outcome": "failed_closed",
        "consumer_invocation_count": 0,
        "provider_request_count": 0,
        "corrected_before_consumer_invocation": True,
    }


def test_issue634_add_timeout_run_records_unpublished_projection() -> None:
    report = _load_report(V4_REPORT_PATH)
    projection = report["projection_attempt"]

    assert projection["outcome"] == "failed_closed"
    assert projection["safe_reason_code"] == "projection_request_physical_retryable"
    assert projection["provider_backed_path_entered"] is True
    assert projection["provider_profile_limits_validated"] is True
    assert projection["provider_internal_retry_limit"] == 2
    assert projection["pre_provider_setup_completed"] is True
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


def test_issue634_add_timeout_run_records_exact_failed_stage() -> None:
    report = _load_report(V4_REPORT_PATH)

    assert report["diagnostic_boundary"] == {
        "failure_stage": "add",
        "safe_exception_class": "TimeoutError",
        "external_provider_reached": "not_observed",
        "context_broker_reached": False,
        "prior_authorized_timeout_reproduced": True,
        "raw_failure_retained": False,
    }


def test_issue634_add_timeout_run_skips_broker_without_publication() -> None:
    report = _load_report(V4_REPORT_PATH)

    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "publication_unavailable",
        "broker_request_count": 0,
        "context_bundle_count": 0,
        "context_bundle_issued": False,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
    }


def test_issue634_add_timeout_run_records_exact_remaining_conditions() -> None:
    report = _load_report(V4_REPORT_PATH)

    assert report["remaining_conditions"] == [
        "provider_backed_projection_completion_within_authorized_bounds_not_verified",
        "reconciliation_not_verified",
        "provider_backed_projection_and_publication_not_verified",
        "authorized_context_broker_request_not_verified",
        "workspace_and_environment_isolation_not_verified",
        "current_policy_enforcement_not_verified",
        "publication_pin_not_verified",
        "source_revision_lineage_not_verified",
        "independent_final_evidence_review_required",
    ]


def test_issue634_add_timeout_run_cleanup_is_closed() -> None:
    report = _load_report(V4_REPORT_PATH)

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


def test_issue634_scoped_connection_policy_run_records_blocked_decision() -> None:
    report = _load_report(V5_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v5"
    assert report["run_reference"] == "run:issue634-recovered-canary-005"
    assert report["target_commit"] == "f810127529a9beb1dd2fc75431961a30dbc567b0"
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["independent_review"] == "required"


def test_issue634_unsupported_connection_flag_run_reaches_physical_add_once() -> None:
    report = _load_report(V5_REPORT_PATH)

    assert report["connection_test_policy"] == {
        "scoped_environment_flag_applied": True,
        "cognee_1_5_4_flag_consumer_found": False,
        "intended_bypass_verified": False,
        "physical_add_entered": True,
    }
    projection = report["projection_attempt"]
    assert projection["outcome"] == "failed_closed"
    assert projection["safe_reason_code"] == "projection.request.physical_retryable"
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


def test_issue634_scoped_connection_policy_run_records_safe_timeout_boundary() -> None:
    report = _load_report(V5_REPORT_PATH)

    assert report["diagnostic_boundary"] == {
        "failure_stage": "add",
        "safe_exception_class": "TimeoutError",
        "root_cause": "relational_connection_pool_exhaustion",
        "configured_pool_size": 1,
        "configured_max_overflow": 0,
        "cognee_pool_timeout_seconds": 280,
        "observed_connection_lifetime_seconds": 281,
        "external_provider_reached": False,
        "context_broker_reached": False,
        "raw_failure_retained": False,
    }
    assert report["projection_attempt"]["provider_request_count_observation"] == 0
    assert report["application_canary"]["broker_request_count"] == 0
    assert report["application_canary"]["temporary_authority_created"] is False


def test_issue634_scoped_connection_policy_run_cleanup_is_closed() -> None:
    report = _load_report(V5_REPORT_PATH)

    assert report["safety"] == {
        "synthetic_content_only": True,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
        "runtime_connect_revoked": True,
        "temporary_authentication_material_rotated_count": 6,
        "temporary_cognee_database_removed": True,
        "task_owned_graph_resource_count": 0,
        "sensitive_run_logs_removed": True,
        "target_stopped_after_capture": True,
        "serving_activated": False,
        "cutover_performed": False,
    }


def test_issue634_larger_pool_run_records_next_least_privilege_blocker() -> None:
    report = _load_report(V6_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v6"
    assert report["run_reference"] == "run:issue634-recovered-canary-006"
    assert report["target_commit"] == "62024024a7d2d74ed2f636469ec302bff083adc7"
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["independent_review"] == "required"


def test_issue634_larger_pool_run_invokes_consumer_once_without_publication() -> None:
    report = _load_report(V6_REPORT_PATH)
    projection = report["projection_attempt"]

    assert projection["outcome"] == "failed_closed"
    assert projection["safe_reason_code"] == "projection.request.physical_retryable"
    assert projection["provider_backed_consumer_invocation_count"] == 1
    assert projection["provider_endpoint_test_phases_observed"] == 2
    assert projection["provider_request_count_exactly_measured"] is False
    assert projection["provider_request_limit_exceeded"] is False
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


def test_issue634_larger_pool_run_records_dataset_database_creation_boundary() -> None:
    report = _load_report(V6_REPORT_PATH)

    assert report["diagnostic_boundary"] == {
        "failure_stage": "add",
        "safe_exception_class": "ProgrammingError",
        "root_cause": "cognee_dataset_database_creation_requires_createdb",
        "configured_pool_size": 5,
        "configured_max_overflow": 0,
        "prior_pool_timeout_reproduced": False,
        "dataset_database_create_attempted": True,
        "database_role_createdb": False,
        "postgresql_error": "permission_denied_to_create_database",
        "external_provider_reached": True,
        "context_broker_reached": False,
        "raw_failure_retained": False,
    }
    assert report["application_canary"]["broker_request_count"] == 0
    assert report["application_canary"]["temporary_authority_created"] is False


def test_issue634_larger_pool_run_cleanup_is_closed() -> None:
    report = _load_report(V6_REPORT_PATH)

    assert report["safety"] == {
        "synthetic_content_only": True,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
        "runtime_connect_revoked": True,
        "temporary_authentication_material_rotated_count": 6,
        "temporary_cognee_database_removed": True,
        "task_owned_graph_resources_created_count": 2,
        "task_owned_graph_resources_removed_count": 2,
        "task_owned_graph_resource_count": 0,
        "sensitive_run_logs_removed": True,
        "target_stopped_after_capture": True,
        "evidence_volume_retained": True,
        "serving_activated": False,
        "cutover_performed": False,
    }


def test_issue634_shared_pgvector_run_records_published_projection() -> None:
    report = _load_report(V7_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v7"
    assert report["run_reference"] == "run:issue634-recovered-canary-007"
    assert report["target_commit"] == "e6338687abee5b5eb08fcf9a9ed7b2ae6743ca6f"
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    projection = report["projection_attempt"]
    assert projection == {
        "outcome": "delivered",
        "provider_backed_path_entered": True,
        "provider_request_count_exactly_measured": False,
        "provider_request_limit_exceeded": False,
        "provider_backed_consumer_invocation_count": 1,
        "projection_run_count": 1,
        "projection_receipt_count": 1,
        "projection_snapshot_count": 1,
        "projection_activation_count": 1,
        "active_projection_count": 1,
        "source_projection_reference_count": 3,
        "reconciliation_result_count": 1,
        "publication_status": "published",
        "vector_dataset_handler": "pgvector_shared",
        "database_role_createdb": False,
        "dataset_database_create_attempted": False,
    }


def test_issue634_shared_pgvector_run_fails_closed_before_broker() -> None:
    report = _load_report(V7_REPORT_PATH)

    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "noninteractive_evidence_authorization_mode_unsupported",
        "broker_request_count": 0,
        "context_bundle_count": 0,
        "context_bundle_issued": False,
        "temporary_authority_created": False,
        "temporary_authority_version_count": 0,
        "temporary_authority_revocation": "not_applicable",
    }
    assert report["pre_broker_validation"] == {
        "validation_kind": "static_production_composition_contract",
        "single_binding_path": "qa_service_noninteractive_processing",
        "service_source_grant_present": True,
        "scope_resolver_accepts_noninteractive_processing": True,
        "identity_revalidation_supports_service_only_authority": True,
        "noninteractive_authority_port_available": True,
        "evidence_verifier_dispatches_by_authorization_mode": False,
        "evidence_verifier_forced_mode": "interactive_processing",
        "existing_human_qualification_deadline_current": False,
        "human_binding_deadline_mutable": False,
        "same_scope_replacement_binding_allowed": False,
        "additional_policy_or_canonical_mutation_permitted": False,
        "fail_closed_before_authority_or_broker_request": True,
    }


def test_issue634_shared_pgvector_run_records_remaining_broker_conditions() -> None:
    report = _load_report(V7_REPORT_PATH)

    assert report["remaining_conditions"] == [
        "provider_request_and_token_budget_independent_metering_not_verified",
        "noninteractive_evidence_authorization_mode_dispatch_not_implemented",
        "authorized_context_broker_request_not_verified",
        "workspace_and_environment_isolation_not_verified",
        "current_policy_enforcement_not_verified",
        "publication_pin_not_verified",
        "source_revision_lineage_not_verified",
        "independent_final_evidence_review_required",
    ]


def test_issue634_shared_pgvector_run_cleanup_is_closed() -> None:
    report = _load_report(V7_REPORT_PATH)

    assert report["safety"] == {
        "synthetic_content_only": True,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
        "runtime_connect_revoked": True,
        "temporary_authentication_material_rotated_count": 6,
        "temporary_cognee_database_removed": True,
        "task_owned_graph_resources_created_count": 2,
        "task_owned_graph_resources_removed_count": 2,
        "task_owned_graph_resource_count": 0,
        "sensitive_run_logs_removed": True,
        "target_stopped_after_capture": True,
        "evidence_volume_retained": True,
        "serving_activated": False,
        "cutover_performed": False,
        "direct_canonical_or_active_pointer_edit_performed": False,
    }


def test_issue634_run008_records_one_failed_consumer_without_provider_or_broker() -> None:
    report = _load_report(V8_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v8"
    assert report["run_reference"] == "run:issue634-recovered-canary-008"
    assert report["target_commit"] == "8860651013991f093dc6cd4bccd320cd30214153"
    assert report["status"] == "blocked"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    projection = report["projection_attempt"]
    assert projection["outcome"] == "failed_closed"
    assert projection["safe_reason_code"] == "projection.request.invalid"
    assert projection["failure_stage"] == "request_envelope_validation"
    assert projection["provider_request_count"] == 0
    assert projection["provider_backed_consumer_invocation_count"] == 1
    assert projection["second_consumer_invocation_performed"] is False
    assert report["application_canary"]["broker_request_count"] == 0


def test_issue634_run008_records_unchanged_canonical_state() -> None:
    report = _load_report(V8_REPORT_PATH)
    projection = report["projection_attempt"]

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
    assert report["application_canary"]["context_bundle_count"] == 0
    assert report["application_canary"]["temporary_authority_created"] is False


def test_issue634_run008_records_reconciliation_as_remaining() -> None:
    report = _load_report(V8_REPORT_PATH)

    assert "reconciliation_not_verified" in report["remaining_conditions"]


def test_issue634_run008_preserves_post_publication_authorization_ordering() -> None:
    report = _load_report(V8_REPORT_PATH)

    assert (
        report["authorization_record"][
            "authority_and_broker_after_publication_only"
        ]
        is True
    )


def test_issue634_run008_cleanup_is_closed() -> None:
    report = _load_report(V8_REPORT_PATH)

    assert report["safety"] == {
        "synthetic_content_only": True,
        "temporary_authority_created": False,
        "temporary_authority_revocation": "not_applicable",
        "runtime_connect_revoked": True,
        "temporary_authentication_material_rotated_count": 6,
        "temporary_cognee_database_removed": True,
        "temporary_cognee_role_removed": True,
        "task_owned_graph_resources_created_count": 0,
        "task_owned_graph_resources_removed_count": 0,
        "task_owned_graph_resource_count": 0,
        "sensitive_run_logs_removed": True,
        "target_stopped_after_capture": True,
        "evidence_volume_retained": True,
        "serving_activated": False,
        "cutover_performed": False,
        "direct_canonical_or_active_pointer_edit_performed": False,
    }


def test_issue634_run009_records_active_reconciled_publication() -> None:
    report = _load_report(V9_REPORT_PATH)

    assert report["schema_version"] == "recovered-application-canary:v9"
    assert report["run_reference"] == "run:issue634-recovered-canary-009"
    assert report["target_commit"] == "f5d7587b83cb5589a4bdf6a762438f2c20f1156d"
    projection = report["projection_attempt"]
    assert projection["outcome"] == "completed"
    assert projection["diagnostic_causation_matches_event_id"] is True
    assert projection["provider_backed_consumer_invocation_count"] == 1
    assert projection["second_consumer_invocation_performed"] is False
    assert projection["projection_candidate_state"] == "sealed"
    assert projection["publication_status"] == "active"
    assert projection["reconciliation_outcome"] == "healthy"
    assert projection["reconciliation_result_count"] == 1


def test_issue634_run009_records_one_authorized_broker_bundle_with_lineage() -> None:
    report = _load_report(V9_REPORT_PATH)
    canary = report["application_canary"]

    assert canary["outcome"] == "allowed"
    assert canary["broker_request_count"] == 1
    assert canary["second_broker_request_performed"] is False
    assert canary["context_bundle_count"] == 1
    assert canary["context_bundle_state"] == "complete"
    assert canary["evidence_reference_count"] == 3
    assert canary["workspace_and_environment_isolation"] == "verified"
    assert canary["current_policy_enforcement"] == "verified"
    assert canary["publication_snapshot_matches_active"] is True
    assert canary["publication_activation_matches_active"] is True
    assert canary["source_projection_reference_matches"] == 3
    assert canary["canonical_source_revision_matches"] == 3
    assert canary["canonical_boundary_member_matches"] == 3
    assert canary["source_revision_lineage"] == "verified"


def test_issue634_run009_preserves_authority_order_and_revocation() -> None:
    report = _load_report(V9_REPORT_PATH)
    canary = report["application_canary"]

    assert report["authorization_record"][
        "authority_and_broker_after_publication_only"
    ] is True
    assert report["safety"]["temporary_authority_created_after_publication"] is True
    assert canary["temporary_authority_binding_count"] == 1
    assert canary["temporary_authority_version_count"] == 2
    assert canary["temporary_authority_revocation"] == "verified"
    assert canary["authority_resolves_after_revocation"] is False


def test_issue634_run009_records_exact_remaining_conditions() -> None:
    report = _load_report(V9_REPORT_PATH)

    assert report["remaining_conditions"] == [
        "provider_request_and_token_budget_independent_metering_not_verified",
        "independent_final_evidence_review_required",
    ]


def test_issue634_run009_cleanup_is_closed() -> None:
    report = _load_report(V9_REPORT_PATH)

    assert report["safety"] == {
        "synthetic_content_only": True,
        "temporary_authority_created_after_publication": True,
        "temporary_authority_revocation": "verified",
        "runtime_connect_revoked": True,
        "temporary_authentication_material_removed": True,
        "temporary_cognee_database_removed": True,
        "temporary_cognee_role_removed": True,
        "task_owned_graph_resources_created_count": 2,
        "task_owned_graph_resources_removed_count": 2,
        "task_owned_graph_resource_count": 0,
        "sensitive_run_logs_removed": True,
        "target_stopped_after_capture": True,
        "evidence_volume_retained": True,
        "serving_activated": False,
        "cutover_performed": False,
        "direct_canonical_or_active_pointer_edit_performed": False,
    }
