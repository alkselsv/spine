"""The immutable Context Bundle domain record.

The bundle is the exact, authorized handoff from one broker attempt to a
downstream consumer.  It stores references and lineage, never protected
payload.  Physical projection data remains in the rebuildable Context Graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.context_profiles import ContextProfileRef
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionConfigVersion,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionReconciliationResult,
    ProjectionSnapshot,
)
from spine.domain.sources import CanonicalBoundary


CONTEXT_BUNDLE_SCHEMA = "context-bundle:v1"
_DIGEST = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,127}$"


def _require_id(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class ContextBundleOutcome(str, Enum):
    """Disclosure-safe retrieval outcomes that may be persisted in a bundle."""

    ALLOWED = "allowed"
    DEGRADED = "degraded"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    INSUFFICIENT = "insufficient"
    ABSTAINED = "abstained"


class ContextBundleState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class ContextBundleEvidenceStatus(str, Enum):
    """Safe validation state of one persisted evidence reference."""

    VALID = "valid"
    MISSING = "missing"
    STALE = "stale"
    TOMBSTONED = "tombstoned"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"
    INACCESSIBLE = "inaccessible"
    FABRICATED = "fabricated"
    CORRUPT = "corrupt"


class EvidenceLocator(DefinitionModel):
    """Closed, content-free locator contract for evidence revalidation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["stable-locator:v1"] = "stable-locator:v1"
    kind: Literal[
        "page",
        "sheet_row",
        "message",
        "character_range",
        "record_field",
        "custom",
    ]
    value: str = Field(min_length=1, max_length=512)


class ContextBundleEvidenceReference(DefinitionModel):
    """A stable, source-revision-specific evidence pointer.

    The reference intentionally contains no excerpt or provider payload.  The
    pinned publication and policy-generation records around it make the
    reference revalidatable without making it a disclosure bypass.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["context-evidence-reference:v1"] = (
        "context-evidence-reference:v1"
    )
    evidence_id: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    environment_id: UUID
    projection_snapshot_id: UUID
    activation_generation: int = Field(ge=1)
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str | None = Field(default=None, pattern=_DIGEST)
    revision_number: int | None = Field(default=None, ge=1)
    processing_receipt_id: UUID | None = None
    locator_contract_version: str | None = Field(default=None, min_length=1, max_length=128)
    locator: EvidenceLocator
    evidence_digest: str = Field(pattern=_DIGEST)
    status: ContextBundleEvidenceStatus = ContextBundleEvidenceStatus.VALID

    @model_validator(mode="after")
    def validate_identity(self) -> "ContextBundleEvidenceReference":
        for field_name in (
            "workspace_id",
            "environment_id",
            "projection_snapshot_id",
            "source_object_id",
            "source_revision_id",
        ):
            _require_id(getattr(self, field_name), field_name)
        if self.processing_receipt_id is not None:
            _require_id(self.processing_receipt_id, "processing_receipt_id")
        return self


class PolicyDecisionGeneration(DefinitionModel):
    """The authorization generations used for one evidence contributor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    policy_version_id: UUID
    policy_generation: int = Field(ge=1)
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    decision_reference: UUID

    @model_validator(mode="after")
    def validate_identity(self) -> "PolicyDecisionGeneration":
        for field_name in (
            "source_object_id",
            "policy_version_id",
            "decision_reference",
        ):
            _require_id(getattr(self, field_name), field_name)
        return self


class ContextBundleRef(DefinitionModel):
    """The public ContextBundleRef:v1 identity from CB-01."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": "spine/context-bundle-ref:v1"},
    )

    bundle_id: UUID
    bundle_version: int = Field(ge=1)
    bundle_digest: str = Field(pattern=_DIGEST)
    publication_pin_id: UUID | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "ContextBundleRef":
        _require_id(self.bundle_id, "bundle_id")
        if self.publication_pin_id is not None:
            _require_id(self.publication_pin_id, "publication_pin_id")
        return self


class ContextBundleLineageRef(DefinitionModel):
    """Exact bundle identity together with its owning tenant and attempt."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": "spine/context-bundle-lineage-ref:v1"},
    )

    schema_version: Literal["context-bundle-lineage-ref:v1"] = (
        "context-bundle-lineage-ref:v1"
    )
    bundle: ContextBundleRef
    workspace_id: UUID
    environment_id: UUID
    run_id: UUID
    attempt_id: UUID

    @model_validator(mode="after")
    def validate_identity(self) -> "ContextBundleLineageRef":
        for field_name in ("workspace_id", "environment_id", "run_id", "attempt_id"):
            _require_id(getattr(self, field_name), field_name)
        return self


class ContextBundle(DefinitionModel):
    """One immutable, replayable broker handoff for one run and attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[CONTEXT_BUNDLE_SCHEMA] = CONTEXT_BUNDLE_SCHEMA
    bundle_id: UUID
    bundle_version: int = Field(ge=1)
    bundle_digest: str = Field(pattern=_DIGEST)
    workspace_id: UUID
    environment_id: UUID
    profile_ref: ContextProfileRef
    trusted_request_context_ref: UUID
    authorization_context_ref: UUID
    request_id: UUID
    run_id: UUID
    attempt_id: UUID
    publication_pin_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=256)
    projection_snapshot: ProjectionSnapshot
    projection_activation: ProjectionActivation
    activation_generation: int = Field(ge=1)
    canonical_boundary: CanonicalBoundary
    canonical_as_of: CanonicalAsOf
    projection_config_version: ProjectionConfigVersion
    freshness: ProjectionFreshness
    health: ProjectionHealth
    effective_reconciliation: ProjectionReconciliationResult | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "effective_reconciliation", "reconciliation_evidence"
        ),
    )
    retrieval_strategy: str = Field(pattern=_IDENTIFIER)
    outcome: ContextBundleOutcome
    reason_code: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    evidence_references: tuple[ContextBundleEvidenceReference, ...] = ()
    policy_decision_generations: tuple[PolicyDecisionGeneration, ...] = ()
    state: ContextBundleState = ContextBundleState.COMPLETE
    created_at: datetime

    @classmethod
    def create(cls, **values: Any) -> "ContextBundle":
        values = dict(values)
        values.pop("bundle_digest", None)
        values.setdefault("schema_version", CONTEXT_BUNDLE_SCHEMA)
        values.setdefault("publication_pin_id", None)
        values.setdefault(
            "effective_reconciliation", values.pop("reconciliation_evidence", None)
        )
        values.setdefault("evidence_references", ())
        values.setdefault("policy_decision_generations", ())
        values.setdefault("state", ContextBundleState.COMPLETE)
        values["bundle_digest"] = cls._digest_for_values(values)
        return cls.model_validate(values)

    @classmethod
    def calculate_digest(cls, bundle: "ContextBundle") -> str:
        return cls._digest_for_values(
            bundle.model_dump(mode="python", exclude={"bundle_digest"})
        )

    @property
    def reference(self) -> ContextBundleRef:
        return ContextBundleRef(
            bundle_id=self.bundle_id,
            bundle_version=self.bundle_version,
            bundle_digest=self.bundle_digest,
            publication_pin_id=self.publication_pin_id,
        )

    @property
    def lineage_reference(self) -> ContextBundleLineageRef:
        return ContextBundleLineageRef(
            bundle=self.reference,
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
        )

    @staticmethod
    def _digest_for_values(values: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            {"schema": CONTEXT_BUNDLE_SCHEMA, "payload": _json_value(values)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def validate_bundle(self) -> "ContextBundle":
        for field_name in (
            "bundle_id",
            "workspace_id",
            "environment_id",
            "trusted_request_context_ref",
            "authorization_context_ref",
            "request_id",
            "run_id",
            "attempt_id",
        ):
            _require_id(getattr(self, field_name), field_name)
        if self.publication_pin_id is not None:
            _require_id(self.publication_pin_id, "publication_pin_id")
        _aware(self.created_at, "created_at")
        if self.bundle_digest != self.calculate_digest(self):
            raise ValueError("context bundle digest does not match content")
        if self.profile_ref.profile_id.int == 0:
            raise ValueError("profile reference is invalid")
        nested = (
            self.projection_snapshot,
            self.projection_activation,
            self.canonical_boundary,
            self.projection_config_version,
        )
        if any(
            item.workspace_id != self.workspace_id or item.environment_id != self.environment_id
            for item in nested
        ):
            raise ValueError("context bundle lineage has a mixed workspace or environment")
        if (
            self.projection_snapshot.projection_snapshot_id
            != self.projection_activation.projection_snapshot_id
            or self.projection_activation.activation_generation != self.activation_generation
            or self.projection_snapshot.canonical_boundary_id
            != self.canonical_boundary.boundary_id
            or self.canonical_as_of.canonical_boundary_id
            != self.canonical_boundary.boundary_id
            or self.canonical_as_of.accepted_through_generation
            != self.canonical_boundary.accepted_through_generation
            or self.projection_snapshot.projection_config_version_id
            != self.projection_config_version.projection_config_version_id
        ):
            raise ValueError("context bundle lineage has a mixed snapshot or generation")
        if self.effective_reconciliation is not None and (
            self.effective_reconciliation.workspace_id != self.workspace_id
            or self.effective_reconciliation.environment_id != self.environment_id
            or self.effective_reconciliation.projection_snapshot_id
            != self.projection_snapshot.projection_snapshot_id
            or self.effective_reconciliation.projection_activation_id
            != self.projection_activation.projection_activation_id
            or self.effective_reconciliation.activation_generation != self.activation_generation
        ):
            raise ValueError("context bundle reconciliation evidence is out of scope")
        if self.state is ContextBundleState.PARTIAL and self.outcome is not ContextBundleOutcome.PARTIAL:
            raise ValueError("partial bundle state requires a partial outcome")
        if self.state is ContextBundleState.CANCELLED and self.outcome is not ContextBundleOutcome.CANCELLED:
            raise ValueError("cancelled bundle state requires a cancelled outcome")
        if self.outcome is ContextBundleOutcome.PARTIAL and self.state is not ContextBundleState.PARTIAL:
            raise ValueError("partial outcome requires partial bundle state")
        if self.outcome is ContextBundleOutcome.CANCELLED and self.state is not ContextBundleState.CANCELLED:
            raise ValueError("cancelled outcome requires cancelled bundle state")
        if self.outcome in {
            ContextBundleOutcome.ALLOWED,
            ContextBundleOutcome.DEGRADED,
            ContextBundleOutcome.PARTIAL,
        } and not self.evidence_references:
            raise ValueError("an evidence-bearing outcome requires evidence references")
        if self.outcome is ContextBundleOutcome.ALLOWED and any(
            evidence.status is not ContextBundleEvidenceStatus.VALID
            for evidence in self.evidence_references
        ):
            raise ValueError("allowed bundle requires valid evidence references")
        for evidence in self.evidence_references:
            if (
                evidence.workspace_id != self.workspace_id
                or evidence.environment_id != self.environment_id
                or evidence.projection_snapshot_id
                != self.projection_snapshot.projection_snapshot_id
                or evidence.activation_generation != self.activation_generation
            ):
                raise ValueError("evidence reference has mixed tenant or snapshot lineage")
        evidence_ids = [item.evidence_id for item in self.evidence_references]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("context bundle evidence identifiers must be unique")
        contributors = {item.source_object_id for item in self.evidence_references}
        decisions = {item.source_object_id for item in self.policy_decision_generations}
        if not contributors <= decisions:
            raise ValueError("every evidence reference requires a policy decision generation")
        return self

    @property
    def reconciliation_evidence(self) -> ProjectionReconciliationResult | None:
        """Compatibility name used by the persistence ticket wording."""

        return self.effective_reconciliation


__all__ = [
    "CONTEXT_BUNDLE_SCHEMA",
    "ContextBundle",
    "ContextBundleEvidenceReference",
    "ContextBundleEvidenceStatus",
    "ContextBundleLineageRef",
    "ContextBundleOutcome",
    "ContextBundleRef",
    "ContextBundleState",
    "EvidenceLocator",
    "PolicyDecisionGeneration",
]
