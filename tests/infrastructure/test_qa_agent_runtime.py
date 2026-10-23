import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from spine.domain.capabilities import SemanticSupportOutcome
from spine.infrastructure.qa_agent_runtime import (
    ConfiguredLLMRuntime,
    ConfiguredSemanticSupportValidator,
    _SEMANTIC_SUPPORT_SYSTEM_PROMPT,
)
from tests.application.test_semantic_support import request as semantic_request
from tests.agents.test_answer_question_candidate_runtime import (
    candidate_context,
    input_artifact,
)
from tests.agents.test_local_runtime import invocation, version


class _Rows:
    def mappings(self) -> "_Rows":
        return self

    def all(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                evidence_id="segment-00000000000000000000000000000005",
                text="Synthetic qualification evidence.",
            )
        ]


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object, parameters: object) -> _Rows:
        del parameters
        sql = str(statement)
        assert "CONCAT('segment-', REPLACE(CAST(segment_id AS text), '-', ''))" in sql
        return _Rows()


class _Artifacts:
    _session_factory = _Session

    @staticmethod
    async def _bind_scope(*_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_evidence_text_uses_canonical_segment_identity() -> None:
    evidence_id = "segment-00000000000000000000000000000005"
    runtime = ConfiguredLLMRuntime(
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        authority_generation_resolver=lambda *_: _generation(4),
        admitted_authority_generation=4,
    )
    candidate_input = SimpleNamespace(
        context_bundle=SimpleNamespace(
            evidence_references=(SimpleNamespace(evidence_id=evidence_id),)
        ),
        question=SimpleNamespace(
            scope=SimpleNamespace(workspace_id=UUID(int=1), environment_id=UUID(int=2))
        ),
    )

    evidence = await runtime._evidence_text(candidate_input)  # type: ignore[arg-type]

    assert evidence == {evidence_id: "Synthetic qualification evidence."}


async def _generation(value: int | None) -> int | None:
    return value


@pytest.mark.asyncio
async def test_evidence_read_rejects_revoked_service_authority() -> None:
    runtime = ConfiguredLLMRuntime(
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        authority_generation_resolver=lambda *_: _generation(None),
        admitted_authority_generation=4,
    )
    candidate_input = SimpleNamespace(
        context_bundle=SimpleNamespace(
            evidence_references=(SimpleNamespace(evidence_id="segment-1"),)
        ),
        question=SimpleNamespace(
            scope=SimpleNamespace(workspace_id=UUID(int=1), environment_id=UUID(int=2))
        ),
    )

    with pytest.raises(RuntimeError, match="authority is unavailable"):
        await runtime._evidence_text(candidate_input)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_protected_action_rejects_authority_revoked_after_prior_check() -> None:
    generations = iter((4, None))

    async def resolve(*_: object) -> int | None:
        return next(generations)

    runtime = ConfiguredLLMRuntime(
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        authority_generation_resolver=resolve,
        admitted_authority_generation=4,
    )

    await runtime._require_current_authority()
    with pytest.raises(RuntimeError, match="authority is unavailable"):
        await runtime._require_current_authority()


@pytest.mark.asyncio
async def test_first_protected_action_rejects_generation_newer_than_admission() -> None:
    runtime = ConfiguredLLMRuntime(
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        authority_generation_resolver=lambda *_: _generation(5),
        admitted_authority_generation=4,
    )

    with pytest.raises(RuntimeError, match="generation changed"):
        await runtime._require_current_authority()


@pytest.mark.asyncio
async def test_invoke_revalidates_after_model_call_before_persisting(monkeypatch) -> None:
    context = candidate_context()
    artifact = input_artifact(context)
    generations = iter((4, 4, None))

    async def resolve(*_: object) -> int | None:
        return next(generations)

    class Artifacts:
        persisted = False

        async def load(self, *_: object, **__: object):
            return artifact

        async def persist_candidate(self, **_: object):
            self.persisted = True
            raise AssertionError("candidate persistence must remain protected")

    artifacts = Artifacts()
    runtime = ConfiguredLLMRuntime(
        artifacts=artifacts,  # type: ignore[arg-type]
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        workspace_id=context.workspace_id,
        environment_id=context.environment_id,
        authority_generation_resolver=resolve,
        admitted_authority_generation=4,
    )

    async def evidence_text(_: object) -> dict[str, str]:
        return {"evidence-1": "Synthetic evidence."}

    async def complete(*_: object) -> tuple[str, tuple[str, ...]]:
        return "Synthetic answer.", ("evidence-1",)

    monkeypatch.setattr(runtime, "_evidence_text", evidence_text)
    monkeypatch.setattr(runtime, "_complete", complete)

    with pytest.raises(RuntimeError, match="authority is unavailable"):
        await runtime.invoke_with_reference(version(), invocation())
    assert artifacts.persisted is False


def test_semantic_support_prompt_does_not_treat_absence_claims_as_grounded() -> None:
    assert "not supported unless the evidence explicitly establishes that absence" in (
        _SEMANTIC_SUPPORT_SYSTEM_PROMPT
    )


@pytest.mark.asyncio
async def test_semantic_support_validator_accepts_claim_reproduced_by_evidence() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system_prompt = payload["messages"][0]["content"]
        user_payload = json.loads(payload["messages"][1]["content"])
        evidence = user_payload["evidence"]
        direct_match_rule = "case, whitespace, or punctuation" in system_prompt
        structured_evidence = all(
            set(item) == {"evidence_id", "content"} for item in evidence
        )
        outcome = "supported" if direct_match_rule and structured_evidence else "inconclusive"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"outcome": outcome})}}]},
        )

    validator = ConfiguredSemanticSupportValidator(
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        transport=httpx.MockTransport(provider),
    )

    assessment = await validator.assess(semantic_request())

    assert assessment.outcome is SemanticSupportOutcome.SUPPORTED
    assert assessment.evidence_ids == ("evidence-a",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_outcome",
    ["unsupported", "inconclusive"],
)
async def test_semantic_support_validator_preserves_non_supported_outcome(
    provider_outcome: str,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"outcome": provider_outcome})
                        }
                    }
                ]
            },
        )
    )
    validator = ConfiguredSemanticSupportValidator(
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        transport=transport,
    )

    assessment = await validator.assess(semantic_request())

    assert assessment.outcome is SemanticSupportOutcome(provider_outcome)
