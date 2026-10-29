"""PostgreSQL persistence for atomic conversational workflow admission."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, select, update

from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    InvalidPersistenceContextError,
    OptimisticConflictError,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.application.persistence.workflow_runs import (
    CLARIFICATION_ANSWERED_EVENT_TYPE,
    CLARIFICATION_REQUESTED_EVENT_TYPE,
    COMMAND_WORKFLOW_OPERATION,
    COMMAND_WORKFLOW_RESULT_TYPE,
    COMMAND_WORKFLOW_SCHEMA_VERSION,
    ClarificationAnsweredDispatchPayload,
    ClarificationRequestedDispatchPayload,
    START_WORKFLOW_EVENT_SCHEMA_VERSION,
    START_WORKFLOW_EVENT_TYPE,
    START_WORKFLOW_OPERATION,
    START_WORKFLOW_RESULT_TYPE,
    START_WORKFLOW_SCHEMA_VERSION,
    WORKFLOW_CANCELLED_EVENT_TYPE,
    WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
    WorkflowCancelledDispatchPayload,
    StartWorkflowDispatchPayload,
    WorkflowRunAdmissionRepository,
    validate_workflow_command_context,
    validate_workflow_run_context,
)
from spine.application.workflow_runs import StartWorkflowCommand, WorkflowCommand
from spine.domain.work_items import WorkItem, WorkItemStatus
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    ClientRequestV1,
    RequirementsArtifact,
    WorkflowCommandReceipt,
    WorkflowEvent,
    WorkflowEventPage,
    WorkflowRun,
    WorkflowRunState,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    work_items,
    workflow_command_receipts,
    workflow_run_events,
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
        start_event_id = uuid5(
            NAMESPACE_URL, f"spine.workflow.run-event:v1:{run_id}:1"
        )
        await self._uow._execute(
            insert(workflow_run_events).values(
                event_id=start_event_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                run_id=run_id,
                sequence=1,
                event_type="workflow.run-started",
                event_schema_version=1,
                run_version=1,
                summary="Workflow run started.",
                correlation_id=command.correlation_id,
                causation_id=command.command_id,
                occurred_at=now,
            )
        )

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

    async def dispatch(
        self,
        command: WorkflowCommand,
        *,
        clarification_successor: RequirementsArtifact | None = None,
    ) -> WorkflowCommandReceipt:
        """Persist one typed continuation with one optimistic logical effect."""

        self._uow._guard_active()
        context = self._uow._context_snapshot_active()
        try:
            validate_workflow_command_context(context, command)
        except ValueError as error:
            await self._uow._fail(InvalidPersistenceContextError(str(error)))
        digest = digest_command(
            operation=COMMAND_WORKFLOW_OPERATION,
            operation_schema_version=COMMAND_WORKFLOW_SCHEMA_VERSION,
            payload={
                "command_id": command.command_id,
                "run_id": command.run_id,
                "workspace_id": command.workspace_id,
                "environment_id": command.environment_id,
                "actor": command.actor.model_dump(mode="json"),
                "command_type": command.command_type,
                "command_schema_version": command.command_schema_version,
                "expected_run_version": command.expected_run_version,
                "payload": command.payload.model_dump(mode="json"),
                "artifact_refs": [item.model_dump(mode="json") for item in command.artifact_refs],
                "correlation_id": command.correlation_id,
                "causation_id": command.causation_id,
            },
        )
        claim = await self._uow.idempotency.claim(
            operation_schema_version=COMMAND_WORKFLOW_SCHEMA_VERSION,
            key=IdempotencyKey(command.idempotency_key),
            digest=digest,
        )
        if isinstance(claim, IdempotencyReplay):
            if claim.result.result_type != COMMAND_WORKFLOW_RESULT_TYPE:
                await self._uow._fail(
                    ConstraintConflictError("Workflow command replay is invalid.")
                )
            receipt = await self._resolve_command_receipt(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                command_id=claim.result.result_id,
            )
            if receipt is None or receipt.run_id != command.run_id:
                await self._uow._fail(
                    ConstraintConflictError("Workflow command replay is unavailable.")
                )
            return receipt.model_copy(update={"admission": "replayed"})

        run = await self._resolve_run(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            run_id=command.run_id,
        )
        if run is None:
            await self._uow._fail(ConstraintConflictError("Workflow run is unavailable."))
        if run.run_version != command.expected_run_version:
            await self._uow._fail(OptimisticConflictError("Workflow run version is stale."))
        if run.state in {
            WorkflowRunState.COMPLETED,
            WorkflowRunState.FAILED,
            WorkflowRunState.CANCELLED,
        }:
            await self._uow._fail(ConstraintConflictError("Workflow transition is invalid."))
        is_clarification_answer = command.command_type == "answer_clarification"
        is_cancellation = command.command_type == "cancel"
        next_clarification = None
        if is_clarification_answer:
            if run.state is not WorkflowRunState.WAITING or clarification_successor is None:
                await self._uow._fail(
                    ConstraintConflictError("Workflow clarification is unavailable.")
                )
            artifact_ref = command.artifact_refs[0]
            if (
                clarification_successor.workspace_id != command.workspace_id
                or clarification_successor.environment_id != command.environment_id
                or clarification_successor.run_id != command.run_id
                or clarification_successor.artifact_id != artifact_ref.artifact_id
                or clarification_successor.predecessor_version != artifact_ref.artifact_version
                or clarification_successor.artifact_version != artifact_ref.artifact_version + 1
            ):
                await self._uow._fail(
                    ConstraintConflictError("Workflow clarification successor is invalid.")
                )
            next_clarification = next(
                (
                    item
                    for item in clarification_successor.clarifications
                    if item.answer is None
                ),
                None,
            )

        answered_version = run.run_version + 1
        resulting_version = answered_version + (
            1 if is_clarification_answer and next_clarification is not None else 0
        )
        resulting_state = (
            WorkflowRunState.CANCELLED
            if is_cancellation
            else (
                WorkflowRunState.WAITING
                if next_clarification is not None
                else WorkflowRunState.RUNNING
            )
            if is_clarification_answer
            else run.state
        )
        update_result = await self._uow._execute(
            update(workflow_runs)
            .where(
                workflow_runs.c.workspace_id == command.workspace_id,
                workflow_runs.c.environment_id == command.environment_id,
                workflow_runs.c.run_id == command.run_id,
                workflow_runs.c.run_version == command.expected_run_version,
            )
            .values(
                run_version=resulting_version,
                state=resulting_state.value,
            )
        )
        if update_result.rowcount != 1:
            await self._uow._fail(OptimisticConflictError("Workflow run version is stale."))
        now = self._clock().astimezone(timezone.utc)
        answered = None
        if is_clarification_answer:
            payload = command.payload
            answered = next(
                (
                    item
                    for item in clarification_successor.clarifications
                    if item.clarification_id == payload.clarification_id
                ),
                None,
            )
            if answered is None or answered.answer is None:
                await self._uow._fail(
                    ConstraintConflictError("Workflow clarification successor is invalid.")
                )
            await self._uow.requirements_artifacts.persist_candidate(
                clarification_successor
            )
        await self._uow._execute(
            insert(workflow_command_receipts).values(
                command_id=command.command_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                run_id=command.run_id,
                work_item_id=run.work_item_id,
                workflow_version_id=run.workflow_version_id,
                idempotency_key=command.idempotency_key,
                command_type=command.command_type,
                command_schema_version=command.command_schema_version,
                expected_run_version=command.expected_run_version,
                resulting_run_version=resulting_version,
                run_state=resulting_state.value,
                actor=command.actor.model_dump(mode="json"),
                command_payload=command.payload.model_dump(mode="json"),
                artifact_refs=[item.model_dump(mode="json") for item in command.artifact_refs],
                command_digest=digest.value,
                trace_id=command.trace_id,
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
                created_at=now,
            )
        )
        event_id = (
            answered.answer.answered_event_id
            if answered is not None and answered.answer is not None
            else uuid5(
                NAMESPACE_URL, f"spine.workflow.command-event:v1:{command.command_id}"
            )
        )
        event_type, event_summary = {
            "add_context": ("workflow.context-added", "Additional context received."),
            "edit_requirement": (
                "workflow.requirement-edited",
                "A requirement was edited by a human reviewer.",
            ),
            "save_review": (
                "workflow.requirements-reviewed",
                "Requirements review was saved.",
            ),
            "request_rework": (
                "workflow.requirements-rework-requested",
                "Requirements rework was requested.",
            ),
            "answer_clarification": (
                "workflow.clarification-answered",
                "Clarification answered.",
            ),
            "cancel": (
                "workflow.run-cancelled",
                "Workflow run cancelled.",
            ),
        }[command.command_type]
        await self._uow._execute(
            insert(workflow_run_events).values(
                event_id=event_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                run_id=command.run_id,
                sequence=answered_version,
                event_type=event_type,
                event_schema_version=1,
                run_version=answered_version,
                summary=event_summary,
                correlation_id=command.correlation_id,
                causation_id=command.command_id,
                occurred_at=now,
            )
        )
        if is_clarification_answer and next_clarification is not None:
            await self._uow._execute(
                insert(workflow_run_events).values(
                    event_id=next_clarification.asked_event_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    run_id=command.run_id,
                    sequence=resulting_version,
                    event_type="workflow.clarification-requested",
                    event_schema_version=1,
                    run_version=resulting_version,
                    summary="Clarification requested.",
                    correlation_id=next_clarification.correlation_id,
                    causation_id=event_id,
                    occurred_at=next_clarification.asked_at,
                )
            )
        if is_clarification_answer:
            signal_payload = ClarificationAnsweredDispatchPayload(
                command_id=command.command_id,
                run_id=command.run_id,
                clarification_id=command.payload.clarification_id,
                artifact_id=clarification_successor.artifact_id,
                artifact_version=clarification_successor.artifact_version,
                next_clarification_id=(
                    next_clarification.clarification_id
                    if next_clarification is not None
                    else None
                ),
                workflow_version_id=run.workflow_version_id,
            )
            signal_event_type = CLARIFICATION_ANSWERED_EVENT_TYPE
        elif is_cancellation:
            signal_payload = WorkflowCancelledDispatchPayload(
                command_id=command.command_id,
                run_id=command.run_id,
                workflow_version_id=run.workflow_version_id,
            )
            signal_event_type = WORKFLOW_CANCELLED_EVENT_TYPE
        else:
            signal_payload = None
            signal_event_type = None
        if signal_payload is not None and signal_event_type is not None:
            intent = self._uow._outbox_events.build_intent(
                event_id=event_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                event_type=signal_event_type,
                schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
                payload=signal_payload,
                aggregate=OpaqueObjectReference(
                    object_type="workflow_run",
                    object_id=command.run_id,
                    schema_version=1,
                ),
                producer_deduplication_id=f"workflow-signal:{command.command_id}",
                trace_id=command.trace_id,
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
            )
            await self._uow.outbox.append(intent)
        await self._uow.idempotency.complete(
            claim,
            OpaqueResultReference(
                COMMAND_WORKFLOW_RESULT_TYPE,
                command.command_id,
                COMMAND_WORKFLOW_SCHEMA_VERSION,
            ),
        )
        return WorkflowCommandReceipt(
            command_id=command.command_id,
            work_item_id=run.work_item_id,
            run_id=run.run_id,
            workflow_version_id=run.workflow_version_id,
            state=resulting_state,
            run_version=resulting_version,
            admission="accepted",
        )

    async def wait_for_clarification(
        self, artifact: RequirementsArtifact
    ) -> WorkflowRun:
        """Persist one replay-safe human wait for the current artifact version."""

        self._uow._guard_active()
        open_items = tuple(item for item in artifact.clarifications if item.answer is None)
        if not open_items:
            await self._uow._fail(
                ConstraintConflictError("Workflow clarification is unavailable.")
            )
        run = await self._resolve_run(
            workspace_id=artifact.workspace_id,
            environment_id=artifact.environment_id,
            run_id=artifact.run_id,
        )
        if run is None or run.work_item_id != artifact.work_item_id:
            await self._uow._fail(
                ConstraintConflictError("Workflow clarification is unavailable.")
            )
        if run.state is WorkflowRunState.WAITING:
            return run
        if run.state not in {WorkflowRunState.ADMITTED, WorkflowRunState.RUNNING}:
            await self._uow._fail(
                ConstraintConflictError("Workflow transition is invalid.")
            )
        resulting_version = run.run_version + 1
        update_result = await self._uow._execute(
            update(workflow_runs)
            .where(
                workflow_runs.c.workspace_id == artifact.workspace_id,
                workflow_runs.c.environment_id == artifact.environment_id,
                workflow_runs.c.run_id == artifact.run_id,
                workflow_runs.c.run_version == run.run_version,
            )
            .values(
                state=WorkflowRunState.WAITING.value,
                run_version=resulting_version,
            )
        )
        if update_result.rowcount != 1:
            await self._uow._fail(
                OptimisticConflictError("Workflow run version is stale.")
            )
        first = open_items[0]
        await self._uow._execute(
            insert(workflow_run_events).values(
                event_id=first.asked_event_id,
                workspace_id=artifact.workspace_id,
                environment_id=artifact.environment_id,
                run_id=artifact.run_id,
                sequence=resulting_version,
                event_type="workflow.clarification-requested",
                event_schema_version=1,
                run_version=resulting_version,
                summary="Clarification requested.",
                correlation_id=first.correlation_id,
                causation_id=run.command_id,
                occurred_at=first.asked_at,
            )
        )
        intent = self._uow._outbox_events.build_intent(
            event_id=first.asked_event_id,
            workspace_id=artifact.workspace_id,
            environment_id=artifact.environment_id,
            event_type=CLARIFICATION_REQUESTED_EVENT_TYPE,
            schema_version=WORKFLOW_SIGNAL_EVENT_SCHEMA_VERSION,
            payload=ClarificationRequestedDispatchPayload(
                run_id=artifact.run_id,
                clarification_id=first.clarification_id,
                artifact_id=artifact.artifact_id,
                artifact_version=artifact.artifact_version,
                workflow_version_id=run.workflow_version_id,
            ),
            aggregate=OpaqueObjectReference(
                object_type="workflow_run",
                object_id=artifact.run_id,
                schema_version=1,
            ),
            producer_deduplication_id=f"workflow-clarification:{first.clarification_id}",
            trace_id=run.trace_id,
            correlation_id=first.correlation_id,
            causation_id=run.command_id,
        )
        await self._uow.outbox.append(intent)
        return run.model_copy(
            update={
                "state": WorkflowRunState.WAITING,
                "run_version": resulting_version,
            }
        )

    async def list_events(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        after_sequence: int | None,
        limit: int,
    ) -> WorkflowEventPage:
        self._uow._guard_active()
        run = await self._resolve_run(
            workspace_id=workspace_id,
            environment_id=environment_id,
            run_id=run_id,
        )
        if run is None:
            return WorkflowEventPage(items=())
        statement = select(workflow_run_events).where(
            workflow_run_events.c.workspace_id == workspace_id,
            workflow_run_events.c.environment_id == environment_id,
            workflow_run_events.c.run_id == run_id,
        )
        if after_sequence is not None:
            statement = statement.where(workflow_run_events.c.sequence > after_sequence)
        rows = (
            await self._uow._execute(
                statement.order_by(workflow_run_events.c.sequence).limit(limit + 1)
            )
        ).mappings().all()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = tuple(self._event_from_row(row) for row in selected)
        return WorkflowEventPage(
            items=items,
            next_sequence=items[-1].sequence if has_more and items else None,
        )

    async def _resolve_command_receipt(
        self, *, workspace_id: UUID, environment_id: UUID, command_id: UUID
    ) -> WorkflowCommandReceipt | None:
        row = (
            await self._uow._execute(
                select(workflow_command_receipts).where(
                    workflow_command_receipts.c.workspace_id == workspace_id,
                    workflow_command_receipts.c.environment_id == environment_id,
                    workflow_command_receipts.c.command_id == command_id,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return WorkflowCommandReceipt(
            command_id=row["command_id"],
            work_item_id=row["work_item_id"],
            run_id=row["run_id"],
            workflow_version_id=row["workflow_version_id"],
            state=WorkflowRunState(row["run_state"]),
            run_version=row["resulting_run_version"],
            admission="accepted",
        )

    @staticmethod
    def _event_from_row(row: Mapping[str, object]) -> WorkflowEvent:
        return WorkflowEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            event_schema_version=row["event_schema_version"],
            run_version=row["run_version"],
            summary=row["summary"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            occurred_at=row["occurred_at"],
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
