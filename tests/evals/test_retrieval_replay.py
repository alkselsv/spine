from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.common import ActorKind, ActorRef
from spine.evals.qa_r1 import load_r1_dataset
from spine.evals.retrieval_replay import DeterministicRetrievalReplay
from spine.memory.contracts import RetrievalQuery


def _query(case, *, workspace_id: UUID | None = None) -> RetrievalQuery:
    return RetrievalQuery(
        workspace_id=case.workspace_id if workspace_id is None else workspace_id,
        environment_id=case.environment_id,
        query=case.question,
        requested_by=ActorRef(kind=ActorKind.HUMAN, id=case.principal_id),
        context_profile="qa-r1-replay:v1",
        limit=10,
    )


@pytest.mark.asyncio
async def test_authorized_current_revision_returns_ranked_evidence() -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "changed-revision")

    result = await DeterministicRetrievalReplay(dataset).retrieve(_query(case))

    assert [reference.evidence_id for reference in result.references] == ["ev-policy-current"]
    assert result.context == ("Retention period: 24 months.",)
    assert result.references[0].source_revision_id == UUID("11111111-1111-4111-8111-111111111132")
    assert result.references[0].locator == {
        "kind": "pdf_page",
        "page": 1,
        "schema_version": "locator:pdf-page:v1",
    }
    assert result.retrieval_strategy == "hybrid:v1"
    assert result.index_version == "qa-r1-replay:v1"


@pytest.mark.asyncio
async def test_every_dataset_case_has_stable_expected_retrieval_outcome() -> None:
    dataset = load_r1_dataset()
    replay = DeterministicRetrievalReplay(dataset)

    for case in dataset.cases:
        first = await replay.retrieve_case(case, _query(case))
        second = await replay.retrieve_case(case, _query(case))

        assert first.model_dump(mode="json") == second.model_dump(mode="json")
        if case.expected_outcome == "answered":
            assert {reference.evidence_id for reference in first.references} == set(case.expected_evidence_ids), case.case_id
        else:
            assert first.references == ()
            assert first.context == ()


@pytest.mark.asyncio
async def test_references_are_fixture_backed_and_not_fabricated() -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "multi-document-multi-hop")
    evidence = {
        item.evidence_id: (revision, item)
        for revision in dataset.source_revisions
        for item in revision.evidence
    }

    result = await DeterministicRetrievalReplay(dataset).retrieve(_query(case))

    assert all(reference.evidence_id in evidence for reference in result.references)
    assert all(
        reference.source_revision_id == evidence[reference.evidence_id][0].revision_id
        and reference.locator == evidence[reference.evidence_id][1].locator.model_dump(mode="json")
        for reference in result.references
    )


@pytest.mark.asyncio
async def test_stale_projection_abstains_until_rebuild_selects_current_revision() -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "changed-revision")
    stale = UUID("11111111-1111-4111-8111-111111111131")
    replay = DeterministicRetrievalReplay(dataset, projected_revision_ids={stale})

    stale_result = await replay.retrieve(_query(case))
    replay.rebuild()
    rebuilt_result = await replay.retrieve(_query(case))

    assert stale_result.references == ()
    assert rebuilt_result.references[0].source_revision_id == UUID("11111111-1111-4111-8111-111111111132")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_id",
    ["denied-acl", "mixed-acl", "tombstone", "absent-evidence-abstention", "workspace-isolation"],
)
async def test_unsafe_fixture_case_returns_no_protected_evidence(case_id: str) -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == case_id)

    result = await DeterministicRetrievalReplay(dataset).retrieve(_query(case))

    assert result.references == ()
    assert result.context == ()
    protected_content = [revision.content for revision in dataset.source_revisions if revision.content]
    assert not any(content in " ".join(result.context) for content in protected_content)


@pytest.mark.asyncio
async def test_cross_workspace_query_returns_empty_without_leaking_other_workspace() -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "allowed-acl")
    other_workspace = UUID("22222222-2222-4222-8222-222222222111")

    result = await DeterministicRetrievalReplay(dataset).retrieve(_query(case, workspace_id=other_workspace))

    assert result.references == ()
    assert result.context == ()


@pytest.mark.asyncio
async def test_duplicate_ingestion_produces_one_stable_logical_result() -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "duplicate-ingestion")
    replay = DeterministicRetrievalReplay(dataset)

    first = await replay.retrieve(_query(case))
    second = await replay.retrieve(_query(case))

    assert len(case.observation_ids) == 2
    assert len({observation.idempotency_key for observation in dataset.observations}) == 1
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [reference.evidence_id for reference in first.references] == ["ev-price-row"]
