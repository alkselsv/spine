"""Versioned, disclosure-safe bindings for the recovery operator boundary."""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from spine.domain.common import ValidatedDefinitionModel


RECOVERY_OPERATOR_PROFILE_VERSION = "spine-recovery-operator:v1"
RECOVERY_OPERATOR_RESULT_VERSION = "spine-recovery-operator-result:v1"
_OPAQUE_REFERENCE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[a-z0-9][a-z0-9_.:-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _validate_reference(value: str, *, field_name: str) -> str:
    if _OPAQUE_REFERENCE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded opaque reference")
    return value


class RecoveryOperatorOperation(str, Enum):
    """The only physical operations exposed by the one-shot binding."""

    BACKUP = "backup"
    RESTORE = "restore"
    BACKUP_AND_RESTORE = "backup_and_restore"


class RecoveryOperatorProfile(ValidatedDefinitionModel):
    """Immutable deployment bindings accepted by the recovery operator.

    Every external value is an opaque reference.  The profile deliberately has
    no database name, filesystem path, DSN, credential, role selector or
    verification bypass field.
    """

    schema_version: Literal[RECOVERY_OPERATOR_PROFILE_VERSION] = (
        RECOVERY_OPERATOR_PROFILE_VERSION
    )
    operation: RecoveryOperatorOperation
    profile_reference: str = Field(min_length=3, max_length=128)
    deployment_scope_reference: str = Field(min_length=3, max_length=128)
    scope_reference: str = Field(min_length=3, max_length=128)
    change_reference: str = Field(min_length=3, max_length=128)
    approval_reference: str = Field(min_length=3, max_length=128)
    recovery_set_id: str = Field(min_length=3, max_length=128)
    operator_identity_reference: str = Field(min_length=3, max_length=128)
    coordinator_identity_reference: str = Field(min_length=3, max_length=128)
    restore_identity_reference: str = Field(min_length=3, max_length=128)
    verifier_identity_reference: str = Field(min_length=3, max_length=128)
    source_inventory_reference: str = Field(min_length=3, max_length=128)
    source_volume_reference: str = Field(min_length=3, max_length=128)
    protected_destination_reference: str = Field(min_length=3, max_length=128)
    encryption_profile_reference: str = Field(min_length=3, max_length=128)
    protected_transfer_profile_reference: str = Field(min_length=3, max_length=128)
    retention_profile_reference: str = Field(min_length=3, max_length=128)
    postgres_tool_profile_reference: str = Field(min_length=3, max_length=128)
    filesystem_semantics_profile_reference: str = Field(min_length=3, max_length=128)
    fence_profile_reference: str = Field(min_length=3, max_length=128)
    source_network_profile_reference: str = Field(min_length=3, max_length=128)
    restore_network_profile_reference: str = Field(min_length=3, max_length=128)
    restore_target_inventory_reference: str | None = Field(default=None, max_length=128)
    serving_identity_inventory_reference: str | None = Field(default=None, max_length=128)
    evidence_destination_reference: str = Field(min_length=3, max_length=128)
    evidence_reviewer_reference: str = Field(min_length=3, max_length=128)
    manifest_reference: str | None = Field(default=None, max_length=128)

    @field_validator(
        "profile_reference",
        "deployment_scope_reference",
        "scope_reference",
        "change_reference",
        "approval_reference",
        "recovery_set_id",
        "operator_identity_reference",
        "coordinator_identity_reference",
        "restore_identity_reference",
        "verifier_identity_reference",
        "source_inventory_reference",
        "source_volume_reference",
        "protected_destination_reference",
        "encryption_profile_reference",
        "protected_transfer_profile_reference",
        "retention_profile_reference",
        "postgres_tool_profile_reference",
        "filesystem_semantics_profile_reference",
        "fence_profile_reference",
        "source_network_profile_reference",
        "restore_network_profile_reference",
        "restore_target_inventory_reference",
        "serving_identity_inventory_reference",
        "evidence_destination_reference",
        "evidence_reviewer_reference",
        "manifest_reference",
    )
    @classmethod
    def _validate_opaque_reference(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "reference")
        return _validate_reference(value, field_name=field_name)

    @model_validator(mode="after")
    def _validate_bindings(self) -> "RecoveryOperatorProfile":
        role_references = {
            self.operator_identity_reference,
            self.coordinator_identity_reference,
            self.restore_identity_reference,
            self.verifier_identity_reference,
        }
        if len(role_references) != 4:
            raise ValueError("operator, coordinator, restore and verifier identities must be distinct")
        if self.evidence_reviewer_reference in role_references:
            raise ValueError("evidence reviewer identity must be independent")
        if self.source_volume_reference == self.protected_destination_reference:
            raise ValueError("source and protected destination references must be distinct")
        if self.source_network_profile_reference == self.restore_network_profile_reference:
            raise ValueError("SOURCE and RESTORE network profiles must be distinct")
        if self.operation in {
            RecoveryOperatorOperation.RESTORE,
            RecoveryOperatorOperation.BACKUP_AND_RESTORE,
        }:
            if self.restore_target_inventory_reference is None:
                raise ValueError("restore_target_inventory_reference is required for restore")
            if self.serving_identity_inventory_reference is None:
                raise ValueError("serving_identity_inventory_reference is required for restore")
        if self.operation is RecoveryOperatorOperation.RESTORE and self.manifest_reference is None:
            raise ValueError("manifest_reference is required for restore")
        if self.operation in {
            RecoveryOperatorOperation.BACKUP,
            RecoveryOperatorOperation.BACKUP_AND_RESTORE,
        } and self.manifest_reference is not None:
            raise ValueError("backup operations cannot select an existing manifest")
        return self


class RecoverySourceInventoryFacts(ValidatedDefinitionModel):
    """Trusted, complete SOURCE inventory facts returned by an approved resolver."""

    inventory_reference: str = Field(min_length=3, max_length=128)
    deployment_scope_reference: str = Field(min_length=3, max_length=128)
    scope_reference: str = Field(min_length=3, max_length=128)
    source_volume_reference: str = Field(min_length=3, max_length=128)
    complete: Literal[True] = True
    trusted: Literal[True] = True

    @field_validator(
        "inventory_reference",
        "deployment_scope_reference",
        "scope_reference",
        "source_volume_reference",
    )
    @classmethod
    def _validate_references(cls, value: str, info: object) -> str:
        return _validate_reference(value, field_name=getattr(info, "field_name", "reference"))


class RecoveryRestoreTargetFacts(ValidatedDefinitionModel):
    """Approved RESTORE facts; a resolver must prove every isolation invariant."""

    inventory_reference: str = Field(min_length=3, max_length=128)
    target_reference: str = Field(min_length=3, max_length=128)
    database_target_reference: str = Field(min_length=3, max_length=128)
    filesystem_target_reference: str = Field(min_length=3, max_length=128)
    empty: Literal[True] = True
    isolated: Literal[True] = True
    non_serving: Literal[True] = True
    source_collision: Literal[False] = False
    serving_identity_inventory_reference: str = Field(min_length=3, max_length=128)
    restore_identity_reference: str = Field(min_length=3, max_length=128)
    verifier_identity_reference: str = Field(min_length=3, max_length=128)
    verifier_write_access: Literal[False] = False
    serving_identity_inventory_complete: Literal[True] = True
    serving_identity_access_denied: Literal[True] = True

    @field_validator(
        "inventory_reference",
        "target_reference",
        "database_target_reference",
        "filesystem_target_reference",
        "serving_identity_inventory_reference",
        "restore_identity_reference",
        "verifier_identity_reference",
    )
    @classmethod
    def _validate_references(cls, value: str, info: object) -> str:
        return _validate_reference(value, field_name=getattr(info, "field_name", "reference"))

    @model_validator(mode="after")
    def _validate_distinct_identities(self) -> "RecoveryRestoreTargetFacts":
        if self.restore_identity_reference == self.verifier_identity_reference:
            raise ValueError("restore and verifier identities must be distinct")
        if self.database_target_reference == self.filesystem_target_reference:
            raise ValueError("database and filesystem restore targets must be distinct")
        return self


class RecoveryOperatorResult(ValidatedDefinitionModel):
    """Content-free immutable result safe for operator/audit reporting."""

    schema_version: Literal[RECOVERY_OPERATOR_RESULT_VERSION] = (
        RECOVERY_OPERATOR_RESULT_VERSION
    )
    operation: RecoveryOperatorOperation
    status: Literal["verified", "rejected", "blocked", "indeterminate"]
    recovery_set_id: str = Field(min_length=3, max_length=128)
    manifest_reference: str | None = Field(default=None, max_length=128)
    manifest_digest: str | None = Field(default=None, max_length=64)
    restore_evidence_reference: str | None = Field(default=None, max_length=128)
    safe_failure_code: str | None = Field(default=None, max_length=64)
    replay: bool = False

    @field_validator(
        "recovery_set_id",
        "manifest_reference",
        "restore_evidence_reference",
    )
    @classmethod
    def _validate_result_references(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _validate_reference(value, field_name=getattr(info, "field_name", "reference"))

    @field_validator("manifest_digest")
    @classmethod
    def _validate_manifest_digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("manifest_digest must be lowercase sha256")
        return value

    @field_validator("safe_failure_code")
    @classmethod
    def _validate_failure_code(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value) is None:
            raise ValueError("safe_failure_code must be bounded")
        return value


__all__ = [
    "RECOVERY_OPERATOR_PROFILE_VERSION",
    "RECOVERY_OPERATOR_RESULT_VERSION",
    "RecoveryOperatorOperation",
    "RecoveryOperatorProfile",
    "RecoveryOperatorResult",
    "RecoveryRestoreTargetFacts",
    "RecoverySourceInventoryFacts",
]
