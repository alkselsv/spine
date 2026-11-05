from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from spine.infrastructure.persistence.postgresql_mappings import (
    current_source_deletion_policy_approvals,
    current_tombstone_authorization_decisions,
    current_tombstone_authorization_issuers,
    qualified_deletion_evidence,
    source_deletion_policy_approvals,
    tombstone_authorization_decisions,
    tombstone_authorization_issuers,
    tombstone_authorizations,
)


def _constraint_names(table, constraint_type):
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, constraint_type)
    }


def test_deletion_authority_tables_use_content_free_tenant_scoped_records() -> None:
    for table in (
        qualified_deletion_evidence,
        source_deletion_policy_approvals,
        tombstone_authorization_issuers,
        tombstone_authorizations,
        tombstone_authorization_decisions,
    ):
        assert {"workspace_id", "environment_id", "record"}.issubset(table.c.keys())
        assert not {
            "content",
            "provider_payload",
            "credential",
            "signed_url",
        }.intersection(table.c.keys())

    assert "fk_qualified_deletion_evidence_qualification_authority" in _constraint_names(
        qualified_deletion_evidence, ForeignKeyConstraint
    )


def test_authorization_schema_fences_command_head_and_exact_authority_bindings() -> (
    None
):
    uniques = _constraint_names(tombstone_authorizations, UniqueConstraint)
    foreign_keys = _constraint_names(tombstone_authorizations, ForeignKeyConstraint)

    assert "uq_tombstone_authorizations_command" in uniques
    assert "uq_tombstone_authorizations_expected_head" in uniques
    assert {
        "fk_tombstone_authorizations_evidence",
        "fk_tombstone_authorizations_policy_approval",
        "fk_tombstone_authorizations_issuer",
        "fk_tombstone_authorizations_expected_acceptance",
    }.issubset(foreign_keys)


def test_current_authority_heads_are_tenant_scoped_and_generation_fenced() -> None:
    assert list(
        current_source_deletion_policy_approvals.primary_key.columns.keys()
    ) == [
        "workspace_id",
        "environment_id",
        "source_contract_id",
        "source_contract_version",
        "connection_id",
        "resource_scope_reference",
    ]
    assert list(current_tombstone_authorization_issuers.primary_key.columns.keys()) == [
        "workspace_id",
        "environment_id",
    ]
    assert list(
        current_tombstone_authorization_decisions.primary_key.columns.keys()
    ) == [
        "workspace_id",
        "environment_id",
        "authorization_id",
    ]
