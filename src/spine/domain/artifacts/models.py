from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Annotated, Literal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field, StrictInt, field_validator, model_validator

from spine.domain.common import ActorRef, DefinitionModel, SchemaRef, SpineModel
from spine.domain.sources import SourceRevision
from spine.domain.sources.google import (
    GOOGLE_SHEET_A1_RANGE_PATTERN,
    GOOGLE_SHEET_CELL_COORDINATE_PATTERN,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    GoogleSheetRowOriginal,
    google_sheet_a1_row,
)


class ArtifactRef(SpineModel):
    id: UUID
    schema_name: str
    schema_version: str


class ArtifactState(str, Enum):
    """Whether an immutable artifact may participate in a new handoff."""

    ACTIVE = "active"
    STALE = "stale"
    TOMBSTONED = "tombstoned"


def validate_artifact_lineage(value: tuple[UUID, ...]) -> tuple[UUID, ...]:
    """Validate the direct immutable lineage shared by drafts and records."""

    if any(identifier.int == 0 for identifier in value):
        raise ValueError("upstream artifact identifiers must be non-zero")
    if len(set(value)) != len(value):
        raise ValueError("upstream artifact identifiers must be unique")
    return value


class Artifact(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    environment_id: UUID
    work_item_id: UUID
    produced_by: ActorRef
    produced_by_agent_version_id: UUID | None = None
    artifact_schema: SchemaRef
    content: Any
    evidence_refs: tuple[str, ...] = ()
    upstream_artifact_ids: tuple[UUID, ...] = ()
    state: ArtifactState = ArtifactState.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_legacy(
        cls,
        value: Mapping[str, Any],
        *,
        environment_id: UUID,
    ) -> Artifact:
        """Upgrade a pre-environment Artifact at an explicit adapter boundary."""

        if "environment_id" in value:
            raise ValueError("legacy artifact already carries an environment")
        return cls.model_validate({**value, "environment_id": environment_id})

    @field_validator(
        "id",
        "workspace_id",
        "environment_id",
        "work_item_id",
        "produced_by_agent_version_id",
    )
    @classmethod
    def reject_zero_identifiers(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("artifact identifiers must be non-zero")
        return value

    @field_validator("upstream_artifact_ids")
    @classmethod
    def validate_upstream_identifiers(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return validate_artifact_lineage(value)

    @model_validator(mode="after")
    def reject_lineage_cycle(self) -> Artifact:
        if self.id in self.upstream_artifact_ids:
            raise ValueError("artifact cannot include itself in upstream lineage")
        return self


class ParseCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class _CodepointRange(DefinitionModel):
    start_codepoint: StrictInt = Field(ge=0)
    end_codepoint: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> "_CodepointRange":
        if self.end_codepoint < self.start_codepoint:
            raise ValueError("locator end must not precede start")
        return self


class PhysicalLineLocator(_CodepointRange):
    """A half-open raw Unicode code-point range on one physical line."""

    kind: Literal["physical_line"] = "physical_line"
    line_number: StrictInt = Field(gt=0)


class PageTextLocator(_CodepointRange):
    """A half-open code-point range in text extracted from one PDF page."""

    kind: Literal["page_text"] = "page_text"
    page_number: StrictInt = Field(gt=0)


class OoxmlBlockPathStep(DefinitionModel):
    """One ordered step through a WordprocessingML body or table hierarchy."""

    kind: Literal["paragraph", "table", "row", "cell"]
    index: StrictInt = Field(ge=0)


class OoxmlTextLocator(_CodepointRange):
    """A half-open code-point range in one structurally addressed DOCX paragraph."""

    kind: Literal["ooxml_text"] = "ooxml_text"
    block_path: tuple[OoxmlBlockPathStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_block_path(self) -> "OoxmlTextLocator":
        kinds = tuple(step.kind for step in self.block_path)
        position = 0
        while position < len(kinds):
            if kinds[position] == "paragraph":
                if position != len(kinds) - 1:
                    raise ValueError("paragraph must terminate an OOXML block path")
                return self
            if kinds[position : position + 3] != ("table", "row", "cell"):
                raise ValueError("invalid OOXML block path")
            position += 3
        raise ValueError("OOXML block path must terminate at a paragraph")


class ExtractedOoxmlText(DefinitionModel):
    """Transient text for one explicitly identified OOXML paragraph path."""

    schema_version: Literal["extracted-ooxml-text:v1"] = "extracted-ooxml-text:v1"
    block_path: tuple[OoxmlBlockPathStep, ...] = Field(min_length=1)
    text: str


class ExtractedPageText(DefinitionModel):
    """Transient extracted text for one explicitly identified PDF page."""

    schema_version: Literal["extracted-page-text:v1"] = "extracted-page-text:v1"
    page_number: StrictInt = Field(gt=0)
    text: str


class XlsxCellLocator(DefinitionModel):
    """A physical worksheet cell or merged range in an XLSX revision."""

    kind: Literal["xlsx_cell"] = "xlsx_cell"
    schema_version: Literal["locator:xlsx-cell:v1"] = "locator:xlsx-cell:v1"
    sheet: str = Field(min_length=1)
    row: StrictInt = Field(gt=0)
    cell_range: str = Field(pattern=r"^[A-Z]+[1-9][0-9]*(?::[A-Z]+[1-9][0-9]*)?$")

    @model_validator(mode="after")
    def validate_coordinates(self) -> "XlsxCellLocator":
        start, end = _parse_a1_range(self.cell_range)
        if start[1] != self.row:
            raise ValueError("locator row must match the first A1 row")
        if end is not None and (end[1] < start[1] or end[0] < start[0]):
            raise ValueError("locator range must be ordered")
        return self


class GoogleSheetCellLocator(DefinitionModel):
    """One representation of a business-identified cell in an exact revision."""

    kind: Literal["google_sheet_cell"] = "google_sheet_cell"
    schema_version: Literal["locator:google-sheet-cell:v1"] = (
        "locator:google-sheet-cell:v1"
    )
    source_revision_id: UUID
    spreadsheet_id: str = Field(min_length=1, max_length=1024)
    sheet_id: StrictInt = Field(ge=0)
    immutable_business_id: str = Field(min_length=1, max_length=1024)
    column_key: str = Field(min_length=1, max_length=256)
    representation: Literal["entered", "effective", "formatted", "format"]
    observed_a1_range: str = Field(
        pattern=GOOGLE_SHEET_A1_RANGE_PATTERN,
        max_length=128,
    )
    observed_cell_coordinate: str = Field(
        pattern=GOOGLE_SHEET_CELL_COORDINATE_PATTERN,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_scope_and_hints(self) -> "GoogleSheetCellLocator":
        if self.source_revision_id.int == 0:
            raise ValueError("source_revision_id must be a non-zero UUID")
        if google_sheet_a1_row(self.observed_a1_range) != google_sheet_a1_row(
            self.observed_cell_coordinate
        ):
            raise ValueError("cell coordinate must belong to the observed row range")
        return self


Locator = Annotated[
    PhysicalLineLocator | PageTextLocator | OoxmlTextLocator | XlsxCellLocator,
    Field(discriminator="kind"),
]


class ParseExclusion(DefinitionModel):
    """A content-free reason why a portion was not parsed."""

    schema_version: Literal["parse-exclusion:v1"] = "parse-exclusion:v1"
    code: Literal[
        "invalid_utf8",
        "unsupported_structure",
        "malformed_structure",
        "bounded_input",
        "blank_page",
        "malformed_document",
        "extraction_failed",
        "unlocatable_content",
        "unsupported_story",
        "unsupported_drawing",
        "ambiguous_merged_cell",
        "formula_without_cached_value",
        "hidden_content",
        "external_link",
    ]
    count: StrictInt = Field(gt=0)
    locator: Locator | None = None


class ParsedSegment(DefinitionModel):
    schema_version: Literal["parsed-segment:v1"] = "parsed-segment:v1"
    segment_id: UUID
    segment_digest: str = Field(pattern=r"[0-9a-f]{64}")
    artifact_digest: str = Field(pattern=r"[0-9a-f]{64}")
    text: str
    text_digest: str = Field(pattern=r"[0-9a-f]{64}")
    locator: Locator
    heading_path: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_digest(self) -> "ParsedSegment":
        expected_text_digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_digest != expected_text_digest:
            raise ValueError("segment text digest does not match text")
        expected_segment_digest = segment_digest(
            artifact_digest=self.artifact_digest,
            locator=self.locator,
            text_digest=self.text_digest,
            heading_path=self.heading_path,
        )
        if self.segment_digest != expected_segment_digest:
            raise ValueError("segment digest does not match segment identity")
        if self.segment_id != uuid5(NAMESPACE_URL, f"spine:parsed-segment:v1:{self.segment_digest}"):
            raise ValueError("segment id does not match segment identity")
        return self


class ParsedDocument(DefinitionModel):
    """Immutable, digest-addressed parser output for one source revision."""

    schema_version: Literal["parsed-document:v1"] = "parsed-document:v1"
    artifact_id: UUID
    artifact_digest: str = Field(pattern=r"[0-9a-f]{64}")
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"[0-9a-f]{64}")
    original_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    parser_name: str
    parser_version: str
    parser_config_reference: str
    locator_contract_version: str
    media_type: str
    coverage: ParseCoverage
    segments: tuple[ParsedSegment, ...] = ()
    exclusions: tuple[ParseExclusion, ...] = ()
    source_byte_length: StrictInt = Field(ge=0)
    source_codepoint_length: StrictInt = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_identity(self) -> "ParsedDocument":
        if any(identifier.int == 0 for identifier in (
            self.artifact_id, self.workspace_id, self.environment_id,
            self.source_object_id, self.source_revision_id,
        )):
            raise ValueError("parsed document identifiers must be non-zero")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.coverage is ParseCoverage.COMPLETE and self.exclusions:
            raise ValueError("complete parsing cannot contain exclusions")
        if self.coverage is not ParseCoverage.COMPLETE and not self.exclusions:
            raise ValueError("partial or failed parsing requires exclusions")
        if self.coverage is ParseCoverage.FAILED and self.segments:
            raise ValueError("failed parsing cannot contain segments")
        if any(segment.artifact_digest != self.artifact_digest for segment in self.segments):
            raise ValueError("segment belongs to another parsed document")
        if self.artifact_digest != parsed_document_digest(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            source_object_id=self.source_object_id,
            source_revision_id=self.source_revision_id,
            source_revision_digest=self.source_revision_digest,
            original_sha256=self.original_sha256,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            parser_config_reference=self.parser_config_reference,
            locator_contract_version=self.locator_contract_version,
            media_type=self.media_type,
            coverage=self.coverage,
            segments=self.segments,
            exclusions=self.exclusions,
            source_byte_length=self.source_byte_length,
            source_codepoint_length=self.source_codepoint_length,
        ):
            raise ValueError("parsed document digest does not match its identity")
        return self

    def resolve_locator(
        self,
        source: str | ExtractedPageText | ExtractedOoxmlText,
        locator: Locator,
    ) -> str:
        segment = next((value for value in self.segments if value.locator == locator), None)
        if segment is None:
            raise ValueError("locator does not resolve")
        return resolve_locator(source, locator, expected_text=segment.text)

    @classmethod
    def from_source(
        cls,
        source_revision: SourceRevision,
        *,
        parser_name: str,
        parser_version: str,
        parser_config_reference: str,
        locator_contract_version: str,
        media_type: str,
        coverage: ParseCoverage,
        segments: tuple[ParsedSegment, ...],
        exclusions: tuple[ParseExclusion, ...],
        source_byte_length: int,
        source_codepoint_length: int,
    ) -> "ParsedDocument":
        digest = parsed_document_digest(
            workspace_id=source_revision.workspace_id,
            environment_id=source_revision.environment_id,
            source_object_id=source_revision.source_object_id,
            source_revision_id=source_revision.revision_id,
            source_revision_digest=source_revision.revision_digest,
            original_sha256=source_revision.original_sha256,
            parser_name=parser_name,
            parser_version=parser_version,
            parser_config_reference=parser_config_reference,
            locator_contract_version=locator_contract_version,
            media_type=media_type,
            coverage=coverage,
            segments=segments,
            exclusions=exclusions,
            source_byte_length=source_byte_length,
            source_codepoint_length=source_codepoint_length,
        )
        artifact_id = uuid5(NAMESPACE_URL, f"spine:parsed-document:v1:{digest}")
        rebound_segments = tuple(
            _build_segment(
                artifact_digest=digest,
                text=segment.text,
                text_digest=segment.text_digest,
                locator=segment.locator,
                heading_path=segment.heading_path,
                segment_digest=segment_digest(
                    artifact_digest=digest,
                    locator=segment.locator,
                    text_digest=segment.text_digest,
                    heading_path=segment.heading_path,
                ),
            )
            for segment in segments
        )
        return cls(
            artifact_id=artifact_id,
            artifact_digest=digest,
            workspace_id=source_revision.workspace_id,
            environment_id=source_revision.environment_id,
            source_object_id=source_revision.source_object_id,
            source_revision_id=source_revision.revision_id,
            source_revision_digest=source_revision.revision_digest,
            original_sha256=source_revision.original_sha256,
            parser_name=parser_name,
            parser_version=parser_version,
            parser_config_reference=parser_config_reference,
            locator_contract_version=locator_contract_version,
            media_type=media_type,
            coverage=coverage,
            segments=rebound_segments,
            exclusions=exclusions,
            source_byte_length=source_byte_length,
            source_codepoint_length=source_codepoint_length,
            created_at=source_revision.observed_at,
        )


def segment_digest(
    *, artifact_digest: str, locator: Locator, text_digest: str, heading_path: tuple[str, ...]
) -> str:
    return hashlib.sha256(_canonical_json({
        "schema_version": "parsed-segment:v1",
        "artifact_digest": artifact_digest,
        "locator": locator.model_dump(mode="json"),
        "text_digest": text_digest,
        "heading_path": heading_path,
    })).hexdigest()


def parsed_document_digest(
    *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
    source_revision_id: UUID, source_revision_digest: str, original_sha256: str,
    parser_name: str, parser_version: str, parser_config_reference: str,
    locator_contract_version: str, media_type: str, coverage: ParseCoverage,
    segments: tuple[ParsedSegment, ...], exclusions: tuple[ParseExclusion, ...],
    source_byte_length: int, source_codepoint_length: int,
) -> str:
    return hashlib.sha256(_canonical_json({
        "schema_version": "parsed-document:v1",
        "workspace_id": str(workspace_id), "environment_id": str(environment_id),
        "source_object_id": str(source_object_id), "source_revision_id": str(source_revision_id),
        "source_revision_digest": source_revision_digest, "original_sha256": original_sha256,
        "parser_name": parser_name, "parser_version": parser_version,
        "parser_config_reference": parser_config_reference,
        "locator_contract_version": locator_contract_version, "media_type": media_type,
        "coverage": coverage.value,
        "segments": [
            {"text_digest": segment.text_digest, "locator": segment.locator.model_dump(mode="json"),
             "heading_path": segment.heading_path}
            for segment in segments
        ],
        "exclusions": [exclusion.model_dump(mode="json") for exclusion in exclusions],
        "source_byte_length": source_byte_length, "source_codepoint_length": source_codepoint_length,
    })).hexdigest()


def _build_segment(
    *, artifact_digest: str, text: str, text_digest: str, locator: Locator,
    heading_path: tuple[str, ...], segment_digest: str,
) -> ParsedSegment:
    return ParsedSegment(
        segment_id=uuid5(NAMESPACE_URL, f"spine:parsed-segment:v1:{segment_digest}"),
        segment_digest=segment_digest,
        artifact_digest=artifact_digest,
        text=text,
        text_digest=text_digest,
        locator=locator,
        heading_path=heading_path,
    )


def resolve_locator(
    source: str | ExtractedPageText | ExtractedOoxmlText,
    locator: Locator,
    *,
    expected_text: str | None = None,
) -> str:
    if isinstance(locator, XlsxCellLocator):
        raise TypeError("XLSX locator source must be resolved by the XLSX parser")
    if isinstance(locator, OoxmlTextLocator):
        if not isinstance(source, ExtractedOoxmlText):
            raise TypeError("OOXML locator source must identify one paragraph path")
        if source.block_path != locator.block_path:
            raise ValueError("locator does not resolve")
        if locator.end_codepoint > len(source.text):
            raise ValueError("locator does not resolve")
        value = source.text[locator.start_codepoint:locator.end_codepoint]
        if expected_text is not None and value != expected_text:
            raise ValueError("locator does not resolve")
        return value
    if isinstance(locator, PageTextLocator):
        if not isinstance(source, ExtractedPageText):
            raise TypeError("page-text locator source must identify one extracted page")
        if source.page_number != locator.page_number:
            raise ValueError("locator does not resolve")
        source_text = source.text
        if locator.end_codepoint > len(source_text):
            raise ValueError("locator does not resolve")
        value = source_text[locator.start_codepoint:locator.end_codepoint]
        if expected_text is not None and value != expected_text:
            raise ValueError("locator does not resolve")
        return value
    if not isinstance(source, str):
        raise TypeError("physical-line locator source must be text")
    source_text = source
    starts: list[int] = []
    offset = 0
    for line in source_text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    if source_text and (not starts or offset < len(source_text)):
        starts.append(offset)
    if locator.line_number > len(starts):
        raise ValueError("locator does not resolve")
    start = starts[locator.line_number - 1]
    line_end = start + len(source_text[start:].splitlines(keepends=True)[0].rstrip("\r\n")) if source_text[start:] else start
    if locator.start_codepoint != start or locator.end_codepoint > line_end:
        raise ValueError("locator does not resolve")
    value = source_text[locator.start_codepoint:locator.end_codepoint]
    if expected_text is not None and value != expected_text:
        raise ValueError("locator does not resolve")
    return value


def resolve_google_sheet_cell_locator(
    original: GoogleSheetRowOriginal,
    locator: GoogleSheetCellLocator,
    *,
    source_revision_id: UUID,
) -> GoogleSheetCellValue | GoogleSheetNumberFormat | str:
    """Resolve a locator only against its exact immutable business row revision."""

    key = original.key
    if (
        source_revision_id != locator.source_revision_id
        or key.spreadsheet_id != locator.spreadsheet_id
        or key.sheet_id != locator.sheet_id
        or key.immutable_business_id != locator.immutable_business_id
    ):
        raise ValueError("locator does not resolve")
    cell = next(
        (item for item in original.cells if item.column_key == locator.column_key),
        None,
    )
    if cell is None:
        raise ValueError("locator does not resolve")
    if locator.representation == "entered":
        return cell.entered
    if locator.representation == "effective":
        return cell.effective
    if locator.representation == "formatted":
        return cell.formatted
    return cell.number_format


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _parse_a1_range(value: str) -> tuple[tuple[int, int], tuple[int, int] | None]:
    parts = value.split(":")
    coordinates = tuple(_parse_a1_coordinate(part) for part in parts)
    return coordinates[0], coordinates[1] if len(coordinates) == 2 else None


def _parse_a1_coordinate(value: str) -> tuple[int, int]:
    split = next((index for index, character in enumerate(value) if character.isdigit()), len(value))
    column, row = value[:split], value[split:]
    if not column or not row:
        raise ValueError("invalid A1 coordinate")
    column_number = 0
    for character in column:
        column_number = column_number * 26 + ord(character) - ord("A") + 1
    return column_number, int(row)
