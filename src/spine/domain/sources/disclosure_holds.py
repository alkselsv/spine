"""Immutable deny-only governance holds for exact source lineage."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class DisclosureHoldEffect(str, Enum):
    """Effects an active hold may prohibit without granting anything else."""

    RETRIEVAL = "retrieval"
    MODEL_USE = "model_use"
    FINAL_DISCLOSURE = "final_disclosure"
    DERIVED_ARTIFACT_DISCLOSURE = "derived_artifact_disclosure"


class DisclosureHoldReason(str, Enum):
    """Content-free reason vocabulary safe for canonical audit records."""

    GOVERNED_SOURCE_REVIEW = "governed_source_review"
    REVIEW_COMPLETED = "review_completed"
    AUTHORITY_SUPERSEDED = "authority_superseded"


def _identifiers(*values: UUID) -> None:
    if any(value.int == 0 for value in values):
        raise ValueError("disclosure hold identifiers must be non-zero UUIDs")


def _lineage(values: tuple[UUID, ...], name: str) -> None:
    if not values:
        raise ValueError(f"{name} must identify an exact non-empty set")
    _identifiers(*values)
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicate identities")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def disclosure_hold_dependency_id(
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    source_revision_id: UUID,
) -> UUID:
    """Return the canonical disclosure-dependency identity for exact lineage."""

    return uuid5(
        NAMESPACE_URL,
        "spine:disclosure-dependency:"
        f"{workspace_id}:{environment_id}:{source_object_id}:{source_revision_id}",
    )


class DisclosureHold(DefinitionModel):
    """One finite, explicitly authorized hold over exact source lineage."""

    schema_version: Literal["disclosure-hold:v1"] = "disclosure-hold:v1"
    hold_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_ids: tuple[UUID, ...] = Field(min_length=1)
    dependency_ids: tuple[UUID, ...] = Field(min_length=1)
    artifact_ids: tuple[UUID, ...] = Field(min_length=1)
    review_decision_id: UUID
    evidence_observation_id: UUID
    prohibited_effects: tuple[DisclosureHoldEffect, ...] = Field(min_length=1)
    authority_reference: UUID
    authority_version_id: UUID
    authority_digest: Digest
    actor_id: UUID
    reason_code: DisclosureHoldReason
    effective_from: datetime
    effective_until: datetime
    audit_event_id: UUID
    outbox_event_id: UUID
    supersedes_hold_id: UUID | None = None

    @model_validator(mode="after")
    def validate_hold(self) -> "DisclosureHold":
        _identifiers(
            self.hold_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.review_decision_id,
            self.evidence_observation_id,
            self.authority_reference,
            self.authority_version_id,
            self.actor_id,
            self.audit_event_id,
            self.outbox_event_id,
        )
        if self.supersedes_hold_id is not None:
            _identifiers(self.supersedes_hold_id)
            if self.supersedes_hold_id == self.hold_id:
                raise ValueError("a disclosure hold cannot supersede itself")
        _lineage(self.source_revision_ids, "source_revision_ids")
        _lineage(self.dependency_ids, "dependency_ids")
        _lineage(self.artifact_ids, "artifact_ids")
        expected_dependencies = {
            disclosure_hold_dependency_id(
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                revision_id,
            )
            for revision_id in self.source_revision_ids
        }
        if set(self.dependency_ids) != expected_dependencies:
            raise ValueError(
                "dependency_ids must exactly bind the held source revisions"
            )
        if len(self.prohibited_effects) != len(set(self.prohibited_effects)):
            raise ValueError("prohibited_effects must be unique")
        _aware(self.effective_from, "effective_from")
        _aware(self.effective_until, "effective_until")
        if self.effective_until <= self.effective_from:
            raise ValueError("disclosure hold interval must be finite and increasing")
        if _DIGEST.fullmatch(self.authority_digest) is None:
            raise ValueError("authority_digest must be lowercase SHA-256 hex")
        return self


class DisclosureHoldRelease(DefinitionModel):
    """A separate authorized release that clears exactly one hold."""

    schema_version: Literal["disclosure-hold-release:v1"] = "disclosure-hold-release:v1"
    release_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    hold_id: UUID
    authority_reference: UUID
    authority_version_id: UUID
    authority_digest: Digest
    actor_id: UUID
    reason_code: DisclosureHoldReason
    released_at: datetime
    audit_event_id: UUID
    outbox_event_id: UUID
    superseded_by_hold_id: UUID | None = None

    @model_validator(mode="after")
    def validate_release(self) -> "DisclosureHoldRelease":
        _identifiers(
            self.release_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.hold_id,
            self.authority_reference,
            self.authority_version_id,
            self.actor_id,
            self.audit_event_id,
            self.outbox_event_id,
        )
        if self.superseded_by_hold_id is not None:
            _identifiers(self.superseded_by_hold_id)
            if self.superseded_by_hold_id == self.hold_id:
                raise ValueError("a disclosure hold cannot supersede itself")
        _aware(self.released_at, "released_at")
        if _DIGEST.fullmatch(self.authority_digest) is None:
            raise ValueError("authority_digest must be lowercase SHA-256 hex")
        return self

    def validate_hold(self, hold: DisclosureHold) -> None:
        if (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.hold_id,
        ) != (
            hold.workspace_id,
            hold.environment_id,
            hold.source_object_id,
            hold.hold_id,
        ):
            raise ValueError("release is not bound to the exact hold")
        if self.released_at < hold.effective_from:
            raise ValueError("release must occur after the hold became effective")


__all__ = [
    "DisclosureHold",
    "DisclosureHoldEffect",
    "DisclosureHoldReason",
    "DisclosureHoldRelease",
]
