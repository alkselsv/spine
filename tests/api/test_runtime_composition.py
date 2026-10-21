from __future__ import annotations

from uuid import UUID

import httpx
import pytest

import spine.api.runtime as runtime_module
from spine.api.authorization import ApiAuthorizationRuntime
from spine.api.runtime import DeployableApiSettings, create_production_app
from spine.application.diagnostics import AuditEventRegistry, DiagnosticEventRegistry
from spine.application.ingestion_history import IngestionHistoryPage
from spine.auth import InMemoryAuthorizationDirectory, RoutePolicyRegistry
from spine.infrastructure.auth import LocalAuthenticationSettings
from spine.infrastructure.auth import RequestAuthorizationComposer, TrustedRequestContextBoundary
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


def _production_app():
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
        ),
    )


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


def test_production_app_exposes_protected_canonical_document_surface() -> None:
    application = _production_app()

    paths = {route.path for route in application.routes}

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
    assert "/api/v1/projections/{projection_kind}/status" in paths
    assert "/api/v1/projections/{projection_kind}/recovery" in paths
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
    application = _production_app()
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
