from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from spine.api.qa_runs import _context, install_qa_run_routes
from spine.application.qa_run_history import (
    QARunDetail,
    QARunHistoryFilters,
    QARunHistoryPage,
    QARunHistoryRequester,
)
from spine.domain.capabilities import AnswerQuestionOutcome
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)
from tests.domain.capabilities.test_answer_question_runs import _run
from tests.domain.capabilities.test_answer_question_runs import _pin
from spine.domain.capabilities.answer_question_runs import AnswerQuestionAttempt


RUN_ID = UUID("a0000000-0000-0000-0000-000000000001")
FOREIGN_RUN_ID = UUID("a0000000-0000-0000-0000-000000000099")
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


class RunHistoryFake:
    def __init__(self, runs=()):
        self.runs = tuple(runs)

    async def list_runs(self, *, context, requester, filters, limit, after):
        del context, requester
        values = tuple(
            run
            for run in self.runs
            if (filters.outcome is None or run.outcome is filters.outcome)
            and (after is None or (run.created_at, run.run_id) < (after.created_at, after.run_id))
        )
        has_more = len(values) > limit
        values = values[:limit]
        return QARunHistoryPage(
            items=tuple(
                type(
                    "Item",
                    (),
                    {
                        "run": run,
                        "attempt_count": 1,
                        "message_link": None,
                    },
                )()
                for run in values
            ),
            has_more=has_more,
        )

    async def get_run(self, *, context, requester, run_id):
        del context, requester
        run = next((item for item in self.runs if item.run_id == run_id), None)
        return None if run is None else QARunDetail(run, (), None, (), (), False)


def _headers(**extra):
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
        **extra,
    }


def _application(fake):
    application = authorized_app([])
    install_qa_run_routes(
        application,
        service=fake,
        cursor_secret=b"issue-302-run-history-cursor-secret",
    )
    return application


def _run_record(run_id=RUN_ID, *, outcome=None):
    return _run().model_copy(
        update={
            "run_id": run_id,
            "workspace_id": WORKSPACE_ID,
            "environment_id": ENVIRONMENT_ID,
            "created_at": NOW,
            "outcome": outcome,
        }
    )


@pytest.mark.asyncio
async def test_run_collection_is_scoped_bounded_and_cursor_bound():
    application = _application(
        RunHistoryFake(
            (
                _run_record(),
                _run_record(
                    UUID("a0000000-0000-0000-0000-000000000002"),
                    outcome=AnswerQuestionOutcome.ABSTAINED,
                ),
            )
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        first = await client.get("/api/v1/qa/runs?limit=1", headers=_headers())
        cursor = first.json()["next_cursor"]
        second = await client.get(
            "/api/v1/qa/runs?limit=1&cursor=" + cursor, headers=_headers()
        )
        tampered = await client.get(
            "/api/v1/qa/runs?limit=1&cursor=" + cursor[:-1] + "x", headers=_headers()
        )
        foreign = await client.get(
            "/api/v1/qa/runs",
            headers=_headers(
                **{
                    "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                    "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
                }
            ),
        )

    assert first.status_code == 200
    assert first.json()["items"][0]["run_id"] == str(RUN_ID)
    assert second.status_code == 200
    assert tampered.status_code == 400
    assert tampered.json()["code"] == "spine.request.invalid_cursor"
    assert foreign.status_code == 403
    assert str(RUN_ID) not in foreign.text


@pytest.mark.asyncio
async def test_run_detail_returns_metadata_only_and_safe_not_found():
    application = _application(RunHistoryFake((_run_record(),)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        detail = await client.get(f"/api/v1/qa/runs/{RUN_ID}", headers=_headers())
        missing = await client.get(
            f"/api/v1/qa/runs/{FOREIGN_RUN_ID}", headers=_headers()
        )

    assert detail.status_code == 200
    payload = detail.json()["data"]
    assert payload["run_id"] == str(RUN_ID)
    assert payload["attempts"] == []
    assert "answer" not in detail.text
    assert "candidate" not in detail.text
    assert missing.status_code == 404
    assert missing.json()["code"] == "spine.qa_runs.not_found"
    assert str(FOREIGN_RUN_ID) not in missing.text


@pytest.mark.asyncio
async def test_run_detail_exposes_pinned_attempt_metadata_and_safe_timeline():
    run = _run_record()
    attempt = AnswerQuestionAttempt.start(
        attempt_id=UUID("a1000000-0000-0000-0000-000000000001"),
        run=run,
        attempt_number=1,
        execution_pin=_pin(run.workspace_id, run.environment_id),
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=NOW,
    )

    class AttemptFake(RunHistoryFake):
        async def get_run(self, *, context, requester, run_id):
            del context, requester
            return QARunDetail(run, (attempt,), None, (None,), (), False)

    application = _application(AttemptFake((run,)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/qa/runs/{run.run_id}", headers=_headers())

    assert response.status_code == 200
    payload = response.json()["data"]["attempts"][0]
    assert payload["execution_pin"]["agent_version"]["agent_version_id"]
    assert payload["timeline"][0]["kind"] == "started"
    assert "input_artifact_ids" in payload


def test_run_context_uses_canonical_boundary_manifest_digest() -> None:
    digest = "a" * 64
    response = _context(
        SimpleNamespace(
            bundle_id=UUID(int=1),
            bundle_version=1,
            bundle_digest="b" * 64,
            retrieval_strategy="hybrid",
            outcome=SimpleNamespace(value="sufficient"),
            reason_code="sufficient_evidence",
            state=SimpleNamespace(value="assembled"),
            projection_snapshot=SimpleNamespace(projection_snapshot_id=UUID(int=2)),
            projection_activation=SimpleNamespace(projection_activation_id=UUID(int=3)),
            activation_generation=1,
            canonical_boundary=SimpleNamespace(
                boundary_id=UUID(int=4),
                manifest_digest=digest,
            ),
            canonical_as_of=SimpleNamespace(
                accepted_through_generation=1,
                boundary_time=NOW,
            ),
            freshness=SimpleNamespace(value="current"),
            health=SimpleNamespace(value="healthy"),
            evidence_references=(),
        )
    )

    assert response.canonical_boundary_digest == digest


@pytest.mark.asyncio
async def test_run_collection_rejects_unbounded_page_and_invalid_time_range():
    application = _application(RunHistoryFake())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        too_large = await client.get("/api/v1/qa/runs?limit=101", headers=_headers())
        invalid_range = await client.get(
            "/api/v1/qa/runs?created_after=2026-01-01T00:00:00Z&created_before=2027-12-31T00:00:00Z",
            headers=_headers(),
        )

    assert too_large.status_code == 422
    assert invalid_range.status_code == 400
    assert "2027" not in invalid_range.text
