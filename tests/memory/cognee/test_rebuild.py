from __future__ import annotations

from uuid import UUID

import pytest

from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionEffect,
    ProjectionEffectAction,
    ProjectionFailureCode,
    ProjectionEffectStatus,
    ProjectionNamespaceState,
    ProjectionOperation,
    ProjectionSourceRevision,
)
from spine.memory.cognee.rebuild import (
    InMemoryProjectionRebuildProgressStore,
    ProjectionReplayItem,
    ShadowRebuildOrchestrator,
    ShadowRebuildPlan,
)
from spine.memory.cognee.graph import GraphProjectionResult, GraphProjectionStatus
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter
from tests.memory.cognee.test_graph import extraction_result, request as graph_request


WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
RUN = UUID("30000000-0000-4000-8000-000000000001")
CANDIDATE = UUID("40000000-0000-4000-8000-000000000001")
PARTITION = UUID("50000000-0000-4000-8000-000000000001")
CONFIG = UUID("60000000-0000-4000-8000-000000000001")
BOUNDARY = UUID("70000000-0000-4000-8000-000000000001")


class RecordingChunkLane:
    def __init__(self, *, fail_once: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once or set()
        self.failed: set[str] = set()

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        self.calls.append(effect.effect_id)
        if effect.effect_id in self.fail_once and effect.effect_id not in self.failed:
            self.failed.add(effect.effect_id)
            raise RuntimeError("provider payload must not cross the lane boundary")
        return effect.model_copy(update={"status": ProjectionEffectStatus.APPLIED})


class TerminalGraphLane:
    def __init__(self) -> None:
        self.calls = 0

    async def write(self, request):
        self.calls += 1
        return GraphProjectionResult(
            operation=request.operation,
            status=GraphProjectionStatus.FAILED,
            command_identity=request.command_identity,
            command_digest=request.command_digest,
            failure_code="configuration_mismatch",
        )


class TerminalChunkLane:
    def __init__(self) -> None:
        self.calls = 0

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        self.calls += 1
        return effect.model_copy(
            update={
                "status": ProjectionEffectStatus.FAILED,
                "failure_code": ProjectionFailureCode.REVISION_FENCED,
            }
        )


def effect(*, identity: str, source_object_id: int, revision_number: int) -> ProjectionEffect:
    source_object = UUID(int=source_object_id)
    revision = UUID(int=source_object_id + 100)
    candidate = ProjectionCandidate(candidate_id=CANDIDATE, run_id=RUN, partition_id=PARTITION)
    config = ProjectionConfigVersion(
        config_id=CONFIG,
        version="config-1",
        config_digest="b" * 64,
        ontology_version="ontology-1",
        adapter_contract_version="projection-rebuild:v1",
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY,
        boundary_digest="c" * 64,
        accepted_through_generation=1,
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_candidate=candidate,
            projection_config=config,
            canonical_boundary=boundary,
            state=ProjectionNamespaceState.SHADOW,
        )
    )
    return ProjectionEffect(
        effect_id=identity,
        operation=ProjectionOperation.REBUILD,
        action=ProjectionEffectAction.UPSERT_SOURCE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity=identity,
        command_digest="a" * 64,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        physical_namespace=namespace,
        source_object_id=source_object,
        source_revision_id=revision,
        revision_digest="d" * 64,
        revision_number=revision_number,
    )


def plan(*items: ProjectionReplayItem) -> ShadowRebuildPlan:
    first = items[0].effect
    assert first is not None
    return ShadowRebuildPlan(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_run_id=RUN,
        projection_candidate=first.projection_candidate,
        projection_config=first.projection_config,
        canonical_boundary=first.canonical_boundary,
        physical_namespace=first.physical_namespace,
        rebuild_identity="rebuild-1",
        rebuild_digest="e" * 64,
        items=items,
    )


@pytest.mark.asyncio
async def test_shadow_rebuild_replays_inputs_in_stable_order_and_is_idempotent() -> None:
    lane = RecordingChunkLane()
    orchestrator = ShadowRebuildOrchestrator(chunk_lane=lane)
    rebuild = plan(
        ProjectionReplayItem.from_effect(
            effect(identity="z-item", source_object_id=12, revision_number=1)
        ),
        ProjectionReplayItem.from_effect(
            effect(identity="a-item", source_object_id=11, revision_number=1)
        ),
    )

    first = await orchestrator.rebuild(rebuild)
    replay = await orchestrator.rebuild(rebuild)

    assert lane.calls == ["a-item", "z-item"]
    assert first.state.value == "complete"
    assert replay.replayed is True
    assert replay.pending_item_ids == ()


@pytest.mark.asyncio
async def test_shadow_rebuild_resumes_only_pending_inputs_after_physical_failure() -> None:
    lane = RecordingChunkLane(fail_once={"z-item"})
    orchestrator = ShadowRebuildOrchestrator(chunk_lane=lane)
    rebuild = plan(
        ProjectionReplayItem.from_effect(
            effect(identity="z-item", source_object_id=12, revision_number=1)
        ),
        ProjectionReplayItem.from_effect(
            effect(identity="a-item", source_object_id=11, revision_number=1)
        ),
    )

    first = await orchestrator.rebuild(rebuild)
    recovered = await orchestrator.recover(rebuild)

    assert first.state.value == "recoverable"
    assert first.failure_code.value == "physical_failure"
    assert first.completed_item_ids == (rebuild.items[1].replay_id,)
    assert recovered.state.value == "complete"
    assert recovered.attempt == 2
    assert lane.calls == ["a-item", "z-item", "z-item"]


@pytest.mark.asyncio
async def test_abandoned_shadow_rebuild_is_recoverable_without_becoming_readable() -> None:
    progress = InMemoryProjectionRebuildProgressStore()
    lane = RecordingChunkLane()
    orchestrator = ShadowRebuildOrchestrator(
        chunk_lane=lane,
        progress_store=progress,
    )
    rebuild = plan(
        ProjectionReplayItem.from_effect(
            effect(identity="a-item", source_object_id=11, revision_number=1)
        ),
    )

    abandoned = await orchestrator.abandon(rebuild)
    recovered = await orchestrator.recover_abandoned(rebuild)

    assert abandoned.state.value == "abandoned"
    assert recovered.state.value == "complete"
    assert recovered.namespace_digest == rebuild.physical_namespace.namespace_digest
    assert rebuild.physical_namespace.state is ProjectionNamespaceState.SHADOW
    assert lane.calls == ["a-item"]


@pytest.mark.asyncio
async def test_terminal_graph_failure_quarantines_candidate_without_retrying_it() -> None:
    extracted = extraction_result(include_relation=False)
    request = graph_request(extracted, operation=ProjectionOperation.REBUILD)
    shadow_namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            projection_candidate=request.projection_candidate,
            projection_config=request.projection_config,
            canonical_boundary=request.canonical_boundary,
            state=ProjectionNamespaceState.SHADOW,
        )
    )
    request = request.model_copy(update={"physical_namespace": shadow_namespace})
    rebuild = ShadowRebuildPlan(
        workspace_id=request.workspace_id,
        environment_id=request.environment_id,
        projection_run_id=request.projection_run_id,
        projection_candidate=request.projection_candidate,
        projection_config=request.projection_config,
        canonical_boundary=request.canonical_boundary,
        physical_namespace=shadow_namespace,
        rebuild_identity="rebuild-graph",
        rebuild_digest="f" * 64,
        items=(ProjectionReplayItem.from_graph_request(request),),
    )
    lane = TerminalGraphLane()
    orchestrator = ShadowRebuildOrchestrator(graph_lane=lane)

    failed = await orchestrator.rebuild(rebuild)
    replay = await orchestrator.rebuild(rebuild)

    assert failed.state.value == "failed"
    assert failed.failure_code.value == "configuration_mismatch"
    assert replay.state.value == "failed"
    assert lane.calls == 1
    assert failed.pending_item_ids == (rebuild.items[0].replay_id,)
    assert "configuration_mismatch" in failed.model_dump_json()


@pytest.mark.asyncio
async def test_terminal_chunk_failure_is_not_retried() -> None:
    lane = TerminalChunkLane()
    orchestrator = ShadowRebuildOrchestrator(chunk_lane=lane)
    rebuild = plan(
        ProjectionReplayItem.from_effect(
            effect(identity="fenced", source_object_id=11, revision_number=1)
        ),
    )

    failed = await orchestrator.rebuild(rebuild)
    replay = await orchestrator.rebuild(rebuild)

    assert failed.state.value == "failed"
    assert failed.failure_code.value == "revision_fenced"
    assert replay.state.value == "failed"
    assert lane.calls == 1
