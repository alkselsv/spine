"""Immutable source-retention applicability contracts.

These records bind an already approved policy to source-bearing artifacts.  They
do not define a duration, authorize cleanup, or turn legal holds into disclosure
authority.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RetentionTargetKind(str, Enum):
    EXACT_SOURCE = "exact_source"
    SOURCE_CLASS = "source_class"


class RetentionArtifactClass(str, Enum):
    """Source-bearing artifact classes governed by an applicability binding."""

    ORIGINAL = "original"
    PARSED_CONTENT = "parsed_content"
    TRANSCRIPT = "transcript"
    PROJECTION = "projection"
    CACHED_ANSWER = "cached_answer"
    CITATION = "citation"


class RetentionAuthorityKind(str, Enum):
    DATA_PORTAL = "data_portal"
    LEGAL_COMPLIANCE = "legal_compliance"


class RetentionApplicability(DefinitionModel):
    """One immutable, approved binding and its current-pointer CAS identity."""

    schema_version: Literal["retention-applicability:v1"] = (
        "retention-applicability:v1"
    )
    applicability_id: UUID
    workspace_id: UUID
    environment_id: UUID
    target_kind: RetentionTargetKind
    source_object_id: UUID | None = None
    source_class: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$"
    )
    source_class_reference: UUID | None = None
    source_class_version_id: UUID | None = None
    source_class_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_classes: tuple[RetentionArtifactClass, ...] = Field(min_length=1)
    retention_policy_id: UUID
    retention_policy_version_id: UUID
    retention_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    applicability_evidence_reference: UUID
    applicability_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approving_authority_reference: UUID
    approving_authority_version_id: UUID
    approving_authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approving_authority_kind: RetentionAuthorityKind
    effective_from: datetime
    effective_until: datetime | None = None
    exception_references: tuple[UUID, ...] = ()
    legal_hold_references: tuple[UUID, ...] = ()
    expected_current_applicability_id: UUID | None
    expected_current_generation: int = Field(ge=0)
    generation: int = Field(ge=1)
    actor_id: UUID
    audit_event_id: UUID
    outbox_event_id: UUID
    approved_at: datetime

    @model_validator(mode="after")
    def validate_binding(self) -> "RetentionApplicability":
        identifiers = (
            self.applicability_id,
            self.workspace_id,
            self.environment_id,
            self.retention_policy_id,
            self.retention_policy_version_id,
            self.applicability_evidence_reference,
            self.approving_authority_reference,
            self.approving_authority_version_id,
            self.actor_id,
            self.audit_event_id,
            self.outbox_event_id,
            *self.exception_references,
            *self.legal_hold_references,
        )
        if self.source_object_id is not None:
            identifiers += (self.source_object_id,)
        if self.source_class_reference is not None:
            identifiers += (self.source_class_reference,)
        if self.source_class_version_id is not None:
            identifiers += (self.source_class_version_id,)
        if self.expected_current_applicability_id is not None:
            identifiers += (self.expected_current_applicability_id,)
        if any(value.int == 0 for value in identifiers):
            raise ValueError("retention applicability identifiers must be non-zero")

        class_fields = (
            self.source_class,
            self.source_class_reference,
            self.source_class_version_id,
            self.source_class_digest,
        )
        if self.target_kind is RetentionTargetKind.EXACT_SOURCE:
            if self.source_object_id is None or any(value is not None for value in class_fields):
                raise ValueError("exact-source applicability requires only a source object")
        elif self.target_kind is RetentionTargetKind.SOURCE_CLASS:
            if self.source_object_id is not None or any(value is None for value in class_fields):
                raise ValueError("source-class applicability requires an approved class identity")

        if len(set(self.artifact_classes)) != len(self.artifact_classes):
            raise ValueError("retention artifact classes must be unique")
        if len(set(self.exception_references)) != len(self.exception_references):
            raise ValueError("retention exception references must be unique")
        if len(set(self.legal_hold_references)) != len(self.legal_hold_references):
            raise ValueError("legal-hold references must be unique")
        for value in (self.effective_from, self.effective_until, self.approved_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("retention applicability timestamps must be timezone-aware")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("retention applicability interval must be positive")
        if (self.expected_current_applicability_id is None) != (
            self.expected_current_generation == 0
        ):
            raise ValueError("expected applicability and generation must describe one head")
        if self.generation != self.expected_current_generation + 1:
            raise ValueError("retention applicability generation must advance the head")
        for digest in (
            self.retention_policy_digest,
            self.applicability_evidence_digest,
            self.approving_authority_digest,
        ):
            if _DIGEST.fullmatch(digest) is None:
                raise ValueError("retention digests must be lowercase SHA-256 hex")
        return self

    @property
    def target_reference(self) -> UUID:
        if self.target_kind is RetentionTargetKind.EXACT_SOURCE:
            assert self.source_object_id is not None
            return self.source_object_id
        assert self.source_class_reference is not None
        return self.source_class_reference


__all__ = [
    "RetentionApplicability",
    "RetentionArtifactClass",
    "RetentionAuthorityKind",
    "RetentionTargetKind",
]
