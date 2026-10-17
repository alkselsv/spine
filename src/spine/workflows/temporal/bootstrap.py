"""Composition helpers for the Q&A Temporal dispatch boundary.

The concrete Temporal SDK client is supplied by the composition root through
the framework-independent client port. This keeps unit tests deterministic and
keeps Temporal types out of application and domain contracts.
"""

from __future__ import annotations

from spine.application.persistence.ask_admission import (
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
    QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
    QA_ASK_CANCELLATION_EVENT_TYPE,
)
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionDispatchConsumer,
    AskAdmissionWorkflowClient,
    AskAdmissionWorkflowLauncher,
    AskCancellationDispatchConsumer,
)
from spine.workflows.temporal.definitions import (
    AskAdmissionActivity,
    AskAdmissionWorkflowDefinition,
    build_ask_admission_activity_bindings as _build_ask_admission_activity_bindings,
)
from spine.workers import OutboxConsumerBinding

ASK_ADMISSION_CONSUMER_NAME = "qa_ask_admission"
ASK_CANCELLATION_CONSUMER_NAME = "qa_ask_cancellation"


def register_ask_admission_delivery_route(
    routes: OutboxDeliveryRouteRegistry,
) -> None:
    """Register the one versioned outbox route owned by this bootstrap."""

    routes.register(
        event_type=QA_ASK_ADMISSION_EVENT_TYPE,
        schema_version=QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
        consumer_name=ASK_ADMISSION_CONSUMER_NAME,
    )
    routes.register(
        event_type=QA_ASK_CANCELLATION_EVENT_TYPE,
        schema_version=QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
        consumer_name=ASK_CANCELLATION_CONSUMER_NAME,
    )


def build_ask_admission_delivery_binding(
    client: AskAdmissionWorkflowClient,
) -> OutboxConsumerBinding:
    """Build the explicit consumer binding for the existing outbox worker."""

    return OutboxConsumerBinding(
        consumer_name=ASK_ADMISSION_CONSUMER_NAME,
        consumer=AskAdmissionDispatchConsumer(AskAdmissionWorkflowLauncher(client)),
    )


def build_ask_cancellation_delivery_binding(
    client: AskAdmissionWorkflowClient,
) -> OutboxConsumerBinding:
    """Build the durable Temporal signal consumer for cancellation intents."""

    return OutboxConsumerBinding(
        consumer_name=ASK_CANCELLATION_CONSUMER_NAME,
        consumer=AskCancellationDispatchConsumer(client),
    )


def build_ask_admission_workflow_definition() -> type[AskAdmissionWorkflowDefinition]:
    """Return the registered workflow class for Temporal worker composition."""

    return AskAdmissionWorkflowDefinition


def build_ask_admission_activity_bindings(
    activity: AskAdmissionActivity,
) -> tuple[object, object]:
    """Bind the application-owned execute/cancel operations as SDK activities."""

    return _build_ask_admission_activity_bindings(activity)


__all__ = [
    "ASK_ADMISSION_CONSUMER_NAME",
    "ASK_CANCELLATION_CONSUMER_NAME",
    "build_ask_cancellation_delivery_binding",
    "build_ask_admission_delivery_binding",
    "build_ask_admission_activity_bindings",
    "build_ask_admission_workflow_definition",
    "register_ask_admission_delivery_route",
]
