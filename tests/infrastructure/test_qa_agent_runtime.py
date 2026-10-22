from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.infrastructure.qa_agent_runtime import (
    ConfiguredLLMRuntime,
    _SEMANTIC_SUPPORT_SYSTEM_PROMPT,
)


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
    )

    await runtime._require_current_authority()
    with pytest.raises(RuntimeError, match="authority is unavailable"):
        await runtime._require_current_authority()


def test_semantic_support_prompt_does_not_treat_absence_claims_as_grounded() -> None:
    assert "not supported unless the evidence explicitly establishes that absence" in (
        _SEMANTIC_SUPPORT_SYSTEM_PROMPT
    )
