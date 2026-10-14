from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionFailureCode,
    ProjectionNamespace,
    ProjectionNamespaceState,
    ProjectionOutcome,
    ProjectionSourceRevision,
    RetrieveCommand,
)
from spine.memory.cognee.retrieval import (
    CogneePublicStructuredRetrievalSdk,
    CogneeStructuredRetrieval,
    CogneeStructuredRetrievalSdk,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter


WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
SOURCE_OBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
SOURCE_REVISION_ID = UUID("40000000-0000-4000-8000-000000000001")
RUN_ID = UUID("50000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("60000000-0000-4000-8000-000000000001")
PARTITION_ID = UUID("70000000-0000-4000-8000-000000000001")
CONFIG_ID = UUID("80000000-0000-4000-8000-000000000001")
BOUNDARY_ID = UUID("90000000-0000-4000-8000-000000000001")


@dataclass
class FakeSearchSdk(CogneeStructuredRetrievalSdk):
    version: str = "1.5.4"
    chunks_response: object = ()
    hybrid_response: object = ()

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def search(self, query: str, **kwargs: object) -> object:
        self.calls.append({"query": query, **kwargs})
        return (
            self.chunks_response
            if kwargs["query_type"] == "chunks"
            else self.hybrid_response
        )


def source(*, status: str = "current") -> ProjectionSourceRevision:
    return ProjectionSourceRevision(
        source_object_id=SOURCE_OBJECT_ID,
        source_revision_id=SOURCE_REVISION_ID,
        revision_digest="a" * 64,
        revision_number=1,
        status=status,
        processing_receipt_id=UUID("a0000000-0000-4000-8000-000000000001"),
        locator_contract_version="locator:v1",
    )


def command(
    *,
    identity: str = "retrieve-1",
    digest: str = "b" * 64,
    state: ProjectionNamespaceState = ProjectionNamespaceState.ACTIVE,
    source_status: str = "current",
) -> RetrieveCommand:
    candidate = ProjectionCandidate(
        candidate_id=CANDIDATE_ID,
        run_id=RUN_ID,
        partition_id=PARTITION_ID,
    )
    config = ProjectionConfigVersion(
        config_id=CONFIG_ID,
        version="config-1",
        config_digest="c" * 64,
        ontology_version="ontology-1",
        adapter_contract_version="context-projection:v1",
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY_ID,
        boundary_digest="d" * 64,
        accepted_through_generation=1,
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_candidate=candidate,
            projection_config=config,
            canonical_boundary=boundary,
            state=state,
        )
    )
    return RetrieveCommand(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_run_id=RUN_ID,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        source_revisions=(source(status=source_status),),
        physical_namespace=namespace,
        command_identity=identity,
        command_digest=digest,
        query="Who works for Acme?",
        limit=10,
    )


def physical_metadata() -> dict[str, Any]:
    return {
        "workspace_id": str(WORKSPACE_ID),
        "environment_id": str(ENVIRONMENT_ID),
        "source_object_id": str(SOURCE_OBJECT_ID),
        "source_revision_id": str(SOURCE_REVISION_ID),
        "revision_digest": "a" * 64,
        "revision_number": 1,
        "processing_receipt_id": "a0000000-0000-4000-8000-000000000001",
        "evidence_digest": "f" * 64,
        "locator": {"kind": "page", "value": "2"},
    }


def search_responses() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    metadata = physical_metadata()
    evidence = {
        "evidence_id": "evidence-1",
        "source_revision_id": str(SOURCE_REVISION_ID),
        "locator": {"kind": "page", "value": "2"},
        "status": "valid",
    }
    chunks = [
        {
            "objects_result": [
                {"id": "chunk-1", "text": "Alice works for Acme.", "metadata": metadata}
            ],
            "evidence": [{"kind": "segment", **evidence}],
        }
    ]
    hybrid = [
        {
            "objects_result": {
                "entities": [
                    {"id": "entity-1", "name": "Acme", "type": "organization", "metadata": metadata}
                ],
                "facts": [
                    {
                        "id": "relation-1",
                        "predicate": "employs",
                        "subject": "acme",
                        "object": "alice",
                        "metadata": metadata,
                    }
                ],
            },
            "evidence": [
                {"kind": "graph_node", **evidence},
                {"kind": "graph_edge", **evidence},
            ],
        }
    ]
    return chunks, hybrid


@pytest.mark.asyncio
async def test_retrieval_maps_public_structured_search_to_provider_neutral_result() -> None:
    chunks, hybrid = search_responses()
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)
    adapter = CogneeStructuredRetrieval(sdk=sdk)

    result = await adapter.retrieve(command())

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].text == "Alice works for Acme."
    assert result.entities[0].name == "Acme"
    assert result.relations[0].kind == "employs"
    assert result.evidence[0].source_revision_id == SOURCE_REVISION_ID
    assert result.evidence[0].locator.value == "2"
    assert {call["query_type"] for call in sdk.calls} == {"chunks", "hybrid_completion"}
    assert all(call["datasets"] == [f"spine_context_{command().physical_namespace.namespace_digest}"] for call in sdk.calls)


@pytest.mark.asyncio
async def test_retrieval_rejects_shadow_namespace_without_physical_call() -> None:
    chunks, hybrid = search_responses()
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(
        command(state=ProjectionNamespaceState.SHADOW)
    )

    assert result.outcome is ProjectionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
    assert sdk.calls == []


@pytest.mark.asyncio
async def test_retrieval_fails_closed_for_cross_scope_physical_metadata() -> None:
    chunks, hybrid = search_responses()
    chunks[0]["objects_result"][0]["metadata"]["workspace_id"] = str(UUID(int=99))  # type: ignore[index]
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.MISSING_EVIDENCE
    assert result.chunks == ()


@pytest.mark.asyncio
async def test_retrieval_fails_closed_for_cross_environment_physical_metadata() -> None:
    chunks, hybrid = search_responses()
    chunks[0]["objects_result"][0]["metadata"]["environment_id"] = str(UUID(int=98))  # type: ignore[index]
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.MISSING_EVIDENCE
    assert result.chunks == ()


@pytest.mark.asyncio
async def test_retrieval_rejects_reference_without_physical_provenance_mapping() -> None:
    chunks, hybrid = search_responses()
    chunks[0]["objects_result"][0]["metadata"] = {  # type: ignore[index]
        "source_revision_id": str(SOURCE_REVISION_ID),
        "locator": {"kind": "page", "value": "2"},
    }
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.INCOMPLETE
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.INCOMPLETE_EVIDENCE
    assert result.chunks == ()


@pytest.mark.asyncio
async def test_retrieval_rejects_reference_without_processing_evidence() -> None:
    chunks, hybrid = search_responses()
    del chunks[0]["objects_result"][0]["metadata"]["processing_receipt_id"]  # type: ignore[index]
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.INCOMPLETE
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.INCOMPLETE_EVIDENCE
    assert result.chunks == ()


@pytest.mark.asyncio
async def test_retrieval_replays_and_rejects_digest_conflict_without_second_search() -> None:
    chunks, hybrid = search_responses()
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)
    adapter = CogneeStructuredRetrieval(sdk=sdk)

    first = await adapter.retrieve(command())
    replay = await adapter.retrieve(command())
    conflict = await adapter.retrieve(command(digest="e" * 64))

    assert first.outcome is ProjectionOutcome.SUCCEEDED
    assert replay.outcome is ProjectionOutcome.REPLAYED
    assert conflict.failure is not None
    assert conflict.failure.code is ProjectionFailureCode.INTEGRITY_CONFLICT
    assert len(sdk.calls) == 2


@pytest.mark.asyncio
async def test_retrieval_does_not_return_content_when_evidence_is_stale() -> None:
    chunks, hybrid = search_responses()
    chunks[0]["evidence"][0]["status"] = "stale"  # type: ignore[index]
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.STALE
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.STALE_EVIDENCE
    assert result.chunks == ()


@pytest.mark.parametrize(
    ("status", "outcome", "code"),
    (
        ("ambiguous", ProjectionOutcome.AMBIGUOUS, ProjectionFailureCode.AMBIGUOUS_EVIDENCE),
        ("tombstoned", ProjectionOutcome.TOMBSTONED, ProjectionFailureCode.TOMBSTONED_SOURCE),
    ),
)
@pytest.mark.asyncio
async def test_retrieval_does_not_return_content_for_unsafe_evidence(
    status: str,
    outcome: ProjectionOutcome,
    code: ProjectionFailureCode,
) -> None:
    chunks, hybrid = search_responses()
    chunks[0]["evidence"][0]["status"] = status  # type: ignore[index]
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is outcome
    assert result.failure is not None
    assert result.failure.code is code
    assert result.chunks == ()


@pytest.mark.asyncio
async def test_retrieval_returns_typed_incomplete_result_for_missing_locator() -> None:
    chunks, hybrid = search_responses()
    del chunks[0]["objects_result"][0]["metadata"]["locator"]  # type: ignore[index]
    del chunks[0]["evidence"][0]["locator"]  # type: ignore[index]
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.INCOMPLETE
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.INCOMPLETE_EVIDENCE
    assert result.chunks == ()


@pytest.mark.asyncio
async def test_public_sdk_bridge_uses_only_public_search_and_search_type() -> None:
    class SearchType:
        CHUNKS = "public-chunks"
        HYBRID_COMPLETION = "public-hybrid"

    class PublicSdk:
        __version__ = "1.5.4"

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def search(self, query: str, **kwargs: object) -> list[object]:
            self.calls.append((query, kwargs))
            return []

    PublicSdk.SearchType = SearchType
    public_sdk = PublicSdk()
    bridge = CogneePublicStructuredRetrievalSdk(public_sdk)

    await bridge.search("query", query_type="chunks", datasets=["dataset"])

    assert public_sdk.calls == [("query", {"query_type": "public-chunks", "datasets": ["dataset"]})]
