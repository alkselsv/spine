"""Cognee 1.5.4 write-side adapter for parsed chunk artifacts.

The adapter owns only the physical projection.  Canonical revisions, parsed
documents and publication state remain Spine-owned inputs supplied by the
caller.  Cognee is reached through its public SDK surface and is imported only
when the concrete SDK bridge is constructed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from spine.domain.artifacts import ParsedDocument, PhysicalLineLocator
from spine.domain.common import DefinitionModel
from spine.memory.contracts import (
    ProjectionEffect,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionFailureCode,
    ProjectionNamespace,
    ProjectionNamespaceState,
    ProjectionOperation,
)
from spine.memory.namespace import (
    NamespaceRoutingError,
    ProjectionNamespaceContext,
    ProjectionNamespaceRouter,
)
from spine.memory.cognee.validation import PhysicalArtifact


SUPPORTED_COGNEE_VERSION = "1.5.4"


class CogneeVersionError(ValueError):
    """The configured SDK is outside the adapter's qualified version."""


class CogneeSdk(Protocol):
    """Small public-SDK-shaped seam used by the physical adapter and its fakes."""

    version: str

    def make_data_item(
        self,
        *,
        data: str,
        data_id: UUID,
        external_metadata: dict[str, object],
    ) -> object: ...

    async def add(
        self,
        *,
        data: list[object],
        dataset_name: str,
        dataset_id: UUID | None,
        node_set: list[str],
    ) -> object: ...

    async def cognify(self, *, datasets: list[str]) -> object: ...

    async def update(
        self,
        *,
        data_id: UUID,
        data: object,
        dataset_id: UUID,
        node_set: list[str],
        chunk_level_diff: bool,
    ) -> object: ...

    async def delete_data(self, *, dataset_id: UUID, data_id: UUID, mode: str) -> object: ...

    async def list_datasets(self) -> Sequence[object]: ...


class ParsedArtifactResolver(Protocol):
    """Resolve the durable parsed artifact needed by one projection effect."""

    async def __call__(self, effect: ProjectionEffect) -> ParsedDocument | None: ...


class CogneePublicSdk:
    """Bridge from the adapter seam to Cognee's public 1.5.4 SDK calls."""

    def __init__(self, sdk: Any | None = None) -> None:
        if sdk is None:
            from spine.memory.cognee_memory import configure_environment

            configure_environment()
            import cognee

            sdk = cognee
        self._sdk = sdk
        self.version = str(getattr(sdk, "__version__", ""))
        if self.version != SUPPORTED_COGNEE_VERSION:
            raise CogneeVersionError(
                f"Cognee {SUPPORTED_COGNEE_VERSION} is required, got {self.version or 'unknown'}"
            )

    def make_data_item(
        self,
        *,
        data: str,
        data_id: UUID,
        external_metadata: dict[str, object],
    ) -> object:
        # DataItem is the documented input type accepted by the public add()
        # function, but is not re-exported at cognee's top level.
        from cognee.tasks.ingestion.data_item import DataItem

        return DataItem(
            data=data,
            data_id=data_id,
            external_metadata=external_metadata,
        )

    async def add(
        self,
        *,
        data: list[object],
        dataset_name: str,
        dataset_id: UUID | None,
        node_set: list[str],
    ) -> object:
        return await self._sdk.add(
            data=data,
            dataset_name=dataset_name,
            dataset_id=dataset_id,
            node_set=node_set,
            run_in_background=False,
        )

    async def cognify(self, *, datasets: list[str]) -> object:
        return await self._sdk.cognify(
            datasets=datasets,
            run_in_background=False,
            raise_on_error=True,
        )

    async def update(
        self,
        *,
        data_id: UUID,
        data: object,
        dataset_id: UUID,
        node_set: list[str],
        chunk_level_diff: bool,
    ) -> object:
        return await self._sdk.update(
            data_id=data_id,
            data=data,
            dataset_id=dataset_id,
            node_set=node_set,
            chunk_level_diff=chunk_level_diff,
        )

    async def delete_data(self, *, dataset_id: UUID, data_id: UUID, mode: str) -> object:
        return await self._sdk.datasets.delete_data(
            dataset_id=dataset_id,
            data_id=data_id,
            mode=mode,
        )

    async def list_datasets(self) -> Sequence[object]:
        return await self._sdk.datasets.list_datasets()


class ChunkProjectionMapping(DefinitionModel):
    """Content-free mapping from a parsed segment to its physical data ID."""

    schema_version: Literal["chunk-projection-mapping:v2"] = "chunk-projection-mapping:v2"
    workspace_id: UUID
    environment_id: UUID
    namespace_digest: str = Field(pattern=r"[0-9a-f]{64}")
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=r"[0-9a-f]{64}")
    revision_number: int = Field(ge=1)
    projection_config_id: UUID
    projection_config_version: str
    projection_config_digest: str = Field(pattern=r"[0-9a-f]{64}")
    segment_id: UUID
    segment_digest: str = Field(pattern=r"[0-9a-f]{64}")
    data_id: UUID
    locator: PhysicalLineLocator


@dataclass
class _SourceRecord:
    revision_number: int
    source_revision_id: UUID
    revision_digest: str
    dataset_id: UUID | None
    mappings: tuple[ChunkProjectionMapping, ...]
    tombstoned: bool = False


class CogneeChunkProjectionStore:
    """Apply source-scoped physical effects to Cognee 1.5.4.

    The in-process manifest is deliberately content-free and is only the
    adapter's retry cache.  Durable command, receipt and publication records
    remain outside this adapter, in the caller-owned persistence/workflow
    boundary.
    """

    def __init__(
        self,
        *,
        sdk: CogneeSdk | None = None,
        artifacts: Mapping[UUID, ParsedDocument] | None = None,
        artifact_resolver: ParsedArtifactResolver | None = None,
        router: ProjectionNamespaceRouter | None = None,
    ) -> None:
        self._sdk = sdk or CogneePublicSdk()
        if self._sdk.version != SUPPORTED_COGNEE_VERSION:
            raise CogneeVersionError(
                f"Cognee {SUPPORTED_COGNEE_VERSION} is required, got {self._sdk.version or 'unknown'}"
            )
        self._artifacts = artifacts or {}
        self._artifact_resolver = artifact_resolver
        self._router = router or ProjectionNamespaceRouter()
        self._lock = asyncio.Lock()
        self._applied_effects: dict[str, ProjectionEffect] = {}
        self._commands: dict[
            tuple[UUID, UUID, str, str, UUID],
            tuple[str, ProjectionOperation, str, bool],
        ] = {}
        self._records: dict[tuple[UUID, UUID, str, UUID], _SourceRecord] = {}
        self._pending_records: dict[tuple[UUID, UUID, str, UUID], _SourceRecord] = {}
        self._dataset_ids: dict[str, UUID] = {}

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        """Apply one physical effect and return a safe status-only outcome."""

        async with self._lock:
            previous_effect = self._applied_effects.get(effect.effect_id)
            if previous_effect is not None:
                if not self._same_effect(previous_effect, effect):
                    return effect.model_copy(
                        update={
                            "status": ProjectionEffectStatus.FAILED,
                            "failure_code": ProjectionFailureCode.INTEGRITY_CONFLICT,
                        }
                    )
                return effect.model_copy(
                    update={"status": ProjectionEffectStatus.REPLAYED, "failure_code": None}
                )
            try:
                self._validate_namespace(effect)
                key = self._record_key(effect)
                command_key = self._command_key(effect)
                previous_command = self._commands.get(command_key) if effect.command_digest else None
                if previous_command is not None:
                    previous_digest, previous_operation, previous_effect_id, completed = previous_command
                    if (
                        previous_digest != effect.command_digest
                        or previous_operation is not effect.operation
                    ):
                        return effect.model_copy(
                            update={
                                "status": ProjectionEffectStatus.FAILED,
                                "failure_code": ProjectionFailureCode.INTEGRITY_CONFLICT,
                            }
                        )
                    if completed and previous_effect_id != effect.effect_id:
                        return effect.model_copy(update={"status": ProjectionEffectStatus.REPLAYED})
                elif effect.command_digest:
                    self._commands[command_key] = (
                        effect.command_digest,
                        effect.operation,
                        effect.effect_id,
                        False,
                    )

                record = self._records.get(key) or self._pending_records.get(key)
                if self._is_fenced(effect, record):
                    return effect.model_copy(
                        update={
                            "status": ProjectionEffectStatus.FAILED,
                            "failure_code": ProjectionFailureCode.REVISION_FENCED,
                        }
                    )
                if effect.action is ProjectionEffectAction.REMOVE_SOURCE:
                    await self._remove(effect, key, record)
                else:
                    await self._upsert(effect, key, record)
            except NamespaceRoutingError:
                return effect.model_copy(
                    update={
                        "status": ProjectionEffectStatus.FAILED,
                        "failure_code": ProjectionFailureCode.NAMESPACE_UNAVAILABLE,
                    }
                )
            except Exception:
                # Provider exceptions may contain source text or credentials.
                # The provider-neutral effect exposes only its status.
                return effect.model_copy(
                    update={
                        "status": ProjectionEffectStatus.FAILED,
                        "failure_code": ProjectionFailureCode.PHYSICAL_FAILURE,
                    }
                )
            self._applied_effects[effect.effect_id] = effect
            if effect.command_digest:
                self._commands[command_key] = (
                    effect.command_digest,
                    effect.operation,
                    effect.effect_id,
                    True,
                )
            return effect.model_copy(
                update={"status": ProjectionEffectStatus.APPLIED, "failure_code": None}
            )

    async def mappings_for(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> tuple[ChunkProjectionMapping, ...]:
        """Return content-free physical mappings for diagnostics and receipts."""

        async with self._lock:
            return tuple(
                mapping
                for record in self._records.values()
                if record.source_revision_id and not record.tombstoned
                for mapping in record.mappings
                if (
                    mapping.workspace_id == workspace_id
                    and mapping.environment_id == environment_id
                    and mapping.source_object_id == source_object_id
                )
            )

    async def physical_artifacts_for(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        namespace_digest: str,
    ) -> tuple[PhysicalArtifact, ...]:
        """Return content-free mappings needed for derived cleanup."""

        async with self._lock:
            records = dict(self._records)
            for key, pending in self._pending_records.items():
                records[key] = pending.record
            mappings: list[ChunkProjectionMapping] = []
            for key, record in records.items():
                if (
                    key[0] != workspace_id
                    or key[1] != environment_id
                    or key[2] != namespace_digest
                ):
                    continue
                pending = self._pending_records.get(key)
                mappings.extend(
                    pending.existing_mappings
                    if pending is not None and pending.record.tombstoned
                    else record.mappings
                )
            return tuple(
                PhysicalArtifact.from_chunk_mapping(mapping)
                for mapping in sorted(
                    mappings,
                    key=lambda item: (str(item.segment_id), str(item.data_id)),
                )
            )

    async def _upsert(
        self,
        effect: ProjectionEffect,
        key: tuple[UUID, UUID, str, UUID],
        record: _SourceRecord | None,
    ) -> None:
        pending = self._pending_records.get(key)
        if (
            pending is not None
            and pending.revision_number == effect.revision_number
            and pending.revision_digest == effect.revision_digest
        ):
            await self._sdk.cognify(datasets=[self.dataset_name(effect.physical_namespace)])
            self._pending_records.pop(key, None)
            self._records[key] = pending
            return

        if record is not None and record.revision_number == effect.revision_number:
            if (
                record.source_revision_id != effect.source_revision_id
                or record.revision_digest != effect.revision_digest
            ):
                raise ValueError("physical revision identity conflict")
            if record.mappings and not record.tombstoned and effect.operation is not ProjectionOperation.REBUILD:
                return

        document = await self._artifact(effect)
        if not document.segments:
            raise ValueError("parsed artifact has no indexable segments")

        if (
            record is not None
            and record.revision_number < effect.revision_number
            and effect.operation is ProjectionOperation.UPDATE
            and record.mappings
        ):
            await self._update_existing(effect, key, record, document)
            return

        if (
            record is not None
            and record.revision_number == effect.revision_number
            and effect.operation is ProjectionOperation.REBUILD
        ):
            await self._delete_mappings(record)
            self._records.pop(key, None)
            record = None

        if record is not None and record.revision_number < effect.revision_number:
            await self._delete_mappings(record)
            self._records.pop(key, None)

        dataset_name = self.dataset_name(effect.physical_namespace)
        dataset_id = record.dataset_id if record is not None else self._dataset_ids.get(dataset_name)
        node_set = [effect.physical_namespace.namespace_digest]
        data_ids = {
            segment.segment_id: self._data_id(effect, segment.segment_id)
            for segment in document.segments
        }
        items = [
            self._sdk.make_data_item(
                data=segment.text,
                data_id=data_ids[segment.segment_id],
                external_metadata=self._metadata(
                    effect,
                    document,
                    segment,
                    data_id=data_ids[segment.segment_id],
                ),
            )
            for segment in document.segments
        ]
        add_result = await self._sdk.add(
            data=items,
            dataset_name=dataset_name,
            dataset_id=dataset_id,
            node_set=node_set,
        )
        resolved_dataset_id = await self._resolve_dataset_id(
            dataset_name,
            dataset_id or self._dataset_id_from_result(add_result),
        )
        mappings = tuple(
            self._mapping(
                effect,
                document,
                segment,
                data_id=data_ids[segment.segment_id],
            )
            for segment in document.segments
        )
        completed_record = _SourceRecord(
            revision_number=effect.revision_number,
            source_revision_id=effect.source_revision_id,
            revision_digest=effect.revision_digest,
            dataset_id=resolved_dataset_id,
            mappings=mappings,
        )
        self._pending_records[key] = completed_record
        await self._sdk.cognify(datasets=[dataset_name])
        self._pending_records.pop(key, None)
        self._records[key] = completed_record

    async def _update_existing(
        self,
        effect: ProjectionEffect,
        key: tuple[UUID, UUID, str, UUID],
        record: _SourceRecord,
        document: ParsedDocument,
    ) -> None:
        if record.dataset_id is None:
            raise ValueError("existing Cognee dataset identity is unavailable")
        dataset_name = self.dataset_name(effect.physical_namespace)
        node_set = [effect.physical_namespace.namespace_digest]
        new_mappings: list[ChunkProjectionMapping] = []
        old_by_locator = {
            mapping.locator.model_dump_json(): mapping for mapping in record.mappings
        }
        reused_data_ids: set[UUID] = set()
        new_segments: list[Any] = []

        for segment in document.segments:
            old_mapping = old_by_locator.get(segment.locator.model_dump_json())
            if old_mapping is None:
                new_segments.append(segment)
                continue
            reused_data_ids.add(old_mapping.data_id)
            item = self._sdk.make_data_item(
                data=segment.text,
                data_id=old_mapping.data_id,
                external_metadata=self._metadata(
                    effect,
                    document,
                    segment,
                    data_id=old_mapping.data_id,
                ),
            )
            await self._sdk.update(
                data_id=old_mapping.data_id,
                data=item,
                dataset_id=record.dataset_id,
                node_set=node_set,
                chunk_level_diff=True,
            )
            new_mappings.append(
                self._mapping(effect, document, segment, data_id=old_mapping.data_id)
            )

        if new_segments:
            new_data_ids = {
                segment.segment_id: self._data_id(effect, segment.segment_id)
                for segment in new_segments
            }
            items = [
                self._sdk.make_data_item(
                    data=segment.text,
                    data_id=new_data_ids[segment.segment_id],
                    external_metadata=self._metadata(
                        effect,
                        document,
                        segment,
                        data_id=new_data_ids[segment.segment_id],
                    ),
                )
                for segment in new_segments
            ]
            await self._sdk.add(
                data=items,
                dataset_name=dataset_name,
                dataset_id=record.dataset_id,
                node_set=node_set,
            )
            new_mappings.extend(
                self._mapping(
                    effect,
                    document,
                    segment,
                    data_id=new_data_ids[segment.segment_id],
                )
                for segment in new_segments
            )

        for old_mapping in record.mappings:
            if old_mapping.data_id in reused_data_ids:
                continue
            await self._sdk.delete_data(
                dataset_id=record.dataset_id,
                data_id=old_mapping.data_id,
                mode="soft",
            )

        completed_record = _SourceRecord(
            revision_number=effect.revision_number,
            source_revision_id=effect.source_revision_id,
            revision_digest=effect.revision_digest,
            dataset_id=record.dataset_id,
            mappings=tuple(new_mappings),
        )
        self._pending_records[key] = completed_record
        await self._sdk.cognify(datasets=[dataset_name])
        self._pending_records.pop(key, None)
        self._records[key] = completed_record

    async def _remove(
        self,
        effect: ProjectionEffect,
        key: tuple[UUID, UUID, str, UUID],
        record: _SourceRecord | None,
    ) -> None:
        if record is not None and record.revision_number == effect.revision_number and record.tombstoned:
            return
        if record is not None:
            await self._delete_mappings(record)
        self._pending_records.pop(key, None)
        self._records[key] = _SourceRecord(
            revision_number=effect.revision_number,
            source_revision_id=effect.source_revision_id,
            revision_digest=effect.revision_digest,
            dataset_id=record.dataset_id if record is not None else None,
            mappings=(),
            tombstoned=True,
        )

    async def _delete_mappings(self, record: _SourceRecord) -> None:
        if record.dataset_id is None:
            return
        for mapping in record.mappings:
            await self._sdk.delete_data(
                dataset_id=record.dataset_id,
                data_id=mapping.data_id,
                mode="soft",
            )

    async def _artifact(self, effect: ProjectionEffect) -> ParsedDocument:
        document = self._artifacts.get(effect.source_revision_id)
        if document is None and self._artifact_resolver is not None:
            document = await self._artifact_resolver(effect)
        if document is None:
            raise ValueError("parsed artifact is unavailable")
        if (
            document.workspace_id != effect.workspace_id
            or document.environment_id != effect.environment_id
            or document.source_object_id != effect.source_object_id
            or document.source_revision_id != effect.source_revision_id
            or document.source_revision_digest != effect.revision_digest
            or document.locator_contract_version != effect.locator_contract_version
        ):
            raise ValueError("parsed artifact lineage does not match projection effect")
        return document

    @staticmethod
    def _mapping(
        effect: ProjectionEffect,
        document: ParsedDocument,
        segment: Any,
        *,
        data_id: UUID,
    ) -> ChunkProjectionMapping:
        return ChunkProjectionMapping(
            workspace_id=effect.workspace_id,
            environment_id=effect.environment_id,
            namespace_digest=effect.physical_namespace.namespace_digest,
            source_object_id=effect.source_object_id,
            source_revision_id=effect.source_revision_id,
            revision_digest=effect.revision_digest,
            revision_number=effect.revision_number,
            projection_config_id=effect.projection_config.config_id,
            projection_config_version=effect.projection_config.version,
            projection_config_digest=effect.projection_config.config_digest,
            segment_id=segment.segment_id,
            segment_digest=segment.segment_digest,
            data_id=data_id,
            locator=segment.locator,
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
        if isinstance(result, Mapping):
            value = result.get("dataset_id") or result.get("datasetId")
        else:
            value = getattr(result, "dataset_id", getattr(result, "datasetId", None))
        return value if isinstance(value, UUID) else None

    def _validate_namespace(self, effect: ProjectionEffect) -> None:
        context = ProjectionNamespaceContext(
            workspace_id=effect.workspace_id,
            environment_id=effect.environment_id,
            projection_kind=effect.physical_namespace.projection_kind,
            projection_candidate=effect.projection_candidate,
            projection_config=effect.projection_config,
            canonical_boundary=effect.canonical_boundary,
            state=effect.physical_namespace.state,
        )
        try:
            self._router.validate(effect.physical_namespace, context)
        except NamespaceRoutingError as error:
            raise NamespaceRoutingError("namespace is not trusted") from error
        if effect.physical_namespace.state in {
            ProjectionNamespaceState.FAILED,
            ProjectionNamespaceState.RETIRED,
        }:
            raise NamespaceRoutingError("namespace is unavailable")

    @staticmethod
    def _same_effect(previous: ProjectionEffect, current: ProjectionEffect) -> bool:
        return (
            previous.workspace_id == current.workspace_id
            and previous.environment_id == current.environment_id
            and previous.command_identity == current.command_identity
            and previous.command_digest == current.command_digest
            and previous.operation is current.operation
            and previous.action is current.action
            and previous.projection_candidate == current.projection_candidate
            and previous.projection_config == current.projection_config
            and previous.canonical_boundary == current.canonical_boundary
            and previous.physical_namespace == current.physical_namespace
            and previous.source_object_id == current.source_object_id
            and previous.source_revision_id == current.source_revision_id
            and previous.revision_digest == current.revision_digest
            and previous.revision_number == current.revision_number
        )

    @staticmethod
    def _command_key(
        effect: ProjectionEffect,
    ) -> tuple[UUID, UUID, str, str, UUID]:
        return (
            effect.workspace_id,
            effect.environment_id,
            effect.physical_namespace.namespace_digest,
            effect.command_identity,
            effect.source_object_id,
        )

    @staticmethod
    def _record_key(effect: ProjectionEffect) -> tuple[UUID, UUID, str, UUID]:
        return (
            effect.workspace_id,
            effect.environment_id,
            effect.physical_namespace.namespace_digest,
            effect.source_object_id,
        )

    @staticmethod
    def _is_fenced(effect: ProjectionEffect, record: _SourceRecord | None) -> bool:
        if record is None:
            return False
        if effect.revision_number < record.revision_number:
            return True
        if effect.revision_number == record.revision_number:
            if (
                effect.source_revision_id != record.source_revision_id
                or effect.revision_digest != record.revision_digest
            ):
                return True
            return record.tombstoned and effect.action is not ProjectionEffectAction.REMOVE_SOURCE
        return False

    @staticmethod
    def dataset_name(namespace: ProjectionNamespace) -> str:
        """Derive a provider-owned dataset name from the server-routed namespace."""

        return f"spine_{namespace.projection_kind}_{namespace.namespace_digest}"

    @staticmethod
    def _data_id(effect: ProjectionEffect, segment_id: UUID) -> UUID:
        return CogneeChunkProjectionStore.data_id_for(
            namespace_digest=effect.physical_namespace.namespace_digest,
            source_object_id=effect.source_object_id,
            segment_id=segment_id,
        )

    @staticmethod
    def data_id_for(
        *, namespace_digest: str, source_object_id: UUID, segment_id: UUID
    ) -> UUID:
        """Derive the provider identity validated at the publication boundary."""

        namespace = uuid5(
            NAMESPACE_URL,
            f"spine:{namespace_digest}",
        )
        return uuid5(namespace, f"{source_object_id}:chunk:{segment_id}")

    @staticmethod
    def _metadata(
        effect: ProjectionEffect,
        document: ParsedDocument,
        segment: Any,
        *,
        data_id: UUID,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "spine_schema": "chunk-artifact:v1",
            "workspace_id": str(effect.workspace_id),
            "environment_id": str(effect.environment_id),
            "source_object_id": str(effect.source_object_id),
            "source_revision_id": str(effect.source_revision_id),
            "revision_digest": effect.revision_digest,
            "revision_number": effect.revision_number,
            "namespace": effect.physical_namespace.namespace,
            "namespace_digest": effect.physical_namespace.namespace_digest,
            "projection_candidate_id": str(effect.projection_candidate.candidate_id),
            "projection_run_id": str(effect.projection_candidate.run_id),
            "projection_config_id": str(effect.projection_config.config_id),
            "projection_config_version": effect.projection_config.version,
            "projection_config_digest": effect.projection_config.config_digest,
            "canonical_boundary_id": str(effect.canonical_boundary.boundary_id),
            "canonical_boundary_digest": effect.canonical_boundary.boundary_digest,
            "parsed_artifact_id": str(document.artifact_id),
            "parser_version": document.parser_version,
            "locator_contract_version": document.locator_contract_version,
            "segment_id": str(segment.segment_id),
            "data_id": str(data_id),
            "segment_digest": segment.segment_digest,
            "evidence_id": str(segment.segment_id),
            "evidence_digest": segment.segment_digest,
            "locator": segment.locator.model_dump(mode="json"),
        }
        if effect.processing_receipt_id is not None:
            metadata["processing_receipt_id"] = str(effect.processing_receipt_id)
        return metadata


__all__ = [
    "ChunkProjectionMapping",
    "CogneeChunkProjectionStore",
    "CogneePublicSdk",
    "CogneeSdk",
    "CogneeVersionError",
    "SUPPORTED_COGNEE_VERSION",
]
