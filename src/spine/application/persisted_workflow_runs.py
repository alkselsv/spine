"""Transactional use cases for starting and reading Workflow Runs."""

from __future__ import annotations

from uuid import UUID

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.workflow_runs import (
    StartWorkflowCommand,
    WorkflowCommand,
    workflow_run_snapshot,
)
from spine.application.persistence.workflow_runs import (
    validate_workflow_command_context,
    validate_workflow_run_context,
)
from spine.domain.workflows import (
    WorkflowCommandReceipt,
    WorkflowEventPage,
    WorkflowRunSnapshot,
)


class PersistedWorkflowRunAdmissionService:
    """Commit one start through the trusted tenant Unit of Work."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def start(
        self, *, context: TrustedPersistenceContext, command: StartWorkflowCommand
    ) -> WorkflowCommandReceipt:
        validate_workflow_run_context(context, command)
        async with self._uow_factory(context) as uow:
            receipt = await uow.workflow_runs.admit(command)
            await uow.commit()
        return receipt

    async def get_run(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
    ) -> WorkflowRunSnapshot | None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Workflow read context is invalid.")
        async with self._uow_factory(context) as uow:
            run = await uow.workflow_runs.get_run(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                run_id=run_id,
            )
            return workflow_run_snapshot(run) if run is not None else None

    async def dispatch(
        self, *, context: TrustedPersistenceContext, command: WorkflowCommand
    ) -> WorkflowCommandReceipt | None:
        validate_workflow_command_context(context, command)
        async with self._uow_factory(context) as uow:
            run = await uow.workflow_runs.get_run(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                run_id=command.run_id,
            )
            if run is None:
                return None
            receipt = await uow.workflow_runs.dispatch(command)
            await uow.commit()
        return receipt

    async def list_events(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        after_sequence: int | None,
        limit: int,
    ) -> WorkflowEventPage | None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Workflow read context is invalid.")
        async with self._uow_factory(context) as uow:
            run = await uow.workflow_runs.get_run(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                run_id=run_id,
            )
            if run is None:
                return None
            return await uow.workflow_runs.list_events(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                run_id=run_id,
                after_sequence=after_sequence,
                limit=limit,
            )


__all__ = ["PersistedWorkflowRunAdmissionService"]
