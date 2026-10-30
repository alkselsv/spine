from spine.infrastructure.connections.postgresql_bitrix24_oauth import (
    bitrix24_grant_authorities,
    bitrix24_grant_bindings,
    bitrix24_oauth_attempts,
    bitrix24_oauth_callback_receipts,
)
from spine.infrastructure.persistence.postgresql_mappings import metadata


def test_bitrix24_oauth_tables_are_refs_only_and_owned_by_connection_adapter() -> None:
    assert bitrix24_grant_bindings.metadata is not metadata
    assert bitrix24_grant_authorities.metadata is bitrix24_grant_bindings.metadata
    assert bitrix24_oauth_attempts.metadata is bitrix24_grant_bindings.metadata
    assert bitrix24_oauth_callback_receipts.metadata is bitrix24_grant_bindings.metadata
    assert "spine.bitrix24_grant_bindings" not in metadata.tables
    assert {
        "binding_id",
        "workspace_id",
        "environment_id",
        "connection_version_id",
        "grant_reference",
        "portal_member_id",
        "application_id",
        "acting_user_id",
    }.issubset(bitrix24_grant_bindings.c.keys())
    assert {
        "active_pair_reference",
        "token_epoch",
        "security_fence",
        "state",
        "refresh_attempt_id",
    }.issubset(bitrix24_grant_authorities.c.keys())
    assert "pair_candidate_reference" in bitrix24_oauth_callback_receipts.c
    forbidden = {"access_token", "refresh_token", "client_secret", "application_token"}
    for table in (
        bitrix24_grant_bindings,
        bitrix24_grant_authorities,
        bitrix24_oauth_attempts,
        bitrix24_oauth_callback_receipts,
    ):
        assert forbidden.isdisjoint(table.c.keys())


def test_bitrix24_binding_foreign_keys_encode_exact_connection_and_tenant_tuple() -> None:
    binding_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in bitrix24_grant_bindings.foreign_key_constraints
    }
    authority_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in bitrix24_grant_authorities.foreign_key_constraints
    }

    assert (
        "spine.provider_connection_versions.connection_version_id",
        "spine.provider_connection_versions.workspace_id",
        "spine.provider_connection_versions.environment_id",
        "spine.provider_connection_versions.approval_id",
        "spine.provider_connection_versions.connection_id",
        "spine.provider_connection_versions.grant_reference",
        "spine.provider_connection_versions.authorization_generation",
        "spine.provider_connection_versions.configuration_generation",
    ) in binding_targets
    assert (
        "spine.bitrix24_grant_bindings.binding_id",
        "spine.bitrix24_grant_bindings.workspace_id",
        "spine.bitrix24_grant_bindings.environment_id",
    ) in authority_targets
