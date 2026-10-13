"""Opt-in local Cognee 1.5.4 physical smoke flow.

The runner is intentionally kept at the adapter/qualification boundary.  It
does not expose Cognee objects to application code and it never imports a
private Cognee module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spine.memory.cognee.qualification import (
    ApiMatrix,
    SearchLane,
    SearchObservation,
    SmokeStatus,
    build_api_matrix,
    inspect_search_payload,
)


@dataclass(frozen=True)
class LiveSmokeConfig:
    dataset_name: str
    source_text: str
    updated_text: str
    query: str
    node_set: tuple[str, ...] = ("issue-198-smoke",)


@dataclass(frozen=True)
class LiveSmokeReport:
    api: ApiMatrix
    dataset_name: str
    node_set: tuple[str, ...]
    searches: tuple[SearchObservation, ...]
    operation_names: tuple[str, ...]


def _dataset_name(dataset: object) -> str | None:
    if isinstance(dataset, dict):
        value = dataset.get("name")
    else:
        value = getattr(dataset, "name", None)
    return value if isinstance(value, str) else None


def _dataset_id(dataset: object) -> Any:
    if isinstance(dataset, dict):
        return dataset.get("id")
    return getattr(dataset, "id", None)


def _data_id(row: object) -> Any:
    if isinstance(row, dict):
        return row.get("id")
    return getattr(row, "id", None)


async def run_local_smoke(config: LiveSmokeConfig) -> LiveSmokeReport:
    """Run the physical local profile against only public Cognee entrypoints."""

    from spine.memory.cognee_memory import configure_environment

    configure_environment()
    import cognee

    api = build_api_matrix(cognee)
    if api.status is not SmokeStatus.SUPPORTED:
        return LiveSmokeReport(
            api=api,
            dataset_name=config.dataset_name,
            node_set=config.node_set,
            searches=(),
            operation_names=(),
        )

    operations: list[str] = []
    searches: list[SearchObservation] = []
    dataset_id = None
    data_id = None
    dataset_created = False
    try:
        await cognee.add(
            config.source_text,
            dataset_name=config.dataset_name,
            node_set=list(config.node_set),
            run_in_background=False,
        )
        operations.append("add")

        datasets = await cognee.datasets.list_datasets()
        dataset = next(
            (candidate for candidate in datasets if _dataset_name(candidate) == config.dataset_name),
            None,
        )
        if dataset is None:
            raise RuntimeError("local smoke dataset was not returned by the public dataset API")
        dataset_created = True
        dataset_id = _dataset_id(dataset)
        data_rows = await cognee.datasets.list_data(dataset_id)
        if not data_rows:
            raise RuntimeError("local smoke data was not returned by the public dataset API")
        data_id = _data_id(data_rows[-1])
        operations.append("datasets.list_data")

        await cognee.cognify(datasets=[config.dataset_name], run_in_background=False)
        operations.append("cognify")

        common = {
            "datasets": [config.dataset_name],
            "only_context": True,
            "verbose": True,
            "include_references": True,
            "top_k": 5,
        }
        chunks = await cognee.search(
            config.query,
            query_type=cognee.SearchType.CHUNKS,
            **common,
        )
        searches.append(inspect_search_payload(SearchLane.CHUNKS, chunks))
        operations.append("search:chunks")

        graph = await cognee.search(
            config.query,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            **common,
        )
        searches.append(inspect_search_payload(SearchLane.GRAPH, graph))
        operations.append("search:graph")

        hybrid = await cognee.search(
            config.query,
            query_type=cognee.SearchType.HYBRID_COMPLETION,
            **common,
        )
        searches.append(inspect_search_payload(SearchLane.HYBRID, hybrid))
        operations.append("search:hybrid")

        await cognee.update(
            data_id=data_id,
            data=config.updated_text,
            dataset_id=dataset_id,
            chunk_level_diff=True,
        )
        operations.append("update")
        await cognee.cognify(datasets=[config.dataset_name], run_in_background=False)
        operations.append("cognify:updated")

        await cognee.datasets.delete_data(dataset_id=dataset_id, data_id=data_id)
        operations.append("datasets.delete_data")
        await cognee.datasets.delete_data(dataset_id=dataset_id, data_id=data_id)
        operations.append("datasets.delete_data:repeat")
        await cognee.forget(dataset=config.dataset_name, memory_only=True)
        operations.append("forget:memory_only")
    finally:
        # Cleanup is also public and dataset-scoped.  If the smoke flow removed
        # the row already, Cognee still accepts the dataset-level cleanup.
        if dataset_created:
            await cognee.forget(dataset=config.dataset_name)
            operations.append("forget:dataset")

    return LiveSmokeReport(
        api=api,
        dataset_name=config.dataset_name,
        node_set=config.node_set,
        searches=tuple(searches),
        operation_names=tuple(operations),
    )


__all__ = ["LiveSmokeConfig", "LiveSmokeReport", "run_local_smoke"]
