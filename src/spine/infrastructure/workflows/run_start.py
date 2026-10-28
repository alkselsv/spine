"""Outbox route and consumer binding for canonical Workflow Run starts."""

from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.persistence.workflow_runs import (
    START_WORKFLOW_EVENT_SCHEMA_VERSION,
    START_WORKFLOW_EVENT_TYPE,
)
from spine.application.workflows.run_start_dispatch import (
    WorkflowRunStartClient,
    WorkflowRunStartDispatchConsumer,
    WorkflowRunStartLauncher,
)
from spine.workers.composition import OutboxConsumerBinding

WORKFLOW_RUN_START_CONSUMER_NAME = "workflow_run_start"


def register_workflow_run_start_delivery_route(
    routes: OutboxDeliveryRouteRegistry,
) -> None:
    routes.register(
        event_type=START_WORKFLOW_EVENT_TYPE,
        schema_version=START_WORKFLOW_EVENT_SCHEMA_VERSION,
        consumer_name=WORKFLOW_RUN_START_CONSUMER_NAME,
    )


def build_workflow_run_start_delivery_binding(
    client: WorkflowRunStartClient,
) -> OutboxConsumerBinding:
    return OutboxConsumerBinding(
        consumer_name=WORKFLOW_RUN_START_CONSUMER_NAME,
        consumer=WorkflowRunStartDispatchConsumer(WorkflowRunStartLauncher(client)),
    )


__all__ = [
    "WORKFLOW_RUN_START_CONSUMER_NAME",
    "build_workflow_run_start_delivery_binding",
    "register_workflow_run_start_delivery_route",
]
