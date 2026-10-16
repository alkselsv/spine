"""In-memory and PostgreSQL persistence for parsed document artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.application.persistence.errors import ParsedArtifactConflictError
from spine.application.persistence.parsed_artifacts import (
    ParsedArtifactCommand,
    ParsedArtifactMetadata,
    ParsedArtifactPersistenceResult,
    ParsedArtifactRepository,
    ParsedArtifactSegmentPage,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.capabilities import Citation
from spine.domain.artifacts import (
    Locator,
    ParseCoverage,
    ParseExclusion,
    ParsedDocument,
    ParsedSegment,
)
from spine.domain.artifacts import (
    OoxmlTextLocator,
    PageTextLocator,
    PhysicalLineLocator,
    XlsxCellLocator,
)
from spine.domain.disclosure import CitationExcerpt
from spine.memory.contracts import StableLocator
from spine.infrastructure.persistence.postgresql_mappings import (
    parsed_document_segments,
    parsed_documents,
)


class InMemoryParsedArtifactRepository:
    """Atomic tenant-scoped repository used by offline and contract tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._documents: dict[tuple[UUID, UUID, UUID], ParsedDocument] = {}
        self._commands: dict[tuple[UUID, UUID, str], tuple[str, UUID]] = {}

    async def persist(self, command: ParsedArtifactCommand) -> ParsedArtifactPersistenceResult:
        document = command.document
        command_key = (document.workspace_id, document.environment_id, command.command_identity)
        artifact_key = (document.workspace_id, document.environment_id, document.artifact_id)
        async with self._lock:
            existing_command = self._commands.get(command_key)
            if existing_command is not None:
                existing_digest, artifact_id = existing_command
                if existing_digest != command.command_digest:
                    raise ParsedArtifactConflictError("Parsed artifact command conflicts with history.")
                existing = self._documents[(*artifact_key[:2], artifact_id)]
                return ParsedArtifactPersistenceResult(existing.model_copy(deep=True), replay=True)
            existing_artifact = self._documents.get(artifact_key)
            if existing_artifact is not None:
                if existing_artifact.artifact_digest != document.artifact_digest:
                    raise ParsedArtifactConflictError("Parsed artifact identity conflicts with history.")
                self._commands[command_key] = (command.command_digest, document.artifact_id)
                return ParsedArtifactPersistenceResult(existing_artifact.model_copy(deep=True), replay=True)
            # Validate every row before either map is changed: this is the
            # in-memory equivalent of the PostgreSQL transaction boundary.
            stored = document.model_copy(deep=True)
            self._documents[artifact_key] = stored
            self._commands[command_key] = (command.command_digest, document.artifact_id)
            return ParsedArtifactPersistenceResult(stored.model_copy(deep=True), replay=False)

    async def get(self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID) -> ParsedDocument | None:
        async with self._lock:
            value = self._documents.get((workspace_id, environment_id, artifact_id))
            return value.model_copy(deep=True) if value is not None else None

    async def resolve_segment(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID,
        source_revision_digest: str, original_sha256: str, parser_version: str,
        locator_contract_version: str, locator: Locator,
    ) -> ParsedSegment | None:
        async with self._lock:
            documents = tuple(
                document for (workspace, environment, _), document in self._documents.items()
                if workspace == workspace_id and environment == environment_id
                and document.source_revision_id == source_revision_id
                and document.source_revision_digest == source_revision_digest
                and document.original_sha256 == original_sha256
                and document.parser_version == parser_version
                and document.locator_contract_version == locator_contract_version
            )
            if len(documents) != 1:
                return None
            segment = next((item for item in documents[0].segments if item.locator == locator), None)
            return segment.model_copy(deep=True) if segment is not None else None

    async def count(self) -> int:
        async with self._lock:
            return len(self._documents)

    async def resolve_stable_segment(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID, source_revision_digest: str, original_sha256: str,
        locator_contract_version: str, locator_kind: str, locator_value: str,
    ) -> tuple[ParsedDocument, ParsedSegment] | None:
        async with self._lock:
            matches = tuple(
                (document, segment)
                for (workspace, environment, _), document in self._documents.items()
                if workspace == workspace_id and environment == environment_id
                and document.source_object_id == source_object_id
                and document.source_revision_id == source_revision_id
                and document.source_revision_digest == source_revision_digest
                and document.original_sha256 == original_sha256
                and document.locator_contract_version == locator_contract_version
                for segment in document.segments
                if _stable_locator_key(segment.locator) == (locator_kind, locator_value)
            )
            if len(matches) != 1:
                return None
            document, segment = matches[0]
            return document.model_copy(deep=True), segment.model_copy(deep=True)

    async def list_segments(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID, source_revision_digest: str, original_sha256: str,
        artifact_id: UUID, artifact_digest: str, parser_name: str, parser_version: str,
        parser_config_reference: str, locator_contract_version: str,
        selector: Locator | None, after_segment_id: UUID | None, limit: int,
    ) -> ParsedArtifactSegmentPage | None:
        async with self._lock:
            document = self._documents.get((workspace_id, environment_id, artifact_id))
            if document is None or not _document_matches_identity(
                document,
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
                source_revision_digest=source_revision_digest,
                original_sha256=original_sha256,
                artifact_digest=artifact_digest,
                parser_name=parser_name,
                parser_version=parser_version,
                parser_config_reference=parser_config_reference,
                locator_contract_version=locator_contract_version,
            ):
                return None
            segments = tuple(
                segment for segment in document.segments
                if selector is None or segment.locator == selector
            )
            start = 0
            if after_segment_id is not None:
                positions = [
                    index for index, segment in enumerate(segments)
                    if segment.segment_id == after_segment_id
                ]
                if len(positions) != 1:
                    return None
                start = positions[0] + 1
            page = segments[start : start + limit + 1]
            return ParsedArtifactSegmentPage(
                metadata=ParsedArtifactMetadata.from_document(document),
                segments=tuple(segment.model_copy(deep=True) for segment in page[:limit]),
                has_more=len(page) > limit,
            )


class PostgreSQLParsedArtifactRepository:
    """Transactionally persist one document and all of its segments."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def persist(self, command: ParsedArtifactCommand) -> ParsedArtifactPersistenceResult:
        document = command.document
        async with self._session_factory() as session:
            async with session.begin():
                existing = (
                    await session.execute(
                        select(parsed_documents).where(
                            parsed_documents.c.workspace_id == document.workspace_id,
                            parsed_documents.c.environment_id == document.environment_id,
                            parsed_documents.c.command_identity == command.command_identity,
                        )
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    if existing["command_digest"] != command.command_digest:
                        raise ParsedArtifactConflictError("Parsed artifact command conflicts with history.")
                    loaded = await self.get(
                        workspace_id=document.workspace_id, environment_id=document.environment_id,
                        artifact_id=existing["artifact_id"], session=session,
                    )
                    if loaded is None:
                        raise ParsedArtifactConflictError("Parsed artifact history is incomplete.")
                    if loaded.artifact_digest != document.artifact_digest:
                        raise ParsedArtifactConflictError("Parsed artifact command conflicts with history.")
                    return ParsedArtifactPersistenceResult(loaded, replay=True)
                existing_artifact = (
                    await session.execute(
                        select(parsed_documents.c.artifact_digest).where(
                            parsed_documents.c.workspace_id == document.workspace_id,
                            parsed_documents.c.environment_id == document.environment_id,
                            parsed_documents.c.artifact_id == document.artifact_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing_artifact is not None:
                    if existing_artifact != document.artifact_digest:
                        raise ParsedArtifactConflictError("Parsed artifact identity conflicts with history.")
                    return ParsedArtifactPersistenceResult(document, replay=True)
                inserted = await session.execute(
                    postgresql_insert(parsed_documents)
                    .values(_document_values(document, command))
                    .on_conflict_do_nothing()
                    .returning(parsed_documents.c.artifact_id)
                )
                if inserted.scalar_one_or_none() is None:
                    return await self._resolve_conflict(session, command)
                if document.segments:
                    await session.execute(
                        insert(parsed_document_segments),
                        [_segment_values(document, segment) for segment in document.segments],
                    )
                return ParsedArtifactPersistenceResult(document, replay=False)

    async def _resolve_conflict(
        self, session: Any, command: ParsedArtifactCommand
    ) -> ParsedArtifactPersistenceResult:
        document = command.document
        existing = (
            await session.execute(select(parsed_documents).where(
                parsed_documents.c.workspace_id == document.workspace_id,
                parsed_documents.c.environment_id == document.environment_id,
                parsed_documents.c.command_identity == command.command_identity,
            ))
        ).mappings().one_or_none()
        if existing is not None:
            if existing["command_digest"] != command.command_digest:
                raise ParsedArtifactConflictError("Parsed artifact command conflicts with history.")
            loaded = await self.get(
                workspace_id=document.workspace_id, environment_id=document.environment_id,
                artifact_id=existing["artifact_id"], session=session,
            )
            if loaded is None or loaded.artifact_digest != document.artifact_digest:
                raise ParsedArtifactConflictError("Parsed artifact history is incomplete.")
            return ParsedArtifactPersistenceResult(loaded, replay=True)
        existing_artifact = (
            await session.execute(select(parsed_documents).where(
                parsed_documents.c.workspace_id == document.workspace_id,
                parsed_documents.c.environment_id == document.environment_id,
                parsed_documents.c.artifact_id == document.artifact_id,
            ))
        ).mappings().one_or_none()
        if existing_artifact is not None and existing_artifact["artifact_digest"] == document.artifact_digest:
            return ParsedArtifactPersistenceResult(document, replay=True)
        raise ParsedArtifactConflictError("Parsed artifact identity conflicts with history.")

    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID, session: Any | None = None
    ) -> ParsedDocument | None:
        owns_session = session is None
        active = session or self._session_factory()
        try:
            result = await active.execute(select(parsed_documents).where(
                parsed_documents.c.workspace_id == workspace_id,
                parsed_documents.c.environment_id == environment_id,
                parsed_documents.c.artifact_id == artifact_id,
            ))
            row = result.mappings().one_or_none()
            if row is None:
                return None
            segments = (await active.execute(select(parsed_document_segments).where(
                parsed_document_segments.c.artifact_id == artifact_id,
                parsed_document_segments.c.workspace_id == workspace_id,
                parsed_document_segments.c.environment_id == environment_id,
                ).order_by(parsed_document_segments.c.segment_ordinal))).mappings().all()
            return _document_from_rows(row, segments)
        finally:
            if owns_session:
                await active.close()

    async def resolve_segment(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID,
        source_revision_digest: str, original_sha256: str, parser_version: str,
        locator_contract_version: str, locator: Locator,
    ) -> ParsedSegment | None:
        async with self._session_factory() as session:
            result = await session.execute(select(parsed_documents).where(
                parsed_documents.c.workspace_id == workspace_id,
                parsed_documents.c.environment_id == environment_id,
                parsed_documents.c.source_revision_id == source_revision_id,
                parsed_documents.c.source_revision_digest == source_revision_digest,
                parsed_documents.c.original_sha256 == original_sha256,
                parsed_documents.c.parser_version == parser_version,
                parsed_documents.c.locator_contract_version == locator_contract_version,
            ))
            row = result.mappings().one_or_none()
            if row is None:
                return None
            segments = (await session.execute(select(parsed_document_segments).where(
                parsed_document_segments.c.workspace_id == workspace_id,
                parsed_document_segments.c.environment_id == environment_id,
                parsed_document_segments.c.artifact_id == row["artifact_id"],
                parsed_document_segments.c.locator == locator.model_dump(mode="json"),
            ))).mappings().all()
            if len(segments) != 1:
                return None
            return ParsedSegment.model_validate({
                "schema_version": segments[0]["schema_version"], "segment_id": segments[0]["segment_id"],
                "segment_digest": segments[0]["segment_digest"], "artifact_digest": row["artifact_digest"],
                "text": segments[0]["text"], "text_digest": segments[0]["text_digest"],
                "locator": segments[0]["locator"], "heading_path": tuple(segments[0]["heading_path"]),
            })

    async def resolve_stable_segment(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID, source_revision_digest: str, original_sha256: str,
        locator_contract_version: str, locator_kind: str, locator_value: str,
    ) -> tuple[ParsedDocument, ParsedSegment] | None:
        async with self._session_factory() as session:
            rows = (await session.execute(select(parsed_documents).where(
                parsed_documents.c.workspace_id == workspace_id,
                parsed_documents.c.environment_id == environment_id,
                parsed_documents.c.source_object_id == source_object_id,
                parsed_documents.c.source_revision_id == source_revision_id,
                parsed_documents.c.source_revision_digest == source_revision_digest,
                parsed_documents.c.original_sha256 == original_sha256,
                parsed_documents.c.locator_contract_version == locator_contract_version,
            ))).mappings().all()
            if len(rows) != 1:
                return None
            row = rows[0]
            segment_rows = (await session.execute(select(parsed_document_segments).where(
                parsed_document_segments.c.workspace_id == workspace_id,
                parsed_document_segments.c.environment_id == environment_id,
                parsed_document_segments.c.artifact_id == row["artifact_id"],
            ).order_by(parsed_document_segments.c.segment_ordinal))).mappings().all()
            document = _document_from_rows(row, segment_rows)
            matches = tuple(
                segment for segment in document.segments
                if _stable_locator_key(segment.locator) == (locator_kind, locator_value)
            )
            if len(matches) != 1:
                return None
            return document, matches[0]

    async def list_segments(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID, source_revision_digest: str, original_sha256: str,
        artifact_id: UUID, artifact_digest: str, parser_name: str, parser_version: str,
        parser_config_reference: str, locator_contract_version: str,
        selector: Locator | None, after_segment_id: UUID | None, limit: int,
    ) -> ParsedArtifactSegmentPage | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(parsed_documents).where(
                    parsed_documents.c.workspace_id == workspace_id,
                    parsed_documents.c.environment_id == environment_id,
                    parsed_documents.c.source_object_id == source_object_id,
                    parsed_documents.c.source_revision_id == source_revision_id,
                    parsed_documents.c.source_revision_digest == source_revision_digest,
                    parsed_documents.c.original_sha256 == original_sha256,
                    parsed_documents.c.artifact_id == artifact_id,
                    parsed_documents.c.artifact_digest == artifact_digest,
                    parsed_documents.c.parser_name == parser_name,
                    parsed_documents.c.parser_version == parser_version,
                    parsed_documents.c.parser_config_reference == parser_config_reference,
                    parsed_documents.c.locator_contract_version == locator_contract_version,
                )
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            after_ordinal = None
            if after_segment_id is not None:
                ordinal = await session.execute(
                    select(parsed_document_segments.c.segment_ordinal).where(
                        parsed_document_segments.c.workspace_id == workspace_id,
                        parsed_document_segments.c.environment_id == environment_id,
                        parsed_document_segments.c.artifact_id == artifact_id,
                        parsed_document_segments.c.segment_id == after_segment_id,
                    )
                )
                after_ordinal = ordinal.scalar_one_or_none()
                if after_ordinal is None:
                    return None
            predicates = [
                parsed_document_segments.c.workspace_id == workspace_id,
                parsed_document_segments.c.environment_id == environment_id,
                parsed_document_segments.c.artifact_id == artifact_id,
            ]
            if selector is not None:
                predicates.append(
                    parsed_document_segments.c.locator == selector.model_dump(mode="json")
                )
            if after_ordinal is not None:
                predicates.append(parsed_document_segments.c.segment_ordinal > after_ordinal)
            rows = (
                await session.execute(
                    select(parsed_document_segments)
                    .where(*predicates)
                    .order_by(parsed_document_segments.c.segment_ordinal)
                    .limit(limit + 1)
                )
            ).mappings().all()
            segments = tuple(
                ParsedSegment.model_validate({
                    "schema_version": item["schema_version"],
                    "segment_id": item["segment_id"],
                    "segment_digest": item["segment_digest"],
                    "artifact_digest": row["artifact_digest"],
                    "text": item["text"],
                    "text_digest": item["text_digest"],
                    "locator": item["locator"],
                    "heading_path": tuple(item["heading_path"]),
                })
                for item in rows[:limit]
            )
            return ParsedArtifactSegmentPage(
                metadata=ParsedArtifactMetadata(
                    artifact_id=row["artifact_id"], artifact_digest=row["artifact_digest"],
                    workspace_id=row["workspace_id"], environment_id=row["environment_id"],
                    source_object_id=row["source_object_id"], source_revision_id=row["source_revision_id"],
                    source_revision_digest=row["source_revision_digest"], original_sha256=row["original_sha256"],
                    parser_name=row["parser_name"], parser_version=row["parser_version"],
                    parser_config_reference=row["parser_config_reference"],
                    locator_contract_version=row["locator_contract_version"], media_type=row["media_type"],
                    coverage=ParseCoverage(row["coverage"]),
                ),
                segments=segments,
                has_more=len(rows) > limit,
            )


class ParsedArtifactCitationExcerptResolver:
    """Resolve exact citation excerpts from the immutable parsed-artifact store."""

    def __init__(self, repository: ParsedArtifactRepository) -> None:
        self._repository = repository

    async def resolve(self, citation: Citation, *, historical: bool) -> CitationExcerpt | None:
        del historical
        evidence = citation.evidence_reference
        if (
            evidence.original_sha256 is None
            or evidence.revision_digest is None
            or evidence.locator_contract_version is None
            or evidence.locator_digest is None
        ):
            return None
        stable = StableLocator(kind=evidence.locator.kind, value=evidence.locator.value)
        if canonical_projection_locator_digest(
            locator_contract_version=evidence.locator_contract_version,
            locator=stable,
        ) != evidence.locator_digest:
            return None
        resolved = await self._repository.resolve_stable_segment(
            workspace_id=citation.workspace_id,
            environment_id=citation.environment_id,
            source_object_id=evidence.source_object_id,
            source_revision_id=evidence.source_revision_id,
            source_revision_digest=evidence.revision_digest,
            original_sha256=evidence.original_sha256,
            locator_contract_version=evidence.locator_contract_version,
            locator_kind=evidence.locator.kind,
            locator_value=evidence.locator.value,
        )
        if resolved is None:
            return None
        document, segment = resolved
        return CitationExcerpt(
            source_object_id=document.source_object_id,
            source_revision_id=document.source_revision_id,
            original_sha256=document.original_sha256,
            revision_digest=document.source_revision_digest,
            revision_number=evidence.revision_number,
            locator_schema=document.locator_contract_version,
            locator_reference=evidence.locator.value,
            locator_digest=evidence.locator_digest,
            excerpt=segment.text,
        )


def _stable_locator_key(locator: Locator) -> tuple[str, str]:
    if isinstance(locator, PhysicalLineLocator):
        return "character_range", f"line:{locator.line_number}:{locator.start_codepoint}-{locator.end_codepoint}"
    if isinstance(locator, PageTextLocator):
        return "page", f"page:{locator.page_number}:{locator.start_codepoint}-{locator.end_codepoint}"
    if isinstance(locator, XlsxCellLocator):
        return "sheet_row", f"{locator.sheet}:{locator.row}:{locator.cell_range}"
    if isinstance(locator, OoxmlTextLocator):
        path = "/".join(f"{step.kind}:{step.index}" for step in locator.block_path)
        return "custom", f"ooxml:{path}:{locator.start_codepoint}-{locator.end_codepoint}"
    raise TypeError("unsupported parsed locator")


def _document_matches_identity(document: ParsedDocument, **expected: object) -> bool:
    return all(getattr(document, name) == value for name, value in expected.items())


def _document_values(document: ParsedDocument, command: ParsedArtifactCommand) -> dict[str, Any]:
    return {
        "artifact_id": document.artifact_id, "workspace_id": document.workspace_id,
        "environment_id": document.environment_id, "source_object_id": document.source_object_id,
        "source_revision_id": document.source_revision_id, "source_revision_digest": document.source_revision_digest,
        "original_sha256": document.original_sha256, "artifact_digest": document.artifact_digest,
        "schema_version": document.schema_version, "parser_name": document.parser_name,
        "parser_version": document.parser_version, "parser_config_reference": document.parser_config_reference,
        "locator_contract_version": document.locator_contract_version, "media_type": document.media_type,
        "coverage": document.coverage.value,
        "exclusions": [item.model_dump(mode="json") for item in document.exclusions],
        "source_byte_length": document.source_byte_length, "source_codepoint_length": document.source_codepoint_length,
        "command_identity": command.command_identity, "command_digest": command.command_digest,
        "created_at": document.created_at,
    }


def _segment_values(document: ParsedDocument, segment: ParsedSegment) -> dict[str, Any]:
    ordinal = document.segments.index(segment)
    return {
        "segment_id": segment.segment_id, "artifact_id": document.artifact_id,
        "workspace_id": document.workspace_id, "environment_id": document.environment_id,
        "segment_ordinal": ordinal, "schema_version": segment.schema_version,
        "segment_digest": segment.segment_digest, "text": segment.text, "text_digest": segment.text_digest,
        "locator": segment.locator.model_dump(mode="json"), "heading_path": list(segment.heading_path),
    }


def _document_from_rows(row: Any, segment_rows: list[Any]) -> ParsedDocument:
    segments = tuple(ParsedSegment.model_validate({
        "schema_version": item["schema_version"], "segment_id": item["segment_id"],
        "segment_digest": item["segment_digest"], "artifact_digest": row["artifact_digest"],
        "text": item["text"], "text_digest": item["text_digest"], "locator": item["locator"],
        "heading_path": tuple(item["heading_path"]),
    }) for item in segment_rows)
    return ParsedDocument.model_validate({
        "schema_version": row["schema_version"], "artifact_id": row["artifact_id"],
        "artifact_digest": row["artifact_digest"], "workspace_id": row["workspace_id"],
        "environment_id": row["environment_id"], "source_object_id": row["source_object_id"],
        "source_revision_id": row["source_revision_id"], "source_revision_digest": row["source_revision_digest"],
        "original_sha256": row["original_sha256"], "parser_name": row["parser_name"],
        "parser_version": row["parser_version"], "parser_config_reference": row["parser_config_reference"],
        "locator_contract_version": row["locator_contract_version"], "media_type": row["media_type"],
        "coverage": row["coverage"], "segments": segments, "exclusions": tuple(
            ParseExclusion.model_validate(value) for value in row["exclusions"]
        ), "source_byte_length": row["source_byte_length"],
        "source_codepoint_length": row["source_codepoint_length"], "created_at": row["created_at"],
    })
