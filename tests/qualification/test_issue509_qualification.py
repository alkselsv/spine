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
