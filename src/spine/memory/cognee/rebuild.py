"""Shadow rebuild orchestration for the Cognee projection lanes.

The orchestrator owns ordering, progress and recovery only.  Chunk and graph
writers remain physical adapters; publication and the active pointer remain
outside this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionEffect,
    ProjectionEffectStatus,
    ProjectionNamespace,
    ProjectionNamespaceState,
    ProjectionOperation,
)
from spine.memory.cognee.extraction import GraphProjectionRequest
from spine.memory.cognee.graph import GraphProjectionResult, GraphProjectionStatus
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter


class ProjectionReplayLane(str, Enum):
    CHUNKS = "chunks"
    GRAPH = "graph"


class ProjectionReplayStatus(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    RECOVERABLE = "recoverable"
    FAILED = "failed"


class ShadowRebuildState(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    RECOVERABLE = "recoverable"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ShadowRebuildFailureCode(str, Enum):
    PHYSICAL_FAILURE = "physical_failure"
    INTEGRITY_CONFLICT = "integrity_conflict"
    NAMESPACE_UNAVAILABLE = "namespace_unavailable"
    REVISION_FENCED = "revision_fenced"
    TOMBSTONED_SOURCE = "tombstoned_source"
    CONFIGURATION_MISMATCH = "configuration_mismatch"
    EXTRACTION_REJECTED = "extraction_rejected"
    UNKNOWN = "unknown"


def _non_zero(value: UUID, name: str) -> None:
    if value.int == 0:
        raise ValueError(f"{name} must be a non-zero UUID")


class ProjectionReplayItem(DefinitionModel):
    """One deterministic source-scoped replay input for a physical lane."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-replay-item:v1"] = "projection-replay-item:v1"
    lane: ProjectionReplayLane
    effect: ProjectionEffect | None = None
    graph_request: GraphProjectionRequest | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "ProjectionReplayItem":
        if self.lane is ProjectionReplayLane.CHUNKS:
            if self.effect is None or self.graph_request is not None:
                raise ValueError("chunk replay item must contain only a physical effect")
        elif self.graph_request is None or self.effect is not None:
            raise ValueError("graph replay item must contain only a graph request")
        return self

    @classmethod
    def from_effect(cls, effect: ProjectionEffect) -> "ProjectionReplayItem":
        return cls(lane=ProjectionReplayLane.CHUNKS, effect=effect)

    @classmethod
    def from_graph_request(cls, request: GraphProjectionRequest) -> "ProjectionReplayItem":
        return cls(lane=ProjectionReplayLane.GRAPH, graph_request=request)

    @property
    def replay_id(self) -> str:
        """Stable, content-free identity for progress and retry bookkeeping."""

        if self.effect is not None:
            source = (
                str(self.effect.source_object_id),
                str(self.effect.source_revision_id),
                self.effect.revision_number,
            )
            identity = (self.effect.command_identity, self.effect.command_digest)
        else:
            assert self.graph_request is not None
            sources = sorted(
                (
                    str(source.source_object_id),
                    str(source.source_revision_id),
                    source.revision_number,
                )
                for source in self.graph_request.result.source_revisions
            )
            source = tuple(sources)
            identity = (
                self.graph_request.command_identity,
                self.graph_request.command_digest,
            )
        payload = {"lane": self.lane.value, "identity": identity, "source": source}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def sort_key(self) -> tuple[object, ...]:
        if self.effect is not None:
            sources = (
                (self.effect.source_object_id.hex, self.effect.revision_number),
            )
            identity = self.effect.command_identity
        else:
            assert self.graph_request is not None
            sources = tuple(
                sorted(
                    (
                        source.source_object_id.hex,
                        source.revision_number,
                    )
                    for source in self.graph_request.result.source_revisions
                )
            )
            identity = self.graph_request.command_identity
        # Chunk work precedes graph work for the same source.  The final replay
        # identity keeps ordering deterministic even when a caller supplies
        # inputs in a different order.
        return (
            sources,
            0 if self.lane is ProjectionReplayLane.CHUNKS else 1,
            identity,
            self.replay_id,
        )


class ShadowRebuildPlan(DefinitionModel):
    """Immutable shadow candidate and its complete physical replay plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["shadow-rebuild-plan:v1"] = "shadow-rebuild-plan:v1"
    workspace_id: UUID
    environment_id: UUID
    projection_run_id: UUID
    projection_candidate: ProjectionCandidate
    projection_config: ProjectionConfigVersion
    canonical_boundary: CanonicalBoundary
    physical_namespace: ProjectionNamespace
    rebuild_identity: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    rebuild_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[ProjectionReplayItem, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> "ShadowRebuildPlan":
        for name in ("workspace_id", "environment_id", "projection_run_id"):
            _non_zero(getattr(self, name), name)
        if self.projection_candidate.run_id != self.projection_run_id:
            raise ValueError("projection candidate must belong to the rebuild run")
        if self.physical_namespace.state not in {
            ProjectionNamespaceState.SHADOW,
            ProjectionNamespaceState.REBUILDING,
        }:
            raise ValueError("a rebuild must target a non-active shadow namespace")
        context = ProjectionNamespaceContext(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            projection_kind=self.physical_namespace.projection_kind,
            projection_candidate=self.projection_candidate,
            projection_config=self.projection_config,
            canonical_boundary=self.canonical_boundary,
            state=self.physical_namespace.state,
        )
        ProjectionNamespaceRouter().validate(self.physical_namespace, context)
        replay_ids: set[str] = set()
        for item in self.items:
            if item.replay_id in replay_ids:
                raise ValueError("rebuild replay inputs must have unique identities")
            replay_ids.add(item.replay_id)
            if item.effect is not None:
                effect = item.effect
                if (
                    effect.workspace_id != self.workspace_id
                    or effect.environment_id != self.environment_id
                    or effect.projection_candidate != self.projection_candidate
                    or effect.projection_config != self.projection_config
                    or effect.canonical_boundary != self.canonical_boundary
                    or effect.physical_namespace != self.physical_namespace
                ):
                    raise ValueError("chunk replay effect is outside the rebuild scope")
            else:
                assert item.graph_request is not None
                request = item.graph_request
                if (
                    request.workspace_id != self.workspace_id
                    or request.environment_id != self.environment_id
                    or request.projection_run_id != self.projection_run_id
                    or request.projection_candidate != self.projection_candidate
                    or request.projection_config != self.projection_config
                    or request.canonical_boundary != self.canonical_boundary
                    or request.physical_namespace != self.physical_namespace
                ):
                    raise ValueError("graph replay request is outside the rebuild scope")
        return self

    @property
    def ordered_items(self) -> tuple[ProjectionReplayItem, ...]:
        return tuple(sorted(self.items, key=lambda item: item.sort_key))

    @property
    def plan_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"rebuild_digest"})
        payload["items"] = [item.replay_id for item in self.ordered_items]
        # Namespace lifecycle is mutable metadata, not candidate identity.
        payload["physical_namespace"].pop("state", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class ProjectionReplayRecord(DefinitionModel):
    """Content-free outcome for one replay input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-replay-record:v1"] = "projection-replay-record:v1"
    replay_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: ProjectionReplayLane
    status: ProjectionReplayStatus
    attempt: int = Field(ge=1)
    failure_code: ShadowRebuildFailureCode | None = None


class ProjectionRebuildProgress(DefinitionModel):
    """Durable-friendly checkpoint for resumable physical work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-rebuild-progress:v1"] = "projection-rebuild-progress:v1"
    workspace_id: UUID
    environment_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    namespace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ShadowRebuildState
    attempt: int = Field(ge=1)
    completed: tuple[ProjectionReplayRecord, ...] = ()
    pending_item_ids: tuple[str, ...] = ()
    failed_item_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_code: ShadowRebuildFailureCode | None = None


class ProjectionRebuildProgressStore(Protocol):
    async def load(self, plan: ShadowRebuildPlan) -> ProjectionRebuildProgress | None: ...

    async def save(self, progress: ProjectionRebuildProgress) -> None: ...


class InMemoryProjectionRebuildProgressStore:
    """Deterministic fake for the durable progress port."""

    def __init__(self) -> None:
        self._records: dict[tuple[UUID, UUID, UUID, UUID], ProjectionRebuildProgress] = {}

    async def load(self, plan: ShadowRebuildPlan) -> ProjectionRebuildProgress | None:
        return self._records.get(self._key(plan))

    async def save(self, progress: ProjectionRebuildProgress) -> None:
        self._records[
            (
                progress.workspace_id,
                progress.environment_id,
                progress.projection_run_id,
                progress.projection_candidate_id,
            )
        ] = progress

    @staticmethod
    def _key(plan: ShadowRebuildPlan) -> tuple[UUID, UUID, UUID, UUID]:
        return (
            plan.workspace_id,
            plan.environment_id,
            plan.projection_run_id,
            plan.projection_candidate.candidate_id,
        )

    @property
    def records(self) -> tuple[ProjectionRebuildProgress, ...]:
        return tuple(self._records.values())


class ChunkProjectionLane(Protocol):
    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect: ...


class GraphProjectionLane(Protocol):
    async def write(self, request: GraphProjectionRequest) -> GraphProjectionResult: ...


class ShadowRebuildResult(DefinitionModel):
    """Safe orchestration outcome; no provider payload or source content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["shadow-rebuild-result:v1"] = "shadow-rebuild-result:v1"
    operation: Literal[ProjectionOperation.REBUILD] = ProjectionOperation.REBUILD
    projection_run_id: UUID
    projection_candidate_id: UUID
    namespace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ShadowRebuildState
    attempt: int = Field(ge=1)
    replayed: bool = False
    records: tuple[ProjectionReplayRecord, ...] = ()
    completed_item_ids: tuple[str, ...] = ()
    pending_item_ids: tuple[str, ...] = ()
    failure_code: ShadowRebuildFailureCode | None = None


_TERMINAL_FAILURES = {
    "integrity_conflict": ShadowRebuildFailureCode.INTEGRITY_CONFLICT,
    "namespace_unavailable": ShadowRebuildFailureCode.NAMESPACE_UNAVAILABLE,
    "revision_fenced": ShadowRebuildFailureCode.REVISION_FENCED,
    "tombstoned_source": ShadowRebuildFailureCode.TOMBSTONED_SOURCE,
    "configuration_mismatch": ShadowRebuildFailureCode.CONFIGURATION_MISMATCH,
    "extraction_rejected": ShadowRebuildFailureCode.EXTRACTION_REJECTED,
}


class ShadowRebuildOrchestrator:
    """Build and resume an isolated candidate across chunk and graph lanes."""

    def __init__(
        self,
        *,
        chunk_lane: ChunkProjectionLane | None = None,
        graph_lane: GraphProjectionLane | None = None,
        progress_store: ProjectionRebuildProgressStore | None = None,
    ) -> None:
        if chunk_lane is None and graph_lane is None:
            raise ValueError("at least one projection lane is required")
        self._chunk_lane = chunk_lane
        self._graph_lane = graph_lane
        self._progress = progress_store or InMemoryProjectionRebuildProgressStore()
        self._lock = asyncio.Lock()

    async def rebuild(self, plan: ShadowRebuildPlan) -> ShadowRebuildResult:
        """Run or resume a rebuild; retry only pending physical work."""

        async with self._lock:
            return await self._run(plan)

    async def recover(self, plan: ShadowRebuildPlan) -> ShadowRebuildResult:
        """Recover an abandoned or recoverable run using its saved checkpoint."""

        return await self.rebuild(plan)

    async def recover_abandoned(self, plan: ShadowRebuildPlan) -> ShadowRebuildResult:
        """Explicitly named recovery entry point for workflow adapters."""

        return await self.recover(plan)

    async def abandon(self, plan: ShadowRebuildPlan) -> ProjectionRebuildProgress:
        """Mark a checkpoint abandoned without making its candidate readable."""

        async with self._lock:
            progress = await self._progress.load(plan)
            if progress is None:
                pending = tuple(item.replay_id for item in plan.ordered_items)
                progress = ProjectionRebuildProgress(
                    workspace_id=plan.workspace_id,
                    environment_id=plan.environment_id,
                    projection_run_id=plan.projection_run_id,
                    projection_candidate_id=plan.projection_candidate.candidate_id,
                    namespace_digest=plan.physical_namespace.namespace_digest,
                    plan_digest=plan.plan_digest,
                    state=ShadowRebuildState.ABANDONED,
                    attempt=1,
                    pending_item_ids=pending,
                )
            else:
                progress = progress.model_copy(update={"state": ShadowRebuildState.ABANDONED})
            await self._progress.save(progress)
            return progress

    async def _run(self, plan: ShadowRebuildPlan) -> ShadowRebuildResult:
        previous = await self._progress.load(plan)
        if previous is not None and previous.plan_digest != plan.plan_digest:
            return self._result(
                plan,
                state=ShadowRebuildState.FAILED,
                attempt=previous.attempt,
                failure_code=ShadowRebuildFailureCode.INTEGRITY_CONFLICT,
                pending=previous.pending_item_ids,
            )
        if previous is not None and previous.state is ShadowRebuildState.COMPLETE:
            return self._result(
                plan,
                state=ShadowRebuildState.COMPLETE,
                attempt=previous.attempt,
                records=tuple(
                    record.model_copy(update={"status": ProjectionReplayStatus.REPLAYED})
                    for record in previous.completed
                ),
                replayed=True,
            )
        if previous is not None and previous.state is ShadowRebuildState.FAILED:
            return self._result(
                plan,
                state=previous.state,
                attempt=previous.attempt,
                records=previous.completed,
                pending=previous.pending_item_ids,
                failure_code=previous.failure_code,
            )

        attempt = 1 if previous is None else previous.attempt + 1
        previous_completed = () if previous is None else previous.completed
        completed = {record.replay_id: record for record in previous_completed}
        ordered = plan.ordered_items
        pending = tuple(item for item in ordered if item.replay_id not in completed)
        running = ProjectionRebuildProgress(
            workspace_id=plan.workspace_id,
            environment_id=plan.environment_id,
            projection_run_id=plan.projection_run_id,
            projection_candidate_id=plan.projection_candidate.candidate_id,
            namespace_digest=plan.physical_namespace.namespace_digest,
            plan_digest=plan.plan_digest,
            state=ShadowRebuildState.RUNNING,
            attempt=attempt,
            completed=tuple(completed.values()),
            pending_item_ids=tuple(item.replay_id for item in pending),
        )
        await self._progress.save(running)

        for index, item in enumerate(pending):
            try:
                record = await self._apply(item, attempt)
            except Exception:
                record = ProjectionReplayRecord(
                    replay_id=item.replay_id,
                    lane=item.lane,
                    status=ProjectionReplayStatus.RECOVERABLE,
                    attempt=attempt,
                    failure_code=ShadowRebuildFailureCode.PHYSICAL_FAILURE,
                )
            if record.status in {
                ProjectionReplayStatus.RECOVERABLE,
                ProjectionReplayStatus.FAILED,
            }:
                failure_state = (
                    ShadowRebuildState.RECOVERABLE
                    if record.status is ProjectionReplayStatus.RECOVERABLE
                    else ShadowRebuildState.FAILED
                )
                failure_progress = running.model_copy(
                    update={
                        "state": failure_state,
                        "completed": tuple(completed.values()),
                        "pending_item_ids": tuple(
                            replay.replay_id for replay in pending[index:]
                        ),
                        "failed_item_id": item.replay_id,
                        "failure_code": record.failure_code,
                    }
                )
                await self._progress.save(failure_progress)
                return self._result(
                    plan,
                    state=failure_state,
                    attempt=attempt,
                    records=tuple(completed.values()) + (record,),
                    pending=failure_progress.pending_item_ids,
                    failure_code=record.failure_code,
                )
            completed[item.replay_id] = record
            checkpoint = running.model_copy(
                update={
                    "completed": tuple(completed.values()),
                    "pending_item_ids": tuple(
                        replay.replay_id for replay in pending[index + 1 :]
                    ),
                    "failed_item_id": None,
                    "failure_code": None,
                }
            )
            await self._progress.save(checkpoint)

        complete = running.model_copy(
            update={
                "state": ShadowRebuildState.COMPLETE,
                "completed": tuple(completed.values()),
                "pending_item_ids": (),
            }
        )
        await self._progress.save(complete)
        return self._result(
            plan,
            state=ShadowRebuildState.COMPLETE,
            attempt=attempt,
            records=complete.completed,
        )

    async def _apply(self, item: ProjectionReplayItem, attempt: int) -> ProjectionReplayRecord:
        if item.effect is not None:
            if self._chunk_lane is None:
                raise RuntimeError("chunk lane is unavailable")
            result = await self._chunk_lane.apply(item.effect)
            status = (
                ProjectionReplayStatus.APPLIED
                if result.status is ProjectionEffectStatus.APPLIED
                else ProjectionReplayStatus.REPLAYED
                if result.status is ProjectionEffectStatus.REPLAYED
                else ProjectionReplayStatus.RECOVERABLE
            )
            failure_code = _TERMINAL_FAILURES.get(
                result.failure_code.value if result.failure_code is not None else "",
            )
            if result.status is ProjectionEffectStatus.FAILED and failure_code is not None:
                status = ProjectionReplayStatus.FAILED
            return ProjectionReplayRecord(
                replay_id=item.replay_id,
                lane=item.lane,
                status=status,
                attempt=attempt,
                failure_code=(
                    failure_code
                    if failure_code is not None
                    else ShadowRebuildFailureCode.PHYSICAL_FAILURE
                    if status is ProjectionReplayStatus.RECOVERABLE
                    else None
                ),
            )

        if self._graph_lane is None or item.graph_request is None:
            raise RuntimeError("graph lane is unavailable")
        result = await self._graph_lane.write(item.graph_request)
        if result.status is GraphProjectionStatus.APPLIED:
            status = ProjectionReplayStatus.APPLIED
            failure_code = None
        elif result.status is GraphProjectionStatus.REPLAYED:
            status = ProjectionReplayStatus.REPLAYED
            failure_code = None
        elif result.status is GraphProjectionStatus.RECOVERABLE:
            status = ProjectionReplayStatus.RECOVERABLE
            failure_code = ShadowRebuildFailureCode.PHYSICAL_FAILURE
        else:
            status = ProjectionReplayStatus.FAILED
            failure_code = _TERMINAL_FAILURES.get(
                result.failure_code or "", ShadowRebuildFailureCode.UNKNOWN
            )
        return ProjectionReplayRecord(
            replay_id=item.replay_id,
            lane=item.lane,
            status=status,
            attempt=attempt,
            failure_code=failure_code,
        )

    @staticmethod
    def _result(
        plan: ShadowRebuildPlan,
        *,
        state: ShadowRebuildState,
        attempt: int,
        records: tuple[ProjectionReplayRecord, ...] = (),
        pending: tuple[str, ...] = (),
        failure_code: ShadowRebuildFailureCode | None = None,
        replayed: bool = False,
    ) -> ShadowRebuildResult:
        completed = tuple(
            record.replay_id
            for record in records
            if record.status
            in {ProjectionReplayStatus.APPLIED, ProjectionReplayStatus.REPLAYED}
        )
        return ShadowRebuildResult(
            projection_run_id=plan.projection_run_id,
            projection_candidate_id=plan.projection_candidate.candidate_id,
            namespace_digest=plan.physical_namespace.namespace_digest,
            state=state,
            attempt=attempt,
            replayed=replayed,
            records=records,
            completed_item_ids=completed,
            pending_item_ids=pending,
            failure_code=failure_code,
        )


__all__ = [
    "ChunkProjectionLane",
    "GraphProjectionLane",
    "InMemoryProjectionRebuildProgressStore",
    "ProjectionRebuildProgress",
    "ProjectionRebuildProgressStore",
    "ProjectionReplayItem",
    "ProjectionReplayLane",
    "ProjectionReplayRecord",
    "ProjectionReplayStatus",
    "ShadowRebuildFailureCode",
    "ShadowRebuildOrchestrator",
    "ShadowRebuildPlan",
    "ShadowRebuildResult",
    "ShadowRebuildState",
]
