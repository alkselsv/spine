from __future__ import annotations

from types import SimpleNamespace

from spine.memory.cognee.qualification import (
    BackendApproval,
    SearchLane,
    SmokeStatus,
    build_api_matrix,
    build_backend_matrix,
    inspect_search_payload,
)


def _sdk() -> SimpleNamespace:
    async def add(data, dataset_name="main", node_set=None):
        del data, dataset_name, node_set

    async def cognify(datasets=None, dry_run=False, raise_on_error=True):
        del datasets, dry_run, raise_on_error

    async def update(data_id, data, dataset_id, chunk_level_diff=True):
        del data_id, data, dataset_id, chunk_level_diff

    async def delete(data_id, dataset_id, mode="soft"):
        del data_id, dataset_id, mode

    async def forget(
        *, data_id=None, dataset=None, dataset_id=None, everything=False, memory_only=False
    ):
        del data_id, dataset, dataset_id, everything, memory_only

    async def search(
        query_text,
        query_type=None,
        datasets=None,
        dataset_ids=None,
        top_k=15,
        only_context=False,
        verbose=False,
        include_references=False,
    ):
        del query_text, query_type, datasets, dataset_ids, top_k
        del only_context, verbose, include_references

    class Datasets:
        @staticmethod
        async def delete_data(dataset_id, data_id, mode="soft"):
            del dataset_id, data_id, mode

    return SimpleNamespace(
        __version__="1.5.4",
        add=add,
        cognify=cognify,
        update=update,
        delete=delete,
        forget=forget,
        search=search,
        datasets=Datasets,
    )


def test_public_sdk_matrix_accepts_the_audited_version_and_required_operations() -> None:
    report = build_api_matrix(_sdk())

    assert report.version == "1.5.4"
    assert report.status is SmokeStatus.SUPPORTED
    assert {check.name for check in report.checks} == {
        "add",
        "cognify",
        "update",
        "delete",
        "datasets.delete_data",
        "forget",
        "search",
    }
    assert all(check.status is SmokeStatus.SUPPORTED for check in report.checks)


def test_public_sdk_matrix_returns_visible_no_go_for_missing_search_contract() -> None:
    sdk = _sdk()
    sdk.search = lambda query_text: query_text

    report = build_api_matrix(sdk)

    assert report.status is SmokeStatus.NO_GO
    search_check = next(check for check in report.checks if check.name == "search")
    assert search_check.status is SmokeStatus.NO_GO
    assert "include_references" in search_check.missing_parameters


def test_search_payload_probe_records_chunks_entities_relations_and_references() -> None:
    report = inspect_search_payload(
        SearchLane.HYBRID,
        [
            {
                "objects_result": {
                    "chunks": [{"payload": {"text": "Acme policy"}, "score": 0.9}],
                    "entities": [{"id": "entity-1", "name": "Acme", "type": "organization"}],
                    "facts": [
                        {
                            "subject": "entity-1",
                            "relationship": "works_for",
                            "object": "entity-2",
                        }
                    ],
                },
                "evidence": [
                    {
                        "kind": "segment",
                        "artifact_id": "chunk-1",
                        "data_id": "data-1",
                        "chunk_id": "chunk-1",
                    },
                    {"kind": "graph_node", "artifact_id": "entity-1"},
                    {"kind": "graph_edge", "artifact_id": "edge-1"},
                ],
            }
        ],
    )

    assert report.status is SmokeStatus.SUPPORTED
    assert report.item_count == 1
    assert report.observed_shapes == ("chunk", "entity", "relation")
    assert report.evidence_kinds == ("graph_edge", "graph_node", "segment")
    assert report.missing_shapes == ()


def test_search_payload_probe_returns_no_go_when_structured_read_is_incomplete() -> None:
    report = inspect_search_payload(
        SearchLane.HYBRID,
        [{"objects_result": {"chunks": [{"text": "only a chunk"}]}, "evidence": []}],
    )

    assert report.status is SmokeStatus.NO_GO
    assert report.missing_shapes == ("entity", "relation")
    assert report.missing_evidence_kinds == ("segment", "graph_node", "graph_edge")


def test_search_payload_probe_returns_no_go_for_a_missing_result() -> None:
    report = inspect_search_payload(SearchLane.CHUNKS, [])

    assert report.status is SmokeStatus.NO_GO
    assert report.item_count == 0
    assert report.missing_shapes == ("chunk",)
    assert report.missing_evidence_kinds == ("segment",)


def test_backend_matrix_distinguishes_local_capability_from_production_approval() -> None:
    profiles = {profile.name: profile for profile in build_backend_matrix()}

    assert profiles["local"].approval is BackendApproval.CAPABILITY_ONLY
    assert profiles["production_candidate"].approval is BackendApproval.NOT_APPROVED
    assert profiles["postgres_demo"].approval is BackendApproval.REJECTED
    assert "production" in profiles["postgres_demo"].reason.lower()
