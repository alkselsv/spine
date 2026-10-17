from __future__ import annotations

from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.workflows.ask_admission_dispatch import (
    ASK_ADMISSION_WORKFLOW_TYPE,
)
from spine.workflows.temporal.bootstrap import (
    ASK_ADMISSION_CONSUMER_NAME,
    build_ask_admission_delivery_binding,
    register_ask_admission_delivery_route,
)


class Client:
    pass


def test_temporal_bootstrap_registers_one_versioned_ask_route() -> None:
    routes = OutboxDeliveryRouteRegistry(version="v1")
    register_ask_admission_delivery_route(routes)
    routes.seal()

    route = routes.resolve("qa.ask-admitted", 1)
    assert route.consumer_name == ASK_ADMISSION_CONSUMER_NAME


def test_temporal_bootstrap_builds_existing_worker_binding() -> None:
    client = Client()
    binding = build_ask_admission_delivery_binding(client)  # type: ignore[arg-type]

    assert binding.consumer_name == ASK_ADMISSION_CONSUMER_NAME
    assert binding.consumer is not None
    assert ASK_ADMISSION_WORKFLOW_TYPE == "spine.qa.ask-admission:v1"
