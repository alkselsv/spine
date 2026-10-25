import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from spine.domain.capabilities import MissingCoverageImpact, SemanticSupportOutcome
from spine.infrastructure.qa_agent_runtime import (
    ConfiguredGroundedConflictValidator,
    ConfiguredLLMRuntime,
    ConfiguredMissingCoverageImpactValidator,
    ConfiguredSemanticSupportValidator,
    _SEMANTIC_SUPPORT_SYSTEM_PROMPT,
    _candidate,
)
from tests.domain.capabilities.test_answer_evaluation import _answer, _conflicting_answer
from tests.domain.capabilities.test_answer_question_candidate import candidate_input
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


def test_configured_candidate_exposes_sentence_level_claims_for_conflict_validation() -> None:
    value = candidate_input()
    evidence_id = value.context_bundle.evidence_references[0].evidence_id

    result = _candidate(
        value,
        "Alpha is enabled. Alpha is disabled!",
        (evidence_id,),
    )

    assert [claim.text for claim in result.answer.claims] == [
        "Alpha is enabled.",
        "Alpha is disabled!",
    ]
    assert [claim.location.start_offset for claim in result.answer.claims] == [0, 18]
    assert {item.claim_id for item in result.answer.relationships} == {
        claim.claim_id for claim in result.answer.claims
    }


@pytest.mark.asyncio
async def test_missing_coverage_validator_returns_versioned_immaterial_decision() -> None:
    claim = _answer().claims[0]

    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "Prefer unknown when unsure" in payload["messages"][0]["content"]
        user_payload = json.loads(payload["messages"][1]["content"])
        assert user_payload["claim_id"] == str(claim.claim_id)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"impact": "immaterial"})}}
                ]
            },
        )

    result = await ConfiguredMissingCoverageImpactValidator(
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        transport=httpx.MockTransport(provider),
    ).assess(
        question="What is Alpha?",
        claim=claim,
        evidence=("Alpha evidence",),
    )

    assert result.impact is MissingCoverageImpact.IMMATERIAL
    assert result.policy_version == "qa.missing-coverage-impact:r1"


@pytest.mark.asyncio
async def test_grounded_conflict_validator_uses_all_grounded_claims_and_evidence() -> None:
    answer = _conflicting_answer()

    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "choose a winner" in payload["messages"][0]["content"]
        claims = json.loads(payload["messages"][1]["content"])["claims"]
        assert {item["claim_id"] for item in claims} == {
            str(claim.claim_id) for claim in answer.claims
        }
        assert all(item["evidence"] for item in claims)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "conflicts": [
                                        [
                                            str(answer.claims[0].claim_id),
                                            str(answer.claims[1].claim_id),
                                        ]
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await ConfiguredGroundedConflictValidator(
        api_key="synthetic",
        model="synthetic",
        endpoint="https://example.invalid/v1",
        transport=httpx.MockTransport(provider),
    ).detect(
        claims=answer.claims,
        evidence={
            answer.claims[0].claim_id: ("Alpha evidence",),
            answer.claims[1].claim_id: ("Beta evidence",),
        },
    )

    assert result.conflict_pairs == (
        (answer.claims[0].claim_id, answer.claims[1].claim_id),
    )
    assert result.policy_version == "qa.grounded-conflict:r1"


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
async def test_semantic_support_validator_classifies_unproven_absence_as_unsupported() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system_prompt = payload["messages"][0]["content"]
        user_payload = json.loads(payload["messages"][1]["content"])
        absence_rule = (
            "Classify an absence claim as unsupported, not inconclusive"
            in system_prompt
        )
        unproven_absence = (
            user_payload["claim"] == "The evidence does not mention Beta."
            and all(
                "Beta" not in item["content"]
                for item in user_payload["evidence"]
            )
        )
        outcome = (
            "unsupported"
            if absence_rule and unproven_absence
            else "inconclusive"
        )
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

    request = semantic_request()
    absence_claim = "The evidence does not mention Beta."
    assessment = await validator.assess(
        request.model_copy(
            update={
                "claim": request.claim.model_copy(
                    update={
                        "text": absence_claim,
                        "location": request.claim.location.model_copy(
                            update={"end_offset": len(absence_claim)}
                        ),
                    }
                )
            }
        )
    )

    assert assessment.outcome is SemanticSupportOutcome.UNSUPPORTED


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
