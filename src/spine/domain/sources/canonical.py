"""Canonical source-head transitions.

The source observation and admission ledgers deliberately stop before this
module.  These models describe the small, append-only record that is allowed
to move a source's current head.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier

from .models import RevisionKind


class CanonicalOrdering(str, Enum):
    TRUSTED = "trusted"
    EXPECTED_CURRENT = "expected_current"


class CurrentSourceHead(DefinitionModel):
    """The PostgreSQL-owned current pointer for one logical source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    workspace_id: UUID
    environment_id: UUID
    current_revision_id: UUID
    current_revision_kind: RevisionKind
    acceptance_id: UUID
    accepted_generation: int = Field(ge=1)
    cas_version: int = Field(ge=1)
    ordering_scheme: VersionIdentifier | None = None
    ordering_token: str | None = None
    changed_at: datetime

    @model_validator(mode="after")
    def validate_head(self) -> "CurrentSourceHead":
        for name in (
            "source_object_id",
            "workspace_id",
            "environment_id",
            "current_revision_id",
            "acceptance_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.ordering_scheme is None and self.ordering_token is not None:
            raise ValueError("ordering token requires an ordering scheme")
        if self.changed_at.tzinfo is None or self.changed_at.utcoffset() is None:
            raise ValueError("changed_at must be timezone-aware")
        return self


class CanonicalAcceptance(DefinitionModel):
    """One committed current-head transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_acceptance_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    predecessor_revision_id: UUID | None = None
    predecessor_acceptance_id: UUID | None = None
    accepted_generation: int = Field(ge=1)
    ordering: CanonicalOrdering
    ordering_scheme: VersionIdentifier | None = None
    ordering_token: str | None = None
    admission_decision_id: UUID | None = None
    access_policy_version_id: UUID | None = None
    provenance_id: UUID
    idempotency_receipt_id: UUID
    audit_event_id: UUID
    outbox_event_id: UUID
    accepted_at: datetime

    @model_validator(mode="after")
    def validate_transition(self) -> "CanonicalAcceptance":
        for name in (
            "canonical_acceptance_id",
            "source_object_id",
            "source_revision_id",
            "workspace_id",
            "environment_id",
            "provenance_id",
            "idempotency_receipt_id",
            "audit_event_id",
            "outbox_event_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.predecessor_revision_id == self.source_revision_id:
            raise ValueError("a source revision cannot precede itself")
        if self.predecessor_acceptance_id == self.canonical_acceptance_id:
            raise ValueError("an acceptance cannot precede itself")
        if (self.ordering_scheme is None) != (self.ordering_token is None):
            raise ValueError("ordering scheme and token must be supplied together")
        if self.accepted_at.tzinfo is None or self.accepted_at.utcoffset() is None:
            raise ValueError("accepted_at must be timezone-aware")
        if self.ordering is CanonicalOrdering.TRUSTED and self.ordering_scheme is None:
            raise ValueError("trusted acceptance requires ordering evidence")
        if self.ordering is CanonicalOrdering.EXPECTED_CURRENT and self.ordering_scheme is not None:
            raise ValueError("expected-current acceptance cannot carry trusted ordering")
        return self


__all__ = ["CanonicalAcceptance", "CanonicalOrdering", "CurrentSourceHead"]
