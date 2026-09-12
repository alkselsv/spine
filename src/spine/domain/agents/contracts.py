from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from spine.domain.common import ActorRef, SpineModel


class AgentRunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


class AgentInvocation(SpineModel):
    work_item_id: UUID
    step_run_id: UUID
    capability: str
    acting_on_behalf_of: ActorRef
    input_artifact_ids: tuple[UUID, ...] = ()
    context_request: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


class AgentResult(SpineModel):
    status: AgentRunStatus
    output_artifact_ids: tuple[UUID, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    structured_metrics: dict[str, float] = Field(default_factory=dict)
    proposed_action_ids: tuple[UUID, ...] = ()
    trace_ref: str | None = None
    error: str | None = None
