"""Governed, idempotent processing of one immutable audio SourceRevision."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from spine.domain.artifacts.transcription import (
    AudioRecordingHandle,
    SpeechToTextRequest,
    SpeechToTextResult,
    TranscriptionArtifact,
    TranscriptionFailureCode,
    TranscriptionJob,
    TranscriptionJobState,
)
from spine.domain.sources import RevisionKind, SourceRevision


class TranscriptionConflictError(Exception):
    """An idempotency identity conflicts with immutable job history."""


class AudioProcessingDeniedError(Exception):
    """Current policy denied processing before any audio/model disclosure."""


class SpeechToTextFailed(Exception):
    """A disclosure-safe typed adapter failure."""

    def __init__(self, *, code: TranscriptionFailureCode | str, retryable: bool) -> None:
        typed_code = TranscriptionFailureCode(code)
        super().__init__(typed_code.value)
        self.code = typed_code
        self.retryable = retryable


class SpeechToTextCancelled(Exception):
    """The adapter stopped work after cancellation was requested."""


class SpeechToTextPort(Protocol):
    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextResult: ...


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

    workspace_id: UUID
    environment_id: UUID
    source_revision: SourceRevision
    recording_duration_ms: int = Field(gt=0)
    processing_profile_reference: str = Field(min_length=1, max_length=128)
    adapter_reference: str = Field(min_length=1, max_length=128)
    model_config_reference: str = Field(min_length=1, max_length=128)
    language_hint: str | None = Field(default=None, min_length=2, max_length=35)
    idempotency_key: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_source_scope(self) -> "TranscribeAudioCommand":
        source = self.source_revision
        if source.workspace_id != self.workspace_id or source.environment_id != self.environment_id:
            raise ValueError("audio revision must belong to the exact command scope")
        if source.kind is not RevisionKind.CONTENT or source.original_reference is None:
            raise ValueError("transcription requires an immutable content revision")
        if source.media_type is None or not source.media_type.startswith("audio/"):
            raise ValueError("transcription requires an audio media type")
        return self

    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"idempotency_key", "source_revision"})
        payload.update(
            {
                "schema": "transcribe-audio-command:v1",
                "source_object_id": str(self.source_revision.source_object_id),
                "source_revision_id": str(self.source_revision.revision_id),
                "source_revision_digest": self.source_revision.revision_digest,
                "original_sha256": self.source_revision.original_sha256,
            }
        )
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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


class TranscriptionRepository(Protocol):
    async def admit(self, job: TranscriptionJob, *, now: datetime) -> TranscriptionAdmission: ...

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


class InMemoryTranscriptionRepository:
    """Atomic tenant-scoped canonical fake for application and contract tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[tuple[UUID, UUID, UUID], TranscriptionJob] = {}
        self._commands: dict[tuple[UUID, UUID, str], UUID] = {}
        self._artifacts: dict[tuple[UUID, UUID, UUID], TranscriptionArtifact] = {}

    async def admit(self, job: TranscriptionJob, *, now: datetime) -> TranscriptionAdmission:
        scope = (job.workspace_id, job.environment_id)
        command_key = (*scope, job.idempotency_key)
        job_key = (*scope, job.job_id)
        async with self._lock:
            existing_id = self._commands.get(command_key)
            if existing_id is not None:
                existing = self._jobs[(*scope, existing_id)]
                if existing.command_digest != job.command_digest:
                    raise TranscriptionConflictError("transcription command conflicts with history")
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
                    raise TranscriptionConflictError("transcription job identity conflicts with history")
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
                raise TranscriptionConflictError("transcription execution lease is stale")
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
                    raise TranscriptionConflictError("transcription artifact conflicts with history")
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
    ) -> None:
        self._repository = repository
        self._speech_to_text = speech_to_text
        self._authorizer = authorizer
        self._clock = clock
        self._lease_id_factory = lease_id_factory
        self._execution_lease = execution_lease

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
        job_id = uuid5(
            NAMESPACE_URL,
            "spine:transcription-job:v1:"
            f"{command.workspace_id}:{command.environment_id}:{source.revision_id}:"
            f"{command.processing_profile_reference}:{command.idempotency_key}",
        )
        admitted_at = self._clock()
        pending = TranscriptionJob(
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
        try:
            adapter_result = await self._speech_to_text.transcribe(request)
            if (
                adapter_result.adapter_reference != command.adapter_reference
                or adapter_result.model_config_reference != command.model_config_reference
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
                recording_duration_ms=command.recording_duration_ms,
                language=adapter_result.detected_language or command.language_hint,
                    segments=adapter_result.segments,
                    created_at=self._clock(),
                )
            except (ValidationError, ValueError):
                raise SpeechToTextFailed(code="invalid_adapter_output", retryable=False) from None
            job = await self._repository.finish(
                job_id=job_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                state=TranscriptionJobState.COMPLETED,
                completed_at=self._clock(),
                artifact=artifact,
                terminal_code="completed",
                retryable=None,
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

    async def _artifact_for(self, job: TranscriptionJob) -> TranscriptionArtifact | None:
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
