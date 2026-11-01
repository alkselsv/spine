import asyncio
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.transcription import (
    AudioPreparationResult,
    InMemoryTranscriptionRepository,
    AudioProcessingAuthorization,
    AudioProcessingDeniedError,
    SpeechToTextCancelled,
    SpeechToTextFailed,
    TranscribeAudioCommand,
    TranscriptionConflictError,
    TranscriptionService,
)
from spine.application.transcription_evidence import (
    AudioEvidenceAccessDeniedError,
    AudioEvidenceAccessRequest,
    AudioEvidenceAuthorization,
    TranscriptionEvidenceResolutionError,
    TranscriptionEvidenceResolver,
)
from spine.domain.artifacts.transcription import (
    AdapterSignal,
    AudioMediaInspection,
    SpeechToTextResult,
    SpeechToTextResultV2,
    SpeechToTextSegment,
    TranscriptionProcessingProfile,
    TranscriptionArtifactOutcome,
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
                    signals=(AdapterSignal(name="fake_quality", value=0.91),),
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


def bounded_profile() -> TranscriptionProcessingProfile:
    return TranscriptionProcessingProfile(
        profile_reference="transcription-long:v1",
        supported_media_types=("audio/wav", "audio/mpeg"),
        supported_codecs=("pcm_s16le", "mp3"),
        maximum_source_bytes=2_000_000,
        maximum_duration_ms=60_000,
        maximum_decoded_bytes=8_000_000,
        maximum_memory_bytes=4_000_000,
        maximum_working_bytes=10_000_000,
        maximum_decode_seconds=5.0,
        chunk_duration_ms=4_000,
        chunk_overlap_ms=1_000,
        maximum_chunks=20,
        maximum_model_attempts=2,
        decoder_reference="fake-decoder:v1",
        reassembly_reference="first-chunk-wins:v1",
    )


def test_bounded_command_requires_explicit_v2_contract() -> None:
    payload = command().model_dump()
    payload.update(
        {
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
        }
    )

    with pytest.raises(ValueError, match="v1 transcription command"):
        TranscribeAudioCommand.model_validate(payload)

    payload["schema_version"] = "transcribe-audio-command:v2"
    bounded = TranscribeAudioCommand.model_validate(payload)

    assert bounded.schema_version == "transcribe-audio-command:v2"


def test_legacy_command_keeps_v1_digest_after_v2_contract_is_added() -> None:
    assert command().digest() == (
        "9e5f20e626d2a2339642d78b7629a45eede09a34eb0c9d50400c85e4848e2c12"
    )


class FakeAudioPreparation:
    def __init__(self) -> None:
        self.active = set()

    async def prepare(self, request):
        result = AudioPreparationResult(
            inspection=AudioMediaInspection(
                inspected_media_type="audio/mpeg",
                container="mp3",
                codec="mp3",
                byte_length=request.recording.original_reference.byte_length,
                duration_ms=10_000,
                audio_stream_count=1,
                silent=False,
            ),
            decoder_reference="fake-decoder:v1",
            decoded_reference=request.recording.original_reference.model_copy(
                update={"object_id": UUID(int=801), "byte_length": 640_000}
            ),
            decoded_sha256=request.recording.original_sha256,
            decoded_byte_length=640_000,
            peak_memory_bytes=320_000,
            peak_working_bytes=960_000,
        )
        self.active.add(result.decoded_reference.object_id)
        return result

    async def release(self, prepared):
        self.active.discard(prepared.decoded_reference.object_id)

    async def abort(self, request):
        self.active.clear()


class ChunkedSpeechToText:
    def __init__(self) -> None:
        self.requests = []

    async def transcribe(self, request):
        self.requests.append(request)
        local_start = 100 if request.chunk.boundary.ordinal == 1 else 1_100
        return SpeechToTextResultV2(
            schema_version="speech-to-text-result:v2",
            adapter_reference=request.adapter_reference,
            model_config_reference=request.model_config_reference,
            detected_language="ru",
            timeline="chunk",
            segments=(
                SpeechToTextSegment(
                    start_ms=local_start,
                    end_ms=local_start + 400,
                    text=f"Фрагмент {request.chunk.boundary.ordinal}",
                ),
            ),
        )


@pytest.mark.asyncio
async def test_long_audio_is_inspected_chunked_and_reassembled_reproducibly() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    preparation = FakeAudioPreparation()
    adapter = ChunkedSpeechToText()
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "recording_duration_ms": 10_000,
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": "transcribe-synthetic-russian-long-1",
        }
    )
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=preparation,
        clock=lambda: NOW,
    )

    result = await service.transcribe(requested)

    assert result.job.state is TranscriptionJobState.COMPLETED
    assert result.artifact is not None
    assert result.artifact.media_inspection.inspected_media_type == "audio/mpeg"
    assert result.artifact.decoder_reference == "fake-decoder:v1"
    assert result.artifact.reassembly_reference == "first-chunk-wins:v1"
    assert [(item.start_ms, item.end_ms) for item in result.artifact.segments] == [
        (100, 500),
        (4_100, 4_500),
        (7_100, 7_500),
    ]
    assert [request.chunk.boundary.ordinal for request in adapter.requests] == [1, 2, 3]
    assert len({request.chunk.chunk_id for request in adapter.requests}) == 3
    assert preparation.active == set()


@pytest.mark.asyncio
async def test_chunked_artifact_preserves_missing_detected_language() -> None:
    class UndetectedLanguage(ChunkedSpeechToText):
        async def transcribe(self, request):
            result = await super().transcribe(request)
            return result.model_copy(update={"detected_language": None})

    adapter = UndetectedLanguage()
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "recording_duration_ms": 10_000,
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": "transcribe-without-detected-language",
        }
    )
    result = await TranscriptionService(
        repository=InMemoryTranscriptionRepository(clock=lambda: NOW),
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=FakeAudioPreparation(),
        clock=lambda: NOW,
    ).transcribe(requested)

    assert result.artifact.language is None
    assert all(request.language_hint == "ru" for request in adapter.requests)


@pytest.mark.asyncio
async def test_expired_worker_resumes_after_durable_chunk_checkpoint() -> None:
    class CrashesOnceOnSecondChunk(ChunkedSpeechToText):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        async def transcribe(self, request):
            if request.chunk.boundary.ordinal == 2 and not self.crashed:
                self.requests.append(request)
                self.crashed = True
                raise RuntimeError("synthetic worker loss")
            return await super().transcribe(request)

    current_time = [NOW]
    repository = InMemoryTranscriptionRepository(clock=lambda: current_time[0])
    adapter = CrashesOnceOnSecondChunk()
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "recording_duration_ms": 10_000,
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": "resume-after-durable-chunk",
        }
    )
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=FakeAudioPreparation(),
        clock=lambda: current_time[0],
    )

    with pytest.raises(RuntimeError, match="worker loss"):
        await service.transcribe(requested)
    job_id = uuid5(
        NAMESPACE_URL,
        "spine:transcription-job:v2:"
        f"{WORKSPACE}:{ENVIRONMENT}:{audio_revision().revision_id}:"
        "transcription-long:v1:resume-after-durable-chunk",
    )
    first_attempt = await repository.get_job(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, job_id=job_id
    )
    checkpoint = await repository.get_checkpoint(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, job_id=job_id
    )
    assert checkpoint is not None
    assert [item.boundary.ordinal for item in checkpoint.chunk_receipts] == [1]

    current_time[0] = NOW + timedelta(minutes=6)
    with pytest.raises(TranscriptionConflictError, match="lease is stale"):
        await repository.checkpoint_chunk(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            job_id=job_id,
            lease_id=first_attempt.execution_lease_id,
            expected_completed_chunks=1,
            checkpoint=checkpoint,
        )
    resumed = await service.transcribe(requested)

    assert resumed.job.state is TranscriptionJobState.COMPLETED
    assert resumed.job.execution_attempt == 2
    assert [request.chunk.boundary.ordinal for request in adapter.requests] == [1, 2, 2, 3]
    with pytest.raises(TranscriptionConflictError, match="lease is stale"):
        await repository.checkpoint_chunk(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            job_id=job_id,
            lease_id=first_attempt.execution_lease_id,
            expected_completed_chunks=1,
            checkpoint=checkpoint,
        )


@pytest.mark.asyncio
async def test_authorized_time_range_resolves_to_exact_original_audio() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    ).transcribe(command())
    locator = result.artifact.locate_time_range(start_ms=120, end_ms=700)

    class Revisions:
        async def resolve_revision(self, *, workspace_id, environment_id, revision_id):
            revision = audio_revision()
            if (
                workspace_id == revision.workspace_id
                and environment_id == revision.environment_id
                and revision_id == revision.revision_id
            ):
                return revision
            return None

    class EvidenceAuthorizer:
        def __init__(self) -> None:
            self.requests: list[AudioEvidenceAccessRequest] = []

        async def authorize(self, request):
            self.requests.append(request)
            return AudioEvidenceAuthorization(
                allowed=True,
                decision_id=UUID(int=7001),
                policy_reference="synthetic-audio-evidence:v1",
                reason_code="allowed",
            )

    authorizer = EvidenceAuthorizer()
    resolved = await TranscriptionEvidenceResolver(
        artifacts=repository,
        revisions=Revisions(),
        authorizer=authorizer,
    ).resolve(locator)

    assert resolved.original_reference == audio_revision().original_reference
    assert (resolved.locator.start_ms, resolved.locator.end_ms) == (120, 700)
    assert authorizer.requests[0].artifact_id == result.artifact.artifact_id


@pytest.mark.asyncio
async def test_time_range_resolution_rejects_mismatched_artifact_lineage() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    ).transcribe(command())
    locator = result.artifact.locate_time_range(start_ms=120, end_ms=700).model_copy(
        update={"artifact_digest": "0" * 64}
    )

    class Revisions:
        async def resolve_revision(self, **kwargs):
            return audio_revision()

    class EvidenceAuthorizer:
        async def authorize(self, request):
            return AudioEvidenceAuthorization(
                allowed=True,
                decision_id=UUID(int=7002),
                policy_reference="synthetic-audio-evidence:v1",
                reason_code="allowed",
            )

    with pytest.raises(TranscriptionEvidenceResolutionError, match="lineage"):
        await TranscriptionEvidenceResolver(
            artifacts=repository,
            revisions=Revisions(),
            authorizer=EvidenceAuthorizer(),
        ).resolve(locator)


@pytest.mark.asyncio
async def test_time_range_resolution_denies_before_reading_artifact() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    ).transcribe(command())

    class Denied:
        async def authorize(self, request):
            return AudioEvidenceAuthorization(
                allowed=False,
                decision_id=UUID(int=7003),
                policy_reference="synthetic-audio-evidence:v1",
                reason_code="access_denied",
            )

    class Revisions:
        async def resolve_revision(self, **kwargs):
            raise AssertionError("denied resolution must not read source lineage")

    with pytest.raises(AudioEvidenceAccessDeniedError, match="not authorized"):
        await TranscriptionEvidenceResolver(
            artifacts=repository,
            revisions=Revisions(),
            authorizer=Denied(),
        ).resolve(result.artifact.locate_time_range(start_ms=120, end_ms=700))


@pytest.mark.asyncio
async def test_time_range_resolution_does_not_disclose_cross_workspace_artifact() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    ).transcribe(command())
    foreign_locator = result.artifact.locate_time_range(
        start_ms=120, end_ms=700
    ).model_copy(update={"workspace_id": OTHER_WORKSPACE})

    class Allowed:
        async def authorize(self, request):
            return AudioEvidenceAuthorization(
                allowed=True,
                decision_id=UUID(int=7004),
                policy_reference="synthetic-audio-evidence:v1",
                reason_code="allowed",
            )

    class Revisions:
        async def resolve_revision(self, **kwargs):
            raise AssertionError("missing scoped artifact must stop resolution")

    with pytest.raises(TranscriptionEvidenceResolutionError, match="unavailable"):
        await TranscriptionEvidenceResolver(
            artifacts=repository,
            revisions=Revisions(),
            authorizer=Allowed(),
        ).resolve(foreign_locator)


@pytest.mark.asyncio
async def test_time_range_resolution_rejects_missing_artifact() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    ).transcribe(command())
    locator = result.artifact.locate_time_range(start_ms=120, end_ms=700).model_copy(
        update={"artifact_id": UUID(int=7007)}
    )

    class Allowed:
        async def authorize(self, request):
            return AudioEvidenceAuthorization(
                allowed=True,
                decision_id=UUID(int=7008),
                policy_reference="synthetic-audio-evidence:v1",
                reason_code="allowed",
            )

    class Revisions:
        async def resolve_revision(self, **kwargs):
            raise AssertionError("missing artifact must stop resolution")

    with pytest.raises(TranscriptionEvidenceResolutionError, match="unavailable"):
        await TranscriptionEvidenceResolver(
            artifacts=repository,
            revisions=Revisions(),
            authorizer=Allowed(),
        ).resolve(locator)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", ["missing", "wrong_revision", "wrong_scope"])
async def test_time_range_resolution_rejects_missing_or_stale_source_lineage(
    source_case: str,
) -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    ).transcribe(command())
    locator = result.artifact.locate_time_range(start_ms=120, end_ms=700)

    class Allowed:
        async def authorize(self, request):
            return AudioEvidenceAuthorization(
                allowed=True,
                decision_id=UUID(int=7005),
                policy_reference="synthetic-audio-evidence:v1",
                reason_code="allowed",
            )

    class Revisions:
        async def resolve_revision(self, **kwargs):
            if source_case == "missing":
                return None
            if source_case == "wrong_revision":
                return audio_revision().model_copy(update={"revision_id": UUID(int=7006)})
            return audio_revision().model_copy(update={"workspace_id": OTHER_WORKSPACE})

    with pytest.raises(TranscriptionEvidenceResolutionError, match="stale or mismatched"):
        await TranscriptionEvidenceResolver(
            artifacts=repository,
            revisions=Revisions(),
            authorizer=Allowed(),
        ).resolve(locator)


@pytest.mark.asyncio
async def test_transient_chunk_failure_retries_with_the_same_stable_identity() -> None:
    class LostResponseSpeechToText(ChunkedSpeechToText):
        def __init__(self) -> None:
            super().__init__()
            self.logical_effects = {}

        async def transcribe(self, request):
            self.requests.append(request)
            result = self.logical_effects.setdefault(
                request.chunk.chunk_id,
                SpeechToTextResultV2(
                    schema_version="speech-to-text-result:v2",
                    adapter_reference=request.adapter_reference,
                    model_config_reference=request.model_config_reference,
                    detected_language="ru",
                    timeline="chunk",
                    segments=(
                        SpeechToTextSegment(start_ms=100, end_ms=500, text="Ответ"),
                    ),
                ),
            )
            if len(self.requests) == 1:
                raise SpeechToTextFailed(code="transient_unavailable", retryable=True)
            return result

    adapter = LostResponseSpeechToText()
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "recording_duration_ms": 1_000,
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": "transcribe-lost-response",
        }
    )
    preparation = FakeAudioPreparation()
    original_prepare = preparation.prepare

    async def short_prepare(request):
        result = await original_prepare(request)
        return result.model_copy(
            update={
                "inspection": result.inspection.model_copy(
                    update={"duration_ms": 1_000}
                )
            }
        )

    preparation.prepare = short_prepare
    result = await TranscriptionService(
        repository=InMemoryTranscriptionRepository(clock=lambda: NOW),
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=preparation,
        clock=lambda: NOW,
    ).transcribe(requested)

    assert result.job.state is TranscriptionJobState.COMPLETED
    assert result.artifact.chunk_receipts[0].model_attempts == 2
    assert adapter.requests[0].chunk.chunk_id == adapter.requests[1].chunk.chunk_id
    assert len(adapter.logical_effects) == 1
    assert preparation.active == set()


@pytest.mark.asyncio
async def test_retry_exhaustion_persists_exact_partial_artifact_and_replays_it() -> (
    None
):
    class SecondChunkFails(ChunkedSpeechToText):
        async def transcribe(self, request):
            self.requests.append(request)
            if request.chunk.boundary.ordinal == 2:
                raise SpeechToTextFailed(code="transient_unavailable", retryable=True)
            return SpeechToTextResultV2(
                schema_version="speech-to-text-result:v2",
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                timeline="chunk",
                segments=(
                    SpeechToTextSegment(start_ms=100, end_ms=500, text="Готово"),
                ),
            )

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    adapter = SecondChunkFails()
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=FakeAudioPreparation(),
        clock=lambda: NOW,
    )
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "recording_duration_ms": 10_000,
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": "transcribe-partial-retry-exhausted",
        }
    )

    result = await service.transcribe(requested)
    replay = await service.transcribe(requested)

    assert result.job.state is TranscriptionJobState.PARTIAL
    assert result.job.terminal_code == "retry_exhausted"
    assert result.artifact.outcome is TranscriptionArtifactOutcome.PARTIAL
    assert [item.boundary.ordinal for item in result.artifact.chunk_receipts] == [1]
    assert result.artifact.failure.code == "retry_exhausted"
    assert result.artifact.failure.cause_code == "transient_unavailable"
    assert result.artifact.failure.failed_chunk_ordinal == 2
    assert len(result.artifact.segments) == 1
    assert replay.artifact == result.artifact
    assert len(adapter.requests) == 3


@pytest.mark.asyncio
async def test_partial_artifact_preserves_completed_silent_chunk_receipt() -> None:
    class SilentThenFails(ChunkedSpeechToText):
        async def transcribe(self, request):
            self.requests.append(request)
            if request.chunk.boundary.ordinal == 2:
                raise SpeechToTextFailed(
                    code="permanent_model_error", retryable=False
                )
            return SpeechToTextResultV2(
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                segments=(),
            )

    result = await TranscriptionService(
        repository=InMemoryTranscriptionRepository(clock=lambda: NOW),
        speech_to_text=SilentThenFails(),
        authorizer=FakeAuthorizer(),
        media_preparation=FakeAudioPreparation(),
        clock=lambda: NOW,
    ).transcribe(
        command().model_copy(
            update={
                "schema_version": "transcribe-audio-command:v2",
                "recording_duration_ms": 10_000,
                "processing_profile_reference": "transcription-long:v1",
                "processing_profile": bounded_profile(),
                "idempotency_key": "partial-after-silent-completed-chunk",
            }
        )
    )

    assert result.job.state is TranscriptionJobState.PARTIAL
    assert result.artifact.outcome is TranscriptionArtifactOutcome.PARTIAL
    assert result.artifact.segments == ()
    assert [receipt.boundary.ordinal for receipt in result.artifact.chunk_receipts] == [1]


@pytest.mark.asyncio
async def test_cancellation_after_completed_chunk_stops_subsequent_model_work() -> None:
    class PausingSpeechToText(ChunkedSpeechToText):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, request):
            self.requests.append(request)
            self.started.set()
            await self.release.wait()
            return SpeechToTextResultV2(
                schema_version="speech-to-text-result:v2",
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                timeline="chunk",
                segments=(
                    SpeechToTextSegment(start_ms=100, end_ms=500, text="Готово"),
                ),
            )

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    adapter = PausingSpeechToText()
    preparation = FakeAudioPreparation()
    original_prepare = preparation.prepare

    async def short_prepare(request):
        result = await original_prepare(request)
        return result.model_copy(
            update={
                "inspection": result.inspection.model_copy(
                    update={"duration_ms": 1_000}
                )
            }
        )

    preparation.prepare = short_prepare
    service = TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=preparation,
        clock=lambda: NOW,
    )
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "recording_duration_ms": 1_000,
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": "transcribe-cancelled-after-first-chunk",
        }
    )

    running = asyncio.create_task(service.transcribe(requested))
    await adapter.started.wait()
    job_id = uuid5(
        NAMESPACE_URL,
        "spine:transcription-job:v2:"
        f"{WORKSPACE}:{ENVIRONMENT}:{audio_revision().revision_id}:"
        "transcription-long:v1:transcribe-cancelled-after-first-chunk",
    )
    with pytest.raises(TranscriptionConflictError, match="unavailable"):
        await service.cancel(
            workspace_id=UUID(int=9991),
            environment_id=ENVIRONMENT,
            job_id=job_id,
        )
    with pytest.raises(TranscriptionConflictError, match="unavailable"):
        await service.cancel(
            workspace_id=WORKSPACE,
            environment_id=UUID(int=9992),
            job_id=job_id,
        )
    await service.cancel(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=job_id,
    )
    adapter.release.set()
    cancelled = await running
    replay = await service.transcribe(requested)

    assert cancelled.job.state is TranscriptionJobState.CANCELLED
    assert cancelled.job.cancellation_requested_at == NOW
    assert cancelled.artifact.outcome is TranscriptionArtifactOutcome.PARTIAL
    assert cancelled.artifact.failure.code == "cancelled"
    assert len(adapter.requests) == 1
    assert replay.job == cancelled.job
    assert replay.artifact == cancelled.artifact


@pytest.mark.asyncio
async def test_cancellation_accepted_during_finalization_wins_atomically() -> None:
    class FinalizationRaceRepository(InMemoryTranscriptionRepository):
        async def finish(self, **kwargs):
            if kwargs["state"] is TranscriptionJobState.COMPLETED:
                await self.request_cancellation(
                    workspace_id=kwargs["workspace_id"],
                    environment_id=kwargs["environment_id"],
                    job_id=kwargs["job_id"],
                    requested_at=kwargs["completed_at"],
                )
            return await super().finish(**kwargs)

    repository = FinalizationRaceRepository(clock=lambda: NOW)
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=ChunkedSpeechToText(),
        authorizer=FakeAuthorizer(),
        media_preparation=FakeAudioPreparation(),
        clock=lambda: NOW,
    ).transcribe(
        command().model_copy(
            update={
                "schema_version": "transcribe-audio-command:v2",
                "processing_profile_reference": "transcription-long:v1",
                "processing_profile": bounded_profile(),
                "idempotency_key": "cancellation-wins-finalization-race",
            }
        )
    )

    assert result.job.state is TranscriptionJobState.CANCELLED
    assert result.job.terminal_code == "cancelled"
    assert result.artifact is None
    assert await repository.count_artifacts() == 0


@pytest.mark.asyncio
async def test_invalid_later_chunk_preserves_completed_work_as_partial() -> None:
    class InvalidSecondChunk(ChunkedSpeechToText):
        async def transcribe(self, request):
            self.requests.append(request)
            result_type = (
                SpeechToTextResult
                if request.chunk.boundary.ordinal == 2
                else SpeechToTextResultV2
            )
            return result_type(
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                segments=(
                    SpeechToTextSegment(start_ms=100, end_ms=500, text="Фрагмент"),
                ),
            )

    result = await TranscriptionService(
        repository=InMemoryTranscriptionRepository(clock=lambda: NOW),
        speech_to_text=InvalidSecondChunk(),
        authorizer=FakeAuthorizer(),
        media_preparation=FakeAudioPreparation(),
        clock=lambda: NOW,
    ).transcribe(
        command().model_copy(
            update={
                "schema_version": "transcribe-audio-command:v2",
                "processing_profile_reference": "transcription-long:v1",
                "processing_profile": bounded_profile(),
                "idempotency_key": "invalid-second-chunk",
            }
        )
    )

    assert result.job.state is TranscriptionJobState.PARTIAL
    assert result.job.terminal_code == "invalid_adapter_output"
    assert result.artifact.outcome is TranscriptionArtifactOutcome.PARTIAL
    assert [item.boundary.ordinal for item in result.artifact.chunk_receipts] == [1]
    assert result.artifact.failure.failed_chunk_ordinal == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inspection_overrides", "expected_code"),
    [
        ({"byte_length": 0, "duration_ms": 0}, "unsupported_media"),
        ({"silent": True}, "unsupported_media"),
        (
            {"inspected_media_type": "audio/ogg", "container": "ogg", "codec": "opus"},
            "unsupported_media",
        ),
        ({"byte_length": 2_000_001}, "resource_limit_exceeded"),
        ({"duration_ms": 60_001}, "resource_limit_exceeded"),
    ],
)
async def test_ineligible_inspected_media_stops_before_model_and_creates_no_artifact(
    inspection_overrides: dict[str, object], expected_code: str
) -> None:
    class IneligibleAudioPreparation(FakeAudioPreparation):
        async def prepare(self, request):
            result = await super().prepare(request)
            return result.model_copy(
                update={
                    "inspection": result.inspection.model_copy(
                        update=inspection_overrides
                    )
                }
            )

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    preparation = IneligibleAudioPreparation()
    adapter = ChunkedSpeechToText()
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": f"ineligible-{expected_code}-{len(str(inspection_overrides))}",
        }
    )

    result = await TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=preparation,
        clock=lambda: NOW,
    ).transcribe(requested)

    assert result.job.state is TranscriptionJobState.FAILED
    assert result.job.terminal_code == expected_code
    assert result.artifact is None
    assert adapter.requests == []
    assert await repository.count_artifacts() == 0
    assert preparation.active == set()


@pytest.mark.asyncio
async def test_no_speech_output_cannot_create_transcription_artifact() -> None:
    class NoSpeech:
        def __init__(self) -> None:
            self.requests = []

        async def transcribe(self, request):
            self.requests.append(request)
            return SpeechToTextResultV2(
                schema_version="speech-to-text-result:v2",
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                timeline="chunk",
                segments=(),
            )

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    adapter = NoSpeech()
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": "synthetic-silence-no-speech",
        }
    )

    result = await TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=FakeAudioPreparation(),
        clock=lambda: NOW,
    ).transcribe(requested)

    assert result.job.state is TranscriptionJobState.FAILED
    assert result.job.terminal_code == "no_speech"
    assert result.artifact is None
    assert await repository.count_artifacts() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind",
    ["corrupt", "decoded_oversized", "memory_oversized", "working_oversized"],
)
async def test_malformed_or_decoded_oversized_media_cleans_up_without_model_work(
    failure_kind: str,
) -> None:
    class FailingPreparation(FakeAudioPreparation):
        async def prepare(self, request):
            if failure_kind == "corrupt":
                raise SpeechToTextFailed(code="corrupt_media", retryable=False)
            result = await super().prepare(request)
            if failure_kind == "decoded_oversized":
                return result.model_copy(
                    update={
                        "decoded_reference": result.decoded_reference.model_copy(
                            update={"byte_length": 8_000_001}
                        ),
                        "decoded_byte_length": 8_000_001,
                    }
                )
            if failure_kind == "memory_oversized":
                return result.model_copy(update={"peak_memory_bytes": 4_000_001})
            return result.model_copy(update={"peak_working_bytes": 10_000_001})

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    preparation = FailingPreparation()
    adapter = ChunkedSpeechToText()
    requested = command().model_copy(
        update={
            "schema_version": "transcribe-audio-command:v2",
            "processing_profile_reference": "transcription-long:v1",
            "processing_profile": bounded_profile(),
            "idempotency_key": f"synthetic-{failure_kind}",
        }
    )

    result = await TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=preparation,
        clock=lambda: NOW,
    ).transcribe(requested)

    assert result.job.state is TranscriptionJobState.FAILED
    assert result.job.terminal_code == (
        "corrupt_media" if failure_kind == "corrupt" else "resource_limit_exceeded"
    )
    assert result.artifact is None
    assert adapter.requests == []
    assert preparation.active == set()


@pytest.mark.asyncio
async def test_decode_timeout_aborts_preparation_and_stops_before_model() -> None:
    class TimedOutPreparation(FakeAudioPreparation):
        def __init__(self) -> None:
            super().__init__()
            self.aborted = False

        async def prepare(self, request):
            self.active.add(request.recording.source_revision_id)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def abort(self, request):
            self.aborted = True
            self.active.discard(request.recording.source_revision_id)

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    preparation = TimedOutPreparation()
    adapter = ChunkedSpeechToText()
    result = await TranscriptionService(
        repository=repository,
        speech_to_text=adapter,
        authorizer=FakeAuthorizer(),
        media_preparation=preparation,
        clock=lambda: NOW,
    ).transcribe(
        command().model_copy(
            update={
                "schema_version": "transcribe-audio-command:v2",
                "processing_profile_reference": "transcription-long:v1",
                "processing_profile": bounded_profile().model_copy(
                    update={"maximum_decode_seconds": 0.01}
                ),
                "idempotency_key": "decode-timeout-cleanup",
            }
        )
    )

    assert result.job.state is TranscriptionJobState.FAILED
    assert result.job.terminal_code == "resource_limit_exceeded"
    assert result.artifact is None
    assert preparation.aborted is True
    assert preparation.active == set()
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_noisy_synthetic_russian_fixture_preserves_adapter_uncertainty() -> None:
    class NoisyRussianSpeech:
        async def transcribe(self, request):
            return SpeechToTextResultV2(
                schema_version="speech-to-text-result:v2",
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                timeline="chunk",
                segments=(
                    SpeechToTextSegment(
                        start_ms=100,
                        end_ms=900,
                        text="Нужен расчёт с уточнением.",
                        signals=(
                            AdapterSignal(name="background_noise", value=True),
                            AdapterSignal(name="recognition_quality", value=0.42),
                        ),
                    ),
                ),
            )

    preparation = FakeAudioPreparation()
    original_prepare = preparation.prepare

    async def noisy_short_fixture(request):
        result = await original_prepare(request)
        return result.model_copy(
            update={
                "inspection": result.inspection.model_copy(
                    update={"duration_ms": 1_000}
                )
            }
        )

    preparation.prepare = noisy_short_fixture
    result = await TranscriptionService(
        repository=InMemoryTranscriptionRepository(clock=lambda: NOW),
        speech_to_text=NoisyRussianSpeech(),
        authorizer=FakeAuthorizer(),
        media_preparation=preparation,
        clock=lambda: NOW,
    ).transcribe(
        command().model_copy(
            update={
                "schema_version": "transcribe-audio-command:v2",
                "processing_profile_reference": "transcription-long:v1",
                "processing_profile": bounded_profile(),
                "idempotency_key": "synthetic-russian-noise-fixture",
            }
        )
    )

    assert result.job.state is TranscriptionJobState.COMPLETED
    assert result.artifact.segments[0].signals == (
        AdapterSignal(name="background_noise", value=True),
        AdapterSignal(name="recognition_quality", value=0.42),
    )


@pytest.mark.asyncio
async def test_accepted_audio_creates_pinned_immutable_transcription_artifact() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
    assert (
        adapter.requests[0].recording.original_reference
        == audio_revision().original_reference
    )


@pytest.mark.asyncio
async def test_exact_replay_returns_one_logical_job_and_artifact_without_second_model_call() -> (
    None
):
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    service = TranscriptionService(
        repository=repository,
        speech_to_text=FakeSpeechToText(),
        authorizer=FakeAuthorizer(),
        clock=lambda: NOW,
    )
    completed = await service.transcribe(command())

    assert (
        await repository.get_job(
        workspace_id=OTHER_WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=completed.job.job_id,
        )
        is None
    )
    assert (
        await repository.get_artifact(
        workspace_id=OTHER_WORKSPACE,
        environment_id=ENVIRONMENT,
        artifact_id=completed.artifact.artifact_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_same_idempotency_key_is_independent_between_two_workspaces() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
async def test_concurrent_exact_replay_has_one_execution_owner_and_one_model_call() -> (
    None
):
    class BlockingSpeechToText(FakeSpeechToText):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, request):
            self.started.set()
            await self.release.wait()
            return await super().transcribe(request)

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
async def test_segment_outside_recording_becomes_typed_invalid_adapter_failure() -> (
    None
):
    class InvalidSpeechToText(FakeSpeechToText):
        async def transcribe(self, request):
            self.requests.append(request)
            return SpeechToTextResult(
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                detected_language="ru",
                segments=(
                    SpeechToTextSegment(start_ms=2100, end_ms=2300, text="Поздно"),
                ),
            )

    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
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

    with pytest.raises(
        ValueError, match="completed transcription requires its artifact"
    ):
        pending.terminal(
            state=TranscriptionJobState.COMPLETED,
            completed_at=NOW,
            artifact=None,
            terminal_code="completed",
            retryable=None,
        )

    with pytest.raises(ValueError, match="failed transcription requires"):
        pending.terminal(
            state=TranscriptionJobState.FAILED,
            completed_at=NOW,
            artifact=None,
            terminal_code="resource_limit_exceeded",
            retryable=False,
        )
