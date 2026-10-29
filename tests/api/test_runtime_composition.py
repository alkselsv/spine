from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

import spine.api.runtime as runtime_module
from spine.api.authorization import ApiAuthorizationRuntime
from spine.api.runtime import DeployableApiSettings, create_production_app
from spine.application.production_qa_deployment import (
    ProductionQAADeploymentProfile,
    ProductionQAAProfileMismatch,
)
from spine.application.diagnostics import AuditEventRegistry, DiagnosticEventRegistry
from spine.application.ingestion_history import IngestionHistoryPage
from spine.auth import InMemoryAuthorizationDirectory, RoutePolicyRegistry
from spine.infrastructure.auth import (
    InteractiveClientQualification,
    LocalAuthenticationSettings,
    OIDCAuthenticationSettings,
)
from spine.infrastructure.auth import RequestAuthorizationComposer, TrustedRequestContextBoundary
from spine.infrastructure.db.readiness import load_migration_inventory
from spine.infrastructure.db.settings import RuntimeDatabaseSettings
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    NOW,
    PERSISTENCE_ISSUER_ID,
    REQUEST_ISSUER_ID,
    WORKSPACE_ID,
    ImmediateAuditGate,
    StaticAuthentication,
    authorization_entry,
    authorization_ready,
)


class EmptyIngestionHistoryService:
    async def list_runs(self, *, context: object, limit: int, after: object | None):
        del context, limit, after
        return IngestionHistoryPage(items=(), has_more=False)

    async def list_processing_history(
        self,
        *,
        context: object,
        limit: int,
        after: object | None,
    ):
        del context, limit, after
        return IngestionHistoryPage(items=(), has_more=False)


def _production_app(*, profile: ProductionQAADeploymentProfile | None = None):
    return create_production_app(
        database_settings=RuntimeDatabaseSettings(
            url="postgresql+psycopg://spine_runtime:secret@postgres/spine"
        ),
        authentication_settings=LocalAuthenticationSettings(
            deployment_mode="development",
            configuration_version="issue-508-local",
            authentication_alias_issuer="https://local.spine.invalid",
            authentication_alias_subject="issue-508-operator",
            bearer_credential="issue-508-local-credential-000000000000",
        ),
        api_settings=DeployableApiSettings(
            cursor_secret="issue-508-cursor-secret-00000000000000000000",
            build_version="issue-508-test",
            ingestion_service_principal_id=UUID(
                "50800000-0000-4000-8000-000000000002"
            ),
            qa_service_principal_id=UUID(
                "50800000-0000-4000-8000-000000000004"
            ),
            original_storage_root="/tmp/spine-test-originals",
            commit_sha="b" * 40,
            database_profile_id="spine-source-postgresql-17",
            scope_policy_id="workspace-environment-v1",
            ingestion_service_alias_issuer="https://local.spine.invalid",
            ingestion_service_alias_subject="issue-508-ingestion",
            qa_capture_worker_credential="worker-capture-credential-for-tests-0001",
            qa_capture_observer_credential="observer-capture-credential-for-tests-0001",
            temporal_cluster_profile_id="local-temporal-v1",
            capture_service_profile_id="api-qa-capture-v1",
            capture_service_url="http://api:8080",
            projection_profile_id="r1-cognee-local-v1",
            model_profile_id="r1-provider-local-v1",
        ),
        deployment_profile=profile or _deployment_profile(),
    )


def _deployment_profile(**overrides: object) -> ProductionQAADeploymentProfile:
    values: dict[str, object] = {
        "profile_id": "spine-r1-local",
        "profile_version": "2026.10.22",
        "deployment_fingerprint": "a" * 64,
        "release_identity": "issue-508-test",
        "commit_sha": "b" * 40,
        "database_profile_id": "spine-source-postgresql-17",
        "migration_revision": load_migration_inventory().supported_head,
        "authentication_profile_id": "issue-508-local",
        "authentication_mode": "local_development",
        "scope_policy_id": "workspace-environment-v1",
        "temporal_cluster_profile_id": "local-temporal-v1",
        "temporal_namespace": "default",
        "temporal_task_queue": "spine-r1-qa",
        "workflow_contract_version": "spine.qa.ask-admission:v1",
        "capture_service_profile_id": "api-qa-capture-v1",
        "capture_worker_workload_id": "spine-qa-worker",
        "capture_observer_workload_id": "spine-qa-observer",
        "capture_contract_version": "r1-qa-observer-capture:v1",
        "projection_profile_id": "r1-cognee-local-v1",
        "model_profile_id": "r1-provider-local-v1",
    }
    values.update(overrides)
    return ProductionQAADeploymentProfile.model_validate(values)


def _authorization_runtime() -> ApiAuthorizationRuntime:
    diagnostic_registry = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_registry)
    persistence_boundary = TrustedContextBoundary.for_testing(
        issuer_id=PERSISTENCE_ISSUER_ID,
        secret=b"issue-538-persistence-context-secret",
    )
    request_boundary = TrustedRequestContextBoundary.for_testing(
        persistence_boundary=persistence_boundary,
        issuer_id=REQUEST_ISSUER_ID,
        secret=b"issue-538-request-context-secret-0001",
    )
    route_policies = RoutePolicyRegistry()
    composer = RequestAuthorizationComposer(
        authentication=StaticAuthentication(),
        authorization_directory=InMemoryAuthorizationDirectory(
            entries=(authorization_entry(),)
        ),
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=AuditEventRegistry.with_default_families(),
        audit_gate=ImmediateAuditGate(),
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=lambda: NOW,
        request_id_source=lambda: UUID("70000000-0000-0000-0000-000000000538"),
    )
    return ApiAuthorizationRuntime(
        composer=composer,
        route_policies=route_policies,
        readiness_probe=authorization_ready,
    )


def _fault_profile_json() -> str:
    return json.dumps(
        {
            "schema_version": "spine-ingestion-fault-profile:v1",
            "profile_id": "qualification-issue577",
            "profile_version": "v1",
            "deployment_profile_id": "spine-r1-local",
            "deployment_profile_version": "2026.10.22",
            "deployment_fingerprint": "a" * 64,
            "rules": [
                {
                    "workspace_id": str(WORKSPACE_ID),
                    "environment_id": str(ENVIRONMENT_ID),
                    "command_identity": "api.document.upload." + "a" * 32,
                    "boundary": "parser",
                    "reason_code": "parser_retryable",
                }
            ],
        }
    )


def test_production_app_enables_exact_bound_ingestion_fault_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPINE_INGESTION_FAULT_PROFILE_JSON", _fault_profile_json())
    original_build = runtime_module.build_production_ingestion
    captured: dict[str, bool] = {}

    def capture_fault_controller(**arguments: object):
        controller = arguments.get("fault_controller")
        captured["enabled"] = bool(
            controller is not None and getattr(controller, "enabled", False)
        )
        return original_build(**arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(
        runtime_module,
        "build_production_ingestion",
        capture_fault_controller,
    )

    application = _production_app()

    assert application.state.ingestion_fault_profile_enabled is True
    assert captured == {"enabled": True}


def test_production_app_rejects_fault_profile_for_oidc_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPINE_INGESTION_FAULT_PROFILE_JSON", _fault_profile_json())
    baseline = _production_app()
    oidc_settings = OIDCAuthenticationSettings(
        configuration_version="issue-577-oidc",
        issuer="https://identity.example.test/tenant",
        audience="https://api.spine.example.test",
        qualified_interactive_clients=(
            InteractiveClientQualification(
                client_id="spine-web",
                configuration_version="issue-577-oidc",
                issuer="https://identity.example.test/tenant",
                audience="https://api.spine.example.test",
                client_credentials_for_audience_disabled=True,
                evidence_reference="qualification-fixture",
            ),
        ),
    )
    api_settings = baseline.state.production_api_settings.model_copy(
        update={"authentication_mode": "oidc"}
    )
    profile = _deployment_profile(
        authentication_profile_id="issue-577-oidc",
        authentication_mode="oidc",
    )

    with pytest.raises(ValueError) as raised:
        create_production_app(
            database_settings=baseline.state.production_database_settings,
            authentication_settings=oidc_settings,
            api_settings=api_settings,
            deployment_profile=profile,
        )

    assert str(raised.value) == "INGESTION_FAULT_PROFILE_FORBIDDEN"


def _production_app_with_empty_ingestion_history(
    monkeypatch: pytest.MonkeyPatch,
):
    authorization = _authorization_runtime()
    monkeypatch.setattr(
        runtime_module,
        "compose_local_api_authorization",
        lambda **_: authorization,
    )
    monkeypatch.setattr(
        runtime_module,
        "OrchestratedIngestionHistoryService",
        lambda _uow_factory: EmptyIngestionHistoryService(),
    )
    return _production_app()


def test_production_app_exposes_protected_canonical_document_surface() -> None:
    application = _production_app()

    paths = set(application.openapi()["paths"])

    assert "/api/v1/documents" in paths
    assert "/api/v1/documents/{source_object_id}" in paths
    assert "/api/v1/documents/{source_object_id}/revisions" in paths
    assert "/api/v1/documents/{source_object_id}/replacement" in paths
    assert "/api/v1/documents/{source_object_id}/tombstone" in paths
    assert "/api/v1/documents/lifecycle/operations/{operation_id}" in paths
    assert "/api/v1/documents/batches" in paths
    assert "/api/v1/documents/processing-history" in paths
    assert "/api/v1/ingestion/runs" in paths
    assert "/api/v1/ingestion/runs/{run_id}" in paths
    assert (
        "/api/v1/knowledge/documents/{source_object_id}/revisions/"
        "{source_revision_id}/parsed-content"
    ) in paths
    assert "/api/v1/projections/{projection_kind}/status" in paths
    assert "/api/v1/projections/{projection_kind}/recovery" in paths
    assert "/api/v1/source-connections" in paths
    assert "/api/v1/source-connections/{connection_id}" in paths
    assert "/api/v1/qa/sessions" in paths
    assert "/api/v1/qa/sessions/{session_id}" in paths
    assert (
        "/api/v1/qa/sessions/{session_id}/messages/{message_id}/events" in paths
    )
    assert "/api/v1/ask/admissions" in paths
    assert "/api/v1/qa/runs" in paths
    assert "/api/v1/qa/runs/{run_id}" in paths
    assert "/api/v1/qa/feedback" in paths
    assert "/api/v1/qa/citations/{citation_id}" in paths
    assert application.state.document_ingestion_service is not None
    assert application.state.batch_document_ingestion_service is not None
    assert application.state.parsed_content_inspection_service is not None
    assert application.state.document_lifecycle_command_service is not None
    assert application.state.projection_status_service is not None
    assert application.state.projection_recovery_service is not None
    assert application.state.qa_run_history_service is not None
    assert application.state.feedback_service is not None
    assert application.state.citation_inspector_service is not None
    assert application.state.qa_stream_service is not None


@pytest.mark.asyncio
async def test_production_app_serves_supported_runs_screen_processing_history_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _production_app_with_empty_ingestion_history(monkeypatch)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/documents/processing-history?limit=25",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["limit"] == 25


@pytest.mark.asyncio
async def test_production_app_serves_ingestion_runs_collection_for_authorized_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _production_app_with_empty_ingestion_history(monkeypatch)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/ingestion/runs?limit=25",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["limit"] == 25
    assert response.json()["workspace_id"] == str(WORKSPACE_ID)
    assert response.json()["environment_id"] == str(ENVIRONMENT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected_status", "protected_identifiers"),
    [
        (
            {
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
            401,
            (str(WORKSPACE_ID), str(ENVIRONMENT_ID)),
        ),
        (
            {
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": "10000000-0000-4000-8000-000000000099",
                "X-Environment-ID": "20000000-0000-4000-8000-000000000099",
            },
            403,
            (
                "10000000-0000-4000-8000-000000000099",
                "20000000-0000-4000-8000-000000000099",
            ),
        ),
    ],
    ids=["missing-bearer", "foreign-scope"],
)
async def test_production_app_rejects_unauthorized_ingestion_runs_collection(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    expected_status: int,
    protected_identifiers: tuple[str, str],
) -> None:
    application = _production_app_with_empty_ingestion_history(monkeypatch)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/ingestion/runs", headers=headers)

    assert response.status_code == expected_status
    assert all(identifier not in response.text for identifier in protected_identifiers)


@pytest.mark.asyncio
async def test_production_app_exposes_authenticated_ephemeral_capture_route() -> None:
    application = _production_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/internal/qa-capture/subscriptions",
            json={"capture_id": str(UUID(int=9001)), "ttl_ms": 5000},
        )
        accepted = await client.post(
            "/internal/qa-capture/subscriptions",
            headers={"Authorization": "Bearer observer-capture-credential-for-tests-0001"},
            json={"capture_id": str(UUID(int=9001)), "ttl_ms": 5000},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["capture_id"] == str(UUID(int=9001))
    assert "/internal/qa-capture/subscriptions" not in application.openapi()["paths"]


def test_production_app_rejects_profile_release_mismatch_without_values() -> None:
    profile = _deployment_profile(release_identity="other-release")

    with pytest.raises(ProductionQAAProfileMismatch) as error:
        create_production_app(
            database_settings=RuntimeDatabaseSettings(
                url="postgresql+psycopg://spine_runtime:secret@postgres/spine"
            ),
            authentication_settings=LocalAuthenticationSettings(
                deployment_mode="development",
                configuration_version="issue-508-local",
                authentication_alias_issuer="https://local.spine.invalid",
                authentication_alias_subject="issue-508-operator",
                bearer_credential="issue-508-local-credential-000000000000",
            ),
            api_settings=DeployableApiSettings(
                cursor_secret="issue-508-cursor-secret-00000000000000000000",
                build_version="issue-508-test",
                ingestion_service_principal_id=UUID("50800000-0000-4000-8000-000000000002"),
                qa_service_principal_id=UUID("50800000-0000-4000-8000-000000000004"),
                original_storage_root="/tmp/spine-test-originals",
                commit_sha="b" * 40,
            database_profile_id="spine-source-postgresql-17",
            scope_policy_id="workspace-environment-v1",
            ingestion_service_alias_issuer="https://local.spine.invalid",
            ingestion_service_alias_subject="issue-508-ingestion",
                qa_capture_worker_credential="worker-capture-credential-for-tests-0001",
                qa_capture_observer_credential="observer-capture-credential-for-tests-0001",
                temporal_cluster_profile_id="local-temporal-v1",
                capture_service_profile_id="api-qa-capture-v1",
                capture_service_url="http://api:8080",
                projection_profile_id="r1-cognee-local-v1",
                model_profile_id="r1-provider-local-v1",
            ),
            deployment_profile=profile,
        )

    assert error.value.reason_code == "PROFILE_MISMATCH"
    assert "other-release" not in str(error.value)


def test_production_app_rejects_authentication_mode_drift() -> None:
    with pytest.raises(ProductionQAAProfileMismatch) as error:
        _production_app(profile=_deployment_profile(authentication_mode="oidc"))

    assert error.value.reason_code == "AUTHORITY_BINDING_MISMATCH"


@pytest.mark.asyncio
async def test_oidc_api_mode_rejects_local_development_authenticator() -> None:
    application = _production_app()
    oidc_api_settings = application.state.production_api_settings.model_copy(
        update={"authentication_mode": "oidc"}
    )

    with pytest.raises(ProductionQAAProfileMismatch) as error:
        runtime_module._validate_authentication_mode(
            oidc_api_settings,
            application.state.production_authentication_settings,
        )

    await application.state.production_engine.dispose()
    assert error.value.reason_code == "AUTHORITY_BINDING_MISMATCH"
