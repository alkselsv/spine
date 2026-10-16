from __future__ import annotations

import pytest

from spine.infrastructure.db.readiness import (
    SchemaRevisionState,
    classify_schema_revision,
    load_migration_inventory,
)


@pytest.mark.parametrize(
    ("database_heads", "expected"),
    (
        ((), SchemaRevisionState.EMPTY),
        (("20261009_01",), SchemaRevisionState.OLDER),
        (("20261014_17",), SchemaRevisionState.READY),
        (("20261015_01",), SchemaRevisionState.NEWER),
        (("unrecognized",), SchemaRevisionState.UNKNOWN),
        (
            ("20261010_09", "20261010_10"),
            SchemaRevisionState.MULTIPLE_HEADS,
        ),
    ),
)
def test_schema_revision_state_classifies_database_heads(
    database_heads: tuple[str, ...],
    expected: SchemaRevisionState,
) -> None:
    state = classify_schema_revision(
        database_heads=database_heads,
        supported_head="20261014_17",
        known_revisions=(
            "20261009_01",
            "20261009_02",
            "20261010_03",
            "20261010_04",
            "20261010_05",
            "20261010_06",
            "20261010_07",
            "20261010_08",
            "20261010_09",
            "20261010_10",
            "20261010_11",
            "20261010_12",
            "20261011_13",
            "20261012_14",
            "20261013_16",
            "20261014_17",
        ),
    )

    assert state is expected


def test_committed_migration_inventory_has_one_linear_head() -> None:
    inventory = load_migration_inventory()

    assert inventory.heads == ("20261030_35",)
    assert inventory.revisions == (
        "20261009_01",
        "20261009_02",
        "20261010_03",
        "20261010_04",
        "20261010_05",
        "20261010_06",
        "20261010_07",
        "20261010_08",
        "20261010_09",
        "20261010_10",
        "20261010_11",
        "20261010_12",
        "20261011_13",
        "20261012_14",
        "20261012_15",
        "20261013_15",
        "20261013_16",
        "20261014_17",
        "20261015_18",
        "20261016_19",
        "20261017_20",
        "20261018_21",
        "20261019_22",
        "20261020_23",
        "20261021_24",
        "20261021_25",
        "20261022_26",
        "20261023_27",
        "20261024_28",
        "20261025_29",
        "20261026_30",
        "20261027_31",
        "20261027_32",
        "20261028_33",
        "20261029_34",
        "20261030_35",
    )
