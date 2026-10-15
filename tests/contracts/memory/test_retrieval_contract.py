from __future__ import annotations

import pytest

from spine.memory.contracts import (
    ProjectionChunk,
    ProjectionFailureCode,
    ProjectionOutcome,
    ProjectionRelation,
    RetrievalScore,
)

from .adapter import ContextProjectionFactory
from .test_conformance import ENVIRONMENT_ID, WORKSPACE_ID, evidence, retrieve_command


async def test_retrieval_is_structured_and_never_requires_a_completion_string(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    reference = evidence()
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(ProjectionChunk(chunk_id="chunk", text="answer evidence", evidence=(reference,)),),
        relations=(
            ProjectionRelation(
                relation_id="relation",
                kind="owns",
                subject_entity_id="subject",
                object_entity_id="object",
                evidence=(reference,),
            ),
        ),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(identity="structured", digest="9" * 64)
    )

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].text == "answer evidence"
    assert result.relations[0].kind == "owns"
    assert result.evidence[0].evidence_id == reference.evidence_id


async def test_retrieval_score_is_optional_and_round_trips_when_provider_supplies_it(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    reference = evidence()
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(
            ProjectionChunk(
                chunk_id="scored",
                text="scored evidence",
                evidence=(reference,),
                score=RetrievalScore(
                    value=0.75,
                    metric="cosine",
                    direction="higher_is_better",
                    provider_contract_version="fake:v1",
                ),
            ),
        ),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(identity="score", digest="8" * 64)
    )

    assert result.chunks[0].score is not None
    assert result.chunks[0].score.value == 0.75


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("missing", ProjectionFailureCode.MISSING_EVIDENCE),
        ("stale", ProjectionFailureCode.STALE_EVIDENCE),
        ("ambiguous", ProjectionFailureCode.AMBIGUOUS_EVIDENCE),
        ("incomplete", ProjectionFailureCode.INCOMPLETE_EVIDENCE),
    ),
)
async def test_retrieval_returns_typed_failure_for_unusable_evidence(
    context_projection_factory: ContextProjectionFactory,
    status: str,
    expected: ProjectionFailureCode,
) -> None:
    adapter = context_projection_factory()
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(
            ProjectionChunk(
                chunk_id="unsafe",
                text="must not be returned",
                evidence=(evidence(status=status),),
            ),
        ),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(identity=f"unusable-{status}", digest="a" * 64)
    )

    assert result.failure is not None
    assert result.failure.code is expected
    assert result.chunks == ()
