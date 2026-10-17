"""Composition helpers for the Q&A Temporal dispatch boundary.

The concrete Temporal SDK client is supplied by the composition root through
the framework-independent client port. This keeps unit tests deterministic and
keeps Temporal types out of application and domain contracts.
"""

from __future__ import annotations

from spine.application.persistence.ask_admission import (
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
)
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionDispatchConsumer,
    AskAdmissionWorkflowClient,
    AskAdmissionWorkflowLauncher,
)
from spine.workers import OutboxConsumerBinding

ASK_ADMISSION_CONSUMER_NAME = "qa_ask_admission"


def register_ask_admission_delivery_route(
    routes: OutboxDeliveryRouteRegistry,
) -> None:
    """Register the one versioned outbox route owned by this bootstrap."""

    routes.register(
        event_type=QA_ASK_ADMISSION_EVENT_TYPE,
        schema_version=QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
        consumer_name=ASK_ADMISSION_CONSUMER_NAME,
    )


def build_ask_admission_delivery_binding(
    client: AskAdmissionWorkflowClient,
) -> OutboxConsumerBinding:
    """Build the explicit consumer binding for the existing outbox worker."""

    return OutboxConsumerBinding(
        consumer_name=ASK_ADMISSION_CONSUMER_NAME,
        consumer=AskAdmissionDispatchConsumer(AskAdmissionWorkflowLauncher(client)),
    )


__all__ = [
    "ASK_ADMISSION_CONSUMER_NAME",
    "build_ask_admission_delivery_binding",
    "register_ask_admission_delivery_route",
]
