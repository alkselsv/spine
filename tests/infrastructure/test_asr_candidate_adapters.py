from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import UUID
import wave
import asyncio

import pytest

from spine.domain.artifacts.transcription import (
    AudioChunkBoundary,
    AudioChunkHandle,
    AudioRecordingHandle,
    SpeechToTextRequest,
    SpeechToTextRequestV2,
    SpeechToTextSegment,
)
from spine.application.transcription import SpeechToTextCancelled, SpeechToTextFailed
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.asr_candidates import (
    AssetDigest,
    CandidateRuntimeManifest,
    GigaAMV3RNNTAdapter,
    WhisperLargeV3Adapter,
    NoEgressEvidence,
    ResolvedAudioChunk,
    canonical_configuration_digest,
    validate_runtime_manifest,
)
from spine.evals.asr_candidate_qualification_runner import _reassemble_segments, preflight_candidate, run_preflight_from_file, save_immutable_report
from spine.evals.transcription_russian_v2 import (
    load_russian_transcription_v2_dataset,
    TRANSCRIPTION_V1_SHA256,
    transcription_dataset_v2_sha256,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_wav(path: Path) -> bytes:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(bytes(32_000))
    return path.read_bytes()


def _runtime_manifest(tmp_path: Path) -> CandidateRuntimeManifest:
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    checkpoint = asset_root / "model.ckpt"
    checkpoint.write_bytes(b"synthetic checkpoint bytes")
    lock = tmp_path / "candidate.lock"
    lock.write_text("pinned offline dependency lock\n", encoding="utf-8")
    preprocessor = tmp_path / "ffmpeg"
    preprocessor.write_bytes(b"synthetic local ffmpeg binary")
    attestation = NoEgressEvidence(
        schema_version="asr-no-egress-attestation:v1",
        boundary_reference="isolated-test-boundary:v1",
        enforcement="network_namespace",
        network_egress_disabled=True,
        network_namespace_inode=os.stat("/proc/self/ns/net").st_ino,
        default_route_absent=True,
        non_loopback_interfaces_absent=True,
    )
    evidence = tmp_path / "no-egress.json"
    evidence.write_text(attestation.model_dump_json(), encoding="utf-8")
    payload = {
        "schema_version": "asr-candidate-runtime:v1",
        "candidate_id": "gigaam-v3-rnnt",
        "adapter_reference": "gigaam-v3-rnnt-adapter:v1",
        "model_config_reference": "gigaam-v3-rnnt-frozen:v1",
        "source_repository": "https://github.com/salute-developers/GigaAM",
        "source_revision": "a" * 40,
        "runtime_reference": "official-gigaam-runtime:v1",
        "runtime_revision": "b" * 40,
        "dependency_lock_path": str(lock),
        "dependency_lock_sha256": _sha(lock.read_bytes()),
        "preprocessing_executable": str(preprocessor),
        "preprocessing_executable_sha256": _sha(preprocessor.read_bytes()),
        "preprocessing_profile_reference": "ffmpeg-mono-16khz-pcm-s16le:v1",
        "asset_root": str(asset_root),
        "checkpoint_path": str(checkpoint),
        "asset_inventory": [
            {
                "path": "model.ckpt",
                "sha256": _sha(checkpoint.read_bytes()),
                "byte_length": checkpoint.stat().st_size,
            }
        ],
        "checkpoint_sha256": _sha(checkpoint.read_bytes()),
        "precision": "fp32_encoder",
        "device": "cpu",
        "language": "ru",
        "translation_enabled": False,
        "language_detection_enabled": False,
        "no_egress_evidence_path": str(evidence),
        "no_egress_evidence_sha256": _sha(evidence.read_bytes()),
        "no_egress": attestation.model_dump(mode="json"),
        "host": {
            "cpu": "synthetic test CPU",
            "ram_mb": 4096,
            "operating_system": "Linux test",
            "python_version": "3.14.4",
        },
        "license_review_reference": "local-test-license-review:v1",
        "license_commercial_use_approved": True,
        "dataset_id": "russian-meeting-transcription-synthetic",
        "dataset_version": "2.0.0",
        "dataset_sha256": "c" * 64,
        "configuration_digest": "0" * 64,
    }
    manifest = CandidateRuntimeManifest.model_validate(payload)
    return manifest.model_copy(update={"configuration_digest": canonical_configuration_digest(manifest)})


class _Resolver:
    def __init__(self, resolved: ResolvedAudioChunk) -> None:
        self.resolved = resolved

    async def resolve(self, request: SpeechToTextRequestV2) -> ResolvedAudioChunk:
        return self.resolved


class _Backend:
    def __init__(self, output: object) -> None:
        self.output = output
        self.loaded = False

    def load(self, manifest: CandidateRuntimeManifest, asset_root: Path) -> object:
        self.loaded = True
        return object()

    def transcribe(self, model: object, audio_path: Path, manifest: CandidateRuntimeManifest) -> object:
        return self.output


def _request(path: Path, payload: bytes, *, duration_ms: int = 1000) -> SpeechToTextRequestV2:
    digest = _sha(payload)
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID("50000000-0000-4000-8000-000000000001"),
        storage_generation=UUID("60000000-0000-4000-8000-000000000001"),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=len(payload),
    )
    recording = AudioRecordingHandle(
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        source_object_id=UUID("30000000-0000-4000-8000-000000000001"),
        source_revision_id=UUID("40000000-0000-4000-8000-000000000001"),
        source_revision_digest="d" * 64,
        original_sha256=digest,
        original_reference=reference,
        media_type="audio/wav",
        recording_duration_ms=duration_ms,
    )
    boundary = AudioChunkBoundary(ordinal=1, start_ms=0, end_ms=duration_ms, overlap_before_ms=0)
    chunk = AudioChunkHandle.create(
        boundary=boundary,
        decoder_reference="offline-test-decoder:v1",
        decoded_reference=reference,
        decoded_sha256=digest,
    )
    return SpeechToTextRequestV2(
        recording=recording,
        processing_profile_reference="offline-asr-qualification:v1",
        adapter_reference="gigaam-v3-rnnt-adapter:v1",
        model_config_reference="gigaam-v3-rnnt-frozen:v1",
        language_hint="ru",
        chunk=chunk,
    )


def test_runtime_manifest_rejects_unlisted_or_changed_assets(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    checkpoint = Path(manifest.checkpoint_path)
    checkpoint.write_bytes(b"changed checkpoint")

    assert "asset_digest_mismatch:model.ckpt" in validate_runtime_manifest(manifest)
    assert "checkpoint_not_bound_to_asset_inventory" not in validate_runtime_manifest(manifest)


@pytest.mark.asyncio
async def test_gigaam_adapter_maps_word_timestamps_through_the_port(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    audio_path = tmp_path / "chunk.wav"
    payload = _write_wav(audio_path)
    audio = ResolvedAudioChunk(
        path=str(audio_path), sha256=_sha(payload), byte_length=len(payload), duration_ms=1000
    )
    backend = _Backend(
        SimpleNamespace(
            text="Привет",
            words=(SimpleNamespace(start=0.1, end=0.6, text="Привет"),),
        )
    )
    adapter = GigaAMV3RNNTAdapter(manifest, _Resolver(audio), backend=backend)
    result = await adapter.transcribe(_request(audio_path, payload))

    assert backend.loaded
    assert result.detected_language == "ru"
    assert result.timeline == "chunk"
    assert [(item.start_ms, item.end_ms, item.text) for item in result.segments] == [(100, 600, "Привет")]


@pytest.mark.asyncio
async def test_gigaam_adapter_rejects_non_russian_or_wrong_configuration(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    audio_path = tmp_path / "chunk.wav"
    payload = _write_wav(audio_path)
    backend = _Backend(SimpleNamespace(text="Привет", words=()))
    adapter = GigaAMV3RNNTAdapter(
        manifest,
        _Resolver(ResolvedAudioChunk(path=str(audio_path), sha256=_sha(payload), byte_length=len(payload), duration_ms=1000)),
        backend=backend,
    )
    request = _request(audio_path, payload).model_copy(update={"language_hint": "en"})

    with pytest.raises(Exception, match="policy_denied"):
        await adapter.transcribe(request)


@pytest.mark.asyncio
async def test_gigaam_adapter_marks_chunk_window_timestamp_fallback(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    audio_path = tmp_path / "chunk.wav"
    payload = _write_wav(audio_path)
    backend = _Backend(SimpleNamespace(text="Привет мир", words=()))
    adapter = GigaAMV3RNNTAdapter(
        manifest,
        _Resolver(ResolvedAudioChunk(path=str(audio_path), sha256=_sha(payload), byte_length=len(payload), duration_ms=1000)),
        backend=backend,
    )

    result = await adapter.transcribe(_request(audio_path, payload))

    assert result.segments[0].signals[0].name == "timestamp_source"
    assert result.segments[0].signals[0].value == "chunk_window"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("backend failed"), asyncio.CancelledError()])
async def test_adapter_maps_partial_runtime_failure_and_cancellation(tmp_path: Path, failure: BaseException) -> None:
    manifest = _runtime_manifest(tmp_path)
    audio_path = tmp_path / "chunk.wav"
    payload = _write_wav(audio_path)

    class FailingBackend(_Backend):
        def transcribe(self, model: object, audio_path: Path, manifest: CandidateRuntimeManifest) -> object:
            raise failure

    adapter = GigaAMV3RNNTAdapter(
        manifest,
        _Resolver(ResolvedAudioChunk(path=str(audio_path), sha256=_sha(payload), byte_length=len(payload), duration_ms=1000)),
        backend=FailingBackend(None),
    )
    expected = SpeechToTextCancelled if isinstance(failure, asyncio.CancelledError) else SpeechToTextFailed

    with pytest.raises(expected):
        await adapter.transcribe(_request(audio_path, payload))


@pytest.mark.asyncio
async def test_adapter_waits_for_uninterruptible_inference_before_reporting_cancellation(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    audio_path = tmp_path / "chunk.wav"
    payload = _write_wav(audio_path)
    started = threading.Event()
    finish = threading.Event()

    class SlowBackend(_Backend):
        def transcribe(self, model: object, audio_path: Path, manifest: CandidateRuntimeManifest) -> object:
            started.set()
            finish.wait(timeout=2)
            return SimpleNamespace(text="готово", words=())

    adapter = GigaAMV3RNNTAdapter(
        manifest,
        _Resolver(ResolvedAudioChunk(path=str(audio_path), sha256=_sha(payload), byte_length=len(payload), duration_ms=1000)),
        backend=SlowBackend(None),
    )
    task = asyncio.create_task(adapter.transcribe(_request(audio_path, payload)))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()

    with pytest.raises(SpeechToTextCancelled):
        await task


def test_chunk_reassembly_restores_timeline_order() -> None:
    segments = (
        SpeechToTextSegment(start_ms=1000, end_ms=1200, text="второй"),
        SpeechToTextSegment(start_ms=0, end_ms=300, text="первый"),
    )

    assert [item.text for item in _reassemble_segments(segments)] == ["первый", "второй"]


def test_candidate_runtime_manifest_binds_its_frozen_configuration(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    changed = manifest.model_copy(update={"beam_size": 4})

    assert canonical_configuration_digest(manifest) == manifest.configuration_digest
    assert "configuration_digest_mismatch" in validate_runtime_manifest(changed)


def test_qualification_preflight_preserves_not_verified_for_missing_coverage(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    dataset = load_russian_transcription_v2_dataset()
    manifest = manifest.model_copy(update={
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_sha256": transcription_dataset_v2_sha256(dataset),
        "configuration_digest": "0" * 64,
    })
    manifest = manifest.model_copy(update={"configuration_digest": canonical_configuration_digest(manifest)})
    data_root = Path(__file__).parents[2] / "src/spine/evals/data"

    report, ready = preflight_candidate(manifest, dataset, asset_root=data_root)

    assert not ready
    assert report.dataset_version == "2.0.0"
    assert report.overall_status.value == "NOT_VERIFIED"
    assert "lack approved, met annotation minima" in next(
        item.detail for item in report.prerequisites if item.name == "qualification_coverage_sufficient"
    )
    assert all(gate.status.value == "NOT_VERIFIED" for gate in report.gates)
    assert report.case_observations == ()


def test_qualification_report_is_immutable_and_serializes_not_verified(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    dataset = load_russian_transcription_v2_dataset()
    manifest = manifest.model_copy(update={
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_sha256": transcription_dataset_v2_sha256(dataset),
        "configuration_digest": "0" * 64,
    })
    manifest = manifest.model_copy(update={"configuration_digest": canonical_configuration_digest(manifest)})
    report, _ = preflight_candidate(
        manifest,
        dataset,
        asset_root=Path(__file__).parents[2] / "src/spine/evals/data",
    )

    output = tmp_path / "actual-report.json"
    digest = save_immutable_report(report, output)

    assert len(digest) == 64
    assert json.loads(output.read_text(encoding="utf-8"))["overall_status"] == "NOT_VERIFIED"
    with pytest.raises(FileExistsError):
        save_immutable_report(report, output)


def test_candidate_manifest_rejects_qualification_set_tuning(tmp_path: Path) -> None:
    manifest = _runtime_manifest(tmp_path)
    payload = manifest.model_dump(mode="json")
    payload["qualification_configuration_frozen"] = False

    with pytest.raises(ValueError, match="configuration must be frozen"):
        CandidateRuntimeManifest.model_validate(payload)


@pytest.mark.asyncio
async def test_whisper_runtime_pins_local_russian_transcription(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    giga_manifest = _runtime_manifest(tmp_path)
    payload = giga_manifest.model_dump(mode="json")
    payload.update({
        "candidate_id": "whisper-large-v3-faster-whisper",
        "adapter_reference": "whisper-large-v3-adapter:v1",
        "model_config_reference": "whisper-large-v3-frozen:v1",
        "source_repository": "https://github.com/SYSTRAN/faster-whisper",
        "runtime_reference": "faster-whisper-ctranslate2:v1",
        "precision": "float32",
        "configuration_digest": "0" * 64,
    })
    whisper_manifest = CandidateRuntimeManifest.model_validate(payload)
    whisper_manifest = whisper_manifest.model_copy(update={
        "configuration_digest": canonical_configuration_digest(whisper_manifest)
    })
    calls: dict[str, object] = {}

    class FakeWhisperModel:
        def __init__(self, path: str, **kwargs: object) -> None:
            calls["model_path"] = path
            calls["model_kwargs"] = kwargs

        def transcribe(self, path: str, **kwargs: object) -> tuple[tuple[object, ...], object]:
            calls["transcribe_kwargs"] = kwargs
            return (SimpleNamespace(start=0.0, end=0.5, text="Привет"),), SimpleNamespace(language="ru")

    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel))
    audio_path = tmp_path / "whisper-chunk.wav"
    payload_bytes = _write_wav(audio_path)
    audio = ResolvedAudioChunk(path=str(audio_path), sha256=_sha(payload_bytes), byte_length=len(payload_bytes), duration_ms=1000)
    adapter = WhisperLargeV3Adapter(whisper_manifest, _Resolver(audio))
    request = _request(audio_path, payload_bytes).model_copy(update={
        "adapter_reference": whisper_manifest.adapter_reference,
        "model_config_reference": whisper_manifest.model_config_reference,
    })

    result = await adapter.transcribe(request)

    assert calls["model_path"] == whisper_manifest.asset_root
    assert calls["model_kwargs"]["local_files_only"] is True
    assert calls["transcribe_kwargs"]["language"] == "ru"
    assert calls["transcribe_kwargs"]["task"] == "transcribe"
    assert result.detected_language == "ru"
    assert result.segments[0].text == "Привет"


def test_historical_v1_dataset_is_recognized_without_inference(tmp_path: Path) -> None:
    payload = _runtime_manifest(tmp_path).model_dump(mode="json")
    payload.update({"dataset_version": "1.0.0", "dataset_sha256": TRANSCRIPTION_V1_SHA256, "configuration_digest": "0" * 64})
    manifest = CandidateRuntimeManifest.model_validate(payload)
    manifest = manifest.model_copy(update={"configuration_digest": canonical_configuration_digest(manifest)})
    manifest_path = tmp_path / "historical.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")

    report = run_preflight_from_file(manifest_path, tmp_path / "absent-dataset-assets")

    assert report.dataset_version == "1.0.0"
    assert report.dataset_sha256 == TRANSCRIPTION_V1_SHA256
    assert report.overall_status.value == "NOT_VERIFIED"
    assert report.case_observations == ()
