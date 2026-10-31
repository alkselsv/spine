"""Immutable contracts for revalidating stored answer disclosure.

These records contain identities and authorization facts only.  Protected
question, answer and source content never cross this seam.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import ContextBundle

_DIGEST = r"^[0-9a-f]{64}$"


def _id(value: UUID, name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{name} must be a non-zero UUID")
    return value


def _time(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


class DisclosureRevalidationCheckpoint(str, Enum):
    """A current-authority boundary at which stored content may be released."""

    BEFORE_STORED_ANSWER_READ = "before_stored_answer_read"
    BEFORE_HISTORY_READ = "before_history_read"
    BEFORE_CITATION_INSPECTION = "before_citation_inspection"


class DisclosureRevalidationDisposition(str, Enum):
    SAFE_REUSE = "safe_reuse"
    REVALIDATION_REQUIRED = "revalidation_required"
    INELIGIBLE = "ineligible"
    RETRYABLE_FAILURE = "retryable_failure"
    CANCELLED = "cancelled"


class DisclosureRevalidationFailureCode(str, Enum):
    REQUESTER_UNAUTHORIZED = "requester_unauthorized"
    DEPENDENCY_MISSING = "dependency_missing"
    DEPENDENCY_SCOPE_MISMATCH = "dependency_scope_mismatch"
    POLICY_CHANGED = "policy_changed"
    POLICY_GENERATION_CHANGED = "policy_generation_changed"
    MEMBERSHIP_GENERATION_CHANGED = "membership_generation_changed"
    AUTHORITY_GENERATION_CHANGED = "authority_generation_changed"
    CONTRIBUTOR_UNAUTHORIZED = "contributor_unauthorized"
    SOURCE_REVISION_REPLACED = "source_revision_replaced"
    SOURCE_REVISION_UNAVAILABLE = "source_revision_unavailable"
    SOURCE_SUPERSEDED = "source_superseded"
    SOURCE_TOMBSTONED = "source_tombstoned"
    RETENTION_INELIGIBLE = "retention_ineligible"
    DISCLOSURE_REVIEW_REQUIRED = "disclosure_review_required"
    DISCLOSURE_HELD = "disclosure_held"
    SNAPSHOT_CHANGED = "snapshot_changed"
    BOUNDARY_CHANGED = "boundary_changed"
    AS_OF_CHANGED = "as_of_changed"
    LATER_PUBLICATION_IMPACT = "later_publication_impact"
    UNKNOWN_PUBLICATION_IMPACT = "unknown_publication_impact"
    REVALIDATION_UNAVAILABLE = "revalidation_unavailable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class DisclosurePublicationImpact(str, Enum):
    NONE = "none"
    ACTIVATION_ONLY = "activation_only"
    MATERIAL = "material"
    UNKNOWN = "unknown"


class DisclosureDependency(DefinitionModel):
    """One protected source whose content reached a generation attempt.

    The dependency is derived from the complete ContextBundle, not from final
    citations.  An uncited or intermediate-attempt contributor is therefore
    still retained as a disclosure dependency.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["disclosure-dependency:v1"] = "disclosure-dependency:v1"
    dependency_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=_DIGEST)
    policy_version_id: UUID | None = None
    policy_generation: int | None = Field(default=None, ge=1)
    membership_generation: int | None = Field(default=None, ge=1)
    authority_generation: int | None = Field(default=None, ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    projection_snapshot_id: UUID
    activation_generation: int = Field(ge=1)
    canonical_boundary_id: UUID
    canonical_boundary_generation: int = Field(ge=0)
    canonical_as_of_boundary_id: UUID
    canonical_as_of_generation: int = Field(ge=0)
    canonical_as_of_time: datetime

    @classmethod
    def from_context_bundle(cls, bundle: ContextBundle, *, source_object_id: UUID) -> "DisclosureDependency":
        references = tuple(
            reference for reference in bundle.evidence_references if reference.source_object_id == source_object_id
        )
        if not references:
            raise ValueError("context bundle has no evidence for the source")
        revisions = {reference.source_revision_id for reference in references}
        digests = {reference.revision_digest for reference in references if reference.revision_digest is not None}
        if len(revisions) != 1 or len(digests) != 1:
            raise ValueError("source dependency has conflicting revision identity")
        policy = next(
            (value for value in bundle.policy_decision_generations if value.source_object_id == source_object_id),
            None,
        )
        seed = ":".join(
            (
                str(bundle.workspace_id),
                str(bundle.environment_id),
                str(source_object_id),
                str(next(iter(revisions))),
            )
        )
        return cls(
            dependency_id=uuid5(NAMESPACE_URL, f"spine:disclosure-dependency:{seed}"),
            workspace_id=bundle.workspace_id,
            environment_id=bundle.environment_id,
            source_object_id=source_object_id,
            source_revision_id=next(iter(revisions)),
            source_revision_digest=next(iter(digests)),
            policy_version_id=policy.policy_version_id if policy else None,
            policy_generation=policy.policy_generation if policy else None,
            membership_generation=policy.membership_generation if policy else None,
            authority_generation=policy.authority_generation if policy else None,
            service_authority_generation=(policy.service_authority_generation if policy else None),
            projection_snapshot_id=bundle.projection_snapshot.projection_snapshot_id,
            activation_generation=bundle.activation_generation,
            canonical_boundary_id=bundle.canonical_boundary.boundary_id,
            canonical_boundary_generation=bundle.canonical_boundary.accepted_through_generation,
            canonical_as_of_boundary_id=bundle.canonical_as_of.canonical_boundary_id,
            canonical_as_of_generation=bundle.canonical_as_of.accepted_through_generation,
            canonical_as_of_time=bundle.canonical_as_of.boundary_time,
        )

    @model_validator(mode="after")
    def validate_identity(self) -> "DisclosureDependency":
        for name in (
            "dependency_id",
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
            "projection_snapshot_id",
            "canonical_boundary_id",
            "canonical_as_of_boundary_id",
        ):
            _id(getattr(self, name), name)
        _time(self.canonical_as_of_time, "canonical_as_of_time")
        if self.canonical_boundary_id != self.canonical_as_of_boundary_id:
            raise ValueError("dependency boundary and as_of identity differ")
        return self


class DisclosureDependencySet(DefinitionModel):
    """The complete, deduplicated source set carried by a stored answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["disclosure-dependency-set:v1"] = "disclosure-dependency-set:v1"
    dependencies: tuple[DisclosureDependency, ...] = ()

    @classmethod
    def from_context_bundle(cls, bundle: ContextBundle) -> "DisclosureDependencySet":
        source_ids = dict.fromkeys(reference.source_object_id for reference in bundle.evidence_references)
        return cls(
            dependencies=tuple(
                DisclosureDependency.from_context_bundle(bundle, source_object_id=source_id) for source_id in source_ids
            )
        )

    def merge(self, other: "DisclosureDependencySet") -> "DisclosureDependencySet":
        if self.dependencies and other.dependencies:
            scopes = {(item.workspace_id, item.environment_id) for item in (*self.dependencies, *other.dependencies)}
            if len(scopes) != 1:
                raise ValueError("disclosure dependencies have mixed scopes")
        values = {item.dependency_id: item for item in self.dependencies}
        for item in other.dependencies:
            existing = values.get(item.dependency_id)
            if existing is not None and existing != item:
                raise ValueError("disclosure dependency identity conflicts")
            values[item.dependency_id] = item
        return type(self)(dependencies=tuple(sorted(values.values(), key=lambda x: x.dependency_id.hex)))

    @model_validator(mode="after")
    def validate_dependencies(self) -> "DisclosureDependencySet":
        ids = [item.dependency_id for item in self.dependencies]
        if len(ids) != len(set(ids)):
            raise ValueError("disclosure dependency IDs must be unique")
        scopes = {(item.workspace_id, item.environment_id) for item in self.dependencies}
        if len(scopes) > 1:
            raise ValueError("disclosure dependencies have mixed scopes")
        return self


class DisclosureDependencyObservation(DefinitionModel):
    """Current non-content facts for one dependency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dependency_id: UUID
    source_object_id: UUID
    current_policy_version_id: UUID | None = None
    current_policy_generation: int | None = Field(default=None, ge=1)
    membership_generation: int | None = Field(default=None, ge=1)
    authority_generation: int | None = Field(default=None, ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    contributor_authorized: bool = False
    current_revision_id: UUID | None = None
    current_revision_digest: str | None = Field(default=None, pattern=_DIGEST)
    historical_revision_id: UUID | None = None
    historical_revision_digest: str | None = Field(default=None, pattern=_DIGEST)
    revision_current: bool = False
    superseded: bool = False
    tombstoned: bool = False
    retention_eligible: bool = False
    lifecycle_disclosure_eligible: bool
    active_disclosure_hold: bool
    historical_revision_available: bool = False

    @model_validator(mode="after")
    def validate_observation(self) -> "DisclosureDependencyObservation":
        _id(self.dependency_id, "dependency_id")
        _id(self.source_object_id, "source_object_id")
        if self.current_policy_version_id is not None:
            _id(self.current_policy_version_id, "current_policy_version_id")
        if self.current_revision_id is not None:
            _id(self.current_revision_id, "current_revision_id")
        if self.historical_revision_id is not None:
            _id(self.historical_revision_id, "historical_revision_id")
        return self


class DisclosurePublicationObservation(DefinitionModel):
    """Current publication identity compared with the pinned answer context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pinned_snapshot_id: UUID
    pinned_activation_generation: int = Field(ge=1)
    pinned_boundary_id: UUID
    pinned_as_of_boundary_id: UUID
    pinned_as_of_generation: int = Field(ge=0)
    current_snapshot_id: UUID | None = None
    current_activation_generation: int | None = Field(default=None, ge=1)
    current_boundary_id: UUID | None = None
    current_as_of_boundary_id: UUID | None = None
    current_as_of_generation: int | None = Field(default=None, ge=0)
    impact: DisclosurePublicationImpact = DisclosurePublicationImpact.NONE

    @property
    def snapshot_changed(self) -> bool:
        return self.current_snapshot_id is not None and self.current_snapshot_id != self.pinned_snapshot_id

    @property
    def boundary_changed(self) -> bool:
        return self.current_boundary_id is not None and self.current_boundary_id != self.pinned_boundary_id

    @property
    def as_of_changed(self) -> bool:
        return self.current_as_of_boundary_id is not None and (
            self.current_as_of_boundary_id != self.pinned_as_of_boundary_id
            or self.current_as_of_generation != self.pinned_as_of_generation
        )

    @model_validator(mode="after")
    def validate_publication(self) -> "DisclosurePublicationObservation":
        for name in (
            "pinned_snapshot_id",
            "pinned_boundary_id",
            "pinned_as_of_boundary_id",
        ):
            _id(getattr(self, name), name)
        for name in (
            "current_snapshot_id",
            "current_boundary_id",
            "current_as_of_boundary_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _id(value, name)
        return self


class DisclosureRevalidationObservation(DefinitionModel):
    """One atomic authority read used by a disclosure checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    requester_authorized: bool = False
    publication: DisclosurePublicationObservation
    dependencies: tuple[DisclosureDependencyObservation, ...] = ()
    available: bool = True

    @model_validator(mode="after")
    def validate_observation(self) -> "DisclosureRevalidationObservation":
        _id(self.workspace_id, "workspace_id")
        _id(self.environment_id, "environment_id")
        ids = [item.dependency_id for item in self.dependencies]
        if len(ids) != len(set(ids)):
            raise ValueError("disclosure observations must be unique")
        return self


class DisclosureRevalidationRequest(DefinitionModel):
    """Content-free request for one stored-answer/history disclosure check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["stored-answer-disclosure-request:v1"] = "stored-answer-disclosure-request:v1"
    request_id: UUID
    workspace_id: UUID
    environment_id: UUID
    requester_id: UUID
    checkpoint: DisclosureRevalidationCheckpoint
    dependencies: DisclosureDependencySet
    run_id: UUID | None = None
    attempt_id: UUID | None = None
    message_id: UUID | None = None
    cancelled: bool = False
    historical_inspection: bool = False
    force_fresh: bool = False

    @property
    def idempotency_key(self) -> str:
        return f"disclosure:{self.request_id}:{self.checkpoint.value}"

    @property
    def command_digest(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"historical_inspection", "force_fresh"},
        )
        if self.historical_inspection:
            payload["historical_inspection"] = True
        if self.force_fresh:
            payload["force_fresh"] = True
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def validate_request(self) -> "DisclosureRevalidationRequest":
        for name in ("request_id", "workspace_id", "environment_id", "requester_id"):
            _id(getattr(self, name), name)
        for name in ("run_id", "attempt_id", "message_id"):
            value = getattr(self, name)
            if value is not None:
                _id(value, name)
        for dependency in self.dependencies.dependencies:
            if dependency.workspace_id != self.workspace_id or dependency.environment_id != self.environment_id:
                raise ValueError("disclosure request dependencies have a different scope")
        return self


class DisclosureValidationFact(DefinitionModel):
    """Immutable, safe-to-audit result for one disclosure checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["disclosure-validation-fact:v1"] = "disclosure-validation-fact:v1"
    validation_id: UUID
    request_id: UUID
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID | None = None
    attempt_id: UUID | None = None
    message_id: UUID | None = None
    checkpoint: DisclosureRevalidationCheckpoint
    dependency_id: UUID | None = None
    disposition: DisclosureRevalidationDisposition
    failure_codes: tuple[DisclosureRevalidationFailureCode, ...] = ()
    evaluated_at: datetime
    fact_digest: str = Field(pattern=_DIGEST)

    @classmethod
    def create(cls, **values: object) -> "DisclosureValidationFact":
        payload = dict(values)
        payload.pop("fact_digest", None)
        encoded = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode()
        payload["fact_digest"] = hashlib.sha256(encoded).hexdigest()
        return cls(**payload)

    @model_validator(mode="after")
    def validate_fact(self) -> "DisclosureValidationFact":
        for name in ("validation_id", "request_id", "workspace_id", "environment_id"):
            _id(getattr(self, name), name)
        for name in ("dependency_id", "run_id", "attempt_id", "message_id"):
            value = getattr(self, name)
            if value is not None:
                _id(value, name)
        _time(self.evaluated_at, "evaluated_at")
        if len(set(self.failure_codes)) != len(self.failure_codes):
            raise ValueError("validation failure codes must be unique")
        payload = {
            "validation_id": self.validation_id,
            "request_id": self.request_id,
            "workspace_id": self.workspace_id,
            "environment_id": self.environment_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "message_id": self.message_id,
            "checkpoint": self.checkpoint,
            "dependency_id": self.dependency_id,
            "disposition": self.disposition,
            "failure_codes": self.failure_codes,
            "evaluated_at": self.evaluated_at,
        }
        expected = hashlib.sha256(
            json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if expected != self.fact_digest:
            raise ValueError("validation fact digest does not match its fields")
        return self


def _jsonable(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


class DisclosureRevalidationResult(DefinitionModel):
    """Disclosure-safe aggregate returned by a current-authority check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["stored-answer-disclosure-result:v1"] = "stored-answer-disclosure-result:v1"
    request_id: UUID
    request_digest: str = Field(pattern=_DIGEST)
    workspace_id: UUID
    environment_id: UUID
    checkpoint: DisclosureRevalidationCheckpoint
    disposition: DisclosureRevalidationDisposition
    failure_codes: tuple[DisclosureRevalidationFailureCode, ...] = ()
    safe_reason: str = Field(min_length=1, max_length=128)
    facts: tuple[DisclosureValidationFact, ...] = ()

    @property
    def can_disclose_content(self) -> bool:
        return self.disposition is DisclosureRevalidationDisposition.SAFE_REUSE

    @model_validator(mode="after")
    def validate_result(self) -> "DisclosureRevalidationResult":
        for name in ("request_id", "workspace_id", "environment_id"):
            _id(getattr(self, name), name)
        if len(set(self.failure_codes)) != len(self.failure_codes):
            raise ValueError("result failure codes must be unique")
        if self.disposition is DisclosureRevalidationDisposition.SAFE_REUSE and self.failure_codes:
            raise ValueError("safe disclosure cannot carry failure codes")
        return self


__all__ = [
    "DisclosureDependency",
    "DisclosureDependencyObservation",
    "DisclosureDependencySet",
    "DisclosurePublicationImpact",
    "DisclosurePublicationObservation",
    "DisclosureRevalidationCheckpoint",
    "DisclosureRevalidationDisposition",
    "DisclosureRevalidationFailureCode",
    "DisclosureRevalidationObservation",
    "DisclosureRevalidationRequest",
    "DisclosureRevalidationResult",
    "DisclosureValidationFact",
]
