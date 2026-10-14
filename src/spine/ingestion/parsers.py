"""Strict, deterministic parsers for production document artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
from io import BytesIO
import re
from typing import Final, Literal
from uuid import UUID

from spine.domain.artifacts import (
    ExtractedPageText,
    ParseCoverage,
    ParseExclusion,
    PageTextLocator,
    ParsedDocument,
    ParsedSegment,
    PhysicalLineLocator,
)
from spine.domain.sources import RevisionKind, SourceRevision, assert_revision_digest

LOCATOR_CONTRACT_VERSION: Final[str] = "physical-line-codepoint:v1"
PARSER_VERSION: Final[str] = "text-markdown:v1"
PARSER_CONFIG_REFERENCE: Final[str] = "text-markdown-default:v1"
PDF_LOCATOR_CONTRACT_VERSION: Final[str] = "pdf-page-extracted-codepoint:v1"
PDF_PARSER_VERSION: Final[str] = f"pdf:v1+pypdf-{version('pypdf')}"
PDF_EXTRACTION_MODE: Final[str] = "plain"
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


@dataclass(frozen=True, slots=True)
class PdfParser:
    """Deterministic PDF text adapter with page-scoped extracted-text locators."""

    max_bytes: int = 10_000_000
    max_pages: int = 10_000
    max_page_chars: int = 2_000_000
    max_total_chars: int = 10_000_000

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_pages, self.max_page_chars, self.max_total_chars) <= 0:
            raise ValueError("parser bounds must be positive")

    @property
    def config_reference(self) -> str:
        limits = (
            f"strict=true:mode={PDF_EXTRACTION_MODE}:"
            f"{self.max_bytes}:{self.max_pages}:"
            f"{self.max_page_chars}:{self.max_total_chars}"
        )
        digest = hashlib.sha256(limits.encode("ascii")).hexdigest()
        return f"pdf-pypdf-config:v1:{digest}"

    def parse(
        self,
        source_revision: SourceRevision,
        content: bytes,
        *,
        media_type: str = "application/pdf",
    ) -> ParsedDocument:
        _validate_pdf_source(source_revision, content)

        if len(content) > self.max_bytes:
            return _pdf_result(
                source_revision,
                content,
                media_type=media_type,
                parser_config_reference=self.config_reference,
                segments=(),
                exclusions=(ParseExclusion(code="bounded_input", count=1),),
                extracted_length=0,
            )

        if not content.startswith(b"%PDF-"):
            return _pdf_result(
                source_revision,
                content,
                media_type=media_type,
                parser_config_reference=self.config_reference,
                segments=(),
                exclusions=(ParseExclusion(code="malformed_document", count=1),),
                extracted_length=0,
            )

        from pypdf import PdfReader

        try:
            reader = PdfReader(BytesIO(content), strict=True)
        except Exception:
            return _pdf_result(
                source_revision,
                content,
                media_type=media_type,
                parser_config_reference=self.config_reference,
                segments=(),
                exclusions=(ParseExclusion(code="malformed_document", count=1),),
                extracted_length=0,
            )
        segments: list[ParsedSegment] = []
        exclusions: list[ParseExclusion] = []
        extracted_length = 0
        try:
            page_count = len(reader.pages)
            if page_count == 0:
                exclusions.append(ParseExclusion(code="malformed_document", count=1))
            if page_count > self.max_pages:
                exclusions.append(
                    ParseExclusion(code="bounded_input", count=page_count - self.max_pages)
                )
            for page_number in range(1, min(page_count, self.max_pages) + 1):
                page = reader.pages[page_number - 1]
                try:
                    page_text = page.extract_text(extraction_mode=PDF_EXTRACTION_MODE) or ""
                except Exception:
                    exclusions.append(
                        ParseExclusion(
                            code="extraction_failed",
                            count=1,
                            locator=PageTextLocator(
                                page_number=page_number,
                                start_codepoint=0,
                                end_codepoint=0,
                            ),
                        )
                    )
                    continue
                if not isinstance(page_text, str):
                    exclusions.append(
                        ParseExclusion(
                            code="unlocatable_content",
                            count=1,
                            locator=PageTextLocator(
                                page_number=page_number,
                                start_codepoint=0,
                                end_codepoint=0,
                            ),
                        )
                    )
                    continue
                if not _has_exact_unicode_text(page_text):
                    exclusions.append(
                        ParseExclusion(
                            code="unlocatable_content",
                            count=1,
                            locator=PageTextLocator(
                                page_number=page_number,
                                start_codepoint=0,
                                end_codepoint=0,
                            ),
                        )
                    )
                    continue
                if len(page_text) > self.max_page_chars:
                    exclusions.append(
                        ParseExclusion(
                            code="bounded_input",
                            count=1,
                            locator=PageTextLocator(
                                page_number=page_number,
                                start_codepoint=0,
                                end_codepoint=0,
                            ),
                        )
                    )
                    continue
                if extracted_length + len(page_text) > self.max_total_chars:
                    exclusions.append(
                        ParseExclusion(
                            code="bounded_input",
                            count=min(page_count, self.max_pages) - page_number + 1,
                        )
                    )
                    break
                extracted_length += len(page_text)
                if not page_text.strip():
                    exclusions.append(
                        ParseExclusion(
                            code="blank_page",
                            count=1,
                            locator=PageTextLocator(
                                page_number=page_number,
                                start_codepoint=0,
                                end_codepoint=0,
                            ),
                        )
                    )
                    continue
                offset = 0
                for raw_line in page_text.splitlines(keepends=True):
                    text = raw_line.rstrip("\r\n")
                    locator = PageTextLocator(
                        page_number=page_number,
                        start_codepoint=offset,
                        end_codepoint=offset + len(text),
                    )
                    offset += len(raw_line)
                    segments.append(
                        ParsedSegment.model_construct(
                            segment_id=UUID(int=1),
                            segment_digest="0" * 64,
                            artifact_digest="0" * 64,
                            text=text,
                            text_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                            locator=locator,
                            heading_path=(),
                        )
                    )
        except Exception:
            exclusions.append(ParseExclusion(code="extraction_failed", count=1))
        return _pdf_result(
            source_revision,
            content,
            media_type=media_type,
            parser_config_reference=self.config_reference,
            segments=tuple(segments),
            exclusions=tuple(exclusions),
            extracted_length=extracted_length,
        )

    def resolve_locator(
        self,
        source_revision: SourceRevision,
        content: bytes,
        document: ParsedDocument,
        locator: PageTextLocator,
    ) -> str:
        """Reopen one pinned PDF revision and resolve an exact page-text range."""

        _validate_pdf_source(source_revision, content)
        expected_identity = (
            source_revision.workspace_id,
            source_revision.environment_id,
            source_revision.source_object_id,
            source_revision.revision_id,
            source_revision.revision_digest,
            source_revision.original_sha256,
            "pdf",
            PDF_PARSER_VERSION,
            self.config_reference,
            PDF_LOCATOR_CONTRACT_VERSION,
            "application/pdf",
        )
        actual_identity = (
            document.workspace_id,
            document.environment_id,
            document.source_object_id,
            document.source_revision_id,
            document.source_revision_digest,
            document.original_sha256,
            document.parser_name,
            document.parser_version,
            document.parser_config_reference,
            document.locator_contract_version,
            document.media_type,
        )
        if actual_identity != expected_identity:
            raise ValueError("locator does not resolve")

        from pypdf import PdfReader

        try:
            reader = PdfReader(BytesIO(content), strict=True)
            page = reader.pages[locator.page_number - 1]
            page_text = page.extract_text(extraction_mode=PDF_EXTRACTION_MODE) or ""
            if not isinstance(page_text, str) or not _has_exact_unicode_text(page_text):
                raise ValueError("locator does not resolve")
            return document.resolve_locator(
                ExtractedPageText(page_number=locator.page_number, text=page_text),
                locator,
            )
        except (IndexError, TypeError, ValueError):
            raise ValueError("locator does not resolve") from None
        except Exception:
            raise ValueError("locator does not resolve") from None


def parse_document(
    source_revision: SourceRevision,
    content: bytes,
    *,
    media_type: str | None = None,
    max_bytes: int = 10_000_000,
    max_lines: int = 1_000_000,
) -> ParsedDocument:
    """Parse an immutable original with the selected production adapter."""

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
    if selected_media_type == "application/pdf":
        return PdfParser(max_bytes=max_bytes).parse(source_revision, content)
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


def _pdf_result(
    source_revision: SourceRevision,
    content: bytes,
    *,
    media_type: str,
    parser_config_reference: str,
    segments: tuple[ParsedSegment, ...],
    exclusions: tuple[ParseExclusion, ...],
    extracted_length: int,
) -> ParsedDocument:
    coverage = (
        ParseCoverage.FAILED
        if exclusions and not segments
        else ParseCoverage.PARTIAL
        if exclusions
        else ParseCoverage.COMPLETE
    )
    return ParsedDocument.from_source(
        source_revision,
        parser_name="pdf",
        parser_version=PDF_PARSER_VERSION,
        parser_config_reference=parser_config_reference,
        locator_contract_version=PDF_LOCATOR_CONTRACT_VERSION,
        media_type=media_type,
        coverage=coverage,
        segments=segments,
        exclusions=exclusions,
        source_byte_length=len(content),
        source_codepoint_length=extracted_length,
    )


def _validate_pdf_source(source_revision: SourceRevision, content: bytes) -> None:
    if source_revision.kind is not RevisionKind.CONTENT:
        raise ValueError("only content revisions can be parsed")
    assert_revision_digest(source_revision)
    if (
        len(content) != source_revision.byte_length
        or hashlib.sha256(content).hexdigest() != source_revision.original_sha256
    ):
        raise ValueError("original content does not match source revision")


def _has_exact_unicode_text(value: str) -> bool:
    if "\ufffd" in value or "\x00" in value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True
