from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from spine.application.projection_result_mapping import ProjectionMappingError
from spine.application.persistence.parsed_artifacts import ParsedArtifactCommand
from spine.domain.artifacts import ParsedDocument
from spine.domain.ingestion import ProcessingCoverage, ProcessingReceipt
from spine.domain.projections import ProjectionPartition
from spine.infrastructure.persistence.parsed_artifacts import (
    InMemoryParsedArtifactRepository,
)
from spine.infrastructure.persistence.cognee_projection_receipt import (
    CogneeProjectionReceiptBuilder,
)
from spine.application.persistence.unit_of_work_activity import (
    track_unit_of_work_activity,
)
from spine.memory.cognee.retrieval.adapter import CogneeDataMetadata
from spine.memory.contracts import ProjectionResult, ProjectionSourceRevision
from tests.application.test_projection_result_mapping import (
    ENVIRONMENT,
    NOW,
    PROCESSING_RECEIPT,
    REVISION,
    SOURCE,
    _command,
)
from tests.domain.artifacts.test_parsed_documents import revision as source_revision


class _PhysicalReader:
    def __init__(self, rows: tuple[CogneeDataMetadata, ...]) -> None:
        self.rows = rows
        self.calls = 0

    async def list_data_metadata(self, dataset_name: str) -> tuple[CogneeDataMetadata, ...]:
        assert dataset_name.startswith("spine_context_")
        self.calls += 1
        return self.rows


class _ProjectionRepository:
    async def list_partitions(
        self, projection_run_id: UUID
    ) -> tuple[ProjectionPartition, ...]:
        assert projection_run_id == _command().projection_run_id
        command = _command()
        return (
            ProjectionPartition(
                projection_partition_id=command.projection_candidate.partition_id,
                projection_run_id=command.projection_run_id,
                projection_candidate_id=command.projection_candidate.candidate_id,
                workspace_id=command.workspace_id,
                environment_id=ENVIRONMENT,
                security_domain_reference="security-domain:1",
                security_domain_version="v1",
                partition_identity="partition-1",
                expected_command_identity=command.command_identity,
                expected_command_digest=command.command_digest,
                created_at=NOW,
            ),
        )


class _IngestionRepository:
    async def resolve_processing_receipt_by_id(
        self, receipt_id: UUID
    ) -> ProcessingReceipt:
        assert receipt_id == PROCESSING_RECEIPT
        command = _command()
        document = _document()
        return ProcessingReceipt(
            processing_receipt_id=PROCESSING_RECEIPT,
            workspace_id=command.workspace_id,
            environment_id=ENVIRONMENT,
            ingestion_run_source_id=UUID(int=41),
            binding_id=UUID(int=42),
            source_object_id=SOURCE,
            source_revision_id=REVISION,
            processor_kind="document_parser",
            parser_name="plain_text",
            parser_version=document.parser_version,
            parser_config_reference=document.parser_config_reference,
            locator_contract_version=document.locator_contract_version,
            original_sha256=document.original_sha256,
            output_kind="parsed_document",
            output_reference=document.artifact_id,
            output_digest=document.artifact_digest,
            coverage=ProcessingCoverage.COMPLETE,
            command_identity="parsed-receipt-1",
            command_digest="a" * 64,
            completed_at=NOW,
        )


class _UnitOfWork:
    ingestion = _IngestionRepository()
    projections = _ProjectionRepository()

    async def __aenter__(self) -> "_UnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _document() -> ParsedDocument:
    from spine.ingestion.parsers import parse_document

    content = b"synthetic evidence\n"
    source = source_revision(
        content,
        workspace_id=_command().workspace_id,
        environment_id=ENVIRONMENT,
    )
    return parse_document(source, content).model_copy(update={"artifact_id": UUID(int=31)})


def _command_for_document(document: ParsedDocument):
    command = _command()
    source = command.source_revisions[0]
    return command.model_copy(
        update={
            "source_revisions": (
                ProjectionSourceRevision(
                    source_object_id=source.source_object_id,
                    source_revision_id=source.source_revision_id,
                    revision_digest=document.source_revision_digest,
                    revision_number=source.revision_number,
                    processing_receipt_id=source.processing_receipt_id,
                    locator_contract_version=document.locator_contract_version,
                ),
            )
        }
    )


def _successful_result() -> ProjectionResult:
    return ProjectionResult(
        operation=_command().operation,
        outcome="succeeded",
        command_identity=_command().command_identity,
        command_digest=_command().command_digest,
        artifact_reference="provider-artifact",
        counts=(),
        effects=(),
    )


def _chunk_row(command) -> CogneeDataMetadata:
    segment = _document().segments[0]
    metadata: dict[str, Any] = {
        "spine_schema": "chunk-artifact:v1",
        "workspace_id": str(command.workspace_id),
        "environment_id": str(command.environment_id),
        "source_object_id": str(SOURCE),
        "source_revision_id": str(REVISION),
        "revision_digest": command.source_revisions[0].revision_digest,
        "revision_number": 1,
        "namespace_digest": command.physical_namespace.namespace_digest,
        "projection_candidate_id": str(command.projection_candidate.candidate_id),
        "projection_run_id": str(command.projection_run_id),
        "projection_config_id": str(command.projection_config.config_id),
        "projection_config_version": command.projection_config.version,
        "projection_config_digest": command.projection_config.config_digest,
        "processing_receipt_id": str(PROCESSING_RECEIPT),
        "segment_id": str(segment.segment_id),
        "segment_digest": segment.segment_digest,
        "locator_contract_version": command.source_revisions[0].locator_contract_version,
        "locator": segment.locator.model_dump(mode="json"),
    }
    return CogneeDataMetadata(data_id=str(segment.segment_id), external_metadata=metadata)


@pytest.mark.asyncio
async def test_receipt_builder_uses_observed_cognee_data_and_canonical_segments() -> None:
    document = _document()
    command = _command_for_document(document)
    repository = InMemoryParsedArtifactRepository()
    await repository.persist(
        ParsedArtifactCommand.create(document=document, command_identity="parsed:1")
    )
    reader = _PhysicalReader((_chunk_row(command),))
    builder = CogneeProjectionReceiptBuilder(
        physical_reader=reader,
        parsed_artifacts=repository,
        uow_factory=lambda _context: _UnitOfWork(),
        context_factory=lambda _command: object(),
        clock=lambda: NOW,
        adapter_version="1.5.4",
    )

    receipt = await builder.build(command, _successful_result())

    assert reader.calls == 1
    assert [(item.kind, item.object_id) for item in receipt.emitted_objects] == [
        ("chunk", str(document.segments[0].segment_id))
    ]
    reference = receipt.source_references[0]
    assert reference.source_revision_id == REVISION
    assert reference.segment_id == f"segment-{document.segments[0].segment_id.hex}"
    assert reference.locator.kind == "character_range"
    assert reference.security_domain_reference == "security-domain:1"


@pytest.mark.asyncio
async def test_receipt_builder_rejects_missing_observed_artifact() -> None:
    repository = InMemoryParsedArtifactRepository()
    document = _document()
    command = _command_for_document(document)
    await repository.persist(
        ParsedArtifactCommand.create(document=document, command_identity="parsed:1")
    )
    builder = CogneeProjectionReceiptBuilder(
        physical_reader=_PhysicalReader(()),
        parsed_artifacts=repository,
        uow_factory=lambda _context: _UnitOfWork(),
        context_factory=lambda _command: object(),
        clock=lambda: NOW,
        adapter_version="1.5.4",
    )

    with pytest.raises(ProjectionMappingError):
        await builder.build(command, _successful_result())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata_field", "replacement"),
    (
        ("workspace_id", str(UUID(int=999))),
        ("revision_digest", "f" * 64),
        ("projection_config_digest", "f" * 64),
    ),
)
async def test_receipt_builder_rejects_foreign_or_stale_physical_metadata(
    metadata_field: str, replacement: str
) -> None:
    document = _document()
    command = _command_for_document(document)
    repository = InMemoryParsedArtifactRepository()
    await repository.persist(
        ParsedArtifactCommand.create(document=document, command_identity="parsed:1")
    )
    row = _chunk_row(command)
    metadata = dict(row.external_metadata)
    metadata[metadata_field] = replacement
    builder = CogneeProjectionReceiptBuilder(
        physical_reader=_PhysicalReader(
            (CogneeDataMetadata(data_id=row.data_id, external_metadata=metadata),)
        ),
        parsed_artifacts=repository,
        uow_factory=lambda _context: _UnitOfWork(),
        context_factory=lambda _command: object(),
        clock=lambda: NOW,
        adapter_version="1.5.4",
    )

    with pytest.raises(ProjectionMappingError):
        await builder.build(command, _successful_result())


@pytest.mark.asyncio
async def test_receipt_builder_refuses_provider_reads_inside_persistence_uow() -> None:
    document = _document()
    command = _command_for_document(document)
    reader = _PhysicalReader((_chunk_row(command),))
    builder = CogneeProjectionReceiptBuilder(
        physical_reader=reader,
        parsed_artifacts=InMemoryParsedArtifactRepository(),
        uow_factory=lambda _context: _UnitOfWork(),
        context_factory=lambda _command: object(),
        clock=lambda: NOW,
        adapter_version="1.5.4",
    )

    with track_unit_of_work_activity():
        with pytest.raises(ProjectionMappingError):
            await builder.build(command, _successful_result())

    assert reader.calls == 0
