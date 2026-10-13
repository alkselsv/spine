from __future__ import annotations

from spine.memory.contracts import (
    ProjectionFailureCode,
    ProjectionNamespaceState,
    ProjectionOutcome,
    RebuildCommand,
)

from .adapter import ContextProjectionFactory
from .test_conformance import (
    SHADOW_CANDIDATE_ID,
    WORKSPACE_ID,
    ENVIRONMENT_ID,
    command_context,
    evidence,
    retrieve_command,
)
from spine.memory.contracts import ProjectionChunk


async def test_rebuild_replay_is_stable_after_a_lost_response(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    projection = context_projection_factory().projection
    command = RebuildCommand(
        **command_context(),
        command_identity="rebuild-retry",
        command_digest="6" * 64,
    )

    first = await projection.rebuild(command)
    replay = await projection.rebuild(command)

    assert first.outcome is ProjectionOutcome.SUCCEEDED
    assert replay.outcome is ProjectionOutcome.REPLAYED
    assert replay.artifact_reference == first.artifact_reference


async def test_shadow_rebuild_artifacts_require_explicit_namespace_selection(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    shadow_namespace = command_context(
        candidate_id=SHADOW_CANDIDATE_ID,
        state=ProjectionNamespaceState.SHADOW,
    )["physical_namespace"]
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        physical_namespace=shadow_namespace,
        chunks=(
            ProjectionChunk(
                chunk_id="candidate-only",
                text="shadow candidate",
                evidence=(evidence(),),
            ),
        ),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(
            identity="ordinary-after-rebuild",
            digest="7" * 64,
            namespace=shadow_namespace,
            candidate_id=SHADOW_CANDIDATE_ID,
            state=ProjectionNamespaceState.SHADOW,
        )
    )

    assert result.outcome is ProjectionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
