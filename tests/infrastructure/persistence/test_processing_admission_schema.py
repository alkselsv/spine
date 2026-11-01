from __future__ import annotations

import importlib
import inspect

from spine.infrastructure.persistence.postgresql_mappings import (
    canonical_acceptances,
    source_processing_admissions,
)


def test_processing_admission_expand_is_reader_first_and_tenant_scoped() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261227_95_processing_admission_expand"
    )

    assert migration.revision == "20261227_95"
    assert migration.down_revision == "20261226_94"
    source = inspect.getsource(migration)
    assert "canonical_acceptance_schema_version" in source
    assert "server_default=\"1\"" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "fk_source_processing_admissions_exact_receipt" in source
    assert "fk_source_processing_admissions_verifier" in source
    assert "fk_source_processing_admissions_lifecycle" in source
    assert "fk_source_processing_admissions_retention" in source
    assert "trg_canonical_acceptance_v2_disabled" in source


def test_processing_admission_expand_keeps_v2_writer_disabled() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261227_95_processing_admission_expand"
    )

    upgrade_source = inspect.getsource(migration.upgrade)
    downgrade_source = inspect.getsource(migration.downgrade)
    assert "deny_canonical_acceptance_v2_write" in upgrade_source
    assert "DROP TRIGGER trg_canonical_acceptance_v2_disabled" in downgrade_source


def test_processing_admission_mapping_exposes_exact_v2_read_contract() -> None:
    admission_columns = {column.name for column in source_processing_admissions.columns}
    acceptance_columns = {column.name for column in canonical_acceptances.columns}

    assert {
        "processing_admission_id",
        "workspace_id",
        "environment_id",
        "source_object_id",
        "source_revision_id",
        "selector_id",
        "provider_connection_id",
        "provider_connection_version_id",
        "provider_approval_id",
        "grant_binding_id",
        "grant_token_epoch",
        "grant_security_fence",
        "receipt_association_id",
        "verifier_evidence_id",
        "lifecycle_eligibility_reference",
        "retention_applicability_reference",
        "mode",
        "record",
    } <= admission_columns
    assert {
        "canonical_acceptance_schema_version",
        "processing_admission_id",
    } <= acceptance_columns
