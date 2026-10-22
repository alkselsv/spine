from __future__ import annotations

from uuid import UUID

import pytest
import httpx

from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionCommandData,
    AskAdmissionCommandUnavailable,
)
from spine.workers.qa_runtime import (
    ProductionQAWorkerRuntime,
    QAWorkerSettings,
    validate_qa_worker_deployment_profile,
    create_qa_worker_health_app,
)
from spine.infrastructure.qa_runtime_support import (
    CurrentAuthorizationGeneration,
    create_qa_worker_context,
)
from spine.application.production_qa_deployment import (
    ProductionQAADeploymentProfile,
    ProductionQAAProfileMismatch,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.application.workflows.ask_admission_dispatch import AskAdmissionWorkflowInput


def test_qa_worker_settings_match_durable_ask_queue() -> None:
    settings = QAWorkerSettings(
        service_principal_id=UUID(int=50801),
        build_version="issue-529-test",
        commit_sha="b" * 40,
        database_profile_id="spine-source-postgresql-17",
        scope_policy_id="workspace-environment-v1",
        temporal_cluster_profile_id="local-temporal-v1",
        capture_service_profile_id="api-qa-capture-v1",
        capture_service_url="http://api:8080",
        capture_worker_credential="worker-capture-credential-for-tests-0001",
        projection_profile_id="r1-cognee-local-v1",
        model_profile_id="r1-provider-local-v1",
    )

    assert settings.temporal_address == "temporal:7233"
    assert settings.temporal_namespace == "default"
    assert settings.temporal_task_queue == "spine-r1-qa"


def _deployment_profile(**overrides: object) -> ProductionQAADeploymentProfile:
    values: dict[str, object] = {
        "profile_id": "spine-r1-local",
        "profile_version": "2026.10.22",
        "deployment_fingerprint": "a" * 64,
        "release_identity": "issue-529-test",
        "commit_sha": "b" * 40,
        "database_profile_id": "spine-source-postgresql-17",
        "migration_revision": "20261119_56",
        "authentication_profile_id": "local-development-v1",
        "authentication_mode": "local_development",
        "scope_policy_id": "workspace-environment-v1",
        "temporal_cluster_profile_id": "local-temporal-v1",
        "temporal_namespace": "default",
        "temporal_task_queue": "spine-r1-qa",
        "temporal_cluster_profile_id": "local-temporal-v1",
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


def test_qa_worker_fails_closed_when_temporal_binding_drifts() -> None:
    settings = QAWorkerSettings(
        service_principal_id=UUID(int=50801),
        build_version="issue-529-test",
        commit_sha="b" * 40,
        database_profile_id="spine-source-postgresql-17",
        scope_policy_id="workspace-environment-v1",
        temporal_task_queue="other-queue",
        temporal_cluster_profile_id="local-temporal-v1",
        capture_service_profile_id="api-qa-capture-v1",
        capture_service_url="http://api:8080",
        capture_worker_credential="worker-capture-credential-for-tests-0001",
        projection_profile_id="r1-cognee-local-v1",
        model_profile_id="r1-provider-local-v1",
    )

    with pytest.raises(ProductionQAAProfileMismatch) as error:
        validate_qa_worker_deployment_profile(settings, _deployment_profile())

    assert error.value.reason_code == "TEMPORAL_BINDING_MISMATCH"
    assert "other-queue" not in str(error.value)


def test_qa_worker_accepts_shared_profile_and_role_specific_capture_secret() -> None:
    settings = QAWorkerSettings(
        service_principal_id=UUID(int=50801),
        build_version="issue-529-test",
        commit_sha="b" * 40,
        database_profile_id="spine-source-postgresql-17",
        scope_policy_id="workspace-environment-v1",
        temporal_cluster_profile_id="local-temporal-v1",
        capture_service_profile_id="api-qa-capture-v1",
        capture_service_url="http://api:8080",
        capture_worker_credential="worker-capture-credential-for-tests-0001",
        projection_profile_id="r1-cognee-local-v1",
        model_profile_id="r1-provider-local-v1",
    )

    validate_qa_worker_deployment_profile(settings, _deployment_profile())


@pytest.mark.asyncio
async def test_worker_readiness_separates_polling_from_completed_execution() -> None:
    class Worker:
        is_running = True

    class Noop:
        async def close(self):
            return None

        async def dispose(self):
            return None

    runtime = ProductionQAWorkerRuntime(
        worker=Worker(),
        runtime_engine=Noop(),
        capture_channel=Noop(),  # type: ignore[arg-type]
        shutdown_timeout_seconds=1,
        health_host="127.0.0.1",
        health_port=8081,
        process_alive=True,
    )
    transport = httpx.ASGITransport(app=create_qa_worker_health_app(runtime))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        liveness = await client.get("/health/live")
        readiness = await client.get("/health/ready")

    assert liveness.json() == {"status": "alive"}
    assert readiness.status_code == 200
    assert readiness.json() == {
        "schema_version": "spine-qa-worker-readiness:v1",
        "status": "ready",
        "process_liveness": "alive",
        "postgresql": "reachable",
        "temporal": "reachable",
        "worker_polling": "polling",
        "application_execution": "not_verified",
    }


@pytest.mark.asyncio
async def test_worker_shutdown_closes_capture_and_database_resources() -> None:
    closed = []

    class Worker:
        async def shutdown(self):
            closed.append("worker")

    class Capture:
        async def close(self):
            closed.append("capture")

    class Engine:
        async def dispose(self):
            closed.append("database")

    runtime = ProductionQAWorkerRuntime(
        worker=Worker(),
        runtime_engine=Engine(),
        capture_channel=Capture(),  # type: ignore[arg-type]
        shutdown_timeout_seconds=1,
        health_host="127.0.0.1",
        health_port=8081,
    )

    await runtime.close()

    assert closed == ["worker", "capture", "database"]


def test_qa_worker_context_is_scoped_to_workflow_input() -> None:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID(int=50802),
        secret=b"issue-508-qa-worker-context-secret",
    )
    workflow_input = AskAdmissionWorkflowInput(
        command_id=UUID(int=50803),
        workspace_id=UUID(int=50804),
        environment_id=UUID(int=50805),
        session_id=UUID(int=50806),
        message_id=UUID(int=50807),
        run_id=UUID(int=50808),
        trace_id=UUID(int=50809),
        authorization_generation=4,
    )

    context = create_qa_worker_context(boundary, UUID(int=50810), workflow_input)

    assert context.scope.workspace_id == workflow_input.workspace_id
    assert context.scope.environment_id == workflow_input.environment_id
    assert context.trace_id == workflow_input.trace_id
    assert context.service_principal_id == UUID(int=50810)
    assert context.operation.value == "answer_question_run"


@pytest.mark.asyncio
async def test_current_authorization_generation_reads_exact_worker_scope() -> None:
    workflow_input = AskAdmissionWorkflowInput(
        command_id=UUID(int=50803),
        workspace_id=UUID(int=50804),
        environment_id=UUID(int=50805),
        session_id=UUID(int=50806),
        message_id=UUID(int=50807),
        run_id=UUID(int=50808),
        trace_id=UUID(int=50809),
        authorization_generation=4,
    )
    command = AskAdmissionCommandData(
        command_id=workflow_input.command_id,
        workspace_id=workflow_input.workspace_id,
        environment_id=workflow_input.environment_id,
        session_id=workflow_input.session_id,
        message_id=workflow_input.message_id,
        run_id=workflow_input.run_id,
        question="synthetic question",
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=50811)),
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="ask-508-current-generation",
    )
    statements = []

    class Result:
        def __init__(self, value=None) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def begin(self):
            return self

        async def execute(self, statement, parameters):
            statements.append((str(statement), parameters))
            return Result(4 if len(statements) == 2 else None)

    service_principal_id = UUID(int=50812)
    generation = await CurrentAuthorizationGeneration(
        Session, service_principal_id
    )(
        workflow_input, command
    )

    assert generation == 4
    assert statements[0][1] == {
        "workspace_id": str(workflow_input.workspace_id),
        "environment_id": str(workflow_input.environment_id),
    }
    assert statements[1][1] == {
        "service_principal_id": str(service_principal_id),
        "workspace_id": str(workflow_input.workspace_id),
        "environment_id": str(workflow_input.environment_id),
        "purpose": "governed_qa.execution.v1",
        "operation": "answer_question_run",
    }
    assert "resolve_current_service_authority" in statements[1][0]


@pytest.mark.asyncio
async def test_current_authorization_generation_rejects_stale_admitted_generation() -> None:
    workflow_input = AskAdmissionWorkflowInput(
        command_id=UUID(int=1),
        workspace_id=UUID(int=2),
        environment_id=UUID(int=3),
        session_id=UUID(int=4),
        message_id=UUID(int=5),
        run_id=UUID(int=6),
        trace_id=UUID(int=7),
        authorization_generation=4,
    )
    command = AskAdmissionCommandData(
        command_id=workflow_input.command_id,
        workspace_id=workflow_input.workspace_id,
        environment_id=workflow_input.environment_id,
        session_id=workflow_input.session_id,
        message_id=workflow_input.message_id,
        run_id=workflow_input.run_id,
        question="synthetic question",
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=8)),
        environment=EnvironmentKind.PRODUCTION,
        idempotency_key="stale-generation",
    )

    class Result:
        def __init__(self, value=None) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def begin(self):
            return self

        async def execute(self, *_):
            type(self).calls += 1
            return Result(5 if type(self).calls == 2 else None)

    with pytest.raises(AskAdmissionCommandUnavailable):
        await CurrentAuthorizationGeneration(Session, UUID(int=9))(
            workflow_input, command
        )
