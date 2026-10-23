"""Executable production-capable API composition for the governed Q&A stack."""

from __future__ import annotations

import logging
import hmac
import asyncio
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import FastAPI
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.composition import (
    compose_local_api_authorization,
    compose_oidc_api_authorization,
)
from spine.api.authorized_ask import install_qa_observer_admission_route
from spine.api.main import create_app
from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticEventRegistry,
    RequiredAuditCoordinator,
)
from spine.application.ask_admission import PersistedAskAdmissionService
from spine.application.answer_question_attempts import PersistedAnswerQuestionAttemptService
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.production_qa_deployment import (
    ProductionQAADeploymentProfile,
    ProductionQAAProfileMismatch,
)
from spine.application.qa_observation.contracts import AdmittedExecutionIdentity
from spine.application.document_queries import CanonicalDocumentQueryService
from spine.application.document_replacement_upload import DocumentReplacementUploadService
from spine.application.ingestion_history import OrchestratedIngestionHistoryService
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
    OIDCAuthenticationSettings,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.db.readiness import (
    load_migration_inventory,
    verify_database_readiness,
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
from spine.infrastructure.qa_observation_capture import (
    CaptureAuthorizer,
    CaptureWorkload,
    CaptureWorkloadRole,
    create_capture_channel_router,
)
from spine.infrastructure.qa_deployment_settings import ProductionQAADeploymentSettings
from spine.infrastructure.qa_composition import (
    PostgreSQLCitationInspectorService,
    PostgreSQLFeedbackService,
    PostgreSQLQASessionService,
)
from spine.infrastructure.qa_observation_capture import HttpCaptureChannel


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
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    ingestion_service_principal_id: UUID
    qa_service_principal_id: UUID
    original_storage_root: Path
    max_upload_bytes: int = Field(default=10_000_000, gt=0)
    startup_verification_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    database_profile_id: str = Field(min_length=1, max_length=128)
    scope_policy_id: str = Field(min_length=1, max_length=128)
    authentication_mode: Literal["oidc", "local_development"] = "local_development"
    ingestion_service_alias_issuer: str = Field(min_length=1, max_length=512)
    ingestion_service_alias_subject: str = Field(min_length=1, max_length=512)
    temporal_address: str = Field(default="temporal:7233", min_length=1, max_length=255)
    temporal_namespace: str = Field(default="default", min_length=1, max_length=255)
    temporal_task_queue: str = Field(default="spine-r1-qa", min_length=1, max_length=255)
    temporal_cluster_profile_id: str = Field(min_length=1, max_length=128)
    capture_service_profile_id: str = Field(min_length=1, max_length=128)
    capture_service_url: AnyHttpUrl
    projection_profile_id: str = Field(min_length=1, max_length=128)
    model_profile_id: str = Field(min_length=1, max_length=128)
    llm_api_key: SecretStr = Field(
        default=SecretStr(""), repr=False, validation_alias="LLM_API_KEY"
    )
    llm_model: str = Field(default="", validation_alias="LLM_MODEL")
    llm_endpoint: str = Field(default="https://api.openai.com/v1", validation_alias="LLM_ENDPOINT")
    qa_capture_worker_credential: SecretStr = Field(min_length=32, repr=False)
    qa_capture_observer_credential: SecretStr = Field(min_length=32, repr=False)

    @model_validator(mode="after")
    def validate_capture_credentials(self) -> "DeployableApiSettings":
        if hmac.compare_digest(
            self.qa_capture_worker_credential.get_secret_value(),
            self.qa_capture_observer_credential.get_secret_value(),
        ):
            raise ValueError("capture workload credentials must be distinct")
        return self


def create_production_app(
    *,
    database_settings: RuntimeDatabaseSettings | None = None,
    authentication_settings: LocalAuthenticationSettings | OIDCAuthenticationSettings | None = None,
    api_settings: DeployableApiSettings | None = None,
    deployment_profile: ProductionQAADeploymentProfile | None = None,
) -> FastAPI:
    """Compose protected canonical reads with real PostgreSQL authority.

    This is intentionally an executable composition root.  It owns its engine,
    trusted context boundaries and audit gate; callers can select configuration,
    but cannot replace those production adapters with request-time fakes.
    """

    resolved_database = database_settings or RuntimeDatabaseSettings()
    resolved_api = api_settings or DeployableApiSettings()
    if authentication_settings is not None:
        resolved_authentication = authentication_settings
    elif resolved_api.authentication_mode == "oidc":
        resolved_authentication = OIDCAuthenticationSettings()
    else:
        resolved_authentication = LocalAuthenticationSettings()
    resolved_profile = deployment_profile or ProductionQAADeploymentSettings().load_profile()
    authentication_mode = _validate_authentication_mode(
        resolved_api, resolved_authentication
    )
    if (
        resolved_profile.release_identity != resolved_api.build_version
        or resolved_profile.commit_sha != resolved_api.commit_sha
    ):
        raise ProductionQAAProfileMismatch("PROFILE_MISMATCH")
    if (
        resolved_profile.database_profile_id != resolved_api.database_profile_id
        or resolved_profile.migration_revision
        != load_migration_inventory().supported_head
    ):
        raise ProductionQAAProfileMismatch("DATABASE_BINDING_MISMATCH")
    if (
        resolved_profile.authentication_profile_id
        != resolved_authentication.configuration_version
        or resolved_profile.authentication_mode != authentication_mode
        or resolved_profile.scope_policy_id != resolved_api.scope_policy_id
    ):
        raise ProductionQAAProfileMismatch("AUTHORITY_BINDING_MISMATCH")
    if (
        resolved_profile.temporal_namespace != resolved_api.temporal_namespace
        or resolved_profile.temporal_task_queue != resolved_api.temporal_task_queue
        or resolved_profile.temporal_cluster_profile_id
        != resolved_api.temporal_cluster_profile_id
    ):
        raise ProductionQAAProfileMismatch("TEMPORAL_BINDING_MISMATCH")
    if resolved_profile.capture_service_profile_id != resolved_api.capture_service_profile_id:
        raise ProductionQAAProfileMismatch("CAPTURE_BINDING_MISMATCH")
    if (
        resolved_profile.projection_profile_id != resolved_api.projection_profile_id
        or resolved_profile.model_profile_id != resolved_api.model_profile_id
    ):
        raise ProductionQAAProfileMismatch("PROVIDER_BINDING_MISMATCH")
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
        authority_alias_issuer=resolved_api.ingestion_service_alias_issuer,
        authority_alias_subject=resolved_api.ingestion_service_alias_subject,
    )
    authorization_arguments = {
        "session_factory": sessions,
        "route_policies": RoutePolicyRegistry(),
        "request_boundary": request_boundary,
        "audit_registry": audit_registry,
        "audit_gate": RequiredAuditCoordinator(persistence.uow_factory),
        "diagnostic_registry": diagnostic_registry,
        "diagnostic_sink": diagnostic_sink,
    }
    if type(resolved_authentication) is OIDCAuthenticationSettings:
        authorization = compose_oidc_api_authorization(
            settings=resolved_authentication,
            **authorization_arguments,
        )
    else:
        authorization = compose_local_api_authorization(
            settings=resolved_authentication,
            close_hook=engine.dispose,
            **authorization_arguments,
        )

    async def verify_production_database_profile() -> None:
        try:
            await verify_database_readiness(engine, resolved_database)
        except Exception:
            raise ProductionQAAProfileMismatch("DATABASE_BINDING_MISMATCH") from None

    qa_stream = AuthorizedQAStreamService(event_log=InMemoryQAEventLog())
    ask_admission = PersistedAskAdmissionService(persistence.uow_factory)
    qa_sessions = PostgreSQLQASessionService(persistence.uow_factory)
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
        document_replacement_upload_service=DocumentReplacementUploadService(
            uow_factory=persistence.uow_factory,
            ingestion_service=ingestion.document_service,
        ),
        batch_document_ingestion_service=ingestion.batch_service,
        ingestion_history_service=OrchestratedIngestionHistoryService(
            persistence.uow_factory
        ),
        ingestion_history_cursor_secret=(
            resolved_api.cursor_secret.get_secret_value().encode("utf-8")
        ),
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
        qa_session_service=qa_sessions,
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
        ask_admission_service=ask_admission,
        max_upload_bytes=resolved_api.max_upload_bytes,
        max_batch_file_bytes=resolved_api.max_upload_bytes,
        build_version=resolved_api.build_version,
        startup_verifiers=(verify_production_database_profile,),
        startup_verification_timeout_seconds=(
            resolved_api.startup_verification_timeout_seconds
        ),
    )
    application.state.production_persistence = persistence
    application.state.production_engine = engine
    application.state.production_persistence_boundary = persistence_boundary
    application.state.production_session_factory = sessions
    application.state.production_database_settings = resolved_database
    application.state.production_api_settings = resolved_api
    application.state.production_authentication_settings = resolved_authentication
    if type(resolved_authentication) is OIDCAuthenticationSettings:
        application.state.shutdown_closers.append(engine.dispose)
    application.state.ask_admission_service = ask_admission
    application.state.qa_session_service = qa_sessions
    application.state.production_qa_deployment_profile = resolved_profile
    application.include_router(
        create_capture_channel_router(
            authenticator=_capture_authenticator(
                profile=resolved_profile,
                worker_credential=resolved_api.qa_capture_worker_credential,
                observer_credential=resolved_api.qa_capture_observer_credential,
            ),
            authorizer=_ProductionCaptureAuthorizer(resolved_profile),
            clock=lambda: datetime.now(timezone.utc),
        )
    )
    return application


async def attach_durable_qa_composition(application: FastAPI) -> None:
    """Attach the existing durable Ask graph and observer to the API process."""

    from temporalio.client import Client
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.contrib.pydantic import pydantic_data_converter

    from spine.application.persistence.context import TrustedPersistenceContext
    from spine.application.workflows.ask_admission_dispatch import (
        AskAdmissionWorkflowInput,
        ScopedAskAdmissionCommandLoader,
    )
    from spine.infrastructure.ingestion_composition import register_ingestion_outbox_events
    from spine.infrastructure.qa_worker_composition import build_production_qa_capability_graph
    from spine.infrastructure.workflows.ask_admission import (
        PersistenceAskAdmissionCommandSource,
        PersistenceAskAdmissionRunStarter,
    )
    from spine.workflows.temporal.client import TemporalSdkWorkflowClient
    from spine.infrastructure.qa_runtime_support import (
        CurrentAskAdmissionAuthorizer,
        CurrentAuthorizationGeneration,
        DiscardWorkerElapsedCapture,
        create_qa_worker_context,
    )

    settings: DeployableApiSettings = application.state.production_api_settings
    persistence: PostgreSQLPersistence = application.state.production_persistence
    database: RuntimeDatabaseSettings = application.state.production_database_settings
    engine = application.state.production_engine
    session_factory = application.state.production_session_factory
    boundary = application.state.production_persistence_boundary

    try:
        await asyncio.wait_for(
            verify_database_readiness(engine, database),
            timeout=settings.startup_verification_timeout_seconds,
        )
    except Exception:
        raise ProductionQAAProfileMismatch("DATABASE_BINDING_MISMATCH") from None
    def context_factory(workflow_input: AskAdmissionWorkflowInput) -> TrustedPersistenceContext:
        return create_qa_worker_context(
            boundary, settings.qa_service_principal_id, workflow_input
        )

    capture_channel = HttpCaptureChannel(
        base_url=str(settings.capture_service_url),
        headers=lambda: {
            "Authorization": "Bearer "
            + settings.qa_capture_observer_credential.get_secret_value()
        },
        timeout=settings.startup_verification_timeout_seconds + 5.0,
    )
    try:
        graph = await asyncio.wait_for(
            build_production_qa_capability_graph(
                session_factory=session_factory,
                persistence=persistence,
                boundary=boundary,
                service_principal_id=settings.qa_service_principal_id,
                llm_api_key=settings.llm_api_key.get_secret_value(),
                llm_model=settings.llm_model,
                llm_endpoint=settings.llm_endpoint,
            ),
            timeout=settings.startup_verification_timeout_seconds,
        )
        temporal = await asyncio.wait_for(
            Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
                data_converter=pydantic_data_converter,
            ),
            timeout=settings.startup_verification_timeout_seconds,
        )
    except BaseException:
        await capture_channel.close()
        raise

    temporal_client = TemporalSdkWorkflowClient(
        temporal,
        task_queue=settings.temporal_task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    attempts = PersistedAnswerQuestionAttemptService(
        uow_factory=persistence.uow_factory,
        execution=graph.execution,
        artifacts=graph.artifacts,
    )
    command_loader = ScopedAskAdmissionCommandLoader(
        PersistenceAskAdmissionCommandSource(
            uow_factory=persistence.uow_factory,
            context_factory=context_factory,
        ),
        CurrentAskAdmissionAuthorizer(graph.configuration),
    )
    from spine.api.composition import compose_durable_ask

    try:
        composition = compose_durable_ask(
            capability=graph.capability,
            sessions=application.state.qa_session_service,
            admission=application.state.ask_admission_service,
            attempts=attempts,
            command_loader=command_loader,
            persistence_context_factory=context_factory,
            temporal_client=temporal_client,
            delivery_routes=OutboxDeliveryRouteRegistry(
                version="issue529.production-api.v1"
            ),
            monotonic_ns=time.monotonic_ns,
            worker_execution_elapsed_port=DiscardWorkerElapsedCapture(),
            authorization_generation_resolver=CurrentAuthorizationGeneration(
                session_factory, settings.qa_service_principal_id
            ),
            run_starter=PersistenceAskAdmissionRunStarter(
                uow_factory=persistence.uow_factory,
                context_factory=context_factory,
            ),
            capture_channel=capture_channel,
        )
        if composition.observer_admission is None:
            raise ProductionQAAProfileMismatch("CAPTURE_BINDING_MISMATCH")
        install_qa_observer_admission_route(
            application,
            observer_admission=composition.observer_admission,
        )
    except BaseException:
        await capture_channel.close()
        raise
    application.state.durable_ask_composition = composition
    application.state.shutdown_closers.append(capture_channel.close)


async def _run_api() -> None:
    import uvicorn

    application = create_production_app()
    try:
        await attach_durable_qa_composition(application)
        await uvicorn.Server(
            uvicorn.Config(
                application,
                host="0.0.0.0",
                port=8080,
                access_log=False,
            )
        ).serve()
    except BaseException:
        await application.state.production_engine.dispose()
        raise


def main() -> None:
    asyncio.run(_run_api())


def _validate_authentication_mode(
    api_settings: DeployableApiSettings,
    authentication_settings: LocalAuthenticationSettings | OIDCAuthenticationSettings,
) -> Literal["oidc", "local_development"]:
    if api_settings.authentication_mode == "oidc":
        if type(authentication_settings) is not OIDCAuthenticationSettings:
            raise ProductionQAAProfileMismatch("AUTHORITY_BINDING_MISMATCH")
        return "oidc"
    if (
        type(authentication_settings) is not LocalAuthenticationSettings
        or authentication_settings.deployment_mode != "development"
    ):
        raise ProductionQAAProfileMismatch("AUTHORITY_BINDING_MISMATCH")
    return "local_development"


def _capture_authenticator(
    *,
    profile: ProductionQAADeploymentProfile,
    worker_credential: SecretStr,
    observer_credential: SecretStr,
):
    expected = (
        (
            worker_credential.get_secret_value(),
            CaptureWorkload(
                workload_id=profile.capture_worker_workload_id,
                role=CaptureWorkloadRole.WORKER,
            ),
        ),
        (
            observer_credential.get_secret_value(),
            CaptureWorkload(
                workload_id=profile.capture_observer_workload_id,
                role=CaptureWorkloadRole.OBSERVER,
            ),
        ),
    )

    async def authenticate(authorization: str | None) -> CaptureWorkload | None:
        scheme, separator, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not credential:
            return None
        for expected_credential, workload in expected:
            if hmac.compare_digest(credential, expected_credential):
                return workload
        return None

    return authenticate


class _ProductionCaptureAuthorizer(CaptureAuthorizer):
    def __init__(self, profile: ProductionQAADeploymentProfile) -> None:
        self._profile = profile

    async def observer_may_bind(
        self, observer: CaptureWorkload, identity: AdmittedExecutionIdentity
    ) -> bool:
        return self._workload_matches(
            observer, CaptureWorkloadRole.OBSERVER
        ) and self._identity_is_complete(identity)

    async def worker_may_publish(
        self, worker: CaptureWorkload, identity: AdmittedExecutionIdentity
    ) -> bool:
        return self._workload_matches(
            worker, CaptureWorkloadRole.WORKER
        ) and self._identity_is_complete(identity)

    async def worker_may_deliver_to_observer(
        self,
        worker: CaptureWorkload,
        observer: CaptureWorkload,
        identity: AdmittedExecutionIdentity,
    ) -> bool:
        return (
            self._workload_matches(worker, CaptureWorkloadRole.WORKER)
            and self._workload_matches(observer, CaptureWorkloadRole.OBSERVER)
            and self._identity_is_complete(identity)
        )

    def _workload_matches(
        self, workload: CaptureWorkload, role: CaptureWorkloadRole
    ) -> bool:
        workload_id = (
            self._profile.capture_worker_workload_id
            if role is CaptureWorkloadRole.WORKER
            else self._profile.capture_observer_workload_id
        )
        return workload.role is role and workload.workload_id == workload_id

    @staticmethod
    def _identity_is_complete(identity: AdmittedExecutionIdentity) -> bool:
        return (
            type(identity) is AdmittedExecutionIdentity
            and identity.command_id.int != 0
            and identity.run_id.int != 0
            and identity.session_id.int != 0
            and identity.message_id.int != 0
            and identity.workspace_id.int != 0
            and identity.environment_id.int != 0
            and identity.requester.id.int != 0
            and identity.attempt_id.int != 0
        )


__all__ = [
    "DeployableApiSettings",
    "attach_durable_qa_composition",
    "create_production_app",
    "main",
]
