import asyncio
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.transcription import (
    InMemoryTranscriptionRepository,
    AudioProcessingAuthorization,
    AudioProcessingDeniedError,
    SpeechToTextCancelled,
    SpeechToTextFailed,
    TranscribeAudioCommand,
    TranscriptionConflictError,
    TranscriptionService,
)
from spine.domain.artifacts.transcription import (
    AdapterSignal,
    SpeechToTextResult,
    SpeechToTextSegment,
    TranscriptionJobState,
)
from spine.domain.sources import RevisionMetadata, content_revision
from spine.domain.sources.original_reference import ObjectReference


WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE = UUID("10000000-0000-0000-0000-000000000002")
ENVIRONMENT = UUID("20000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc)
AUTHORIZATION_ID = UUID("60000000-0000-0000-0000-000000000001")


def audio_revision(*, workspace_id: UUID = WORKSPACE):
    original = ObjectReference(
        schema_version=1,
        object_id=UUID("30000000-0000-0000-0000-000000000001"),
        storage_generation=UUID("30000000-0000-0000-0000-000000000002"),
        digest_algorithm="sha256",
        digest_hex="a" * 64,
        byte_length=32000,
    )
    return content_revision(
        revision_id=UUID("50000000-0000-0000-0000-000000000001"),
        source_object_id=UUID("40000000-0000-0000-0000-000000000001"),
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        original_reference=original,
        media_type="audio/wav",
        revision_metadata=RevisionMetadata(
            embedded_title="synthetic russian short audio",
            document_language="ru",
        ),
        original_sha256=original.digest_hex,
        byte_length=original.byte_length,
        observed_at=NOW,
    )


def command(*, workspace_id: UUID = WORKSPACE) -> TranscribeAudioCommand:
    return TranscribeAudioCommand(
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT,
        source_revision=audio_revision(workspace_id=workspace_id),
        recording_duration_ms=2200,
        processing_profile_reference="transcription-short:v1",
        adapter_reference="fake-stt:v1",
        model_config_reference="fake-russian:v1",
        language_hint="ru",
        idempotency_key="transcribe-synthetic-russian-1",
    )


class FakeSpeechToText:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.requests = []

    async def transcribe(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return SpeechToTextResult(
            adapter_reference=request.adapter_reference,
            model_config_reference=request.model_config_reference,
            detected_language="ru",
            segments=(
                SpeechToTextSegment(
                    start_ms=120,
                    end_ms=2100,
                    text="Подготовьте коммерческое предложение.",
                    signals=(
                        AdapterSignal(name="fake_quality", value=0.91),
                    ),
                ),
            ),
        )


class FakeAuthorizer:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.requests = []

    async def authorize(self, request):
        self.requests.append(request)
        return AudioProcessingAuthorization(
            allowed=self.allowed,
            decision_id=AUTHORIZATION_ID,
            policy_reference="synthetic-audio-processing:v1",
            reason_code="allowed" if self.allowed else "processing_denied",
        )


@pytest.mark.asyncio
async def test_accepted_audio_creates_pinned_immutable_transcription_artifact() -> None:
    repository = InMemoryTranscriptionRepository()
    adapter = FakeSpeechToText()
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    )

    result = await service.transcribe(command())

    assert result.job.state is TranscriptionJobState.COMPLETED
    assert result.artifact is not None
    assert result.artifact.workspace_id == WORKSPACE
    assert result.artifact.environment_id == ENVIRONMENT
    assert result.artifact.source_revision_id == audio_revision().revision_id
    assert result.artifact.source_revision_digest == audio_revision().revision_digest
    assert result.artifact.original_sha256 == "a" * 64
    assert result.artifact.processing_profile_reference == "transcription-short:v1"
    assert result.artifact.adapter_reference == "fake-stt:v1"
    assert result.artifact.model_config_reference == "fake-russian:v1"
    assert result.job.authorization_decision_id == AUTHORIZATION_ID
    assert result.job.authorization_policy_reference == "synthetic-audio-processing:v1"
    assert result.artifact.recording_duration_ms == 2200
    assert result.artifact.segments[0].start_ms == 120
    assert result.artifact.segments[0].end_ms == 2100
    assert result.artifact.segments[0].signals == (
        AdapterSignal(name="fake_quality", value=0.91),
    )
    assert adapter.requests[0].recording.original_reference == audio_revision().original_reference


@pytest.mark.asyncio
async def test_exact_replay_returns_one_logical_job_and_artifact_without_second_model_call() -> None:
    repository = InMemoryTranscriptionRepository()
    adapter = FakeSpeechToText()
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    )

    first = await service.transcribe(command())
    replay = await service.transcribe(command())

    assert replay.replay is True
    assert replay.job.job_id == first.job.job_id
    assert replay.artifact == first.artifact
    assert await repository.count_jobs() == 1
    assert await repository.count_artifacts() == 1
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_state", "expected_code"),
    [
        (
            SpeechToTextFailed(code="permanent_model_error", retryable=False),
            TranscriptionJobState.FAILED,
            "permanent_model_error",
        ),
        (SpeechToTextCancelled(), TranscriptionJobState.CANCELLED, "cancelled"),
    ],
)
async def test_adapter_terminal_outcome_is_typed_and_creates_no_artifact(
    failure: Exception,
    expected_state: TranscriptionJobState,
    expected_code: str,
) -> None:
    repository = InMemoryTranscriptionRepository()
    adapter = FakeSpeechToText(failure=failure)
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    )

    result = await service.transcribe(command())
    replay = await service.transcribe(command())

    assert result.job.state is expected_state
    assert result.job.terminal_code == expected_code
    assert result.artifact is None
    assert replay.job == result.job
    assert replay.replay is True
    assert await repository.count_artifacts() == 0
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_repository_reads_are_isolated_between_workspaces() -> None:
    repository = InMemoryTranscriptionRepository()
    service = TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    )
    completed = await service.transcribe(command())

    assert await repository.get_job(
        workspace_id=OTHER_WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=completed.job.job_id,
    ) is None
    assert await repository.get_artifact(
        workspace_id=OTHER_WORKSPACE,
        environment_id=ENVIRONMENT,
        artifact_id=completed.artifact.artifact_id,
    ) is None


@pytest.mark.asyncio
async def test_same_idempotency_key_is_independent_between_two_workspaces() -> None:
    repository = InMemoryTranscriptionRepository()
    adapter = FakeSpeechToText()
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    )

    first = await service.transcribe(command(workspace_id=WORKSPACE))
    second = await service.transcribe(command(workspace_id=OTHER_WORKSPACE))

    assert first.job.job_id != second.job.job_id
    assert first.artifact.workspace_id == WORKSPACE
    assert second.artifact.workspace_id == OTHER_WORKSPACE
    assert await repository.count_jobs() == 2
    assert await repository.count_artifacts() == 2


@pytest.mark.asyncio
async def test_denied_processing_stops_before_job_or_model_disclosure() -> None:
    repository = InMemoryTranscriptionRepository()
    adapter = FakeSpeechToText()
    authorizer = FakeAuthorizer(allowed=False)
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=authorizer,
        clock=lambda: NOW,
    )

    with pytest.raises(AudioProcessingDeniedError, match="not authorized"):
        await service.transcribe(command())

    assert len(authorizer.requests) == 1
    assert adapter.requests == []
    assert await repository.count_jobs() == 0
    assert await repository.count_artifacts() == 0


@pytest.mark.asyncio
async def test_concurrent_exact_replay_has_one_execution_owner_and_one_model_call() -> None:
    class BlockingSpeechToText(FakeSpeechToText):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, request):
            self.started.set()
            await self.release.wait()
            return await super().transcribe(request)

    repository = InMemoryTranscriptionRepository()
    adapter = BlockingSpeechToText()
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    )

    owner = asyncio.create_task(service.transcribe(command()))
    await adapter.started.wait()
    concurrent_replay = await service.transcribe(command())
    adapter.release.set()
    completed = await owner
    terminal_replay = await service.transcribe(command())

    assert concurrent_replay.replay is True
    assert concurrent_replay.job.state is TranscriptionJobState.PENDING
    assert concurrent_replay.artifact is None
    assert completed.job.state is TranscriptionJobState.COMPLETED
    assert terminal_replay.artifact == completed.artifact
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_expired_pending_execution_is_reclaimed_after_worker_crash() -> None:
    repository = InMemoryTranscriptionRepository()
    current_time = [NOW]
    crashed = TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(failure=RuntimeError("worker crashed")),
        authorizer=FakeAuthorizer(),
        clock=lambda: current_time[0],
        lease_id_factory=lambda: UUID(int=701),
        execution_lease=timedelta(minutes=5),
    )

    with pytest.raises(RuntimeError, match="worker crashed"):
        await crashed.transcribe(command())

    current_time[0] = NOW + timedelta(minutes=6)
    pending_job_id = uuid5(
        NAMESPACE_URL,
        "spine:transcription-job:v1:"
        f"{WORKSPACE}:{ENVIRONMENT}:{audio_revision().revision_id}:"
        "transcription-short:v1:transcribe-synthetic-russian-1",
    )
    pending = await repository.get_job(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=pending_job_id,
    )
    assert pending is not None
    with pytest.raises(TranscriptionConflictError, match="lease has expired"):
        await repository.finish(
            job_id=pending.job_id,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            state=TranscriptionJobState.FAILED,
            completed_at=current_time[0],
            artifact=None,
            terminal_code="transient_unavailable",
            retryable=True,
            lease_id=UUID(int=701),
        )
    recovered_adapter = FakeSpeechToText()
    recovered = await TranscriptionService(
        repository=repository,
        speech_to_text=recovered_adapter,
        authorizer=FakeAuthorizer(),
        clock=lambda: current_time[0],
        lease_id_factory=lambda: UUID(int=702),
        execution_lease=timedelta(minutes=5),
    ).transcribe(command())

    assert recovered.replay is True
    assert recovered.job.state is TranscriptionJobState.COMPLETED
    assert recovered.job.execution_attempt == 2
    assert recovered.job.execution_lease_id == UUID(int=702)
    assert len(recovered_adapter.requests) == 1
    assert await repository.count_jobs() == 1
    with pytest.raises(TranscriptionConflictError, match="conflicts with history"):
        await repository.finish(
            job_id=recovered.job.job_id,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            state=TranscriptionJobState.FAILED,
            completed_at=current_time[0],
            artifact=None,
            terminal_code="transient_unavailable",
            retryable=True,
            lease_id=UUID(int=701),
        )


@pytest.mark.asyncio
async def test_segment_outside_recording_becomes_typed_invalid_adapter_failure() -> None:
    class InvalidSpeechToText(FakeSpeechToText):
        async def transcribe(self, request):
            self.requests.append(request)
            return SpeechToTextResult(
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                segments=(SpeechToTextSegment(start_ms=2100, end_ms=2300, text="Поздно"),),
            )

    repository = InMemoryTranscriptionRepository()
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=InvalidSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    ).transcribe(command())

    assert result.job.state is TranscriptionJobState.FAILED
    assert result.job.terminal_code == "invalid_adapter_output"
    assert result.job.retryable is False
    assert result.artifact is None


@pytest.mark.asyncio
async def test_terminal_transition_revalidates_job_shape() -> None:
    repository = InMemoryTranscriptionRepository()
    crashed = TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(failure=RuntimeError("worker crashed")),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
        lease_id_factory=lambda: UUID(int=703),
    )
    with pytest.raises(RuntimeError):
        await crashed.transcribe(command())
    requested = command()
    job_id = uuid5(
        NAMESPACE_URL,
        "spine:transcription-job:v1:"
        f"{requested.workspace_id}:{requested.environment_id}:"
        f"{requested.source_revision.revision_id}:"
        f"{requested.processing_profile_reference}:{requested.idempotency_key}",
    )
    pending = await repository.get_job(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=job_id,
    )
    assert pending is not None

    with pytest.raises(ValueError, match="completed transcription requires its artifact"):
        pending.terminal(
            state=TranscriptionJobState.COMPLETED,
            completed_at=NOW,
            artifact=None,
            terminal_code="completed",
            retryable=None,
        )
