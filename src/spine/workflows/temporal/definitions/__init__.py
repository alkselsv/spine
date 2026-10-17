"""Deterministic Temporal workflow definitions.

The SDK adapter registers this framework-independent definition with the real
worker.  Protected command loading and capability execution remain activity
concerns; the workflow input itself contains identities and lineage only.
"""

from __future__ import annotations

from typing import Protocol

from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionWorkflowInput,
)


class AskAdmissionActivity(Protocol):
    async def execute(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AuthorizedAskResult: ...


class AskAdmissionWorkflowDefinition:
    """Stable workflow body whose side effects are delegated to one activity."""

    def __init__(self, activity: AskAdmissionActivity) -> None:
        self._activity = activity

    async def run(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
        return await self._activity.execute(workflow_input)


__all__ = ["AskAdmissionActivity", "AskAdmissionWorkflowDefinition"]
