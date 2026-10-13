from __future__ import annotations

from spine.memory.contracts import (
    ProjectionChunk,
    ProjectionFailureCode,
    ProjectionOutcome,
    RemoveCommand,
)

from .adapter import ContextProjectionFactory
from .test_conformance import ENVIRONMENT_ID, WORKSPACE_ID, command_context, evidence, retrieve_command


async def test_remove_is_idempotent_and_tombstoned_evidence_is_fenced(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    command = RemoveCommand(
        **command_context(),
        command_identity="remove-tombstone",
        command_digest="b" * 64,
    )
    first = await adapter.projection.remove(command)
    replay = await adapter.projection.remove(command)

    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(
            ProjectionChunk(
                chunk_id="deleted",
                text="deleted source content",
                evidence=(evidence(status="tombstoned"),),
            ),
        ),
    )
    retrieval = await adapter.projection.retrieve(
        retrieve_command(identity="after-tombstone", digest="c" * 64)
    )

    assert first.outcome is ProjectionOutcome.SUCCEEDED
    assert replay.outcome is ProjectionOutcome.REPLAYED
    assert retrieval.outcome is ProjectionOutcome.TOMBSTONED
    assert retrieval.chunks == ()
    assert retrieval.failure is not None
    assert retrieval.failure.code is ProjectionFailureCode.TOMBSTONED_SOURCE
