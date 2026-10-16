from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.feedback import (
    FeedbackRequester,
    FeedbackTargetReference,
    PersistedFeedbackService,
)
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.application.persistence.context import EnvironmentScope, PersistenceOperation
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.feedback import FeedbackOutcome, FeedbackTargetKind
from spine.domain.common import ContextOrigin
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.feedback import (
    InMemoryFeedbackRepository,
    InMemoryFeedbackTargetResolver,
)


WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("10000000-0000-0000-0000-000000000001")
RUN_ID = UUID("70000000-0000-0000-0000-000000000001")
MESSAGE_ID = UUID("50000000-0000-0000-0000-000000000001")
ANSWER_ID = UUID("80000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("60000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


class AuditWriter:
    def __init__(self):
        self.events = []

    async def append(self, event):
        self.events.append(event)
        return UUID("92000000-0000-0000-0000-000000000001")


def context(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID):
    return SimpleNamespace(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        operation=PersistenceOperation("qa_feedback"),
        trace_id=TRACE_ID,
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=SUBJECT_ID,
        service_principal_id=None,
        validate_shape=lambda: None,
    )


def service():
    target = FeedbackTargetReference(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        target_kind=FeedbackTargetKind.ANSWER,
        target_id=ANSWER_ID,
        run_id=RUN_ID,
        message_id=MESSAGE_ID,
    )
    resolver = InMemoryFeedbackTargetResolver((target,))
    return PersistedFeedbackService(
        repository=InMemoryFeedbackRepository(),
        target_resolver=resolver,
        audit_writer=AuditWriter(),
        clock=lambda: NOW,
        feedback_id_factory=lambda: UUID("90000000-0000-0000-0000-000000000001"),
        command_id_factory=lambda: UUID("91000000-0000-0000-0000-000000000001"),
    )

@pytest.mark.asyncio
async def test_feedback_submission_replays_one_immutable_record_and_changed_payload_conflicts():
    feedback = service()
    requester = FeedbackRequester(SUBJECT_ID, authorization_generation=7)
    values = dict(
        context=context(),
        requester=requester,
        run_id=RUN_ID,
        message_id=MESSAGE_ID,
        target_kind=FeedbackTargetKind.ANSWER,
        target_id=ANSWER_ID,
        citation_id=None,
        outcome=FeedbackOutcome.ANSWER_HELPFUL,
        comment="Useful answer",
        reason=None,
        idempotency_key=IdempotencyKey("feedback-1"),
    )

    first = await feedback.submit(**values)
    replay = await feedback.submit(**values)

    assert first.feedback_id == replay.feedback_id
    assert first.command_id == replay.command_id
    assert first.audit_event_id == replay.audit_event_id
    assert replay.replay is True
    with pytest.raises(IdempotencyConflictError):
        await feedback.submit(**{**values, "comment": "Changed judgment"})


@pytest.mark.asyncio
async def test_feedback_target_is_scope_bound_without_mutating_answer_records():
    feedback = service()
    requester = FeedbackRequester(SUBJECT_ID)

    with pytest.raises(LookupError):
        await feedback.submit(
            context=context(UUID("20000000-0000-0000-0000-000000000002")),
            requester=requester,
            run_id=RUN_ID,
            message_id=MESSAGE_ID,
            target_kind=FeedbackTargetKind.ANSWER,
            target_id=ANSWER_ID,
            citation_id=None,
            outcome=FeedbackOutcome.ANSWER_HELPFUL,
            comment=None,
            reason=None,
            idempotency_key=IdempotencyKey("foreign-feedback"),
        )


def test_feedback_domain_rejects_unbounded_or_mismatched_feedback():
    from pydantic import ValidationError
    from spine.domain.feedback import FeedbackRecord

    with pytest.raises(ValidationError):
        FeedbackRecord(
            feedback_id=UUID("90000000-0000-0000-0000-000000000001"),
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            subject_id=SUBJECT_ID,
            trace_id=TRACE_ID,
            run_id=RUN_ID,
            target_kind=FeedbackTargetKind.CITATION,
            target_id=ANSWER_ID,
            citation_id=None,
            outcome=FeedbackOutcome.CITATION_HELPFUL,
            comment="x" * 1_001,
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_feedback_audit_and_diagnostic_references_exclude_comment_text():
    feedback = service()
    audit = AuditWriter()
    diagnostics = RecordingDiagnosticSink(DiagnosticEventRegistry.with_default_families())
    feedback = feedback.__class__(
        **{
            **{field: getattr(feedback, field) for field in feedback.__dataclass_fields__},
            "audit_writer": audit,
            "diagnostic_registry": DiagnosticEventRegistry.with_default_families(),
            "diagnostic_sink": diagnostics,
        }
    )
    request_context = context()
    request_context.origin = ContextOrigin.INTERACTIVE
    request_context.acting_subject_id = SUBJECT_ID
    request_context.service_principal_id = None

    result = await feedback.submit(
        context=request_context,
        requester=FeedbackRequester(SUBJECT_ID),
        run_id=RUN_ID,
        message_id=MESSAGE_ID,
        target_kind=FeedbackTargetKind.ANSWER,
        target_id=ANSWER_ID,
        citation_id=None,
        outcome=FeedbackOutcome.ANSWER_HELPFUL,
        comment="private comment",
        reason="operator_reason",
        idempotency_key=IdempotencyKey("audited-feedback"),
    )

    assert result.audit_event_id is not None
    assert audit.events[0].payload_json() == {"feedback_type": "answer.helpful"}
    assert diagnostics.events[0].payload_json()["outcome"] == "answer.helpful"
    assert "private comment" not in audit.events[0].model_dump_json()
    assert "private comment" not in diagnostics.events[0].model_dump_json()
