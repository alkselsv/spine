"""Build canonical adapter receipts from Cognee's public physical inventory."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
from typing import Protocol
from uuid import UUID

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.parsed_artifacts import ParsedArtifactRepository
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.application.projection_request_execution import AdapterProjectionReceiptBuilder
from spine.application.projection_result_mapping import (
    AdapterImplementationMetadata,
    AdapterProjectedObject,
    AdapterProjectionReceipt,
    AdapterSourceProjectionReference,
    ProjectionMappingError,
)
from spine.application.persistence.unit_of_work_activity import unit_of_work_is_active
from spine.application.projection_result_persistence import stable_locator_from_parsed_locator
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory
from spine.domain.artifacts import (
    OoxmlTextLocator,
    PageTextLocator,
    ParsedDocument,
    ParsedSegment,
    PhysicalLineLocator,
    XlsxCellLocator,
)
from spine.domain.ingestion import ProcessingReceipt
from spine.domain.projections import ProjectionPartition
from spine.memory.cognee.indexing.store import CogneeChunkProjectionStore
from spine.memory.cognee.models.baseline import (
    StableLocator as CogneeGraphStableLocator,
)
from spine.memory.contracts import (
    ProjectCommand,
    ProjectionNamespaceState,
    ProjectionOperation,
    ProjectionResult,
    StableLocator,
)
from spine.memory.cognee.retrieval.adapter import (
    CogneeDataMetadata,
    CogneeDatasetUnavailableError,
)


class CogneePhysicalDataReader(Protocol):
    """Read data metadata from the routed Cognee dataset using its public API."""

    version: str

    async def list_data_metadata(
        self, dataset_name: str
    ) -> tuple[CogneeDataMetadata, ...]: ...


class CogneeProjectionReceiptBuilder(AdapterProjectionReceiptBuilder):
    """Join observed Cognee objects to exact PostgreSQL revisions and segments.

    Provider metadata is the source of emitted object IDs and references. The
    parsed-artifact repository supplies only the canonical evidence to validate
    that metadata against; it never manufactures the emitted inventory.
    """

    def __init__(
        self,
        *,
        physical_reader: CogneePhysicalDataReader,
        parsed_artifacts: ParsedArtifactRepository,
        uow_factory: UnitOfWorkFactory,
        context_factory: Callable[[ProjectCommand], TrustedPersistenceContext],
        clock: Callable[[], datetime],
        adapter_version: str | None = None,
    ) -> None:
        self._physical_reader = physical_reader
        self._parsed_artifacts = parsed_artifacts
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._clock = clock
        self._adapter_version = adapter_version or physical_reader.version

    async def build(
        self, command: ProjectCommand, result: ProjectionResult
    ) -> AdapterProjectionReceipt:
        if result.failure is not None or result.lifecycle.value not in {
            "succeeded",
            "recovered",
            "replayed",
        }:
            raise ProjectionMappingError("Cognee projection did not produce a receipt-ready result")

        self._validate_result_scope(command, result)
        if unit_of_work_is_active():
            raise ProjectionMappingError(
                "Cognee physical evidence must be read outside a PostgreSQL unit of work"
            )
        try:
            rows = await self._physical_reader.list_data_metadata(
                CogneeChunkProjectionStore.dataset_name(command.physical_namespace)
            )
        except CogneeDatasetUnavailableError:
            if command.operation is not ProjectionOperation.REMOVE:
                raise
            rows = ()
        if command.operation is ProjectionOperation.REMOVE:
            if any(self._row_mentions_sources(row, command) for row in rows):
                raise ProjectionMappingError("Cognee still exposes a removed source artifact")
            return self._receipt(command, result, (), (), ())

        source_keys = {
            (source.source_object_id, source.source_revision_id): source
            for source in command.source_revisions
        }
        parsed, security_domain = await self._resolve_parsed_artifacts(command)
        canonical_segments = {
            (source_key, f"segment-{segment.segment_id.hex}"): segment
            for source_key, document in parsed.items()
            for segment in document.segments
        }
        segments_by_locator: dict[
            tuple[tuple[UUID, UUID], str], list[tuple[str, ParsedSegment]]
        ] = {}
        for (source_key, segment_id), segment in canonical_segments.items():
            stable = _locator_identity(
                stable_locator_from_parsed_locator(segment.locator)
            )
            segments_by_locator.setdefault((source_key, stable), []).append(
                (segment_id, segment)
            )
        if not canonical_segments:
            raise ProjectionMappingError("canonical parsed segments are unavailable")

        references: list[AdapterSourceProjectionReference] = []
        emitted: list[AdapterProjectedObject] = []
        seen_objects: set[tuple[str, str]] = set()
        observed_chunks: set[tuple[tuple[UUID, UUID], str]] = set()
        observed_ids: set[str] = set()
        observed_rows: list[CogneeDataMetadata] = []
        for row in rows:
            metadata = row.external_metadata
            schema = metadata.get("spine_schema")
            if schema not in {"chunk-artifact:v1", "graph-artifact:v1"}:
                continue
            data_id = _as_uuid(row.data_id)
            if data_id is None or row.data_id in observed_ids:
                raise ProjectionMappingError("Cognee physical inventory is ambiguous")
            observed_ids.add(row.data_id)
            source_key = self._validate_metadata_scope(command, metadata)
            if source_key not in source_keys:
                raise ProjectionMappingError("Cognee physical inventory contains a foreign source")
            observed_rows.append(row)
            source = source_keys[source_key]
            if metadata.get("revision_digest") != source.revision_digest:
                raise ProjectionMappingError("Cognee physical inventory contains a stale revision")
            if metadata.get("revision_number") != source.revision_number:
                raise ProjectionMappingError("Cognee physical inventory revision is ambiguous")
            if metadata.get("locator_contract_version") != source.locator_contract_version:
                raise ProjectionMappingError("Cognee physical inventory locator contract is stale")
            if metadata.get("projection_config_id") != str(command.projection_config.config_id):
                raise ProjectionMappingError("Cognee physical inventory configuration is stale")
            if metadata.get("projection_config_version") != command.projection_config.version:
                raise ProjectionMappingError("Cognee physical inventory configuration is stale")
            if metadata.get("projection_config_digest") != command.projection_config.config_digest:
                raise ProjectionMappingError("Cognee physical inventory configuration is stale")
            receipt_id = source.processing_receipt_id
            if metadata.get("processing_receipt_id") != str(receipt_id):
                raise ProjectionMappingError("Cognee physical inventory receipt lineage is invalid")

            if schema == "chunk-artifact:v1":
                segment_uuid = _as_uuid(metadata.get("segment_id"))
                metadata_data_id = _as_uuid(metadata.get("data_id"))
                segment_id = f"segment-{segment_uuid.hex}" if segment_uuid else ""
                segment = canonical_segments.get((source_key, segment_id))
                if (
                    segment is None
                    or (
                        metadata.get("data_id") is not None
                        and metadata_data_id != data_id
                    )
                    or (
                        metadata.get("data_id") is None
                        and data_id != segment.segment_id
                    )
                    or metadata.get("segment_digest") != segment.segment_digest
                    or metadata.get("locator") != segment.locator.model_dump(mode="json")
                    or metadata.get("locator_contract_version")
                    != source.locator_contract_version
                ):
                    raise ProjectionMappingError("Cognee chunk does not match its parsed segment")
                observed_chunks.add((source_key, segment_id))
                projected_kind = "chunk"
                projected_id = row.data_id
                locators = ((segment_id, stable_locator_from_parsed_locator(segment.locator)),)
            else:
                projected_kind = _required_string(metadata, "artifact_kind")
                projected_id = _required_string(metadata, "artifact_id")
                locators = self._graph_locators(
                    metadata=metadata,
                    source_key=source_key,
                    segments_by_locator=segments_by_locator,
                )
            object_key = (projected_kind, projected_id)
            if object_key in seen_objects:
                raise ProjectionMappingError("Cognee emitted object identity is ambiguous")
            seen_objects.add(object_key)
            emitted.append(AdapterProjectedObject(kind=projected_kind, object_id=projected_id))
            for segment_id, locator in locators:
                references.append(
                    AdapterSourceProjectionReference(
                        workspace_id=command.workspace_id,
                        environment_id=command.environment_id,
                        namespace_digest=command.physical_namespace.namespace_digest,
                        source_object_id=source.source_object_id,
                        source_revision_id=source.source_revision_id,
                        revision_digest=source.revision_digest,
                        processing_receipt_id=receipt_id,
                        segment_id=segment_id,
                        locator_schema_version=source.locator_contract_version,
                        locator=locator,
                        locator_digest=canonical_projection_locator_digest(
                            locator_contract_version=source.locator_contract_version,
                            locator=locator,
                        ),
                        projected_kind=projected_kind,
                        projected_object_id=projected_id,
                        projection_config_version_id=command.projection_config.config_id,
                        security_domain_reference=security_domain.security_domain_reference,
                        security_domain_version=security_domain.security_domain_version,
                    )
                )

        for source_key, document in parsed.items():
            expected = {
                (source_key, f"segment-{segment.segment_id.hex}")
                for segment in document.segments
            }
            if not expected.issubset(observed_chunks):
                raise ProjectionMappingError("Cognee physical inventory is incomplete")
        if not emitted or not references:
            raise ProjectionMappingError("Cognee physical evidence is missing")
        return self._receipt(
            command, result, tuple(emitted), tuple(references), tuple(observed_rows)
        )

    async def _resolve_parsed_artifacts(
        self, command: ProjectCommand
    ) -> tuple[dict[tuple[UUID, UUID], ParsedDocument], ProjectionPartition]:
        receipts: dict[UUID, ProcessingReceipt] = {}
        async with self._uow_factory(self._context_factory(command)) as uow:
            for source in command.source_revisions:
                if source.processing_receipt_id is None:
                    raise ProjectionMappingError("canonical processing receipt is missing")
                receipt = await uow.ingestion.resolve_processing_receipt_by_id(
                    source.processing_receipt_id
                )
                if receipt is None:
                    raise ProjectionMappingError("canonical processing receipt is unavailable")
                receipts[source.source_revision_id] = receipt
            partitions = await uow.projections.list_partitions(command.projection_run_id)
        matches = [
            partition
            for partition in partitions
            if partition.projection_partition_id == command.projection_candidate.partition_id
        ]
        if len(matches) != 1:
            raise ProjectionMappingError("canonical projection partition is missing or ambiguous")
        partition = matches[0]
        if (
            partition.workspace_id != command.workspace_id
            or partition.environment_id != command.environment_id
        ):
            raise ProjectionMappingError("canonical projection partition scope is invalid")

        documents: dict[tuple[UUID, UUID], ParsedDocument] = {}
        for source in command.source_revisions:
            receipt = receipts[source.source_revision_id]
            if (
                receipt.workspace_id != command.workspace_id
                or receipt.environment_id != command.environment_id
                or receipt.source_object_id != source.source_object_id
                or receipt.source_revision_id != source.source_revision_id
                or receipt.processing_receipt_id != source.processing_receipt_id
                or receipt.locator_contract_version != source.locator_contract_version
                or receipt.output_reference is None
                or receipt.output_digest is None
            ):
                raise ProjectionMappingError("canonical processing receipt is invalid")
            document = await self._parsed_artifacts.get(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                artifact_id=receipt.output_reference,
            )
            if (
                document is None
                or document.workspace_id != command.workspace_id
                or document.environment_id != command.environment_id
                or document.source_object_id != source.source_object_id
                or document.source_revision_id != source.source_revision_id
                or document.source_revision_digest != source.revision_digest
                or document.original_sha256 != receipt.original_sha256
                or document.artifact_digest != receipt.output_digest
                or document.parser_version != receipt.parser_version
                or document.parser_config_reference != receipt.parser_config_reference
                or document.locator_contract_version != receipt.locator_contract_version
                or not document.segments
            ):
                raise ProjectionMappingError("canonical parsed artifact is invalid")
            documents[(source.source_object_id, source.source_revision_id)] = document
        return documents, partition

    def _graph_locators(
        self,
        *,
        metadata: Mapping[str, object],
        source_key: tuple[UUID, UUID],
        segments_by_locator: Mapping[
            tuple[tuple[UUID, UUID], str], list[tuple[str, ParsedSegment]]
        ],
    ) -> tuple[tuple[str, StableLocator], ...]:
        raw_evidence = metadata.get("evidence")
        if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, (str, bytes)):
            raise ProjectionMappingError("Cognee graph evidence is missing")
        found: list[tuple[str, StableLocator]] = []
        for raw in raw_evidence:
            if not isinstance(raw, Mapping):
                raise ProjectionMappingError("Cognee graph evidence is malformed")
            evidence_id = raw.get("evidence_id")
            locator_raw = raw.get("locator")
            if not isinstance(evidence_id, str) or not isinstance(locator_raw, Mapping):
                raise ProjectionMappingError("Cognee graph evidence is malformed")
            locator, matches = _resolve_graph_locator(
                locator_raw=locator_raw,
                source_key=source_key,
                segments_by_locator=segments_by_locator,
            )
            if len(matches) != 1:
                raise ProjectionMappingError("Cognee graph evidence is missing or ambiguous")
            if locator is None:
                raise ProjectionMappingError("Cognee graph locator is malformed")
            segment_id, segment = matches[0]
            if locator != stable_locator_from_parsed_locator(segment.locator):
                raise ProjectionMappingError("Cognee graph locator does not match parsed evidence")
            found.append((segment_id, locator))
        if len(found) != 1 or len({item[0] for item in found}) != len(found):
            raise ProjectionMappingError("Cognee graph evidence is ambiguous")
        return tuple(found)

    @staticmethod
    def _validate_result_scope(command: ProjectCommand, result: ProjectionResult) -> None:
        if (
            result.operation is not command.operation
            or result.command_identity != command.command_identity
            or result.command_digest != command.command_digest
            or command.physical_namespace.state
            not in {ProjectionNamespaceState.SHADOW, ProjectionNamespaceState.REBUILDING}
        ):
            raise ProjectionMappingError("Cognee result does not match its canonical command")

    @staticmethod
    def _validate_metadata_scope(
        command: ProjectCommand, metadata: Mapping[str, object]
    ) -> tuple[UUID, UUID]:
        workspace_id = _as_uuid(metadata.get("workspace_id"))
        environment_id = _as_uuid(metadata.get("environment_id"))
        source_object_id = _as_uuid(metadata.get("source_object_id"))
        source_revision_id = _as_uuid(metadata.get("source_revision_id"))
        if (
            workspace_id != command.workspace_id
            or environment_id != command.environment_id
            or metadata.get("namespace_digest")
            != command.physical_namespace.namespace_digest
            or metadata.get("projection_candidate_id")
            != str(command.projection_candidate.candidate_id)
            or metadata.get("projection_run_id") != str(command.projection_run_id)
            or source_object_id is None
            or source_revision_id is None
        ):
            raise ProjectionMappingError("Cognee physical artifact scope is invalid")
        return source_object_id, source_revision_id

    @staticmethod
    def _row_mentions_sources(row: CogneeDataMetadata, command: ProjectCommand) -> bool:
        source_ids = {str(item.source_object_id) for item in command.source_revisions}
        return row.external_metadata.get("source_object_id") in source_ids

    def _receipt(
        self,
        command: ProjectCommand,
        result: ProjectionResult,
        emitted: tuple[AdapterProjectedObject, ...],
        references: tuple[AdapterSourceProjectionReference, ...],
        rows: tuple[CogneeDataMetadata, ...],
    ) -> AdapterProjectionReceipt:
        physical_digest = sha256(
            json.dumps(
                [
                    {
                        "data_id": row.data_id,
                        "metadata": _safe_metadata(row.external_metadata),
                    }
                    for row in sorted(rows, key=lambda item: item.data_id)
                    if row.external_metadata.get("spine_schema")
                    in {"chunk-artifact:v1", "graph-artifact:v1"}
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        completed_at = self._clock()
        return AdapterProjectionReceipt(
            result=result,
            artifact_kind="cognee_dataset",
            artifact_integrity_digest=physical_digest,
            emitted_objects=emitted,
            source_references=references,
            implementation_schema_version="cognee-projection-receipt:v1",
            implementation_metadata=AdapterImplementationMetadata(
                adapter_contract=command.projection_config.adapter_contract_version,
                adapter_name="cognee",
                adapter_version=self._adapter_version,
            ),
            idempotency_key=f"{command.command_identity}:{command.command_digest}",
            completed_at=completed_at,
        )


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _locator_identity(locator: StableLocator) -> str:
    return json.dumps(
        locator.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _required_string(metadata: Mapping[str, object], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectionMappingError("Cognee physical metadata is incomplete")
    return value


def _resolve_graph_locator(
    *,
    locator_raw: Mapping[str, object],
    source_key: tuple[UUID, UUID],
    segments_by_locator: Mapping[
        tuple[tuple[UUID, UUID], str], list[tuple[str, ParsedSegment]]
    ],
) -> tuple[StableLocator | None, list[tuple[str, ParsedSegment]]]:
    try:
        locator = StableLocator.model_validate(locator_raw)
    except ValueError:
        try:
            graph_locator = CogneeGraphStableLocator.model_validate(locator_raw)
        except ValueError:
            raise ProjectionMappingError("Cognee graph locator is malformed") from None
        matches = [
            item
            for (candidate_source, _identity), candidates in segments_by_locator.items()
            if candidate_source == source_key
            for item in candidates
            if _graph_locator_matches_segment(graph_locator, item[1])
        ]
        if len(matches) != 1:
            return None, matches
        return stable_locator_from_parsed_locator(matches[0][1].locator), matches
    return locator, segments_by_locator.get(
        (source_key, _locator_identity(locator)),
        [],
    )


def _graph_locator_matches_segment(
    locator: CogneeGraphStableLocator,
    segment: ParsedSegment,
) -> bool:
    parsed = segment.locator
    if locator.kind == "pdf_page":
        return isinstance(parsed, PageTextLocator) and parsed.page_number == locator.page
    if locator.kind == "text_range":
        return (
            isinstance(parsed, (PhysicalLineLocator, PageTextLocator, OoxmlTextLocator))
            and parsed.start_codepoint == locator.start
            and parsed.end_codepoint == locator.end
        )
    if locator.kind == "xlsx_row":
        return (
            isinstance(parsed, XlsxCellLocator)
            and parsed.sheet == locator.sheet
            and parsed.row == locator.row
        )
    return False


def _safe_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    """Keep only the fixed Spine fields used as physical evidence."""

    allowed = {
        "spine_schema", "workspace_id", "environment_id", "source_object_id",
        "source_revision_id", "revision_digest", "revision_number", "namespace_digest",
        "projection_candidate_id", "projection_run_id", "projection_config_id",
        "projection_config_version", "projection_config_digest", "processing_receipt_id",
        "segment_id", "segment_digest", "locator_contract_version", "locator",
        "artifact_kind", "artifact_id", "evidence",
    }
    return {key: metadata[key] for key in allowed if key in metadata}


__all__ = ["CogneePhysicalDataReader", "CogneeProjectionReceiptBuilder"]
