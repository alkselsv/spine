from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from spine.domain.common import SpineModel


class EvaluationVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"


class EvaluationResult(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    evaluator_key: str
    evaluator_version: str
    subject_type: str
    subject_id: UUID
    verdict: EvaluationVerdict
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    details: dict[str, Any] = Field(default_factory=dict)
