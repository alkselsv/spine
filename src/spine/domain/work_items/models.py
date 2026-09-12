from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from spine.domain.common import ActorRef, EntityRef, EnvironmentKind, SpineModel


class WorkItemStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkItem(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    environment: EnvironmentKind
    workflow_version_id: UUID
    title: str
    status: WorkItemStatus = WorkItemStatus.PENDING
    triggered_by: ActorRef
    subject: EntityRef | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
