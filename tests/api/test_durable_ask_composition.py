from __future__ import annotations

from spine.api.composition import compose_durable_ask
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
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
    assert routes.resolve("qa.ask-admitted", 1).consumer_name == (
        ASK_ADMISSION_CONSUMER_NAME
    )
    assert routes.resolve("qa.ask-cancellation-requested", 1).consumer_name == (
        ASK_CANCELLATION_CONSUMER_NAME
    )
    assert len(composition.activity_bindings) == 2
    assert composition.workflow_executor is not None
    assert ASK_ADMISSION_WORKFLOW_TYPE == "spine.qa.ask-admission:v1"

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


class ElapsedPort:
    async def record_worker_execution_elapsed(self, sample) -> None:
        del sample
