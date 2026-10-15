"""Provider-neutral contracts for revalidating a pinned ContextBundle.

The revalidation contract deliberately contains only immutable identities and
safe state observations.  It never carries excerpts or provider payloads.
"""

from __future__ import annotations

from enum import Enum
import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel

from .models import ContextBundle, ContextBundleOutcome, ContextBundleRef, EvidenceLocator


def _nonzero(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


class RevalidationCheckpoint(str, Enum):
    """The protected-use point at which a saved bundle is checked."""

    BEFORE_EVIDENCE_REUSE = "before_evidence_reuse"
    BEFORE_FINAL_DISCLOSURE = "before_final_disclosure"


class RevalidationDisposition(str, Enum):
    """Safe action available to the caller after a checkpoint."""

    SAFE_REPLAY = "safe_replay"
    SAFE_REGENERATION = "safe_regeneration"
    REJECTED = "rejected"
    RETRYABLE_FAILURE = "retryable_failure"
    CANCELLED = "cancelled"


class RevalidationFailureCode(str, Enum):
    """Non-leaking reasons for refusing to reuse a protected bundle."""

    BUNDLE_INTEGRITY = "bundle_integrity"
    BUNDLE_LINEAGE_MISMATCH = "bundle_lineage_mismatch"
    PIN_INCONSISTENT = "pin_inconsistent"
    AUTHORITY_GENERATION_CHANGED = "authority_generation_changed"
    POLICY_CHANGED = "policy_changed"
    POLICY_GENERATION_CHANGED = "policy_generation_changed"
    SERVICE_GRANT_EXPIRED = "service_grant_expired"
    CONTRIBUTOR_UNAUTHORIZED = "contributor_unauthorized"
    SOURCE_REVISION_REPLACED = "source_revision_replaced"
    SOURCE_REVISION_UNAVAILABLE = "source_revision_unavailable"
    SOURCE_TOMBSTONED = "source_tombstoned"
    SOURCE_SUPERSEDED = "source_superseded"
    RETENTION_INELIGIBLE = "retention_ineligible"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_CORRUPT = "evidence_corrupt"
    PROVENANCE_UNVERIFIED = "provenance_unverified"
    EVIDENCE_LINEAGE_MISMATCH = "evidence_lineage_mismatch"
    ACTIVE_PROJECTION_CHANGED = "active_projection_changed"
    REGENERATION_LIMIT = "regeneration_limit"
    REVALIDATION_UNAVAILABLE = "revalidation_unavailable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class RevalidationAvailability(str, Enum):
    AVAILABLE = "available"
    RETRYABLE_FAILURE = "retryable_failure"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"


class RevalidationPublicationImpact(str, Enum):
    """How much the current active publication may affect this attempt."""

    NONE = "none"
    ACTIVATION_ONLY = "activation_only"
    MATERIAL = "material"
    UNKNOWN = "unknown"


class RevalidationPublicationObservation(DefinitionModel):
    """Safe publication facts observed while the attempt remains pinned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pinned_snapshot_id: UUID
    pinned_activation_generation: int = Field(ge=1)
    pinned_publication_pin_id: UUID | None = None
    current_snapshot_id: UUID | None = None
    current_activation_generation: int | None = Field(default=None, ge=1)
    current_publication_available: bool = True
    pin_consistent: bool = True
    impact: RevalidationPublicationImpact = RevalidationPublicationImpact.NONE

    @model_validator(mode="after")
    def validate_publication(self) -> "RevalidationPublicationObservation":
        _nonzero(self.pinned_snapshot_id, "pinned_snapshot_id")
        if self.pinned_publication_pin_id is not None:
            _nonzero(self.pinned_publication_pin_id, "pinned_publication_pin_id")
        if self.current_snapshot_id is not None:
            _nonzero(self.current_snapshot_id, "current_snapshot_id")
        return self

    @property
    def activation_changed(self) -> bool:
        return (
            self.current_activation_generation is not None
            and self.current_activation_generation != self.pinned_activation_generation
        )

    @property
    def active_projection_changed(self) -> bool:
        return (
            self.current_snapshot_id is not None
            and self.current_snapshot_id != self.pinned_snapshot_id
        )


class RevalidationSourceObservation(DefinitionModel):
    """Current non-content facts for one protected source contributor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    current_policy_version_id: UUID | None = None
    current_policy_generation: int | None = Field(default=None, ge=1)
    membership_generation: int | None = Field(default=None, ge=1)
    authority_generation: int | None = Field(default=None, ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    service_grant_valid: bool = True
    contributor_authorized: bool = True
    current_revision_id: UUID | None = None
    current_revision_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_revision_current: bool = True
    tombstoned: bool = False
    superseded: bool = False
    retention_eligible: bool = True

    @model_validator(mode="after")
    def validate_source(self) -> "RevalidationSourceObservation":
        _nonzero(self.source_object_id, "source_object_id")
        if self.current_policy_version_id is not None:
            _nonzero(self.current_policy_version_id, "current_policy_version_id")
        if self.current_revision_id is not None:
            _nonzero(self.current_revision_id, "current_revision_id")
        return self


class RevalidationEvidenceObservation(DefinitionModel):
    """Current integrity and provenance facts for one evidence reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=128)
    source_object_id: UUID
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    projection_snapshot_id: UUID | None = None
    activation_generation: int | None = Field(default=None, ge=1)
    source_revision_id: UUID | None = None
    revision_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision_number: int | None = Field(default=None, ge=1)
    processing_receipt_id: UUID | None = None
    locator_contract_version: str | None = Field(default=None, min_length=1, max_length=128)
    locator: EvidenceLocator | None = None
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    present: bool = True
    digest_matches: bool = True
    provenance_valid: bool = True

    @model_validator(mode="after")
    def validate_evidence(self) -> "RevalidationEvidenceObservation":
        _nonzero(self.source_object_id, "source_object_id")
        for field_name in (
            "workspace_id",
            "environment_id",
            "projection_snapshot_id",
            "source_revision_id",
            "processing_receipt_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _nonzero(value, field_name)
        return self


class RevalidationObservation(DefinitionModel):
    """One atomic, content-free read of all revalidation dependencies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    publication: RevalidationPublicationObservation
    source_states: tuple[RevalidationSourceObservation, ...] = ()
    evidence_states: tuple[RevalidationEvidenceObservation, ...] = ()
    authorization_context_current: bool = True
    availability: RevalidationAvailability = RevalidationAvailability.AVAILABLE

    @model_validator(mode="after")
    def validate_observation(self) -> "RevalidationObservation":
        _nonzero(self.workspace_id, "workspace_id")
        _nonzero(self.environment_id, "environment_id")
        sources = [item.source_object_id for item in self.source_states]
        if len(set(sources)) != len(sources):
            raise ValueError("revalidation source observations must be unique")
        evidence = [item.evidence_id for item in self.evidence_states]
        if len(set(evidence)) != len(evidence):
            raise ValueError("revalidation evidence observations must be unique")
        return self


class RevalidationRequest(DefinitionModel):
    """Input to the broker revalidation seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["attempt-revalidation-request:v1"] = (
        "attempt-revalidation-request:v1"
    )
    bundle: ContextBundle
    workspace_id: UUID
    environment_id: UUID
    checkpoint: RevalidationCheckpoint
    idempotency_key: str = Field(min_length=1, max_length=256)
    cancelled: bool = False
    regeneration_count: int = Field(default=0, ge=0)
    regeneration_budget: int = Field(default=1, ge=0, le=3)
    retry_attempt: int = Field(default=1, ge=1)
    max_retry_attempts: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def validate_request(self) -> "RevalidationRequest":
        _nonzero(self.workspace_id, "workspace_id")
        _nonzero(self.environment_id, "environment_id")
        if self.bundle.workspace_id != self.workspace_id or self.bundle.environment_id != self.environment_id:
            raise ValueError("revalidation request scope does not match bundle")
        if self.regeneration_count > self.regeneration_budget:
            raise ValueError("regeneration count exceeds its bounded budget")
        return self

    @property
    def command_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"bundle"})
        payload["bundle_digest"] = self.bundle.bundle_digest
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class RevalidationRegenerationPlan(DefinitionModel):
    """A bounded handoff to a new attempt; the old bundle is never changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prior_bundle: ContextBundleRef
    prior_attempt_id: UUID
    new_attempt_id: UUID
    new_publication_pin_id: UUID
    new_idempotency_key: str = Field(min_length=1, max_length=256)
    regeneration_number: int = Field(ge=1)
    reasons: tuple[RevalidationFailureCode, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_plan(self) -> "RevalidationRegenerationPlan":
        for field_name in (
            "prior_attempt_id",
            "new_attempt_id",
            "new_publication_pin_id",
        ):
            _nonzero(getattr(self, field_name), field_name)
        if self.prior_attempt_id == self.new_attempt_id:
            raise ValueError("regeneration must create a new attempt")
        return self


class RevalidationCheckpointResult(DefinitionModel):
    """Disclosure-safe result of one revalidation checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["attempt-revalidation-result:v1"] = (
        "attempt-revalidation-result:v1"
    )
    disposition: RevalidationDisposition
    checkpoint: RevalidationCheckpoint
    workspace_id: UUID
    environment_id: UUID
    bundle_reference: ContextBundleRef | None = None
    bundle_outcome: ContextBundleOutcome | None = None
    idempotency_key: str = Field(min_length=1, max_length=256)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_codes: tuple[RevalidationFailureCode, ...] = ()
    regeneration: RevalidationRegenerationPlan | None = None
    activation_changed: bool = False
    active_projection_changed: bool = False
    retryable: bool = False
    retry_attempt: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_result(self) -> "RevalidationCheckpointResult":
        _nonzero(self.workspace_id, "workspace_id")
        _nonzero(self.environment_id, "environment_id")
        if len(set(self.failure_codes)) != len(self.failure_codes):
            raise ValueError("revalidation failure codes must be unique")
        if self.disposition is RevalidationDisposition.SAFE_REPLAY:
            if self.regeneration is not None or self.failure_codes or self.retryable:
                raise ValueError("safe replay cannot carry failure or regeneration state")
            if self.bundle_reference is None:
                raise ValueError("safe replay requires the immutable bundle reference")
        elif self.disposition is RevalidationDisposition.SAFE_REGENERATION:
            if (
                self.regeneration is None
                or not self.failure_codes
                or self.retryable
                or self.bundle_reference is not None
                or self.bundle_outcome is not None
            ):
                raise ValueError("safe regeneration requires bounded plan and reasons")
        elif self.disposition is RevalidationDisposition.RETRYABLE_FAILURE:
            if not self.retryable or self.bundle_reference is not None or self.regeneration is not None:
                raise ValueError("retryable failure cannot carry protected bundle state")
        elif self.disposition is RevalidationDisposition.CANCELLED:
            if (
                self.retryable
                or self.regeneration is not None
                or self.bundle_reference is not None
                or self.bundle_outcome not in (None, ContextBundleOutcome.CANCELLED)
            ):
                raise ValueError("cancelled revalidation cannot carry protected state")
        elif (
            self.regeneration is not None
            or self.retryable
            or self.bundle_reference is not None
            or self.bundle_outcome is not None
        ):
            raise ValueError("rejected revalidation cannot carry a regeneration or retry state")
        return self

    @property
    def safe_to_reuse_bundle(self) -> bool:
        return self.disposition is RevalidationDisposition.SAFE_REPLAY

    @property
    def safe_to_regenerate(self) -> bool:
        return self.disposition is RevalidationDisposition.SAFE_REGENERATION


def regeneration_ids(
    *, bundle: ContextBundle, request: RevalidationRequest
) -> tuple[UUID, UUID, str]:
    """Derive stable new attempt/pin identities for an idempotent regeneration."""

    seed = (
        f"{bundle.bundle_id}:{bundle.attempt_id}:{request.idempotency_key}:"
        f"{request.regeneration_count + 1}"
    )
    new_attempt = uuid5(NAMESPACE_URL, f"spine:revalidation:attempt:{seed}")
    new_pin = uuid5(NAMESPACE_URL, f"spine:revalidation:pin:{seed}")
    new_key = f"{bundle.idempotency_key}:revalidation:{request.regeneration_count + 1}"
    if len(new_key) > 256:
        new_key = f"{bundle.idempotency_key[:192]}:{hashlib.sha256(seed.encode()).hexdigest()[:48]}"
    return new_attempt, new_pin, new_key


__all__ = [
    "RevalidationAvailability",
    "RevalidationCheckpoint",
    "RevalidationCheckpointResult",
    "RevalidationDisposition",
    "RevalidationEvidenceObservation",
    "RevalidationFailureCode",
    "RevalidationObservation",
    "RevalidationPublicationImpact",
    "RevalidationPublicationObservation",
    "RevalidationRegenerationPlan",
    "RevalidationRequest",
    "RevalidationSourceObservation",
    "regeneration_ids",
]
