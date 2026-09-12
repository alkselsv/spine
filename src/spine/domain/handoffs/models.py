from __future__ import annotations

from enum import Enum
from uuid import UUID, uuid4

from pydantic import Field

from spine.domain.common import DefinitionModel, SchemaRef, SpineModel


class OnFailure(str, Enum):
    RETRY = "retry"
    FALLBACK = "fallback"
    HUMAN_REVIEW = "human_review"
    STOP = "stop"


class HandoffStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class HandoffPolicy(DefinitionModel):
    from_step: str
    to_step: str
    artifact_schema: SchemaRef
    evaluator_keys: tuple[str, ...] = ()
    minimum_score: float | None = Field(default=None, ge=0.0, le=1.0)
    on_failure: OnFailure = OnFailure.STOP


class Handoff(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    work_item_id: UUID
    artifact_id: UUID
    policy: HandoffPolicy
    status: HandoffStatus = HandoffStatus.PENDING
