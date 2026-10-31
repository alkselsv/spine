"""Provider-neutral, immutable source lifecycle evidence and authority records."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
LifecycleDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _nonzero(*values: UUID) -> None:
    if any(value.int == 0 for value in values):
        raise ValueError("lifecycle identifiers must be non-zero UUIDs")


class LifecycleObservationKind(str, Enum):
    """Content-free facts that may inform, but never enact, governance."""

    PROVIDER_OUTAGE = "provider_outage"
    PERMISSION_INACCESSIBLE = "permission_inaccessible"
    GRANT_UNCERTAIN = "grant_uncertain"
    GRANT_CONFIRMED_REVOKED = "grant_confirmed_revoked"
    SCOPE_APPROVAL_SUPERSEDED = "scope_approval_superseded"
    SCOPE_WITHDRAWN = "scope_withdrawn"
    MOVE_WITHIN_SCOPE = "move_within_scope"
    MOVE_OUTSIDE_SCOPE = "move_outside_scope"
    MOVE_INTO_APPROVED_SCOPE = "move_into_approved_scope"
    TRASHED = "trashed"
    DELETED_METADATA = "deleted_metadata"
    FILTERED_ABSENCE = "filtered_absence"
    INCOMPLETE_SCAN = "incomplete_scan"
    QUALIFIED_DELETION_EVIDENCE = "qualified_deletion_evidence"
    RETENTION_APPLICABILITY_MISSING = "retention_applicability_missing"
    RETENTION_EXPIRED = "retention_expired"
    LEGAL_HOLD_APPLIED = "legal_hold_applied"
    LEGAL_HOLD_RELEASED = "legal_hold_released"
    REAPPEARED = "reappeared"
    IDENTITY_CONTINUITY_UNPROVEN = "identity_continuity_unproven"


class LifecycleObservationConfidence(str, Enum):
    UNCERTAIN = "uncertain"
    QUALIFIED = "qualified"


class LifecycleReasonCode(str, Enum):
    """Allowlisted content-free reason vocabulary for audit-safe records."""

    PROVIDER_OUTAGE = "provider_outage"
    PERMISSION_INACCESSIBLE = "permission_inaccessible"
    GRANT_UNCERTAIN = "grant_uncertain"
    GRANT_REVOKED = "grant_revoked"
    SCOPE_SUPERSEDED = "scope_superseded"
    SCOPE_WITHDRAWN = "scope_withdrawn"
    MOVE_WITHIN_SCOPE = "move_within_scope"
    MOVE_OUTSIDE_SCOPE = "move_outside_scope"
    MOVE_INTO_APPROVED_SCOPE = "move_into_approved_scope"
    PROVIDER_TRASHED = "provider_trashed"
    DELETED_METADATA = "deleted_metadata"
    FILTERED_ABSENCE = "filtered_absence"
    SCAN_INCOMPLETE = "scan_incomplete"
    QUALIFIED_DELETION = "qualified_deletion"
    RETENTION_BINDING_MISSING = "retention_binding_missing"
    RETENTION_EXPIRED = "retention_expired"
    LEGAL_HOLD_APPLIED = "legal_hold_applied"
    LEGAL_HOLD_RELEASED = "legal_hold_released"
    REAPPEARED = "reappeared"
    IDENTITY_CONTINUITY_UNPROVEN = "identity_continuity_unproven"


class LifecycleDecisionEffect(str, Enum):
    """Governed lifecycle outcome; tombstone candidates are not transitions."""

    RECORD_ONLY = "record_only"
    ELIGIBLE = "eligible"
    READ_WITHDRAWN = "read_withdrawn"
    DISCLOSURE_REVIEW_REQUIRED = "disclosure_review_required"
    DISCLOSURE_HELD = "disclosure_held"
    RETENTION_APPLICABILITY_UNRESOLVED = "retention_applicability_unresolved"
    RETENTION_INELIGIBLE = "retention_ineligible"
    TOMBSTONE_CANDIDATE = "tombstone_candidate"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    IDENTITY_CONTINUITY_UNPROVEN = "identity_continuity_unproven"


class LifecycleEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    READ_WITHDRAWN = "read_withdrawn"
    DISCLOSURE_REVIEW_REQUIRED = "disclosure_review_required"
    DISCLOSURE_HELD = "disclosure_held"
    RETENTION_APPLICABILITY_UNRESOLVED = "retention_applicability_unresolved"
    RETENTION_INELIGIBLE = "retention_ineligible"
    TOMBSTONED = "tombstoned"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    IDENTITY_CONTINUITY_UNPROVEN = "identity_continuity_unproven"


class LifecycleObservation(DefinitionModel):
    """Immutable evidence reference bound to one exact source and approved scope."""

    schema_version: Literal["lifecycle-observation:v1"] = "lifecycle-observation:v1"
    observation_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    scope_reference: UUID
    scope_version_id: UUID
    scope_digest: LifecycleDigest
    provider_identity_reference: UUID
    kind: LifecycleObservationKind
    evidence_reference: UUID
    evidence_digest: LifecycleDigest
    confidence: LifecycleObservationConfidence
    reason_code: LifecycleReasonCode
    observed_at: datetime

    @model_validator(mode="after")
    def validate_binding(self) -> "LifecycleObservation":
        _nonzero(
            self.observation_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.connection_id,
            self.connection_version_id,
            self.scope_reference,
            self.scope_version_id,
            self.provider_identity_reference,
            self.evidence_reference,
        )
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("lifecycle observation timestamp must be timezone-aware")
        if not _DIGEST.fullmatch(self.scope_digest) or not _DIGEST.fullmatch(
            self.evidence_digest
        ):
            raise ValueError("lifecycle digests must be lowercase SHA-256 hex")
        if (
            self.kind is LifecycleObservationKind.QUALIFIED_DELETION_EVIDENCE
            and self.confidence is not LifecycleObservationConfidence.QUALIFIED
        ):
            raise ValueError("qualified deletion evidence requires qualified confidence")
        return self


class LifecycleDecision(DefinitionModel):
    """Append-only governance decision with an explicit current-pointer CAS."""

    schema_version: Literal["lifecycle-decision:v1"] = "lifecycle-decision:v1"
    decision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    observation_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    scope_reference: UUID
    scope_version_id: UUID
    scope_digest: LifecycleDigest
    effect: LifecycleDecisionEffect
    authority_reference: UUID
    authority_version_id: UUID
    authority_digest: LifecycleDigest
    status: LifecycleEligibilityStatus
    legal_hold_active: bool
    expected_current_decision_id: UUID | None
    expected_current_generation: int = Field(ge=0)
    generation: int = Field(ge=1)
    actor_id: UUID
    audit_event_id: UUID
    outbox_event_id: UUID
    decided_at: datetime

    @model_validator(mode="after")
    def validate_transition(self) -> "LifecycleDecision":
        _nonzero(
            self.decision_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.observation_id,
            self.connection_id,
            self.connection_version_id,
            self.scope_reference,
            self.scope_version_id,
            self.authority_reference,
            self.authority_version_id,
            self.actor_id,
            self.audit_event_id,
            self.outbox_event_id,
        )
        if self.expected_current_decision_id is not None:
            _nonzero(self.expected_current_decision_id)
        if (self.expected_current_decision_id is None) != (
            self.expected_current_generation == 0
        ):
            raise ValueError("expected decision and generation must describe the same head")
        if self.effect is LifecycleDecisionEffect.TOMBSTONE_CANDIDATE:
            raise ValueError("evidence cannot turn a tombstone candidate into a decision")
        if self.generation != self.expected_current_generation + 1:
            raise ValueError("lifecycle decision generation must advance the expected head")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("lifecycle decision timestamp must be timezone-aware")
        if not _DIGEST.fullmatch(self.scope_digest) or not _DIGEST.fullmatch(
            self.authority_digest
        ):
            raise ValueError("lifecycle digests must be lowercase SHA-256 hex")
        return self


class CurrentLifecycleEligibility(DefinitionModel):
    """Tenant-scoped read model resolved from the authority pointer, never event time."""

    schema_version: Literal["current-lifecycle-eligibility:v1"] = (
        "current-lifecycle-eligibility:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    scope_reference: UUID
    scope_version_id: UUID
    scope_digest: LifecycleDigest
    current_decision_id: UUID | None
    generation: int = Field(ge=0)
    status: LifecycleEligibilityStatus
    legal_hold_active: bool

    @model_validator(mode="after")
    def validate_pointer(self) -> "CurrentLifecycleEligibility":
        _nonzero(
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.connection_id,
            self.connection_version_id,
            self.scope_reference,
            self.scope_version_id,
        )
        if (self.current_decision_id is None) != (self.generation == 0):
            raise ValueError("eligibility pointer and generation must describe the same head")
        if self.current_decision_id is not None:
            _nonzero(self.current_decision_id)
        if not _DIGEST.fullmatch(self.scope_digest):
            raise ValueError("lifecycle scope digest must be lowercase SHA-256 hex")
        return self


def lifecycle_status_for_observation(
    kind: LifecycleObservationKind,
    confidence: LifecycleObservationConfidence,
) -> LifecycleDecisionEffect:
    """Return a safe candidate effect; observations never mutate eligibility."""

    if kind in {
        LifecycleObservationKind.PROVIDER_OUTAGE,
        LifecycleObservationKind.PERMISSION_INACCESSIBLE,
        LifecycleObservationKind.GRANT_UNCERTAIN,
        LifecycleObservationKind.FILTERED_ABSENCE,
        LifecycleObservationKind.INCOMPLETE_SCAN,
    }:
        return LifecycleDecisionEffect.AUTHORITY_UNAVAILABLE
    if kind in {
        LifecycleObservationKind.GRANT_CONFIRMED_REVOKED,
        LifecycleObservationKind.SCOPE_APPROVAL_SUPERSEDED,
        LifecycleObservationKind.SCOPE_WITHDRAWN,
    }:
        return (
            LifecycleDecisionEffect.READ_WITHDRAWN
            if confidence is LifecycleObservationConfidence.QUALIFIED
            else LifecycleDecisionEffect.AUTHORITY_UNAVAILABLE
        )
    if kind in {
        LifecycleObservationKind.MOVE_OUTSIDE_SCOPE,
        LifecycleObservationKind.TRASHED,
        LifecycleObservationKind.DELETED_METADATA,
    }:
        return (
            LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED
            if confidence is LifecycleObservationConfidence.QUALIFIED
            else LifecycleDecisionEffect.AUTHORITY_UNAVAILABLE
        )
    if kind is LifecycleObservationKind.QUALIFIED_DELETION_EVIDENCE:
        return LifecycleDecisionEffect.TOMBSTONE_CANDIDATE
    if kind is LifecycleObservationKind.RETENTION_APPLICABILITY_MISSING:
        return LifecycleDecisionEffect.RETENTION_APPLICABILITY_UNRESOLVED
    if kind is LifecycleObservationKind.RETENTION_EXPIRED:
        return LifecycleDecisionEffect.RETENTION_INELIGIBLE
    if kind is LifecycleObservationKind.IDENTITY_CONTINUITY_UNPROVEN:
        return LifecycleDecisionEffect.IDENTITY_CONTINUITY_UNPROVEN
    if kind in {
        LifecycleObservationKind.MOVE_WITHIN_SCOPE,
        LifecycleObservationKind.MOVE_INTO_APPROVED_SCOPE,
        LifecycleObservationKind.LEGAL_HOLD_APPLIED,
        LifecycleObservationKind.LEGAL_HOLD_RELEASED,
        LifecycleObservationKind.REAPPEARED,
    }:
        return LifecycleDecisionEffect.RECORD_ONLY
    raise ValueError("unsupported lifecycle observation kind")


__all__ = [
    "CurrentLifecycleEligibility",
    "LifecycleDecision",
    "LifecycleDecisionEffect",
    "LifecycleEligibilityStatus",
    "LifecycleObservation",
    "LifecycleObservationConfidence",
    "LifecycleObservationKind",
    "LifecycleReasonCode",
    "lifecycle_status_for_observation",
]
