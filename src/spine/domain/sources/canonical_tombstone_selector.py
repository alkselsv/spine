"""Trusted writer selection for governed canonical tombstones."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel


class CanonicalTombstoneWriterMode(str, Enum):
    LEGACY_V1_ALLOWED = "legacy_v1_allowed"
    V3_REQUIRED = "v3_required"
    DENY_NEW = "deny_new"


class CanonicalTombstoneSelectorHead(BaseModel):
    """CAS pointer to one immutable selector series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    selector_series_id: UUID
    current_selector_id: UUID
    cas_version: int = Field(ge=1)


class TrustedCanonicalTombstoneVersionSelector(DefinitionModel):
    """Immutable, scope-exact tombstone writer choice.

    Absence is intentionally not represented: a missing current selector is a
    denial at the application/persistence boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["trusted-canonical-tombstone-version-selector:v1"] = (
        "trusted-canonical-tombstone-version-selector:v1"
    )
    selector_id: UUID
    selector_series_id: UUID
    selector_version: int = Field(ge=1)
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_contract_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    source_contract_version: str = Field(
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$"
    )
    provider_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    connection_id: UUID
    resource_scope_reference: UUID
    mode: CanonicalTombstoneWriterMode
    approval_reference: UUID
    predecessor_selector_id: UUID | None = None
    audit_reference: UUID
    idempotency_identity: str = Field(min_length=1, max_length=255)
    valid_from: datetime
    valid_until: datetime
    created_at: datetime

    @model_validator(mode="after")
    def validate_selector(self) -> "TrustedCanonicalTombstoneVersionSelector":
        identifiers = (
            self.selector_id,
            self.selector_series_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.connection_id,
            self.resource_scope_reference,
            self.approval_reference,
            self.audit_reference,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("tombstone selector identifiers must be non-zero")
        if (self.selector_version == 1) != (self.predecessor_selector_id is None):
            raise ValueError("selector version and predecessor do not form a valid history")
        if self.predecessor_selector_id == self.selector_id:
            raise ValueError("selector cannot precede itself")
        for value in (self.valid_from, self.valid_until, self.created_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("tombstone selector timestamps must be timezone-aware")
        if self.valid_until <= self.valid_from or not (
            self.valid_from <= self.created_at < self.valid_until
        ):
            raise ValueError("tombstone selector validity interval is invalid")
        return self

    @property
    def series_identity(self) -> tuple[object, ...]:
        return (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_contract_id,
            self.source_contract_version,
            self.provider_kind,
            self.connection_id,
            self.resource_scope_reference,
        )

    def writer_mode_at(self, evaluated_at: datetime) -> CanonicalTombstoneWriterMode:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("selector evaluation time must be timezone-aware")
        if not self.valid_from <= evaluated_at < self.valid_until:
            return CanonicalTombstoneWriterMode.DENY_NEW
        return self.mode


__all__ = [
    "CanonicalTombstoneSelectorHead",
    "CanonicalTombstoneWriterMode",
    "TrustedCanonicalTombstoneVersionSelector",
]
