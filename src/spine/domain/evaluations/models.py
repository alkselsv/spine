from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator

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
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    verdict: EvaluationVerdict
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_qa_scope(self) -> "EvaluationResult":
        if self.subject_type == "qa_case" and (self.workspace_id is None or self.environment_id is None):
            raise ValueError("qa_case evaluations require workspace and environment scope")
        return self
