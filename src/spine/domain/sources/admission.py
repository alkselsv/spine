"""Immutable admission decisions for observed source revisions.

Admission is deliberately separate from canonical acceptance.  A decision only
records the security validator's disposition; it never changes the source head
or makes a revision current.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated
from uuid import UUID

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier, require_version_identifier

from .models import BoundedIdentifier


class AdmissionDisposition(str, Enum):
    ADMITTED = "admitted"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class AdmissionReference(DefinitionModel):
    """Opaque, tenant-local reference to validator authority evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_type: BoundedIdentifier
    reference_id: UUID
    schema_version: int = Field(ge=1)

    @field_validator("reference_id")
    @classmethod
    def validate_reference_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("reference_id must be a non-zero UUID")
        return value


ReasonCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")]


class AdmissionDecision(DefinitionModel):
    """One immutable node in the admission decision CAS chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID = Field(
        validation_alias=AliasChoices("decision_id", "admission_decision_id")
    )
    source_object_id: UUID
    # ``revision_id`` is the domain spelling used by SourceRevision.  Accept
    # the table/spec spelling as an input alias at the boundary as well.
    revision_id: UUID = Field(
        validation_alias=AliasChoices("revision_id", "source_revision_id")
    )
    workspace_id: UUID
    environment_id: UUID
    decision_version: int = Field(ge=1)
    predecessor_decision_id: UUID | None = None
    disposition: AdmissionDisposition
    validator_profile_version: VersionIdentifier = Field(
        validation_alias=AliasChoices("validator_profile_version", "validator_profile", "profile_version")
    )
    reason_schema_version: VersionIdentifier = Field(
        validation_alias=AliasChoices("reason_schema_version", "reason_code_schema_version")
    )
    reason_codes: tuple[ReasonCode, ...] = Field(
        min_length=1, validation_alias=AliasChoices("reason_codes", "reason_code_set")
    )
    authority_reference: AdmissionReference = Field(
        validation_alias=AliasChoices("authority_reference", "authority_ref")
    )
    trace_id: UUID
    audit_event_id: UUID = Field(
        validation_alias=AliasChoices("audit_event_id", "audit_reference")
    )
    decided_at: datetime

    @field_validator(
        "decision_id",
        "source_object_id",
        "revision_id",
        "workspace_id",
        "environment_id",
        "trace_id",
        "audit_event_id",
    )
    @classmethod
    def validate_non_zero_uuid(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("admission identifiers must be non-zero UUIDs")
        return value

    @field_validator("predecessor_decision_id")
    @classmethod
    def validate_predecessor(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("predecessor_decision_id must be a non-zero UUID")
        return value

    @field_validator("validator_profile_version", "reason_schema_version")
    @classmethod
    def validate_versions(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "version")
        return require_version_identifier(value, field_name=field_name)

    @field_validator("decided_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_chain(self) -> "AdmissionDecision":
        if self.predecessor_decision_id == self.decision_id:
            raise ValueError("an admission decision cannot precede itself")
        if self.predecessor_decision_id is None and self.decision_version != 1:
            raise ValueError("the root admission decision must have version 1")
        return self

    @property
    def source_revision_id(self) -> UUID:
        """Spec-compatible name for the observed revision identifier."""

        return self.revision_id


__all__ = [
    "AdmissionDecision",
    "AdmissionDisposition",
    "AdmissionReference",
    "ReasonCode",
]
