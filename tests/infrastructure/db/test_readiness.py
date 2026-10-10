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
        (("20261010_12",), SchemaRevisionState.READY),
        (("20261011_01",), SchemaRevisionState.NEWER),
        (("unrecognized",), SchemaRevisionState.UNKNOWN),
        (
            ("20261010_07", "20261010_08"),
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
        supported_head="20261010_12",
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
        ),
    )

    assert state is expected


def test_committed_migration_inventory_has_one_linear_head() -> None:
    inventory = load_migration_inventory()

    assert inventory.heads == ("20261010_12",)
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
    )
