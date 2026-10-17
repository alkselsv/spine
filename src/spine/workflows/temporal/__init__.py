"""Temporal adapter boundary.

Temporal is intentionally not imported until the orchestration phase.  Domain
workflow definitions remain usable without the SDK installed.
"""

from spine.workflows.temporal.bootstrap import (
    ASK_ADMISSION_CONSUMER_NAME,
    build_ask_admission_delivery_binding,
    build_ask_admission_workflow_definition,
    register_ask_admission_delivery_route,
)
from spine.workflows.temporal.client import (
    TemporalSdkClient,
    TemporalSdkWorkflowClient,
)

__all__ = [
    "ASK_ADMISSION_CONSUMER_NAME",
    "build_ask_admission_delivery_binding",
    "build_ask_admission_workflow_definition",
    "register_ask_admission_delivery_route",
    "TemporalSdkClient",
    "TemporalSdkWorkflowClient",
]
