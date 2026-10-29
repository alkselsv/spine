"""Deterministic durable state for the R2 requirements human wait."""

from __future__ import annotations

from enum import Enum
from typing import Any

from temporalio import workflow

from spine.application.persistence.workflow_runs import (
    ClarificationAnsweredDispatchPayload,
    ClarificationRequestedDispatchPayload,
    WorkflowCancelledDispatchPayload,
)
from spine.application.workflows.run_start_dispatch import (
    WORKFLOW_RUN_START_TYPE,
    WorkflowRunStartInput,
)


class RequirementsWorkflowExecutionState(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    CANCELLED = "cancelled"


@workflow.defn(name=WORKFLOW_RUN_START_TYPE)
class RequirementsWorkflowDefinition:
    """Keep one pinned WorkflowRun alive across human clarification signals."""

    def __init__(self) -> None:
        self._input: WorkflowRunStartInput | None = None
        self._state = RequirementsWorkflowExecutionState.RUNNING
        self._clarification_id: str | None = None
        self._artifact_id: str | None = None
        self._artifact_version: int | None = None
        self._last_command_id: str | None = None

    @workflow.run
    async def run(self, raw_input: dict[str, Any]) -> dict[str, Any]:
        self._input = WorkflowRunStartInput.model_validate(raw_input, strict=False)
        await workflow.wait_condition(
            lambda: self._state is RequirementsWorkflowExecutionState.CANCELLED
        )
        return self.status()

    @workflow.signal(name="clarification_requested")
    def clarification_requested(self, raw_payload: dict[str, Any]) -> None:
        payload = ClarificationRequestedDispatchPayload.model_validate(
            raw_payload, strict=False
        )
        self._validate_identity(payload.run_id, payload.workflow_version_id)
        if self._state is RequirementsWorkflowExecutionState.CANCELLED:
            return
        if self._state is RequirementsWorkflowExecutionState.WAITING:
            if (
                self._clarification_id == str(payload.clarification_id)
                and self._artifact_id == str(payload.artifact_id)
                and self._artifact_version == payload.artifact_version
            ):
                return
            raise ValueError("a different clarification is already waiting")
        self._state = RequirementsWorkflowExecutionState.WAITING
        self._clarification_id = str(payload.clarification_id)
        self._artifact_id = str(payload.artifact_id)
        self._artifact_version = payload.artifact_version

    @workflow.signal(name="clarification_answered")
    def clarification_answered(self, raw_payload: dict[str, Any]) -> None:
        payload = ClarificationAnsweredDispatchPayload.model_validate(
            raw_payload, strict=False
        )
        self._validate_identity(payload.run_id, payload.workflow_version_id)
        if self._state is RequirementsWorkflowExecutionState.CANCELLED:
            return
        if self._last_command_id == str(payload.command_id):
            return
        if (
            self._state is not RequirementsWorkflowExecutionState.WAITING
            or self._clarification_id != str(payload.clarification_id)
            or self._artifact_id != str(payload.artifact_id)
            or self._artifact_version is None
            or payload.artifact_version != self._artifact_version + 1
        ):
            raise ValueError("clarification answer does not match the durable wait")
        self._state = RequirementsWorkflowExecutionState.RUNNING
        self._artifact_version = payload.artifact_version
        self._last_command_id = str(payload.command_id)

    @workflow.signal(name="run_cancelled")
    def run_cancelled(self, raw_payload: dict[str, Any]) -> None:
        payload = WorkflowCancelledDispatchPayload.model_validate(
            raw_payload, strict=False
        )
        self._validate_identity(payload.run_id, payload.workflow_version_id)
        self._last_command_id = str(payload.command_id)
        self._state = RequirementsWorkflowExecutionState.CANCELLED

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "run_id": str(self._input.run_id) if self._input is not None else None,
            "workflow_version_id": (
                str(self._input.workflow_version_id) if self._input is not None else None
            ),
            "clarification_id": self._clarification_id,
            "artifact_id": self._artifact_id,
            "artifact_version": self._artifact_version,
            "last_command_id": self._last_command_id,
        }

    def _validate_identity(self, run_id: object, workflow_version_id: object) -> None:
        if self._input is None:
            raise ValueError("workflow input is unavailable")
        if (
            run_id != self._input.run_id
            or workflow_version_id != self._input.workflow_version_id
        ):
            raise ValueError("workflow signal does not match the pinned run")


__all__ = [
    "RequirementsWorkflowDefinition",
    "RequirementsWorkflowExecutionState",
]
