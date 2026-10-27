from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.connectors.google_sheets import (
    GoogleSheetsCellData,
    GoogleSheetsConcurrentModificationError,
    GoogleSheetsDeveloperMetadata,
    GoogleSheetsProviderAdapter,
    GoogleSheetsReconciliationProviderAdapter,
    GoogleSheetsReadProfile,
    GoogleSheetsRangePage,
    GoogleSheetsRowData,
    GoogleSheetsSnapshot,
    GoogleSheetsConfiguredScopeError,
    GoogleSheetsUnsafeIdentityError,
)
from spine.domain.artifacts import (
    GoogleSheetCellLocator,
    resolve_google_sheet_cell_locator,
)
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
                    type="CURRENCY", pattern="#,##0.00 [$₽-ru-RU]"
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
        self.provider_versions = ["1", "1"]
        self.version_calls = 0

    async def get_provider_version(
        self, connection, resource_scope_reference, spreadsheet_id
    ) -> str:
        del connection, resource_scope_reference, spreadsheet_id
        version = self.provider_versions[
            min(self.version_calls, len(self.provider_versions) - 1)
        ]
        self.version_calls += 1
        return version

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
    assert observation.original.cells[1].number_format.pattern == "#,##0.00 [$₽-ru-RU]"
    assert observation.location.observed_row_number == 2
    assert observation.location.observed_a1_range == "A2:C2"
    assert observation.location.developer_metadata_id == 9001
    normalized = observation.to_normalized_observation()
    assert normalized.external_identity.provider_key.sheet_id == 17
    assert normalized.original_bytes.startswith(b'{"cells":')
    assert normalized.provider_version_before == "1"
    assert normalized.provider_version_after == "1"


@pytest.mark.asyncio
async def test_sheets_range_reader_returns_all_rows_with_range_page_and_version_guards():
    snapshot = _snapshot(
        _row(business_id="EST-2026-0001", row_number=2),
        _row(business_id="EST-2026-0002", row_number=3, metadata_value="EST-2026-0002"),
    )

    class PagedSheetsService(FakeSheetsService):
        async def get_spreadsheet_range_page(
            self,
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        ):
            del connection, resource_scope_reference
            assert (spreadsheet_id, sheet_id, a1_range, page_token) == (
                "spreadsheet-456",
                17,
                "A2:C100",
                "page-1",
            )
            return GoogleSheetsRangePage(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                a1_range=a1_range,
                page_token=page_token,
                next_page_token="page-2",
                complete=True,
                provider_version_before="22",
                provider_version_after="22",
                snapshot=snapshot,
            )

    connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
    )
    command = SimpleNamespace(
        resource_scope_reference=SimpleNamespace(value=SCOPE),
        authorization_generation=7,
        configuration_generation=11,
        a1_range="A2:C100",
        provider_page_token="page-1",
    )

    result = await GoogleSheetsProviderAdapter(
        service=PagedSheetsService(snapshot), profile=_profile(), clock=lambda: NOW
    ).read_page(connection, command)

    assert result.page_token == "page-1"
    assert result.next_page_token == "page-2"
    assert result.provider_version_before == result.provider_version_after == "22"
    assert [row.original.key.immutable_business_id for row in result.observations] == [
        "EST-2026-0001",
        "EST-2026-0002",
    ]
    assert all(
        row.to_normalized_observation().provider_version_after == "22"
        for row in result.observations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "failure_code"),
    (
        (_row(business_id="", metadata_value=""), "missing_business_id"),
        (
            _row(business_id="EST-2026-0001", effective_business_id="OTHER-ROW"),
            "conflicting_business_id",
        ),
        (_row(metadata_value="OTHER-ROW"), "developer_metadata_mismatch"),
    ),
)
async def test_sheets_range_reader_returns_safe_identity_rejection_code(
    row: GoogleSheetsRowData, failure_code: str
):
    snapshot = _snapshot(row)

    class PagedSheetsService(FakeSheetsService):
        async def get_spreadsheet_range_page(
            self,
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        ):
            del connection, resource_scope_reference
            return GoogleSheetsRangePage(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                a1_range=a1_range,
                page_token=page_token,
                next_page_token=None,
                complete=True,
                provider_version_before="22",
                provider_version_after="22",
                snapshot=snapshot,
            )

    result = await GoogleSheetsProviderAdapter(
        service=PagedSheetsService(snapshot), profile=_profile(), clock=lambda: NOW
    ).read_page(
        SimpleNamespace(connection_id=CONNECTION),
        SimpleNamespace(
            resource_scope_reference=SimpleNamespace(value=SCOPE),
            authorization_generation=7,
            configuration_generation=11,
            a1_range="A2:C100",
            provider_page_token=None,
        ),
    )

    assert result.identity_rejection_codes == (failure_code,)
    assert result.observations == ()


@pytest.mark.asyncio
async def test_sheets_range_reader_quarantines_duplicate_business_ids():
    snapshot = _snapshot(
        _row(business_id="EST-2026-0001", row_number=2),
        _row(
            business_id="EST-2026-0001",
            row_number=3,
        ).model_copy(
            update={
                "developer_metadata": (
                    GoogleSheetsDeveloperMetadata(
                        metadata_id=9002,
                        key="spine.business_id",
                        value="EST-2026-0001",
                        row_number=3,
                    ),
                )
            }
        ),
    )

    class PagedSheetsService(FakeSheetsService):
        async def get_spreadsheet_range_page(
            self,
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        ):
            del connection, resource_scope_reference
            return GoogleSheetsRangePage(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                a1_range=a1_range,
                page_token=page_token,
                next_page_token=None,
                complete=True,
                provider_version_before="22",
                provider_version_after="22",
                snapshot=snapshot,
            )

    result = await GoogleSheetsProviderAdapter(
        service=PagedSheetsService(snapshot), profile=_profile(), clock=lambda: NOW
    ).read_page(
        SimpleNamespace(connection_id=CONNECTION),
        SimpleNamespace(
            resource_scope_reference=SimpleNamespace(value=SCOPE),
            authorization_generation=7,
            configuration_generation=11,
            a1_range="A2:C100",
            provider_page_token=None,
        ),
    )

    assert result.identity_rejection_codes == ("duplicate_business_id",)
    assert result.observations == ()


@pytest.mark.asyncio
async def test_reconciliation_provider_selects_the_profile_for_each_configured_sheet():
    second_snapshot = GoogleSheetsSnapshot(
        spreadsheet_id="spreadsheet-456",
        sheet_id=18,
        sheet_title="Archived estimates",
        locale="ru_RU",
        time_zone="Europe/Moscow",
        rows=(
            _row(
                business_id="EST-2025-0001",
                row_number=2,
                metadata_value="EST-2025-0001",
            ),
        ),
    )

    class MultiSheetService:
        async def get_spreadsheet_range_page(
            self,
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        ):
            del connection, resource_scope_reference
            assert (spreadsheet_id, a1_range, page_token) == (
                "spreadsheet-456",
                "A2:C100",
                None,
            )
            return GoogleSheetsRangePage(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                a1_range=a1_range,
                page_token=None,
                next_page_token=None,
                complete=True,
                provider_version_before="23",
                provider_version_after="23",
                snapshot=second_snapshot,
            )

    connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
    )
    provider = GoogleSheetsReconciliationProviderAdapter(
        service=MultiSheetService(),
        profiles=(_profile(), _profile(sheet_id=18)),
        clock=lambda: NOW,
    )
    result = await provider.read(
        connection,
        SimpleNamespace(
            connection_id=CONNECTION,
            resource_scope_reference=SimpleNamespace(value=SCOPE),
            resource_object_reference="spreadsheet-456",
            sheet_id=18,
            a1_range="A2:C100",
            provider_page_token=None,
            authorization_generation=7,
            configuration_generation=11,
        ),
    )

    assert result.sheet_id == 18
    assert result.observations[0].original.key.immutable_business_id == "EST-2025-0001"


@pytest.mark.asyncio
async def test_sheets_location_changes_preserve_content_digest_and_append_locator_provenance():
    first = _provider(FakeSheetsService(_snapshot(_row(row_number=2))))
    moved_row = _row(row_number=5)
    second_service = FakeSheetsService(
        GoogleSheetsSnapshot(
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
            sheet_title="Renamed estimates",
            locale="ru_RU",
            time_zone="Europe/Moscow",
            rows=(
                moved_row.model_copy(
                    update={
                        "developer_metadata": (
                            GoogleSheetsDeveloperMetadata(
                                metadata_id=9002,
                                key="spine.business_id",
                                value="EST-2026-0001",
                                row_number=5,
                            ),
                        )
                    }
                ),
            ),
        )
    )
    second = _provider(second_service)
    command = SimpleNamespace(
        resource_scope_reference=SimpleNamespace(value=SCOPE),
        authorization_generation=7,
        configuration_generation=11,
    )
    connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
    )

    first_observation = await first.read(connection, command)
    second_observation = await second.read(connection, command)
    first_normalized = first_observation.to_normalized_observation()
    second_normalized = second_observation.to_normalized_observation()

    assert first_normalized.original_bytes == second_normalized.original_bytes
    assert first_normalized.event_digest == second_normalized.event_digest
    assert first_normalized.event_identity != second_normalized.event_identity
    assert (
        first_normalized.origin_locator_digest
        != second_normalized.origin_locator_digest
    )
    assert first_normalized.origin_locator_value == "sheet-row:2:a1:a2:c2"
    assert second_normalized.origin_locator_value == "sheet-row:5:a1:a5:c5"


@pytest.mark.asyncio
async def test_sheets_row_sort_preserves_target_content_and_updates_its_coordinate():
    target = _row(row_number=2)
    other = _row(
        business_id="EST-2026-0002",
        row_number=3,
        metadata_value="EST-2026-0002",
    )

    def at_row(row: GoogleSheetsRowData, row_number: int) -> GoogleSheetsRowData:
        return row.model_copy(
            update={
                "row_number": row_number,
                "a1_range": f"A{row_number}:C{row_number}",
                "cells": tuple(
                    cell.model_copy(
                        update={
                            "coordinate": f"{cell.coordinate.rstrip('0123456789')}"
                            f"{row_number}"
                        }
                    )
                    for cell in row.cells
                ),
                "developer_metadata": tuple(
                    metadata.model_copy(update={"row_number": row_number})
                    for metadata in row.developer_metadata
                ),
            }
        )

    first = _snapshot(target, other)
    sorted_rows = _snapshot(at_row(other, 2), at_row(target, 3))
    profile = _profile(target_business_id="EST-2026-0001")
    command = SimpleNamespace(
        resource_scope_reference=SimpleNamespace(value=SCOPE),
        authorization_generation=7,
        configuration_generation=11,
    )
    connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
    )

    before = await GoogleSheetsProviderAdapter(
        service=FakeSheetsService(first), profile=profile, clock=lambda: NOW
    ).read(connection, command)
    after = await GoogleSheetsProviderAdapter(
        service=FakeSheetsService(sorted_rows), profile=profile, clock=lambda: NOW
    ).read(connection, command)

    before_normalized = before.to_normalized_observation()
    after_normalized = after.to_normalized_observation()
    assert before.original.key == after.original.key
    assert before_normalized.original_bytes == after_normalized.original_bytes
    assert before_normalized.event_digest == after_normalized.event_digest
    assert before.location.observed_row_number == 2
    assert after.location.observed_row_number == 3
    assert before_normalized.origin_locator_value == "sheet-row:2:a1:a2:c2"
    assert after_normalized.origin_locator_value == "sheet-row:3:a1:a3:c3"


@pytest.mark.asyncio
async def test_sheets_reader_rejects_provider_change_during_snapshot_read():
    service = FakeSheetsService(_snapshot(_row()))
    service.provider_versions = ["14", "15"]

    with pytest.raises(GoogleSheetsConcurrentModificationError):
        await _provider(service).read(
            SimpleNamespace(
                connection_id=CONNECTION,
                workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
                environment_id=UUID("20000000-0000-4000-8000-000000000001"),
            ),
            SimpleNamespace(
                resource_scope_reference=SimpleNamespace(value=SCOPE),
                authorization_generation=7,
                configuration_generation=11,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cell_update",
    (
        {"entered": GoogleSheetCellValue(kind="formula", value="=SUM(C2:E2)")},
        {"entered": GoogleSheetCellValue(kind="number", value="1250.5")},
        {"effective": GoogleSheetCellValue(kind="number", value="1251.5")},
        {"formatted": "1 251,50 ₽"},
        {
            "number_format": GoogleSheetNumberFormat(
                type="CURRENCY", pattern="#,##0.00 ₽"
            )
        },
    ),
)
async def test_sheets_revision_digest_changes_when_a_configured_representation_changes(
    cell_update: dict[str, object],
) -> None:
    row = _row()
    changed_cells = (
        row.cells[0],
        row.cells[1].model_copy(update=cell_update),
        row.cells[2],
    )
    changed_row = row.model_copy(update={"cells": changed_cells})
    connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
    )
    command = SimpleNamespace(
        resource_scope_reference=SimpleNamespace(value=SCOPE),
        authorization_generation=7,
        configuration_generation=11,
    )

    original = await _provider(FakeSheetsService(_snapshot(row))).read(
        connection, command
    )
    changed = await _provider(FakeSheetsService(_snapshot(changed_row))).read(
        connection, command
    )

    assert original.to_normalized_observation().original_bytes != (
        changed.to_normalized_observation().original_bytes
    )
    assert original.to_normalized_observation().event_digest != (
        changed.to_normalized_observation().event_digest
    )


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

    with pytest.raises(
        GoogleSheetsConfiguredScopeError, match="configured spreadsheet"
    ):
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
