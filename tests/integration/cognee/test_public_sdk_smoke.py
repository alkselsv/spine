from __future__ import annotations

import os
from uuid import uuid4

import pytest

from spine.memory.cognee.live_smoke import LiveSmokeConfig, run_local_smoke
from spine.memory.cognee.qualification import SearchLane, SmokeStatus


pytestmark = [
    pytest.mark.cognee_live,
    pytest.mark.skipif(
        os.environ.get("SPINE_COGNEE_LIVE") != "1",
        reason="set SPINE_COGNEE_LIVE=1 to run the local Cognee qualification",
    ),
]


@pytest.mark.asyncio
async def test_public_cognee_154_local_smoke_matrix() -> None:
    dataset_name = f"spine_issue_198_{uuid4().hex}"
    report = await run_local_smoke(
        LiveSmokeConfig(
            dataset_name=dataset_name,
            source_text=(
                "Acme employs Alice. Alice works for Acme. "
                "The policy is stored in the context projection."
            ),
            updated_text=(
                "Acme employs Alice. Alice works for Acme. "
                "The updated policy is stored in the context projection."
            ),
            query="Who works for Acme and where is the policy stored?",
        )
    )

    assert report.api.status is SmokeStatus.SUPPORTED
    assert report.dataset_name == dataset_name
    assert report.node_set == ("issue-198-smoke",)
    assert {observation.lane for observation in report.searches} == {
        SearchLane.CHUNKS,
        SearchLane.GRAPH,
        SearchLane.HYBRID,
    }
    assert all(observation.status is SmokeStatus.SUPPORTED for observation in report.searches)
    assert {
        "add",
        "cognify",
        "search:chunks",
        "search:graph",
        "search:hybrid",
        "update",
        "search:updated",
        "datasets.delete_data",
        "search:deleted",
        "datasets.delete_data:repeat",
        "forget:memory_only",
        "forget:dataset",
    }.issubset(report.operation_names)
    assert report.updated_content_observed is True
    assert report.deleted_content_absent is True
