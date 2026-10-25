from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from spine.api.qa_sessions import _message_response, install_qa_session_routes
from spine.application.qa_completeness import completeness_presentation
from tests.application.test_qa_completeness import _record as completeness_record
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.context import EnvironmentScope, PersistenceOperation
from spine.application.qa_sessions import PersistedQASessionService
from spine.application.qa_sessions import QASessionRequester
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.infrastructure.persistence.qa_sessions import InMemoryQASessionRepository
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
MESSAGE_ID = UUID("50000000-0000-0000-0000-000000000001")


class AllowDisclosure:
    async def is_eligible(self, *, message, requester):
        del message, requester
        return True

    async def is_accepted_eligible(self, *, message, requester, record):
        del message, requester, record
        return True


class DenyDisclosure:
    async def is_eligible(self, *, message, requester):
        del message, requester
        return False

    async def is_accepted_eligible(self, *, message, requester, record):
        del message, requester, record
        return False


def app(*, allowed: bool = True):
    application = authorized_app([])
    service = PersistedQASessionService(
        repository=InMemoryQASessionRepository(),
        disclosure_eligibility=AllowDisclosure() if allowed else DenyDisclosure(),
        clock=lambda: NOW,
    )
    install_qa_session_routes(
        application,
        service=service,
        cursor_secret=b"issue-297-qa-session-cursor-secret",
    )
    return application, service


def headers():
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_session_create_replay_and_message_read_use_stable_scoped_contracts():
    application, _ = app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/qa/sessions", json={}, headers={**headers(), "Idempotency-Key": "session-1"}
        )
        replay = await client.post(
            "/api/v1/qa/sessions", json={}, headers={**headers(), "Idempotency-Key": "session-1"}
        )
        session_id = created.json()["result"]["session_id"]
        message = await client.post(
            f"/api/v1/qa/sessions/{session_id}/messages",
            json={"question": "Protected question"},
            headers={**headers(), "Idempotency-Key": "message-1"},
        )
        detail = await client.get(
            f"/api/v1/qa/sessions/{session_id}/messages/{message.json()['result']['message_id']}",
            headers=headers(),
        )

    assert created.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["replay"] is True
    assert replay.json()["result"]["session_id"] == session_id
    assert message.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["data"]["question"] == "Protected question"
    assert detail.json()["data"]["state"] == "pending"


@pytest.mark.asyncio
async def test_session_cursors_are_scope_bound_and_protected_content_is_hidden():
    application, service = app()
    # Seed a second session through the same public application service so the
    # page boundary is observable without reaching into repository internals.
    from spine.application.persistence.context import EnvironmentScope, PersistenceOperation
    from spine.application.qa_sessions import QASessionRequester
    from types import SimpleNamespace

    context = SimpleNamespace(
        scope=EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID),
        operation=PersistenceOperation("qa_sessions"),
        trace_id=UUID("50000000-0000-0000-0000-000000000073"),
        validate_shape=lambda: None,
    )
    requester = QASessionRequester(UUID("10000000-0000-0000-0000-000000000073"))
    await service.create_session(context=context, requester=requester, idempotency_key=IdempotencyKey("seed-1"))
    second = await service.create_session(context=context, requester=requester, idempotency_key=IdempotencyKey("seed-2"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        first = await client.get("/api/v1/qa/sessions", params={"limit": 1}, headers=headers())
        cursor = first.json()["next_cursor"]
        second_page = await client.get("/api/v1/qa/sessions", params={"limit": 1, "cursor": cursor}, headers=headers())
        tampered = await client.get("/api/v1/qa/sessions", params={"limit": 1, "cursor": cursor[:-1] + "x"}, headers=headers())
        foreign = await client.get(
            "/api/v1/qa/sessions",
            headers={
                **headers(),
                "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
            },
        )

    assert first.status_code == 200
    assert second_page.status_code == 200
    first_id = first.json()["items"][0]["session_id"]
    assert second_page.json()["items"][0]["session_id"] != first_id
    assert tampered.status_code == 400
    assert tampered.json()["code"] == "spine.request.invalid_cursor"
    assert foreign.status_code == 403
    assert str(second.value.session_id) not in foreign.text


@pytest.mark.asyncio
async def test_administrator_without_content_grant_receives_metadata_only_message():
    application, _ = app(allowed=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/qa/sessions", json={}, headers={**headers(), "Idempotency-Key": "session-denied"}
        )
        message = await client.post(
            f"/api/v1/qa/sessions/{created.json()['result']['session_id']}/messages",
            json={"question": "protected question"},
            headers={**headers(), "Idempotency-Key": "message-denied"},
        )

    payload = message.json()["result"]
    assert message.status_code == 200
    assert payload["disclosure_eligibility"] == "ineligible"
    assert payload["question"] is None
    assert payload["answer"] is None


def test_message_response_serializes_only_safe_completeness_fields() -> None:
    record = completeness_record()
    response = _message_response(
        SimpleNamespace(
            message_id=MESSAGE_ID,
            session_id=UUID(int=81),
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            version=2,
            ordinal=1,
            trace_id=UUID(int=82),
            run_id=record.run_id,
            state="terminal",
            terminal_outcome=record.outcome,
            cancelled=False,
            disclosure_eligibility=SimpleNamespace(value="ineligible"),
            question=None,
            answer=None,
            accepted_presentation=None,
            completeness=completeness_presentation(record),
            created_at=NOW,
            updated_at=NOW,
        )
    )

    payload = response.model_dump(mode="json")
    assert payload["question"] is None
    assert payload["answer"] is None
    assert payload["completeness"]["coverage_status"] == "unproven"
    assert "members" not in payload["completeness"]
    assert "source_object_id" not in str(payload)


@pytest.mark.asyncio
async def test_message_route_requires_idempotency_and_safe_not_found():
    application, _ = app()
    missing = UUID("40000000-0000-0000-0000-000000000099")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        no_key = await client.post(f"/api/v1/qa/sessions/{missing}/messages", json={"question": "secret"}, headers=headers())
        not_found = await client.get(f"/api/v1/qa/sessions/{missing}", headers=headers())

    assert no_key.status_code == 400
    assert no_key.json()["code"] == "spine.qa_sessions.invalid_request"
    assert "secret" not in no_key.text
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "spine.qa_sessions.not_found"
    assert str(missing) not in not_found.text


@pytest.mark.asyncio
async def test_answered_message_with_expected_but_unverifiable_canonical_join_is_content_free_503():
    application, service = app()
    context = SimpleNamespace(
        scope=EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID),
        operation=PersistenceOperation("qa_sessions"),
        trace_id=UUID("50000000-0000-0000-0000-000000000073"),
        validate_shape=lambda: None,
    )
    requester = QASessionRequester(UUID("10000000-0000-0000-0000-000000000073"))
    session = await service.create_session(
        context=context, requester=requester, idempotency_key=IdempotencyKey("503-session")
    )
    message = await service.create_pending_message(
        context=context, requester=requester, session_id=session.value.session_id,
        question="Secret question", run_id=UUID("70000000-0000-0000-0000-000000000001"),
        idempotency_key=IdempotencyKey("503-message"),
    )
    await service.complete_message(
        context=context, session_id=session.value.session_id,
        message_id=message.value.message_id, answer="Secret answer",
        outcome=AnswerQuestionOutcome.ANSWERED, cancelled=False,
        idempotency_key=IdempotencyKey("503-complete"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/qa/sessions/{session.value.session_id}/messages/{message.value.message_id}",
            headers=headers(),
        )

    assert response.status_code == 503
    assert "Secret answer" not in response.text
    assert "Secret question" not in response.text
    assert str(message.value.message_id) not in response.text
