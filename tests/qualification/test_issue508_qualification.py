from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-508-governed-qa-browser-report.json"
LIVE_TEST_PATH = ROOT / "web/e2e/production/governed-qa.spec.ts"
LIVE_CONFIG_PATH = ROOT / "web/playwright.production.config.ts"
LIVE_INPUT_CONFIG_PATH = ROOT / "web/e2e/production/live-config.ts"
RUNBOOK_PATH = ROOT / "docs/runbooks/r1-governed-qa-browser-qualification.md"
COMPOSE_PATH = ROOT / "compose.yaml"
PYTHON_IMAGE_PATH = ROOT / "Dockerfile"
WEB_IMAGE_PATH = ROOT / "web/Dockerfile"
WEB_PROXY_PATH = ROOT / "docker/web/default.conf.template"
REQUIRED_SCENARIOS = {
    "upload_active_projection",
    "answer_citation_feedback_run",
    "duplicate_retry",
    "sse_reconnect",
    "session_restoration",
    "abstention",
    "cancellation",
    "two_workspace_isolation",
    "evidence_disclosure_rejection",
    "safe_ui_states",
}
REQUIRED_UI_STATES = {
    "loading",
    "empty",
    "partial",
    "stale",
    "degraded",
    "permission-denied",
    "terminal-failure",
}


def _report() -> dict[str, Any]:
    value = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_issue508_report_is_metadata_only_and_fail_closed() -> None:
    report = _report()

    assert report["schema_version"] == "issue-508-governed-qa-browser:v1"
    assert report["ticket"] == 508
    assert report["status"] in {"pass", "blocked", "not_verified"}
    assert set(report["required_scenarios"]) == REQUIRED_SCENARIOS
    assert set(report["scenarios"]) <= REQUIRED_SCENARIOS
    assert set(report["ui_states"]) <= REQUIRED_UI_STATES
    assert report["execution"] == {
        "api_mocking": False,
        "browser": "chromium",
        "cli_user_actions": False,
        "controlled_fixture": False,
        "post_admission_response_disconnect_injected": True,
        "route_interception": False,
    }
    assert report["privacy"] == {
        "artifact_no_leak_scan": "pass",
        "content_fields_recorded": False,
        "sentinel_scan": "pass",
        "screenshots": False,
        "trace": False,
        "video": False,
    }
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()
    assert "access_token" not in serialized
    assert "bearer " not in serialized


def test_issue508_pass_requires_complete_correlated_real_browser_evidence() -> None:
    report = _report()

    if report["status"] != "pass":
        assert report["reason_code"]
        assert report["citation_locator_validation"] in {"pass", "not_verified"}
        return

    assert set(report["scenarios"]) == REQUIRED_SCENARIOS
    assert set(report["ui_states"]) == REQUIRED_UI_STATES
    assert report["reason_code"] is None
    assert report["citation_locator_validation"] == "pass"
    assert len(report["scope"]["scope_references"]) == 2
    assert len({item["workspace_id"] for item in report["scope"]["scope_references"]}) == 2
    assert len({item["environment_id"] for item in report["scope"]["scope_references"]}) == 2
    deployment = report["deployment"]
    assert len(deployment["commit_sha"]) == 40
    assert len(deployment["deployment_fingerprint"]) == 64
    assert all(deployment[key] for key in (
        "api_release_identity",
        "provider_profile_identity",
        "web_release_identity",
    ))
    references = report["correlated_references"]
    assert references["admission_command_ids"]
    assert references["ask_run_ids"]
    assert references["cancellation_run_id"] in references["terminal_run_ids"]
    assert references["feedback_ids"]
    assert references["ingestion_run_id"]
    assert references["session_ids"]
    assert references["source_object_id"]
    assert references["source_revision_id"]
    assert references["terminal_run_ids"]
    assert references["trace_ids"]


def test_issue508_live_target_has_no_controlled_routes_or_content_artifacts() -> None:
    source = LIVE_TEST_PATH.read_text(encoding="utf-8")
    config = LIVE_CONFIG_PATH.read_text(encoding="utf-8")
    input_config = LIVE_INPUT_CONFIG_PATH.read_text(encoding="utf-8")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "page.route(" not in source
    assert "route.fulfill(" not in source
    assert "fixture=" not in source
    assert "controlled_fixture: false" in source
    assert "api_mocking: false" in source
    assert "post_admission_response_disconnect_injected: true" in source
    assert "route_interception: false" in source
    assert "permission_denied_state_not_visible" in source
    assert "'/api/v1/documents/batches'" in source
    assert "safeUuid(value.source_revision_id) === this.sourceRevisionId" in source
    assert "new Set(retryRuns).size !== 1" in source
    assert "this.askReplaysByKey.get(keys[0]!)?.includes(true)" in source
    assert "answer_admission_identity_not_observed" in source
    assert "citation_evidence_identity_not_observed" in source
    assert "trace_correlation_incomplete" in source
    assert "cancelled_terminal_run_not_correlated" in source
    assert "artifact_no_leak_scan_failed" in source
    assert "this.captureSensitiveValue(value.excerpt)" in source
    assert "this.captureSensitiveValue(body.data.answer)" in source
    assert "Array.isArray(body.items)" in source
    assert "noLeakSentinel" in source
    assert "cross_workspace_session_not_denied" in source
    assert "cross_workspace_source_not_denied" in source
    assert "workspace_b_citation_not_validated" in source
    assert "safeUuid(value.source_revision_id) === this.workspaceBSourceRevisionId" in source
    assert "production_api_surface_incomplete" in source
    assert "production_ingestion_surface_incomplete" in source
    assert "qualification_authority_missing" in source
    assert "trace: 'off'" in config
    assert "screenshot: 'off'" in config
    assert "video: 'off'" in config
    assert "webServer" not in config
    assert "qualification_configuration_missing:SPINE_E2E_WEB_BASE_URL" in config
    assert "127.0.0.1" not in config
    assert "loadEnv('qualification', repositoryRoot, '')" in config
    assert "process.env[name] === undefined" in config
    assert "docx|md|markdown|pdf|txt|xlsx" in input_config
    assert "corepack pnpm test:e2e:production" in runbook


def test_issue508_compose_profile_is_reproducible_and_fail_closed() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    python_image = PYTHON_IMAGE_PATH.read_text(encoding="utf-8")
    web_image = WEB_IMAGE_PATH.read_text(encoding="utf-8")
    web_proxy = WEB_PROXY_PATH.read_text(encoding="utf-8")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    for service in (
        "postgres:",
        "database-bootstrap:",
        "qualification-authority-provisioner:",
        "qualification-authority-verifier:",
        "temporal:",
        "temporal-ui:",
        "neo4j:",
        "api:",
        "qa-worker:",
        "web:",
    ):
        assert f"  {service}" in compose
    assert "image: pgvector/pgvector:0.8.7-pg17-bookworm" in compose
    assert "image: pgvector/pgvector:0.8.1-pg17-bookworm" not in compose
    assert "condition: service_healthy" in compose
    assert "condition: service_completed_successfully" in compose
    assert "127.0.0.1:${SPINE_WEB_PORT:-4175}:8080" in compose
    assert "./data/incoming/issue508:/app/data/incoming/issue508:ro" in compose
    assert "SPINE_E2E_ACCESS_TOKEN: ${SPINE_E2E_ACCESS_TOKEN}" in compose
    assert "SPINE_E2E_ACCESS_TOKEN=" not in compose
    shared_environment = compose.split("x-app-environment:", 1)[1].split("services:", 1)[0]
    assert "SPINE_LOCAL_AUTH_BEARER_CREDENTIAL" not in shared_environment
    assert "SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_ISSUER" not in shared_environment
    assert "SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_SUBJECT" not in shared_environment
    assert "SPINE_DISPATCHER_DATABASE_URL" not in shared_environment
    assert "SPINE_MIGRATION_DATABASE_URL" not in shared_environment
    assert "SPINE_API_INGESTION_SERVICE_PRINCIPAL_ID" not in shared_environment
    api_environment = compose.split("  api:", 1)[1].split("  outbox-dispatcher:", 1)[0]
    assert "SPINE_LOCAL_AUTH_BEARER_CREDENTIAL: ${SPINE_E2E_ACCESS_TOKEN}" in api_environment
    assert (
        "SPINE_API_INGESTION_SERVICE_ALIAS_ISSUER: "
        "${SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_ISSUER}"
    ) in api_environment
    assert (
        "SPINE_API_INGESTION_SERVICE_ALIAS_SUBJECT: "
        "${SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_SUBJECT}"
    ) in api_environment
    assert "SPINE_PROJECTION_WORKER_SERVICE_PRINCIPAL_ID" in compose
    assert "SPINE_QA_WORKER_SERVICE_PRINCIPAL_ID" in compose
    assert "SPINE_API_QA_SERVICE_PRINCIPAL_ID: ${SPINE_QA_WORKER_SERVICE_PRINCIPAL_ID}" in compose
    assert "SPINE_WORKER_SERVICE_PRINCIPAL_ID: ${SPINE_API_INGESTION_SERVICE_PRINCIPAL_ID}" not in compose
    assert "SPINE_QA_WORKER_SERVICE_PRINCIPAL_ID: ${SPINE_API_INGESTION_SERVICE_PRINCIPAL_ID}" not in compose
    dispatcher_environment = compose.split("  outbox-dispatcher:", 1)[1].split(
        "  qa-worker:", 1
    )[0]
    assert (
        "SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON: "
        "${SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON:-}"
    ) in dispatcher_environment
    assert 'CMD ["spine-api"]' in python_image
    assert "spine-qualification-bootstrap" not in compose
    assert "profiles: [\"qualification-provisioning\"]" in compose
    assert "spine-qualification-provision" in compose
    assert "spine-qualification-verify" in compose
    verifier = compose.split("  qualification-authority-verifier:", 1)[1].split(
        "  original-storage-bootstrap:", 1
    )[0]
    assert "SPINE_QUALIFICATION_RUNTIME_DATABASE_URL" in verifier
    assert "SPINE_QUALIFICATION_MIGRATION_DATABASE_URL" not in verifier
    assert 'command: ["spine-qa-worker"]' in compose
    assert 'SPINE_QA_WORKER_TEMPORAL_TASK_QUEUE: spine-r1-qa' in compose
    assert "SPINE_API_MODEL_PROFILE_ID: ${SPINE_QA_MODEL_PROFILE_ID:?" in compose
    assert "SPINE_QA_WORKER_MODEL_PROFILE_ID: ${SPINE_QA_MODEL_PROFILE_ID:?" in compose
    assert "SPINE_API_MODEL_PROFILE_ID: ${LLM_MODEL}" not in compose
    assert "SPINE_QA_WORKER_MODEL_PROFILE_ID: ${LLM_MODEL}" not in compose
    assert "uv sync --frozen --no-dev" in python_image
    assert "pnpm install --frozen-lockfile" in web_image
    assert "resolver 127.0.0.11" in web_proxy
    assert "proxy_pass $spine_api" in web_proxy
    assert "docker compose up --build --detach --wait" in runbook


def test_issue508_compose_passes_opt_in_ingestion_fault_profile_only_to_api() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    binding = (
        "SPINE_INGESTION_FAULT_PROFILE_JSON: "
        "${SPINE_INGESTION_FAULT_PROFILE_JSON:-}"
    )
    api = compose.split("  api:", 1)[1].split("  outbox-dispatcher:", 1)[0]

    assert binding in api
    assert compose.count(binding) == 1
