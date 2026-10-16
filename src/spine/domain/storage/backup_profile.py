"""Versioned rules shared by coordinated PostgreSQL and original-storage backup."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.domain.sources.original_reference import ObjectReference


R1_BACKUP_PROFILE_VERSION = "r1-coordinated-backup:v1"
R1_BACKUP_INCLUSION_POLICY = "r1-finalized-originals-and-canonical-metadata:v1"
R1_STAGED_RESTART_POLICY = "exclude-staged-restart-or-reconcile-before-use:v1"


class BackupProtectionProfile(BaseModel):
    """Deployment-reviewed, non-secret bindings required by the R1 profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_volume_id: str = Field(min_length=1, max_length=128)
    destination_id: str = Field(min_length=1, max_length=128)
    encryption_at_rest_profile: str = Field(min_length=1, max_length=128)
    protected_transfer_profile: str = Field(min_length=1, max_length=128)
    backup_identity: str = Field(min_length=1, max_length=128)
    restore_identity: str = Field(min_length=1, max_length=128)
    retention_days: Literal[30] = 30
    cadence: Literal["daily"] = "daily"

    @model_validator(mode="after")
    def validate_references(self) -> "BackupProtectionProfile":
        for name in (
            "source_volume_id",
            "destination_id",
            "encryption_at_rest_profile",
            "protected_transfer_profile",
            "backup_identity",
            "restore_identity",
        ):
            value = getattr(self, name)
            if not value.isascii() or any(
                not (char.isalnum() or char in "_.:/-") for char in value
            ):
                raise ValueError(f"{name} must be a safe configuration reference")
        if self.source_volume_id == self.destination_id:
            raise ValueError("backup destination must be separate from original storage")
        return self


def canonical_included_set_digest(references: Sequence[ObjectReference]) -> str:
    """Hash the exact immutable object identities that both artifacts must cover."""

    ordered = sorted(references, key=lambda reference: reference.object_id.hex)
    object_ids = [reference.object_id for reference in ordered]
    if len(set(object_ids)) != len(object_ids):
        raise ValueError("included object identities must be unique")
    payload = [reference.model_dump(mode="json") for reference in ordered]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BackupProtectionProfile",
    "R1_BACKUP_INCLUSION_POLICY",
    "R1_BACKUP_PROFILE_VERSION",
    "R1_STAGED_RESTART_POLICY",
    "canonical_included_set_digest",
]
