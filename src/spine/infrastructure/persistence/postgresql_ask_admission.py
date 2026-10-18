"""PostgreSQL implementation of atomic Q&A Ask admission."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, insert, select

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    AskAdmissionDispatchPayload,
    AskAdmissionRepository,
    AskAdmissionResult,
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
    QA_ASK_ADMISSION_RESULT_TYPE,
    QA_ASK_ADMISSION_SCHEMA_VERSION,
    validate_ask_admission_context,
)
from spine.application.persistence.errors import (
    AnswerQuestionRunNotFoundError,
    ConstraintConflictError,
    InvalidPersistenceContextError,
)
from spine.application.persistence.idempotency import (
    IdempotencyReplay,
    OpaqueResultReference,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionDiagnosticContext,
    AnswerQuestionRun,
)
from spine.domain.qa_sessions import QAMessage, QAMessageState
from spine.infrastructure.persistence.postgresql_mappings import (
    answer_question_runs,
    qa_message_versions,
    qa_session_versions,
)

if TYPE_CHECKING:
    from spine.infrastructure.persistence.postgresql import PostgreSQLTenantUnitOfWork


class PostgreSQLAskAdmissionRepository(AskAdmissionRepository):
    """Persist all admission effects through the owning PostgreSQL transaction."""

    def __init__(
        self,
        uow: PostgreSQLTenantUnitOfWork,
        *,
        run_id_factory: Callable[[], UUID] = uuid4,
        message_id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow = uow
        self._run_id_factory = run_id_factory
        self._message_id_factory = message_id_factory
        self._clock = clock

    async def admit(self, command: AskAdmissionCommand) -> AskAdmissionResult:
        self._uow._guard_active()
        context = self._uow._context_snapshot_active()
        try:
            validate_ask_admission_context(context, command)
        except ValueError as error:
            await self._uow._fail(InvalidPersistenceContextError(str(error)))
        expected_digest = AskAdmissionCommand.create(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            session_id=command.session_id,
            subject=command.subject,
            environment=command.environment,
            question=command.question,
            idempotency_key=command.idempotency_key,
            clarification_of_message_id=command.clarification_of_message_id,
        ).digest
        if command.digest != expected_digest:
            await self._uow._fail(ConstraintConflictError("Ask admission digest is invalid."))

        claim = await self._uow.idempotency.claim(
            operation_schema_version=QA_ASK_ADMISSION_SCHEMA_VERSION,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            if (
                claim.result.result_type != QA_ASK_ADMISSION_RESULT_TYPE
                or claim.result.schema_version != QA_ASK_ADMISSION_SCHEMA_VERSION
            ):
                await self._uow._fail(ConstraintConflictError("Ask admission replay is invalid."))
            return await self._replay(command, claim)

        session = await self._uow.qa_sessions.resolve_session(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            session_id=command.session_id,
        )
        if session is None or session.subject_id != command.subject.id:
            await self._uow._fail(ConstraintConflictError("Ask session is unavailable."))
        if command.clarification_of_message_id is not None:
            clarification = await self._uow.qa_sessions.resolve_message(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                session_id=command.session_id,
                message_id=command.clarification_of_message_id,
            )
            if clarification is None:
                await self._uow._fail(
                    ConstraintConflictError("Ask clarification target is unavailable.")
                )
        environment = await self._uow.environments.resolve(command.environment_id)
        if environment is None or environment.kind is not command.environment:
            await self._uow._fail(InvalidPersistenceContextError("Ask environment is invalid."))

        session_lock = await self._uow._execute(
            select(qa_session_versions.c.session_id)
            .where(
                qa_session_versions.c.workspace_id == command.workspace_id,
                qa_session_versions.c.environment_id == command.environment_id,
                qa_session_versions.c.session_id == command.session_id,
            )
            .order_by(qa_session_versions.c.version.desc())
            .limit(1)
            .with_for_update()
        )
        if session_lock.scalar_one_or_none() is None:
            await self._uow._fail(ConstraintConflictError("Ask session is unavailable."))
        ordinal_result = await self._uow._execute(
            select(func.max(qa_message_versions.c.ordinal)).where(
                qa_message_versions.c.workspace_id == command.workspace_id,
                qa_message_versions.c.environment_id == command.environment_id,
                qa_message_versions.c.session_id == command.session_id,
            )
        )
        ordinal = (ordinal_result.scalar_one_or_none() or 0) + 1
        run_id = self._run_id_factory()
        message_id = self._message_id_factory()
        if run_id.int == 0 or message_id.int == 0:
            await self._uow._fail(ConstraintConflictError("Ask admission identity is invalid."))
        now = self._clock()
        run = AnswerQuestionRun.admit(
            run_id=run_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            environment=command.environment,
            subject=command.subject,
            idempotency_key=command.idempotency_key.value,
            command_digest=command.digest.value,
            input_artifact_ids=(),
            diagnostic_context=AnswerQuestionDiagnosticContext(trace_id=context.trace_id),
            created_at=now,
        )
        message = QAMessage(
            message_id=message_id,
            session_id=command.session_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            version=1,
            ordinal=ordinal,
            trace_id=context.trace_id,
            run_id=run_id,
            question=command.question,
            state=QAMessageState.PENDING,
            created_at=now,
            updated_at=now,
        )
        await self._uow._execute(insert(answer_question_runs).values(self._run_values(run)))
        await self._uow._execute(insert(qa_message_versions).values(self._message_values(message)))
        dispatch_event_id = uuid5(
            NAMESPACE_URL,
            f"spine.qa.ask-admission-dispatch:v1:{run_id}",
        )
        payload = AskAdmissionDispatchPayload(
            command_id=claim.receipt_id,
            session_id=command.session_id,
            message_id=message_id,
            run_id=run_id,
            capture_id=command.capture_id,
            schema_version=2 if command.capture_id is not None else 1,
        )
        intent = self._uow._outbox_events.build_intent(
            event_id=dispatch_event_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            event_type=QA_ASK_ADMISSION_EVENT_TYPE,
            schema_version=QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
            payload=payload,
            aggregate=OpaqueObjectReference(
                object_type="answer_question_run", object_id=run_id, schema_version=1
            ),
            producer_deduplication_id=f"qa-ask-admission:{run_id}",
            trace_id=context.trace_id,
        )
        await self._uow.outbox.append(intent)
        result = OpaqueResultReference(
            result_type=QA_ASK_ADMISSION_RESULT_TYPE,
            result_id=run_id,
            schema_version=QA_ASK_ADMISSION_SCHEMA_VERSION,
        )
        await self._uow.idempotency.complete(claim, result)
        return AskAdmissionResult(
            command_id=claim.receipt_id,
            session_id=command.session_id,
            message_id=message_id,
            run_id=run_id,
            dispatch_event_id=dispatch_event_id,
            replay=False,
        )

    async def _replay(self, command: AskAdmissionCommand, claim: IdempotencyReplay) -> AskAdmissionResult:
        try:
            run = await self._uow.answer_question_runs.resolve_run_in_scope(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                run_id=claim.result.result_id,
            )
        except AnswerQuestionRunNotFoundError:
            await self._uow._fail(ConstraintConflictError("Ask admission replay is unavailable."))
        if run.subject.id != command.subject.id or run.idempotency_key != command.idempotency_key.value:
            await self._uow._fail(ConstraintConflictError("Ask admission replay is invalid."))
        link = await self._uow.answer_question_runs.resolve_message_link(run.run_id)
        if link is None or link.session_id != command.session_id:
            await self._uow._fail(ConstraintConflictError("Ask admission replay is unavailable."))
        return AskAdmissionResult(
            command_id=claim.receipt_id,
            session_id=link.session_id,
            message_id=link.message_id,
            run_id=run.run_id,
            dispatch_event_id=uuid5(
                NAMESPACE_URL,
                f"spine.qa.ask-admission-dispatch:v1:{run.run_id}",
            ),
            replay=True,
        )

    @staticmethod
    def _run_values(run: AnswerQuestionRun) -> dict[str, object]:
        return {
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "environment_id": run.environment_id,
            "idempotency_key": run.idempotency_key,
            "command_digest": run.command_digest,
            "state": run.state.value,
            "outcome": None,
            "payload": run.model_dump(mode="json"),
            "created_at": run.created_at,
            "completed_at": None,
        }

    @staticmethod
    def _message_values(message: QAMessage) -> dict[str, object]:
        return {
            "message_id": message.message_id,
            "session_id": message.session_id,
            "workspace_id": message.workspace_id,
            "environment_id": message.environment_id,
            "version": message.version,
            "ordinal": message.ordinal,
            "trace_id": message.trace_id,
            "run_id": message.run_id,
            "question": message.question,
            "answer": message.answer,
            "state": message.state.value,
            "terminal_outcome": (
                message.terminal_outcome.value
                if message.terminal_outcome is not None
                else None
            ),
            "cancelled": message.cancelled,
            "disclosure_dependency_ids": [
                str(item) for item in message.disclosure_dependency_ids
            ],
            "disclosure_dependencies": message.disclosure_dependencies.model_dump(
                mode="json"
            ),
            "disclosure_eligibility": message.disclosure_eligibility.value,
            "created_at": message.created_at,
            "updated_at": message.updated_at,
        }
