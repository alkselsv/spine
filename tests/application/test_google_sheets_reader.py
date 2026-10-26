from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.connectors.google_sheets import (
    GoogleSheetsCellData,
    GoogleSheetsDeveloperMetadata,
    GoogleSheetsProviderAdapter,
    GoogleSheetsReadProfile,
    GoogleSheetsRowData,
    GoogleSheetsSnapshot,
    GoogleSheetsConfiguredScopeError,
    GoogleSheetsUnsafeIdentityError,
)
from spine.domain.artifacts import GoogleSheetCellLocator, resolve_google_sheet_cell_locator
from spine.domain.sources.google import (
    GoogleSheetNumberFormat,
    GoogleSheetCellValue,
)


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
CONNECTION = UUID("30000000-0000-4000-8000-000000000001")
SCOPE = UUID("40000000-0000-4000-8000-000000000001")


def _cell(
    column_key: str,
    coordinate: str,
    *,
    entered: GoogleSheetCellValue,
    effective: GoogleSheetCellValue,
    formatted: str,
    number_format: GoogleSheetNumberFormat,
) -> GoogleSheetsCellData:
    return GoogleSheetsCellData(
        column_key=column_key,
        coordinate=coordinate,
        entered=entered,
        effective=effective,
        formatted=formatted,
        number_format=number_format,
    )


def _profile(**updates: object) -> GoogleSheetsReadProfile:
    values: dict[str, object] = {
        "spreadsheet_id": "spreadsheet-456",
        "sheet_id": 17,
        "business_id_column": "estimate_id",
        "columns": (
            {"column_key": "estimate_id", "coordinate": "A"},
            {"column_key": "total", "coordinate": "B"},
            {"column_key": "note", "coordinate": "C"},
        ),
        "developer_metadata_key": "spine.business_id",
    }
    values.update(updates)
    return GoogleSheetsReadProfile.model_validate(values)


def _row(
    *,
    business_id: str = "EST-2026-0001",
    effective_business_id: str | None = None,
    row_number: int = 2,
    metadata_value: str | None = "EST-2026-0001",
) -> GoogleSheetsRowData:
    metadata = ()
    if metadata_value is not None:
        metadata = (
            GoogleSheetsDeveloperMetadata(
                metadata_id=9001,
                key="spine.business_id",
                value=metadata_value,
                row_number=row_number,
            ),
        )
    return GoogleSheetsRowData(
        row_number=row_number,
        a1_range=f"A{row_number}:C{row_number}",
        cells=(
            _cell(
                "estimate_id",
                f"A{row_number}",
                entered=GoogleSheetCellValue(kind="string", value=business_id),
                effective=GoogleSheetCellValue(
                    kind="string",
                    value=(
                        business_id
                        if effective_business_id is None
                        else effective_business_id
                    ),
                ),
                formatted=business_id,
                number_format=GoogleSheetNumberFormat(type="TEXT"),
            ),
            _cell(
                "total",
                f"B{row_number}",
                entered=GoogleSheetCellValue(kind="formula", value="=SUM(D2:E2)"),
                effective=GoogleSheetCellValue(kind="number", value="1250.500"),
                formatted="1 250,50 ₽",
                number_format=GoogleSheetNumberFormat(
                    type="CURRENCY", pattern='#,##0.00 [$₽-ru-RU]'
                ),
            ),
            _cell(
                "note",
                f"C{row_number}",
                entered=GoogleSheetCellValue(kind="blank"),
                effective=GoogleSheetCellValue(kind="blank"),
                formatted="",
                number_format=GoogleSheetNumberFormat(type="TEXT"),
            ),
        ),
        developer_metadata=metadata,
    )


def _snapshot(*rows: GoogleSheetsRowData) -> GoogleSheetsSnapshot:
    return GoogleSheetsSnapshot(
        spreadsheet_id="spreadsheet-456",
        sheet_id=17,
        sheet_title="Estimates",
        locale="ru_RU",
        time_zone="Europe/Moscow",
        rows=rows,
    )


class FakeSheetsService:
    def __init__(self, snapshot: GoogleSheetsSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str, int]] = []

    async def get_spreadsheet(
        self, connection, resource_scope_reference, spreadsheet_id, sheet_id
    ) -> GoogleSheetsSnapshot:
        self.calls.append(
            (str(resource_scope_reference.value), spreadsheet_id, sheet_id)
        )
        assert connection.connection_id == CONNECTION
        return self.snapshot


def _provider(service: FakeSheetsService) -> GoogleSheetsProviderAdapter:
    return GoogleSheetsProviderAdapter(
        service=service,
        profile=_profile(),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_sheets_reader_preserves_all_configured_cell_representations_and_context():
    service = FakeSheetsService(_snapshot(_row()))

    observation = await _provider(service).read(
        SimpleNamespace(
            connection_id=CONNECTION,
            workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
            environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        ),
        SimpleNamespace(
            connection_id=CONNECTION,
            resource_scope_reference=SimpleNamespace(value=SCOPE),
            authorization_generation=7,
            configuration_generation=11,
        ),
    )

    assert service.calls == [(str(SCOPE), "spreadsheet-456", 17)]
    assert observation.original.key.immutable_business_id == "EST-2026-0001"
    assert observation.original.spreadsheet_locale == "ru_RU"
    assert observation.original.spreadsheet_time_zone == "Europe/Moscow"
    assert observation.original.cells[1].entered.kind == "formula"
    assert observation.original.cells[1].effective.value == "1250.5"
    assert observation.original.cells[1].formatted == "1 250,50 ₽"
    assert observation.original.cells[1].number_format.pattern == '#,##0.00 [$₽-ru-RU]'
    assert observation.location.observed_row_number == 2
    assert observation.location.observed_a1_range == "A2:C2"
    assert observation.location.developer_metadata_id == 9001
    normalized = observation.to_normalized_observation()
    assert normalized.external_identity.provider_key.sheet_id == 17
    assert normalized.original_bytes.startswith(b'{"cells":')


@pytest.mark.asyncio
async def test_sheets_row_locator_resolves_only_the_stored_revision_representation():
    observation = await _provider(FakeSheetsService(_snapshot(_row()))).read(
        SimpleNamespace(
            connection_id=CONNECTION,
            workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
            environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        ),
        SimpleNamespace(
            connection_id=CONNECTION,
            resource_scope_reference=SimpleNamespace(value=SCOPE),
            authorization_generation=7,
            configuration_generation=11,
        ),
    )
    revision = UUID("33333333-3333-3333-3333-333333333333")
    locator = GoogleSheetCellLocator(
        source_revision_id=revision,
        spreadsheet_id="spreadsheet-456",
        sheet_id=17,
        immutable_business_id="EST-2026-0001",
        column_key="total",
        representation="effective",
        observed_a1_range="A2:C2",
        observed_cell_coordinate="B2",
    )

    assert resolve_google_sheet_cell_locator(
        observation.original, locator, source_revision_id=revision
    ) == GoogleSheetCellValue(kind="number", value="1250.5")
    with pytest.raises(ValueError, match="locator does not resolve"):
        resolve_google_sheet_cell_locator(
            observation.original,
            locator,
            source_revision_id=UUID("44444444-4444-4444-4444-444444444444"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "message"),
    (
        ((_row(business_id=""),), "business ID"),
        ((_row(), _row(row_number=3)), "duplicate business ID"),
            ((_row(effective_business_id="OTHER-ROW"),), "conflicting business ID"),
            ((_row(metadata_value="OTHER-ROW"),), "Metadata"),
    ),
)
async def test_sheets_reader_rejects_unsafe_row_identity_before_normalization(
    rows: tuple[GoogleSheetsRowData, ...], message: str
) -> None:
    service = FakeSheetsService(_snapshot(*rows))

    with pytest.raises(GoogleSheetsUnsafeIdentityError, match=message):
        await _provider(service).read(
            SimpleNamespace(
                connection_id=CONNECTION,
                workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
                environment_id=UUID("20000000-0000-4000-8000-000000000001"),
            ),
            SimpleNamespace(
                connection_id=CONNECTION,
                resource_scope_reference=SimpleNamespace(value=SCOPE),
                authorization_generation=7,
                configuration_generation=11,
            ),
        )


@pytest.mark.asyncio
async def test_sheets_reader_rejects_unconfigured_sheet_identity() -> None:
    service = FakeSheetsService(
        GoogleSheetsSnapshot(
            spreadsheet_id="other-sheet",
            sheet_id=17,
            sheet_title="Estimates",
            locale="ru_RU",
            time_zone="Europe/Moscow",
            rows=(_row(),),
        )
    )

    with pytest.raises(GoogleSheetsConfiguredScopeError, match="configured spreadsheet"):
        await _provider(service).read(
            SimpleNamespace(
                connection_id=CONNECTION,
                workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
                environment_id=UUID("20000000-0000-4000-8000-000000000001"),
            ),
            SimpleNamespace(
                connection_id=CONNECTION,
                resource_scope_reference=SimpleNamespace(value=SCOPE),
                authorization_generation=7,
                configuration_generation=11,
            ),
        )


def test_sheets_row_replay_has_stable_normalized_identity_and_digest() -> None:
    first = _provider(FakeSheetsService(_snapshot(_row())))
    second = _provider(FakeSheetsService(_snapshot(_row())))
    command = SimpleNamespace(
        connection_id=CONNECTION,
        resource_scope_reference=SimpleNamespace(value=SCOPE),
        authorization_generation=7,
        configuration_generation=11,
    )
    connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
    )

    import asyncio

    first_observation = asyncio.run(first.read(connection, command))
    second_observation = asyncio.run(second.read(connection, command))

    assert first_observation.to_normalized_observation().model_dump(mode="json") == (
        second_observation.to_normalized_observation().model_dump(mode="json")
    )
