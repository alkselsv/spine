from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.private_asr_corpus import (
    AdmittedSourceRevision,
    ContentVerificationResult,
    VerifiedAuthority,
)
from spine.application.private_asr_qualification import PrivateEvaluationUnavailable
from spine.application.transcription import AudioPreparationResult
from spine.domain.artifacts.transcription import (
    AudioChunkBoundary,
    AudioChunkHandle,
    AudioRecordingHandle,
    SpeechToTextRequestV2,
    TranscriptionProcessingProfile,
)
from spine.domain.evaluations.private_asr_corpus import AudioDigestVerification, PrivateReference
from spine.domain.sources import (
    IdentityMode,
    RevisionMetadata,
    SourceKind,
    SourceObject,
    content_revision,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.private_asr_audio import (
    DecoderProcessResult,
    LocalEphemeralDecodedAudioStore,
    PinnedFFmpegPrivateAudioPreparation,
)


def _id(number: int) -> UUID:
    return UUID(int=number)


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _private_ref(number: int, kind: str) -> PrivateReference:
    return PrivateReference(reference_id=_id(number), reference_kind=kind)


def _profile(**overrides) -> TranscriptionProcessingProfile:
    values = {
        "profile_reference": "private-audio:test:v1",
        "supported_media_types": ("audio/wav",),
        "supported_codecs": ("pcm_s16le",),
        "maximum_source_bytes": 100_000,
        "maximum_duration_ms": 10_000,
        "maximum_decoded_bytes": 100_000,
        "maximum_memory_bytes": 1_000_000,
        "maximum_working_bytes": 300_000,
        "maximum_decode_seconds": 2.0,
        "chunk_duration_ms": 1_000,
        "chunk_overlap_ms": 0,
        "maximum_chunks": 10,
        "maximum_model_attempts": 1,
        "decoder_reference": "ffmpeg-mono-16khz-pcm16:v1",
        "reassembly_reference": "reassembly:test:v1",
    }
    values.update(overrides)
    return TranscriptionProcessingProfile(**values)


class _StagedAudio:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0

    async def read(self, staged_ref, *, context):
        self.reads += 1
        assert staged_ref.reference_id == _id(20)
        yield self.payload[:10]
        yield self.payload[10:]


class _SourceGuard:
    def __init__(self, *, deny_on_call: int | None = None) -> None:
        self.calls = 0
        self.deny_on_call = deny_on_call

    async def current_and_exact(self, recording, content, *, context):
        self.calls += 1
        return self.calls != self.deny_on_call


class _DecodedStore:
    def __init__(self) -> None:
        self.payloads = {}
        self.deleted = []

    async def put(self, payload: bytes, *, expected_sha256: str) -> ObjectReference:
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        ref = ObjectReference(
            schema_version=1, object_id=_id(40 + len(self.payloads)),
            storage_generation=_id(50 + len(self.payloads)), digest_algorithm="sha256",
            digest_hex=expected_sha256, byte_length=len(payload),
        )
        self.payloads[ref.object_id] = payload
        return ref

    async def delete(self, reference: ObjectReference) -> None:
        self.deleted.append(reference)
        self.payloads.pop(reference.object_id, None)


class _Process:
    def __init__(
        self, probe: dict | None = None, decoded: bytes | None = None,
        *, peak_memory_bytes: int = 4096, elapsed_seconds: float = 0.01,
        probe_raw: bytes | None = None, enforce_output_limit: bool = True,
    ) -> None:
        self.probe = probe or {
            "format": {"format_name": "wav", "duration": "1.0"},
            "streams": [{
                "codec_type": "audio", "codec_name": "pcm_s16le",
                "bits_per_sample": 16, "duration": "1.0",
            }],
        }
        self.decoded = decoded or (b"\x01\x00" * 16_000)
        self.peak_memory_bytes = peak_memory_bytes
        self.elapsed_seconds = elapsed_seconds
        self.probe_raw = probe_raw
        self.enforce_output_limit = enforce_output_limit
        self.calls = []

    async def run(
        self, executable, arguments, source, *, timeout_seconds,
        memory_limit_bytes, output_limit_bytes,
    ):
        self.calls.append((executable, arguments, source, timeout_seconds, memory_limit_bytes, output_limit_bytes))
        is_probe = "-show_streams" in arguments
        output = (
            self.probe_raw if is_probe and self.probe_raw is not None
            else json.dumps(self.probe).encode() if is_probe
            else self.decoded
        )
        if self.enforce_output_limit and len(output) > output_limit_bytes:
            raise RuntimeError("over limit")
        return DecoderProcessResult(
            stdout=output, peak_memory_bytes=self.peak_memory_bytes,
            elapsed_seconds=self.elapsed_seconds,
        )


def _inputs(payload: bytes, *, media_type: str = "audio/wav"):
    digest = hashlib.sha256(payload).hexdigest()
    original_reference = ObjectReference(
        schema_version=1, object_id=_id(5), storage_generation=_id(6),
        digest_algorithm="sha256", digest_hex=digest, byte_length=len(payload),
    )
    source = SourceObject(
        source_object_id=_id(3), workspace_id=_id(1), environment_id=_id(2),
        source_kind=SourceKind.RECORD.value, identity_mode=IdentityMode.UPLOAD,
        upload_identity="synthetic-private-asr-recording", created_at=NOW,
    )
    revision = content_revision(
        revision_id=_id(4), source_object_id=_id(3), workspace_id=_id(1),
        environment_id=_id(2), kind="content", revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        original_reference=original_reference, media_type=media_type,
        revision_metadata=RevisionMetadata(document_language="ru"),
        original_sha256=digest, byte_length=len(payload), observed_at=NOW,
    )
    recording = AudioRecordingHandle(
        workspace_id=_id(1), environment_id=_id(2), source_object_id=_id(3),
        source_revision_id=_id(4), source_revision_digest=revision.revision_digest,
        original_sha256=digest,
        original_reference=original_reference,
        media_type=media_type, recording_duration_ms=1_000,
    )
    admitted = AdmittedSourceRevision(
        source_object=source, source_revision=revision, source_revision_number=1,
        workspace_id=_id(1), environment_id=_id(2),
        purpose_approval_ref=_private_ref(30, "purpose-approval:test:v1"),
        legal_data_evidence_ref=_private_ref(31, "legal-evidence:test:v1"),
        retention_profile_ref=_private_ref(32, "retention:test:v1"), actor_id=_id(9),
        audit_receipt_ref=_private_ref(33, "audit:test:v1"),
        access_policy_version=_id(7), policy_version="policy:test:v1",
        authority=VerifiedAuthority(
            purpose="asr_model_evaluation", process_granted=True,
            human_read_granted=True, legal_data_evidence_confirmed=True,
            purpose_approved=True, actor_authorized=True, retention_current=True,
            audit_permitted=True, environment_allowed=True, current=True,
            revoked=False, expires_at=NOW + timedelta(days=1), revocation_epoch=0,
            attestation_ref=_private_ref(34, "attestation:test:v1"),
            read_grant_ref=_private_ref(35, "read-grant:test:v1"),
            process_grant_ref=_private_ref(36, "process-grant:test:v1"),
        ),
        admitted_at=NOW,
    )
    content = ContentVerificationResult(
        admitted=admitted,
        digest_verification=AudioDigestVerification(
            audio_sha256=digest, source_revision_id=_id(4),
            byte_length=len(payload), verified_at=NOW,
            audit_receipt_ref=_private_ref(37, "content-audit:test:v1"),
        ),
        byte_length=len(payload), staged_ref=_private_ref(20, "stage:test:v1"),
        original_ref=_private_ref(21, "original:test:v1"),
    )
    return recording, content


def _adapter(tmp_path: Path, payload: bytes, process=None, guard=None, reader=None, store=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    binaries = []
    for name in ("ffmpeg", "ffprobe"):
        binary = tmp_path / name
        binary.write_bytes(f"synthetic {name}".encode())
        binary.chmod(0o500)
        binaries.append((str(binary), hashlib.sha256(binary.read_bytes()).hexdigest()))
    reader = reader or _StagedAudio(payload)
    store = store or _DecodedStore()
    source_guard = guard or _SourceGuard()
    adapter = PinnedFFmpegPrivateAudioPreparation(
        ffmpeg_path=binaries[0][0], ffprobe_path=binaries[1][0],
        ffmpeg_sha256=binaries[0][1], ffprobe_sha256=binaries[1][1],
        decoder_build_version="FFmpeg synthetic pinned build",
        staged_audio=reader, source_guard=source_guard, decoded_store=store,
        process=process or _Process(),
    )
    return adapter, reader, store, source_guard


@pytest.mark.asyncio
async def test_verified_wav_is_decoded_with_bounded_pipe_only_commands(tmp_path):
    raw = b"synthetic admitted wav bytes"
    recording, content = _inputs(raw)
    process = _Process()
    adapter, reader, store, guard = _adapter(tmp_path, raw, process=process)

    result = await adapter.prepare(recording, content, _profile(), context=None)

    assert isinstance(result, AudioPreparationResult)
    assert result.original_sha256 == recording.original_sha256
    assert result.decoder_build_sha256 == hashlib.sha256((tmp_path / "ffmpeg").read_bytes()).hexdigest()
    assert result.decoded_byte_length == 32_000
    assert result.inspection.container == "wav"
    assert len(result.chunks) == 1
    assert result.chunks[0].boundary.end_ms == 1_000
    assert result.chunks[0].decoded_reference.digest_hex != result.decoded_sha256
    assert reader.reads == 1
    assert guard.calls == 5
    assert all("-protocol_whitelist" in call[1] and "pipe" in call[1] for call in process.calls)
    assert any("atrim=end_sample=16000" in item for item in process.calls[1][1])
    assert store.payloads[result.decoded_reference.object_id] == process.decoded

    await adapter.release(result, context=None)
    await adapter.release(result, context=None)
    assert result.decoded_reference in store.deleted
    assert not store.payloads


@pytest.mark.asyncio
async def test_source_digest_mismatch_never_reaches_decoder(tmp_path):
    recording, content = _inputs(b"verified audio")
    reader = _StagedAudio(b"verifier audio")
    process = _Process()
    adapter, _, store, _ = _adapter(tmp_path, b"verifier audio", process=process, reader=reader)

    with pytest.raises(PrivateEvaluationUnavailable, match="digest"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert reader.reads == 1
    assert process.calls == []
    assert not store.payloads


@pytest.mark.asyncio
async def test_source_revision_lineage_mismatch_never_reads_staged_content(tmp_path):
    raw = b"synthetic admitted wav bytes"
    recording, content = _inputs(raw)
    changed_revision = content.admitted.source_revision.model_copy(
        update={"source_object_id": _id(999)}
    )
    content = content.model_copy(update={
        "admitted": content.admitted.model_copy(update={"source_revision": changed_revision}),
    })
    process = _Process()
    adapter, reader, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(PrivateEvaluationUnavailable, match="source identity"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert reader.reads == 0
    assert process.calls == []
    assert not store.payloads


@pytest.mark.asyncio
async def test_denied_source_is_not_read_or_decoded(tmp_path):
    raw = b"synthetic admitted wav bytes"
    recording, content = _inputs(raw)
    process = _Process()
    adapter, reader, store, _ = _adapter(
        tmp_path, raw, process=process, guard=_SourceGuard(deny_on_call=1)
    )

    with pytest.raises(PrivateEvaluationUnavailable, match="authorized"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert reader.reads == 0
    assert process.calls == []
    assert not store.payloads


@pytest.mark.asyncio
async def test_revocation_after_decode_discards_output_before_staging(tmp_path):
    raw = b"synthetic admitted wav bytes"
    recording, content = _inputs(raw)
    process = _Process()
    adapter, reader, store, _ = _adapter(
        tmp_path, raw, process=process, guard=_SourceGuard(deny_on_call=4)
    )

    with pytest.raises(PrivateEvaluationUnavailable, match="authorized"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert reader.reads == 1
    assert len(process.calls) == 2
    assert not store.payloads


@pytest.mark.asyncio
async def test_revocation_after_ephemeral_write_removes_every_staged_artifact(tmp_path):
    raw = b"synthetic admitted wav bytes"
    recording, content = _inputs(raw)
    process = _Process()
    adapter, _, store, _ = _adapter(
        tmp_path, raw, process=process, guard=_SourceGuard(deny_on_call=5)
    )

    with pytest.raises(PrivateEvaluationUnavailable, match="authorized"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert len(store.deleted) == 2
    assert not store.payloads


@pytest.mark.asyncio
async def test_arbitrary_path_is_not_a_verified_source_reference(tmp_path):
    raw = b"synthetic admitted wav bytes"
    recording, content = _inputs(raw)
    content = content.model_copy(update={"staged_ref": "/tmp/audio.wav"})
    process = _Process()
    adapter, reader, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(PrivateEvaluationUnavailable, match="opaque source references"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert reader.reads == 0
    assert process.calls == []
    assert not store.payloads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probe",
    [
        {"format": {"format_name": "ogg", "duration": "1.0"}, "streams": [{"codec_type": "audio", "codec_name": "opus", "duration": "1.0"}]},
        {"format": {"format_name": "wav", "duration": "1.0"}, "streams": [
            {"codec_type": "audio", "codec_name": "pcm_s16le", "bits_per_sample": 16},
            {"codec_type": "audio", "codec_name": "pcm_s16le", "bits_per_sample": 16},
        ]},
        {"format": {"format_name": "wav", "duration": "1.0"}, "streams": [{"codec_type": "audio", "codec_name": "pcm_s32le", "bits_per_sample": 32}]},
    ],
)
async def test_unsupported_or_ambiguous_media_never_runs_decoder(tmp_path, probe):
    raw = b"synthetic admitted wav bytes"
    recording, content = _inputs(raw)
    process = _Process(probe=probe)
    adapter, _, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(PrivateEvaluationUnavailable):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert len(process.calls) == 1
    assert not store.payloads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("format_name", "codec", "profile", "bits", "media_type", "container"),
    [
        ("wav", "pcm_s24le", None, 24, "audio/wav", "wav"),
        ("mp3", "mp3", None, None, "audio/mpeg", "mp3"),
        ("mov,mp4,m4a,3gp,3g2,mj2", "aac", "LC", None, "audio/mp4", "m4a"),
    ],
)
async def test_allowlisted_actual_media_container_and_codec_are_accepted(
    tmp_path, format_name, codec, profile, bits, media_type, container,
):
    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw, media_type=media_type)
    stream = {"codec_type": "audio", "codec_name": codec, "duration": "1.0"}
    if bits is not None:
        stream["bits_per_sample"] = bits
    if profile is not None:
        stream["profile"] = profile
    process = _Process(probe={
        "format": {"format_name": format_name, "duration": "1.0"},
        "streams": [stream],
    })
    adapter, _, store, _ = _adapter(tmp_path, raw, process=process)
    codecs = {"pcm_s24le": "pcm_s24le", "mp3": "mp3", "aac": "aac"}
    prepared = await adapter.prepare(
        recording, content,
        _profile(supported_media_types=(media_type,), supported_codecs=(codecs[codec],)),
        context=None,
    )

    assert prepared.inspection.container == container
    assert len(prepared.chunks) == 1
    assert len(store.payloads) == 2


@pytest.mark.asyncio
async def test_mime_metadata_mismatch_is_rejected_after_actual_inspection(tmp_path):
    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw)
    process = _Process(probe={
        "format": {"format_name": "mp3", "duration": "1.0"},
        "streams": [{"codec_type": "audio", "codec_name": "mp3", "duration": "1.0"}],
    })
    adapter, _, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(PrivateEvaluationUnavailable, match="admitted metadata"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert len(process.calls) == 1
    assert not store.payloads


@pytest.mark.asyncio
async def test_memory_limit_failure_does_not_stage_decoded_audio(tmp_path):
    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw)
    process = _Process(peak_memory_bytes=1_000_001)
    adapter, _, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(PrivateEvaluationUnavailable, match="memory limit"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert len(process.calls) == 1
    assert not store.payloads


@pytest.mark.asyncio
async def test_corrupt_probe_and_truncated_decode_fail_closed(tmp_path):
    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw)
    corrupt_process = _Process(probe_raw=b"not-json")
    corrupt_adapter, _, corrupt_store, _ = _adapter(
        tmp_path / "corrupt", raw, process=corrupt_process,
    )

    with pytest.raises(PrivateEvaluationUnavailable, match="inspection failed"):
        await corrupt_adapter.prepare(recording, content, _profile(), context=None)
    assert len(corrupt_process.calls) == 1
    assert not corrupt_store.payloads

    truncated_process = _Process(decoded=b"\x01\x00" * 15_999)
    truncated_adapter, _, truncated_store, _ = _adapter(
        tmp_path / "truncated", raw, process=truncated_process,
    )
    with pytest.raises(PrivateEvaluationUnavailable, match="decoded audio length"):
        await truncated_adapter.prepare(recording, content, _profile(), context=None)
    assert len(truncated_process.calls) == 2
    assert not truncated_store.payloads


@pytest.mark.asyncio
async def test_oversized_decoder_output_is_rejected_before_ephemeral_staging(tmp_path):
    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw)
    process = _Process(decoded=b"\x01" * 100_001, enforce_output_limit=False)
    adapter, _, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(PrivateEvaluationUnavailable, match="output limit"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert len(process.calls) == 2
    assert not store.payloads


@pytest.mark.asyncio
async def test_duration_exceeding_profile_is_rejected_before_decode(tmp_path):
    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw)
    process = _Process(probe={
        "format": {"format_name": "wav", "duration": "1.0"},
        "streams": [{
            "codec_type": "audio", "codec_name": "pcm_s16le",
            "bits_per_sample": 16, "duration": "1.0",
        }],
    })
    adapter, _, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(ValueError, match="duration limit"):
        await adapter.prepare(
            recording, content, _profile(maximum_duration_ms=500), context=None,
        )

    assert len(process.calls) == 1
    assert not store.payloads


@pytest.mark.asyncio
async def test_decoder_timeout_fails_without_fallback_or_staging(tmp_path):
    from spine.application.private_asr_qualification import PrivateEvaluationUnavailable

    class TimeoutProcess(_Process):
        async def run(self, executable, arguments, source, **limits):
            if "-show_streams" not in arguments:
                raise PrivateEvaluationUnavailable("decoder wall-time limit exceeded")
            return await super().run(executable, arguments, source, **limits)

    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw)
    process = TimeoutProcess()
    adapter, _, store, _ = _adapter(tmp_path, raw, process=process)

    with pytest.raises(PrivateEvaluationUnavailable, match="wall-time limit"):
        await adapter.prepare(recording, content, _profile(), context=None)

    assert len(process.calls) == 1
    assert not store.payloads


@pytest.mark.asyncio
async def test_cancelled_staging_is_cleaned_and_retry_can_complete(tmp_path):
    class CancelFirstChunkStore(_DecodedStore):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_once = True

        async def put(self, payload: bytes, *, expected_sha256: str) -> ObjectReference:
            if len(self.payloads) == 1 and self.cancel_once:
                self.cancel_once = False
                raise asyncio.CancelledError
            return await super().put(payload, expected_sha256=expected_sha256)

    raw = b"synthetic staged media bytes"
    recording, content = _inputs(raw)
    store = CancelFirstChunkStore()
    process = _Process()
    adapter, _, _, _ = _adapter(tmp_path, raw, process=process, store=store)

    with pytest.raises(asyncio.CancelledError):
        await adapter.prepare(recording, content, _profile(), context=None)
    assert len(store.deleted) == 1
    assert not store.payloads

    prepared = await adapter.prepare(recording, content, _profile(), context=None)
    assert len(prepared.chunks) == 1
    assert len(store.payloads) == 2
    await adapter.release(prepared, context=None)
    assert not store.payloads


@pytest.mark.asyncio
async def test_ephemeral_store_resolves_only_owned_verified_references_and_deletes(tmp_path):
    tmp_path.chmod(0o700)
    store = LocalEphemeralDecodedAudioStore(
        staging_parent=tmp_path, maximum_staged_bytes=100_000,
    )
    payload = b"ephemeral pcm bytes"
    reference = await store.put(payload, expected_sha256=hashlib.sha256(payload).hexdigest())

    assert store.local_path_for(reference).read_bytes() == payload
    await store.delete(reference)
    await store.delete(reference)
    with pytest.raises(PrivateEvaluationUnavailable, match="unavailable"):
        store.local_path_for(reference)
    await store.close()


@pytest.mark.asyncio
async def test_ephemeral_store_enforces_aggregate_disk_limit(tmp_path):
    tmp_path.chmod(0o700)
    store = LocalEphemeralDecodedAudioStore(
        staging_parent=tmp_path, maximum_staged_bytes=4,
    )
    payload = b"five!"

    with pytest.raises(PrivateEvaluationUnavailable, match="disk limit"):
        await store.put(payload, expected_sha256=hashlib.sha256(payload).hexdigest())

    await store.close()


@pytest.mark.asyncio
async def test_chunk_resolver_uses_the_exact_per_chunk_digest_and_boundary(tmp_path):
    import io
    import wave

    from spine.infrastructure.private_asr_audio import EphemeralAudioChunkResolver

    tmp_path.chmod(0o700)
    store = LocalEphemeralDecodedAudioStore(
        staging_parent=tmp_path, maximum_staged_bytes=100_000,
    )
    pcm = b"\x01\x00" * 16_000
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(pcm)
    wav_payload = wav_buffer.getvalue()
    reference = await store.put(wav_payload, expected_sha256=hashlib.sha256(wav_payload).hexdigest())
    recording, _ = _inputs(b"source")
    boundary = AudioChunkBoundary(ordinal=1, start_ms=0, end_ms=1_000, overlap_before_ms=0)
    handle = AudioChunkHandle.create(
        boundary=boundary, decoder_reference="ffmpeg-mono-16khz-pcm16:v1",
        decoded_reference=reference, decoded_sha256=reference.digest_hex,
    )
    request = SpeechToTextRequestV2(
        recording=recording, processing_profile_reference="profile:test:v1",
        adapter_reference="candidate:test:v1", model_config_reference="model:test:v1",
        chunk=handle,
    )

    resolved = await EphemeralAudioChunkResolver(store).resolve(request)

    assert Path(resolved.path).read_bytes() == wav_payload
    assert resolved.sha256 == reference.digest_hex
    assert resolved.duration_ms == 1_000
    await store.delete(reference)
    await store.close()
