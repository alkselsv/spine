"""Provider-neutral contract for exact external commit-fence permits.

This contract records an authority decision; constructing or matching it does
not establish approval. Only a configured, supported authority adapter may
report an acquired permit. Existing v1 deletion, retention, report-source and
approval records remain unchanged and are not implicitly upgraded to this
contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictInt, StrictStr, model_validator

from spine.application.private_asr_corpus import PrivateArtifactSourceLineage
from spine.domain.common import ValidatedDefinitionModel


_SCOPE_TOKEN = r"^[a-z][a-z0-9_.:-]{0,63}$"
_IDEMPOTENCY_KEY = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"


def _require_utc(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a UTC timestamp")


def _require_nonzero(*identifiers: UUID) -> None:
    if any(identifier.int == 0 for identifier in identifiers):
        raise ValueError("commit-fence identifiers must be non-zero")


def _source_lineage_sort_key(
    item: PrivateArtifactSourceLineage,
) -> tuple[str, str, str]:
    return (
        str(item.source_object_id),
        str(item.source_revision_id),
        item.source_revision_digest,
    )


def canonical_commit_fence_lineage_digest(
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_lineage: tuple[PrivateArtifactSourceLineage, ...],
) -> str:
    """Hash complete source lineage independent of its input ordering.

    Identity uniqueness follows the existing v2 deletion contract: one exact
    workspace/environment/source/revision/digest tuple may occur once. Other
    lineage metadata remains part of the digest payload.
    """

    _require_nonzero(workspace_id, environment_id)
    if not source_lineage:
        raise ValueError("commit-fence source lineage must not be empty")

    identities = tuple(
        (item.source_object_id, item.source_revision_id, item.source_revision_digest)
        for item in source_lineage
    )
    if len(set(identities)) != len(identities):
        raise ValueError("commit-fence source lineage identities must be unique")

    ordered = sorted(source_lineage, key=_source_lineage_sort_key)
    payload = {
        "schema_version": "external-commit-fence-lineage:v1",
        "workspace_id": str(workspace_id),
        "environment_id": str(environment_id),
        "source_lineage": [item.model_dump(mode="json") for item in ordered],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(b"spine.external-commit-fence-lineage:v1\0" + canonical).hexdigest()


class CommitFenceBinding(ValidatedDefinitionModel):
    """Immutable identity an external authority must fence through commit."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["external-commit-fence-binding:v1"] = (
        "external-commit-fence-binding:v1"
    )
    issuer_id: UUID
    approval_version: StrictStr = Field(min_length=1, max_length=128)
    approval_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    purpose: StrictStr = Field(pattern=_SCOPE_TOKEN)
    action: StrictStr = Field(pattern=_SCOPE_TOKEN)
    subject_id: UUID
    state_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_lineage: tuple[PrivateArtifactSourceLineage, ...] = Field(min_length=1)
    lineage_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    generation: StrictInt = Field(ge=1)
    revocation_epoch: StrictInt = Field(ge=0)
    operation_id: UUID
    idempotency_key: StrictStr = Field(pattern=_IDEMPOTENCY_KEY)
    valid_from: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def validate_exact_binding(self) -> "CommitFenceBinding":
        _require_nonzero(
            self.issuer_id,
            self.workspace_id,
            self.environment_id,
            self.subject_id,
            self.operation_id,
        )
        _require_utc(self.valid_from, field_name="valid_from")
        _require_utc(self.valid_until, field_name="valid_until")
        if self.valid_until <= self.valid_from:
            raise ValueError("commit-fence validity interval must be positive")
        expected_lineage_digest = canonical_commit_fence_lineage_digest(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            source_lineage=self.source_lineage,
        )
        if self.lineage_digest != expected_lineage_digest:
            raise ValueError("commit-fence lineage digest does not match exact source lineage")
        object.__setattr__(
            self,
            "source_lineage",
            tuple(sorted(self.source_lineage, key=_source_lineage_sort_key)),
        )
        return self

    def calculate_digest(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(b"spine.external-commit-fence-binding:v1\0" + canonical).hexdigest()

    def is_valid_at(self, now: datetime) -> bool:
        """Return the half-open time-window result; equality at expiry denies."""

        _require_utc(now, field_name="now")
        return self.valid_from <= now < self.valid_until


class CommitFenceOutcomeKind(str, Enum):
    ACQUIRED = "acquired"
    DENIED = "denied"
    STALE = "stale"
    EXPIRED = "expired"
    AMBIGUOUS = "ambiguous"
    RELEASED = "released"


class CommitFenceOutcome(ValidatedDefinitionModel):
    """Authority-reported result bound to the exact fence request.

    This is a data contract, not an authority registry or permit verifier. The
    status must come from the supported authority implementation; equality of
    caller-supplied binding fields is never sufficient to infer acquisition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["external-commit-fence-outcome:v1"] = (
        "external-commit-fence-outcome:v1"
    )
    binding: CommitFenceBinding
    outcome: CommitFenceOutcomeKind
    evaluated_at: datetime
    permit_reference: UUID | None = None
    permit_valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> "CommitFenceOutcome":
        _require_utc(self.evaluated_at, field_name="evaluated_at")
        acquired = self.outcome is CommitFenceOutcomeKind.ACQUIRED
        released = self.outcome is CommitFenceOutcomeKind.RELEASED
        if acquired:
            if self.permit_reference is None or self.permit_valid_until is None:
                raise ValueError("acquired fence outcome requires an opaque permit reference")
            _require_nonzero(self.permit_reference)
            _require_utc(self.permit_valid_until, field_name="permit_valid_until")
            if not self.binding.is_valid_at(self.evaluated_at):
                raise ValueError("expired binding cannot have an acquired outcome")
            if not self.evaluated_at < self.permit_valid_until <= self.binding.valid_until:
                raise ValueError("permit validity must be bounded by the exact binding")
        elif released:
            if self.permit_reference is None or self.permit_valid_until is not None:
                raise ValueError("released outcome requires only its opaque permit reference")
            _require_nonzero(self.permit_reference)
        elif self.permit_reference is not None or self.permit_valid_until is not None:
            raise ValueError("non-acquired outcome cannot carry a permit")
        return self

    @property
    def binding_digest(self) -> str:
        return self.binding.calculate_digest()


__all__ = [
    "CommitFenceBinding",
    "CommitFenceOutcome",
    "CommitFenceOutcomeKind",
    "canonical_commit_fence_lineage_digest",
]
