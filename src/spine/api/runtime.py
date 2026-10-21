"""Executable production-capable API composition for the governed Q&A stack."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.composition import compose_local_api_authorization
from spine.api.main import create_app
from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticEventRegistry,
    RequiredAuditCoordinator,
)
from spine.application.ask_admission import PersistedAskAdmissionService
from spine.application.document_queries import CanonicalDocumentQueryService
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.projection_control import (
    CanonicalProjectionStatusService,
    ProjectionRecoveryRequestService,
)
from spine.application.qa_run_history import PersistedQARunHistoryService
from spine.application.qa_events import InMemoryQAEventLog
from spine.application.qa_streaming import AuthorizedQAStreamService
from spine.auth import RoutePolicyRegistry
from spine.infrastructure.auth import (
    LocalAuthenticationSettings,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.diagnostics import JsonLoggingDiagnosticSink
from spine.infrastructure.document_lifecycle import (
    ProductionDocumentLifecycleCommandService,
)
from spine.infrastructure.ingestion_composition import (
    build_production_ingestion,
    register_ingestion_outbox_events,
)
from spine.infrastructure.operational_correlation import (
    PostgreSQLOperationalCorrelationReader,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.qa_composition import (
    PostgreSQLCitationInspectorService,
    PostgreSQLFeedbackService,
    PostgreSQLQASessionService,
)


class DeployableApiSettings(BaseSettings):
    """Process configuration not already owned by a concrete adapter."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_API_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    cursor_secret: SecretStr = Field(min_length=32, repr=False)
    build_version: str = Field(min_length=1, max_length=128)
    ingestion_service_principal_id: UUID
    qa_service_principal_id: UUID
    original_storage_root: Path
    max_upload_bytes: int = Field(default=10_000_000, gt=0)


def create_production_app(
    *,
    database_settings: RuntimeDatabaseSettings | None = None,
    authentication_settings: LocalAuthenticationSettings | None = None,
    api_settings: DeployableApiSettings | None = None,
) -> FastAPI:
    """Compose protected canonical reads with real PostgreSQL authority.

    This is intentionally an executable composition root.  It owns its engine,
    trusted context boundaries and audit gate; callers can select configuration,
    but cannot replace those production adapters with request-time fakes.
    """

    resolved_database = database_settings or RuntimeDatabaseSettings()
    resolved_authentication = authentication_settings or LocalAuthenticationSettings()
    resolved_api = api_settings or DeployableApiSettings()
    engine = create_async_engine(
        resolved_database.url.get_secret_value(),
        pool_size=resolved_database.pool_size,
        max_overflow=resolved_database.max_overflow,
        pool_timeout=resolved_database.pool_timeout_seconds,
        pool_pre_ping=resolved_database.pool_pre_ping,
        pool_reset_on_return="rollback",
        isolation_level="READ COMMITTED",
        connect_args={
            "connect_timeout": resolved_database.connect_timeout_seconds,
            "options": POSTGRESQL_SEARCH_PATH_OPTIONS,
        },
        hide_parameters=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    persistence_boundary = TrustedContextBoundary.create()
    request_boundary = TrustedRequestContextBoundary.create(
        persistence_boundary=persistence_boundary
    )
    audit_registry = AuditEventRegistry.with_default_families()
    diagnostic_registry = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = JsonLoggingDiagnosticSink(
        diagnostic_registry,
        logger=logging.getLogger("spine.diagnostics"),
    )
    outbox_events = OutboxEventRegistry()
    register_ingestion_outbox_events(outbox_events)
    persistence = PostgreSQLPersistence(
        session_factory=sessions,
        context_verifier=request_boundary,
        outbox_events=outbox_events,
        audit_events=audit_registry,
    )
    ingestion = build_production_ingestion(
        session_factory=sessions,
        uow_factory=persistence.uow_factory,
        context_verifier=request_boundary,
        persistence_boundary=persistence_boundary,
        service_principal_id=resolved_api.ingestion_service_principal_id,
        qa_service_principal_id=resolved_api.qa_service_principal_id,
        storage_root=resolved_api.original_storage_root,
        outbox_events=outbox_events,
        audit_events=audit_registry,
        max_bytes=resolved_api.max_upload_bytes,
        authority_alias_issuer=(
            resolved_authentication.authentication_alias_issuer
        ),
        authority_alias_subject=(
            resolved_authentication.authentication_alias_subject
        ),
    )
    authorization = compose_local_api_authorization(
        settings=resolved_authentication,
        session_factory=sessions,
        route_policies=RoutePolicyRegistry(),
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=RequiredAuditCoordinator(persistence.uow_factory),
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        close_hook=engine.dispose,
    )
    qa_stream = AuthorizedQAStreamService(event_log=InMemoryQAEventLog())
    application = create_app(
        authorization_runtime=authorization,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        document_query_service=CanonicalDocumentQueryService(
            persistence.uow_factory
        ),
        document_query_cursor_secret=(
            resolved_api.cursor_secret.get_secret_value().encode("utf-8")
        ),
        document_ingestion_service=ingestion.document_service,
        batch_document_ingestion_service=ingestion.batch_service,
        document_lifecycle_command_service=ProductionDocumentLifecycleCommandService(
            uow_factory=persistence.uow_factory,
            outbox_events=outbox_events,
            audit_events=audit_registry,
        ),
        projection_status_service=CanonicalProjectionStatusService(
            persistence.uow_factory
        ),
        projection_recovery_service=ProjectionRecoveryRequestService(
            uow_factory=persistence.uow_factory,
            outbox_events=outbox_events,
        ),
        qa_session_service=PostgreSQLQASessionService(persistence.uow_factory),
        qa_session_cursor_secret=(
            resolved_api.cursor_secret.get_secret_value().encode("utf-8")
        ),
        qa_run_history_service=PersistedQARunHistoryService(
            persistence.uow_factory
        ),
        qa_run_history_cursor_secret=(
            resolved_api.cursor_secret.get_secret_value().encode("utf-8")
        ),
        operational_correlation_reader=PostgreSQLOperationalCorrelationReader(
            persistence.uow_factory
        ),
        feedback_service=PostgreSQLFeedbackService(persistence.uow_factory),
        citation_inspector_service=PostgreSQLCitationInspectorService(
            persistence.uow_factory,
            sessions,
            persistence_boundary,
        ),
        qa_stream_service=qa_stream,
        qa_stream_disclosure_authorizer=lambda _receipt, _context: False,
        ask_admission_service=PersistedAskAdmissionService(
            persistence.uow_factory
        ),
        max_upload_bytes=resolved_api.max_upload_bytes,
        max_batch_file_bytes=resolved_api.max_upload_bytes,
        build_version=resolved_api.build_version,
    )
    application.state.production_persistence = persistence
    return application


__all__ = ["DeployableApiSettings", "create_production_app"]
