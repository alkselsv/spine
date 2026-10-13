from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

import pytest

from spine.domain.artifacts import (
    ParseCoverage,
    ParseExclusion,
    ParsedDocument,
    PhysicalLineLocator,
    resolve_locator,
)
from spine.domain.sources import RevisionMetadata, SourceObject, SourceRevision, content_revision
from spine.domain.sources.original_reference import ObjectReference


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SOURCE_OBJECT = UUID(int=3)
REVISION = UUID(int=4)


def revision(
    content: bytes,
    *,
    media_type: str = "text/plain",
    workspace_id: UUID = WORKSPACE,
    environment_id: UUID = ENVIRONMENT,
) -> SourceRevision:
    digest = sha256(content).hexdigest()
    return content_revision(
        revision_id=REVISION,
        source_object_id=SOURCE_OBJECT,
        workspace_id=workspace_id,
        environment_id=environment_id,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=RevisionMetadata(embedded_title="fixture"),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=UUID(int=5),
            storage_generation=UUID(int=6),
            digest_algorithm="sha256",
            digest_hex=digest,
            byte_length=len(content),
        ),
        original_sha256=digest,
        byte_length=len(content),
        media_type=media_type,
        observed_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
    )


def test_parsed_document_identity_is_deterministic_and_revision_scoped() -> None:
    from spine.ingestion.parsers import parse_document

    content = "α\r\n\r\nrepeat\r\nrepeat\r\n"
    source = revision(content.encode())

    first = parse_document(source, content.encode())
    second = parse_document(source, content.encode())

    assert first == second
    assert first.coverage is ParseCoverage.COMPLETE
    assert [segment.text for segment in first.segments] == ["α", "", "repeat", "repeat"]
    assert first.segments[2].locator != first.segments[3].locator
    assert resolve_locator(content, first.segments[2].locator) == "repeat"
    assert resolve_locator(content, first.segments[3].locator) == "repeat"
    assert first.artifact_digest == first.model_copy(update={"created_at": first.created_at}).artifact_digest


def test_locator_rejects_resolution_against_another_revision_text() -> None:
    from spine.ingestion.parsers import parse_document

    source = revision(b"same\n")
    document = parse_document(source, b"same\n")
    locator = document.segments[0].locator

    with pytest.raises(ValueError, match="locator does not resolve"):
        document.resolve_locator("different\n", locator)


def test_markdown_keeps_heading_path_as_context_only() -> None:
    from spine.ingestion.parsers import parse_document

    content = b"# One\ntext\n## Two\ntext\n"
    document = parse_document(revision(content, media_type="text/markdown"), content)

    assert document.coverage is ParseCoverage.COMPLETE
    assert document.segments[1].heading_path == ("One",)
    assert document.segments[3].heading_path == ("One", "Two")
    assert document.segments[1].locator.line_number == 2


def test_markdown_fenced_structure_is_excluded_from_complete_coverage() -> None:
    from spine.ingestion.parsers import parse_document

    content = b"before\n```python\nsecret\n```\nafter\n"
    document = parse_document(revision(content, media_type="text/markdown"), content)

    assert document.coverage is ParseCoverage.PARTIAL
    assert any(item.code == "unsupported_structure" for item in document.exclusions)
    assert [segment.text for segment in document.segments] == ["before", "after"]


def test_malformed_utf8_is_failed_and_never_complete() -> None:
    from spine.ingestion.parsers import parse_document

    content = b"valid\n\xff\n"
    document = parse_document(revision(content), content)

    assert document.coverage is ParseCoverage.FAILED
    assert document.segments == ()
    assert document.exclusions == (
        ParseExclusion(code="invalid_utf8", count=1),
    )


def test_locator_round_trip_uses_raw_codepoint_offsets() -> None:
    locator = PhysicalLineLocator(
        line_number=2,
        start_codepoint=3,
        end_codepoint=5,
    )

    assert resolve_locator("ж\r\néx\n", locator) == "éx"
