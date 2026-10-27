"""Governed, row-aware Google Sheets reading behind trusted admission.

The adapter models the Sheets API's spreadsheet/sheet/grid resources directly.
It never uses Drive CSV export and stops at a normalized connector observation;
canonical persistence and projection remain owned by the shared handoff.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Protocol
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, model_validator

from spine.application.connections.google import (
    GoogleProviderContractError,
    GoogleProvider,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.connectors.observations import (
    ConnectorCoverage,
    NormalizedConnectorObservation,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.connections.google import GoogleConnection, OpaqueGoogleReference
from spine.domain.sources.google import (
    GOOGLE_SHEET_CELL_COORDINATE_PATTERN,
    GoogleRegisterRowKey,
    GoogleSheetCell,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    GoogleSheetRowLocation,
    GoogleSheetRowOriginal,
    google_sheet_a1_row,
    google_sheet_row_content_digest,
    google_sheet_row_original_bytes,
    google_source_identity,
)

GOOGLE_SHEETS_ROW_LOCATOR = "locator:google-sheet-cell:v1"
GOOGLE_SHEETS_ROW_MEDIA_TYPE = "application/vnd.spine.google-sheet-row+json"
GOOGLE_SHEETS_PROVIDER_MEDIA_TYPE = "application/vnd.google-apps.spreadsheet"
_GOOGLE_SHEET_COLUMN_PATTERN = r"^[A-Z]+$"


class GoogleSheetsUnsafeIdentityError(GoogleProviderContractError):
    """A row cannot be safely associated with a stable register identity."""

    def __init__(self, message: str, *, code: str = "invalid_identity") -> None:
        super().__init__(message)
        self.code = code


class GoogleSheetsConfiguredScopeError(GoogleProviderContractError):
    """The provider response is outside the configured spreadsheet/sheet."""


class GoogleSheetsConcurrentModificationError(GoogleProviderContractError):
    """The spreadsheet changed while its configured ranges were being read."""


class GoogleSheetsColumn(DefinitionModel):
    """One explicitly configured register column and its Sheets grid column."""

    column_key: StrictStr = Field(min_length=1, max_length=256)
    coordinate: StrictStr = Field(pattern=_GOOGLE_SHEET_COLUMN_PATTERN, max_length=4)


class GoogleSheetsReadProfile(DefinitionModel):
    """Immutable extraction and identity policy for one register scope."""

    spreadsheet_id: StrictStr = Field(min_length=1, max_length=1024)
    sheet_id: StrictInt = Field(ge=0)
    business_id_column: StrictStr = Field(min_length=1, max_length=256)
    columns: tuple[GoogleSheetsColumn, ...] = Field(min_length=1, max_length=4096)
    developer_metadata_key: StrictStr | None = Field(default=None, max_length=256)
    target_business_id: StrictStr | None = Field(default=None, max_length=1024)
    extraction_profile: VersionIdentifier = "google-sheets-row-extraction:v1"

    @model_validator(mode="after")
    def validate_columns(self) -> "GoogleSheetsReadProfile":
        keys = tuple(column.column_key for column in self.columns)
        coordinates = tuple(column.coordinate for column in self.columns)
        if len(set(keys)) != len(keys):
            raise ValueError("configured Sheets columns must be unique")
        if len(set(coordinates)) != len(coordinates):
            raise ValueError("configured Sheets coordinates must be unique")
        if self.business_id_column not in keys:
            raise ValueError("business_id_column must be configured")
        return self


class GoogleSheetsCellData(DefinitionModel):
    """The four revision-bearing representations returned for one cell."""

    column_key: StrictStr = Field(min_length=1, max_length=256)
    coordinate: StrictStr = Field(
        pattern=GOOGLE_SHEET_CELL_COORDINATE_PATTERN, max_length=16
    )
    entered: GoogleSheetCellValue
    effective: GoogleSheetCellValue
    formatted: StrictStr = Field(max_length=16_384)
    number_format: GoogleSheetNumberFormat


class GoogleSheetsDeveloperMetadata(DefinitionModel):
    """One row-attached Developer Metadata entry from the Sheets API."""

    metadata_id: StrictInt = Field(ge=0)
    key: StrictStr = Field(min_length=1, max_length=256)
    value: StrictStr | None = Field(default=None, max_length=1024)
    row_number: StrictInt = Field(gt=0)


class GoogleSheetsRowData(DefinitionModel):
    """One observed grid row with its current provider locators."""

    row_number: StrictInt = Field(gt=0)
    a1_range: StrictStr = Field(max_length=128)
    cells: tuple[GoogleSheetsCellData, ...] = Field(min_length=1, max_length=4096)
    developer_metadata: tuple[GoogleSheetsDeveloperMetadata, ...] = Field(
        default=(), max_length=128
    )

    @model_validator(mode="after")
    def validate_location(self) -> "GoogleSheetsRowData":
        if google_sheet_a1_row(self.a1_range) != self.row_number:
            raise ValueError("Sheets row A1 range must match row_number")
        keys = tuple(cell.column_key for cell in self.cells)
        coordinates = tuple(cell.coordinate for cell in self.cells)
        if len(set(keys)) != len(keys):
            raise ValueError("Sheets row cell column keys must be unique")
        if len(set(coordinates)) != len(coordinates):
            raise ValueError("Sheets row cell coordinates must be unique")
        if any(
            google_sheet_a1_row(cell.coordinate) != self.row_number
            for cell in self.cells
        ):
            raise ValueError("Sheets cell coordinate must match row_number")
        return self


class GoogleSheetsSnapshot(DefinitionModel):
    """The bounded result of one Sheets spreadsheet/sheet read."""

    spreadsheet_id: StrictStr = Field(min_length=1, max_length=1024)
    sheet_id: StrictInt = Field(ge=0)
    sheet_title: StrictStr = Field(min_length=1, max_length=1024)
    locale: StrictStr = Field(min_length=1, max_length=128)
    time_zone: StrictStr = Field(min_length=1, max_length=128)
    rows: tuple[GoogleSheetsRowData, ...] = Field(max_length=4096)


class GoogleSheetsProviderPage(DefinitionModel):
    """One version-guarded page read from an explicitly configured A1 range."""

    spreadsheet_id: StrictStr = Field(min_length=1, max_length=1024)
    sheet_id: StrictInt = Field(ge=0)
    a1_range: StrictStr = Field(min_length=1, max_length=1024)
    page_token: StrictStr | None = Field(default=None, max_length=4096)
    next_page_token: StrictStr | None = Field(default=None, max_length=4096)
    complete: bool
    provider_version_before: StrictStr = Field(min_length=1, max_length=128)
    provider_version_after: StrictStr = Field(min_length=1, max_length=128)
    snapshot: GoogleSheetsSnapshot
    observations: tuple[GoogleSheetsObservation, ...] = ()
    identity_rejection_codes: tuple[StrictStr, ...] = Field(default=(), max_length=32)


class GoogleSheetsRangePage(DefinitionModel):
    """Raw page response returned by the fakeable Sheets range service."""

    spreadsheet_id: StrictStr = Field(min_length=1, max_length=1024)
    sheet_id: StrictInt = Field(ge=0)
    a1_range: StrictStr = Field(min_length=1, max_length=1024)
    page_token: StrictStr | None = Field(default=None, max_length=4096)
    next_page_token: StrictStr | None = Field(default=None, max_length=4096)
    complete: bool
    provider_version_before: StrictStr = Field(min_length=1, max_length=128)
    provider_version_after: StrictStr = Field(min_length=1, max_length=128)
    snapshot: GoogleSheetsSnapshot


class GoogleSheetsService(Protocol):
    """Fakeable boundary for the official Sheets spreadsheet read semantics."""

    async def get_provider_version(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        spreadsheet_id: str,
    ) -> str: ...

    async def get_spreadsheet(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        spreadsheet_id: str,
        sheet_id: int,
    ) -> GoogleSheetsSnapshot: ...

    async def get_spreadsheet_range_page(
        self,
        connection: GoogleConnection,
        resource_scope_reference: OpaqueGoogleReference,
        spreadsheet_id: str,
        sheet_id: int,
        a1_range: str,
        page_token: str | None,
    ) -> GoogleSheetsRangePage: ...


class _ConnectorReadCommand(Protocol):
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int
    configuration_generation: int


class GoogleSheetsObservation(DefinitionModel):
    """One validated row observation before the shared canonical handoff."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    original: GoogleSheetRowOriginal
    location: GoogleSheetRowLocation
    provider_version_before: StrictStr = Field(min_length=1, max_length=128)
    provider_version_after: StrictStr = Field(min_length=1, max_length=128)
    observed_at: datetime
    received_at: datetime

    @model_validator(mode="after")
    def validate_observation(self) -> "GoogleSheetsObservation":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        for value in (self.observed_at, self.received_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Sheets observation timestamps must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("received_at cannot precede observed_at")
        if self.provider_version_before != self.provider_version_after:
            raise ValueError("Sheets provider version must remain stable during a read")
        return self

    def to_normalized_observation(self) -> NormalizedConnectorObservation:
        digest = google_sheet_row_content_digest(self.original)
        location_digest = hashlib.sha256(
            self.location.model_dump_json().encode("utf-8")
        ).hexdigest()
        key = self.original.key
        locator_value = _row_locator_value(self.location)
        event_identity_digest = hashlib.sha256(
            f"{digest}:{location_digest}".encode("ascii")
        ).hexdigest()
        return NormalizedConnectorObservation(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            connection_id=self.connection_id,
            resource_scope_reference=self.resource_scope_reference,
            authorization_generation=self.authorization_generation,
            configuration_generation=self.configuration_generation,
            source_kind="record",
            external_identity=google_source_identity(key),
            original_bytes=google_sheet_row_original_bytes(self.original),
            media_type=GOOGLE_SHEETS_ROW_MEDIA_TYPE,
            extraction_profile=self.original.extraction_profile,
            locator_contract_version=GOOGLE_SHEETS_ROW_LOCATOR,
            coverage=ConnectorCoverage.COMPLETE,
            producer_reference="google-sheets-reader",
            event_identity=f"sheet-row:{event_identity_digest[:54]}",
            event_digest=digest,
            origin_locator_kind="google-sheet-row",
            origin_locator_value=locator_value,
            origin_locator_schema="google-sheet-row-location:v1",
            origin_locator_digest=location_digest,
            observer_service="google-sheets-reader",
            received_at=self.received_at,
            observed_at=self.observed_at,
            provider_media_type=GOOGLE_SHEETS_PROVIDER_MEDIA_TYPE,
            provider_version_before=self.provider_version_before,
            provider_version_after=self.provider_version_after,
        )


@dataclass(frozen=True, slots=True)
class GoogleSheetsProviderAdapter(GoogleProvider[GoogleSheetsObservation]):
    """Read one configured row through Sheets, never through Drive export."""

    service: GoogleSheetsService
    profile: GoogleSheetsReadProfile
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def read(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
    ) -> GoogleSheetsObservation:
        version_before = await self.service.get_provider_version(
            connection,
            command.resource_scope_reference,
            self.profile.spreadsheet_id,
        )
        snapshot = await self.service.get_spreadsheet(
            connection,
            command.resource_scope_reference,
            self.profile.spreadsheet_id,
            self.profile.sheet_id,
        )
        version_after = await self.service.get_provider_version(
            connection,
            command.resource_scope_reference,
            self.profile.spreadsheet_id,
        )
        if (
            not version_before
            or len(version_before) > 128
            or version_before != version_after
        ):
            raise GoogleSheetsConcurrentModificationError(
                "Google Sheets spreadsheet changed during guarded read."
            )
        if snapshot.spreadsheet_id != self.profile.spreadsheet_id:
            raise GoogleSheetsConfiguredScopeError(
                "Sheets response is outside the configured spreadsheet."
            )
        if snapshot.sheet_id != self.profile.sheet_id:
            raise GoogleSheetsConfiguredScopeError(
                "Sheets response is outside the configured sheet."
            )
        selected_rows = self._select_rows(snapshot, connection.connection_id)
        if len(selected_rows) != 1:
            raise GoogleSheetsUnsafeIdentityError(
                "configured Sheets scope must contain exactly one row"
            )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Google Sheets reader clock must be timezone-aware")
        return self._observation(
            connection,
            command,
            snapshot,
            selected_rows[0],
            version_before,
            version_after,
            now,
        )

    async def read_page(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
    ) -> GoogleSheetsProviderPage:
        if command.a1_range is None:
            raise GoogleSheetsConfiguredScopeError(
                "Sheets reconciliation requires an explicitly configured A1 range."
            )
        if self.profile.target_business_id is not None:
            raise GoogleSheetsConfiguredScopeError(
                "Sheets reconciliation cannot use a row-targeted profile."
            )
        response = await self.service.get_spreadsheet_range_page(
            connection,
            command.resource_scope_reference,
            self.profile.spreadsheet_id,
            self.profile.sheet_id,
            command.a1_range,
            command.provider_page_token,
        )
        if (
            response.spreadsheet_id != self.profile.spreadsheet_id
            or response.sheet_id != self.profile.sheet_id
            or response.a1_range != command.a1_range
            or response.page_token != command.provider_page_token
            or response.snapshot.spreadsheet_id != self.profile.spreadsheet_id
            or response.snapshot.sheet_id != self.profile.sheet_id
        ):
            raise GoogleSheetsConfiguredScopeError(
                "Sheets range page is outside its configured spreadsheet/sheet/range."
            )
        if (
            not response.provider_version_before
            or response.provider_version_before != response.provider_version_after
        ):
            raise GoogleSheetsConcurrentModificationError(
                "Google Sheets spreadsheet changed during guarded range read."
            )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Google Sheets reader clock must be timezone-aware")
        identity_rejection_codes: tuple[str, ...] = ()
        try:
            selected_rows = self._select_rows(
                response.snapshot, connection.connection_id
            )
        except GoogleSheetsUnsafeIdentityError as error:
            # A page with any unsafe identity is wholly withheld. Only a bounded
            # code crosses the adapter boundary; provider values stay local.
            selected_rows = []
            identity_rejection_codes = (error.code,)
        observations = tuple(
            self._observation(
                connection,
                command,
                response.snapshot,
                selected,
                response.provider_version_before,
                response.provider_version_after,
                now,
            )
            for selected in selected_rows
        )
        return GoogleSheetsProviderPage(
            spreadsheet_id=response.spreadsheet_id,
            sheet_id=response.sheet_id,
            a1_range=response.a1_range,
            page_token=response.page_token,
            next_page_token=response.next_page_token,
            complete=response.complete,
            provider_version_before=response.provider_version_before,
            provider_version_after=response.provider_version_after,
            snapshot=response.snapshot,
            observations=observations,
            identity_rejection_codes=identity_rejection_codes,
        )

    def _observation(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
        snapshot: GoogleSheetsSnapshot,
        selected: tuple[GoogleSheetRowOriginal, GoogleSheetRowLocation],
        version_before: str,
        version_after: str,
        observed_at: datetime,
    ) -> GoogleSheetsObservation:
        return GoogleSheetsObservation(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            connection_id=connection.connection_id,
            resource_scope_reference=command.resource_scope_reference.value,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            original=selected[0],
            location=selected[1],
            provider_version_before=version_before,
            provider_version_after=version_after,
            observed_at=observed_at,
            received_at=observed_at,
        )

    def _select_rows(
        self, snapshot: GoogleSheetsSnapshot, connection_id: UUID
    ) -> list[tuple[GoogleSheetRowOriginal, GoogleSheetRowLocation]]:
        identities: dict[str, int] = {}
        candidates: list[tuple[GoogleSheetRowOriginal, GoogleSheetRowLocation]] = []
        configured = {column.column_key: column for column in self.profile.columns}
        for row in snapshot.rows:
            cells = {cell.column_key: cell for cell in row.cells}
            if set(configured) - set(cells):
                missing = sorted(set(configured) - set(cells))[0]
                raise GoogleSheetsUnsafeIdentityError(
                    "configured Sheets column is missing",
                    code="missing_business_id" if missing == self.profile.business_id_column else "invalid_identity",
                )
            for column in self.profile.columns:
                if (
                    _column_from_coordinate(cells[column.column_key].coordinate)
                    != column.coordinate
                ):
                    raise GoogleSheetsUnsafeIdentityError(
                        "configured Sheets column coordinate does not match the observed cell"
                    )
            business_cell = cells[self.profile.business_id_column]
            business_id = _business_id(business_cell.entered, business_cell.effective)
            if business_id in identities:
                raise GoogleSheetsUnsafeIdentityError(
                    "duplicate business ID in the observed Sheets scope",
                    code="duplicate_business_id",
                )
            identities[business_id] = row.row_number
            metadata_id = self._validate_metadata(row, business_id)
            original = GoogleSheetRowOriginal(
                key=GoogleRegisterRowKey(
                    connection_id=connection_id,
                    spreadsheet_id=snapshot.spreadsheet_id,
                    sheet_id=snapshot.sheet_id,
                    immutable_business_id=business_id,
                ),
                spreadsheet_locale=snapshot.locale,
                spreadsheet_time_zone=snapshot.time_zone,
                cells=tuple(
                    GoogleSheetCell(
                        column_key=column.column_key,
                        entered=cells[column.column_key].entered,
                        effective=cells[column.column_key].effective,
                        formatted=cells[column.column_key].formatted,
                        number_format=cells[column.column_key].number_format,
                    )
                    for column in self.profile.columns
                ),
            )
            candidates.append(
                (
                    original,
                    GoogleSheetRowLocation(
                        sheet_title=snapshot.sheet_title,
                        observed_row_number=row.row_number,
                        observed_a1_range=row.a1_range,
                        developer_metadata_id=metadata_id,
                    ),
                )
            )
        if not candidates:
            raise GoogleSheetsUnsafeIdentityError(
                "business ID is missing from the observed Sheets scope",
                code="missing_business_id",
            )
        if self.profile.target_business_id is not None:
            candidates = [
                candidate
                for candidate in candidates
                if candidate[0].key.immutable_business_id
                == self.profile.target_business_id
            ]
            if not candidates:
                raise GoogleSheetsUnsafeIdentityError(
                    "configured business ID is missing from the observed Sheets scope"
                )
        return candidates

    def _validate_metadata(
        self, row: GoogleSheetsRowData, business_id: str
    ) -> int | None:
        if self.profile.developer_metadata_key is None:
            return None
        matches = tuple(
            item
            for item in row.developer_metadata
            if item.key == self.profile.developer_metadata_key
        )
        if (
            len(matches) != 1
            or matches[0].value != business_id
            or matches[0].row_number != row.row_number
        ):
            raise GoogleSheetsUnsafeIdentityError(
                "configured Developer Metadata does not match the business ID",
                code="developer_metadata_mismatch",
            )
        return matches[0].metadata_id


@dataclass(frozen=True, slots=True)
class GoogleSheetsObservationReader:
    """Adapt admitted Sheets observations to the shared connector handoff."""

    admission: GoogleReadAdmission[GoogleSheetsObservation]

    async def read(
        self,
        command: _ConnectorReadCommand,
        context: TrustedPersistenceContext,
    ) -> NormalizedConnectorObservation:
        observation = await self.admission.read(
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
            ),
            context,
        )
        return observation.to_normalized_observation()


@dataclass(frozen=True, slots=True)
class GoogleSheetsReconciliationProviderAdapter(
    GoogleProvider[GoogleSheetsProviderPage]
):
    """Expose the paginated range read through the Google admission port."""

    service: GoogleSheetsService
    profiles: tuple[GoogleSheetsReadProfile, ...]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        keys = tuple(
            (profile.spreadsheet_id, profile.sheet_id) for profile in self.profiles
        )
        if len(set(keys)) != len(keys):
            raise ValueError(
                "Google Sheets reconciliation profiles must be unique by sheet"
            )

    async def read(
        self, connection: GoogleConnection, command: GoogleReadCommand
    ) -> GoogleSheetsProviderPage:
        if command.resource_object_reference is None or command.sheet_id is None:
            raise GoogleSheetsConfiguredScopeError(
                "Sheets range read requires a spreadsheet and numeric sheet ID."
            )
        profile = next(
            (
                candidate
                for candidate in self.profiles
                if candidate.spreadsheet_id == command.resource_object_reference
                and candidate.sheet_id == command.sheet_id
            ),
            None,
        )
        if profile is None:
            raise GoogleSheetsConfiguredScopeError(
                "Sheets range read is outside the configured profiles."
            )
        provider = GoogleSheetsProviderAdapter(
            service=self.service, profile=profile, clock=self.clock
        )
        return await provider.read_page(connection, command)


GoogleSheetsProvider = GoogleSheetsProviderAdapter
GoogleSheetsReader = GoogleSheetsObservationReader


def _business_id(entered: GoogleSheetCellValue, effective: GoogleSheetCellValue) -> str:
    if entered.kind != "string" or not isinstance(entered.value, str):
        raise GoogleSheetsUnsafeIdentityError(
            "business ID must be a non-empty entered string",
            code="missing_business_id",
        )
    value = entered.value
    if not value or not value.strip():
        raise GoogleSheetsUnsafeIdentityError(
            "business ID is missing", code="missing_business_id"
        )
    if effective.kind != "string" or effective.value != value:
        raise GoogleSheetsUnsafeIdentityError(
            "conflicting business ID representations", code="conflicting_business_id"
        )
    return value


def _column_from_coordinate(coordinate: str) -> str:
    return re.sub(r"[1-9][0-9]*$", "", coordinate)


def _row_locator_value(location: GoogleSheetRowLocation) -> str:
    """Keep the observed row and A1 coordinate readable in revision provenance."""

    value = (
        f"sheet-row:{location.observed_row_number}:"
        f"a1:{location.observed_a1_range.lower()}"
    )
    if re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", value) is None:
        raise ValueError("Google Sheets row locator exceeds the provenance bound")
    return value


__all__ = [
    "GOOGLE_SHEETS_PROVIDER_MEDIA_TYPE",
    "GOOGLE_SHEETS_ROW_LOCATOR",
    "GOOGLE_SHEETS_ROW_MEDIA_TYPE",
    "GoogleSheetsCellData",
    "GoogleSheetsColumn",
    "GoogleSheetsConfiguredScopeError",
    "GoogleSheetsConcurrentModificationError",
    "GoogleSheetsDeveloperMetadata",
    "GoogleSheetsObservation",
    "GoogleSheetsObservationReader",
    "GoogleSheetsReconciliationProviderAdapter",
    "GoogleSheetsProvider",
    "GoogleSheetsProviderAdapter",
    "GoogleSheetsReadProfile",
    "GoogleSheetsReader",
    "GoogleSheetsRowData",
    "GoogleSheetsService",
    "GoogleSheetsSnapshot",
    "GoogleSheetsUnsafeIdentityError",
]
