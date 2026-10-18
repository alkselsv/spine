"""Attempt-scoped worker timing handoff for the Q&A observer boundary."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator

from spine.domain.common import ValidatedDefinitionModel


WORKER_EXECUTION_ELAPSED_SCHEMA = "r1-qa-worker-execution-elapsed:v1"


class WorkerExecutionElapsedSample(ValidatedDefinitionModel):
    """Safe scalar timing metadata for one completed worker activity attempt."""

    schema_version: Literal[WORKER_EXECUTION_ELAPSED_SCHEMA] = (
        WORKER_EXECUTION_ELAPSED_SCHEMA
    )
    run_id: UUID
    attempt_id: UUID
    capture_id: UUID | None = None
    worker_execution_elapsed_ns: int = Field(ge=0)

    @field_validator("run_id", "attempt_id", "capture_id")
    @classmethod
    def require_non_zero_id(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("worker timing identifiers must be non-zero")
        return value


class WorkerExecutionElapsedCapturePort(Protocol):
    """Receive one safe timing sample through the C0 observer boundary."""

    async def record_worker_execution_elapsed(
        self, sample: WorkerExecutionElapsedSample
    ) -> None: ...


__all__ = [
    "WorkerExecutionElapsedCapturePort",
    "WorkerExecutionElapsedSample",
]
