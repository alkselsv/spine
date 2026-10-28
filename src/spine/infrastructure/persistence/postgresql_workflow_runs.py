"""PostgreSQL persistence for atomic conversational workflow admission."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, select

from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    InvalidPersistenceContextError,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.application.persistence.workflow_runs import (
    START_WORKFLOW_EVENT_SCHEMA_VERSION,
    START_WORKFLOW_EVENT_TYPE,
    START_WORKFLOW_OPERATION,
    START_WORKFLOW_RESULT_TYPE,
    START_WORKFLOW_SCHEMA_VERSION,
    StartWorkflowDispatchPayload,
    WorkflowRunAdmissionRepository,
    validate_workflow_run_context,
)
from spine.application.workflow_runs import StartWorkflowCommand
from spine.domain.work_items import WorkItem, WorkItemStatus
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    ClientRequestV1,
    WorkflowCommandReceipt,
    WorkflowRun,
    WorkflowRunState,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    work_items,
    workflow_runs,
)

if TYPE_CHECKING:
    from spine.infrastructure.persistence.postgresql import PostgreSQLTenantUnitOfWork


class PostgreSQLWorkflowRunAdmissionRepository(WorkflowRunAdmissionRepository):
    """Commit WorkItem, WorkflowRun, receipt and dispatch intent in one UoW."""

    def __init__(
        self,
        uow: PostgreSQLTenantUnitOfWork,
        *,
        work_item_id_factory: Callable[[], UUID] = uuid4,
        run_id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow = uow
        self._work_item_id_factory = work_item_id_factory
        self._run_id_factory = run_id_factory
        self._clock = clock

    async def admit(self, command: StartWorkflowCommand) -> WorkflowCommandReceipt:
        self._uow._guard_active()
        context = self._uow._context_snapshot_active()
        try:
            validate_workflow_run_context(context, command)
        except ValueError as error:
            await self._uow._fail(InvalidPersistenceContextError(str(error)))

        digest = digest_command(
            operation=START_WORKFLOW_OPERATION,
            operation_schema_version=START_WORKFLOW_SCHEMA_VERSION,
            payload={
                "workspace_id": command.workspace_id,
                "environment_id": command.environment_id,
                "actor": command.actor.model_dump(mode="json"),
                "workflow_key": command.workflow_key,
                "workflow_version_id": command.workflow_version_id,
                "workflow_version": command.workflow_version,
                "work_item_key": command.work_item_key,
                "client_request": command.client_request.model_dump(mode="json"),
                "correlation_id": command.correlation_id,
                "causation_id": command.causation_id,
            },
        )
        claim = await self._uow.idempotency.claim(
            operation_schema_version=START_WORKFLOW_SCHEMA_VERSION,
            key=IdempotencyKey(command.idempotency_key),
            digest=digest,
        )
        if isinstance(claim, IdempotencyReplay):
            if claim.result.result_type != START_WORKFLOW_RESULT_TYPE:
                await self._uow._fail(ConstraintConflictError("Workflow start replay is invalid."))
            run = await self._resolve_run(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                run_id=claim.result.result_id,
            )
            if run is None or run.started_by != command.actor:
                await self._uow._fail(ConstraintConflictError("Workflow start replay is unavailable."))
            return WorkflowCommandReceipt(
                command_id=run.command_id,
                work_item_id=run.work_item_id,
                run_id=run.run_id,
                workflow_version_id=run.workflow_version_id,
                state=run.state,
                admission="replayed",
            )

        await self._validate_accepted_sources(command.client_request.accepted_sources)
        if await self._find_by_work_item_key(command) is not None:
            await self._uow._fail(
                IdempotencyConflictError("Work item key is already admitted.")
            )
        environment = await self._uow.environments.resolve(command.environment_id)
        if (
            environment is None
            or environment.workspace_id != command.workspace_id
            or (command.environment is not None and environment.kind is not command.environment)
        ):
            await self._uow._fail(InvalidPersistenceContextError("Workflow environment is invalid."))
        work_item_id, run_id = self._work_item_id_factory(), self._run_id_factory()
        if work_item_id.int == 0 or run_id.int == 0:
            await self._uow._fail(ConstraintConflictError("Workflow start identity is invalid."))
        now = self._clock().astimezone(timezone.utc)
        work_item = WorkItem(
            id=work_item_id,
            workspace_id=command.workspace_id,
            environment=environment.kind,
            workflow_version_id=command.workflow_version_id,
            title="Conversational request",
            status=WorkItemStatus.PENDING,
            triggered_by=command.actor,
            input=command.client_request.model_dump(mode="json"),
            idempotency_key=command.idempotency_key,
        )
        run = WorkflowRun(
            run_id=run_id,
            command_id=command.command_id,
            work_item_id=work_item_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            workflow_key=command.workflow_key,
            workflow_version_id=command.workflow_version_id,
            workflow_version=command.workflow_version,
            state=WorkflowRunState.ADMITTED,
            started_by=command.actor,
            client_request=command.client_request,
            trace_id=command.trace_id,
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
            created_at=now,
        )
        await self._uow._execute(insert(work_items).values(**self._work_item_values(work_item, command, now)))
        await self._uow._execute(insert(workflow_runs).values(**self._run_values(run)))

        event_id = uuid5(NAMESPACE_URL, f"spine.workflow.start:v1:{run_id}")
        intent = self._uow._outbox_events.build_intent(
            event_id=event_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            event_type=START_WORKFLOW_EVENT_TYPE,
            schema_version=START_WORKFLOW_EVENT_SCHEMA_VERSION,
            payload=StartWorkflowDispatchPayload(
                command_id=command.command_id,
                run_id=run_id,
                work_item_id=work_item_id,
                workflow_key=command.workflow_key,
                workflow_version_id=command.workflow_version_id,
                workflow_version=command.workflow_version,
            ),
            aggregate=OpaqueObjectReference(
                object_type="workflow_run", object_id=run_id, schema_version=1
            ),
            producer_deduplication_id=f"workflow-start:{run_id}",
            trace_id=command.trace_id,
            correlation_id=command.correlation_id,
            causation_id=command.causation_id,
        )
        await self._uow.outbox.append(intent)
        await self._uow.idempotency.complete(
            claim,
            OpaqueResultReference(
                START_WORKFLOW_RESULT_TYPE,
                run_id,
                START_WORKFLOW_SCHEMA_VERSION,
            ),
        )
        return WorkflowCommandReceipt(
            command_id=command.command_id,
            work_item_id=work_item_id,
            run_id=run_id,
            workflow_version_id=command.workflow_version_id,
            state=run.state,
            admission="accepted",
        )

    async def get_run(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID
    ) -> WorkflowRun | None:
        self._uow._guard_active()
        return await self._resolve_run(
            workspace_id=workspace_id,
            environment_id=environment_id,
            run_id=run_id,
        )

    async def _resolve_run(
        self, *, workspace_id: UUID, environment_id: UUID, run_id: UUID
    ) -> WorkflowRun | None:
        row = (
            await self._uow._execute(
                select(workflow_runs, work_items.c.input, work_items.c.triggered_by)
                .select_from(
                    workflow_runs.join(
                        work_items,
                        (workflow_runs.c.workspace_id == work_items.c.workspace_id)
                        & (workflow_runs.c.environment_id == work_items.c.environment_id)
                        & (workflow_runs.c.work_item_id == work_items.c.id),
                    )
                )
                .where(
                    workflow_runs.c.workspace_id == workspace_id,
                    workflow_runs.c.environment_id == environment_id,
                    workflow_runs.c.run_id == run_id,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return WorkflowRun(
            run_id=row["run_id"],
            command_id=row["command_id"],
            work_item_id=row["work_item_id"],
            workspace_id=row["workspace_id"],
            environment_id=row["environment_id"],
            workflow_key=row["workflow_key"],
            workflow_version_id=row["workflow_version_id"],
            workflow_version=row["workflow_version"],
            state=WorkflowRunState(row["state"]),
            run_version=row["run_version"],
            started_by=row["triggered_by"],
            client_request=ClientRequestV1.model_validate(row["input"]),
            trace_id=row["trace_id"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            created_at=row["created_at"],
        )

    async def _find_by_work_item_key(
        self, command: StartWorkflowCommand
    ) -> Mapping[str, object] | None:
        result = await self._uow._execute(
            select(work_items).where(
                work_items.c.workspace_id == command.workspace_id,
                work_items.c.environment_id == command.environment_id,
                work_items.c.work_item_key == command.work_item_key,
            )
        )
        return result.mappings().one_or_none()

    async def _validate_accepted_sources(
        self, references: tuple[AcceptedSourceRevisionRef, ...]
    ) -> None:
        for reference in references:
            revision = await self._uow.sources.resolve_revision(
                reference.source_revision_id
            )
            if revision is None or revision.source_object_id != reference.source_object_id:
                await self._uow._fail(
                    ConstraintConflictError("Workflow source reference is unavailable.")
                )
            decision = await self._uow.admissions.resolve_effective(
                reference.source_revision_id
            )
            if decision is None or decision.disposition.value != "admitted":
                await self._uow._fail(
                    ConstraintConflictError("Workflow source reference is unavailable.")
                )
            provenance = await self._uow.sources.list_provenance(
                reference.source_revision_id
            )
            if not any(
                item.provenance_id == reference.provenance_id
                and item.source_object_id == reference.source_object_id
                for item in provenance
            ):
                await self._uow._fail(
                    ConstraintConflictError("Workflow source reference is unavailable.")
                )
            acceptances = await self._uow.canonical.list_acceptances(
                reference.source_object_id
            )
            if not any(
                item.source_revision_id == reference.source_revision_id
                and item.provenance_id == reference.provenance_id
                for item in acceptances
            ):
                await self._uow._fail(
                    ConstraintConflictError("Workflow source reference is unavailable.")
                )

    @staticmethod
    def _work_item_values(
        work_item: WorkItem, command: StartWorkflowCommand, created_at: datetime
    ) -> dict[str, object]:
        return {
            "id": work_item.id,
            "workspace_id": work_item.workspace_id,
            "environment_id": command.environment_id,
            "workflow_version_id": work_item.workflow_version_id,
            "work_item_key": command.work_item_key,
            "title": work_item.title,
            "status": work_item.status.value,
            "triggered_by": work_item.triggered_by.model_dump(mode="json"),
            "input": work_item.input,
            "idempotency_key": work_item.idempotency_key,
            "created_at": created_at,
        }

    @staticmethod
    def _run_values(run: WorkflowRun) -> dict[str, object]:
        return {
            **run.model_dump(mode="python", exclude={"client_request"}),
            "state": run.state.value,
            "started_by": run.started_by.model_dump(mode="json"),
        }


__all__ = ["PostgreSQLWorkflowRunAdmissionRepository"]
