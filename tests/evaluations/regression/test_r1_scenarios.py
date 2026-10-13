from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.common import ActorKind, ActorRef
from spine.domain.evaluations import EvaluationVerdict
from spine.evals.qa_r1 import QACase, QADataset, SourceRevisionFixture, load_r1_dataset
from spine.evals.retrieval_replay import DeterministicRetrievalReplay
from spine.evaluations.citation_metrics import (
    AnswerClaim,
    AnswerSubmission,
    Citation,
    ContextBundle,
    evaluate_answer_outcome,
    evaluate_citations,
    evaluate_groundedness,
)
from spine.memory.contracts import RetrievalQuery, RetrievalResult


DATASET = load_r1_dataset()
REPLAY_PROFILE = "qa-r1-replay:v1"
CONTEXT_BUNDLE_ID = "qa-r1-regression-bundle"


def _case(case_id: str) -> QACase:
    return next(case for case in DATASET.cases if case.case_id == case_id)


def _query(case: QACase) -> RetrievalQuery:
    return RetrievalQuery(
        workspace_id=case.workspace_id,
        environment_id=case.environment_id,
        query=case.question,
        requested_by=ActorRef(kind=ActorKind.HUMAN, id=case.principal_id),
        context_profile=REPLAY_PROFILE,
        limit=10,
    )


def _revisions(dataset: QADataset) -> dict[UUID, SourceRevisionFixture]:
    return {revision.revision_id: revision for revision in dataset.source_revisions}


def _answer_from_retrieval(
    case: QACase,
    result: RetrievalResult,
    *,
    dataset: QADataset,
) -> tuple[AnswerSubmission, ContextBundle]:
    revisions = _revisions(dataset)
    citations = tuple(
        Citation(
            citation_id=f"citation-{reference.evidence_id}",
            evidence_id=reference.evidence_id,
            source_object_id=UUID(reference.source_id),
            source_revision_id=reference.source_revision_id,
            workspace_id=reference.workspace_id,
            environment_id=reference.environment_id,
            source_revision_digest=revisions[reference.source_revision_id].revision_digest,
            locator=reference.locator,
            context_bundle_id=CONTEXT_BUNDLE_ID,
        )
        for reference in result.references
    )
    citations_by_evidence = {citation.evidence_id: citation for citation in citations}
    claims = tuple(
        AnswerClaim(
            claim_id=claim.claim_id,
            text=claim.text,
            citation_ids=tuple(
                citations_by_evidence[evidence_id].citation_id
                for evidence_id in claim.evidence_ids
            ),
        )
        for claim in case.expected_claims
    )
    return (
        AnswerSubmission(
            outcome=case.expected_outcome,
            claims=claims,
            citations=citations,
            semantic_score=1.0,
        ),
        ContextBundle(
            bundle_id=CONTEXT_BUNDLE_ID,
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            references=result.references,
        ),
    )


@pytest.mark.asyncio
async def test_answered_cases_replay_expected_claims_and_grounded_evidence() -> None:
    replay = DeterministicRetrievalReplay(DATASET)

    for case in DATASET.cases:
        if case.expected_outcome != "answered":
            continue

        result = await replay.retrieve_case(case, _query(case))
        answer, bundle = _answer_from_retrieval(case, result, dataset=DATASET)

        assert case.expected_answer
        assert [reference.evidence_id for reference in result.references] == list(
            case.expected_evidence_ids
        ), case.case_id
        assert evaluate_citations(
            answer, case, dataset=DATASET, context_bundle=bundle
        ).verdict is EvaluationVerdict.PASS
        assert evaluate_groundedness(
            answer, case, dataset=DATASET, context_bundle=bundle
        ).verdict is EvaluationVerdict.PASS
        assert evaluate_answer_outcome(
            answer, case, dataset=DATASET, context_bundle=bundle
        ).verdict is EvaluationVerdict.PASS


@pytest.mark.asyncio
async def test_non_answered_cases_replay_empty_and_preserve_expected_outcome() -> None:
    replay = DeterministicRetrievalReplay(DATASET)
    protected_content = tuple(
        revision.content
        for revision in DATASET.source_revisions
        if revision.content is not None
    )

    for case in DATASET.cases:
        if case.expected_outcome == "answered":
            continue

        result = await replay.retrieve_case(case, _query(case))
        answer = AnswerSubmission(outcome=case.expected_outcome)
        bundle = ContextBundle(
            bundle_id=CONTEXT_BUNDLE_ID,
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            references=result.references,
        )

        assert result.context == ()
        assert result.references == ()
        serialized = result.model_dump_json()
        assert not any(content in serialized for content in protected_content)
        assert evaluate_answer_outcome(
            answer, case, dataset=DATASET, context_bundle=bundle
        ).verdict is EvaluationVerdict.PASS
        assert evaluate_groundedness(
            answer, case, dataset=DATASET, context_bundle=bundle
        ).verdict is EvaluationVerdict.PASS


@pytest.mark.asyncio
async def test_stale_projection_retry_rebuilds_current_revision() -> None:
    case = _case("changed-revision")
    stale_revision = UUID("11111111-1111-4111-8111-111111111131")
    replay = DeterministicRetrievalReplay(DATASET, projected_revision_ids={stale_revision})

    first_attempt = await replay.retrieve_case(case, _query(case))
    replay.rebuild()
    retry = await replay.retrieve_case(case, _query(case))

    assert first_attempt.references == ()
    assert [reference.evidence_id for reference in retry.references] == [
        "ev-policy-current"
    ]
    assert retry.references[0].source_revision_id == UUID(
        "11111111-1111-4111-8111-111111111132"
    )


@pytest.mark.asyncio
async def test_tombstoned_revision_stays_unavailable_after_rebuild() -> None:
    tombstone_case = _case("tombstone")
    predecessor_id = UUID("11111111-1111-4111-8111-111111111137")
    tombstone_id = UUID("11111111-1111-4111-8111-111111111136")
    case_with_history = tombstone_case.model_copy(
        update={"available_revision_ids": (predecessor_id, tombstone_id)}
    )
    dataset = DATASET.model_copy(update={"cases": (*DATASET.cases[:-1], case_with_history)})
    replay = DeterministicRetrievalReplay(dataset, projected_revision_ids={predecessor_id})

    stale_projection = await replay.retrieve_case(case_with_history, _query(case_with_history))
    replay.rebuild()
    rebuilt_projection = await replay.retrieve_case(case_with_history, _query(case_with_history))

    assert stale_projection.references == ()
    assert rebuilt_projection.references == ()
    assert rebuilt_projection.context == ()


@pytest.mark.asyncio
async def test_duplicate_ingestion_and_partial_replay_retry_are_idempotent() -> None:
    case = _case("duplicate-ingestion")
    replay = DeterministicRetrievalReplay(DATASET, projected_revision_ids=set())

    partial_attempt = await replay.retrieve_case(case, _query(case))
    replay.rebuild()
    first_success = await replay.retrieve_case(case, _query(case))
    replay.rebuild()
    second_success = await replay.retrieve_case(case, _query(case))

    assert partial_attempt.references == ()
    assert first_success.model_dump(mode="json") == second_success.model_dump(mode="json")
    assert [reference.evidence_id for reference in first_success.references] == [
        "ev-price-row"
    ]
    assert len(case.observation_ids) == 2
    observations = {
        observation.observation_id: observation for observation in DATASET.observations
    }
    assert len(
        {observations[observation_id].idempotency_key for observation_id in case.observation_ids}
    ) == 1
