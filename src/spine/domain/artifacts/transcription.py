"""Immutable provider-neutral transcription contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StrictBool, StrictFloat, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources.original_reference import ObjectReference


class AdapterSignal(DefinitionModel):
    """One optional signal actually emitted by the selected STT adapter."""

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    value: StrictBool | StrictInt | StrictFloat | str


class SpeechToTextSegment(DefinitionModel):
    """Transient provider-neutral segment on the original recording timeline."""

    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text: str = Field(min_length=1)
    anonymous_speaker_label: str | None = Field(default=None, min_length=1, max_length=128)
    signals: tuple[AdapterSignal, ...] = ()

    @model_validator(mode="after")
    def validate_bounds_and_signals(self) -> "SpeechToTextSegment":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        names = tuple(signal.name for signal in self.signals)
        if len(set(names)) != len(names):
            raise ValueError("adapter signal names must be unique per segment")
        return self


class SpeechToTextResult(DefinitionModel):
    """Successful adapter output before canonical artifact persistence."""

    schema_version: Literal["speech-to-text-result:v1"] = "speech-to-text-result:v1"
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    detected_language: str | None = Field(default=None, min_length=2, max_length=35)
    segments: tuple[SpeechToTextSegment, ...]


class AudioRecordingHandle(DefinitionModel):
    """Opaque immutable audio input; it contains no bytes or signed URL."""

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_reference: ObjectReference
    media_type: str = Field(pattern=r"^audio/[a-z0-9.+-]+$")
    recording_duration_ms: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_identity(self) -> "AudioRecordingHandle":
        identifiers = (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_revision_id,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("recording identifiers must be non-zero")
        if self.original_reference.digest_hex != self.original_sha256:
            raise ValueError("recording original digest does not match its handle")
        return self


class SpeechToTextRequest(DefinitionModel):
    schema_version: Literal["speech-to-text-request:v1"] = "speech-to-text-request:v1"
    recording: AudioRecordingHandle
    processing_profile_reference: VersionIdentifier
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    language_hint: str | None = Field(default=None, min_length=2, max_length=35)


class TranscriptSegment(DefinitionModel):
    schema_version: Literal["transcript-segment:v1"] = "transcript-segment:v1"
    segment_id: UUID
    ordinal: StrictInt = Field(gt=0)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text: str = Field(min_length=1)
    anonymous_speaker_label: str | None = Field(default=None, min_length=1, max_length=128)
    signals: tuple[AdapterSignal, ...] = ()

    @model_validator(mode="after")
    def validate_bounds(self) -> "TranscriptSegment":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        if self.segment_id.int == 0:
            raise ValueError("segment_id must be non-zero")
        return self


class TranscriptionArtifact(DefinitionModel):
    """Immutable ASR output pinned to one exact audio revision and configuration."""

    schema_version: Literal["transcription-artifact:v1"] = "transcription-artifact:v1"
    artifact_id: UUID
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing_job_id: UUID
    processing_profile_reference: VersionIdentifier
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    recording_duration_ms: StrictInt = Field(gt=0)
    language: str | None = Field(default=None, min_length=2, max_length=35)
    segments: tuple[TranscriptSegment, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_artifact(self) -> "TranscriptionArtifact":
        identifiers = (
            self.artifact_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_revision_id,
            self.processing_job_id,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("transcription identifiers must be non-zero")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if any(segment.end_ms > self.recording_duration_ms for segment in self.segments):
            raise ValueError("segment bounds must be within recording duration")
        if tuple(segment.ordinal for segment in self.segments) != tuple(
            range(1, len(self.segments) + 1)
        ):
            raise ValueError("segment ordinals must be contiguous")
        expected = _artifact_digest(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            source_object_id=self.source_object_id,
            source_revision_id=self.source_revision_id,
            source_revision_digest=self.source_revision_digest,
            original_sha256=self.original_sha256,
            processing_job_id=self.processing_job_id,
            processing_profile_reference=self.processing_profile_reference,
            adapter_reference=self.adapter_reference,
            model_config_reference=self.model_config_reference,
            recording_duration_ms=self.recording_duration_ms,
            language=self.language,
            source_segments=tuple(
                SpeechToTextSegment(
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text,
                    anonymous_speaker_label=segment.anonymous_speaker_label,
                    signals=segment.signals,
                )
                for segment in self.segments
            ),
        )
        if expected != self.artifact_digest:
            raise ValueError("artifact digest does not match immutable content")
        expected_id = uuid5(NAMESPACE_URL, f"spine:transcription-artifact:v1:{expected}")
        if self.artifact_id != expected_id:
            raise ValueError("artifact id does not match immutable content")
        for segment in self.segments:
            expected_segment_id = uuid5(
                NAMESPACE_URL,
                f"spine:transcript-segment:v1:{expected}:{segment.ordinal}",
            )
            if segment.segment_id != expected_segment_id:
                raise ValueError("segment id does not match immutable content")
        return self

    @classmethod
    def create(
        cls,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        source_revision_digest: str,
        original_sha256: str,
        processing_job_id: UUID,
        processing_profile_reference: str,
        adapter_reference: str,
        model_config_reference: str,
        recording_duration_ms: int,
        language: str | None,
        segments: tuple[SpeechToTextSegment, ...],
        created_at: datetime | None = None,
    ) -> "TranscriptionArtifact":
        digest = _artifact_digest(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            source_revision_digest=source_revision_digest,
            original_sha256=original_sha256,
            processing_job_id=processing_job_id,
            processing_profile_reference=processing_profile_reference,
            adapter_reference=adapter_reference,
            model_config_reference=model_config_reference,
            recording_duration_ms=recording_duration_ms,
            language=language,
            source_segments=segments,
        )
        transcript_segments = tuple(
            TranscriptSegment(
                segment_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:transcript-segment:v1:{digest}:{ordinal}",
                ),
                ordinal=ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                anonymous_speaker_label=segment.anonymous_speaker_label,
                signals=segment.signals,
            )
            for ordinal, segment in enumerate(segments, start=1)
        )
        return cls(
            artifact_id=uuid5(NAMESPACE_URL, f"spine:transcription-artifact:v1:{digest}"),
            artifact_digest=digest,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            source_revision_digest=source_revision_digest,
            original_sha256=original_sha256,
            processing_job_id=processing_job_id,
            processing_profile_reference=processing_profile_reference,
            adapter_reference=adapter_reference,
            model_config_reference=model_config_reference,
            recording_duration_ms=recording_duration_ms,
            language=language,
            segments=transcript_segments,
            created_at=created_at or datetime.now(timezone.utc),
        )


class TranscriptionJobState(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TranscriptionFailureCode(str, Enum):
    TRANSIENT_UNAVAILABLE = "transient_unavailable"
    PERMANENT_MODEL_ERROR = "permanent_model_error"
    POLICY_DENIED = "policy_denied"
    UNSUPPORTED_MEDIA = "unsupported_media"
    INVALID_ADAPTER_OUTPUT = "invalid_adapter_output"


class TranscriptionJob(DefinitionModel):
    schema_version: Literal["transcription-job:v1"] = "transcription-job:v1"
    job_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing_profile_reference: VersionIdentifier
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    authorization_decision_id: UUID
    authorization_policy_reference: VersionIdentifier
    idempotency_key: str = Field(min_length=1, max_length=255)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_lease_id: UUID
    execution_lease_expires_at: datetime
    execution_attempt: StrictInt = Field(gt=0)
    state: TranscriptionJobState
    artifact_id: UUID | None = None
    terminal_code: str | None = Field(default=None, min_length=1, max_length=128)
    retryable: bool | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "TranscriptionJob":
        if any(
            value.int == 0
            for value in (
                self.job_id,
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
            self.source_revision_id,
            self.authorization_decision_id,
            self.execution_lease_id,
            )
        ):
            raise ValueError("transcription job identifiers must be non-zero")
        if self.state is TranscriptionJobState.PENDING:
            if any(
                value is not None
                for value in (
                    self.artifact_id,
                    self.terminal_code,
                    self.retryable,
                    self.completed_at,
                )
            ):
                raise ValueError("pending transcription cannot contain a terminal outcome")
        elif self.completed_at is None:
            raise ValueError("terminal transcription requires completed_at")
        elif self.state is TranscriptionJobState.COMPLETED:
            if (
                self.artifact_id is None
                or self.terminal_code != "completed"
                or self.retryable is not None
            ):
                raise ValueError("completed transcription requires its artifact")
        elif self.artifact_id is not None or self.terminal_code is None:
            raise ValueError("failed or cancelled transcription cannot contain an artifact")
        elif self.state is TranscriptionJobState.CANCELLED and (
            self.terminal_code != "cancelled" or self.retryable is not False
        ):
            raise ValueError("cancelled transcription requires its typed outcome")
        elif self.state is TranscriptionJobState.FAILED and (
            self.terminal_code not in {code.value for code in TranscriptionFailureCode}
            or self.retryable is None
        ):
            raise ValueError("failed transcription requires its typed outcome")
        for value in (self.created_at, self.completed_at, self.execution_lease_expires_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("job timestamps must be timezone-aware")
        return self

    def terminal(
        self,
        *,
        state: TranscriptionJobState,
        completed_at: datetime,
        artifact: TranscriptionArtifact | None,
        terminal_code: str,
        retryable: bool | None,
    ) -> "TranscriptionJob":
        """Apply the only legal pending-to-terminal transition."""

        artifact_id = artifact.artifact_id if artifact is not None else None
        if self.state is not TranscriptionJobState.PENDING:
            if (
                self.state is not state
                or self.artifact_id != artifact_id
                or self.terminal_code != terminal_code
                or self.retryable != retryable
            ):
                raise ValueError("terminal transcription conflicts with history")
            return self
        if state is TranscriptionJobState.PENDING:
            raise ValueError("terminal transition requires a terminal state")
        if completed_at >= self.execution_lease_expires_at:
            raise ValueError("transcription execution lease has expired")
        if artifact is not None and (
            artifact.workspace_id != self.workspace_id
            or artifact.environment_id != self.environment_id
            or artifact.processing_job_id != self.job_id
            or artifact.source_object_id != self.source_object_id
            or artifact.source_revision_id != self.source_revision_id
            or artifact.source_revision_digest != self.source_revision_digest
            or artifact.original_sha256 != self.original_sha256
        ):
            raise ValueError("artifact scope or lineage conflicts with job")
        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": state,
                "artifact_id": artifact_id,
                "terminal_code": terminal_code,
                "retryable": retryable,
                "completed_at": completed_at,
            }
        )

    def reclaim(
        self, *, lease_id: UUID, lease_expires_at: datetime, now: datetime
    ) -> "TranscriptionJob":
        """Claim an expired pending execution without changing logical identity."""

        if self.state is not TranscriptionJobState.PENDING:
            raise ValueError("only a pending transcription can be reclaimed")
        if self.execution_lease_expires_at > now:
            raise ValueError("transcription execution lease is still active")
        return type(self).model_validate(
            {
                **self.model_dump(),
                "execution_lease_id": lease_id,
                "execution_lease_expires_at": lease_expires_at,
                "execution_attempt": self.execution_attempt + 1,
            }
        )


def _artifact_digest(
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    source_revision_id: UUID,
    source_revision_digest: str,
    original_sha256: str,
    processing_job_id: UUID,
    processing_profile_reference: str,
    adapter_reference: str,
    model_config_reference: str,
    recording_duration_ms: int,
    language: str | None,
    source_segments: tuple[SpeechToTextSegment, ...],
) -> str:
    payload = {
        "schema": "transcription-artifact:v1",
        "workspace_id": str(workspace_id),
        "environment_id": str(environment_id),
        "source_object_id": str(source_object_id),
        "source_revision_id": str(source_revision_id),
        "source_revision_digest": source_revision_digest,
        "original_sha256": original_sha256,
        "processing_job_id": str(processing_job_id),
        "processing_profile_reference": processing_profile_reference,
        "adapter_reference": adapter_reference,
        "model_config_reference": model_config_reference,
        "recording_duration_ms": recording_duration_ms,
        "language": language,
        "segments": [segment.model_dump(mode="json") for segment in source_segments],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "AdapterSignal",
    "AudioRecordingHandle",
    "SpeechToTextRequest",
    "SpeechToTextResult",
    "SpeechToTextSegment",
    "TranscriptSegment",
    "TranscriptionArtifact",
    "TranscriptionFailureCode",
    "TranscriptionJob",
    "TranscriptionJobState",
]
