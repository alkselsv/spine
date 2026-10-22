from __future__ import annotations

import pytest
from pydantic import ValidationError

from spine.domain.storage.recovery_operator import (
    RECOVERY_OPERATOR_PROFILE_VERSION,
    RecoveryOperatorOperation,
    RecoveryOperatorProfile,
    RecoveryOperatorResult,
    RecoveryRestoreTargetFacts,
    RecoverySourceInventoryFacts,
)


def profile_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": RECOVERY_OPERATOR_PROFILE_VERSION,
        "operation": RecoveryOperatorOperation.BACKUP,
        "profile_reference": "profile:recovery-v1",
        "deployment_scope_reference": "deployment:production",
        "scope_reference": "scope:workspace-environment",
        "change_reference": "change:2026-532",
        "approval_reference": "approval:storage-owner",
        "recovery_set_id": "recovery-set:one",
        "operator_identity_reference": "identity:operator",
        "coordinator_identity_reference": "identity:coordinator",
        "restore_identity_reference": "identity:restore",
        "verifier_identity_reference": "identity:verifier",
        "source_inventory_reference": "inventory:source-v1",
        "source_volume_reference": "volume:source",
        "protected_destination_reference": "destination:protected",
        "encryption_profile_reference": "encryption:prod",
        "protected_transfer_profile_reference": "transfer:tls",
        "retention_profile_reference": "retention:30d",
        "postgres_tool_profile_reference": "tool:postgres-17",
        "filesystem_semantics_profile_reference": "filesystem:posix-v1",
        "fence_profile_reference": "fence:adr-0022-v1",
        "source_network_profile_reference": "network:source",
        "restore_network_profile_reference": "network:restore",
        "restore_target_inventory_reference": "inventory:restore-v1",
        "serving_identity_inventory_reference": "inventory:serving-v1",
        "evidence_destination_reference": "evidence:protected",
        "evidence_reviewer_reference": "identity:reviewer",
    }
    values.update(overrides)
    return values


def test_recovery_operator_profile_accepts_versioned_opaque_bindings() -> None:
    profile = RecoveryOperatorProfile.model_validate(profile_values())

    assert profile.schema_version == RECOVERY_OPERATOR_PROFILE_VERSION
    assert profile.recovery_set_id == "recovery-set:one"
    assert profile.operation is RecoveryOperatorOperation.BACKUP


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_volume_reference", "C:/source"),
        ("protected_destination_reference", "postgresql://user:password@host/db"),
        ("coordinator_identity_reference", "role:backup=superuser"),
    ],
)
def test_recovery_operator_profile_rejects_arbitrary_selectors(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        RecoveryOperatorProfile.model_validate(profile_values(**{field: value}))


def test_restore_profile_requires_manifest_and_distinct_roles() -> None:
    with pytest.raises(ValidationError, match="manifest_reference"):
        RecoveryOperatorProfile.model_validate(
            profile_values(operation=RecoveryOperatorOperation.RESTORE)
        )

    with pytest.raises(ValidationError, match="identities must be distinct"):
        RecoveryOperatorProfile.model_validate(
            profile_values(
                restore_identity_reference="identity:coordinator",
                operation=RecoveryOperatorOperation.RESTORE,
                manifest_reference="manifest:one",
            )
        )


def test_recovery_operator_facts_require_trusted_complete_source() -> None:
    with pytest.raises(ValidationError):
        RecoverySourceInventoryFacts(
            inventory_reference="inventory:source-v1",
            deployment_scope_reference="deployment:production",
            scope_reference="scope:workspace-environment",
            source_volume_reference="volume:source",
            complete=False,
            trusted=True,
        )


def test_recovery_operator_target_facts_reject_shared_or_serving_target() -> None:
    with pytest.raises(ValidationError):
        RecoveryRestoreTargetFacts(
            inventory_reference="inventory:restore-v1",
            target_reference="target:restore",
            database_target_reference="database:restore",
            filesystem_target_reference="filesystem:restore",
            serving_identity_inventory_reference="inventory:serving-v1",
            empty=True,
            isolated=True,
            non_serving=False,
            source_collision=False,
            restore_identity_reference="identity:restore",
            verifier_identity_reference="identity:verifier",
            verifier_write_access=False,
            serving_identity_inventory_complete=True,
            serving_identity_access_denied=False,
        )


def test_recovery_operator_result_is_metadata_only_and_immutable() -> None:
    result = RecoveryOperatorResult(
        operation=RecoveryOperatorOperation.BACKUP,
        status="verified",
        recovery_set_id="recovery-set:one",
        manifest_reference="manifest:one",
        manifest_digest="a" * 64,
    )

    assert "password" not in result.model_dump_json()
    with pytest.raises(ValidationError):
        result.model_copy(update={"manifest_reference": "file:/secret"})
