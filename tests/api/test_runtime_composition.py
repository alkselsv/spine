from __future__ import annotations

from uuid import UUID

from spine.api.runtime import DeployableApiSettings, create_production_app
from spine.infrastructure.auth import LocalAuthenticationSettings
from spine.infrastructure.db.settings import RuntimeDatabaseSettings


def test_production_app_exposes_protected_canonical_document_surface() -> None:
    application = create_production_app(
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
            original_storage_root="/tmp/spine-test-originals",
        ),
    )

    paths = {route.path for route in application.routes}

    assert "/api/v1/documents" in paths
    assert "/api/v1/documents/{source_object_id}" in paths
    assert "/api/v1/documents/{source_object_id}/revisions" in paths
    assert "/api/v1/documents/{source_object_id}/replacement" in paths
    assert "/api/v1/documents/{source_object_id}/tombstone" in paths
    assert "/api/v1/documents/lifecycle/operations/{operation_id}" in paths
    assert "/api/v1/documents/batches" in paths
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
