"""Outbox route and consumer binding for canonical Workflow Run starts."""

from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.persistence.workflow_runs import (
    CLARIFICATION_ANSWERED_EVENT_TYPE,
    CLARIFICATION_REQUESTED_EVENT_TYPE,
    START_WORKFLOW_EVENT_SCHEMA_VERSION,
    START_WORKFLOW_EVENT_TYPE,
    WORKFLOW_CANCELLED_EVENT_TYPE,
    REQUIREMENTS_ACCEPTED_EVENT_TYPE,
    WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
)
from spine.application.workflows.requirements_clarification_dispatch import (
    RequirementsWorkflowSignalClient,
    RequirementsWorkflowSignalDispatchConsumer,
)
from spine.application.workflows.run_start_dispatch import (
    WorkflowRunStartClient,
    WorkflowRunStartDispatchConsumer,
    WorkflowRunStartLauncher,
)
from spine.workers.composition import OutboxConsumerBinding

WORKFLOW_RUN_START_CONSUMER_NAME = "workflow_run_start"
WORKFLOW_RUN_SIGNAL_CONSUMER_NAME = "workflow_run_signal"


def register_workflow_run_start_delivery_route(
    routes: OutboxDeliveryRouteRegistry,
) -> None:
    routes.register(
        event_type=START_WORKFLOW_EVENT_TYPE,
        schema_version=START_WORKFLOW_EVENT_SCHEMA_VERSION,
        consumer_name=WORKFLOW_RUN_START_CONSUMER_NAME,
    )
    for event_type in (
        CLARIFICATION_REQUESTED_EVENT_TYPE,
        CLARIFICATION_ANSWERED_EVENT_TYPE,
        WORKFLOW_CANCELLED_EVENT_TYPE,
        REQUIREMENTS_ACCEPTED_EVENT_TYPE,
    ):
        routes.register(
            event_type=event_type,
            schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
            consumer_name=WORKFLOW_RUN_SIGNAL_CONSUMER_NAME,
        )


def build_workflow_run_start_delivery_binding(
    client: WorkflowRunStartClient,
) -> OutboxConsumerBinding:
    return OutboxConsumerBinding(
        consumer_name=WORKFLOW_RUN_START_CONSUMER_NAME,
        consumer=WorkflowRunStartDispatchConsumer(WorkflowRunStartLauncher(client)),
    )


def build_workflow_run_signal_delivery_binding(
    client: RequirementsWorkflowSignalClient,
) -> OutboxConsumerBinding:
    return OutboxConsumerBinding(
        consumer_name=WORKFLOW_RUN_SIGNAL_CONSUMER_NAME,
        consumer=RequirementsWorkflowSignalDispatchConsumer(client),
    )


__all__ = [
    "WORKFLOW_RUN_START_CONSUMER_NAME",
    "WORKFLOW_RUN_SIGNAL_CONSUMER_NAME",
    "build_workflow_run_start_delivery_binding",
    "build_workflow_run_signal_delivery_binding",
    "register_workflow_run_start_delivery_route",
]
