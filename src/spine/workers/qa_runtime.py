"""Executable Temporal worker for durable governed Q&A execution."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.composition import compose_durable_ask
from spine.application.answer_question_attempts import (
    PersistedAnswerQuestionAttemptService,
)
from spine.application.ask_admission import PersistedAskAdmissionService
from spine.application.diagnostics import AuditEventRegistry
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.production_qa_deployment import (
    ProductionQAADeploymentProfile,
    ProductionQAAProfileMismatch,
)
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionWorkflowInput,
    ScopedAskAdmissionCommandLoader,
)
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.db.readiness import verify_database_readiness
from spine.infrastructure.ingestion_composition import register_ingestion_outbox_events
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.qa_observation_capture import HttpCaptureChannel
from spine.infrastructure.qa_deployment_settings import ProductionQAADeploymentSettings
from spine.infrastructure.qa_composition import PostgreSQLQASessionService
from spine.infrastructure.qa_runtime_support import (
    CurrentAskAdmissionAuthorizer,
    CurrentAuthorizationGeneration,
    DiscardWorkerElapsedCapture,
    create_qa_worker_context,
)
from spine.infrastructure.workflows.ask_admission import (
    PersistenceAskAdmissionCommandSource,
    PersistenceAskAdmissionRunStarter,
)
from spine.workflows.temporal.client import TemporalSdkWorkflowClient


class QAWorkerSettings(BaseSettings):
    """Deployment settings owned by the Q&A activity worker."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_QA_WORKER_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    service_principal_id: UUID
    build_version: str = Field(min_length=1, max_length=128)
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    database_profile_id: str = Field(min_length=1, max_length=128)
    scope_policy_id: str = Field(min_length=1, max_length=128)
    temporal_address: str = Field(default="temporal:7233", min_length=1, max_length=255)
    temporal_namespace: str = Field(default="default", min_length=1, max_length=255)
    temporal_task_queue: str = Field(default="spine-r1-qa", min_length=1, max_length=255)
    temporal_cluster_profile_id: str = Field(min_length=1, max_length=128)
    authorization_generation: int = Field(default=1, ge=1)
    capture_service_profile_id: str = Field(min_length=1, max_length=128)
    capture_service_url: AnyHttpUrl
    capture_worker_credential: SecretStr = Field(min_length=32, repr=False)
    projection_profile_id: str = Field(min_length=1, max_length=128)
    model_profile_id: str = Field(min_length=1, max_length=128)
    startup_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    shutdown_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    health_host: str = Field(default="0.0.0.0", min_length=1, max_length=255)
    health_port: int = Field(default=8081, ge=1, le=65535)
    llm_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="LLM_API_KEY", repr=False
    )
    llm_model: str = Field(default="", validation_alias="LLM_MODEL")
    llm_endpoint: str = Field(
        default="https://api.openai.com/v1", validation_alias="LLM_ENDPOINT"
    )


@dataclass(slots=True)
class _QAWorkerExecutionState:
    application_execution_completed: bool = False


@dataclass(slots=True)
class ProductionQAWorkerRuntime:
    """Own the SDK worker and resources for one deployable process."""

    worker: object
    runtime_engine: object
    capture_channel: HttpCaptureChannel
    shutdown_timeout_seconds: float
    health_host: str
    health_port: int
    database_reachable: bool = True
    temporal_connected: bool = True
    process_alive: bool = False
    execution_state: _QAWorkerExecutionState = field(
        default_factory=_QAWorkerExecutionState
    )

    @property
    def readiness_snapshot(self) -> "QAWorkerReadiness":
        worker_polling = bool(getattr(self.worker, "is_running", False))
        ready = (
            self.database_reachable
            and self.temporal_connected
            and worker_polling
        )
        return QAWorkerReadiness(
            status="ready" if ready else "not_ready",
            process_liveness="alive" if self.process_alive else "stopped",
            postgresql="reachable" if self.database_reachable else "unavailable",
            temporal="reachable" if self.temporal_connected else "unavailable",
            worker_polling="polling" if worker_polling else "not_polling",
            application_execution=(
                "completed"
                if self.execution_state.application_execution_completed
                else "not_verified"
            ),
        )

    async def run_forever(self) -> None:
        self.process_alive = True
        try:
            await self.worker.run()  # type: ignore[attr-defined]
        finally:
            self.process_alive = False

    async def close(self) -> None:
        shutdown = getattr(self.worker, "shutdown", None)
        try:
            if callable(shutdown):
                await asyncio.wait_for(
                    shutdown(), timeout=self.shutdown_timeout_seconds
                )
        finally:
            try:
                await asyncio.wait_for(
                    self.capture_channel.close(),
                    timeout=self.shutdown_timeout_seconds,
                )
            finally:
                await asyncio.wait_for(
                    self.runtime_engine.dispose(),
                    timeout=self.shutdown_timeout_seconds,
                )


class QAWorkerReadiness(BaseModel):
    """Content-free process readiness with explicit unverified execution state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["spine-qa-worker-readiness:v1"] = (
        "spine-qa-worker-readiness:v1"
    )
    status: Literal["ready", "not_ready"]
    process_liveness: Literal["alive", "stopped"]
    postgresql: Literal["reachable", "unavailable"]
    temporal: Literal["reachable", "unavailable"]
    worker_polling: Literal["polling", "not_polling"]
    application_execution: Literal["completed", "not_verified"]


def create_qa_worker_health_app(runtime: ProductionQAWorkerRuntime) -> FastAPI:
    """Expose only bounded lifecycle and readiness codes on the worker listener."""

    application = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @application.get("/health/live")
    async def liveness() -> JSONResponse:
        alive = runtime.process_alive
        return JSONResponse(
            status_code=200 if alive else 503,
            content={"status": "alive" if alive else "stopped"},
        )

    @application.get("/health/ready")
    async def readiness() -> JSONResponse:
        report = runtime.readiness_snapshot
        return JSONResponse(
            status_code=200 if report.status == "ready" else 503,
            content=report.model_dump(mode="json"),
        )

    return application


def validate_qa_worker_deployment_profile(
    settings: QAWorkerSettings,
    profile: ProductionQAADeploymentProfile,
) -> None:
    """Fail closed if this process does not match the shared deployment pin."""

    if (
        settings.build_version != profile.release_identity
        or settings.commit_sha != profile.commit_sha
        or settings.database_profile_id != profile.database_profile_id
        or settings.scope_policy_id != profile.scope_policy_id
    ):
        raise ProductionQAAProfileMismatch("PROFILE_MISMATCH")
    if (
        settings.temporal_namespace != profile.temporal_namespace
        or settings.temporal_task_queue != profile.temporal_task_queue
    ):
        raise ProductionQAAProfileMismatch("TEMPORAL_BINDING_MISMATCH")
    if settings.capture_service_profile_id != profile.capture_service_profile_id:
        raise ProductionQAAProfileMismatch("CAPTURE_BINDING_MISMATCH")
    if (
        settings.temporal_cluster_profile_id
        != profile.temporal_cluster_profile_id
    ):
        raise ProductionQAAProfileMismatch("TEMPORAL_BINDING_MISMATCH")
    if (
        settings.projection_profile_id != profile.projection_profile_id
        or settings.model_profile_id != profile.model_profile_id
    ):
        raise ProductionQAAProfileMismatch("PROVIDER_BINDING_MISMATCH")


def _worker_capture_channel(settings: QAWorkerSettings) -> HttpCaptureChannel:
    credential = settings.capture_worker_credential.get_secret_value()
    return HttpCaptureChannel(
        base_url=str(settings.capture_service_url),
        headers=lambda: {"Authorization": f"Bearer {credential}"},
        timeout=settings.shutdown_timeout_seconds + 5.0,
    )


async def _close_worker_startup_resources(
    capture_channel: HttpCaptureChannel, engine: object
) -> None:
    try:
        await capture_channel.close()
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


async def create_production_qa_worker_runtime(
    *,
    database_settings: RuntimeDatabaseSettings | None = None,
    worker_settings: QAWorkerSettings | None = None,
    deployment_profile: ProductionQAADeploymentProfile | None = None,
) -> ProductionQAWorkerRuntime:
    """Compose PostgreSQL admission, production capability and Temporal worker."""

    from temporalio.client import Client
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.contrib.pydantic import pydantic_data_converter

    from spine.infrastructure.qa_worker_composition import (
        build_production_qa_capability_graph,
    )

    database = database_settings or RuntimeDatabaseSettings()
    settings = worker_settings or QAWorkerSettings()
    profile = deployment_profile or ProductionQAADeploymentSettings().load_profile()
    validate_qa_worker_deployment_profile(settings, profile)
    from spine.infrastructure.db.readiness import load_migration_inventory

    if profile.migration_revision != load_migration_inventory().supported_head:
        raise ProductionQAAProfileMismatch("DATABASE_BINDING_MISMATCH")
    capture_channel = _worker_capture_channel(settings)
    engine = create_async_engine(
        database.url.get_secret_value(),
        pool_size=database.pool_size,
        max_overflow=database.max_overflow,
        pool_timeout=database.pool_timeout_seconds,
        pool_pre_ping=database.pool_pre_ping,
        pool_reset_on_return="rollback",
        isolation_level="READ COMMITTED",
        connect_args={
            "connect_timeout": database.connect_timeout_seconds,
            "options": POSTGRESQL_SEARCH_PATH_OPTIONS,
        },
        hide_parameters=True,
    )
    try:
        await asyncio.wait_for(
            verify_database_readiness(engine, database),
            timeout=settings.startup_timeout_seconds,
        )
    except Exception:
        await _close_worker_startup_resources(capture_channel, engine)
        raise ProductionQAAProfileMismatch("DATABASE_BINDING_MISMATCH") from None
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    boundary = TrustedContextBoundary.create()
    outbox_events = OutboxEventRegistry()
    register_ingestion_outbox_events(outbox_events)
    persistence = PostgreSQLPersistence(
        session_factory=session_factory,
        context_verifier=boundary,
        outbox_events=outbox_events,
        audit_events=AuditEventRegistry.with_default_families(),
    )

    def context_factory(workflow_input: AskAdmissionWorkflowInput):
        return create_qa_worker_context(
            boundary, settings.service_principal_id, workflow_input
        )

    try:
        graph = await asyncio.wait_for(
            build_production_qa_capability_graph(
                session_factory=session_factory,
                persistence=persistence,
                boundary=boundary,
                service_principal_id=settings.service_principal_id,
                llm_api_key=settings.llm_api_key.get_secret_value(),
                llm_model=settings.llm_model,
                llm_endpoint=settings.llm_endpoint or "https://api.openai.com/v1",
            ),
            timeout=settings.startup_timeout_seconds,
        )
    except BaseException:
        await _close_worker_startup_resources(capture_channel, engine)
        raise
    sessions = PostgreSQLQASessionService(persistence.uow_factory)
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
    try:
        temporal = await asyncio.wait_for(
            Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
                data_converter=pydantic_data_converter,
            ),
            timeout=settings.startup_timeout_seconds,
        )
    except BaseException:
        await _close_worker_startup_resources(capture_channel, engine)
        raise
    temporal_client = TemporalSdkWorkflowClient(
        temporal,
        task_queue=settings.temporal_task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    execution_state = _QAWorkerExecutionState()
    try:
        composition = compose_durable_ask(
            capability=graph.capability,
            sessions=sessions,
            admission=PersistedAskAdmissionService(persistence.uow_factory),
            attempts=attempts,
            command_loader=command_loader,
            persistence_context_factory=context_factory,
            temporal_client=temporal_client,
            delivery_routes=OutboxDeliveryRouteRegistry(
                version="issue508.qa-worker.v1"
            ),
            monotonic_ns=time.monotonic_ns,
            worker_execution_elapsed_port=DiscardWorkerElapsedCapture(),
            authorization_generation=settings.authorization_generation,
            authorization_generation_resolver=CurrentAuthorizationGeneration(
                session_factory,
                settings.service_principal_id,
            ),
            run_starter=PersistenceAskAdmissionRunStarter(
                uow_factory=persistence.uow_factory,
                context_factory=context_factory,
            ),
            worker_capture_channel=capture_channel,
            on_terminal_execution=lambda: setattr(
                execution_state, "application_execution_completed", True
            ),
        )
        worker = composition.create_temporal_worker()
    except BaseException:
        await _close_worker_startup_resources(capture_channel, engine)
        raise
    return ProductionQAWorkerRuntime(
        worker=worker,
        runtime_engine=engine,
        capture_channel=capture_channel,
        shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
        health_host=settings.health_host,
        health_port=settings.health_port,
        execution_state=execution_state,
    )


async def _run() -> None:
    runtime = await create_production_qa_worker_runtime()
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            create_qa_worker_health_app(runtime),
            host=runtime.health_host,
            port=runtime.health_port,
            access_log=False,
            log_config=None,
        )
    )
    health_task = asyncio.create_task(server.serve())
    worker_task = asyncio.create_task(runtime.run_forever())
    try:
        done, _ = await asyncio.wait(
            {health_task, worker_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            await task
    finally:
        server.should_exit = True
        try:
            await runtime.close()
        finally:
            try:
                await asyncio.wait_for(
                    health_task, timeout=runtime.shutdown_timeout_seconds
                )
            finally:
                if not worker_task.done():
                    await asyncio.wait_for(
                        worker_task, timeout=runtime.shutdown_timeout_seconds
                    )


def main() -> None:
    asyncio.run(_run())


__all__ = [
    "ProductionQAWorkerRuntime",
    "QAWorkerSettings",
    "QAWorkerReadiness",
    "create_qa_worker_health_app",
    "create_production_qa_worker_runtime",
    "main",
    "validate_qa_worker_deployment_profile",
]
