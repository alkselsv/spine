"""Immutable ContextProfile definitions shared by broker and persistence."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Any, Mapping
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import ValidatedDefinitionModel


CONTEXT_PROFILE_SCHEMA = "context-profile:v1"
_DIGEST = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"


class RetrievalStrategy(str, Enum):
    CHUNKS = "chunks"
    HYBRID = "hybrid"
    GRAPH_COMPLETION = "graph_completion"


class CompletenessRequirement(str, Enum):
    BEST_EFFORT = "best_effort"
    COMPLETE = "complete"
    EXHAUSTIVE = "exhaustive"


class PartialAnswerFallback(str, Enum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"


def _require_id(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class ContextProfileRef(ValidatedDefinitionModel):
    """The public ContextProfileRef:v1 identity owned by CB-01."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": "spine/context-profile-ref:v1"},
    )

    profile_id: UUID
    profile_version: int = Field(ge=1)
    profile_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_identity(self) -> "ContextProfileRef":
        _require_id(self.profile_id, "profile_id")
        return self


class ContextProfileVersion(ValidatedDefinitionModel):
    """One immutable server-approved retrieval restriction set.

    A profile contains no content permission, source identity, projection pin,
    or authorization policy. It can only reduce the work a broker performs
    after authorization has already been evaluated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: UUID
    workspace_id: UUID
    environment_id: UUID
    profile_version: int = Field(ge=1)
    profile_digest: str = Field(pattern=_DIGEST)
    schema_version: str = CONTEXT_PROFILE_SCHEMA
    retrieval_strategy: RetrievalStrategy
    max_results: int = Field(ge=1, le=1000)
    max_tokens: int = Field(ge=1, le=65536)
    max_relation_hops: int = Field(ge=0, le=8)
    freshness_max_age_seconds: int = Field(ge=0, le=31_536_000)
    completeness_requirement: CompletenessRequirement
    partial_answer_fallback: PartialAnswerFallback
    server_approved: bool
    revoked: bool = False
    predecessor_profile_id: UUID | None = None
    created_at: datetime

    @classmethod
    def create(cls, **values: Any) -> "ContextProfileVersion":
        """Create a profile while calculating its canonical SHA-256 digest."""

        values = dict(values)
        values.pop("profile_digest", None)
        values.setdefault("schema_version", CONTEXT_PROFILE_SCHEMA)
        values.setdefault("revoked", False)
        values.setdefault("predecessor_profile_id", None)
        values["profile_digest"] = cls._digest_for_values(values)
        return cls.model_validate(values)

    @property
    def reference(self) -> ContextProfileRef:
        return ContextProfileRef(
            profile_id=self.profile_id,
            profile_version=self.profile_version,
            profile_digest=self.profile_digest,
        )

    @classmethod
    def calculate_digest(cls, profile: "ContextProfileVersion") -> str:
        return cls._digest_for_values(
            profile.model_dump(mode="python", exclude={"profile_digest"})
        )

    @staticmethod
    def _digest_for_values(values: Mapping[str, Any]) -> str:
        payload = _json_value(values)
        encoded = json.dumps(
            {"schema": CONTEXT_PROFILE_SCHEMA, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @model_validator(mode="after")
    def validate_profile(self) -> "ContextProfileVersion":
        _require_id(self.profile_id, "profile_id")
        _require_id(self.workspace_id, "workspace_id")
        _require_id(self.environment_id, "environment_id")
        if self.schema_version != CONTEXT_PROFILE_SCHEMA:
            raise ValueError("context profile schema is incompatible")
        if self.profile_version == 1 and self.predecessor_profile_id is not None:
            raise ValueError("profile version 1 cannot have a predecessor")
        if self.profile_version > 1 and self.predecessor_profile_id is None:
            raise ValueError("replacement profile versions require a predecessor")
        if self.predecessor_profile_id is not None:
            _require_id(self.predecessor_profile_id, "predecessor_profile_id")
        if self.predecessor_profile_id == self.profile_id:
            raise ValueError("profile cannot precede itself")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if not self.server_approved:
            raise ValueError("profile must be server-approved")
        if self.profile_digest != self.calculate_digest(self):
            raise ValueError("context profile digest does not match configuration")
        return self


__all__ = [
    "CONTEXT_PROFILE_SCHEMA",
    "CompletenessRequirement",
    "ContextProfileRef",
    "ContextProfileVersion",
    "PartialAnswerFallback",
    "RetrievalStrategy",
]
