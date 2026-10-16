from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.authorized_ask import install_authorized_ask_routes
from spine.application.authorized_ask import (
    AuthorizedAskResult,
    AuthorizedAskServiceUnavailable,
    AskCommand,
    AskCancellationCommand,
)
from spine.domain.capabilities import (
    AcceptedGroundedAnswer,
    AnswerDisclosureValidation,
    AnswerContent,
    AnswerQuestionInput,
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionScope,
    ClarificationRequest,
    LineageReference,
    QuestionSubject,
    RunAttemptCorrelation,
)
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)
from tests.domain.capabilities.test_answer_question_candidate import candidate_output


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
RUN_ID = UUID("40000000-0000-0000-0000-000000000001")
ATTEMPT_ID = UUID("40000000-0000-0000-0000-000000000002")
MESSAGE_ID = UUID("40000000-0000-0000-0000-000000000003")
COMMAND_ID = UUID("40000000-0000-0000-0000-000000000004")
TRACE_ID = UUID("50000000-0000-0000-0000-000000000073")
SESSION_ID = UUID("60000000-0000-0000-0000-000000000001")


def headers(workspace_id: UUID = WORKSPACE_ID, environment_id: UUID = ENVIRONMENT_ID) -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(environment_id),
    }


def safe_output(outcome: AnswerQuestionOutcome) -> AnswerQuestionOutput:
    reason = {
        AnswerQuestionOutcome.CLARIFICATION_REQUIRED: AnswerQuestionSafeReasonCode.AMBIGUOUS_QUESTION,
        AnswerQuestionOutcome.ABSTAINED: AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
        AnswerQuestionOutcome.DENIED: AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
        AnswerQuestionOutcome.FAILED: AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
        AnswerQuestionOutcome.CANCELLED: AnswerQuestionSafeReasonCode.CANCELLED,
    }[outcome]
    question = AnswerQuestionInput(
        subject=QuestionSubject(text="Which policy applies?"),
        scope=AnswerQuestionScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID),
        correlation=RunAttemptCorrelation(
            run_id=RUN_ID, attempt_id=ATTEMPT_ID, trace_id=TRACE_ID
        ),
    )
    return AnswerQuestionOutput(
        correlation=question.correlation,
        scope=question.scope,
        outcome=outcome,
        clarification=(
            ClarificationRequest(prompt="Which customer should I use?")
            if outcome is AnswerQuestionOutcome.CLARIFICATION_REQUIRED
            else None
        ),
        reason=AnswerQuestionSafeReason(code=reason),
        lineage=(LineageReference(kind="run", identifier=RUN_ID, schema_version="answer-question-run:v1"),),
    )


def answered_result() -> AuthorizedAskResult:
    candidate = candidate_output()
    answer_values = candidate.answer.model_dump(mode="python")

    def align_lineage(value):
        if isinstance(value, UUID):
            return {
                UUID(int=1): WORKSPACE_ID,
                UUID(int=2): ENVIRONMENT_ID,
                UUID(int=9): RUN_ID,
                UUID(int=10): ATTEMPT_ID,
            }.get(value, value)
        if isinstance(value, dict):
            return {key: align_lineage(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(align_lineage(item) for item in value)
        if isinstance(value, list):
            return [align_lineage(item) for item in value]
        return value

    answer = AnswerContent.model_validate(align_lineage(answer_values))
    validation = AnswerDisclosureValidation(
        citation_validation_reference=UUID(int=710),
        semantic_support_validation_reference=UUID(int=711),
        evaluation_lineage_reference=UUID(int=712),
        disclosure_eligibility_reference=UUID(int=713),
        context_bundle_id=answer.citations[0].context_bundle.bundle.bundle_id,
        context_bundle_version=answer.citations[0].context_bundle.bundle.bundle_version,
        context_bundle_digest=answer.citations[0].context_bundle.bundle.bundle_digest,
        claim_ids=tuple(claim.claim_id for claim in answer.claims),
        citation_ids=tuple(citation.citation_id for citation in answer.citations),
        validation_fact_ids=(UUID(int=714),),
    )
    accepted = AcceptedGroundedAnswer(
        answer=answer,
        validation=validation,
        accepted_at=NOW,
    )
    output = AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            trace_id=TRACE_ID,
        ),
        scope=AnswerQuestionScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        ),
        outcome=AnswerQuestionOutcome.ANSWERED,
        answer=answer,
        lineage=(
            LineageReference(kind="run", identifier=RUN_ID, schema_version="answer-question-run:v1"),
            LineageReference(
                kind="context_bundle",
                identifier=answer.citations[0].context_bundle.bundle.bundle_id,
                schema_version="context-bundle:v1",
            ),
        ),
    )
    return AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=output,
        accepted_answer=accepted,
        replay=False,
    )


@dataclass
class FakeAskService:
    result: AuthorizedAskResult
    calls: list[AskCommand | AskCancellationCommand]

    async def ask(self, command: AskCommand) -> AuthorizedAskResult:
        self.calls.append(command)
        if command.workspace_id != WORKSPACE_ID or command.environment_id != ENVIRONMENT_ID:
            raise AssertionError("trusted scope was not used")
        return self.result

    async def cancel(self, command: AskCancellationCommand) -> AuthorizedAskResult:
        self.calls.append(command)
        return self.result


def app(service: FakeAskService):
    application = authorized_app([])
    install_authorized_ask_routes(application, service=service)
    return application


@pytest.mark.asyncio
async def test_authorized_ask_returns_only_accepted_answer_and_citation_references() -> None:
    service = FakeAskService(answered_result(), [])
    application = app(service)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": "ask-1"},
        )

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["outcome"] == "answered"
    assert body["answer"]["text"] == "The policy applies to this customer."
    assert body["as_of"]["projection_snapshot_id"] == str(UUID(int=5))
    assert body["answer"]["citations"][0]["citation_id"] == str(UUID(int=702))
    assert "candidate" not in response.text
    assert "chain" not in response.text
    assert service.calls[0].subject_id == UUID("10000000-0000-0000-0000-000000000073")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", tuple(AnswerQuestionOutcome))
async def test_authorized_ask_maps_each_terminal_outcome_without_renaming(outcome: AnswerQuestionOutcome) -> None:
    result = answered_result() if outcome is AnswerQuestionOutcome.ANSWERED else AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(outcome),
        accepted_answer=None,
        replay=False,
    )
    service = FakeAskService(result, [])
    application = app(service)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": f"ask-{outcome.value}"},
        )

    payload = response.json()["result"]
    assert response.status_code == 200
    assert payload["outcome"] == outcome.value
    if outcome is not AnswerQuestionOutcome.ANSWERED:
        assert payload["answer"] is None
        assert "The policy applies" not in response.text


@pytest.mark.asyncio
async def test_authorized_ask_replay_and_conflict_are_service_owned_and_safe() -> None:
    first = answered_result()

    class ReplayThenConflict(FakeAskService):
        async def ask(self, command: AskCommand) -> AuthorizedAskResult:
            self.calls.append(command)
            if len(self.calls) == 1:
                return first
            from spine.application.persistence.errors import IdempotencyConflictError

            raise IdempotencyConflictError("protected question and raw payload")

    service = ReplayThenConflict(first, [])
    application = app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        first_response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": "same-key"},
        )
        conflict_response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "A different secret"},
            headers={**headers(), "Idempotency-Key": "same-key"},
        )

    assert first_response.status_code == 200
    assert conflict_response.status_code == 409
    assert "protected question" not in conflict_response.text


@pytest.mark.asyncio
async def test_authorized_ask_same_key_replays_stable_result() -> None:
    first = answered_result()

    class Replay(FakeAskService):
        async def ask(self, command: AskCommand) -> AuthorizedAskResult:
            self.calls.append(command)
            return replace(first, replay=len(self.calls) > 1)

    service = Replay(first, [])
    application = app(service)
    payload = {
        "schema_version": 1,
        "session_id": str(SESSION_ID),
        "question": "Which policy applies?",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        first_response = await client.post(
            "/api/v1/ask", json=payload, headers={**headers(), "Idempotency-Key": "replay-key"}
        )
        replay_response = await client.post(
            "/api/v1/ask", json=payload, headers={**headers(), "Idempotency-Key": "replay-key"}
        )

    assert first_response.status_code == replay_response.status_code == 200
    assert replay_response.json()["replay"] is True
    assert replay_response.json()["result"] == first_response.json()["result"]


@pytest.mark.asyncio
async def test_authorized_ask_rejects_other_environment_without_calling_service() -> None:
    service = FakeAskService(answered_result(), [])
    application = app(service)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(WORKSPACE_ID, OTHER_ENVIRONMENT_ID), "Idempotency-Key": "ask-other-env"},
        )

    assert response.status_code == 403
    assert service.calls == []


@pytest.mark.asyncio
async def test_authorized_ask_maps_unavailable_service_to_safe_error() -> None:
    class Unavailable(FakeAskService):
        async def ask(self, command: AskCommand) -> AuthorizedAskResult:
            del command
            raise AuthorizedAskServiceUnavailable()

    application = app(Unavailable(answered_result(), []))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(), "Idempotency-Key": "ask-unavailable"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "spine.qa.unavailable"
    assert "temporarily unavailable" in response.text


@pytest.mark.asyncio
async def test_authorized_ask_rejects_invalid_idempotency_key_without_disclosure() -> None:
    service = FakeAskService(answered_result(), [])
    application = app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers=headers(),
        )

    assert response.status_code == 400
    assert response.json()["code"] == "spine.qa.ask.invalid_request"
    assert "Which policy applies" not in response.text


@pytest.mark.asyncio
async def test_authorized_ask_cancellation_uses_trusted_scope_and_stable_terminal_result() -> None:
    result = AuthorizedAskResult(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        output=safe_output(AnswerQuestionOutcome.CANCELLED),
        accepted_answer=None,
        replay=False,
    )
    service = FakeAskService(result, [])
    application = app(service)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/ask/{RUN_ID}/cancel",
            json={"schema_version": 1},
            headers={**headers(), "Idempotency-Key": "cancel-1"},
        )

    assert response.status_code == 200
    assert response.json()["result"]["outcome"] == "cancelled"
    assert service.calls[0].run_id == RUN_ID


@pytest.mark.asyncio
async def test_authorized_ask_denies_other_workspace_without_calling_service() -> None:
    service = FakeAskService(answered_result(), [])
    application = app(service)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ask",
            json={"schema_version": 1, "session_id": str(SESSION_ID), "question": "Which policy applies?"},
            headers={**headers(OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID), "Idempotency-Key": "ask-foreign"},
        )

    assert response.status_code == 403
    assert service.calls == []
    assert "Which policy applies" not in response.text


@pytest.mark.asyncio
async def test_legacy_ask_is_unchanged_and_not_routed_through_authorized_service(monkeypatch) -> None:
    service = FakeAskService(answered_result(), [])
    application = app(service)

    async def legacy_ask(question, *, dataset=None, session_id=None):
        assert question == "legacy"
        assert dataset is None
        assert session_id is None
        return ()

    monkeypatch.setattr("spine.api.main.ask", legacy_ask)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "legacy"})

    assert response.status_code == 200
    assert response.json() == {"question": "legacy", "answer": "(нет результатов)", "raw_count": 0}
    assert service.calls == []
