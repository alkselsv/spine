from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
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
    return registry
