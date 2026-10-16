from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.feedback import install_feedback_routes
from spine.application.feedback import (
    FeedbackTargetReference,
    PersistedFeedbackService,
)
from spine.domain.feedback import FeedbackOutcome, FeedbackTargetKind
from spine.infrastructure.persistence.feedback import (
    InMemoryFeedbackRepository,
    InMemoryFeedbackTargetResolver,
)
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


RUN_ID = UUID("70000000-0000-0000-0000-000000000001")
MESSAGE_ID = UUID("50000000-0000-0000-0000-000000000001")
ANSWER_ID = UUID("80000000-0000-0000-0000-000000000001")
CITATION_ID = UUID("81000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("50000000-0000-0000-0000-000000000073")
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


class AuditWriter:
    async def append(self, event):
        del event
        return UUID("92000000-0000-0000-0000-000000000001")


def app():
    application = authorized_app([])
    resolver = InMemoryFeedbackTargetResolver(
        (
            FeedbackTargetReference(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                target_kind=FeedbackTargetKind.ANSWER,
                target_id=ANSWER_ID,
                run_id=RUN_ID,
                message_id=MESSAGE_ID,
            ),
            FeedbackTargetReference(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                target_kind=FeedbackTargetKind.CITATION,
                target_id=CITATION_ID,
                run_id=RUN_ID,
                message_id=MESSAGE_ID,
                citation_id=CITATION_ID,
            ),
        )
    )
    service = PersistedFeedbackService(
        repository=InMemoryFeedbackRepository(),
        target_resolver=resolver,
        audit_writer=AuditWriter(),
        clock=lambda: NOW,
        feedback_id_factory=lambda: UUID("90000000-0000-0000-0000-000000000001"),
        command_id_factory=lambda: UUID("91000000-0000-0000-0000-000000000001"),
    )
    install_feedback_routes(application, service=service)
    return application


def headers(**extra: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
        **extra,
    }


@pytest.mark.asyncio
async def test_feedback_route_records_safe_result_and_replays_idempotently():
    application = app()
    payload = {
        "run_id": str(RUN_ID),
        "message_id": str(MESSAGE_ID),
        "target_kind": "answer",
        "target_id": str(ANSWER_ID),
        "outcome": "answer.helpful",
        "comment": "Protected administrator comment",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        first = await client.post(
            "/api/v1/qa/feedback",
            json=payload,
            headers=headers(**{"Idempotency-Key": "feedback-1"}),
        )
        replay = await client.post(
            "/api/v1/qa/feedback",
            json=payload,
            headers=headers(**{"Idempotency-Key": "feedback-1"}),
        )
        conflict = await client.post(
            "/api/v1/qa/feedback",
            json={**payload, "comment": "Changed judgment"},
            headers=headers(**{"Idempotency-Key": "feedback-1"}),
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["replay"] is True
    assert conflict.status_code == 409
    assert first.json()["result"]["outcome"] == "answer.helpful"
    assert "Protected administrator comment" not in first.text
    assert first.json()["result"]["feedback_id"] == replay.json()["result"]["feedback_id"]


@pytest.mark.asyncio
async def test_feedback_route_records_exact_citation_feedback():
    application = app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/qa/feedback",
            json={
                "run_id": str(RUN_ID),
                "message_id": str(MESSAGE_ID),
                "target_kind": "citation",
                "target_id": str(CITATION_ID),
                "citation_id": str(CITATION_ID),
                "outcome": "citation.helpful",
            },
            headers=headers(**{"Idempotency-Key": "citation-feedback-1"}),
        )

    assert response.status_code == 200
    assert response.json()["result"]["target_kind"] == "citation"
    assert response.json()["result"]["citation_id"] == str(CITATION_ID)


@pytest.mark.asyncio
async def test_feedback_denies_foreign_scope_and_unknown_target_without_enumeration():
    application = app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        foreign = await client.post(
            "/api/v1/qa/feedback",
            json={
                "run_id": str(RUN_ID),
                "target_kind": "answer",
                "target_id": str(ANSWER_ID),
                "outcome": "answer.incorrect",
            },
            headers=headers(
                **{
                    "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                    "Idempotency-Key": "foreign-feedback",
                }
            ),
        )
        missing = await client.post(
            "/api/v1/qa/feedback",
            json={
                "run_id": str(RUN_ID),
                "target_kind": "answer",
                "target_id": str(UUID("80000000-0000-0000-0000-000000000099")),
                "outcome": "answer.incorrect",
            },
            headers=headers(**{"Idempotency-Key": "missing-feedback"}),
        )

    assert foreign.status_code == 403
    assert missing.status_code == 404
    assert str(ANSWER_ID) not in missing.text


@pytest.mark.asyncio
async def test_feedback_requires_idempotency_key_and_never_returns_protected_comment():
    application = app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/qa/feedback",
            json={
                "run_id": str(RUN_ID),
                "message_id": str(MESSAGE_ID),
                "target_kind": "answer",
                "target_id": str(ANSWER_ID),
                "outcome": "answer.helpful",
                "comment": "secret feedback text",
            },
            headers=headers(),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "spine.request.validation"
    assert "secret feedback text" not in response.text
    assert "secret feedback text" not in response.text
