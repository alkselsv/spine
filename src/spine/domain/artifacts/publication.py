"""Immutable contracts for publishing transcript text as governed evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from enum import Enum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StrictBool, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier

from .transcription import TranscriptSegment, TranscriptionArtifact
from .transcript_review import CorrectedTranscriptArtifact

TranscriptPublicationArtifact = TranscriptionArtifact | CorrectedTranscriptArtifact


_DIGEST = r"^[0-9a-f]{64}$"


class TimeRangeLocator(DefinitionModel):
    """A stable, half-open locator into one immutable transcript version."""

    schema_version: Literal["time-range-locator:v1"] = "time-range-locator:v1"
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=_DIGEST)
    transcript_artifact_id: UUID
    transcript_artifact_digest: str = Field(pattern=_DIGEST)
    transcript_artifact_schema_version: VersionIdentifier
    projection_version: VersionIdentifier = "transcript-projection:v1"
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    segment_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> "TimeRangeLocator":
        identifiers = (
            self.source_object_id,
            self.source_revision_id,
            self.transcript_artifact_id,
        )
        if any(identifier.int == 0 for identifier in identifiers):
            raise ValueError("time-range locator identifiers must be non-zero")
        if self.end_ms <= self.start_ms:
            raise ValueError("time-range locator end_ms must be greater than start_ms")
        if any(identifier.int == 0 for identifier in self.segment_ids):
            raise ValueError("time-range locator segment ids must be non-zero")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("time-range locator segment ids must be unique")
        return self

    @classmethod
    def from_segment(
        cls,
        *,
        artifact: TranscriptPublicationArtifact,
        segment: TranscriptSegment,
        projection_version: VersionIdentifier = "transcript-projection:v1",
    ) -> "TimeRangeLocator":
        if segment not in artifact.segments:
            raise ValueError("segment does not belong to transcript artifact")
        return cls(
            source_object_id=artifact.source_object_id,
            source_revision_id=artifact.source_revision_id,
            source_revision_digest=artifact.source_revision_digest,
            transcript_artifact_id=artifact.artifact_id,
            transcript_artifact_digest=artifact.artifact_digest,
            transcript_artifact_schema_version=artifact.schema_version,
            projection_version=projection_version,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            segment_ids=(segment.segment_id,),
        )

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"schema_version"})
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class TranscriptPublicationApproval(DefinitionModel):
    """An explicit human decision required by a publication policy."""

    schema_version: Literal["transcript-publication-approval:v1"] = (
        "transcript-publication-approval:v1"
    )
    approval_id: UUID
    workspace_id: UUID
    environment_id: UUID
    transcript_artifact_id: UUID
    transcript_artifact_digest: str = Field(pattern=_DIGEST)
    policy_reference: VersionIdentifier
    approved: StrictBool
    approver_id: UUID
    decided_at: datetime

    @model_validator(mode="after")
    def validate_shape(self) -> "TranscriptPublicationApproval":
        if any(
            identifier.int == 0
            for identifier in (
                self.approval_id,
                self.workspace_id,
                self.environment_id,
                self.transcript_artifact_id,
                self.approver_id,
            )
        ):
            raise ValueError("publication approval identifiers must be non-zero")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("publication approval timestamp must be timezone-aware")
        return self


class TranscriptPublicationState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    STALE = "stale"


class TranscriptProjectionState(str, Enum):
    CURRENT = "current"
    STALE = "stale"


class TranscriptProjectionSegment(DefinitionModel):
    """Text copied into a publication while retaining exact artifact lineage."""

    schema_version: Literal["transcript-projection-segment:v1"] = (
        "transcript-projection-segment:v1"
    )
    segment_id: UUID
    ordinal: StrictInt = Field(gt=0)
    text: str = Field(min_length=1)
    text_digest: str = Field(pattern=_DIGEST)
    locator: TimeRangeLocator

    @model_validator(mode="after")
    def validate_text_digest(self) -> "TranscriptProjectionSegment":
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_digest != expected:
            raise ValueError("transcript projection text digest does not match text")
        if self.segment_id not in self.locator.segment_ids:
            raise ValueError("transcript projection locator does not identify segment")
        return self

    @classmethod
    def from_segment(
        cls,
        *,
        artifact: TranscriptPublicationArtifact,
        segment: TranscriptSegment,
        projection_version: VersionIdentifier = "transcript-projection:v1",
    ) -> "TranscriptProjectionSegment":
        return cls(
            segment_id=segment.segment_id,
            ordinal=segment.ordinal,
            text=segment.text,
            text_digest=hashlib.sha256(segment.text.encode("utf-8")).hexdigest(),
            locator=TimeRangeLocator.from_segment(
                artifact=artifact,
                segment=segment,
                projection_version=projection_version,
            ),
        )

    def with_projection_version(
        self, *, projection_version: VersionIdentifier
    ) -> "TranscriptProjectionSegment":
        locator = self.locator.model_copy(
            update={"projection_version": projection_version}
        )
        return type(self).model_validate(
            {**self.model_dump(mode="python"), "locator": locator}
        )


class TranscriptPublication(DefinitionModel):
    """Immutable active or historical publication of one transcript artifact."""

    schema_version: Literal["transcript-publication:v1"] = "transcript-publication:v1"
    publication_id: UUID
    publication_digest: str = Field(pattern=_DIGEST)
    predecessor_publication_id: UUID | None = None
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=_DIGEST)
    transcript_artifact_id: UUID
    transcript_artifact_digest: str = Field(pattern=_DIGEST)
    transcript_artifact_schema_version: VersionIdentifier
    policy_reference: VersionIdentifier
    approval_id: UUID | None = None
    partial_allowed: bool = False
    projection_version: VersionIdentifier
    projection_state: TranscriptProjectionState = TranscriptProjectionState.CURRENT
    state: TranscriptPublicationState = TranscriptPublicationState.ACTIVE
    segments: tuple[TranscriptProjectionSegment, ...] = ()
    published_at: datetime

    @model_validator(mode="after")
    def validate_lineage(self) -> "TranscriptPublication":
        if any(
            identifier.int == 0
            for identifier in (
                self.publication_id,
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                self.source_revision_id,
                self.transcript_artifact_id,
            )
        ):
            raise ValueError("transcript publication identifiers must be non-zero")
        if (
            self.predecessor_publication_id is not None
            and self.predecessor_publication_id.int == 0
        ):
            raise ValueError("predecessor publication id must be non-zero")
        if self.approval_id is not None and self.approval_id.int == 0:
            raise ValueError("transcript publication approval must be non-zero")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("transcript publication timestamp must be timezone-aware")
        if tuple(segment.ordinal for segment in self.segments) != tuple(
            range(1, len(self.segments) + 1)
        ):
            raise ValueError("transcript projection segment ordinals must be contiguous")
        for segment in self.segments:
            locator = segment.locator
            if (
                locator.source_object_id != self.source_object_id
                or locator.source_revision_id != self.source_revision_id
                or locator.source_revision_digest != self.source_revision_digest
                or locator.transcript_artifact_id != self.transcript_artifact_id
                or locator.transcript_artifact_digest != self.transcript_artifact_digest
            ):
                raise ValueError("transcript projection lineage is inconsistent")
        if self.publication_digest != self.calculate_digest(self):
            raise ValueError("transcript publication digest does not match content")
        return self

    @classmethod
    def create(
        cls,
        *,
        artifact: TranscriptPublicationArtifact,
        policy_reference: str,
        approval_id: UUID | None,
        partial_allowed: bool = False,
        projection_version: str,
        published_at: datetime,
    ) -> "TranscriptPublication":
        publication_id = uuid5(
            NAMESPACE_URL,
            "spine:transcript-publication:v1:"
            f"{artifact.artifact_id}:{artifact.artifact_digest}:{policy_reference}:{approval_id or 'none'}",
        )
        segments = tuple(
            TranscriptProjectionSegment.from_segment(
                artifact=artifact,
                segment=segment,
                projection_version=projection_version,
            )
            for segment in artifact.segments
        )
        values = {
            "schema_version": "transcript-publication:v1",
            "publication_id": publication_id,
            "predecessor_publication_id": None,
            "workspace_id": artifact.workspace_id,
            "environment_id": artifact.environment_id,
            "source_object_id": artifact.source_object_id,
            "source_revision_id": artifact.source_revision_id,
            "source_revision_digest": artifact.source_revision_digest,
            "transcript_artifact_id": artifact.artifact_id,
            "transcript_artifact_digest": artifact.artifact_digest,
            "transcript_artifact_schema_version": artifact.schema_version,
            "policy_reference": policy_reference,
            "approval_id": approval_id,
            "partial_allowed": partial_allowed,
            "projection_version": projection_version,
            "projection_state": TranscriptProjectionState.CURRENT,
            "state": TranscriptPublicationState.ACTIVE,
            "segments": segments,
            "published_at": published_at,
        }
        return cls(
            **values,
            publication_digest=cls._digest_for_values(values),
        )

    @classmethod
    def calculate_digest(cls, publication: "TranscriptPublication") -> str:
        values = publication.model_dump(mode="json", exclude={"publication_digest"})
        return cls._digest_for_values(values)

    @staticmethod
    def _digest_for_values(values: object) -> str:
        return hashlib.sha256(
            json.dumps(
                values,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_default,
            ).encode()
        ).hexdigest()

    def with_state(
        self,
        *,
        state: TranscriptPublicationState,
        projection_state: TranscriptProjectionState,
    ) -> "TranscriptPublication":
        values = self.model_dump(mode="python")
        values.update(state=state, projection_state=projection_state)
        values["publication_digest"] = self._digest_for_values(
            {key: value for key, value in values.items() if key != "publication_digest"}
        )
        return type(self).model_validate(values)

    def successor_projection(
        self,
        *,
        projection_version: str,
        predecessor_publication_id: UUID | None = None,
    ) -> "TranscriptPublication":
        """Create an active successor for a rebuild or rollback projection."""

        publication_id = uuid5(
            NAMESPACE_URL,
            f"spine:transcript-publication-successor:v1:{self.publication_id}:{projection_version}",
        )
        values = self.model_dump(mode="python")
        values.update(
            publication_id=publication_id,
            predecessor_publication_id=predecessor_publication_id or self.publication_id,
            projection_version=projection_version,
            projection_state=TranscriptProjectionState.CURRENT,
            state=TranscriptPublicationState.ACTIVE,
            segments=tuple(
                segment.with_projection_version(projection_version=projection_version)
                for segment in self.segments
            ),
        )
        values["publication_digest"] = self._digest_for_values(
            {key: value for key, value in values.items() if key != "publication_digest"}
        )
        return type(self).model_validate(values)

    def successor_state(
        self,
        *,
        state: TranscriptPublicationState,
        projection_state: TranscriptProjectionState,
    ) -> "TranscriptPublication":
        successor = self.successor_projection(projection_version=self.projection_version)
        values = successor.model_dump(mode="python")
        values.update(state=state, projection_state=projection_state)
        values["publication_digest"] = self._digest_for_values(
            {key: value for key, value in values.items() if key != "publication_digest"}
        )
        return type(self).model_validate(values)


def _json_default(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    raise TypeError(f"unsupported digest value: {type(value).__name__}")


__all__ = [
    "TimeRangeLocator",
    "TranscriptProjectionSegment",
    "TranscriptProjectionState",
    "TranscriptPublication",
    "TranscriptPublicationApproval",
    "TranscriptPublicationState",
]
