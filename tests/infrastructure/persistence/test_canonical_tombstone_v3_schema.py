import importlib
import inspect

from sqlalchemy import ForeignKeyConstraint

from spine.infrastructure.persistence.postgresql_mappings import (
    canonical_acceptances,
    current_trusted_canonical_tombstone_selectors,
    trusted_canonical_tombstone_selector_versions,
)


def test_v3_reader_first_migration_retains_schema_on_writer_rollback() -> None:
    migration = importlib.import_module(
        "migrations.versions.20270112_111_canonical_tombstone_v3"
    )

    assert migration.revision == "20270112_111"
    assert migration.down_revision == "20270111_110"
    upgrade = inspect.getsource(migration.upgrade)
    canonical_check = inspect.getsource(migration._canonical_acceptance_check)
    downgrade = inspect.getsource(migration.downgrade)
    assert "trusted_canonical_tombstone_selector_versions" in upgrade
    assert "canonical_acceptance_schema_version = 3" in canonical_check
    assert "deny_canonical_acceptance_v3_write" in downgrade
    assert "drop_column" not in downgrade
    assert "drop_table" not in downgrade


def test_v3_mapping_has_exact_authorization_and_selector_links() -> None:
    acceptance_columns = set(canonical_acceptances.c.keys())
    selector_columns = set(trusted_canonical_tombstone_selector_versions.c.keys())
    foreign_keys = {
        constraint.name
        for constraint in canonical_acceptances.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert {
        "revision_kind",
        "tombstone_authorization_id",
        "tombstone_authorization_digest",
        "transition_id",
    } <= acceptance_columns
    assert "fk_canonical_acceptances_tombstone_authorization" in foreign_keys
    assert {
        "selector_id",
        "selector_series_id",
        "source_object_id",
        "mode",
        "valid_from",
        "valid_until",
        "record",
    } <= selector_columns
    assert list(current_trusted_canonical_tombstone_selectors.primary_key.columns) == [
        current_trusted_canonical_tombstone_selectors.c.workspace_id,
        current_trusted_canonical_tombstone_selectors.c.environment_id,
        current_trusted_canonical_tombstone_selectors.c.selector_series_id,
    ]
