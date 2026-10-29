"""Governed, idempotent processing of one immutable audio SourceRevision."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from spine.domain.artifacts.transcription import (
    AudioChunkHandle,
    AudioMediaInspection,
    AudioRecordingHandle,
    SpeechToTextRequest,
    SpeechToTextRequestV2,
    SpeechToTextResult,
    SpeechToTextResultV2,
    SpeechToTextSegment,
    TranscriptionArtifact,
    TranscriptionArtifactOutcome,
    TranscriptionChunkCheckpoint,
    TranscriptionChunkReceipt,
    TranscriptionFailureProvenance,
    TranscriptionFailureCode,
    TranscriptionJob,
    TranscriptionJobState,
    TranscriptionProcessingProfile,
)
from spine.domain.sources import RevisionKind, SourceRevision
from spine.domain.sources.original_reference import ObjectReference


class TranscriptionConflictError(Exception):
    """An idempotency identity conflicts with immutable job history."""


class AudioProcessingDeniedError(Exception):
    """Current policy denied processing before any audio/model disclosure."""


class SpeechToTextFailed(Exception):
    """A disclosure-safe typed adapter failure."""

    def __init__(
        self, *, code: TranscriptionFailureCode | str, retryable: bool
    ) -> None:
        typed_code = TranscriptionFailureCode(code)
        super().__init__(typed_code.value)
        self.code = typed_code
        self.retryable = retryable


class SpeechToTextCancelled(Exception):
    """The adapter stopped work after cancellation was requested."""


class SpeechToTextPort(Protocol):
    async def transcribe(
        self, request: SpeechToTextRequest | SpeechToTextRequestV2
    ) -> SpeechToTextResult | SpeechToTextResultV2: ...


class AudioPreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recording: AudioRecordingHandle
    processing_profile: TranscriptionProcessingProfile


class AudioPreparationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inspection: AudioMediaInspection
    decoder_reference: str = Field(min_length=1, max_length=128)
    decoded_reference: ObjectReference
    decoded_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decoded_byte_length: int = Field(gt=0)
    peak_memory_bytes: int = Field(ge=0)
    peak_working_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_decoded_reference(self) -> "AudioPreparationResult":
        if self.decoded_reference.digest_hex != self.decoded_sha256:
            raise ValueError(
                "decoded reference digest does not match preparation result"
            )
        if self.decoded_reference.byte_length != self.decoded_byte_length:
            raise ValueError(
                "decoded reference length does not match preparation result"
            )
        return self


class AudioPreparationPort(Protocol):
    async def prepare(
        self, request: AudioPreparationRequest
    ) -> AudioPreparationResult: ...

    async def release(self, prepared: AudioPreparationResult) -> None: ...

    async def abort(self, request: AudioPreparationRequest) -> None: ...


class AudioProcessingAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing_profile_reference: str = Field(min_length=1, max_length=128)


class AudioProcessingAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    decision_id: UUID
    policy_reference: str = Field(min_length=1, max_length=128)
    reason_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_decision(self) -> "AudioProcessingAuthorization":
        if self.decision_id.int == 0:
            raise ValueError("authorization decision id must be non-zero")
        if self.allowed != (self.reason_code == "allowed"):
            raise ValueError("authorization outcome and reason do not match")
        return self


class AudioProcessingAuthorizer(Protocol):
    async def authorize(
        self, request: AudioProcessingAuthorizationRequest
    ) -> AudioProcessingAuthorization: ...


class TranscribeAudioCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "transcribe-audio-command:v1", "transcribe-audio-command:v2"
    ] = "transcribe-audio-command:v1"
    workspace_id: UUID
    environment_id: UUID
    source_revision: SourceRevision
    recording_duration_ms: int = Field(gt=0)
    processing_profile_reference: str = Field(min_length=1, max_length=128)
    processing_profile: TranscriptionProcessingProfile | None = None
    adapter_reference: str = Field(min_length=1, max_length=128)
    model_config_reference: str = Field(min_length=1, max_length=128)
    language_hint: str | None = Field(default=None, min_length=2, max_length=35)
    idempotency_key: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_source_scope(self) -> "TranscribeAudioCommand":
        source = self.source_revision
        if (
            source.workspace_id != self.workspace_id
            or source.environment_id != self.environment_id
        ):
            raise ValueError("audio revision must belong to the exact command scope")
        if source.kind is not RevisionKind.CONTENT or source.original_reference is None:
            raise ValueError("transcription requires an immutable content revision")
        if source.media_type is None or not source.media_type.startswith("audio/"):
            raise ValueError("transcription requires an audio media type")
        if (
            self.processing_profile is not None
            and self.processing_profile.profile_reference
            != self.processing_profile_reference
        ):
            raise ValueError(
                "processing profile reference does not match its definition"
            )
        if (
            self.schema_version == "transcribe-audio-command:v1"
            and self.processing_profile is not None
        ):
            raise ValueError("v1 transcription command cannot define bounded processing")
        if (
            self.schema_version == "transcribe-audio-command:v2"
            and self.processing_profile is None
        ):
            raise ValueError("v2 transcription command requires a processing profile")
        return self

    def digest(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"schema_version", "idempotency_key", "source_revision"},
        )
        payload.update(
            {
                "schema": self.schema_version,
                "source_object_id": str(self.source_revision.source_object_id),
                "source_revision_id": str(self.source_revision.revision_id),
                "source_revision_digest": self.source_revision.revision_digest,
                "original_sha256": self.source_revision.original_sha256,
            }
        )
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TranscriptionAdmission:
    job: TranscriptionJob
    replay: bool
    execution_owner: bool
    lease_id: UUID


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    job: TranscriptionJob
    artifact: TranscriptionArtifact | None
    replay: bool


ChunkedTranscriptionOutcome = tuple[
    SpeechToTextResult,
    tuple[TranscriptionChunkReceipt, ...],
    TranscriptionFailureProvenance | None,
    bool,
]


class TranscriptionRepository(Protocol):
    async def admit(
        self, job: TranscriptionJob, *, now: datetime
    ) -> TranscriptionAdmission: ...

    async def finish(
        self,
        *,
        job_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        state: TranscriptionJobState,
        completed_at: datetime,
        artifact: TranscriptionArtifact | None,
        terminal_code: str,
        retryable: bool | None,
        lease_id: UUID,
    ) -> TranscriptionJob: ...

    async def get_job(
        self, *, workspace_id: UUID, environment_id: UUID, job_id: UUID
    ) -> TranscriptionJob | None: ...

    async def get_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptionArtifact | None: ...

    async def request_cancellation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        job_id: UUID,
        requested_at: datetime,
    ) -> TranscriptionJob: ...

    async def get_checkpoint(
        self, *, workspace_id: UUID, environment_id: UUID, job_id: UUID
    ) -> TranscriptionChunkCheckpoint | None: ...

    async def checkpoint_chunk(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        job_id: UUID,
        lease_id: UUID,
        expected_completed_chunks: int,
        checkpoint: TranscriptionChunkCheckpoint,
    ) -> TranscriptionChunkCheckpoint: ...


class InMemoryTranscriptionRepository:
    """Atomic tenant-scoped canonical fake for application and contract tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[tuple[UUID, UUID, UUID], TranscriptionJob] = {}
        self._commands: dict[tuple[UUID, UUID, str], UUID] = {}
        self._artifacts: dict[tuple[UUID, UUID, UUID], TranscriptionArtifact] = {}
        self._checkpoints: dict[
            tuple[UUID, UUID, UUID], TranscriptionChunkCheckpoint
        ] = {}

    async def admit(
        self, job: TranscriptionJob, *, now: datetime
    ) -> TranscriptionAdmission:
        scope = (job.workspace_id, job.environment_id)
        command_key = (*scope, job.idempotency_key)
        job_key = (*scope, job.job_id)
        async with self._lock:
            existing_id = self._commands.get(command_key)
            if existing_id is not None:
                existing = self._jobs[(*scope, existing_id)]
                if existing.command_digest != job.command_digest:
                    raise TranscriptionConflictError(
                        "transcription command conflicts with history"
                    )
                owner = False
                if (
                    existing.state is TranscriptionJobState.PENDING
                    and existing.execution_lease_expires_at <= now
                ):
                    existing = existing.reclaim(
                        lease_id=job.execution_lease_id,
                        lease_expires_at=job.execution_lease_expires_at,
                        now=now,
                    )
                    self._jobs[(*scope, existing_id)] = existing
                    owner = True
                return TranscriptionAdmission(
                    existing.model_copy(deep=True),
                    True,
                    owner,
                    existing.execution_lease_id,
                )
            existing = self._jobs.get(job_key)
            if existing is not None:
                if existing.command_digest != job.command_digest:
                    raise TranscriptionConflictError(
                        "transcription job identity conflicts with history"
                    )
                self._commands[command_key] = existing.job_id
                return TranscriptionAdmission(
                    existing.model_copy(deep=True),
                    True,
                    False,
                    existing.execution_lease_id,
                )
            stored = job.model_copy(deep=True)
            self._jobs[job_key] = stored
            self._commands[command_key] = job.job_id
            return TranscriptionAdmission(
                stored.model_copy(deep=True),
                False,
                True,
                stored.execution_lease_id,
            )

    async def finish(
        self,
        *,
        job_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        state: TranscriptionJobState,
        completed_at: datetime,
        artifact: TranscriptionArtifact | None,
        terminal_code: str,
        retryable: bool | None,
        lease_id: UUID,
    ) -> TranscriptionJob:
        key = (workspace_id, environment_id, job_id)
        async with self._lock:
            current = self._jobs.get(key)
            if current is None:
                raise TranscriptionConflictError("transcription job is unavailable")
            if (
                current.state is TranscriptionJobState.PENDING
                and current.execution_lease_id != lease_id
            ):
                raise TranscriptionConflictError(
                    "transcription execution lease is stale"
                )
            if (
                current.state is TranscriptionJobState.PENDING
                and current.cancellation_requested_at is not None
                and state is not TranscriptionJobState.CANCELLED
            ):
                state = TranscriptionJobState.CANCELLED
                artifact = None
                terminal_code = "cancelled"
                retryable = False
            try:
                terminal = current.terminal(
                    state=state,
                    completed_at=completed_at,
                    artifact=artifact,
                    terminal_code=terminal_code,
                    retryable=retryable,
                )
            except ValueError as error:
                raise TranscriptionConflictError(str(error)) from None
            if current.state is not TranscriptionJobState.PENDING:
                return terminal.model_copy(deep=True)
            if artifact is not None:
                artifact_key = (workspace_id, environment_id, artifact.artifact_id)
                existing_artifact = self._artifacts.get(artifact_key)
                if existing_artifact is not None and existing_artifact != artifact:
                    raise TranscriptionConflictError(
                        "transcription artifact conflicts with history"
                    )
                self._artifacts[artifact_key] = artifact.model_copy(deep=True)
            self._jobs[key] = terminal
            return terminal.model_copy(deep=True)

    async def get_job(
        self, *, workspace_id: UUID, environment_id: UUID, job_id: UUID
    ) -> TranscriptionJob | None:
        async with self._lock:
            value = self._jobs.get((workspace_id, environment_id, job_id))
            return value.model_copy(deep=True) if value is not None else None

    async def get_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptionArtifact | None:
        async with self._lock:
            value = self._artifacts.get((workspace_id, environment_id, artifact_id))
            return value.model_copy(deep=True) if value is not None else None

    async def request_cancellation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        job_id: UUID,
        requested_at: datetime,
    ) -> TranscriptionJob:
        key = (workspace_id, environment_id, job_id)
        async with self._lock:
            current = self._jobs.get(key)
            if current is None:
                raise TranscriptionConflictError("transcription job is unavailable")
            updated = current.request_cancellation(requested_at=requested_at)
            self._jobs[key] = updated
            return updated.model_copy(deep=True)

    async def get_checkpoint(
        self, *, workspace_id: UUID, environment_id: UUID, job_id: UUID
    ) -> TranscriptionChunkCheckpoint | None:
        async with self._lock:
            value = self._checkpoints.get((workspace_id, environment_id, job_id))
            return value.model_copy(deep=True) if value is not None else None

    async def checkpoint_chunk(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        job_id: UUID,
        lease_id: UUID,
        expected_completed_chunks: int,
        checkpoint: TranscriptionChunkCheckpoint,
    ) -> TranscriptionChunkCheckpoint:
        key = (workspace_id, environment_id, job_id)
        async with self._lock:
            job = self._jobs.get(key)
            if (
                job is None
                or job.state is not TranscriptionJobState.PENDING
                or job.execution_lease_id != lease_id
                or checkpoint.updated_at >= job.execution_lease_expires_at
            ):
                raise TranscriptionConflictError("transcription checkpoint lease is stale")
            current = self._checkpoints.get(key)
            completed = len(current.chunk_receipts) if current is not None else 0
            if completed != expected_completed_chunks:
                if current == checkpoint:
                    return current.model_copy(deep=True)
                raise TranscriptionConflictError("transcription checkpoint is stale")
            self._checkpoints[key] = checkpoint.model_copy(deep=True)
            return checkpoint.model_copy(deep=True)

    async def count_jobs(self) -> int:
        async with self._lock:
            return len(self._jobs)

    async def count_artifacts(self) -> int:
        async with self._lock:
            return len(self._artifacts)


class TranscriptionService:
    def __init__(
        self,
        *,
        repository: TranscriptionRepository,
        speech_to_text: SpeechToTextPort,
        authorizer: AudioProcessingAuthorizer,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        lease_id_factory: Callable[[], UUID] = uuid4,
        execution_lease: timedelta = timedelta(minutes=5),
        media_preparation: AudioPreparationPort | None = None,
    ) -> None:
        self._repository = repository
        self._speech_to_text = speech_to_text
        self._authorizer = authorizer
        self._clock = clock
        self._lease_id_factory = lease_id_factory
        self._execution_lease = execution_lease
        self._media_preparation = media_preparation

    async def cancel(
        self, *, workspace_id: UUID, environment_id: UUID, job_id: UUID
    ) -> TranscriptionResult:
        job = await self._repository.request_cancellation(
            workspace_id=workspace_id,
            environment_id=environment_id,
            job_id=job_id,
            requested_at=self._clock(),
        )
        return TranscriptionResult(
            job=job,
            artifact=await self._artifact_for(job),
            replay=job.state is not TranscriptionJobState.PENDING,
        )

    async def transcribe(self, command: TranscribeAudioCommand) -> TranscriptionResult:
        source = command.source_revision
        authorization = await self._authorizer.authorize(
            AudioProcessingAuthorizationRequest(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                source_object_id=source.source_object_id,
                source_revision_id=source.revision_id,
                source_revision_digest=source.revision_digest,
                original_sha256=source.original_sha256,
                processing_profile_reference=command.processing_profile_reference,
            )
        )
        if not authorization.allowed:
            raise AudioProcessingDeniedError("audio processing is not authorized")
        digest = command.digest()
        job_contract_version = command.schema_version.rsplit(":", 1)[-1]
        job_id = uuid5(
            NAMESPACE_URL,
            f"spine:transcription-job:{job_contract_version}:"
            f"{command.workspace_id}:{command.environment_id}:{source.revision_id}:"
            f"{command.processing_profile_reference}:{command.idempotency_key}",
        )
        admitted_at = self._clock()
        pending = TranscriptionJob(
            schema_version=f"transcription-job:{job_contract_version}",
            job_id=job_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            source_object_id=source.source_object_id,
            source_revision_id=source.revision_id,
            source_revision_digest=source.revision_digest,
            original_sha256=source.original_sha256,
            processing_profile_reference=command.processing_profile_reference,
            adapter_reference=command.adapter_reference,
            model_config_reference=command.model_config_reference,
            authorization_decision_id=authorization.decision_id,
            authorization_policy_reference=authorization.policy_reference,
            idempotency_key=command.idempotency_key,
            command_digest=digest,
            execution_lease_id=self._lease_id_factory(),
            execution_lease_expires_at=admitted_at + self._execution_lease,
            execution_attempt=1,
            state=TranscriptionJobState.PENDING,
            created_at=admitted_at,
        )
        admission = await self._repository.admit(pending, now=admitted_at)
        if not admission.execution_owner:
            return TranscriptionResult(
                job=admission.job,
                artifact=await self._artifact_for(admission.job),
                replay=True,
            )

        request = SpeechToTextRequest(
            recording=AudioRecordingHandle(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                source_object_id=source.source_object_id,
                source_revision_id=source.revision_id,
                source_revision_digest=source.revision_digest,
                original_sha256=source.original_sha256,
                original_reference=source.original_reference,
                media_type=source.media_type,
                recording_duration_ms=command.recording_duration_ms,
            ),
            processing_profile_reference=command.processing_profile_reference,
            adapter_reference=command.adapter_reference,
            model_config_reference=command.model_config_reference,
            language_hint=command.language_hint,
        )
        prepared: AudioPreparationResult | None = None
        try:
            artifact_provenance: dict[str, object] = {}
            partial_failure: TranscriptionFailureProvenance | None = None
            cancelled = False
            if command.processing_profile is not None:
                if self._media_preparation is None:
                    raise SpeechToTextFailed(
                        code="invalid_adapter_output", retryable=False
                    )
                try:
                    preparation_request = AudioPreparationRequest(
                        recording=request.recording,
                        processing_profile=command.processing_profile,
                    )
                    prepared = await asyncio.wait_for(
                        self._media_preparation.prepare(preparation_request),
                        timeout=command.processing_profile.maximum_decode_seconds,
                    )
                except TimeoutError:
                    await self._media_preparation.abort(preparation_request)
                    raise SpeechToTextFailed(
                        code="resource_limit_exceeded", retryable=False
                    ) from None
                except BaseException:
                    await self._media_preparation.abort(preparation_request)
                    raise
                (
                    adapter_result,
                    chunk_receipts,
                    partial_failure,
                    cancelled,
                ) = await self._transcribe_chunks(
                    request=request,
                    prepared=prepared,
                    profile=command.processing_profile,
                    job_id=job_id,
                    lease_id=admission.lease_id,
                )
                recording_duration_ms = prepared.inspection.duration_ms
                artifact_provenance = {
                    "media_inspection": prepared.inspection,
                    "decoder_reference": prepared.decoder_reference,
                    "decoded_sha256": prepared.decoded_sha256,
                    "reassembly_reference": command.processing_profile.reassembly_reference,
                    "chunk_receipts": chunk_receipts,
                    "outcome": (
                        TranscriptionArtifactOutcome.PARTIAL
                        if partial_failure is not None
                        else TranscriptionArtifactOutcome.COMPLETED
                    ),
                    "failure": partial_failure,
                }
            else:
                adapter_result = await self._speech_to_text.transcribe(request)
                if not isinstance(adapter_result, SpeechToTextResult):
                    raise SpeechToTextFailed(
                        code="invalid_adapter_output", retryable=False
                    )
                recording_duration_ms = command.recording_duration_ms
            if (
                adapter_result.adapter_reference != command.adapter_reference
                or adapter_result.model_config_reference
                != command.model_config_reference
            ):
                raise SpeechToTextFailed(code="invalid_adapter_output", retryable=False)
            try:
                artifact = TranscriptionArtifact.create(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                source_object_id=source.source_object_id,
                source_revision_id=source.revision_id,
                source_revision_digest=source.revision_digest,
                original_sha256=source.original_sha256,
                processing_job_id=job_id,
                processing_profile_reference=command.processing_profile_reference,
                adapter_reference=command.adapter_reference,
                model_config_reference=command.model_config_reference,
                recording_duration_ms=recording_duration_ms,
                language=adapter_result.detected_language or command.language_hint,
                segments=adapter_result.segments,
                **artifact_provenance,
                created_at=self._clock(),
            )
            except (ValidationError, ValueError):
                raise SpeechToTextFailed(
                    code="invalid_adapter_output", retryable=False
                ) from None
            terminal_state = (
                TranscriptionJobState.CANCELLED
                if cancelled
                else (
                    TranscriptionJobState.PARTIAL
                    if partial_failure is not None
                    else TranscriptionJobState.COMPLETED
                )
            )
            terminal_code = (
                partial_failure.code if partial_failure is not None else "completed"
            )
            job = await self._repository.finish(
                job_id=job_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                state=terminal_state,
                completed_at=self._clock(),
                artifact=artifact,
                terminal_code=terminal_code,
                retryable=(
                    partial_failure.retryable if partial_failure is not None else None
                ),
                lease_id=admission.lease_id,
            )
            return TranscriptionResult(
                job=job,
                artifact=await self._artifact_for(job),
                replay=admission.replay,
            )
        except SpeechToTextCancelled:
            job = await self._repository.finish(
                job_id=job_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                state=TranscriptionJobState.CANCELLED,
                completed_at=self._clock(),
                artifact=None,
                terminal_code="cancelled",
                retryable=False,
                lease_id=admission.lease_id,
            )
            return TranscriptionResult(job=job, artifact=None, replay=admission.replay)
        except SpeechToTextFailed as failure:
            job = await self._repository.finish(
                job_id=job_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                state=TranscriptionJobState.FAILED,
                completed_at=self._clock(),
                artifact=None,
                terminal_code=failure.code.value,
                retryable=failure.retryable,
                lease_id=admission.lease_id,
            )
            return TranscriptionResult(job=job, artifact=None, replay=admission.replay)
        finally:
            if prepared is not None and self._media_preparation is not None:
                await self._media_preparation.release(prepared)

    async def _transcribe_chunks(
        self,
        *,
        request: SpeechToTextRequest,
        prepared: AudioPreparationResult,
        profile: TranscriptionProcessingProfile,
        job_id: UUID,
        lease_id: UUID,
    ) -> ChunkedTranscriptionOutcome:
        if prepared.decoder_reference != profile.decoder_reference:
            raise SpeechToTextFailed(code="invalid_adapter_output", retryable=False)
        if prepared.decoded_byte_length > profile.maximum_decoded_bytes:
            raise SpeechToTextFailed(code="resource_limit_exceeded", retryable=False)
        if prepared.peak_memory_bytes > profile.maximum_memory_bytes:
            raise SpeechToTextFailed(code="resource_limit_exceeded", retryable=False)
        if prepared.peak_working_bytes > profile.maximum_working_bytes:
            raise SpeechToTextFailed(code="resource_limit_exceeded", retryable=False)
        try:
            boundaries = profile.plan_chunks(prepared.inspection)
        except ValueError as error:
            message = str(error)
            code = (
                "resource_limit_exceeded"
                if "limit" in message or "maximum chunk" in message
                else "unsupported_media"
            )
            raise SpeechToTextFailed(code=code, retryable=False) from None

        checkpoint = await self._repository.get_checkpoint(
            workspace_id=request.recording.workspace_id,
            environment_id=request.recording.environment_id,
            job_id=job_id,
        )
        if checkpoint is not None:
            self._validate_checkpoint(
                checkpoint=checkpoint,
                request=request,
                prepared=prepared,
                profile=profile,
                job_id=job_id,
                boundaries=boundaries,
            )
        segments = list(checkpoint.segments) if checkpoint is not None else []
        receipts = list(checkpoint.chunk_receipts) if checkpoint is not None else []
        detected_language = checkpoint.detected_language if checkpoint is not None else None
        for boundary in boundaries[len(receipts) :]:
            chunk = AudioChunkHandle.create(
                boundary=boundary,
                decoder_reference=prepared.decoder_reference,
                decoded_reference=prepared.decoded_reference,
                decoded_sha256=prepared.decoded_sha256,
            )
            attempts = 0
            chunk_result: SpeechToTextResultV2 | None = None
            while attempts < profile.maximum_model_attempts:
                if await self._cancellation_requested(
                    workspace_id=request.recording.workspace_id,
                    environment_id=request.recording.environment_id,
                    job_id=job_id,
                ):
                    return self._cancelled_chunk_result(
                        request=request,
                        detected_language=detected_language,
                        segments=segments,
                        receipts=receipts,
                        failed_chunk_ordinal=boundary.ordinal,
                        model_attempts=attempts,
                    )
                attempts += 1
                try:
                    candidate = await self._speech_to_text.transcribe(
                        SpeechToTextRequestV2(
                            recording=request.recording,
                            processing_profile_reference=request.processing_profile_reference,
                            adapter_reference=request.adapter_reference,
                            model_config_reference=request.model_config_reference,
                            language_hint=request.language_hint,
                            chunk=chunk,
                        )
                    )
                    if not isinstance(candidate, SpeechToTextResultV2):
                        return self._invalid_chunk_result(
                            request=request,
                            detected_language=detected_language,
                            segments=segments,
                            receipts=receipts,
                            failed_chunk_ordinal=boundary.ordinal,
                            model_attempts=attempts,
                        )
                    chunk_result = candidate
                    break
                except SpeechToTextCancelled:
                    return self._cancelled_chunk_result(
                        request=request,
                        detected_language=detected_language,
                        segments=segments,
                        receipts=receipts,
                        failed_chunk_ordinal=boundary.ordinal,
                        model_attempts=attempts,
                    )
                except SpeechToTextFailed as failure:
                    if failure.retryable and attempts < profile.maximum_model_attempts:
                        continue
                    terminal_code = (
                        "retry_exhausted" if failure.retryable else failure.code.value
                    )
                    if not receipts:
                        raise SpeechToTextFailed(
                            code=terminal_code,
                            retryable=False,
                        ) from None
                    return self._stopped_chunk_result(
                        request=request,
                        detected_language=detected_language,
                        segments=segments,
                        receipts=receipts,
                        failure=TranscriptionFailureProvenance(
                            code=terminal_code,
                            cause_code=(
                                failure.code.value if failure.retryable else None
                            ),
                            failed_chunk_ordinal=boundary.ordinal,
                            model_attempts=attempts,
                            retryable=False,
                        ),
                        cancelled=False,
                    )
            if chunk_result is None:
                raise SpeechToTextFailed(code="retry_exhausted", retryable=False)
            if (
                chunk_result.adapter_reference != request.adapter_reference
                or chunk_result.model_config_reference
                != request.model_config_reference
            ):
                return self._invalid_chunk_result(
                    request=request,
                    detected_language=detected_language,
                    segments=segments,
                    receipts=receipts,
                    failed_chunk_ordinal=boundary.ordinal,
                    model_attempts=attempts,
                )
            detected_language = detected_language or chunk_result.detected_language
            normalized_segments: list[SpeechToTextSegment] = []
            for segment in chunk_result.segments:
                chunk_duration = boundary.end_ms - boundary.start_ms
                if segment.end_ms > chunk_duration:
                    return self._invalid_chunk_result(
                        request=request,
                        detected_language=detected_language,
                        segments=segments,
                        receipts=receipts,
                        failed_chunk_ordinal=boundary.ordinal,
                        model_attempts=attempts,
                    )
                absolute_start = boundary.start_ms + segment.start_ms
                absolute_end = boundary.start_ms + segment.end_ms
                overlap_end = boundary.start_ms + boundary.overlap_before_ms
                if boundary.ordinal > 1 and absolute_end <= overlap_end:
                    continue
                normalized_segments.append(
                    segment.model_copy(
                        update={"start_ms": absolute_start, "end_ms": absolute_end}
                    )
                )
            receipt = TranscriptionChunkReceipt(
                chunk_id=chunk.chunk_id,
                boundary=boundary,
                model_attempts=attempts,
            )
            next_receipts = [*receipts, receipt]
            next_segments = [*segments, *normalized_segments]
            next_checkpoint = TranscriptionChunkCheckpoint(
                job_id=job_id,
                source_revision_id=request.recording.source_revision_id,
                source_revision_digest=request.recording.source_revision_digest,
                processing_profile_reference=profile.profile_reference,
                decoder_reference=prepared.decoder_reference,
                decoded_sha256=prepared.decoded_sha256,
                reassembly_reference=profile.reassembly_reference,
                detected_language=detected_language,
                chunk_receipts=tuple(next_receipts),
                segments=tuple(next_segments),
                updated_at=self._clock(),
            )
            await self._repository.checkpoint_chunk(
                workspace_id=request.recording.workspace_id,
                environment_id=request.recording.environment_id,
                job_id=job_id,
                lease_id=lease_id,
                expected_completed_chunks=len(receipts),
                checkpoint=next_checkpoint,
            )
            receipts = next_receipts
            segments = next_segments
            if await self._cancellation_requested(
                workspace_id=request.recording.workspace_id,
                environment_id=request.recording.environment_id,
                job_id=job_id,
            ):
                return self._cancelled_chunk_result(
                    request=request,
                    detected_language=detected_language,
                    segments=segments,
                    receipts=receipts,
                    failed_chunk_ordinal=None,
                    model_attempts=0,
                )
        if not segments:
            raise SpeechToTextFailed(code="no_speech", retryable=False)
        return (
            SpeechToTextResult(
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language=detected_language,
                segments=tuple(segments),
            ),
            tuple(receipts),
            None,
            False,
        )

    @staticmethod
    def _validate_checkpoint(
        *,
        checkpoint: TranscriptionChunkCheckpoint,
        request: SpeechToTextRequest,
        prepared: AudioPreparationResult,
        profile: TranscriptionProcessingProfile,
        job_id: UUID,
        boundaries: tuple,
    ) -> None:
        expected_lineage = (
            job_id,
            request.recording.source_revision_id,
            request.recording.source_revision_digest,
            profile.profile_reference,
            prepared.decoder_reference,
            prepared.decoded_sha256,
            profile.reassembly_reference,
        )
        actual_lineage = (
            checkpoint.job_id,
            checkpoint.source_revision_id,
            checkpoint.source_revision_digest,
            checkpoint.processing_profile_reference,
            checkpoint.decoder_reference,
            checkpoint.decoded_sha256,
            checkpoint.reassembly_reference,
        )
        checkpoint_boundaries = tuple(
            receipt.boundary for receipt in checkpoint.chunk_receipts
        )
        if (
            actual_lineage != expected_lineage
            or checkpoint_boundaries != boundaries[: len(checkpoint_boundaries)]
        ):
            raise TranscriptionConflictError(
                "transcription checkpoint does not match immutable processing lineage"
            )

    async def _cancellation_requested(
        self, *, workspace_id: UUID, environment_id: UUID, job_id: UUID
    ) -> bool:
        job = await self._repository.get_job(
            workspace_id=workspace_id,
            environment_id=environment_id,
            job_id=job_id,
        )
        if job is None:
            raise TranscriptionConflictError("transcription job is unavailable")
        return job.cancellation_requested_at is not None

    @staticmethod
    def _cancelled_chunk_result(
        *,
        request: SpeechToTextRequest,
        detected_language: str | None,
        segments: list[SpeechToTextSegment],
        receipts: list[TranscriptionChunkReceipt],
        failed_chunk_ordinal: int | None,
        model_attempts: int,
    ) -> ChunkedTranscriptionOutcome:
        if not receipts:
            raise SpeechToTextCancelled()
        return TranscriptionService._stopped_chunk_result(
            request=request,
            detected_language=detected_language,
            segments=segments,
            receipts=receipts,
            failure=TranscriptionFailureProvenance(
                code="cancelled",
                failed_chunk_ordinal=failed_chunk_ordinal,
                model_attempts=model_attempts,
                retryable=False,
            ),
            cancelled=True,
        )

    @staticmethod
    def _invalid_chunk_result(
        *,
        request: SpeechToTextRequest,
        detected_language: str | None,
        segments: list[SpeechToTextSegment],
        receipts: list[TranscriptionChunkReceipt],
        failed_chunk_ordinal: int,
        model_attempts: int,
    ) -> ChunkedTranscriptionOutcome:
        if not receipts:
            raise SpeechToTextFailed(code="invalid_adapter_output", retryable=False)
        return TranscriptionService._stopped_chunk_result(
            request=request,
            detected_language=detected_language,
            segments=segments,
            receipts=receipts,
            failure=TranscriptionFailureProvenance(
                code="invalid_adapter_output",
                failed_chunk_ordinal=failed_chunk_ordinal,
                model_attempts=model_attempts,
                retryable=False,
            ),
            cancelled=False,
        )

    @staticmethod
    def _stopped_chunk_result(
        *,
        request: SpeechToTextRequest,
        detected_language: str | None,
        segments: list[SpeechToTextSegment],
        receipts: list[TranscriptionChunkReceipt],
        failure: TranscriptionFailureProvenance,
        cancelled: bool,
    ) -> ChunkedTranscriptionOutcome:
        return (
            SpeechToTextResult(
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language=detected_language,
                segments=tuple(segments),
            ),
            tuple(receipts),
            failure,
            cancelled,
        )

    async def _artifact_for(
        self, job: TranscriptionJob
    ) -> TranscriptionArtifact | None:
        if job.artifact_id is None:
            return None
        return await self._repository.get_artifact(
            workspace_id=job.workspace_id,
            environment_id=job.environment_id,
            artifact_id=job.artifact_id,
        )


__all__ = [
    "AudioProcessingAuthorization",
    "AudioProcessingAuthorizationRequest",
    "AudioProcessingAuthorizer",
    "AudioProcessingDeniedError",
    "AudioPreparationPort",
    "AudioPreparationRequest",
    "AudioPreparationResult",
    "InMemoryTranscriptionRepository",
    "SpeechToTextCancelled",
    "SpeechToTextFailed",
    "SpeechToTextPort",
    "TranscribeAudioCommand",
    "TranscriptionConflictError",
    "TranscriptionRepository",
    "TranscriptionResult",
    "TranscriptionService",
]
