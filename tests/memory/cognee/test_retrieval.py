from __future__ import annotations

from dataclasses import dataclass
import json
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
    RetrievalReadContext,
    RetrieveCommand,
)
from spine.memory.cognee.retrieval import (
    CogneeDataMetadata,
    CogneeDatasetUnavailableError,
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
SNAPSHOT_ID = UUID("91000000-0000-4000-8000-000000000001")


@dataclass
class FakeSearchSdk(CogneeStructuredRetrievalSdk):
    version: str = "1.5.4"
    chunks_response: object = ()
    hybrid_response: object = ()
    data_metadata: tuple[CogneeDataMetadata, ...] = ()
    supports_source_isolation: bool = False

    def __post_init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def search(self, query: str, **kwargs: object) -> object:
        self.calls.append({"query": query, **kwargs})
        return (
            self.chunks_response
            if kwargs["query_type"] == "chunks"
            else self.hybrid_response
        )

    async def list_data_metadata(
        self, dataset_name: str
    ) -> tuple[CogneeDataMetadata, ...]:
        self.calls.append({"list_data_metadata": dataset_name})
        return self.data_metadata


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
        snapshot_id=SNAPSHOT_ID,
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
        "locator_contract_version": "locator:v1",
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


def read_context_for(
    item: RetrieveCommand,
    *,
    source_isolation_pre_retrieval_enforced: bool = True,
) -> RetrievalReadContext:
    return RetrievalReadContext(
        workspace_id=item.workspace_id,
        environment_id=item.environment_id,
        publication_pin_id=UUID("a1000000-0000-4000-8000-000000000001"),
        publication_pin_digest="1" * 64,
        projection_snapshot_id=item.projection_candidate.snapshot_id,
        projection_activation_id=UUID("a2000000-0000-4000-8000-000000000001"),
        activation_generation=1,
        canonical_boundary_id=item.canonical_boundary.boundary_id,
        canonical_boundary_digest=item.canonical_boundary.boundary_digest,
        projection_run_id=item.projection_run_id,
        projection_candidate_id=item.projection_candidate.candidate_id,
        projection_partition_id=item.projection_candidate.partition_id,
        projection_receipt_id=UUID("a3000000-0000-4000-8000-000000000001"),
        physical_namespace=item.physical_namespace.namespace,
        physical_namespace_digest=item.physical_namespace.namespace_digest,
        source_object_ids=(SOURCE_OBJECT_ID,),
        source_revision_ids=(SOURCE_REVISION_ID,),
        provider_capability_reference="cognee-source-filter:v1",
        source_isolation_pre_retrieval_enforced=source_isolation_pre_retrieval_enforced,
    )


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
    assert result.evidence[0].revision_digest == "a" * 64
    assert result.evidence[0].revision_number == 1
    assert result.evidence[0].processing_receipt_id == UUID(
        "a0000000-0000-4000-8000-000000000001"
    )
    assert result.evidence[0].locator_contract_version == "locator:v1"
    assert result.evidence[0].locator.value == "2"
    search_calls = [call for call in sdk.calls if "query_type" in call]
    assert {call["query_type"] for call in search_calls} == {
        "chunks",
        "hybrid_completion",
    }
    assert all(
        call["datasets"]
        == [f"spine_context_{command().physical_namespace.namespace_digest}"]
        for call in search_calls
    )


@pytest.mark.asyncio
async def test_retrieval_maps_only_explicit_score_contract() -> None:
    chunks, hybrid = search_responses()
    chunks[0]["objects_result"][0]["metadata"]["retrieval_score"] = {  # type: ignore[index]
        "value": 0.9,
        "metric": "cosine",
        "direction": "higher_is_better",
        "provider_contract_version": "cognee:v1",
    }
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.chunks[0].score is not None
    assert result.chunks[0].score.metric == "cosine"


@pytest.mark.asyncio
async def test_retrieval_fails_closed_before_search_without_source_isolation_proof() -> None:
    sdk = FakeSearchSdk()
    item = command().model_copy(
        update={"read_context": read_context_for(
            command(), source_isolation_pre_retrieval_enforced=False
        )}
    )

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(item)

    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.SOURCE_ISOLATION_UNPROVEN
    assert sdk.calls == []


@pytest.mark.asyncio
async def test_retrieval_joins_public_dataset_metadata_to_chunk_document_id() -> None:
    data_id = "a0000000-0000-4000-8000-000000000009"
    metadata = {
        **physical_metadata(),
        "spine_schema": "chunk-artifact:v1",
        "segment_id": data_id,
        "evidence_id": "segment-evidence-1",
        "evidence_digest": "f" * 64,
    }
    chunks = [
        {
            "objects_result": [
                {
                    "payload": {
                        "text": "Alice works for Acme.",
                        "document_id": data_id,
                        "metadata": {"index_fields": ["text"]},
                    }
                }
            ],
            "evidence": [],
        }
    ]
    sdk = FakeSearchSdk(
        chunks_response=chunks,
        data_metadata=(
            CogneeDataMetadata(data_id=data_id, external_metadata=metadata),
        ),
    )

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].chunk_id == data_id
    assert result.evidence[0].evidence_id == "segment-evidence-1"
    assert result.evidence[0].source_revision_id == SOURCE_REVISION_ID
    assert sdk.calls[0] == {
        "list_data_metadata": (
            f"spine_context_{command().physical_namespace.namespace_digest}"
        )
    }


@pytest.mark.asyncio
async def test_retrieval_joins_namespace_scoped_data_id_to_canonical_segment() -> None:
    data_id = "a0000000-0000-4000-8000-000000000019"
    segment_id = "a0000000-0000-4000-8000-000000000009"
    metadata = {
        **physical_metadata(),
        "spine_schema": "chunk-artifact:v1",
        "data_id": data_id,
        "segment_id": segment_id,
        "evidence_id": "segment-evidence-1",
        "evidence_digest": "f" * 64,
    }
    chunks = [
        {
            "objects_result": [
                {
                    "payload": {
                        "text": "Alice works for Acme.",
                        "document_id": data_id,
                        "metadata": {"index_fields": ["text"]},
                    }
                }
            ],
            "evidence": [],
        }
    ]
    sdk = FakeSearchSdk(
        chunks_response=chunks,
        data_metadata=(
            CogneeDataMetadata(data_id=data_id, external_metadata=metadata),
        ),
    )

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].chunk_id == segment_id
    assert result.evidence[0].evidence_id == "segment-evidence-1"
    assert result.evidence[0].source_revision_id == SOURCE_REVISION_ID


@pytest.mark.asyncio
async def test_retrieval_rejects_conflicting_joined_and_search_metadata() -> None:
    data_id = "a0000000-0000-4000-8000-000000000009"
    metadata = {
        **physical_metadata(),
        "spine_schema": "chunk-artifact:v1",
        "segment_id": data_id,
        "evidence_id": "segment-evidence-1",
        "evidence_digest": "f" * 64,
    }
    chunks = [
        {
            "objects_result": [
                {
                    "id": "provider-chunk-1",
                    "text": "protected customer text",
                    "document_id": data_id,
                    "metadata": {"workspace_id": str(UUID(int=99))},
                }
            ],
            "evidence": [{"kind": "segment", "data_id": data_id}],
        }
    ]
    sdk = FakeSearchSdk(
        chunks_response=chunks,
        data_metadata=(
            CogneeDataMetadata(data_id=data_id, external_metadata=metadata),
        ),
    )

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.AMBIGUOUS
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.AMBIGUOUS_EVIDENCE
    assert result.chunks == ()
    assert "protected customer text" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_retrieval_rejects_mismatched_locator_contract_version() -> None:
    chunks, hybrid = search_responses()
    chunks[0]["objects_result"][0]["metadata"]["locator_contract_version"] = (
        "fabricated-locator:v1"
    )

    result = await CogneeStructuredRetrieval(
        sdk=FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)
    ).retrieve(command())

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.MISSING_EVIDENCE
    assert result.chunks == ()


@pytest.mark.asyncio
async def test_retrieval_maps_typed_graph_artifacts_from_public_chunk_results() -> None:
    entity_data_id = "a0000000-0000-4000-8000-000000000010"
    relation_data_id = "a0000000-0000-4000-8000-000000000011"
    evidence = {
        "evidence_id": "graph-evidence-1",
        "source_revision_id": str(SOURCE_REVISION_ID),
        "locator": {"kind": "page", "value": "2"},
        "evidence_digest": "f" * 64,
        "status": "valid",
    }

    def graph_metadata(kind: str) -> dict[str, object]:
        return {
            **physical_metadata(),
            "spine_schema": "graph-artifact:v1",
            "artifact_kind": kind,
            "evidence": [evidence],
        }

    chunks = [
        {
            "objects_result": [
                {
                    "payload": {
                        "text": json.dumps(
                            {
                                "entity_id": "entity-acme",
                                "entity_type": "organization",
                                "canonical_name": "Acme",
                            }
                        ),
                        "document_id": entity_data_id,
                    }
                },
                {
                    "payload": {
                        "text": json.dumps(
                            {
                                "relation_id": "relation-employs",
                                "relation_type": "employs",
                                "subject_entity_id": "entity-acme",
                                "object_entity_id": "entity-alice",
                            }
                        ),
                        "document_id": relation_data_id,
                    }
                },
            ]
        }
    ]
    sdk = FakeSearchSdk(
        chunks_response=chunks,
        data_metadata=(
            CogneeDataMetadata(
                data_id=entity_data_id,
                external_metadata=graph_metadata("entity"),
            ),
            CogneeDataMetadata(
                data_id=relation_data_id,
                external_metadata=graph_metadata("relation"),
            ),
        ),
    )

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks == ()
    assert result.entities[0].entity_id == "entity-acme"
    assert result.relations[0].relation_id == "relation-employs"
    assert result.evidence[0].source_revision_id == SOURCE_REVISION_ID


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
async def test_retrieval_rejects_mismatched_explicit_context_without_physical_call() -> None:
    chunks, hybrid = search_responses()
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)
    base = command()
    wrong_snapshot = base.projection_candidate.model_copy(
        update={"snapshot_id": UUID("91000000-0000-4000-8000-000000000002")}
    )
    wrong_boundary = base.canonical_boundary.model_copy(
        update={
            "boundary_id": UUID("90000000-0000-4000-8000-000000000002"),
            "boundary_digest": "e" * 64,
        }
    )
    wrong_namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=base.workspace_id,
            environment_id=base.environment_id,
            projection_candidate=wrong_snapshot,
            projection_config=base.projection_config,
            canonical_boundary=base.canonical_boundary,
            state=ProjectionNamespaceState.ACTIVE,
        )
    )
    mismatched = (
        base.model_copy(
            update={
                "projection_candidate": wrong_snapshot,
                "command_identity": "wrong-snapshot",
                "command_digest": "1" * 64,
            }
        ),
        base.model_copy(
            update={
                "canonical_boundary": wrong_boundary,
                "command_identity": "wrong-boundary",
                "command_digest": "2" * 64,
            }
        ),
        base.model_copy(
            update={
                "physical_namespace": wrong_namespace,
                "command_identity": "wrong-namespace",
                "command_digest": "3" * 64,
            }
        ),
    )

    results = [
        await CogneeStructuredRetrieval(sdk=sdk).retrieve(item)
        for item in mismatched
    ]

    assert all(result.outcome is ProjectionOutcome.FAILED for result in results)
    assert all(
        result.failure is not None
        and result.failure.code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
        for result in results
    )
    assert all(result.chunks == () for result in results)
    assert all(result.entities == () for result in results)
    assert all(result.relations == () for result in results)
    assert all("Acme" not in result.model_dump_json() for result in results)
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
    assert len([call for call in sdk.calls if "query_type" in call]) == 2


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
async def test_retrieval_logs_only_bounded_stage_for_rejected_physical_evidence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks, hybrid = search_responses()
    chunks[0]["objects_result"][0]["metadata"] = {  # type: ignore[index]
        "source_revision_id": str(SOURCE_REVISION_ID),
        "text": "protected customer text",
    }
    sdk = FakeSearchSdk(chunks_response=chunks, hybrid_response=hybrid)

    await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert "stage=reference_scope" in caplog.text
    assert "protected customer text" not in caplog.text


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


@pytest.mark.asyncio
async def test_public_sdk_bridge_reads_external_metadata_through_dataset_api() -> None:
    dataset_id = UUID("a0000000-0000-4000-8000-000000000001")
    data_id = UUID("b0000000-0000-4000-8000-000000000001")

    class Datasets:
        async def list_datasets(self) -> list[object]:
            return [{"id": dataset_id, "name": "dataset"}]

        async def list_data(self, requested_id: UUID) -> list[object]:
            assert requested_id == dataset_id
            return [
                {
                    "id": data_id,
                    "external_metadata": {"source_revision_id": "revision-1"},
                }
            ]

    class PublicSdk:
        __version__ = "1.5.4"
        datasets = Datasets()

    records = await CogneePublicStructuredRetrievalSdk(
        PublicSdk()
    ).list_data_metadata("dataset")

    assert records == (
        CogneeDataMetadata(
            data_id=str(data_id),
            external_metadata={"source_revision_id": "revision-1"},
        ),
    )


@pytest.mark.asyncio
async def test_public_sdk_bridge_rejects_incomplete_physical_metadata_rows() -> None:
    dataset_id = UUID("a0000000-0000-4000-8000-000000000001")

    class Datasets:
        async def list_datasets(self) -> list[object]:
            return [{"id": dataset_id, "name": "dataset"}]

        async def list_data(self, requested_id: UUID) -> list[object]:
            assert requested_id == dataset_id
            return [{"id": UUID(int=1)}]

    class PublicSdk:
        __version__ = "1.5.4"
        datasets = Datasets()

    with pytest.raises(ValueError, match="metadata is malformed"):
        await CogneePublicStructuredRetrievalSdk(PublicSdk()).list_data_metadata(
            "dataset"
        )


@pytest.mark.asyncio
async def test_public_sdk_bridge_reports_an_absent_dataset() -> None:
    class Datasets:
        @staticmethod
        async def list_datasets() -> list[object]:
            return []

    class PublicSdk:
        __version__ = "1.5.4"
        datasets = Datasets()

    with pytest.raises(CogneeDatasetUnavailableError):
        await CogneePublicStructuredRetrievalSdk(
            PublicSdk()
        ).list_data_metadata("absent-dataset")


@pytest.mark.asyncio
async def test_retrieval_returns_missing_without_searching_an_empty_dataset() -> None:
    class MissingDatasetSdk(FakeSearchSdk):
        async def list_data_metadata(
            self, dataset_name: str
        ) -> tuple[CogneeDataMetadata, ...]:
            self.calls.append({"list_data_metadata": dataset_name})
            raise CogneeDatasetUnavailableError("synthetic protected provider detail")

    sdk = MissingDatasetSdk()

    result = await CogneeStructuredRetrieval(sdk=sdk).retrieve(command())

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.MISSING_EVIDENCE
    assert sdk.calls == [
        {
            "list_data_metadata": (
                f"spine_context_{command().physical_namespace.namespace_digest}"
            )
        }
    ]
