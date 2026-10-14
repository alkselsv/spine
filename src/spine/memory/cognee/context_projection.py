"""Complete ``ContextProjection`` composition for Cognee physical lanes.

The module does not own canonical lifecycle or publication.  It composes the
provider-neutral command coordinator with Cognee-backed mutation lanes and the
structured retrieval adapter supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from spine.domain.artifacts import ParsedDocument
from spine.memory.cognee.extraction import (
    ExtractionResult,
    GraphProjectionRequest,
    ProjectionGraphWriter,
)
from spine.memory.cognee.graph import CogneeGraphProjectionWriter, GraphProjectionStatus
from spine.memory.cognee.indexing.store import CogneeChunkProjectionStore, CogneeSdk
from spine.memory.cognee.retrieval.adapter import (
    CogneeStructuredRetrieval,
    CogneeStructuredRetrievalSdk,
)
from spine.memory.contracts import (
    ContextProjection,
    ProjectionEffect,
    ProjectionEffectStatus,
    ProjectionEffectStore,
    ProjectionFailureCode,
    ProjectionOperation,
    ProjectionResult,
    ProjectionRetrievalResult,
    ProjectionSourceRevision,
    ProjectCommand,
    RebuildCommand,
    RemoveCommand,
    RetrieveCommand,
    UpdateCommand,
)
from spine.memory.coordinator import ProjectionCoordinator


class StructuredRetrieval(Protocol):
    """Read lane required by the complete projection adapter."""

    async def retrieve(self, command: RetrieveCommand) -> ProjectionRetrievalResult: ...


class CogneeGraphEffectStore(ProjectionEffectStore):
    """Translate one source-scoped effect into the graph writer contract."""

    def __init__(
        self,
        *,
        writer: ProjectionGraphWriter,
        extraction_results: Mapping[UUID, ExtractionResult],
    ) -> None:
        self._writer = writer
        self._results = extraction_results

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        result = self._results.get(effect.source_revision_id)
        if result is None or effect.command_digest is None:
            return effect.model_copy(
                update={
                    "status": ProjectionEffectStatus.FAILED,
                    "failure_code": ProjectionFailureCode.PHYSICAL_FAILURE,
                }
            )
        response = await self._writer.write(
            GraphProjectionRequest(
                operation=effect.operation,
                workspace_id=effect.workspace_id,
                environment_id=effect.environment_id,
                projection_run_id=effect.projection_candidate.run_id,
                command_identity=effect.command_identity,
                command_digest=effect.command_digest,
                projection_candidate=effect.projection_candidate,
                projection_config=effect.projection_config,
                canonical_boundary=effect.canonical_boundary,
                physical_namespace=effect.physical_namespace,
                source_revision=ProjectionSourceRevision(
                    source_object_id=effect.source_object_id,
                    source_revision_id=effect.source_revision_id,
                    revision_digest=effect.revision_digest,
                    revision_number=effect.revision_number,
                    status=(
                        "tombstoned"
                        if effect.operation is ProjectionOperation.REMOVE
                        else "current"
                    ),
                    processing_receipt_id=effect.processing_receipt_id,
                    locator_contract_version=effect.locator_contract_version,
                ),
                result=result,
            )
        )
        if response.status in {
            GraphProjectionStatus.APPLIED,
            GraphProjectionStatus.REPLAYED,
        }:
            return effect.model_copy(
                update={
                    "status": (
                        ProjectionEffectStatus.REPLAYED
                        if response.status is GraphProjectionStatus.REPLAYED
                        else ProjectionEffectStatus.APPLIED
                    ),
                    "failure_code": None,
                }
            )
        code = {
            "integrity_conflict": ProjectionFailureCode.INTEGRITY_CONFLICT,
            "namespace_unavailable": ProjectionFailureCode.NAMESPACE_UNAVAILABLE,
            "revision_fenced": ProjectionFailureCode.REVISION_FENCED,
            "tombstoned_source": ProjectionFailureCode.TOMBSTONED_SOURCE,
        }.get(response.failure_code or "", ProjectionFailureCode.PHYSICAL_FAILURE)
        return effect.model_copy(
            update={"status": ProjectionEffectStatus.FAILED, "failure_code": code}
        )


class _PhysicalLanes:
    """Apply an effect to all lanes while retaining successful checkpoints."""

    def __init__(
        self,
        chunks: ProjectionEffectStore,
        graph: ProjectionEffectStore | None,
    ) -> None:
        self._lanes = (chunks,) if graph is None else (chunks, graph)
        self._completed: dict[str, set[int]] = {}

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        completed = self._completed.setdefault(effect.effect_id, set())
        for lane_index, lane in enumerate(self._lanes):
            if lane_index in completed:
                continue
            result = await lane.apply(effect)
            if result.status not in {
                ProjectionEffectStatus.APPLIED,
                ProjectionEffectStatus.REPLAYED,
            }:
                return effect.model_copy(
                    update={
                        "status": result.status,
                        "failure_code": result.failure_code,
                    }
                )
            completed.add(lane_index)
        return effect.model_copy(
            update={"status": ProjectionEffectStatus.APPLIED, "failure_code": None}
        )


class CogneeContextProjection(ContextProjection):
    """Expose the complete port over physical Cognee read and write lanes.

    Command identity, revision fencing and retry checkpoints are coordinated
    above the provider adapters.  Canonical source/candidate/snapshot state is
    still supplied on each command and remains outside Cognee.
    """

    def __init__(
        self,
        *,
        effect_store: ProjectionEffectStore,
        retrieval: StructuredRetrieval,
        graph_effect_store: ProjectionEffectStore | None = None,
    ) -> None:
        self._writes = ProjectionCoordinator(
            physical_store=_PhysicalLanes(effect_store, graph_effect_store)
        )
        self._retrieval = retrieval

    async def project(self, command: ProjectCommand) -> ProjectionResult:
        return await self._writes.project(command)

    async def update(self, command: UpdateCommand) -> ProjectionResult:
        return await self._writes.update(command)

    async def remove(self, command: RemoveCommand) -> ProjectionResult:
        return await self._writes.remove(command)

    async def rebuild(self, command: RebuildCommand) -> ProjectionResult:
        return await self._writes.rebuild(command)

    async def retrieve(self, command: RetrieveCommand) -> ProjectionRetrievalResult:
        return await self._retrieval.retrieve(command)


def create_cognee_context_projection(
    *,
    artifacts: Mapping[UUID, ParsedDocument],
    extraction_results: Mapping[UUID, ExtractionResult],
    sdk: CogneeSdk | None = None,
    retrieval_sdk: CogneeStructuredRetrievalSdk | None = None,
) -> CogneeContextProjection:
    """Build the complete adapter from the qualified public Cognee seams."""

    chunks = CogneeChunkProjectionStore(sdk=sdk, artifacts=artifacts)
    graph_writer = CogneeGraphProjectionWriter(sdk=sdk)
    graph = CogneeGraphEffectStore(
        writer=graph_writer,
        extraction_results=extraction_results,
    )
    retrieval = CogneeStructuredRetrieval(sdk=retrieval_sdk)
    return CogneeContextProjection(
        effect_store=chunks,
        graph_effect_store=graph,
        retrieval=retrieval,
    )


__all__ = [
    "CogneeContextProjection",
    "CogneeGraphEffectStore",
    "StructuredRetrieval",
    "create_cognee_context_projection",
]
