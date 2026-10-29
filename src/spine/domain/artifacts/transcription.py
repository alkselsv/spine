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


class AudioMediaInspection(DefinitionModel):
    """Decoder-independent facts observed from the media payload."""

    schema_version: Literal["audio-media-inspection:v1"] = "audio-media-inspection:v1"
    inspected_media_type: str = Field(pattern=r"^audio/[a-z0-9.+-]+$")
    container: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.+-]+$")
    codec: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.+-]+$")
    byte_length: StrictInt = Field(ge=0)
    duration_ms: StrictInt = Field(ge=0)
    audio_stream_count: StrictInt = Field(ge=0, le=32)
    silent: StrictBool


class AudioChunkBoundary(DefinitionModel):
    """One deterministic window on the original recording timeline."""

    schema_version: Literal["audio-chunk-boundary:v1"] = "audio-chunk-boundary:v1"
    ordinal: StrictInt = Field(gt=0)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    overlap_before_ms: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "AudioChunkBoundary":
        if self.end_ms <= self.start_ms:
            raise ValueError("chunk end_ms must be greater than start_ms")
        if self.overlap_before_ms >= self.end_ms - self.start_ms:
            raise ValueError("chunk overlap must be shorter than the chunk")
        if self.ordinal == 1 and self.overlap_before_ms != 0:
            raise ValueError("the first chunk cannot have preceding overlap")
        return self


class TranscriptionProcessingProfile(DefinitionModel):
    """Versioned eligibility, resource and deterministic chunking policy."""

    schema_version: Literal["transcription-processing-profile:v1"] = (
        "transcription-processing-profile:v1"
    )
    profile_reference: VersionIdentifier
    supported_media_types: tuple[str, ...] = Field(min_length=1)
    supported_codecs: tuple[str, ...] = Field(min_length=1)
    maximum_source_bytes: StrictInt = Field(gt=0)
    maximum_duration_ms: StrictInt = Field(gt=0)
    maximum_decoded_bytes: StrictInt = Field(gt=0)
    maximum_memory_bytes: StrictInt = Field(gt=0)
    maximum_working_bytes: StrictInt = Field(gt=0)
    maximum_decode_seconds: StrictFloat = Field(gt=0)
    chunk_duration_ms: StrictInt = Field(gt=0)
    chunk_overlap_ms: StrictInt = Field(ge=0)
    maximum_chunks: StrictInt = Field(gt=0)
    maximum_model_attempts: StrictInt = Field(gt=0, le=10)
    decoder_reference: VersionIdentifier
    reassembly_reference: VersionIdentifier

    @model_validator(mode="after")
    def validate_profile(self) -> "TranscriptionProcessingProfile":
        if len(set(self.supported_media_types)) != len(self.supported_media_types):
            raise ValueError("supported media types must be unique")
        if len(set(self.supported_codecs)) != len(self.supported_codecs):
            raise ValueError("supported codecs must be unique")
        if self.chunk_overlap_ms >= self.chunk_duration_ms:
            raise ValueError("chunk overlap must be shorter than chunk duration")
        if self.maximum_working_bytes < self.maximum_decoded_bytes:
            raise ValueError("working byte limit cannot be below decoded byte limit")
        return self

    def plan_chunks(
        self, inspection: AudioMediaInspection
    ) -> tuple[AudioChunkBoundary, ...]:
        """Govern inspected media and return stable original-timeline windows."""

        if (
            inspection.inspected_media_type not in self.supported_media_types
            or inspection.codec not in self.supported_codecs
        ):
            raise ValueError("unsupported media type or codec")
        if inspection.byte_length == 0:
            raise ValueError("empty media cannot be transcribed")
        if inspection.byte_length > self.maximum_source_bytes:
            raise ValueError("source byte limit exceeded")
        if inspection.duration_ms == 0:
            raise ValueError("empty media duration cannot be transcribed")
        if inspection.duration_ms > self.maximum_duration_ms:
            raise ValueError("duration limit exceeded")
        if inspection.audio_stream_count == 0:
            raise ValueError("audio stream is required")
        if inspection.silent:
            raise ValueError("silent audio cannot be transcribed")

        step_ms = self.chunk_duration_ms - self.chunk_overlap_ms
        boundaries: list[AudioChunkBoundary] = []
        start_ms = 0
        while start_ms < inspection.duration_ms:
            ordinal = len(boundaries) + 1
            if ordinal > self.maximum_chunks:
                raise ValueError("maximum chunk count exceeded")
            boundaries.append(
                AudioChunkBoundary(
                    ordinal=ordinal,
                    start_ms=start_ms,
                    end_ms=min(
                        start_ms + self.chunk_duration_ms, inspection.duration_ms
                    ),
                    overlap_before_ms=0 if ordinal == 1 else self.chunk_overlap_ms,
                )
            )
            if boundaries[-1].end_ms == inspection.duration_ms:
                break
            start_ms += step_ms
        return tuple(boundaries)


class AdapterSignal(DefinitionModel):
    """One optional signal actually emitted by the selected STT adapter."""

    name: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$"
    )
    value: StrictBool | StrictInt | StrictFloat | str


class SpeechToTextSegment(DefinitionModel):
    """Transient provider-neutral segment on the original recording timeline."""

    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text: str = Field(min_length=1)
    anonymous_speaker_label: str | None = Field(
        default=None, min_length=1, max_length=128
    )
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


class SpeechToTextResultV2(DefinitionModel):
    """Chunk-local adapter output introduced by bounded transcription."""

    schema_version: Literal["speech-to-text-result:v2"] = "speech-to-text-result:v2"
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    detected_language: str | None = Field(default=None, min_length=2, max_length=35)
    timeline: Literal["chunk"] = "chunk"
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


class AudioChunkHandle(DefinitionModel):
    """Stable opaque decoder output window supplied to one model invocation."""

    schema_version: Literal["audio-chunk-handle:v1"] = "audio-chunk-handle:v1"
    chunk_id: UUID
    boundary: AudioChunkBoundary
    decoder_reference: VersionIdentifier
    decoded_reference: ObjectReference
    decoded_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> "AudioChunkHandle":
        expected = uuid5(
            NAMESPACE_URL,
            "spine:audio-chunk:v1:"
            f"{self.decoded_sha256}:{self.decoder_reference}:"
            f"{self.boundary.ordinal}:{self.boundary.start_ms}:{self.boundary.end_ms}",
        )
        if self.chunk_id != expected:
            raise ValueError("chunk id does not match decoder output and boundary")
        if self.decoded_reference.digest_hex != self.decoded_sha256:
            raise ValueError("decoded reference digest does not match chunk handle")
        return self

    @classmethod
    def create(
        cls,
        *,
        boundary: AudioChunkBoundary,
        decoder_reference: str,
        decoded_reference: ObjectReference,
        decoded_sha256: str,
    ) -> "AudioChunkHandle":
        return cls(
            chunk_id=uuid5(
                NAMESPACE_URL,
                "spine:audio-chunk:v1:"
                f"{decoded_sha256}:{decoder_reference}:"
                f"{boundary.ordinal}:{boundary.start_ms}:{boundary.end_ms}",
            ),
            boundary=boundary,
            decoder_reference=decoder_reference,
            decoded_reference=decoded_reference,
            decoded_sha256=decoded_sha256,
        )


class SpeechToTextRequest(DefinitionModel):
    schema_version: Literal["speech-to-text-request:v1"] = "speech-to-text-request:v1"
    recording: AudioRecordingHandle
    processing_profile_reference: VersionIdentifier
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    language_hint: str | None = Field(default=None, min_length=2, max_length=35)


class SpeechToTextRequestV2(DefinitionModel):
    schema_version: Literal["speech-to-text-request:v2"] = "speech-to-text-request:v2"
    recording: AudioRecordingHandle
    processing_profile_reference: VersionIdentifier
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    language_hint: str | None = Field(default=None, min_length=2, max_length=35)
    chunk: AudioChunkHandle


class TranscriptionChunkReceipt(DefinitionModel):
    """Immutable proof of one completed deterministic model window."""

    schema_version: Literal["transcription-chunk-receipt:v1"] = (
        "transcription-chunk-receipt:v1"
    )
    chunk_id: UUID
    boundary: AudioChunkBoundary
    model_attempts: StrictInt = Field(gt=0)
    state: Literal["completed"] = "completed"

    @model_validator(mode="after")
    def validate_identity(self) -> "TranscriptionChunkReceipt":
        if self.chunk_id.int == 0:
            raise ValueError("chunk receipt id must be non-zero")
        return self


class TranscriptionChunkCheckpoint(DefinitionModel):
    """Durable cumulative output committed after one completed model window."""

    schema_version: Literal["transcription-chunk-checkpoint:v1"] = (
        "transcription-chunk-checkpoint:v1"
    )
    job_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing_profile_reference: VersionIdentifier
    decoder_reference: VersionIdentifier
    decoded_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reassembly_reference: VersionIdentifier
    detected_language: str | None = Field(default=None, min_length=2, max_length=35)
    chunk_receipts: tuple[TranscriptionChunkReceipt, ...] = Field(min_length=1)
    segments: tuple[SpeechToTextSegment, ...]
    updated_at: datetime

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "TranscriptionChunkCheckpoint":
        if self.job_id.int == 0 or self.source_revision_id.int == 0:
            raise ValueError("checkpoint identifiers must be non-zero")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("checkpoint updated_at must be timezone-aware")
        if tuple(item.boundary.ordinal for item in self.chunk_receipts) != tuple(
            range(1, len(self.chunk_receipts) + 1)
        ):
            raise ValueError("checkpoint chunk receipts must be a contiguous prefix")
        for receipt in self.chunk_receipts:
            boundary = receipt.boundary
            expected_chunk_id = uuid5(
                NAMESPACE_URL,
                "spine:audio-chunk:v1:"
                f"{self.decoded_sha256}:{self.decoder_reference}:"
                f"{boundary.ordinal}:{boundary.start_ms}:{boundary.end_ms}",
            )
            if receipt.chunk_id != expected_chunk_id:
                raise ValueError("checkpoint chunk receipt identity does not match lineage")
        return self


class TranscriptionTimeRangeLocator(DefinitionModel):
    """Stable audio evidence range pinned to one source and transcript version."""

    schema_version: Literal["transcription-time-range-locator:v1"] = (
        "transcription-time-range-locator:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: UUID
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_schema_version: Literal[
        "transcription-artifact:v1", "transcription-artifact:v2"
    ]
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_locator(self) -> "TranscriptionTimeRangeLocator":
        identifiers = (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_revision_id,
            self.artifact_id,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("time-range locator identifiers must be non-zero")
        if self.end_ms <= self.start_ms:
            raise ValueError("time-range locator end_ms must be greater than start_ms")
        return self


class TranscriptionArtifactOutcome(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"


TranscriptionFailureValue = Literal[
    "transient_unavailable",
    "permanent_model_error",
    "policy_denied",
    "unsupported_media",
    "corrupt_media",
    "resource_limit_exceeded",
    "no_speech",
    "retry_exhausted",
    "cancelled",
    "invalid_adapter_output",
]


class TranscriptionFailureProvenance(DefinitionModel):
    """Disclosure-safe exact reason why a bounded artifact stopped early."""

    schema_version: Literal["transcription-failure-provenance:v1"] = (
        "transcription-failure-provenance:v1"
    )
    code: TranscriptionFailureValue
    cause_code: TranscriptionFailureValue | None = None
    failed_chunk_ordinal: StrictInt | None = Field(default=None, gt=0)
    model_attempts: StrictInt = Field(ge=0)
    retryable: StrictBool


class TranscriptSegment(DefinitionModel):
    schema_version: Literal["transcript-segment:v1"] = "transcript-segment:v1"
    segment_id: UUID
    ordinal: StrictInt = Field(gt=0)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text: str = Field(min_length=1)
    anonymous_speaker_label: str | None = Field(
        default=None, min_length=1, max_length=128
    )
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

    schema_version: Literal[
        "transcription-artifact:v1", "transcription-artifact:v2"
    ] = "transcription-artifact:v1"
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
    media_inspection: AudioMediaInspection | None = None
    decoder_reference: VersionIdentifier | None = None
    decoded_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reassembly_reference: VersionIdentifier | None = None
    chunk_receipts: tuple[TranscriptionChunkReceipt, ...] = ()
    outcome: TranscriptionArtifactOutcome = TranscriptionArtifactOutcome.COMPLETED
    failure: TranscriptionFailureProvenance | None = None
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
        if any(
            segment.end_ms > self.recording_duration_ms for segment in self.segments
        ):
            raise ValueError("segment bounds must be within recording duration")
        if tuple(segment.ordinal for segment in self.segments) != tuple(
            range(1, len(self.segments) + 1)
        ):
            raise ValueError("segment ordinals must be contiguous")
        provenance = (
            self.media_inspection,
            self.decoder_reference,
            self.decoded_sha256,
            self.reassembly_reference,
        )
        if any(value is not None for value in provenance) != all(
            value is not None for value in provenance
        ):
            raise ValueError("bounded transcription provenance must be complete")
        if self.schema_version == "transcription-artifact:v1" and any(
            value is not None for value in provenance
        ):
            raise ValueError("v1 transcription artifact cannot contain bounded provenance")
        if self.schema_version == "transcription-artifact:v1" and (
            self.chunk_receipts
            or self.outcome is not TranscriptionArtifactOutcome.COMPLETED
            or self.failure is not None
        ):
            raise ValueError("v1 transcription artifact cannot contain v2 outcome fields")
        if self.schema_version == "transcription-artifact:v2" and not all(
            value is not None for value in provenance
        ):
            raise ValueError("v2 transcription artifact requires bounded provenance")
        if self.media_inspection is not None:
            if self.media_inspection.duration_ms != self.recording_duration_ms:
                raise ValueError("inspection duration must match artifact duration")
            if tuple(
                receipt.boundary.ordinal for receipt in self.chunk_receipts
            ) != tuple(range(1, len(self.chunk_receipts) + 1)):
                raise ValueError("chunk receipt ordinals must be contiguous")
            for receipt in self.chunk_receipts:
                boundary = receipt.boundary
                expected_chunk_id = uuid5(
                    NAMESPACE_URL,
                    "spine:audio-chunk:v1:"
                    f"{self.decoded_sha256}:{self.decoder_reference}:"
                    f"{boundary.ordinal}:{boundary.start_ms}:{boundary.end_ms}",
                )
                if receipt.chunk_id != expected_chunk_id:
                    raise ValueError("chunk receipt identity does not match provenance")
        if self.outcome is TranscriptionArtifactOutcome.COMPLETED:
            if self.failure is not None:
                raise ValueError("completed artifact cannot contain failure provenance")
            if self.schema_version == "transcription-artifact:v2" and not self._has_full_chunk_coverage():
                raise ValueError("completed artifact requires full recording chunk coverage")
        elif self.failure is None or not self.chunk_receipts or not self.segments:
            raise ValueError(
                "partial artifact requires completed work and failure provenance"
            )
        elif (
            self.failure.failed_chunk_ordinal is not None
            and self.failure.failed_chunk_ordinal <= len(self.chunk_receipts)
        ):
            raise ValueError("partial failure must follow completed chunk receipts")
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
            media_inspection=self.media_inspection,
            decoder_reference=self.decoder_reference,
            decoded_sha256=self.decoded_sha256,
            reassembly_reference=self.reassembly_reference,
            chunk_receipts=self.chunk_receipts,
            outcome=self.outcome,
            failure=self.failure,
            schema_version=self.schema_version,
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
        expected_id = uuid5(
            NAMESPACE_URL, f"spine:{self.schema_version}:{expected}"
        )
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

    def _has_full_chunk_coverage(self) -> bool:
        if not self.chunk_receipts:
            return False
        boundaries = tuple(receipt.boundary for receipt in self.chunk_receipts)
        if boundaries[0].start_ms != 0 or boundaries[-1].end_ms != self.recording_duration_ms:
            return False
        return all(
            current.start_ms == previous.end_ms - current.overlap_before_ms
            for previous, current in zip(boundaries, boundaries[1:])
        )

    def locate_time_range(self, *, start_ms: int, end_ms: int) -> TranscriptionTimeRangeLocator:
        if start_ms < 0 or end_ms <= start_ms or end_ms > self.recording_duration_ms:
            raise ValueError("time range must be within the original recording")
        return TranscriptionTimeRangeLocator(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            source_object_id=self.source_object_id,
            source_revision_id=self.source_revision_id,
            source_revision_digest=self.source_revision_digest,
            original_sha256=self.original_sha256,
            artifact_id=self.artifact_id,
            artifact_digest=self.artifact_digest,
            artifact_schema_version=self.schema_version,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    def validate_time_range_locator(self, locator: TranscriptionTimeRangeLocator) -> None:
        expected = (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_revision_id,
            self.source_revision_digest,
            self.original_sha256,
            self.artifact_id,
            self.artifact_digest,
            self.schema_version,
        )
        actual = (
            locator.workspace_id,
            locator.environment_id,
            locator.source_object_id,
            locator.source_revision_id,
            locator.source_revision_digest,
            locator.original_sha256,
            locator.artifact_id,
            locator.artifact_digest,
            locator.artifact_schema_version,
        )
        if actual != expected:
            raise ValueError("time-range locator lineage does not match artifact")
        if locator.end_ms > self.recording_duration_ms:
            raise ValueError("time-range locator exceeds recording duration")

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
        media_inspection: AudioMediaInspection | None = None,
        decoder_reference: str | None = None,
        decoded_sha256: str | None = None,
        reassembly_reference: str | None = None,
        chunk_receipts: tuple[TranscriptionChunkReceipt, ...] = (),
        outcome: TranscriptionArtifactOutcome = TranscriptionArtifactOutcome.COMPLETED,
        failure: TranscriptionFailureProvenance | None = None,
        created_at: datetime | None = None,
    ) -> "TranscriptionArtifact":
        schema_version = (
            "transcription-artifact:v2"
            if media_inspection is not None
            else "transcription-artifact:v1"
        )
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
            media_inspection=media_inspection,
            decoder_reference=decoder_reference,
            decoded_sha256=decoded_sha256,
            reassembly_reference=reassembly_reference,
            chunk_receipts=chunk_receipts,
            outcome=outcome,
            failure=failure,
            schema_version=schema_version,
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
            artifact_id=uuid5(
                NAMESPACE_URL, f"spine:{schema_version}:{digest}"
            ),
            schema_version=schema_version,
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
            media_inspection=media_inspection,
            decoder_reference=decoder_reference,
            decoded_sha256=decoded_sha256,
            reassembly_reference=reassembly_reference,
            chunk_receipts=chunk_receipts,
            outcome=outcome,
            failure=failure,
            segments=transcript_segments,
            created_at=created_at or datetime.now(timezone.utc),
        )


class TranscriptionJobState(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TranscriptionFailureCode(str, Enum):
    TRANSIENT_UNAVAILABLE = "transient_unavailable"
    PERMANENT_MODEL_ERROR = "permanent_model_error"
    POLICY_DENIED = "policy_denied"
    UNSUPPORTED_MEDIA = "unsupported_media"
    CORRUPT_MEDIA = "corrupt_media"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    NO_SPEECH = "no_speech"
    RETRY_EXHAUSTED = "retry_exhausted"
    CANCELLED = "cancelled"
    INVALID_ADAPTER_OUTPUT = "invalid_adapter_output"


V1_TRANSCRIPTION_FAILURE_CODES = frozenset(
    {
        TranscriptionFailureCode.TRANSIENT_UNAVAILABLE.value,
        TranscriptionFailureCode.PERMANENT_MODEL_ERROR.value,
        TranscriptionFailureCode.POLICY_DENIED.value,
        TranscriptionFailureCode.UNSUPPORTED_MEDIA.value,
        TranscriptionFailureCode.INVALID_ADAPTER_OUTPUT.value,
    }
)
V2_TRANSCRIPTION_FAILURE_CODES = frozenset(
    code.value for code in TranscriptionFailureCode
)


class TranscriptionJob(DefinitionModel):
    schema_version: Literal["transcription-job:v1", "transcription-job:v2"] = (
        "transcription-job:v1"
    )
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
    cancellation_requested_at: datetime | None = None
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
                raise ValueError(
                    "pending transcription cannot contain a terminal outcome"
                )
        elif self.completed_at is None:
            raise ValueError("terminal transcription requires completed_at")
        if self.schema_version == "transcription-job:v1" and (
            self.state is TranscriptionJobState.PARTIAL
            or self.cancellation_requested_at is not None
        ):
            raise ValueError("v1 transcription job cannot contain bounded state")
        if self.state is TranscriptionJobState.COMPLETED:
            if (
                self.artifact_id is None
                or self.terminal_code != "completed"
                or self.retryable is not None
            ):
                raise ValueError("completed transcription requires its artifact")
        elif self.state is TranscriptionJobState.PARTIAL:
            if (
                self.artifact_id is None
                or self.terminal_code
                not in {code.value for code in TranscriptionFailureCode}
                or self.retryable is None
            ):
                raise ValueError(
                    "partial transcription requires its artifact and failure"
                )
        elif self.state is TranscriptionJobState.FAILED and (
            self.artifact_id is not None or self.terminal_code is None
        ):
            raise ValueError("failed transcription cannot contain an artifact")
        elif self.state is TranscriptionJobState.CANCELLED and (
            self.terminal_code != "cancelled" or self.retryable is not False
        ):
            raise ValueError("cancelled transcription requires its typed outcome")
        elif self.state is TranscriptionJobState.FAILED and (
            self.terminal_code
            not in (
                V1_TRANSCRIPTION_FAILURE_CODES
                if self.schema_version == "transcription-job:v1"
                else V2_TRANSCRIPTION_FAILURE_CODES
            )
            or self.retryable is None
        ):
            raise ValueError("failed transcription requires its typed outcome")
        for value in (
            self.created_at,
            self.completed_at,
            self.execution_lease_expires_at,
            self.cancellation_requested_at,
        ):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("job timestamps must be timezone-aware")
        if (
            self.cancellation_requested_at is not None
            and self.cancellation_requested_at < self.created_at
        ):
            raise ValueError("cancellation cannot precede job creation")
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
        if (
            state is TranscriptionJobState.COMPLETED
            and artifact is not None
            and artifact.outcome is not TranscriptionArtifactOutcome.COMPLETED
        ):
            raise ValueError("completed job requires a completed artifact")
        if state in {
            TranscriptionJobState.PARTIAL,
            TranscriptionJobState.CANCELLED,
        } and (
            artifact is not None
            and artifact.outcome is not TranscriptionArtifactOutcome.PARTIAL
        ):
            raise ValueError("non-completed job cannot contain a completed artifact")
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

    def request_cancellation(self, *, requested_at: datetime) -> "TranscriptionJob":
        """Record stable cancellation intent without claiming work has stopped."""

        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError("cancellation timestamp must be timezone-aware")
        if requested_at < self.created_at:
            raise ValueError("cancellation cannot precede job creation")
        if self.state is not TranscriptionJobState.PENDING:
            return self
        if self.cancellation_requested_at is not None:
            return self
        return type(self).model_validate(
            {**self.model_dump(), "cancellation_requested_at": requested_at}
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
    media_inspection: AudioMediaInspection | None,
    decoder_reference: str | None,
    decoded_sha256: str | None,
    reassembly_reference: str | None,
    chunk_receipts: tuple[TranscriptionChunkReceipt, ...],
    outcome: TranscriptionArtifactOutcome,
    failure: TranscriptionFailureProvenance | None,
    schema_version: str,
    source_segments: tuple[SpeechToTextSegment, ...],
) -> str:
    payload = {
        "schema": schema_version,
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
    if media_inspection is not None:
        payload.update(
            {
                "media_inspection": media_inspection.model_dump(mode="json"),
                "decoder_reference": decoder_reference,
                "decoded_sha256": decoded_sha256,
                "reassembly_reference": reassembly_reference,
                "chunk_receipts": [
                    receipt.model_dump(mode="json") for receipt in chunk_receipts
                ],
            }
        )
    if outcome is not TranscriptionArtifactOutcome.COMPLETED:
        payload.update(
            {
                "outcome": outcome.value,
                "failure": (
                    failure.model_dump(mode="json") if failure is not None else None
                ),
            }
        )
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "AdapterSignal",
    "AudioChunkHandle",
    "AudioChunkBoundary",
    "AudioMediaInspection",
    "AudioRecordingHandle",
    "SpeechToTextRequest",
    "SpeechToTextResult",
    "SpeechToTextSegment",
    "TranscriptSegment",
    "TranscriptionArtifact",
    "TranscriptionArtifactOutcome",
    "TranscriptionChunkCheckpoint",
    "TranscriptionChunkReceipt",
    "TranscriptionFailureProvenance",
    "TranscriptionFailureCode",
    "TranscriptionJob",
    "TranscriptionJobState",
    "TranscriptionProcessingProfile",
    "TranscriptionTimeRangeLocator",
]
