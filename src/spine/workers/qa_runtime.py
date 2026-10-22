"""Executable Temporal worker for durable governed Q&A execution."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.api.composition import compose_durable_ask
from spine.application.answer_question_attempts import (
    PersistedAnswerQuestionAttemptService,
)
from spine.application.ask_admission import PersistedAskAdmissionService
from spine.application.diagnostics import AuditEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionCommandData,
    AskAdmissionCommandUnavailable,
    AskAdmissionWorkflowInput,
    ScopedAskAdmissionCommandLoader,
)
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.ingestion_composition import register_ingestion_outbox_events
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.qa_composition import PostgreSQLQASessionService
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
    temporal_address: str = Field(default="temporal:7233", min_length=1, max_length=255)
    temporal_namespace: str = Field(default="default", min_length=1, max_length=255)
    temporal_task_queue: str = Field(default="spine-r1-qa", min_length=1, max_length=255)
    authorization_generation: int = Field(default=1, ge=1)
    llm_api_key: str = Field(default="", validation_alias="LLM_API_KEY", repr=False)
    llm_model: str = Field(default="", validation_alias="LLM_MODEL")
    llm_endpoint: str = Field(
        default="https://api.openai.com/v1", validation_alias="LLM_ENDPOINT"
    )


def _qa_worker_context(
    boundary: TrustedContextBoundary,
    service_principal_id: UUID,
    workflow_input: AskAdmissionWorkflowInput,
):
    """Issue authority only for the exact admitted workflow scope."""

    return boundary.worker(
        scope=EnvironmentScope(
            workspace_id=workflow_input.workspace_id,
            environment_id=workflow_input.environment_id,
        ),
        service_principal_id=service_principal_id,
        purpose=PersistencePurpose("governed_qa.execution.v1"),
        operation=PersistenceOperation("answer_question_run"),
        trace_id=workflow_input.trace_id,
    )


class _CurrentAdmissionAuthorizer:
    """Fail closed when protected command identity is incomplete or mismatched."""

    def __init__(self, configuration: object | None = None) -> None:
        self._configuration = configuration

    async def authorize(
        self,
        *,
        workflow_input: AskAdmissionWorkflowInput,
        command: AskAdmissionCommandData,
    ) -> bool:
        allowed = (
            command.matches(workflow_input)
            and command.subject is not None
            and command.subject.id.int != 0
            and command.environment is not None
            and command.idempotency_key is not None
        )
        if not allowed:
            return False
        prepare = getattr(self._configuration, "prepare", None)
        if not callable(prepare):
            return False
        await prepare(command)
        return True


class _DiscardWorkerElapsed:
    async def record_worker_execution_elapsed(self, sample: object) -> None:
        del sample


class _CurrentAuthorizationGeneration:
    """Resolve fresh service authority at one durable activity/action boundary."""

    def __init__(self, session_factory: object, service_principal_id: UUID) -> None:
        self._session_factory = session_factory
        self._service_principal_id = service_principal_id

    async def __call__(
        self,
        workflow_input: AskAdmissionWorkflowInput,
        command: AskAdmissionCommandData,
    ) -> int:
        if (
            command.workspace_id != workflow_input.workspace_id
            or command.environment_id != workflow_input.environment_id
        ):
            raise AskAdmissionCommandUnavailable(
                "Admitted Ask authority scope is unavailable."
            )
        session = self._session_factory()  # type: ignore[operator]
        async with session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                        "set_config('spine.environment_id', :environment_id, true)"
                    ),
                    {
                        "workspace_id": str(workflow_input.workspace_id),
                        "environment_id": str(workflow_input.environment_id),
                    },
                )
                generation = (
                    await session.execute(
                        text(
                            "SELECT spine.resolve_current_service_authority("
                            ":service_principal_id, :workspace_id, :environment_id, "
                            ":purpose, :operation)"
                        ),
                        {
                            "service_principal_id": str(self._service_principal_id),
                            "workspace_id": str(workflow_input.workspace_id),
                            "environment_id": str(workflow_input.environment_id),
                            "purpose": "governed_qa.execution.v1",
                            "operation": "answer_question_run",
                        },
                    )
                ).scalar_one_or_none()
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise AskAdmissionCommandUnavailable(
                "Current authorization generation is unavailable."
            )
        return generation


@dataclass(slots=True)
class ProductionQAWorkerRuntime:
    """Own the SDK worker and resources for one deployable process."""

    worker: object
    runtime_engine: object

    async def run_forever(self) -> None:
        await self.worker.run()  # type: ignore[attr-defined]

    async def close(self) -> None:
        shutdown = getattr(self.worker, "shutdown", None)
        if callable(shutdown):
            await shutdown()
        await self.runtime_engine.dispose()  # type: ignore[attr-defined]


async def create_production_qa_worker_runtime(
    *,
    database_settings: RuntimeDatabaseSettings | None = None,
    worker_settings: QAWorkerSettings | None = None,
) -> ProductionQAWorkerRuntime:
    """Compose PostgreSQL admission, production capability and Temporal worker."""

    from temporalio.client import Client
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Worker

    from spine.infrastructure.qa_worker_composition import (
        build_production_qa_capability_graph,
    )

    database = database_settings or RuntimeDatabaseSettings()
    settings = worker_settings or QAWorkerSettings()
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
        return _qa_worker_context(
            boundary, settings.service_principal_id, workflow_input
        )

    graph = await build_production_qa_capability_graph(
        session_factory=session_factory,
        persistence=persistence,
        boundary=boundary,
        service_principal_id=settings.service_principal_id,
        llm_api_key=settings.llm_api_key,
        llm_model=settings.llm_model,
        llm_endpoint=settings.llm_endpoint or "https://api.openai.com/v1",
    )
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
        _CurrentAdmissionAuthorizer(graph.configuration),
    )
    temporal = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    temporal_client = TemporalSdkWorkflowClient(
        temporal,
        task_queue=settings.temporal_task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    composition = compose_durable_ask(
        capability=graph.capability,
        sessions=sessions,
        admission=PersistedAskAdmissionService(persistence.uow_factory),
        attempts=attempts,
        command_loader=command_loader,
        persistence_context_factory=context_factory,
        temporal_client=temporal_client,
        delivery_routes=OutboxDeliveryRouteRegistry(version="issue508.qa-worker.v1"),
        monotonic_ns=time.monotonic_ns,
        worker_execution_elapsed_port=_DiscardWorkerElapsed(),
        authorization_generation=settings.authorization_generation,
        authorization_generation_resolver=_CurrentAuthorizationGeneration(
            session_factory, settings.service_principal_id
        ),
        run_starter=PersistenceAskAdmissionRunStarter(
            uow_factory=persistence.uow_factory,
            context_factory=context_factory,
        ),
    )
    worker = Worker(
        temporal,
        task_queue=settings.temporal_task_queue,
        workflows=[composition.workflow_definition],
        activities=list(composition.activity_bindings),
    )
    return ProductionQAWorkerRuntime(worker=worker, runtime_engine=engine)


async def _run() -> None:
    runtime = await create_production_qa_worker_runtime()
    try:
        await runtime.run_forever()
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(_run())


__all__ = [
    "ProductionQAWorkerRuntime",
    "QAWorkerSettings",
    "create_production_qa_worker_runtime",
    "main",
]
