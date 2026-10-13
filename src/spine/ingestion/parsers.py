"""Strict, deterministic TXT and Markdown parsing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Final, Literal
from uuid import UUID

from spine.domain.artifacts import (
    ParseCoverage,
    ParseExclusion,
    ParsedDocument,
    ParsedSegment,
    PhysicalLineLocator,
)
from spine.domain.sources import RevisionKind, SourceRevision, assert_revision_digest

LOCATOR_CONTRACT_VERSION: Final[str] = "physical-line-codepoint:v1"
PARSER_VERSION: Final[str] = "text-markdown:v1"
PARSER_CONFIG_REFERENCE: Final[str] = "text-markdown-default:v1"
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*#*\s*$")


@dataclass(frozen=True, slots=True)
class TextParser:
    max_bytes: int = 10_000_000
    max_lines: int = 1_000_000

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_lines <= 0:
            raise ValueError("parser bounds must be positive")

    def parse(self, source_revision: SourceRevision, content: bytes, *, media_type: str) -> ParsedDocument:
        return _parse(source_revision, content, media_type=media_type, markdown=False, limits=self)


@dataclass(frozen=True, slots=True)
class MarkdownParser(TextParser):
    def parse(self, source_revision: SourceRevision, content: bytes, *, media_type: str = "text/markdown") -> ParsedDocument:
        return _parse(source_revision, content, media_type=media_type, markdown=True, limits=self)


def parse_document(
    source_revision: SourceRevision,
    content: bytes,
    *,
    media_type: str | None = None,
    max_bytes: int = 10_000_000,
    max_lines: int = 1_000_000,
) -> ParsedDocument:
    """Parse an immutable original for the supported text formats only."""

    if source_revision.kind is not RevisionKind.CONTENT:
        raise ValueError("only content revisions can be parsed")
    assert_revision_digest(source_revision)
    selected_media_type = (media_type or source_revision.media_type or "").lower()
    if selected_media_type in {"text/markdown", "text/x-markdown"}:
        return MarkdownParser(max_bytes=max_bytes, max_lines=max_lines).parse(
            source_revision, content, media_type="text/markdown"
        )
    if selected_media_type == "text/plain":
        return TextParser(max_bytes=max_bytes, max_lines=max_lines).parse(
            source_revision, content, media_type="text/plain"
        )
    raise ValueError("unsupported parser media type")


def _parse(
    source_revision: SourceRevision,
    content: bytes,
    *,
    media_type: str,
    markdown: bool,
    limits: TextParser,
) -> ParsedDocument:
    if len(content) != source_revision.byte_length or hashlib.sha256(content).hexdigest() != source_revision.original_sha256:
        raise ValueError("original content does not match source revision")
    if len(content) > limits.max_bytes:
        return _failed_document(source_revision, content, media_type=media_type, markdown=markdown,
                                 code="bounded_input")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _failed_document(source_revision, content, media_type=media_type, markdown=markdown,
                                 code="invalid_utf8")

    lines = list(_lines(text))
    exclusions: list[ParseExclusion] = []
    if len(lines) > limits.max_lines:
        exclusions.append(ParseExclusion(code="bounded_input", count=len(lines) - limits.max_lines))
        lines = lines[:limits.max_lines]
    segments: list[ParsedSegment] = []
    headings: list[str] = []
    fence_marker: str | None = None
    unsupported_lines = 0
    for line_number, start, raw_line in lines:
        line = raw_line.rstrip("\r\n")
        heading_path = ()
        if markdown:
            stripped = line.lstrip()
            if fence_marker is not None:
                unsupported_lines += 1
                if stripped.startswith(fence_marker):
                    fence_marker = None
                continue
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_marker = stripped[:3]
                unsupported_lines += 1
                continue
            match = _HEADING.match(line)
            if match:
                level = len(match.group(1))
                title = match.group(2).strip()
                if not title:
                    exclusions.append(ParseExclusion(code="malformed_structure", count=1,
                        locator=PhysicalLineLocator(line_number=line_number, start_codepoint=start, end_codepoint=start + len(line))))
                else:
                    headings = headings[: level - 1] + [title]
                heading_path = tuple(headings)
            else:
                heading_path = tuple(headings)
        locator = PhysicalLineLocator(
            line_number=line_number,
            start_codepoint=start,
            end_codepoint=start + len(line),
        )
        text_digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
        # The document digest is the parent identity, so segment identities are
        # completed by ParsedDocument.from_source after the parent is derived.
        segments.append(ParsedSegment.model_construct(
            segment_id=UUID(int=1), segment_digest="0" * 64,
            artifact_digest="0" * 64, text=line, text_digest=text_digest,
            locator=locator, heading_path=heading_path,
        ))
    if fence_marker is not None:
        exclusions.append(ParseExclusion(code="malformed_structure", count=1))
    if unsupported_lines:
        exclusions.append(ParseExclusion(code="unsupported_structure", count=unsupported_lines))
    coverage = ParseCoverage.PARTIAL if exclusions else ParseCoverage.COMPLETE
    return ParsedDocument.from_source(
        source_revision, parser_name="markdown" if markdown else "text", parser_version=PARSER_VERSION,
        parser_config_reference=PARSER_CONFIG_REFERENCE, locator_contract_version=LOCATOR_CONTRACT_VERSION,
        media_type=media_type, coverage=coverage, segments=tuple(segments), exclusions=tuple(exclusions),
        source_byte_length=len(content), source_codepoint_length=len(text),
    )


def _lines(text: str) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    offset = 0
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        result.append((line_number, offset, line))
        offset += len(line)
    if text and (not result or offset < len(text)):
        result.append((len(result) + 1, offset, text[offset:]))
    return result


def _failed_document(
    source_revision: SourceRevision,
    content: bytes,
    *,
    media_type: str,
    markdown: bool,
    code: Literal["bounded_input", "invalid_utf8"],
) -> ParsedDocument:
    return ParsedDocument.from_source(
        source_revision,
        parser_name="markdown" if markdown else "text",
        parser_version=PARSER_VERSION,
        parser_config_reference=PARSER_CONFIG_REFERENCE,
        locator_contract_version=LOCATOR_CONTRACT_VERSION,
        media_type=media_type,
        coverage=ParseCoverage.FAILED,
        segments=(),
        exclusions=(ParseExclusion(code=code, count=1),),
        source_byte_length=len(content),
        source_codepoint_length=0,
    )
