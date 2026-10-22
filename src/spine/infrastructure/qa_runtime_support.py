"""Shared infrastructure helpers for the API and Q&A worker process roots."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionCommandData,
    AskAdmissionCommandUnavailable,
    AskAdmissionWorkflowInput,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


def create_qa_worker_context(
    boundary: TrustedContextBoundary,
    service_principal_id: UUID,
    workflow_input: AskAdmissionWorkflowInput,
) -> TrustedPersistenceContext:
    """Issue persistence authority only for one admitted workflow scope."""

    return boundary.worker(
        scope=EnvironmentScope(
            workspace_id=workflow_input.workspace_id,
            environment_id=workflow_input.environment_id,
        ),
        service_principal_id=service_principal_id,
        purpose=PersistencePurpose("governed_qa.execution.v1"),
        operation=PersistenceOperation("answer_question_run"),
        trace_id=workflow_input.trace_id,
    )


class CurrentAskAdmissionAuthorizer:
    """Fail closed when canonical admitted command identity is incomplete."""

    def __init__(self, configuration: object | None = None) -> None:
        self._configuration = configuration

    async def authorize(
        self,
        *,
        workflow_input: AskAdmissionWorkflowInput,
        command: AskAdmissionCommandData,
    ) -> bool:
        allowed = (
            command.matches(workflow_input)
            and command.subject is not None
            and command.subject.id.int != 0
            and command.environment is not None
            and command.idempotency_key is not None
        )
        if not allowed:
            return False
        prepare = getattr(self._configuration, "prepare", None)
        if not callable(prepare):
            return False
        await prepare(command)
        return True


class DiscardWorkerElapsedCapture:
    """Accept optional worker elapsed measurements without external effects."""

    async def record_worker_execution_elapsed(self, sample: object) -> None:
        del sample


class CurrentAuthorizationGeneration:
    """Resolve fresh service authority at the durable activity boundary."""

    def __init__(
        self,
        session_factory: object,
        service_principal_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._service_principal_id = service_principal_id

    async def __call__(
        self,
        workflow_input: AskAdmissionWorkflowInput,
        command: AskAdmissionCommandData,
    ) -> int:
        if (
            command.workspace_id != workflow_input.workspace_id
            or command.environment_id != workflow_input.environment_id
        ):
            raise AskAdmissionCommandUnavailable(
                "Admitted Ask authority scope is unavailable."
            )
        session = self._session_factory()  # type: ignore[operator]
        async with session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                        "set_config('spine.environment_id', :environment_id, true)"
                    ),
                    {
                        "workspace_id": str(workflow_input.workspace_id),
                        "environment_id": str(workflow_input.environment_id),
                    },
                )
                generation = (
                    await session.execute(
                        text(
                            "SELECT spine.resolve_current_service_authority("
                            ":service_principal_id, :workspace_id, :environment_id, "
                            ":purpose, :operation)"
                        ),
                        {
                            "service_principal_id": str(self._service_principal_id),
                            "workspace_id": str(workflow_input.workspace_id),
                            "environment_id": str(workflow_input.environment_id),
                            "purpose": "governed_qa.execution.v1",
                            "operation": "answer_question_run",
                        },
                    )
                ).scalar_one_or_none()
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or generation != workflow_input.authorization_generation
        ):
            raise AskAdmissionCommandUnavailable(
                "Current authorization generation is unavailable."
            )
        return generation


__all__ = [
    "CurrentAskAdmissionAuthorizer",
    "CurrentAuthorizationGeneration",
    "DiscardWorkerElapsedCapture",
    "create_qa_worker_context",
]
