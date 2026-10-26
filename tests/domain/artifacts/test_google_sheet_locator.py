from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.artifacts import (
    GoogleSheetCellLocator,
    resolve_google_sheet_cell_locator,
)
from spine.domain.sources.google import (
    GoogleRegisterRowKey,
    GoogleSheetCell,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    GoogleSheetRowOriginal,
)


CONNECTION = UUID("11111111-1111-1111-1111-111111111111")
REVISION = UUID("33333333-3333-3333-3333-333333333333")


def original() -> GoogleSheetRowOriginal:
    return GoogleSheetRowOriginal(
        key=GoogleRegisterRowKey(
            connection_id=CONNECTION,
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
            immutable_business_id="EST-2026-0001",
        ),
        spreadsheet_locale="ru_RU",
        spreadsheet_time_zone="Europe/Moscow",
        cells=(
            GoogleSheetCell(
                column_key="total",
                entered=GoogleSheetCellValue(kind="formula", value="=SUM(B2:C2)"),
                effective=GoogleSheetCellValue(kind="number", value="1250.5"),
                formatted="1 250,50 ₽",
                number_format=GoogleSheetNumberFormat(
                    type="CURRENCY", pattern='#,##0.00 [$₽-ru-RU]'
                ),
            ),
        ),
    )


def locator(**updates: object) -> GoogleSheetCellLocator:
    values: dict[str, object] = {
        "source_revision_id": REVISION,
        "spreadsheet_id": "spreadsheet-456",
        "sheet_id": 17,
        "immutable_business_id": "EST-2026-0001",
        "column_key": "total",
        "representation": "effective",
        "observed_a1_range": "A2:Z2",
        "observed_cell_coordinate": "H2",
    }
    values.update(updates)
    return GoogleSheetCellLocator.model_validate(values)


@pytest.mark.parametrize(
    ("representation", "expected"),
    (
        ("entered", GoogleSheetCellValue(kind="formula", value="=SUM(B2:C2)")),
        ("effective", GoogleSheetCellValue(kind="number", value="1250.5")),
        ("formatted", "1 250,50 ₽"),
        (
            "format",
            GoogleSheetNumberFormat(
                type="CURRENCY", pattern='#,##0.00 [$₽-ru-RU]'
            ),
        ),
    ),
)
def test_google_sheet_locator_resolves_exact_stored_representation(
    representation: str,
    expected: object,
) -> None:
    value = resolve_google_sheet_cell_locator(
        original(),
        locator(representation=representation),
        source_revision_id=REVISION,
    )

    assert value == expected


@pytest.mark.parametrize(
    "updates",
    (
        {"source_revision_id": UUID("44444444-4444-4444-4444-444444444444")},
        {"spreadsheet_id": "other-spreadsheet"},
        {"sheet_id": 18},
        {"immutable_business_id": "OTHER-ROW"},
        {"column_key": "missing-column"},
    ),
)
def test_google_sheet_locator_rejects_wrong_revision_row_or_column(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="locator does not resolve"):
        resolve_google_sheet_cell_locator(
            original(),
            locator(**updates),
            source_revision_id=REVISION,
        )


def test_google_sheet_locator_hints_do_not_select_the_evidence() -> None:
    moved = locator(
        observed_a1_range="A99:Z99",
        observed_cell_coordinate="H99",
    )

    assert resolve_google_sheet_cell_locator(
        original(), moved, source_revision_id=REVISION
    ) == GoogleSheetCellValue(kind="number", value="1250.5")
