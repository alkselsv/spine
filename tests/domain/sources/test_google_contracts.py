from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.google import (
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    GoogleSheetCell,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    GoogleSheetRowLocation,
    GoogleSheetRowObservation,
    GoogleSheetRowOriginal,
    GoogleSheetScopeKey,
    google_sheet_row_content_digest,
    google_sheet_row_original_bytes,
    google_source_identity,
)


CONNECTION = UUID("11111111-1111-1111-1111-111111111111")
OTHER_CONNECTION = UUID("22222222-2222-2222-2222-222222222222")


def test_google_source_identity_is_bounded_versioned_and_component_inspectable() -> None:
    keys = (
        GoogleDriveFileKey(connection_id=CONNECTION, file_id="drive-file_123"),
        GoogleSheetScopeKey(
            connection_id=CONNECTION,
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
        ),
        GoogleRegisterRowKey(
            connection_id=CONNECTION,
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
            immutable_business_id="EST-2026-0001",
        ),
    )

    identities = tuple(google_source_identity(key) for key in keys)

    assert [identity.external_namespace for identity in identities] == ["google"] * 3
    assert [identity.external_generation for identity in identities] == [
        "google-source-key:v1"
    ] * 3
    assert all(len(identity.external_object_id) <= 64 for identity in identities)
    assert all(identity.external_object_id.startswith("gsk1:") for identity in identities)
    assert [identity.provider_key for identity in identities] == list(keys)
    assert len({identity.external_object_id for identity in identities}) == 3
    assert identities[0].external_object_id == (
        "gsk1:pzjjmq3uftklaey6cxksm2xlvftr2c4fnf5obhpxy2uhemoaqrla"
    )


def test_google_source_identity_is_deterministic_and_isolated_by_connection() -> None:
    first = GoogleRegisterRowKey(
        connection_id=CONNECTION,
        spreadsheet_id="sheet|with:delimiters",
        sheet_id=7,
        immutable_business_id="row|with:delimiters",
    )
    same = first.model_copy()
    other_connection = first.model_copy(update={"connection_id": OTHER_CONNECTION})
    other_boundary = first.model_copy(
        update={
            "spreadsheet_id": "sheet",
            "immutable_business_id": "with:delimiters|row|with:delimiters",
        }
    )

    assert google_source_identity(first) == google_source_identity(same)
    assert google_source_identity(first).external_object_id != (
        google_source_identity(other_connection).external_object_id
    )
    assert google_source_identity(first).external_object_id != (
        google_source_identity(other_boundary).external_object_id
    )


def row_original() -> GoogleSheetRowOriginal:
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
                effective=GoogleSheetCellValue(kind="number", value="1250.500"),
                formatted="1 250,50 ₽",
                number_format=GoogleSheetNumberFormat(
                    type="CURRENCY",
                    pattern='#,##0.00 [$₽-ru-RU]',
                ),
            ),
            GoogleSheetCell(
                column_key="note",
                entered=GoogleSheetCellValue(kind="blank"),
                effective=GoogleSheetCellValue(kind="blank"),
                formatted="",
                number_format=GoogleSheetNumberFormat(type="TEXT"),
            ),
        ),
    )


def row_observation(
    original: GoogleSheetRowOriginal,
    *,
    sheet_title: str = "Estimates",
    observed_row_number: int = 2,
    observed_a1_range: str = "A2:Z2",
) -> GoogleSheetRowObservation:
    return GoogleSheetRowObservation(
        original=original,
        location=GoogleSheetRowLocation(
            sheet_title=sheet_title,
            observed_row_number=observed_row_number,
            observed_a1_range=observed_a1_range,
            developer_metadata_id=9001,
        ),
        observed_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )


def test_google_sheet_row_original_has_a_stable_golden_vector() -> None:
    original = row_original()

    assert google_sheet_row_original_bytes(original) == (
        b'{"cells":[{"column_key":"note","effective":{"kind":"blank","value":null},'
        b'"entered":{"kind":"blank","value":null},"formatted":"","number_format":'
        b'{"pattern":null,"type":"TEXT"}},{"column_key":"total","effective":'
        b'{"kind":"number","value":"1250.5"},"entered":{"kind":"formula","value":'
        b'"=SUM(B2:C2)"},"formatted":"1 250,50 \xe2\x82\xbd","number_format":'
        b'{"pattern":"#,##0.00 [$\xe2\x82\xbd-ru-RU]","type":"CURRENCY"}}],'
        b'"extraction_profile":"google-sheets-row-extraction:v1","key":'
        b'{"connection_id":"11111111-1111-1111-1111-111111111111",'
        b'"immutable_business_id":"EST-2026-0001","kind":"register_row",'
        b'"schema_version":"google-register-row-key:v1","sheet_id":17,'
        b'"spreadsheet_id":"spreadsheet-456"},"revision_profile":'
        b'"google-sheets-row-revision:v1","schema_version":'
        b'"google-sheet-row-original:v1","spreadsheet_locale":"ru_RU",'
        b'"spreadsheet_time_zone":"Europe/Moscow"}'
    )
    assert google_sheet_row_content_digest(original) == (
        "e9dc9c257b7b1b91d4078933607f0a4741b8c802ca6adda1bda384f4a17c28ce"
    )


def test_google_sheet_row_digest_ignores_location_and_title_hints() -> None:
    original = row_original()
    before = row_observation(original)
    after_sort_and_rename = row_observation(
        original,
        sheet_title="Архив смет",
        observed_row_number=91,
        observed_a1_range="A91:Z91",
    )

    assert google_sheet_row_content_digest(before) == google_sheet_row_content_digest(
        after_sort_and_rename
    )


@pytest.mark.parametrize(
    "changed_cell",
    (
        GoogleSheetCell(
            column_key="total",
            entered=GoogleSheetCellValue(kind="formula", value="=SUM(B2:D2)"),
            effective=GoogleSheetCellValue(kind="number", value="1250.5"),
            formatted="1 250,50 ₽",
            number_format=GoogleSheetNumberFormat(
                type="CURRENCY", pattern='#,##0.00 [$₽-ru-RU]'
            ),
        ),
        GoogleSheetCell(
            column_key="total",
            entered=GoogleSheetCellValue(kind="formula", value="=SUM(B2:C2)"),
            effective=GoogleSheetCellValue(kind="number", value="1300"),
            formatted="1 250,50 ₽",
            number_format=GoogleSheetNumberFormat(
                type="CURRENCY", pattern='#,##0.00 [$₽-ru-RU]'
            ),
        ),
        GoogleSheetCell(
            column_key="total",
            entered=GoogleSheetCellValue(kind="formula", value="=SUM(B2:C2)"),
            effective=GoogleSheetCellValue(kind="number", value="1250.5"),
            formatted="1 250,500 ₽",
            number_format=GoogleSheetNumberFormat(
                type="CURRENCY", pattern='#,##0.00 [$₽-ru-RU]'
            ),
        ),
        GoogleSheetCell(
            column_key="total",
            entered=GoogleSheetCellValue(kind="formula", value="=SUM(B2:C2)"),
            effective=GoogleSheetCellValue(kind="number", value="1250.5"),
            formatted="1 250,50 ₽",
            number_format=GoogleSheetNumberFormat(
                type="CURRENCY", pattern='#,##0.000 [$₽-ru-RU]'
            ),
        ),
    ),
)
def test_google_sheet_row_digest_changes_with_revision_bearing_cell_data(
    changed_cell: GoogleSheetCell,
) -> None:
    original = row_original()
    changed = original.model_copy(update={"cells": (original.cells[1], changed_cell)})

    assert google_sheet_row_content_digest(changed) != google_sheet_row_content_digest(
        original
    )


def test_google_sheet_row_contract_rejects_duplicate_columns_and_unknown_fields() -> None:
    original = row_original()

    with pytest.raises(ValidationError, match="column_key values must be unique"):
        GoogleSheetRowOriginal(
            key=original.key,
            spreadsheet_locale=original.spreadsheet_locale,
            spreadsheet_time_zone=original.spreadsheet_time_zone,
            cells=(original.cells[0], original.cells[0]),
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GoogleRegisterRowKey.model_validate(
            {
                **original.key.model_dump(),
                "sheet_title": "mutable-title-must-not-be-identity",
            }
        )
