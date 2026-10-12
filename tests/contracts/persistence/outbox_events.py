from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
    CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
    CanonicalAcceptanceOutboxPayload,
)


class WorkspaceCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: OpaqueObjectReference
    lifecycle_state: str


def create_outbox_event_registry() -> OutboxEventRegistry:
    registry = OutboxEventRegistry()
    registry.register(
        event_type="workspace.created",
        schema_version=1,
        payload_type=WorkspaceCreatedPayload,
    )
    registry.register(
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload_type=CanonicalAcceptanceOutboxPayload,
    )
    return registry
