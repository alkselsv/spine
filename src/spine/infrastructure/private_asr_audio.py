"""Governed FFmpeg decoding for the private ASR qualification runner.

The decoder accepts no paths. Source bytes arrive through an opaque stage
reference, and decoded bytes leave through a short lived object store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import tempfile
import time
import wave
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.private_asr_corpus import ContentVerificationResult
from spine.application.private_asr_qualification import (
    GovernedPrivateAudioPreparationPort,
    PrivateEvaluationUnavailable,
)
from spine.application.transcription import AudioPreparationResult, AudioPreparedChunk
from spine.domain.artifacts.transcription import (
    AudioMediaInspection,
    AudioRecordingHandle,
    SpeechToTextRequestV2,
    TranscriptionProcessingProfile,
)
from spine.domain.common import DefinitionModel
from spine.domain.evaluations.private_asr_corpus import PrivateReference
from spine.domain.sources.original_reference import ObjectReference

if TYPE_CHECKING:
    from spine.infrastructure.asr_candidates import ResolvedAudioChunk


class StagedAudioReader(Protocol):
    def read(
        self, staged_ref: PrivateReference, *, context: TrustedPersistenceContext,
    ) -> AsyncIterator[bytes]: ...


class PrivateAudioSourceGuard(Protocol):
    async def current_and_exact(
        self,
        recording: AudioRecordingHandle,
        content: ContentVerificationResult,
        *, context: TrustedPersistenceContext,
    ) -> bool: ...


class EphemeralDecodedAudioStore(Protocol):
    async def put(self, payload: bytes, *, expected_sha256: str) -> ObjectReference: ...
    async def delete(self, reference: ObjectReference) -> None: ...


class LocalEphemeralDecodedAudioStore:
    """Private per-process temp store addressed only by opaque object refs."""

    def __init__(self, *, staging_parent: str | Path, maximum_staged_bytes: int) -> None:
        if maximum_staged_bytes <= 0:
            raise ValueError("ephemeral staging byte limit must be positive")
        parent = Path(staging_parent)
        if not parent.is_absolute() or parent.is_symlink():
            raise ValueError("ephemeral staging parent must be an absolute trusted directory")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("ephemeral staging parent must be private to its owner")
        self._root = Path(tempfile.mkdtemp(prefix="spine-private-asr-", dir=parent))
        os.chmod(self._root, 0o700)
        self._owned: dict[tuple[UUID, UUID], Path] = {}
        self._owned_bytes: dict[tuple[UUID, UUID], int] = {}
        self._maximum_staged_bytes = maximum_staged_bytes
        self._staged_bytes = 0

    async def put(self, payload: bytes, *, expected_sha256: str) -> ObjectReference:
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            raise ValueError("decoded audio digest mismatch")
        if self._staged_bytes + len(payload) > self._maximum_staged_bytes:
            raise PrivateEvaluationUnavailable("ephemeral staging disk limit exceeded")
        object_id, generation = uuid4(), uuid4()
        path = self._root / f"{object_id.hex}-{generation.hex}.pcm"
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self._owned[(object_id, generation)] = path
        self._owned_bytes[(object_id, generation)] = len(payload)
        self._staged_bytes += len(payload)
        return ObjectReference(
            schema_version=1,
            object_id=object_id,
            storage_generation=generation,
            digest_algorithm="sha256",
            digest_hex=digest,
            byte_length=len(payload),
        )

    async def delete(self, reference: ObjectReference) -> None:
        key = reference.object_id, reference.storage_generation
        path = self._owned.get(key)
        if path is not None:
            path.unlink(missing_ok=True)
            self._owned.pop(key, None)
            self._staged_bytes -= self._owned_bytes.pop(key, 0)

    def local_path_for(self, reference: ObjectReference) -> Path:
        """Resolve an owned opaque object ref for a colocated model adapter."""
        path = self._owned.get((reference.object_id, reference.storage_generation))
        if path is None or path.is_symlink() or not path.is_file():
            raise PrivateEvaluationUnavailable("ephemeral decoded object is unavailable")
        if path.stat().st_size != reference.byte_length or _sha256_path(path) != reference.digest_hex:
            raise PrivateEvaluationUnavailable("ephemeral decoded object verification failed")
        return path

    async def close(self) -> None:
        for path in tuple(self._owned.values()):
            path.unlink(missing_ok=True)
        self._owned.clear()
        self._owned_bytes.clear()
        self._staged_bytes = 0
        self._root.rmdir()


class EphemeralAudioChunkResolver:
    """Resolve runner chunk refs to verified local WAVs without accepting paths."""

    def __init__(self, store: LocalEphemeralDecodedAudioStore) -> None:
        self._store = store

    async def resolve(self, request: SpeechToTextRequestV2) -> "ResolvedAudioChunk":
        from spine.infrastructure.asr_candidates import ResolvedAudioChunk

        reference = request.chunk.decoded_reference
        path = self._store.local_path_for(reference)
        return ResolvedAudioChunk(
            path=str(path),
            sha256=request.chunk.decoded_sha256,
            byte_length=reference.byte_length,
            duration_ms=request.chunk.boundary.end_ms - request.chunk.boundary.start_ms,
        )


class AudioDecoderProcess(Protocol):
    async def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        source: bytes,
        *,
        timeout_seconds: float,
        memory_limit_bytes: int,
        output_limit_bytes: int,
    ) -> "DecoderProcessResult": ...


class DecoderProcessResult(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)
    stdout: bytes
    peak_memory_bytes: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)


class LinuxNoNetworkDecoderProcess:
    """Run a pinned media tool under a new network namespace and hard limits."""

    async def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        source: bytes,
        *,
        timeout_seconds: float,
        memory_limit_bytes: int,
        output_limit_bytes: int,
    ) -> DecoderProcessResult:
        if not Path(executable).is_absolute():
            raise ValueError("decoder executable must be an absolute pinned path")
        unshare = shutil.which("unshare")
        prlimit = shutil.which("prlimit")
        if unshare is None or prlimit is None:
            raise PrivateEvaluationUnavailable("isolated decoder launcher is unavailable")

        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                unshare, "--net", "--", prlimit,
                f"--as={memory_limit_bytes}",
                f"--cpu={max(1, int(timeout_seconds) + 1)}",
                "--core=0", "--", executable, *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise PrivateEvaluationUnavailable("isolated decoder could not start") from None
        assert process.stdin is not None and process.stdout is not None

        async def feed_input() -> None:
            try:
                view = memoryview(source)
                for offset in range(0, len(view), 64 * 1024):
                    process.stdin.write(view[offset : offset + 64 * 1024])
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        async def collect_output() -> bytes:
            chunks: list[bytes] = []
            size = 0
            while block := await process.stdout.read(64 * 1024):
                size += len(block)
                if size > output_limit_bytes:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    raise PrivateEvaluationUnavailable("decoder output limit exceeded")
                chunks.append(block)
            return b"".join(chunks)

        peak_memory = 0

        async def sample_memory() -> None:
            nonlocal peak_memory
            status_path = Path(f"/proc/{process.pid}/status")
            while process.returncode is None:
                try:
                    for line in status_path.read_text(encoding="ascii").splitlines():
                        if line.startswith("VmHWM:"):
                            peak_memory = max(peak_memory, int(line.split()[1]) * 1024)
                            break
                except (OSError, ValueError, IndexError):
                    pass
                await asyncio.sleep(0.01)

        tasks: list[asyncio.Task[object]] = []
        try:
            output_task = asyncio.create_task(collect_output())
            feed_task = asyncio.create_task(feed_input())
            memory_task = asyncio.create_task(sample_memory())
            tasks.extend((output_task, feed_task, memory_task))
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            await feed_task
            stdout = await output_task
            await memory_task
        except (TimeoutError, asyncio.TimeoutError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise PrivateEvaluationUnavailable("decoder wall-time limit exceeded") from None
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if process.returncode != 0:
            raise PrivateEvaluationUnavailable("decoder rejected the staged media")
        if peak_memory == 0 or peak_memory > memory_limit_bytes:
            raise PrivateEvaluationUnavailable("decoder memory observation is unavailable or over budget")
        return DecoderProcessResult(
            stdout=stdout,
            peak_memory_bytes=peak_memory,
            elapsed_seconds=time.monotonic() - started,
        )


class PinnedFFmpegPrivateAudioPreparation(GovernedPrivateAudioPreparationPort):
    """Inspect and decode only exact admitted bytes with a hash-pinned FFmpeg."""

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        ffprobe_path: str,
        ffmpeg_sha256: str,
        ffprobe_sha256: str,
        decoder_build_version: str,
        staged_audio: StagedAudioReader,
        source_guard: PrivateAudioSourceGuard,
        decoded_store: EphemeralDecodedAudioStore,
        process: AudioDecoderProcess | None = None,
    ) -> None:
        self._ffmpeg_path = _pinned_executable(ffmpeg_path, ffmpeg_sha256)
        self._ffprobe_path = _pinned_executable(ffprobe_path, ffprobe_sha256)
        self._ffmpeg_sha256 = ffmpeg_sha256
        self._ffprobe_sha256 = ffprobe_sha256
        if not decoder_build_version or len(decoder_build_version) > 128:
            raise ValueError("an exact decoder build version is required")
        self._decoder_build_version = decoder_build_version
        self._staged_audio = staged_audio
        self._source_guard = source_guard
        self._decoded_store = decoded_store
        self._process = process or LinuxNoNetworkDecoderProcess()
        self._prepared: dict[tuple[UUID, UUID], tuple[ObjectReference, ...]] = {}

    async def prepare(
        self,
        recording: AudioRecordingHandle,
        content: ContentVerificationResult,
        profile: TranscriptionProcessingProfile,
        *, context: TrustedPersistenceContext,
    ) -> AudioPreparationResult:
        if not isinstance(content.staged_ref, PrivateReference):
            raise PrivateEvaluationUnavailable("only verified opaque source references are accepted")
        identity = self._identity(recording, content)
        if identity in self._prepared:
            raise PrivateEvaluationUnavailable("audio preparation is already active")
        if profile.decoder_reference != "ffmpeg-mono-16khz-pcm16:v1":
            raise PrivateEvaluationUnavailable("processing profile selects another decoder")
        if (
            recording.original_sha256 != content.digest_verification.audio_sha256
            or content.digest_verification.byte_length != content.byte_length
            or recording.original_reference.digest_hex != content.digest_verification.audio_sha256
            or recording.original_reference.byte_length != content.byte_length
            or content.admitted.source_revision.revision_id != recording.source_revision_id
            or content.admitted.source_revision.source_object_id != recording.source_object_id
            or content.admitted.source_revision.workspace_id != recording.workspace_id
            or content.admitted.source_revision.environment_id != recording.environment_id
            or content.admitted.source_revision.revision_digest != recording.source_revision_digest
            or content.admitted.source_revision.original_sha256 != recording.original_sha256
            or content.admitted.source_revision.byte_length != content.byte_length
            or content.admitted.workspace_id != recording.workspace_id
            or content.admitted.environment_id != recording.environment_id
            or content.digest_verification.source_revision_id != recording.source_revision_id
            or not content.staged_ref.reference_id.int
        ):
            raise PrivateEvaluationUnavailable("verified staged source identity does not match")
        _verify_binary_digest(self._ffmpeg_path, self._ffmpeg_sha256)
        _verify_binary_digest(self._ffprobe_path, self._ffprobe_sha256)
        await self._authorize(recording, content, context)
        source = await self._read_verified_source(recording, content, context, profile)
        await self._authorize(recording, content, context)

        probe, probe_memory = await self._run_probe(source, profile)
        if probe_memory > profile.maximum_memory_bytes:
            raise PrivateEvaluationUnavailable("media inspection exceeded the memory limit")
        inspection = self._inspect(probe, recording, len(source))
        boundaries = profile.plan_chunks(inspection)
        if any(item.end_ms - item.start_ms > 25_000 for item in boundaries):
            raise PrivateEvaluationUnavailable("audio chunk exceeds the 25-second model bound")
        await self._authorize(recording, content, context)
        decoded_result = await self._run_decode(
            source, profile, duration_ms=inspection.duration_ms,
        )
        await self._authorize(recording, content, context)
        decoded = decoded_result.stdout
        if decoded_result.peak_memory_bytes > profile.maximum_memory_bytes:
            raise PrivateEvaluationUnavailable("decoder exceeded the memory limit")
        if not decoded:
            raise PrivateEvaluationUnavailable("decoder produced no PCM audio")
        expected_bytes = inspection.duration_ms * 32
        if len(decoded) != expected_bytes or len(decoded) > profile.maximum_decoded_bytes:
            raise PrivateEvaluationUnavailable("decoded audio length is outside the profile bounds")
        inspection = inspection.model_copy(update={"silent": not any(decoded)})
        profile.plan_chunks(inspection)
        decoded_sha256 = hashlib.sha256(decoded).hexdigest()
        chunk_sizes = tuple(44 + (item.end_ms - item.start_ms) * 32 for item in boundaries)
        staged_bytes = len(decoded) + sum(chunk_sizes)
        working_bytes = max(
            len(source) * 2,
            len(source) + len(decoded) + 2 * max(chunk_sizes),
        )
        if (
            working_bytes > profile.maximum_working_bytes
            or staged_bytes > profile.maximum_working_bytes
        ):
            raise PrivateEvaluationUnavailable("audio working-set limit exceeded")
        output_references: list[ObjectReference] = []
        try:
            output_reference = await self._decoded_store.put(decoded, expected_sha256=decoded_sha256)
            output_references.append(output_reference)
            chunks: list[AudioPreparedChunk] = []
            for boundary, expected_size in zip(boundaries, chunk_sizes, strict=True):
                payload = _wav_chunk(decoded, boundary.start_ms, boundary.end_ms)
                if len(payload) != expected_size:
                    raise PrivateEvaluationUnavailable("chunk container size changed")
                chunk_digest = hashlib.sha256(payload).hexdigest()
                chunk_reference = await self._decoded_store.put(
                    payload, expected_sha256=chunk_digest
                )
                output_references.append(chunk_reference)
                chunks.append(AudioPreparedChunk(
                    boundary=boundary, decoded_reference=chunk_reference,
                ))
            await self._authorize(recording, content, context)
            result = AudioPreparationResult(
                inspection=inspection,
                decoder_reference=profile.decoder_reference,
                decoder_build_version=self._decoder_build_version,
                decoder_build_sha256=self._ffmpeg_sha256,
                original_sha256=recording.original_sha256,
                decoded_reference=output_reference,
                decoded_sha256=decoded_sha256,
                decoded_byte_length=len(decoded),
                peak_memory_bytes=max(probe_memory, decoded_result.peak_memory_bytes),
                peak_working_bytes=working_bytes,
                chunks=tuple(chunks),
            )
            self._prepared[identity] = tuple(output_references)
            return result
        except BaseException:
            for reference in reversed(output_references):
                await self._decoded_store.delete(reference)
            raise

    async def release(
        self, prepared: AudioPreparationResult, *, context: TrustedPersistenceContext,
    ) -> None:
        del context
        await self._delete_prepared(prepared.decoded_reference)

    async def abort(
        self,
        recording: AudioRecordingHandle,
        content: ContentVerificationResult,
        profile: TranscriptionProcessingProfile,
        *, context: TrustedPersistenceContext,
    ) -> None:
        del profile, context
        identity = self._identity(recording, content)
        references = self._prepared.get(identity)
        if references is not None:
            for reference in reversed(references):
                await self._decoded_store.delete(reference)
            self._prepared.pop(identity, None)

    async def _delete_prepared(self, reference: ObjectReference) -> None:
        for identity, owned in tuple(self._prepared.items()):
            if reference in owned:
                for item in reversed(owned):
                    await self._decoded_store.delete(item)
                self._prepared.pop(identity, None)
                break

    async def _read_verified_source(
        self, recording: AudioRecordingHandle, content: ContentVerificationResult,
        context: TrustedPersistenceContext, profile: TranscriptionProcessingProfile,
    ) -> bytes:
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        working_source_limit = (
            profile.maximum_working_bytes - profile.maximum_decoded_bytes
        ) // 2
        source_limit = min(profile.maximum_source_bytes, working_source_limit)
        if source_limit <= 0:
            raise PrivateEvaluationUnavailable("audio working-set budget is insufficient")
        try:
            async for chunk in self._staged_audio.read(content.staged_ref, context=context):
                if not isinstance(chunk, bytes) or not chunk:
                    raise ValueError("invalid staged content block")
                total += len(chunk)
                if total > source_limit:
                    raise ValueError("source byte limit exceeded")
                digest.update(chunk)
                chunks.append(chunk)
        except Exception:
            raise PrivateEvaluationUnavailable("verified staged content is unavailable") from None
        if total != content.byte_length or total != recording.original_reference.byte_length:
            raise PrivateEvaluationUnavailable("verified source length changed")
        if digest.hexdigest() != recording.original_sha256:
            raise PrivateEvaluationUnavailable("verified source digest changed")
        return b"".join(chunks)

    async def _run_probe(
        self, source: bytes, profile: TranscriptionProcessingProfile,
    ) -> tuple[dict[str, object], int]:
        result = await self._process.run(
            self._ffprobe_path,
            ("-v", "error", "-protocol_whitelist", "pipe", "-show_format", "-show_streams",
             "-of", "json", "pipe:0"),
            source,
            timeout_seconds=min(profile.maximum_decode_seconds, 10.0),
            memory_limit_bytes=profile.maximum_memory_bytes,
            output_limit_bytes=256_000,
        )
        if result.elapsed_seconds > min(profile.maximum_decode_seconds, 10.0):
            raise PrivateEvaluationUnavailable("media inspection exceeded the time limit")
        if len(result.stdout) > 256_000:
            raise PrivateEvaluationUnavailable("media inspection output limit exceeded")
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise PrivateEvaluationUnavailable("media inspection failed") from None
        if not isinstance(payload, dict):
            raise PrivateEvaluationUnavailable("media inspection failed")
        return payload, result.peak_memory_bytes

    async def _run_decode(
        self,
        source: bytes,
        profile: TranscriptionProcessingProfile,
        *,
        duration_ms: int,
    ) -> DecoderProcessResult:
        result = await self._process.run(
            self._ffmpeg_path,
            ("-v", "error", "-nostdin", "-fflags", "+bitexact", "-protocol_whitelist", "pipe",
             "-i", "pipe:0", "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
             "-threads", "1", "-ac", "1", "-ar", "16000", "-af",
             f"aresample=dither_method=none,atrim=end_sample={duration_ms * 16}",
             "-flags:a", "+bitexact", "-f", "s16le", "pipe:1"),
            source,
            timeout_seconds=profile.maximum_decode_seconds,
            memory_limit_bytes=profile.maximum_memory_bytes,
            output_limit_bytes=profile.maximum_decoded_bytes,
        )
        if result.elapsed_seconds > profile.maximum_decode_seconds:
            raise PrivateEvaluationUnavailable("decoder exceeded the time limit")
        if len(result.stdout) > profile.maximum_decoded_bytes:
            raise PrivateEvaluationUnavailable("decoder output limit exceeded")
        return result

    @staticmethod
    def _inspect(
        payload: dict[str, object], recording: AudioRecordingHandle,
        source_length: int,
    ) -> AudioMediaInspection:
        streams = payload.get("streams")
        fmt = payload.get("format")
        if not isinstance(streams, list) or not isinstance(fmt, dict):
            raise PrivateEvaluationUnavailable("media inspection is incomplete")
        audio = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
        if len(audio) != 1:
            raise PrivateEvaluationUnavailable("exactly one audio stream is required")
        stream = audio[0]
        codec = stream.get("codec_name")
        format_name = fmt.get("format_name")
        if not isinstance(codec, str) or not isinstance(format_name, str):
            raise PrivateEvaluationUnavailable("media format is unsupported")
        if format_name == "wav" and codec in {"pcm_s16le", "pcm_s24le"}:
            if stream.get("bits_per_sample") not in (16, 24) and stream.get("bits_per_raw_sample") not in ("16", "24"):
                raise PrivateEvaluationUnavailable("WAV sample depth is unsupported")
            container, media_type = "wav", "audio/wav"
        elif format_name == "mp3" and codec == "mp3":
            container, media_type = "mp3", "audio/mpeg"
        elif set(format_name.split(",")) & {"mov", "mp4", "m4a"} and codec == "aac" and stream.get("profile") == "LC":
            container, media_type = "m4a", "audio/mp4"
        else:
            raise PrivateEvaluationUnavailable("media codec is not allowlisted")
        duration = fmt.get("duration") or stream.get("duration")
        try:
            duration_ms = round(float(duration) * 1000)
        except (TypeError, ValueError, OverflowError):
            raise PrivateEvaluationUnavailable("media duration is unavailable") from None
        if media_type != recording.media_type or duration_ms != recording.recording_duration_ms:
            raise PrivateEvaluationUnavailable("inspected media differs from the admitted metadata")
        return AudioMediaInspection(
            inspected_media_type=media_type,
            container=container,
            codec=codec,
            byte_length=source_length,
            duration_ms=duration_ms,
            audio_stream_count=1,
            silent=False,
        )

    async def _authorize(
        self, recording: AudioRecordingHandle, content: ContentVerificationResult,
        context: TrustedPersistenceContext,
    ) -> None:
        # Do not make authorization optional or defer it to the runner. It must
        # bracket staged reads and decoder access in this adapter itself.
        try:
            allowed = await self._source_guard.current_and_exact(
                recording, content, context=context
            )
        except Exception:
            raise PrivateEvaluationUnavailable("audio source authorization is unavailable") from None
        if not allowed:
            raise PrivateEvaluationUnavailable("audio source is no longer authorized")

    @staticmethod
    def _identity(
        recording: AudioRecordingHandle, content: ContentVerificationResult,
    ) -> tuple[UUID, UUID]:
        return recording.source_revision_id, content.staged_ref.reference_id


def _pinned_executable(path: str, expected_sha256: str) -> str:
    if not Path(path).is_absolute() or len(expected_sha256) != 64:
        raise ValueError("pinned decoder paths and SHA-256 values are required")
    path = str(Path(path).resolve(strict=True))
    _verify_binary_digest(path, expected_sha256)
    return path


def _wav_chunk(decoded_pcm: bytes, start_ms: int, end_ms: int) -> bytes:
    start_frame, end_frame = start_ms * 16, end_ms * 16
    start_byte, end_byte = start_frame * 2, end_frame * 2
    if start_byte < 0 or end_byte > len(decoded_pcm) or start_byte >= end_byte:
        raise PrivateEvaluationUnavailable("decoded chunk boundaries exceed verified PCM")
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(decoded_pcm[start_byte:end_byte])
    return output.getvalue()


def _verify_binary_digest(path: str, expected_sha256: str) -> None:
    try:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ValueError
        digest = hashlib.sha256()
        with open(path, "rb") as executable:
            for chunk in iter(lambda: executable.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError):
        raise ValueError("pinned decoder executable is unavailable") from None
    if digest.hexdigest() != expected_sha256:
        raise ValueError("pinned decoder build digest does not match")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as content:
        for chunk in iter(lambda: content.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "AudioDecoderProcess", "DecoderProcessResult", "EphemeralDecodedAudioStore",
    "EphemeralAudioChunkResolver", "LinuxNoNetworkDecoderProcess",
    "PinnedFFmpegPrivateAudioPreparation", "LocalEphemeralDecodedAudioStore",
    "PrivateAudioSourceGuard", "StagedAudioReader",
]
