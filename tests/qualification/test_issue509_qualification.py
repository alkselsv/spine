"""Executable traceability checks for Issue #509 qualification evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = PROJECT_ROOT / "docs/qualification/issue-509-backup-recovery-qualification.json"
READINESS_REPORT_PATH = PROJECT_ROOT / "docs/qualification/issue-509-local-readiness-report-v2.json"
TEMPORAL_READINESS_REPORT_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-temporal-readiness-report-v1.json"
)
RUNTIME_COMPOSITION_REPORT_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-runtime-composition-readiness-v1.json"
)
TEMPORAL_READINESS_REPORT_V2_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-temporal-readiness-report-v2.json"
)
PROJECTION_RECOVERY_REPORT_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-projection-recovery-attempt-v1.json"
)
APPLICATION_RECOVERY_REPORT_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-application-recovery-attempt-v1.json"
)
APPLICATION_RECOVERY_REPORT_V2_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-application-recovery-attempt-v2.json"
)
APPLICATION_RECOVERY_REPORT_V3_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-application-recovery-attempt-v3.json"
)
APPLICATION_RECOVERY_REPORT_V4_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-application-recovery-attempt-v4.json"
)
APPLICATION_RECOVERY_REPORT_V5_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-application-recovery-attempt-v5.json"
)
APPLICATION_RECOVERY_REPORT_V6_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-application-recovery-attempt-v6.json"
)
APPLICATION_RECOVERY_DIAGNOSTIC_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-application-recovery-diagnostic-v1.json"
)
NEO4J_RUNTIME_COMPATIBILITY_REPORT_PATH = (
    PROJECT_ROOT / "docs/qualification/issue-509-neo4j-runtime-compatibility-v1.json"
)
COMPOSE_PATH = PROJECT_ROOT / "deploy/qualification/issue-509/compose.yaml"
DOCKERIGNORE_PATH = PROJECT_ROOT / ".dockerignore"
QUALIFICATION_DOCKERFILE_PATH = PROJECT_ROOT / "deploy/qualification/issue-509/Dockerfile"

EXPECTED_CRITERIA = {f"AC{number}" for number in range(1, 10)}
OUTCOME_STATUSES = {"pass", "fail", "blocked", "not_verified", "unavailable"}
CRITERIA_STATUSES = OUTCOME_STATUSES - {"fail"}
FORBIDDEN_REPORT_TOKENS = (
    "password",
    "secret",
    "credential",
    "endpoint",
    "filename",
    "raw_payload",
    "source_text",
    "stack_trace",
)
QUALIFICATION_BUILD_CONTEXT_ALLOWLIST = {
    "!pyproject.toml",
    "!uv.lock",
    "!README.md",
    "!alembic.ini",
    "!migrations",
    "!migrations/**",
    "!src",
    "!src/**",
}


def _load_report() -> dict[str, Any]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _load_readiness_report() -> dict[str, Any]:
    return json.loads(READINESS_REPORT_PATH.read_text(encoding="utf-8"))


def _load_projection_recovery_report() -> dict[str, Any]:
    return json.loads(PROJECTION_RECOVERY_REPORT_PATH.read_text(encoding="utf-8"))


def _load_application_recovery_report() -> dict[str, Any]:
    return json.loads(APPLICATION_RECOVERY_REPORT_PATH.read_text(encoding="utf-8"))


def _load_application_recovery_report_v2() -> dict[str, Any]:
    return json.loads(APPLICATION_RECOVERY_REPORT_V2_PATH.read_text(encoding="utf-8"))


def _load_application_recovery_report_v3() -> dict[str, Any]:
    return json.loads(APPLICATION_RECOVERY_REPORT_V3_PATH.read_text(encoding="utf-8"))


def _load_application_recovery_report_v4() -> dict[str, Any]:
    return json.loads(APPLICATION_RECOVERY_REPORT_V4_PATH.read_text(encoding="utf-8"))


def _load_application_recovery_report_v5() -> dict[str, Any]:
    return json.loads(APPLICATION_RECOVERY_REPORT_V5_PATH.read_text(encoding="utf-8"))


def _load_application_recovery_report_v6() -> dict[str, Any]:
    return json.loads(APPLICATION_RECOVERY_REPORT_V6_PATH.read_text(encoding="utf-8"))


def _load_application_recovery_diagnostic() -> dict[str, Any]:
    return json.loads(APPLICATION_RECOVERY_DIAGNOSTIC_PATH.read_text(encoding="utf-8"))


def _load_neo4j_runtime_compatibility_report() -> dict[str, Any]:
    return json.loads(
        NEO4J_RUNTIME_COMPATIBILITY_REPORT_PATH.read_text(encoding="utf-8")
    )


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _all_strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _all_strings(nested)]
    return []


def _fingerprint(material: dict[str, Any]) -> str:
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_issue509_report_is_closed_versioned_and_content_free() -> None:
    report = _load_report()

    assert set(report) == {
        "schema_version",
        "report_id",
        "ticket",
        "parent_ticket",
        "generated_at",
        "target_commit",
        "immutable",
        "content_free",
        "decision",
        "production_qualified",
        "scope",
        "outcome_semantics",
        "criteria",
        "evidence_artifacts",
        "unresolved_conditions",
        "safety_assertions",
        "unsupported_claims",
    }
    assert report["schema_version"] == "r1-backup-recovery-qualification:v1"
    assert report["report_id"] == "issue-509:qualification:v1"
    assert report["ticket"] == 509
    assert report["parent_ticket"] == 16
    assert report["immutable"] is True
    assert report["content_free"] is True
    assert report["decision"] == "blocked"
    assert report["production_qualified"] is False
    assert re.fullmatch(r"[0-9a-f]{40}", report["target_commit"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", report["generated_at"])
    assert set(report["outcome_semantics"]) == OUTCOME_STATUSES

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_report_covers_every_acceptance_criterion_fail_closed() -> None:
    report = _load_report()
    criteria = report["criteria"]

    assert {item["criterion_id"] for item in criteria} == EXPECTED_CRITERIA
    assert all(
        set(item) == {
            "criterion_id",
            "status",
            "reason_codes",
            "evidence_artifact_ids",
            "deployment_exercise",
        }
        for item in criteria
    )
    assert all(item["status"] in CRITERIA_STATUSES for item in criteria)
    assert {item["criterion_id"] for item in criteria if item["status"] == "blocked"} == {
        "AC1",
        "AC3",
    }
    assert {item["criterion_id"] for item in criteria if item["status"] == "not_verified"} == {
        "AC2",
        "AC5",
        "AC6",
        "AC7",
    }
    assert {item["criterion_id"] for item in criteria if item["status"] == "pass"} == {
        "AC4",
        "AC8",
        "AC9",
    }

    assert len({item["artifact_id"] for item in report["evidence_artifacts"]}) == len(
        report["evidence_artifacts"]
    )
    artifacts = {item["artifact_id"]: item for item in report["evidence_artifacts"]}
    assert artifacts["deployment-approved-recovery-manifest:v1"]["status"] == "unavailable"
    assert artifacts["deployment-approved-recovery-manifest:v1"]["digest"] is None
    assert artifacts["isolated-restore-evidence:v1"]["status"] == "unavailable"
    assert artifacts["authorized-recovery-canary:v1"]["status"] == "unavailable"
    assert all(item["physical_verification"] is False for item in artifacts.values())
    assert report["unresolved_conditions"]
    assert all(value is False for value in report["unsupported_claims"].values())


def test_issue509_report_does_not_promote_repository_evidence_to_live_recovery() -> None:
    report = _load_report()
    live_criteria = {"AC1", "AC2", "AC3", "AC5", "AC6", "AC7"}

    for item in report["criteria"]:
        if item["criterion_id"] in live_criteria:
            assert item["status"] != "pass"
            assert item["deployment_exercise"] in {"not_started", "covered_by_deterministic_tests_only"}

    assert report["safety_assertions"] == [
        "live_source_deployment_not_modified",
        "isolated_target_not_created_without_approval",
        "no_sensitive_auth_values_recorded",
        "no_production_readiness_or_cutover_claim_made",
        "physical_projection_remains_non_authoritative",
    ]


def test_issue509_projection_recovery_report_records_verified_candidate_fail_closed() -> None:
    report = _load_projection_recovery_report()

    assert report["schema_version"] == "r1-projection-recovery-attempt:v1"
    assert report["run_reference"] == "run:issue509-projection-recovery-001"
    assert report["target_commit"] == "868b1ff14068671c06c11db60d88aa7f6a06c0a2"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["immutable"] is True
    assert report["content_free"] is True
    assert report["projection_recovery"] == {
        "outcome": "verified",
        "source_revision_count": 1,
        "projection_effect_count": 1,
        "physical_retrieval_outcome": "verified",
        "physical_retrieval_chunk_count": 3,
        "physical_retrieval_evidence_count": 3,
        "lineage_check": "verified",
    }
    assert report["application_canary"] == {
        "publication_status": "unpublished",
        "fail_closed_status_check": "verified",
        "authorized_source_read": "not_verified",
    }
    assert report["safety"] == {
        "serving_activated": False,
        "promotion_performed": False,
        "cutover_performed": False,
        "temporary_restore_privileges_revoked": True,
        "restore_function_owner_reverted": True,
        "target_stopped_after_capture": True,
    }
    assert report["independent_review"] == "required"


def test_issue509_projection_recovery_report_contains_only_safe_metadata() -> None:
    report = _load_projection_recovery_report()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_recovery_report_records_provider_timeout_fail_closed() -> None:
    report = _load_application_recovery_report()

    assert report["schema_version"] == "r1-recovered-application-attempt:v1"
    assert report["run_reference"] == "run:issue509-application-canary-001"
    assert report["target_commit"] == "dc912739f9513247f9d0e7d7d48c61a5efcaa3b2"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["projection_attempt"] == {
        "outcome": "failed_closed",
        "safe_reason_code": "provider_connectivity_timeout",
        "provider_attempted": True,
        "projection_receipt_count": 0,
        "projection_snapshot_count": 0,
        "projection_activation_count": 0,
        "publication_status": "unpublished",
    }
    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "publication_unavailable",
        "authorized_source_read": "not_verified",
    }
    assert report["safety"] == {
        "synthetic_content_only": True,
        "serving_activated": False,
        "cutover_performed": False,
        "runtime_connect_revoked": True,
        "temporary_operator_auth_rotated": True,
        "target_stopped_after_capture": True,
    }
    assert report["independent_review"] == "required"


def test_issue509_application_recovery_report_contains_only_safe_metadata() -> None:
    report = _load_application_recovery_report()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_recovery_v2_records_neo4j_refusal_fail_closed() -> None:
    report = _load_application_recovery_report_v2()

    assert report["schema_version"] == "r1-recovered-application-attempt:v2"
    assert report["run_reference"] == "run:issue509-application-canary-002"
    assert report["target_commit"] == "b7144457dc5c1a02780cb7bd3120578bdc23da29"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["projection_attempt"] == {
        "outcome": "failed_closed",
        "safe_reason_code": "neo4j_multi_database_unsupported",
        "provider_attempted": True,
        "llm_connection_check": "verified",
        "embedding_connection_check": "verified",
        "projection_receipt_count": 0,
        "projection_snapshot_count": 0,
        "projection_activation_count": 0,
        "publication_status": "unpublished",
    }
    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "publication_unavailable",
        "authorized_source_read": "not_verified",
    }
    assert report["safety"] == {
        "synthetic_content_only": True,
        "serving_activated": False,
        "cutover_performed": False,
        "runtime_connect_revoked": True,
        "temporary_operator_auth_rotated": True,
        "temporary_auth_volume_removed": True,
        "target_stopped_after_capture": True,
    }
    assert report["independent_review"] == "required"


def test_issue509_application_recovery_v2_contains_only_safe_metadata() -> None:
    report = _load_application_recovery_report_v2()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_recovery_v3_records_canonical_refusal_fail_closed() -> None:
    report = _load_application_recovery_report_v3()

    assert report["schema_version"] == "r1-recovered-application-attempt:v3"
    assert report["run_reference"] == "run:issue509-application-canary-003"
    assert report["target_commit"] == "9c37fdc20a86e7afd9dd2d973d58ecb93194b610"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["projection_attempt"] == {
        "outcome": "failed_closed",
        "safe_reason_code": "projection_request_integrity_conflict",
        "provider_attempted": True,
        "llm_connection_check": "verified",
        "embedding_connection_check": "verified",
        "physical_projection": "completed",
        "graph_handler": "neo4j_community",
        "graph_dataset_isolation": "verified",
        "projection_receipt_count": 0,
        "projection_snapshot_count": 0,
        "projection_activation_count": 0,
        "active_projection_count": 0,
        "publication_status": "unpublished",
    }
    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "publication_unavailable",
        "authorized_source_read": "not_verified",
        "application_read_count": 0,
    }
    assert report["safety"] == {
        "synthetic_content_only": True,
        "backend_access_control_enabled": True,
        "postgresql_tls_required": True,
        "serving_activated": False,
        "cutover_performed": False,
        "runtime_connect_revoked": True,
        "temporary_operator_auth_rotated": True,
        "temporary_auth_volume_removed": True,
        "temporary_graph_removed": True,
        "target_stopped_after_capture": True,
    }
    assert report["independent_review"] == "required"


def test_issue509_application_recovery_v3_contains_only_safe_metadata() -> None:
    report = _load_application_recovery_report_v3()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_recovery_v4_records_publication_and_closed_canary() -> None:
    report = _load_application_recovery_report_v4()

    assert report["schema_version"] == "r1-recovered-application-attempt:v4"
    assert report["run_reference"] == "run:issue509-application-canary-004"
    assert report["target_commit"] == "12693cd9fc3a857e1e196d00d8ed4eec4680e237"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["projection_attempt"]["outcome"] == "completed"
    assert report["projection_attempt"]["canonical_handoff"] == "completed"
    assert report["projection_attempt"]["publication_status"] == "published"
    assert report["projection_attempt"]["projection_receipt_count"] == 1
    assert report["projection_attempt"]["projection_snapshot_count"] == 1
    assert report["projection_attempt"]["projection_activation_count"] == 1
    assert report["application_canary"] == {
        "outcome": "not_executed",
        "safe_reason_code": "qualification_authority_expired",
        "authorized_source_read": "not_verified",
        "application_read_count": 0,
        "authority_mutation_attempted": False,
    }
    assert report["safety"]["runtime_connect_revoked"] is True
    assert report["safety"]["temporary_graph_removed"] is True
    assert report["safety"]["target_stopped_after_capture"] is True
    assert report["independent_review"] == "required"


def test_issue509_application_recovery_v4_contains_only_safe_metadata() -> None:
    report = _load_application_recovery_report_v4()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_recovery_v5_records_profile_resolution_fail_closed() -> None:
    report = _load_application_recovery_report_v5()

    assert report["schema_version"] == "r1-recovered-application-attempt:v5"
    assert report["run_reference"] == "run:issue509-application-canary-005"
    assert report["target_commit"] == "13457e0877d05dd21991b2e730fa7304648a9591"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["projection_attempt"]["publication_status"] == "published"
    assert report["projection_attempt"]["projection_receipt_count"] == 1
    assert report["application_canary"] == {
        "outcome": "failed_closed",
        "safe_reason_code": "profile_unavailable",
        "broker_request_count": 1,
        "authorized_source_read": "not_verified",
        "permitted_source_read_count": 0,
        "context_bundle_issued": False,
        "temporary_authority_created": True,
        "temporary_authority_revoked": True,
    }
    assert report["diagnostic_boundary"] == {
        "source_retrieval_reached": False,
        "failure_stage": "context_profile_resolution",
        "missing_request_scope_initialization": "identified",
    }
    assert report["safety"]["runtime_connect_revoked"] is True
    assert report["safety"]["temporary_authority_revoked"] is True
    assert report["safety"]["temporary_graph_removed"] is True
    assert report["safety"]["target_stopped_after_capture"] is True


def test_issue509_application_recovery_v5_contains_only_safe_metadata() -> None:
    report = _load_application_recovery_report_v5()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_recovery_v6_records_corrected_preflight_and_fail_closed() -> None:
    report = _load_application_recovery_report_v6()

    assert report["schema_version"] == "r1-recovered-application-attempt:v6"
    assert report["run_reference"] == "run:issue509-application-canary-006"
    assert report["target_commit"] == "a7c3f8de4aa8cfd49d0611fceb2fc6858a723638"
    assert report["decision"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["projection_attempt"]["publication_status"] == "published"
    assert report["projection_attempt"]["projection_receipt_count"] == 1
    assert report["projection_attempt"]["projection_snapshot_count"] == 1
    assert report["projection_attempt"]["projection_activation_count"] == 1
    assert report["application_canary"] == {
        "outcome": "failed_closed",
        "safe_reason_code": "profile_unavailable",
        "broker_request_count": 1,
        "authorized_source_read": "not_verified",
        "permitted_source_read_count": 0,
        "context_bundle_issued": False,
        "temporary_authority_created": True,
        "temporary_authority_revoked": True,
    }
    assert report["diagnostic_boundary"] == {
        "offline_request_scope_preflight": "verified",
        "profile_document_integrity": "verified",
        "source_retrieval_reached": False,
        "failure_stage": "context_profile_resolution",
        "remaining_root_cause": "not_verified",
    }
    assert report["safety"]["runtime_connect_revoked"] is True
    assert report["safety"]["temporary_authority_revoked"] is True
    assert report["safety"]["temporary_graph_removed"] is True
    assert report["safety"]["target_stopped_after_capture"] is True


def test_issue509_application_recovery_v6_contains_only_safe_metadata() -> None:
    report = _load_application_recovery_report_v6()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_recovery_diagnostic_proves_operation_mismatch() -> None:
    report = _load_application_recovery_diagnostic()

    assert report["schema_version"] == "r1-recovered-application-diagnostic:v1"
    assert report["source_attempt_reference"] == "run:issue509-application-canary-003"
    assert report["decision"] == "verified_root_cause"
    assert report["production_qualified"] is False
    assert report["provider_call_count"] == 0
    assert report["failure_control"] == {
        "trusted_operation": "projection_result",
        "mapping_outcome": "failed_closed",
        "safe_reason_code": "projection_request_integrity_conflict",
        "projection_receipt_count": 0,
    }
    assert report["causal_control"] == {
        "trusted_operation": "projection_receipt",
        "mapping_outcome": "completed",
        "safe_reason_code": "none",
        "projection_receipt_count": 1,
        "source_reference_count": 3,
        "receipt_claim_count": 1,
    }
    assert report["source_target_mutated"] is False
    assert report["diagnostic_clone_disposable"] is True
    assert report["independent_review"] == "required"


def test_issue509_application_recovery_diagnostic_contains_only_safe_metadata() -> None:
    report = _load_application_recovery_diagnostic()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_neo4j_runtime_compatibility_records_real_handler_probe() -> None:
    report = _load_neo4j_runtime_compatibility_report()

    assert report == {
        "schema_version": "r1-cognee-graph-runtime-compatibility:v1",
        "run_reference": "run:issue509-neo4j-compatibility-001",
        "target_commit": "d152ddad63dd9c56ef810e34a8b1149fe2879e9e",
        "completed_at": "2026-10-27T19:43:44Z",
        "decision": "verified_configuration",
        "production_qualified": False,
        "immutable": True,
        "content_free": True,
        "failed_placement": {
            "execution_mode": "container",
            "handler": "neo4j",
            "server_edition": "community",
            "safe_reason_code": "neo4j_multi_database_unsupported",
        },
        "compatible_placement": {
            "execution_mode": "host",
            "handler": "neo4j_community",
            "server_edition": "community",
            "backend_access_control": "enabled",
            "dataset_isolation": "verified",
            "bolt_readiness": "verified",
            "cleanup": "verified",
        },
        "provider_call_count": 0,
        "application_projection_retried": False,
        "remaining_conditions": [
            "fresh_recovered_application_target_required",
            "new_provider_run_authorization_required",
            "provider_backed_projection_and_publication_not_verified",
            "authorized_application_source_read_not_verified",
            "independent_final_evidence_review_required",
        ],
    }


def test_issue509_neo4j_runtime_compatibility_contains_only_safe_metadata() -> None:
    report = _load_neo4j_runtime_compatibility_report()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_runbook_pins_safe_graph_handler_placement() -> None:
    runbook = (
        PROJECT_ROOT / "deploy/qualification/issue-509/README.md"
    ).read_text(encoding="utf-8")

    for required in (
        "GRAPH_DATASET_DATABASE_HANDLER=neo4j_community",
        "host-side qualification runner",
        "Neo4j Enterprise or AuraDB",
        "ENABLE_BACKEND_ACCESS_CONTROL=false",
        "issue-509-neo4j-runtime-compatibility-v1.json",
    ):
        assert required in runbook


def test_issue509_runbook_pins_projection_receipt_trusted_operation() -> None:
    runbook = (
        PROJECT_ROOT / "deploy/qualification/issue-509/README.md"
    ).read_text(encoding="utf-8")

    assert "_projection_receipt_context" in runbook
    assert "ProjectionReceiptCommand requires `projection_receipt`" in runbook
    assert "must not issue `projection_result`" in runbook


def test_issue509_temporal_services_use_the_pinned_image_dynamic_config() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert compose.count(
        "DYNAMIC_CONFIG_FILE_PATH: /etc/temporal/config/dynamicconfig/docker.yaml"
    ) == 2
    assert "DYNAMIC_CONFIG_FILE_PATH: config/dynamicconfig/development-sql.yaml" not in compose


def test_issue509_temporal_self_healthcheck_uses_loopback_and_clients_use_service_dns() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert compose.count('BIND_ON_IP: "0.0.0.0"') == 2
    assert compose.count("TEMPORAL_ADDRESS: 127.0.0.1:7233") == 2
    assert compose.count("cluster health --address 127.0.0.1:7233") == 2
    assert "SPINE_TEMPORAL_ADDRESS: source-temporal:7233" in compose
    assert "SPINE_TEMPORAL_ADDRESS: restore-temporal:7233" in compose


def test_issue509_readiness_report_binds_blocked_runtime_observation() -> None:
    report = _load_readiness_report()

    assert report["report_schema"] == "spine.issue-509.local-readiness:v2"
    assert report["issue"] == 509
    assert re.fullmatch(r"[0-9a-f]{40}", report["target_commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", report["deployment_fingerprint"])
    assert report["production_qualified"] is False
    assert report["readiness"]["configuration"] == "PASS"
    assert report["readiness"]["runtime_reachability"] == "BLOCKED"
    assert report["readiness"]["decision"] == "BLOCKED"
    assert report["readiness"]["authorization"] == "NOT_VERIFIED"


def test_issue509_readiness_report_records_neo4j_and_temporal_observations() -> None:
    report = _load_readiness_report()

    assert report["observations"]["source_neo4j"]["status"] == "PASS"
    assert report["observations"]["restore_neo4j"]["status"] == "PASS"
    assert report["observations"]["source_temporal"]["cluster_health"] == "NOT_VERIFIED"
    assert report["observations"]["restore_temporal"]["status"] == "NOT_VERIFIED"


def test_issue509_readiness_report_keeps_recovery_checks_unverified() -> None:
    report = _load_readiness_report()

    assert all(value == "NOT_VERIFIED" for key, value in report["readiness"].items() if key in {
        "backup",
        "restore",
        "integrity_reconciliation",
        "projection_recovery",
        "authorized_canary",
        "safe_diagnostics",
    })


def test_issue509_readiness_report_does_not_claim_physical_qualification() -> None:
    report = _load_readiness_report()

    assert report["qualification_claims"] == {
        "physical_backup": False,
        "isolated_restore": False,
        "projection_recovery": False,
        "authorized_canary": False,
        "production_readiness": False,
    }


def test_issue509_readiness_report_contains_only_safe_metadata() -> None:
    report = _load_readiness_report()
    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def _temporal_readiness_report() -> dict[str, Any]:
    return json.loads(TEMPORAL_READINESS_REPORT_PATH.read_text(encoding="utf-8"))


def _runtime_composition_report() -> dict[str, Any]:
    return json.loads(RUNTIME_COMPOSITION_REPORT_PATH.read_text(encoding="utf-8"))


def test_issue509_runtime_composition_report_is_bound_to_commit_and_compose() -> None:
    report = _runtime_composition_report()

    assert report["report_schema"] == "spine.issue-509.runtime-composition-readiness:v1"
    assert report["issue"] == 509
    assert re.fullmatch(r"[a-f0-9]{40}", report["target_commit"])
    assert report["deployment_fingerprint"] == hashlib.sha256(
        COMPOSE_PATH.read_bytes()
    ).hexdigest()
    assert report["production_qualified"] is False


def test_issue509_runtime_composition_report_separates_worker_test_from_deployment() -> None:
    report = _runtime_composition_report()

    assert report["observations"]["worker_factory_integration"] == {
        "status": "PASS",
        "environment": "Temporal SDK test server",
        "workflow_outcome": "ABSTAINED",
        "deployed_application_readiness": False,
    }
    assert report["readiness"]["deployed_worker_pollers"] == "NOT_VERIFIED"
    assert report["readiness"]["application_api"] == "NOT_VERIFIED"
    assert report["readiness"]["decision"] == "BLOCKED"


def test_issue509_runtime_composition_report_keeps_all_physical_recovery_unverified() -> None:
    report = _runtime_composition_report()

    assert all(
        report["readiness"][criterion] == "NOT_VERIFIED"
        for criterion in (
            "backup",
            "restore",
            "integrity_reconciliation",
            "projection_recovery",
            "authorized_canary",
        )
    )
    assert report["qualification_claims"] == {
        "deployed_api_worker": False,
        "trusted_authorization": False,
        "physical_backup": False,
        "isolated_restore": False,
        "projection_recovery": False,
        "authorized_canary": False,
        "production_readiness": False,
    }


def test_issue509_runtime_composition_report_contains_only_safe_metadata() -> None:
    report = _runtime_composition_report()

    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|\s)/(?:home|tmp|var|etc|mnt|root)/", text)


def _temporal_readiness_report_v2() -> dict[str, Any]:
    return json.loads(TEMPORAL_READINESS_REPORT_V2_PATH.read_text(encoding="utf-8"))


def test_issue509_temporal_readiness_report_is_bound_to_deployment_fingerprint() -> None:
    report = _temporal_readiness_report()
    assert report["report_schema"] == "spine.issue-509.temporal-readiness:v1"
    assert report["target_commit"] == "d6db466b93a330c24a392dcf62e21dcee9008f52"
    assert report["production_qualified"] is False
    assert report["decision"] == "BLOCKED"
    assert re.fullmatch(r"[a-f0-9]{64}", report["deployment_fingerprint"])
    assert _fingerprint(report["deployment_fingerprint_material"]) == report[
        "deployment_fingerprint"
    ]


def test_issue509_temporal_profile_fingerprints_match_profile_material() -> None:
    report = _temporal_readiness_report()
    for side in ("source", "restore"):
        profile = report["profiles"][side]
        assert re.fullmatch(r"[a-f0-9]{64}", profile["fingerprint"])
        assert _fingerprint(profile["fingerprint_material"]) == profile["fingerprint"]


def test_issue509_temporal_services_and_namespaces_are_ready_in_both_profiles() -> None:
    report = _temporal_readiness_report()
    for side in ("source", "restore"):
        profile = report["profiles"][side]
        assert profile["postgres_health"] == "PASS"
        assert profile["persistence_schema"] == "PASS"
        assert profile["temporal_docker_health"] == "healthy"
        assert profile["cluster_health_rpc"] == "SERVING"
        assert profile["namespace"] == "REGISTERED"
        assert profile["network_isolation"] == "PASS"


def test_issue509_temporal_synthetic_workflow_probe_reaches_terminal_status() -> None:
    report = _temporal_readiness_report()
    for side in ("source", "restore"):
        profile = report["profiles"][side]
        assert profile["synthetic_workflow_admission"] == "PASS"
        assert profile["synthetic_workflow_terminal_status"] == "TERMINATED"


def test_issue509_temporal_report_does_not_claim_application_worker_readiness() -> None:
    report = _temporal_readiness_report()
    for side in ("source", "restore"):
        profile = report["profiles"][side]
        assert profile["active_worker_pollers"] == 0
        assert profile["application_worker_readiness"] == "NOT_VERIFIED"


def test_issue509_temporal_report_keeps_unverified_application_gaps_explicit() -> None:
    report = _temporal_readiness_report()
    assert report["application_authorization"] == "NOT_VERIFIED"
    assert report["recovery_qualification"] == "NOT_STARTED"


def test_issue509_temporal_readiness_report_contains_only_safe_metadata() -> None:
    report = _temporal_readiness_report()
    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_temporal_readiness_report_v2_binds_post_merge_deployment() -> None:
    report = _temporal_readiness_report_v2()

    assert report["report_schema"] == "spine.issue-509.temporal-readiness:v2"
    assert report["target_commit"] == "a8565fd86cc2c8c56158d22a5664733c05f138d2"
    assert report["decision"] == "BLOCKED"
    assert report["production_qualified"] is False
    assert _fingerprint(report["deployment_fingerprint_material"]) == report[
        "deployment_fingerprint"
    ]
    for side in ("source", "restore"):
        profile = report["profiles"][side]
        assert _fingerprint(profile["fingerprint_material"]) == profile["fingerprint"]


def test_issue509_temporal_readiness_report_v2_binds_current_profile_sources() -> None:
    report = _temporal_readiness_report_v2()
    source_files = report["deployment_fingerprint_material"]["source_files"]
    paths = {
        "ignore_rules": PROJECT_ROOT / ".dockerignore",
        "compose_config": COMPOSE_PATH,
        "application_image_recipe": QUALIFICATION_DOCKERFILE_PATH,
        "persistence_image_recipe": PROJECT_ROOT
        / "deploy/qualification/issue-509/postgres/Dockerfile",
        "persistence_init_script": PROJECT_ROOT
        / "deploy/qualification/issue-509/postgres/init-qualification.sh",
    }

    assert set(source_files) == set(paths)
    for key, path in paths.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source_files[key]


def test_issue509_temporal_readiness_report_v2_records_only_observed_server_states() -> None:
    report = _temporal_readiness_report_v2()
    for side in ("source", "restore"):
        profile = report["profiles"][side]
        assert profile["postgres_container_health"] == "healthy"
        assert profile["postgres_tcp_readiness"] == "PASS"
        assert profile["temporal_container_health"] == "healthy"
        assert profile["cluster_health_rpc"] == "SERVING"
        assert profile["namespace"] == "REGISTERED"
        assert profile["synthetic_workflow_probe"] == "NOT_VERIFIED"
        assert profile["active_application_worker_pollers"] == 0
        assert profile["application_worker_readiness"] == "NOT_VERIFIED"
        assert profile["network_isolation"] == "NOT_VERIFIED"

    assert report["application_api_readiness"] == "NOT_VERIFIED"
    assert report["application_authorization"] == "NOT_VERIFIED"
    assert report["backup"] == "NOT_STARTED"
    assert report["isolated_restore"] == "NOT_STARTED"
    assert report["integrity_reconciliation"] == "NOT_STARTED"
    assert report["physical_projection_recovery"] == "NOT_STARTED"
    assert report["authorized_canary"] == "NOT_STARTED"
    assert report["safe_diagnostics"] == "NOT_VERIFIED"


def test_issue509_temporal_readiness_report_v2_contains_only_safe_metadata() -> None:
    report = _temporal_readiness_report_v2()
    for text in _all_strings(report):
        lowered = text.lower()
        assert not any(token in lowered for token in FORBIDDEN_REPORT_TOKENS)
        assert "\\" not in text
        assert not re.search(r"(?:^|[^:])/(?:[^/]|$)", text)


def test_issue509_application_build_context_is_default_deny_and_allowlisted() -> None:
    dockerignore = set(DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines())
    dockerfile = QUALIFICATION_DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "**" in dockerignore
    assert {line for line in dockerignore if line.startswith("!")} == (
        QUALIFICATION_BUILD_CONTEXT_ALLOWLIST
    )
    for required_copy in (
        "COPY pyproject.toml uv.lock README.md ./",
        "COPY alembic.ini ./",
        "COPY migrations ./migrations",
        "COPY src ./src",
    ):
        assert required_copy in dockerfile
