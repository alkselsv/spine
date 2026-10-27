"""Versioned Google source identity and Sheets evidence contracts."""

from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from spine.domain.common import DefinitionModel


GOOGLE_SOURCE_IDENTITY_SCHEMA = "google-source-identity:v1"
GOOGLE_EXTERNAL_NAMESPACE = "google"
GOOGLE_EXTERNAL_GENERATION = "google-source-key:v1"
GOOGLE_SHEET_ROW_ORIGINAL_SCHEMA = "google-sheet-row-original:v1"
GOOGLE_SHEET_ROW_EXTRACTION_PROFILE = "google-sheets-row-extraction:v1"
GOOGLE_SHEET_ROW_REVISION_PROFILE = "google-sheets-row-revision:v1"
GOOGLE_SHEET_A1_RANGE_PATTERN = (
    r"^[A-Z]+[1-9][0-9]*(?::[A-Z]+[1-9][0-9]*)?$"
)
GOOGLE_SHEET_CELL_COORDINATE_PATTERN = r"^[A-Z]+[1-9][0-9]*$"

ProviderIdentifier = Annotated[str, Field(min_length=1, max_length=1024)]
BusinessIdentifier = Annotated[str, Field(min_length=1, max_length=1024)]


class _GoogleProviderKey(DefinitionModel):
    connection_id: UUID

    @model_validator(mode="after")
    def validate_connection(self) -> "_GoogleProviderKey":
        if self.connection_id.int == 0:
            raise ValueError("connection_id must be a non-zero UUID")
        return self


class GoogleDriveFileKey(_GoogleProviderKey):
    """Stable Drive identity; names, paths and content are deliberately absent."""

    kind: Literal["drive_file"] = "drive_file"
    schema_version: Literal["google-drive-file-key:v1"] = "google-drive-file-key:v1"
    file_id: ProviderIdentifier


class GoogleSheetScopeKey(_GoogleProviderKey):
    """Stable sheet scope; a mutable sheet title is not identity."""

    kind: Literal["sheet_scope"] = "sheet_scope"
    schema_version: Literal["google-sheet-scope-key:v1"] = "google-sheet-scope-key:v1"
    spreadsheet_id: ProviderIdentifier
    sheet_id: StrictInt = Field(ge=0)


class GoogleRegisterRowKey(_GoogleProviderKey):
    """Stable register-row identity independent of grid coordinates."""

    kind: Literal["register_row"] = "register_row"
    schema_version: Literal["google-register-row-key:v1"] = "google-register-row-key:v1"
    spreadsheet_id: ProviderIdentifier
    sheet_id: StrictInt = Field(ge=0)
    immutable_business_id: BusinessIdentifier


class GoogleDriveLifecycleSignal(str, Enum):
    """Typed provider signals; none is deletion authority on its own."""

    REMOVED = "removed"
    TRASHED = "trashed"
    MOVED_OUT_OF_SCOPE = "moved_out_of_scope"
    HTTP_401 = "http_401"
    HTTP_403 = "http_403"
    HTTP_404 = "http_404"
    CONFIRMED_ABSENT = "confirmed_absent"
    PRESENT = "present"
    AMBIGUOUS = "ambiguous"


GoogleProviderKey = GoogleDriveFileKey | GoogleSheetScopeKey | GoogleRegisterRowKey


class GoogleSourceIdentity(DefinitionModel):
    """Bounded opaque SourceObject fields plus their inspectable provider key."""

    schema_version: Literal[GOOGLE_SOURCE_IDENTITY_SCHEMA] = GOOGLE_SOURCE_IDENTITY_SCHEMA
    external_namespace: Literal[GOOGLE_EXTERNAL_NAMESPACE] = GOOGLE_EXTERNAL_NAMESPACE
    external_generation: Literal[GOOGLE_EXTERNAL_GENERATION] = GOOGLE_EXTERNAL_GENERATION
    external_object_id: str = Field(pattern=r"^gsk1:[a-z2-7]{52}$", max_length=64)
    provider_key: GoogleProviderKey = Field(discriminator="kind")

    @model_validator(mode="after")
    def validate_opaque_identifier(self) -> "GoogleSourceIdentity":
        expected = _encode_google_source_key(self.provider_key)
        if self.external_object_id != expected:
            raise ValueError("external_object_id does not match provider key")
        return self


class GoogleSheetCellValue(DefinitionModel):
    """One typed Sheets value after deterministic v1 extraction."""

    kind: Literal["blank", "string", "number", "boolean", "formula", "error"]
    value: StrictStr | StrictBool | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "GoogleSheetCellValue":
        if self.kind == "blank":
            if self.value is not None:
                raise ValueError("blank cell value must be null")
            return self
        if self.kind == "boolean":
            if not isinstance(self.value, bool):
                raise ValueError("boolean cell value must be a boolean")
            return self
        if not isinstance(self.value, str):
            raise ValueError(f"{self.kind} cell value must be a string")
        if self.kind in {"formula", "error"} and not self.value:
            raise ValueError(f"{self.kind} cell value must not be empty")
        if self.kind == "number":
            object.__setattr__(self, "value", _canonical_decimal(self.value))
        return self


class GoogleSheetNumberFormat(DefinitionModel):
    """The selected Google number-format representation for one cell."""

    type: str = Field(min_length=1, max_length=64)
    pattern: str | None = Field(default=None, max_length=1024)


class GoogleSheetCell(DefinitionModel):
    """Revision-bearing representations of one configured register column."""

    column_key: str = Field(min_length=1, max_length=256)
    entered: GoogleSheetCellValue
    effective: GoogleSheetCellValue
    formatted: str = Field(max_length=16_384)
    number_format: GoogleSheetNumberFormat


class GoogleSheetRowOriginal(DefinitionModel):
    """Canonical immutable row original; mutable grid hints are excluded."""

    schema_version: Literal[GOOGLE_SHEET_ROW_ORIGINAL_SCHEMA] = (
        GOOGLE_SHEET_ROW_ORIGINAL_SCHEMA
    )
    extraction_profile: Literal[GOOGLE_SHEET_ROW_EXTRACTION_PROFILE] = (
        GOOGLE_SHEET_ROW_EXTRACTION_PROFILE
    )
    revision_profile: Literal[GOOGLE_SHEET_ROW_REVISION_PROFILE] = (
        GOOGLE_SHEET_ROW_REVISION_PROFILE
    )
    key: GoogleRegisterRowKey
    spreadsheet_locale: str = Field(min_length=1, max_length=128)
    spreadsheet_time_zone: str = Field(min_length=1, max_length=128)
    cells: tuple[GoogleSheetCell, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_columns(self) -> "GoogleSheetRowOriginal":
        keys = tuple(cell.column_key for cell in self.cells)
        if len(set(keys)) != len(keys):
            raise ValueError("column_key values must be unique")
        return self


class GoogleSheetRowLocation(DefinitionModel):
    """Append-only observation hints which never participate in row identity."""

    schema_version: Literal["google-sheet-row-location:v1"] = (
        "google-sheet-row-location:v1"
    )
    sheet_title: str = Field(min_length=1, max_length=1024)
    observed_row_number: StrictInt = Field(gt=0)
    observed_a1_range: str = Field(
        pattern=GOOGLE_SHEET_A1_RANGE_PATTERN,
        max_length=128,
    )
    developer_metadata_id: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_row_hint(self) -> "GoogleSheetRowLocation":
        if google_sheet_a1_row(self.observed_a1_range) != self.observed_row_number:
            raise ValueError("observed A1 range must match observed_row_number")
        return self


class GoogleSheetRowObservation(DefinitionModel):
    """One row original paired with non-revision-bearing provider hints."""

    schema_version: Literal["google-sheet-row-observation:v1"] = (
        "google-sheet-row-observation:v1"
    )
    original: GoogleSheetRowOriginal
    location: GoogleSheetRowLocation
    observed_at: datetime

    @model_validator(mode="after")
    def validate_observed_at(self) -> "GoogleSheetRowObservation":
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


def google_source_identity(key: GoogleProviderKey) -> GoogleSourceIdentity:
    """Map one exact Google provider key to bounded SourceObject identity fields."""

    return GoogleSourceIdentity(
        external_object_id=_encode_google_source_key(key),
        provider_key=key,
    )


def google_sheet_row_original_bytes(
    original: GoogleSheetRowOriginal,
) -> bytes:
    """Serialize the exact revision-bearing row data under the pinned v1 profile."""

    payload = original.model_dump(mode="json")
    payload["cells"] = [
        cell.model_dump(mode="json")
        for cell in sorted(original.cells, key=lambda item: item.column_key)
    ]
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def google_sheet_row_content_digest(
    value: GoogleSheetRowOriginal | GoogleSheetRowObservation,
) -> str:
    """Digest only immutable row content, never coordinate/title provenance."""

    original = value.original if isinstance(value, GoogleSheetRowObservation) else value
    return hashlib.sha256(google_sheet_row_original_bytes(original)).hexdigest()


def google_sheet_a1_row(value: str) -> int:
    """Return the one row addressed by a validated single-row A1 range."""

    if re.fullmatch(GOOGLE_SHEET_A1_RANGE_PATTERN, value) is None:
        raise ValueError("Google Sheets A1 value is invalid")
    rows = tuple(
        int(item) for item in re.findall(r"[A-Z]+([1-9][0-9]*)", value)
    )
    if not rows or any(row != rows[0] for row in rows):
        raise ValueError("Google Sheets A1 range must address one row")
    return rows[0]


def _encode_google_source_key(key: GoogleProviderKey) -> str:
    payload = key.model_dump(mode="json")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"gsk1:{encoded}"


def _canonical_decimal(value: str) -> str:
    if len(value) > 512:
        raise ValueError("number cell value exceeds the extraction profile bound")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("number cell value must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError("number cell value must be a finite decimal")
    if number == 0:
        return "0"
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical


__all__ = [
    "GOOGLE_EXTERNAL_GENERATION",
    "GOOGLE_EXTERNAL_NAMESPACE",
    "GOOGLE_SHEET_A1_RANGE_PATTERN",
    "GOOGLE_SHEET_CELL_COORDINATE_PATTERN",
    "GOOGLE_SHEET_ROW_EXTRACTION_PROFILE",
    "GOOGLE_SHEET_ROW_ORIGINAL_SCHEMA",
    "GOOGLE_SHEET_ROW_REVISION_PROFILE",
    "GOOGLE_SOURCE_IDENTITY_SCHEMA",
    "GoogleDriveFileKey",
    "GoogleDriveLifecycleSignal",
    "GoogleProviderKey",
    "GoogleRegisterRowKey",
    "GoogleSheetCell",
    "GoogleSheetCellValue",
    "GoogleSheetNumberFormat",
    "GoogleSheetRowLocation",
    "GoogleSheetRowObservation",
    "GoogleSheetRowOriginal",
    "GoogleSheetScopeKey",
    "GoogleSourceIdentity",
    "google_sheet_row_content_digest",
    "google_sheet_row_original_bytes",
    "google_sheet_a1_row",
    "google_source_identity",
]
