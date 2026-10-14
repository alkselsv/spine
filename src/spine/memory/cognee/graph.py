"""Cognee 1.5.4 write-side adapter for ontology-approved graph artifacts.

The adapter accepts only the provider-neutral output of the extraction seam.
It stores a content-free physical manifest in memory for retry and fencing;
canonical source state, receipts and publication remain outside the adapter.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.memory.contracts import (
    ProjectionNamespaceState,
    ProjectionOperation,
)
from spine.memory.cognee.extraction import (
    AcceptedEntity,
    AcceptedRelation,
    ExtractionSourceRevision,
    ExtractionStatus,
    GraphProjectionRequest,
)
from spine.memory.cognee.indexing.store import (
    CogneePublicSdk,
    CogneeSdk,
    CogneeVersionError,
    SUPPORTED_COGNEE_VERSION,
)


_DIGEST_PATTERN = r"[0-9a-f]{64}"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.:-]{0,63}$"
from spine.memory.namespace import (
    NamespaceRoutingError,
    ProjectionNamespaceContext,
    ProjectionNamespaceRouter,
)
from spine.memory.cognee.validation import PhysicalArtifactLocator


class GraphProjectionStatus(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    RECOVERABLE = "recoverable"
    FAILED = "failed"
    FENCED = "fenced"


class GraphArtifactMapping(DefinitionModel):
    """Content-free mapping for one physical entity or relation artifact."""

    schema_version: Literal["graph-artifact-mapping:v2"] = "graph-artifact-mapping:v2"
    workspace_id: UUID
    environment_id: UUID
    namespace_digest: str = Field(pattern=_DIGEST_PATTERN)
    artifact_kind: Literal["entity", "relation"]
    artifact_id: str = Field(min_length=1, max_length=64)
    data_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=_DIGEST_PATTERN)
    revision_number: int = Field(ge=1)
    projection_config_id: UUID
    projection_config_version: str
    projection_config_digest: str = Field(pattern=_DIGEST_PATTERN)
    locators: tuple[PhysicalArtifactLocator, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()


class GraphProjectionResult(DefinitionModel):
    """Disclosure-safe result of one graph write command."""

    schema_version: Literal["graph-projection-result:v1"] = "graph-projection-result:v1"
    operation: ProjectionOperation
    status: GraphProjectionStatus
    command_identity: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    command_digest: str = Field(pattern=_DIGEST_PATTERN)
    mappings: tuple[GraphArtifactMapping, ...] = ()
    pending_artifact_ids: tuple[str, ...] = ()
    failure_code: str | None = Field(default=None, min_length=1, max_length=64)
    attempt: int = Field(default=1, ge=1)
    recovered: bool = False

    @model_validator(mode="after")
    def validate_status(self) -> "GraphProjectionResult":
        if self.status is GraphProjectionStatus.RECOVERABLE:
            if self.failure_code != "physical_failure" or not self.pending_artifact_ids:
                raise ValueError("recoverable graph result must name pending artifacts")
        elif self.status in {
            GraphProjectionStatus.APPLIED,
            GraphProjectionStatus.REPLAYED,
        } and self.failure_code is not None:
            raise ValueError("successful graph result cannot contain a failure")
        elif self.status is GraphProjectionStatus.FENCED and self.failure_code not in {
            "revision_fenced",
            "tombstoned_source",
        }:
            raise ValueError("fenced graph result requires a revision fence")
        return self


@dataclass
class _GraphRecord:
    revision_number: int
    source_revision_id: UUID
    revision_digest: str
    dataset_id: UUID | None
    mappings: tuple[GraphArtifactMapping, ...]
    tombstoned: bool = False


@dataclass
class _CommandState:
    digest: str
    operation: ProjectionOperation
    result: GraphProjectionResult | None = None


@dataclass
class _PendingRecord:
    record: _GraphRecord
    existing_mappings: tuple[GraphArtifactMapping, ...] = ()
    completed_update_keys: frozenset[tuple[str, str]] = frozenset()
    additions_applied: bool = False
    deleted_data_ids: frozenset[UUID] = frozenset()


class CogneeGraphProjectionWriter:
    """Write ontology-approved entities and relations to a routed Cognee dataset."""

    def __init__(
        self,
        *,
        sdk: CogneeSdk | None = None,
        router: ProjectionNamespaceRouter | None = None,
    ) -> None:
        self._sdk = sdk or CogneePublicSdk()
        if self._sdk.version != SUPPORTED_COGNEE_VERSION:
            raise CogneeVersionError(
                f"Cognee {SUPPORTED_COGNEE_VERSION} is required, got {self._sdk.version or 'unknown'}"
            )
        self._router = router or ProjectionNamespaceRouter()
        self._lock = asyncio.Lock()
        self._commands: dict[tuple[UUID, UUID, str, str], _CommandState] = {}
        self._records: dict[tuple[UUID, UUID, str, UUID], _GraphRecord] = {}
        self._pending_records: dict[tuple[UUID, UUID, str, UUID], _PendingRecord] = {}
        self._dataset_ids: dict[str, UUID] = {}

    async def write(self, request: GraphProjectionRequest) -> GraphProjectionResult:
        """Apply one graph command with safe replay, recovery and revision fencing."""

        async with self._lock:
            key = self._command_key(request)
            previous = self._commands.get(key)
            if previous is not None:
                if (
                    previous.digest != request.command_digest
                    or previous.operation is not request.operation
                ):
                    return self._result(request, GraphProjectionStatus.FAILED, "integrity_conflict")
                if previous.result is not None and previous.result.status in {
                    GraphProjectionStatus.APPLIED,
                    GraphProjectionStatus.REPLAYED,
                }:
                    return previous.result.model_copy(
                        update={
                            "status": GraphProjectionStatus.REPLAYED,
                            "mappings": tuple(
                                mapping.model_copy()
                                for mapping in previous.result.mappings
                            ),
                        }
                    )
                attempt = previous.result.attempt + 1 if previous.result is not None else 1
            else:
                self._commands[key] = _CommandState(
                    digest=request.command_digest,
                    operation=request.operation,
                )
                attempt = 1
            failure = self._validate_request(request)
            if failure is not None:
                result = self._result(
                    request,
                    GraphProjectionStatus.FENCED
                    if failure in {"revision_fenced", "tombstoned_source"}
                    else GraphProjectionStatus.FAILED,
                    failure,
                    attempt=attempt,
                )
                self._commands[key].result = result
                return result

            try:
                mappings: list[GraphArtifactMapping] = []
                for source in self._sources_for(request):
                    mappings.extend(await self._apply_source(request, source))
            except _GraphFence as error:
                result = self._result(
                    request,
                    GraphProjectionStatus.FENCED,
                    error.code,
                    attempt=attempt,
                )
                self._commands[key].result = result
                return result
            except Exception:
                pending = self._pending_artifacts(request) or ("graph-command",)
                pending_mappings = tuple(
                    item for item in pending if isinstance(item, GraphArtifactMapping)
                )
                result = self._result(
                    request,
                    GraphProjectionStatus.RECOVERABLE,
                    "physical_failure",
                    mappings=pending_mappings,
                    pending_artifact_ids=tuple(
                        item.artifact_id if isinstance(item, GraphArtifactMapping) else item
                        for item in pending
                    ),
                    attempt=attempt,
                )
                self._commands[key].result = result
                return result

            result = self._result(
                request,
                GraphProjectionStatus.APPLIED,
                mappings=tuple(
                    sorted(
                        mappings,
                        key=lambda item: (
                            item.source_object_id.int,
                            item.artifact_kind,
                            item.artifact_id,
                        ),
                    )
                ),
                attempt=attempt,
                recovered=attempt > 1,
            )
            self._commands[key].result = result
            return result

    async def mappings_for(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        namespace_digest: str,
    ) -> tuple[GraphArtifactMapping, ...]:
        """Return content-free graph mappings for receipts and diagnostics."""

        async with self._lock:
            return tuple(
                mapping
                for record in self._records.values()
                if not record.tombstoned
                for mapping in record.mappings
                if (
                    mapping.workspace_id == workspace_id
                    and mapping.environment_id == environment_id
                    and mapping.namespace_digest == namespace_digest
                )
            )

    async def _apply_source(
        self,
        request: GraphProjectionRequest,
        source: ExtractionSourceRevision,
    ) -> tuple[GraphArtifactMapping, ...]:
        key = self._record_key(request, source.source_object_id)
        record = self._records.get(key)
        pending = self._pending_records.get(key)

        if pending is not None:
            if (
                pending.record.revision_number != source.revision_number
                or pending.record.source_revision_id != source.source_revision_id
                or pending.record.revision_digest != source.revision_digest
            ):
                raise _GraphFence("revision_fenced")
            return await self._resume_pending(request, key, pending)

        if record is not None and self._is_fenced(source, record, request.operation):
            raise _GraphFence(
                "tombstoned_source"
                if record.tombstoned and request.operation is not ProjectionOperation.REMOVE
                else "revision_fenced"
            )

        if request.operation is ProjectionOperation.REMOVE:
            if source.status != "tombstoned":
                raise _GraphFence("tombstoned_source")
            tombstone = _GraphRecord(
                revision_number=source.revision_number,
                source_revision_id=source.source_revision_id,
                revision_digest=source.revision_digest,
                dataset_id=record.dataset_id if record is not None else None,
                mappings=(),
                tombstoned=True,
            )
            pending = _PendingRecord(
                record=tombstone,
                existing_mappings=record.mappings if record is not None else (),
            )
            self._pending_records[key] = pending
            return await self._resume_pending(request, key, pending)

        if source.status != "current":
            raise _GraphFence("revision_fenced")

        desired = self._desired_mappings(request, source)
        next_record = _GraphRecord(
            revision_number=source.revision_number,
            source_revision_id=source.source_revision_id,
            revision_digest=source.revision_digest,
            dataset_id=record.dataset_id if record is not None else None,
            mappings=tuple(desired),
        )
        pending = _PendingRecord(
            record=next_record,
            existing_mappings=record.mappings if record is not None else (),
        )
        self._pending_records[key] = pending
        return await self._resume_pending(request, key, pending)

    async def _resume_pending(
        self,
        request: GraphProjectionRequest,
        key: tuple[UUID, UUID, str, UUID],
        pending: _PendingRecord,
    ) -> tuple[GraphArtifactMapping, ...]:
        dataset_name = self.dataset_name(request)
        dataset_id = pending.record.dataset_id or self._dataset_ids.get(dataset_name)
        node_set = [request.physical_namespace.namespace_digest]

        if pending.record.tombstoned:
            for previous in pending.existing_mappings:
                if previous.data_id in pending.deleted_data_ids:
                    continue
                await self._sdk.delete_data(
                    dataset_id=dataset_id or self._dataset_id_required(dataset_name),
                    data_id=previous.data_id,
                    mode="soft",
                )
                pending.deleted_data_ids = pending.deleted_data_ids | {previous.data_id}
        else:
            desired_by_key = {
                self._artifact_key(item): item for item in pending.record.mappings
            }
            existing_by_key = {
                self._artifact_key(item): item for item in pending.existing_mappings
            }
            for artifact_key, mapping in desired_by_key.items():
                previous = existing_by_key.get(artifact_key)
                if previous is None or artifact_key in pending.completed_update_keys:
                    continue
                await self._sdk.update(
                    data_id=previous.data_id,
                    data=self._data_item(request, mapping),
                    dataset_id=dataset_id or self._dataset_id_required(dataset_name),
                    node_set=node_set,
                    chunk_level_diff=True,
                )
                pending.completed_update_keys = pending.completed_update_keys | {
                    artifact_key
                }

            additions = [
                mapping
                for artifact_key, mapping in desired_by_key.items()
                if artifact_key not in existing_by_key
            ]
            if additions and not pending.additions_applied:
                add_result = await self._sdk.add(
                    data=[self._data_item(request, mapping) for mapping in additions],
                    dataset_name=dataset_name,
                    dataset_id=dataset_id,
                    node_set=node_set,
                )
                dataset_id = await self._resolve_dataset_id(
                    dataset_name,
                    dataset_id or self._dataset_id_from_result(add_result),
                )
                pending.record.dataset_id = dataset_id
                pending.additions_applied = True

            for artifact_key, previous in existing_by_key.items():
                if artifact_key in desired_by_key or previous.data_id in pending.deleted_data_ids:
                    continue
                await self._sdk.delete_data(
                    dataset_id=dataset_id or self._dataset_id_required(dataset_name),
                    data_id=previous.data_id,
                    mode="soft",
                )
                pending.deleted_data_ids = pending.deleted_data_ids | {previous.data_id}

        await self._sdk.cognify(datasets=[dataset_name])
        self._pending_records.pop(key, None)
        self._records[key] = pending.record
        return pending.record.mappings

    def _desired_mappings(
        self,
        request: GraphProjectionRequest,
        source: ExtractionSourceRevision,
    ) -> tuple[GraphArtifactMapping, ...]:
        result = request.result
        entities = (
            entity for entity in result.entities if entity.source_revision_id == source.source_revision_id
        )
        relations = (
            relation for relation in result.relations if relation.source_revision_id == source.source_revision_id
        )
        mappings = [self._entity_mapping(request, source, entity) for entity in entities]
        mappings.extend(self._relation_mapping(request, source, relation) for relation in relations)
        return tuple(sorted(mappings, key=lambda item: (item.artifact_kind, item.artifact_id)))

    def _entity_mapping(
        self,
        request: GraphProjectionRequest,
        source: ExtractionSourceRevision,
        entity: AcceptedEntity,
    ) -> GraphArtifactMapping:
        return self._artifact_mapping(
            request=request,
            source=source,
            artifact_kind="entity",
            artifact_id=entity.entity_id,
            evidence_ids=tuple(reference.evidence_id for reference in entity.evidence),
            locators=_evidence_locators(entity.evidence),
        )

    def _relation_mapping(
        self,
        request: GraphProjectionRequest,
        source: ExtractionSourceRevision,
        relation: AcceptedRelation,
    ) -> GraphArtifactMapping:
        return self._artifact_mapping(
            request=request,
            source=source,
            artifact_kind="relation",
            artifact_id=relation.relation_id,
            evidence_ids=tuple(reference.evidence_id for reference in relation.evidence),
            locators=_evidence_locators(relation.evidence),
        )

    def _artifact_mapping(
        self,
        *,
        request: GraphProjectionRequest,
        source: ExtractionSourceRevision,
        artifact_kind: Literal["entity", "relation"],
        artifact_id: str,
        evidence_ids: tuple[str, ...],
        locators: tuple[PhysicalArtifactLocator, ...],
    ) -> GraphArtifactMapping:
        return GraphArtifactMapping(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            namespace_digest=request.physical_namespace.namespace_digest,
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            data_id=self._data_id(
                request,
                artifact_kind,
                artifact_id,
                source.source_object_id,
            ),
            source_object_id=source.source_object_id,
            source_revision_id=source.source_revision_id,
            revision_digest=source.revision_digest,
            revision_number=source.revision_number,
            projection_config_id=request.projection_config.config_id,
            projection_config_version=request.projection_config.version,
            projection_config_digest=request.projection_config.config_digest,
            locators=locators,
            evidence_ids=evidence_ids,
        )

    def _data_item(
        self,
        request: GraphProjectionRequest,
        mapping: GraphArtifactMapping,
    ) -> object:
        result = request.result
        if mapping.artifact_kind == "entity":
            entity = next(item for item in result.entities if item.entity_id == mapping.artifact_id)
            payload: dict[str, object] = {
                "schema_version": "cognee-entity-artifact:v1",
                "entity_id": entity.entity_id,
                "entity_type": entity.entity_type,
                "canonical_name": entity.canonical_name,
                "chunk_ids": entity.chunk_ids,
            }
            evidence = entity.evidence
        else:
            relation = next(item for item in result.relations if item.relation_id == mapping.artifact_id)
            payload = {
                "schema_version": "cognee-relation-artifact:v1",
                "relation_id": relation.relation_id,
                "relation_type": relation.relation_type,
                "subject_entity_id": relation.subject_entity_id,
                "object_entity_id": relation.object_entity_id,
            }
            evidence = relation.evidence
        return self._sdk.make_data_item(
            data=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            data_id=mapping.data_id,
            external_metadata={
                "spine_schema": "graph-artifact:v1",
                "artifact_kind": mapping.artifact_kind,
                "artifact_id": mapping.artifact_id,
                "workspace_id": str(request.workspace_id),
                "environment_id": str(request.environment_id),
                "namespace": request.physical_namespace.namespace,
                "namespace_digest": mapping.namespace_digest,
                "projection_candidate_id": str(request.projection_candidate.candidate_id),
                "projection_run_id": str(request.projection_candidate.run_id),
                "projection_config_id": str(request.projection_config.config_id),
                "projection_config_version": request.projection_config.version,
                "projection_config_digest": request.projection_config.config_digest,
                "ontology_version": result.ontology_version,
                "ontology_digest": result.ontology_digest,
                "extraction_config_version": result.extraction_config_version,
                "extraction_config_digest": result.extraction_config_digest,
                "source_object_id": str(mapping.source_object_id),
                "source_revision_id": str(mapping.source_revision_id),
                "revision_digest": mapping.revision_digest,
                "revision_number": mapping.revision_number,
                "evidence": [reference.model_dump(mode="json") for reference in evidence],
            },
        )

    async def _delete_mappings(self, record: _GraphRecord) -> None:
        if record.dataset_id is None:
            return
        for mapping in record.mappings:
            await self._sdk.delete_data(
                dataset_id=record.dataset_id,
                data_id=mapping.data_id,
                mode="soft",
            )

    async def _resolve_dataset_id(self, dataset_name: str, candidate: UUID | None) -> UUID | None:
        if candidate is not None:
            self._dataset_ids[dataset_name] = candidate
            return candidate
        datasets = await self._sdk.list_datasets()
        for dataset in datasets:
            name = dataset.get("name") if isinstance(dataset, Mapping) else getattr(dataset, "name", None)
            identifier = dataset.get("id") if isinstance(dataset, Mapping) else getattr(dataset, "id", None)
            if name == dataset_name and isinstance(identifier, UUID):
                self._dataset_ids[dataset_name] = identifier
                return identifier
        return None

    @staticmethod
    def _dataset_id_from_result(result: object) -> UUID | None:
        value = (
            result.get("dataset_id") or result.get("datasetId")
            if isinstance(result, Mapping)
            else getattr(result, "dataset_id", getattr(result, "datasetId", None))
        )
        return value if isinstance(value, UUID) else None

    def _dataset_id_required(self, dataset_name: str) -> UUID:
        dataset_id = self._dataset_ids.get(dataset_name)
        if dataset_id is None:
            raise ValueError("graph dataset identity is unavailable")
        return dataset_id

    def _validate_request(self, request: GraphProjectionRequest) -> str | None:
        context = ProjectionNamespaceContext(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            projection_kind=request.physical_namespace.projection_kind,
            projection_candidate=request.projection_candidate,
            projection_config=request.projection_config,
            canonical_boundary=request.canonical_boundary,
            state=request.physical_namespace.state,
        )
        try:
            self._router.validate(request.physical_namespace, context)
        except (NamespaceRoutingError, ValueError):
            return "namespace_unavailable"
        if request.physical_namespace.state in {
            ProjectionNamespaceState.FAILED,
            ProjectionNamespaceState.RETIRED,
        }:
            return "namespace_unavailable"
        if (
            request.result.status is ExtractionStatus.REJECTED
            and request.operation is not ProjectionOperation.REMOVE
        ):
            return "extraction_rejected"
        if request.projection_config.ontology_version != request.result.ontology_version:
            return "configuration_mismatch"
        if request.operation is ProjectionOperation.REMOVE:
            if not any(
                source.status == "tombstoned"
                for source in request.result.source_revisions
            ):
                return "tombstoned_source"
        else:
            fact_revision_ids = {
                *(entity.source_revision_id for entity in request.result.entities),
                *(relation.source_revision_id for relation in request.result.relations),
            }
            fact_sources = tuple(
                source
                for source in request.result.source_revisions
                if source.source_revision_id in fact_revision_ids
            )
            if fact_sources and any(
                source.status != "current" for source in fact_sources
            ):
                if any(source.status == "tombstoned" for source in fact_sources):
                    return "tombstoned_source"
                return "revision_fenced"
            if not fact_sources and not any(
                source.status == "current"
                for source in request.result.source_revisions
            ):
                if any(
                    source.status == "tombstoned"
                    for source in request.result.source_revisions
                ):
                    return "tombstoned_source"
                return "revision_fenced"
        return None

    def _sources_for(self, request: GraphProjectionRequest) -> tuple[ExtractionSourceRevision, ...]:
        statuses = {"tombstoned"} if request.operation is ProjectionOperation.REMOVE else {"current"}
        return tuple(
            source
            for source in request.result.source_revisions
            if source.status in statuses
        )

    @staticmethod
    def _is_fenced(
        source: ExtractionSourceRevision,
        record: _GraphRecord,
        operation: ProjectionOperation,
    ) -> bool:
        if source.revision_number < record.revision_number:
            return True
        if source.revision_number == record.revision_number:
            if (
                source.source_revision_id != record.source_revision_id
                or source.revision_digest != record.revision_digest
            ):
                return True
            return record.tombstoned and operation is not ProjectionOperation.REMOVE
        return False

    @staticmethod
    def _artifact_key(mapping: GraphArtifactMapping) -> tuple[str, str]:
        return mapping.artifact_kind, mapping.artifact_id

    @staticmethod
    def _command_key(request: GraphProjectionRequest) -> tuple[UUID, UUID, str, str]:
        return (
            request.workspace_id,
            request.environment_id,
            request.physical_namespace.namespace_digest,
            request.command_identity,
        )

    @staticmethod
    def _record_key(
        request: GraphProjectionRequest,
        source_object_id: UUID,
    ) -> tuple[UUID, UUID, str, UUID]:
        return (
            request.workspace_id,
            request.environment_id,
            request.physical_namespace.namespace_digest,
            source_object_id,
        )

    @staticmethod
    def _data_id(
        request: GraphProjectionRequest,
        kind: str,
        artifact_id: str,
        source_object_id: UUID,
    ) -> UUID:
        namespace = uuid5(NAMESPACE_URL, f"spine:{request.physical_namespace.namespace_digest}")
        return uuid5(namespace, f"{source_object_id}:{kind}:{artifact_id}")

    @staticmethod
    def dataset_name(request: GraphProjectionRequest) -> str:
        return (
            f"spine_{request.physical_namespace.projection_kind}_"
            f"{request.physical_namespace.namespace_digest}"
        )

    def _pending_artifacts(
        self,
        request: GraphProjectionRequest,
    ) -> tuple[GraphArtifactMapping | str, ...]:
        pending: list[GraphArtifactMapping | str] = []
        for source in self._sources_for(request):
            record = self._pending_records.get(self._record_key(request, source.source_object_id))
            if record is not None:
                pending.extend(
                    record.existing_mappings
                    if record.record.tombstoned
                    else record.record.mappings
                )
        return tuple(pending)

    @staticmethod
    def _result(
        request: GraphProjectionRequest,
        status: GraphProjectionStatus,
        failure_code: str | None = None,
        *,
        mappings: tuple[GraphArtifactMapping, ...] = (),
        pending_artifact_ids: tuple[str, ...] = (),
        attempt: int = 1,
        recovered: bool = False,
    ) -> GraphProjectionResult:
        return GraphProjectionResult(
            operation=request.operation,
            status=status,
            command_identity=request.command_identity,
            command_digest=request.command_digest,
            mappings=mappings,
            pending_artifact_ids=pending_artifact_ids,
            failure_code=failure_code,
            attempt=attempt,
            recovered=recovered,
        )


class _GraphFence(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _evidence_locators(evidence) -> tuple[PhysicalArtifactLocator, ...]:
    return tuple(
        PhysicalArtifactLocator.from_payload(
            kind=reference.locator.kind,
            payload=reference.locator,
        )
        for reference in evidence
    )


CogneeGraphProjectionStore = CogneeGraphProjectionWriter


__all__ = [
    "CogneeGraphProjectionStore",
    "CogneeGraphProjectionWriter",
    "GraphArtifactMapping",
    "GraphProjectionResult",
    "GraphProjectionStatus",
]
