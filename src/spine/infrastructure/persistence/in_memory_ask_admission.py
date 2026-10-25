"""Transactional in-memory Ask admission adapter used by offline tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    AskAdmissionDispatchPayload,
    AskAdmissionRepository,
    AskAdmissionResult,
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
    QA_ASK_ADMISSION_RESULT_TYPE,
    QA_ASK_ADMISSION_SCHEMA_VERSION,
    ask_admission_metadata_schema_version,
    validate_ask_admission_context,
)
from spine.application.persistence.errors import (
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

if TYPE_CHECKING:
    from spine.infrastructure.persistence.in_memory import InMemoryUnitOfWork


@dataclass(frozen=True, slots=True)
class InMemoryAskAdmissionRecord:
    run: AnswerQuestionRun
    message: QAMessage
    dispatch_event_id: UUID


class InMemoryAskAdmissionRepository(AskAdmissionRepository):
    def __init__(
        self,
        uow: InMemoryUnitOfWork,
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
        claim = await self._uow.idempotency.claim(
            operation_schema_version=QA_ASK_ADMISSION_SCHEMA_VERSION,
            key=command.idempotency_key,
            digest=command.digest,
        )
        records = self._uow._base_ask_admissions | self._uow._pending_ask_admissions
        if isinstance(claim, IdempotencyReplay):
            record = records.get((command.workspace_id, command.environment_id, claim.result.result_id))
            if record is None or claim.result.result_type != QA_ASK_ADMISSION_RESULT_TYPE:
                await self._uow._fail(ConstraintConflictError("Ask admission replay is unavailable."))
            if record.run.subject.id != command.subject.id or record.message.session_id != command.session_id:
                await self._uow._fail(ConstraintConflictError("Ask admission replay is invalid."))
            return AskAdmissionResult(
                command_id=claim.receipt_id,
                session_id=record.message.session_id,
                message_id=record.message.message_id,
                run_id=record.run.run_id,
                dispatch_event_id=record.dispatch_event_id,
                replay=True,
            )

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
        existing_messages = await self._uow.qa_sessions.list_messages(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            session_id=command.session_id,
            limit=100000,
            after=None,
        )
        ordinal = max((item.ordinal for item in existing_messages.items), default=0)
        ordinal = max(
            ordinal,
            max(
                (
                    item.message.ordinal
                    for item in records.values()
                    if item.message.session_id == command.session_id
                ),
                default=0,
            ),
        ) + 1
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
        dispatch_event_id = uuid5(
            NAMESPACE_URL, f"spine.qa.ask-admission-dispatch:v1:{run_id}"
        )
        await self._uow.outbox.append(
            self._uow._outbox_events.build_intent(
                event_id=dispatch_event_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                event_type=QA_ASK_ADMISSION_EVENT_TYPE,
                schema_version=QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
                payload=AskAdmissionDispatchPayload(
                    command_id=claim.receipt_id,
                    session_id=command.session_id,
                    message_id=message_id,
                    run_id=run_id,
                    authorization_generation=command.authorization_generation,
                    capture_id=command.capture_id,
                    capture_subscription=command.capture_subscription,
                    allow_partial_answer=command.allow_partial_answer,
                    schema_version=ask_admission_metadata_schema_version(
                        command.capture_id,
                        command.capture_subscription,
                        command.authorization_generation,
                        completeness_intent_versioned=True,
                    ),
                ),
                aggregate=OpaqueObjectReference(
                    object_type="answer_question_run",
                    object_id=run_id,
                    schema_version=1,
                ),
                producer_deduplication_id=f"qa-ask-admission:{run_id}",
                trace_id=context.trace_id,
            )
        )
        self._uow._pending_answer_question_runs[(command.workspace_id, command.environment_id, run_id)] = run
        self._uow._pending_ask_admissions[(command.workspace_id, command.environment_id, run_id)] = InMemoryAskAdmissionRecord(
            run=run, message=message, dispatch_event_id=dispatch_event_id
        )
        await self._uow.idempotency.complete(
            claim,
            OpaqueResultReference(QA_ASK_ADMISSION_RESULT_TYPE, run_id, 1),
        )
        return AskAdmissionResult(
            command_id=claim.receipt_id,
            session_id=command.session_id,
            message_id=message_id,
            run_id=run_id,
            dispatch_event_id=dispatch_event_id,
            replay=False,
        )
