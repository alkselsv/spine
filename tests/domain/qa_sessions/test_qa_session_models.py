from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessage, QAMessageState, QASession, QASessionStatus


WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("10000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("40000000-0000-0000-0000-000000000001")
MESSAGE_ID = UUID("50000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("60000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def test_session_record_is_frozen_versioned_and_closed() -> None:
    session = QASession(
        session_id=SESSION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        subject_id=SUBJECT_ID,
        version=1,
        status=QASessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )

    assert session.schema_version == 1
    with pytest.raises((TypeError, ValidationError)):
        session.status = QASessionStatus.ARCHIVED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        QASession(**{**session.model_dump(), "unknown": True})


def test_message_requires_terminal_outcome_and_matching_cancellation() -> None:
    with pytest.raises(ValidationError, match="terminal messages require an outcome"):
        QAMessage(
            message_id=MESSAGE_ID,
            session_id=SESSION_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            version=1,
            ordinal=1,
            trace_id=TRACE_ID,
            question="Where is the policy?",
            state=QAMessageState.TERMINAL,
            created_at=NOW,
            updated_at=NOW,
        )

    message = QAMessage(
        message_id=MESSAGE_ID,
        session_id=SESSION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        version=1,
        ordinal=1,
        trace_id=TRACE_ID,
        question="Where is the policy?",
        answer="In the handbook.",
        state=QAMessageState.TERMINAL,
        terminal_outcome=AnswerQuestionOutcome.ANSWERED,
        created_at=NOW,
        updated_at=NOW,
    )
    assert message.cancelled is False


def test_message_restores_json_disclosure_dependencies_from_postgresql() -> None:
    message = QAMessage.model_validate(
        {
            "message_id": MESSAGE_ID,
            "session_id": SESSION_ID,
            "workspace_id": WORKSPACE_ID,
            "environment_id": ENVIRONMENT_ID,
            "version": 1,
            "ordinal": 1,
            "trace_id": TRACE_ID,
            "question": "Where is the policy?",
            "state": QAMessageState.PENDING,
            "disclosure_dependencies": {
                "schema_version": "disclosure-dependency-set:v1",
                "dependencies": [],
            },
            "created_at": NOW,
            "updated_at": NOW,
        }
    )

    assert message.disclosure_dependency_ids == ()
