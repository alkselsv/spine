from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.api.composition import compose_durable_ask
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.production_qa_composition import (
    ProductionQAACompositionEvidence,
    _issue_production_qa_composition_evidence,
)
from spine.application.production_runtime_readiness import ProductionRuntimeProfile
from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
)
from spine.workflows.temporal.bootstrap import (
    ASK_ADMISSION_CONSUMER_NAME,
    ASK_CANCELLATION_CONSUMER_NAME,
    build_ask_admission_workflow_definition,
)


def test_durable_ask_composition_wires_api_outbox_and_temporal_executor(
    monkeypatch,
) -> None:
    routes = OutboxDeliveryRouteRegistry(version="v1")
    composition = compose_durable_ask(
        capability=object(),  # type: ignore[arg-type]
        sessions=object(),  # type: ignore[arg-type]
        admission=object(),  # type: ignore[arg-type]
        attempts=object(),  # type: ignore[arg-type]
        command_loader=object(),  # type: ignore[arg-type]
        persistence_context_factory=lambda _workflow: object(),  # type: ignore[arg-type]
        temporal_client=object(),  # type: ignore[arg-type]
        delivery_routes=routes,
        monotonic_ns=lambda: 100,
        worker_execution_elapsed_port=ElapsedPort(),
    )

    assert composition.workflow_definition is build_ask_admission_workflow_definition()
    assert [binding.consumer_name for binding in composition.delivery_bindings] == [
        ASK_ADMISSION_CONSUMER_NAME,
        ASK_CANCELLATION_CONSUMER_NAME,
    ]
    assert routes.resolve("qa.ask-admitted", 2).consumer_name == (
        ASK_ADMISSION_CONSUMER_NAME
    )
    assert routes.resolve("qa.ask-cancellation-requested", 1).consumer_name == (
        ASK_CANCELLATION_CONSUMER_NAME
    )
    assert len(composition.activity_bindings) == 2
    assert composition.workflow_executor is not None
    assert composition.real_qualification_evidence is None
    assert ASK_ADMISSION_WORKFLOW_TYPE == "spine.qa.ask-admission:v1"
    with pytest.raises(RuntimeError, match="composed SDK client"):
        composition.create_temporal_worker()

    from spine.api import main

    received: dict[str, object] = {}

    def create_app(**dependencies: object) -> object:
        received.update(dependencies)
        return object()

    monkeypatch.setattr(main, "create_app", create_app)
    injected_clock = lambda: 42
    application = composition.create_api_app(clock=injected_clock)

    assert application is not None
    assert received["ask_admission_service"] is composition.admission
    assert received["authorized_ask_service"] is composition.api_ask_service
    assert received["clock"] is injected_clock

    with pytest.raises(RuntimeError, match="incomplete or mismatched"):
        composition.create_trusted_runtime_preflight(
            profile=_runtime_profile(),
            inspector=SimpleNamespace(is_fake=False, inspect=lambda: None),
        )


def test_production_qualification_evidence_cannot_be_forged_by_a_fake_composition() -> None:
    fake = object()

    with pytest.raises(TypeError, match="API composition root"):
        ProductionQAACompositionEvidence(fake, object())  # type: ignore[arg-type]


def test_validated_production_graph_receives_qualification_evidence(monkeypatch) -> None:
    patched_types = {
        "spine.application.ask_admission": ("PersistedAskAdmissionService",),
        "spine.application.authorized_ask": ("PersistedAuthorizedAskService",),
        "spine.application.production_qa_capability": (
            "ProductionAnswerQuestionCapability",
            "PersistedAnswerQuestionCandidateRunner",
            "ExistingAnswerQuestionCandidateValidator",
        ),
        "spine.application.answer_question_context": ("AnswerQuestionContextService",),
        "spine.application.answer_disclosure": ("AnswerDisclosureGate",),
        "spine.application.authorized_context_broker": (
            "AuthorizedContextBrokerApplicationService",
        ),
        "spine.application.qa_observation.admission": ("ObserverAskAdmission",),
        "spine.application.qa_sessions": ("PersistedQASessionService",),
        "spine.application.answer_question_execution": ("AnswerQuestionExecutionBoundary",),
        "spine.application.workflows.ask_admission_dispatch": (
            "AskAdmissionWorkflowExecutor",
            "AskAdmissionDispatchConsumer",
            "AskCancellationDispatchConsumer",
        ),
        "spine.infrastructure.auth.trusted_context": ("TrustedRequestContextBoundary",),
        "spine.infrastructure.persistence.postgresql": (
            "PostgreSQLTenantUnitOfWorkFactory",
        ),
        "spine.infrastructure.qa_observation_capture.http": ("HttpCaptureChannel",),
        "spine.workflows.temporal.client": ("TemporalSdkWorkflowClient",),
    }
    fake_types: dict[str, type] = {}
    for module_name, names in patched_types.items():
        module = importlib.import_module(module_name)
        for name in names:
            fake_type = type(f"TrustedTest{name}", (), {})
            monkeypatch.setattr(module, name, fake_type)
            fake_types[name] = fake_type

    class SDKClient:
        pass

    temporal_module = importlib.import_module("temporalio.client")
    monkeypatch.setattr(temporal_module, "Client", SDKClient)
    temporal_client = fake_types["TemporalSdkWorkflowClient"]()
    temporal_client._client = SDKClient()
    service = fake_types["PersistedAuthorizedAskService"]()
    capability = fake_types["ProductionAnswerQuestionCapability"]()
    capability.execution = fake_types["AnswerQuestionExecutionBoundary"]()
    capability.context = fake_types["AnswerQuestionContextService"]()
    capability.context._broker = fake_types["AuthorizedContextBrokerApplicationService"]()
    capability.disclosure_gate = fake_types["AnswerDisclosureGate"]()
    capability.candidate_runner = fake_types["PersistedAnswerQuestionCandidateRunner"]()
    capability.validator = fake_types["ExistingAnswerQuestionCandidateValidator"]()
    service.capability = capability
    admission = fake_types["PersistedAskAdmissionService"]()
    admission.uow_factory = fake_types["PostgreSQLTenantUnitOfWorkFactory"]()
    admission.uow_factory._context_verifier = fake_types["TrustedRequestContextBoundary"]()
    executor = fake_types["AskAdmissionWorkflowExecutor"]()
    executor._service = service
    observer_admission = fake_types["ObserverAskAdmission"]()
    channel = fake_types["HttpCaptureChannel"]()
    observer_admission._admission = admission
    observer_admission._channel = channel

    class AdmissionConsumer:
        pass

    class CancellationConsumer:
        pass

    monkeypatch.setattr(
        importlib.import_module("spine.application.workflows.ask_admission_dispatch"),
        "AskAdmissionDispatchConsumer",
        AdmissionConsumer,
    )
    monkeypatch.setattr(
        importlib.import_module("spine.application.workflows.ask_admission_dispatch"),
        "AskCancellationDispatchConsumer",
        CancellationConsumer,
    )
    admission_consumer = AdmissionConsumer()
    admission_consumer._launcher = SimpleNamespace(_client=temporal_client)
    cancellation_consumer = CancellationConsumer()
    cancellation_consumer._client = temporal_client

    from spine.api.composition import DurableAskComposition
    from spine.workflows.temporal.bootstrap import build_ask_admission_activity_bindings
    from spine.workers.composition import OutboxConsumerBinding

    composition = DurableAskComposition(
        admission=admission,
        api_ask_service=service,
        workflow_executor=executor,
        workflow_client=temporal_client,
        delivery_bindings=(
            OutboxConsumerBinding("qa_ask_admission", admission_consumer),
            OutboxConsumerBinding("qa_ask_cancellation", cancellation_consumer),
        ),
        workflow_definition=build_ask_admission_workflow_definition(),
        activity_bindings=build_ask_admission_activity_bindings(executor),
        observer_admission=observer_admission,
        sessions=fake_types["PersistedQASessionService"](),
        capture_channel=channel,
    )

    evidence = composition.real_qualification_evidence

    assert evidence is not None
    assert evidence.matches(composition)

    from spine.application.production_runtime_readiness import (
        ProductionRuntimeInspection,
        RuntimeReadinessStatus,
    )
    from spine.evals.real_benchmark_qualification import (
        PrerequisiteState,
        RealBenchmarkPreflight,
        RuntimePrerequisite,
        RuntimePrerequisiteCheck,
    )

    class RuntimeInspector:
        is_fake = False
        production_evidence = evidence

        def __init__(self, observed_profile=None) -> None:
            self.observed_profile = observed_profile or _runtime_profile()

        async def inspect(self):
            return ProductionRuntimeInspection(
                profile=self.observed_profile,
                preflight=RealBenchmarkPreflight(
                    workspace_id=UUID(int=1),
                    environment_id=UUID(int=2),
                    prerequisites=tuple(
                        RuntimePrerequisiteCheck(
                            prerequisite=item,
                            state=PrerequisiteState.READY,
                        )
                        for item in RuntimePrerequisite
                    ),
                ),
            )

    runtime_preflight = composition.create_trusted_runtime_preflight(
        profile=_runtime_profile(),
        inspector=RuntimeInspector(),
    )
    report = asyncio.run(runtime_preflight.report())

    assert report.configuration is RuntimeReadinessStatus.READY
    assert report.qualification is RuntimeReadinessStatus.READY

    mismatched_preflight = composition.create_trusted_runtime_preflight(
        profile=_runtime_profile(),
        inspector=RuntimeInspector(
            _runtime_profile().model_copy(
                update={"temporal_task_queue": "spine-r1-other"}
            )
        ),
    )
    mismatched_preflight_result = asyncio.run(mismatched_preflight.inspect())
    production_check = next(
        check
        for check in mismatched_preflight_result.prerequisites
        if check.prerequisite is RuntimePrerequisite.PRODUCTION_COMPOSITION
    )
    assert production_check.state is PrerequisiteState.UNAVAILABLE
    assert mismatched_preflight.readiness_report is not None
    assert (
        mismatched_preflight.readiness_report.configuration
        is RuntimeReadinessStatus.BLOCKED
    )

    class FakeInspector(RuntimeInspector):
        is_fake = True

    with pytest.raises(TypeError, match="is_fake=False"):
        composition.create_trusted_runtime_preflight(
            profile=_runtime_profile(),
            inspector=FakeInspector(),
        )

    mismatched_runner = SimpleNamespace(
        supports_real_qualification=True,
        production_qualification_evidence=_issue_production_qa_composition_evidence(
            object(), object()  # type: ignore[arg-type]
        ),
    )
    with pytest.raises(RuntimeError, match="different production composition"):
        composition.create_real_benchmark_qualification_runtime(
            profile=_runtime_profile(),
            inspector=RuntimeInspector(),
            runner=mismatched_runner,  # type: ignore[arg-type]
        )


def _runtime_profile() -> ProductionRuntimeProfile:
    return ProductionRuntimeProfile(
        postgresql_major=17,
        migration_revision="migration:v1",
        temporal_namespace="spine-r1",
        temporal_task_queue="spine-r1-qa",
        worker_roles=("qa", "outbox"),
        authentication_mode="oidc",
        storage_profile="original-storage:v1",
        context_projection_provider="cognee:1.5.4",
        projection_backend_versions=("neo4j:5.26.32", "pgvector:0.8.7"),
        llm_model_version="provider:model-v1",
        embedding_model_version="provider:embedding-v1",
        embedding_dimensions=1536,
        web_release_identity="web:release-v1",
        api_release_identity="api:release-v1",
        commit_sha="b" * 40,
        deployment_fingerprint="a" * 64,
    )


class ElapsedPort:
    async def record_worker_execution_elapsed(self, sample) -> None:
        del sample
