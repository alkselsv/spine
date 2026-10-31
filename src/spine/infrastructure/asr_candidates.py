"""Offline local adapters for the two frozen ASR qualification candidates.

Candidate dependencies are imported only after the operator manifest, asset
inventory, dependency lock, and no-egress attestation pass preflight.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
from pathlib import Path, PurePosixPath
import subprocess
import re
from typing import Literal, Protocol, cast

from pydantic import Field, StrictBool, StrictInt, model_validator

from spine.application.transcription import (
    SpeechToTextCancelled,
    SpeechToTextFailed,
    SpeechToTextPort,
)
from spine.domain.artifacts.transcription import (
    AdapterSignal,
    SpeechToTextRequest,
    SpeechToTextRequestV2,
    SpeechToTextResultV2,
    SpeechToTextSegment,
    TranscriptionFailureCode,
)
from spine.domain.common import DefinitionModel, VersionIdentifier


_SHA256 = r"^[0-9a-f]{64}$"
_GIT_REVISION = r"^[0-9a-f]{40}$"


class AssetDigest(DefinitionModel):
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=_SHA256)
    byte_length: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def path_is_relative_and_safe(self) -> "AssetDigest":
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.path:
            raise ValueError("asset inventory paths must be safe relative paths")
        return self


class NoEgressEvidence(DefinitionModel):
    schema_version: Literal["asr-no-egress-attestation:v1"]
    boundary_reference: VersionIdentifier
    enforcement: Literal["network_namespace", "firewall_policy", "isolated_vm"]
    network_egress_disabled: StrictBool
    network_namespace_inode: StrictInt | None = Field(default=None, gt=0)
    default_route_absent: StrictBool = False
    non_loopback_interfaces_absent: StrictBool = False

    @model_validator(mode="after")
    def namespace_evidence_is_complete(self) -> "NoEgressEvidence":
        if self.enforcement == "network_namespace" and (
            self.network_namespace_inode is None
            or not self.default_route_absent
            or not self.non_loopback_interfaces_absent
        ):
            raise ValueError("network namespace evidence must record namespace inode, no default route and no external interfaces")
        return self


class CandidateHostEvidence(DefinitionModel):
    cpu: str = Field(min_length=1, max_length=256)
    gpu: str | None = Field(default=None, max_length=256)
    ram_mb: StrictInt = Field(ge=0)
    vram_mb: StrictInt | None = Field(default=None, ge=0)
    operating_system: str = Field(min_length=1, max_length=256)
    python_version: str = Field(min_length=1, max_length=32)
    production_capacity_claim: Literal[False] = False


class CandidateDependencyPin(DefinitionModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    version: str = Field(min_length=1, max_length=128)
    installed_tree_sha256: str = Field(pattern=_SHA256)


class CandidateDependencyLock(DefinitionModel):
    schema_version: Literal["asr-candidate-dependency-lock:v1"]
    candidate_id: Literal["gigaam-v3-rnnt", "whisper-large-v3-faster-whisper"]
    python_version: str = Field(min_length=1, max_length=32)
    packages: tuple[CandidateDependencyPin, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def package_inventory_is_complete_and_unique(self) -> "CandidateDependencyLock":
        names = [_normalize_distribution_name(item.name) for item in self.packages]
        if len(names) != len(set(names)):
            raise ValueError("dependency lock package names must be unique")
        required = {"gigaam"} if self.candidate_id == "gigaam-v3-rnnt" else {"faster-whisper", "ctranslate2"}
        if not required.issubset(names):
            raise ValueError("dependency lock omits a required candidate runtime package")
        return self


class CandidateRuntimeManifest(DefinitionModel):
    """Operator supplied identity for a single, immutable candidate run."""

    schema_version: Literal["asr-candidate-runtime:v1"]
    candidate_id: Literal["gigaam-v3-rnnt", "whisper-large-v3-faster-whisper"]
    adapter_reference: VersionIdentifier
    model_config_reference: VersionIdentifier
    source_repository: Literal[
        "https://github.com/salute-developers/GigaAM",
        "https://github.com/SYSTRAN/faster-whisper",
    ]
    source_checkout_path: str = Field(min_length=1)
    source_revision: str = Field(pattern=_GIT_REVISION)
    runtime_checkout_path: str = Field(min_length=1)
    runtime_reference: VersionIdentifier
    runtime_revision: str = Field(pattern=_GIT_REVISION)
    dependency_lock_path: str = Field(min_length=1)
    dependency_lock_sha256: str = Field(pattern=_SHA256)
    preprocessing_executable: str = Field(min_length=1)
    preprocessing_executable_sha256: str = Field(pattern=_SHA256)
    preprocessing_profile_reference: VersionIdentifier
    asset_root: str = Field(min_length=1)
    checkpoint_path: str = Field(min_length=1)
    asset_inventory: tuple[AssetDigest, ...] = Field(min_length=1)
    checkpoint_sha256: str = Field(pattern=_SHA256)
    precision: VersionIdentifier
    device: Literal["cpu", "cuda"]
    language: Literal["ru"] = "ru"
    translation_enabled: Literal[False] = False
    language_detection_enabled: Literal[False] = False
    beam_size: StrictInt = Field(default=5, gt=0, le=10)
    no_egress_evidence_path: str = Field(min_length=1)
    no_egress_evidence_sha256: str = Field(pattern=_SHA256)
    no_egress: NoEgressEvidence
    host: CandidateHostEvidence
    license_review_reference: VersionIdentifier
    license_commercial_use_approved: StrictBool | None = None
    chunk_duration_limit_ms: StrictInt = Field(default=25_000, gt=0, le=25_000)
    dataset_id: VersionIdentifier
    dataset_version: VersionIdentifier
    dataset_sha256: str = Field(pattern=_SHA256)
    configuration_digest: str = Field(pattern=_SHA256)
    tuning_split: Literal["development"] = "development"
    qualification_configuration_frozen: StrictBool = True

    @model_validator(mode="after")
    def frozen_candidate_identity(self) -> "CandidateRuntimeManifest":
        is_gigaam = self.candidate_id == "gigaam-v3-rnnt"
        expected = (
            "https://github.com/salute-developers/GigaAM"
            if is_gigaam
            else "https://github.com/SYSTRAN/faster-whisper"
        )
        if self.source_repository != expected:
            raise ValueError("candidate source repository does not match frozen identity")
        expected_runtime = "official-gigaam-runtime:v1" if is_gigaam else "faster-whisper-ctranslate2:v1"
        if self.runtime_reference != expected_runtime:
            raise ValueError("candidate runtime reference does not match frozen identity")
        if is_gigaam:
            if self.precision not in {"fp16_encoder", "fp32_encoder"}:
                raise ValueError("GigaAM precision must explicitly select fp16_encoder or fp32_encoder")
            if self.device == "cpu" and self.precision == "fp16_encoder":
                raise ValueError("GigaAM CPU inference cannot silently downgrade an fp16 encoder profile")
        elif self.precision not in {"float16", "float32", "int8", "int8_float16"}:
            raise ValueError("Whisper compute_type must explicitly select a supported precision")
        if not self.no_egress.network_egress_disabled:
            raise ValueError("candidate inference requires an affirmative no-egress attestation")
        if not self.qualification_configuration_frozen:
            raise ValueError("qualification configuration must be frozen before model access")
        paths = [asset.path for asset in self.asset_inventory]
        if len(paths) != len(set(paths)):
            raise ValueError("asset inventory paths must be unique")
        return self


class ResolvedAudioChunk(DefinitionModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)
    byte_length: StrictInt = Field(gt=0)
    sample_rate_hz: Literal[16000] = 16000
    channels: Literal[1] = 1
    duration_ms: StrictInt = Field(gt=0)


class CandidateAudioResolver(Protocol):
    async def resolve(self, request: SpeechToTextRequestV2) -> ResolvedAudioChunk: ...


class RuntimeBackend(Protocol):
    def load(self, manifest: CandidateRuntimeManifest, asset_root: Path) -> object: ...

    def transcribe(self, model: object, audio_path: Path, manifest: CandidateRuntimeManifest) -> object: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as content:
        for block in iter(lambda: content.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_runtime_manifest(
    manifest: CandidateRuntimeManifest,
    *,
    verify_runtime_import_location: bool = False,
) -> tuple[str, ...]:
    """Verify every staged byte and attestation before any optional import."""

    problems: list[str] = []
    if canonical_configuration_digest(manifest) != manifest.configuration_digest:
        problems.append("configuration_digest_mismatch")
    if not _checkout_matches(manifest.source_checkout_path, manifest.source_repository, manifest.source_revision):
        problems.append("candidate_source_checkout_revision_mismatch")
    if not _checkout_matches(manifest.runtime_checkout_path, manifest.source_repository, manifest.runtime_revision):
        problems.append("candidate_runtime_checkout_revision_mismatch")
    if verify_runtime_import_location and not _runtime_module_is_from_checkout(manifest):
        problems.append("candidate_runtime_module_not_from_pinned_checkout")
    if manifest.license_commercial_use_approved is not True:
        problems.append("license_review_not_approved")
    lock_path = Path(manifest.dependency_lock_path)
    if lock_path.is_symlink() or not lock_path.is_file() or sha256_file(lock_path) != manifest.dependency_lock_sha256:
        problems.append("dependency_lock_missing_or_digest_mismatch")
    else:
        try:
            lock = CandidateDependencyLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
        except Exception:
            problems.append("dependency_lock_invalid")
        else:
            if lock.candidate_id != manifest.candidate_id:
                problems.append("dependency_lock_candidate_mismatch")
            if lock.python_version != manifest.host.python_version:
                problems.append("dependency_lock_python_version_mismatch")
            if verify_runtime_import_location:
                problems.extend(_installed_dependency_mismatches(lock))
    preprocessing_executable = Path(manifest.preprocessing_executable)
    if (
        preprocessing_executable.is_symlink()
        or not preprocessing_executable.is_file()
        or sha256_file(preprocessing_executable) != manifest.preprocessing_executable_sha256
    ):
        problems.append("preprocessing_executable_missing_or_digest_mismatch")
    asset_root = Path(manifest.asset_root)
    if not asset_root.is_dir() or asset_root.is_symlink():
        problems.append("asset_root_missing_or_unsafe")
        return tuple(problems)
    expected: dict[str, AssetDigest] = {asset.path: asset for asset in manifest.asset_inventory}
    observed: set[str] = set()
    for path in asset_root.rglob("*"):
        if path.is_symlink():
            problems.append("asset_symlink_forbidden")
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(asset_root).as_posix()
        observed.add(relative)
        asset = expected.get(relative)
        if asset is None:
            problems.append("unlisted_asset_present")
            continue
        if path.stat().st_size != asset.byte_length or sha256_file(path) != asset.sha256:
            problems.append(f"asset_digest_mismatch:{relative}")
    if observed != set(expected):
        problems.append("asset_inventory_incomplete")
    checkpoint = Path(manifest.checkpoint_path)
    try:
        relative_checkpoint = checkpoint.resolve(strict=True).relative_to(asset_root.resolve(strict=True)).as_posix()
    except (OSError, ValueError):
        problems.append("checkpoint_missing_or_outside_asset_root")
    else:
        checkpoint_digest = expected.get(relative_checkpoint)
        if checkpoint_digest is None or checkpoint_digest.sha256 != manifest.checkpoint_sha256:
            problems.append("checkpoint_not_bound_to_asset_inventory")
    evidence_path = Path(manifest.no_egress_evidence_path)
    if evidence_path.is_symlink() or not evidence_path.is_file() or sha256_file(evidence_path) != manifest.no_egress_evidence_sha256:
        problems.append("no_egress_evidence_missing_or_digest_mismatch")
    else:
        try:
            evidence = NoEgressEvidence.model_validate_json(evidence_path.read_text(encoding="utf-8"))
        except Exception:
            problems.append("no_egress_evidence_invalid")
        else:
            if evidence != manifest.no_egress:
                problems.append("no_egress_evidence_mismatch")
    return tuple(dict.fromkeys(problems))


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed_distribution_digest(distribution: importlib.metadata.Distribution) -> str | None:
    files = distribution.files
    if files is None:
        return None
    inventory: list[tuple[str, str, int]] = []
    for item in sorted(files, key=str):
        path = Path(distribution.locate_file(item))
        if path.is_symlink() or not path.is_file():
            return None
        try:
            inventory.append((str(item), sha256_file(path), path.stat().st_size))
        except OSError:
            return None
    if not inventory:
        return None
    payload = json.dumps(inventory, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _installed_dependency_mismatches(lock: CandidateDependencyLock) -> list[str]:
    installed: dict[str, importlib.metadata.Distribution] = {}
    try:
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata.get("Name")
            if name:
                installed[_normalize_distribution_name(name)] = distribution
    except (OSError, importlib.metadata.PackageNotFoundError):
        return ["installed_dependency_inventory_unavailable"]
    locked = {_normalize_distribution_name(item.name): item for item in lock.packages}
    problems = ["installed_dependency_set_mismatch"] if set(installed) != set(locked) else []
    for name, pin in locked.items():
        distribution = installed.get(name)
        if distribution is None:
            problems.append(f"installed_dependency_missing:{name}")
        elif distribution.version != pin.version:
            problems.append(f"installed_dependency_version_mismatch:{name}")
        elif _installed_distribution_digest(distribution) != pin.installed_tree_sha256:
            problems.append(f"installed_dependency_digest_mismatch:{name}")
    return problems


def _checkout_matches(path_value: str, repository: str, revision: str) -> bool:
    """Verify a staged local checkout's origin and HEAD without network access."""

    path = Path(path_value)
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        remote = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        remote.rstrip("/").removesuffix(".git") == repository.rstrip("/").removesuffix(".git")
        and head == revision
        and not dirty
    )


def _runtime_module_is_from_checkout(manifest: CandidateRuntimeManifest) -> bool:
    module_name = "gigaam" if manifest.candidate_id == "gigaam-v3-rnnt" else "faster_whisper"
    try:
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            return False
        origin = Path(spec.origin).resolve(strict=True)
        checkout = Path(manifest.runtime_checkout_path).resolve(strict=True)
        relative_origin = origin.relative_to(checkout).as_posix()
        subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", relative_origin],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (ImportError, OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def canonical_configuration_digest(manifest: CandidateRuntimeManifest) -> str:
    """Hash the frozen behavior and provenance fields without private paths."""

    payload = {
        "schema_version": manifest.schema_version,
        "candidate_id": manifest.candidate_id,
        "adapter_reference": manifest.adapter_reference,
        "model_config_reference": manifest.model_config_reference,
        "source_repository": manifest.source_repository,
        "source_revision": manifest.source_revision,
        "runtime_reference": manifest.runtime_reference,
        "runtime_revision": manifest.runtime_revision,
        "dependency_lock_sha256": manifest.dependency_lock_sha256,
        "preprocessing_executable_sha256": manifest.preprocessing_executable_sha256,
        "preprocessing_profile_reference": manifest.preprocessing_profile_reference,
        "asset_inventory": [item.model_dump(mode="json") for item in manifest.asset_inventory],
        "checkpoint_sha256": manifest.checkpoint_sha256,
        "precision": manifest.precision,
        "device": manifest.device,
        "language": manifest.language,
        "translation_enabled": manifest.translation_enabled,
        "language_detection_enabled": manifest.language_detection_enabled,
        "beam_size": manifest.beam_size,
        "no_egress_evidence_sha256": manifest.no_egress_evidence_sha256,
        "no_egress": manifest.no_egress.model_dump(mode="json"),
        "license_review_reference": manifest.license_review_reference,
        "license_commercial_use_approved": manifest.license_commercial_use_approved,
        "chunk_duration_limit_ms": manifest.chunk_duration_limit_ms,
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "dataset_sha256": manifest.dataset_sha256,
        "tuning_split": manifest.tuning_split,
        "qualification_configuration_frozen": manifest.qualification_configuration_frozen,
        "host_profile": manifest.host.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_request(request: SpeechToTextRequest | SpeechToTextRequestV2, manifest: CandidateRuntimeManifest) -> SpeechToTextRequestV2:
    if not isinstance(request, SpeechToTextRequestV2):
        raise SpeechToTextFailed(code=TranscriptionFailureCode.UNSUPPORTED_MEDIA, retryable=False)
    if request.adapter_reference != manifest.adapter_reference or request.model_config_reference != manifest.model_config_reference:
        raise SpeechToTextFailed(code=TranscriptionFailureCode.POLICY_DENIED, retryable=False)
    if request.language_hint not in (None, "ru", "ru-RU"):
        raise SpeechToTextFailed(code=TranscriptionFailureCode.POLICY_DENIED, retryable=False)
    if request.chunk.boundary.end_ms - request.chunk.boundary.start_ms > manifest.chunk_duration_limit_ms:
        raise SpeechToTextFailed(code=TranscriptionFailureCode.RESOURCE_LIMIT_EXCEEDED, retryable=False)
    return request


def _validate_resolved_audio(audio: ResolvedAudioChunk, request: SpeechToTextRequestV2) -> Path:
    path = Path(audio.path)
    if not path.is_file() or path.is_symlink():
        raise SpeechToTextFailed(code=TranscriptionFailureCode.UNSUPPORTED_MEDIA, retryable=False)
    if (
        audio.sha256 != request.chunk.decoded_sha256
        or audio.sha256 != request.chunk.decoded_reference.digest_hex
        or path.stat().st_size != audio.byte_length
        or sha256_file(path) != audio.sha256
    ):
        raise SpeechToTextFailed(code=TranscriptionFailureCode.CORRUPT_MEDIA, retryable=False)
    try:
        import wave

        with wave.open(str(path), "rb") as source:
            metadata = source.getnchannels(), source.getframerate(), round(source.getnframes() * 1000 / source.getframerate())
    except (OSError, wave.Error):
        raise SpeechToTextFailed(code=TranscriptionFailureCode.CORRUPT_MEDIA, retryable=False) from None
    if metadata != (audio.channels, audio.sample_rate_hz, audio.duration_ms):
        raise SpeechToTextFailed(code=TranscriptionFailureCode.CORRUPT_MEDIA, retryable=False)
    if audio.duration_ms > 25_000:
        raise SpeechToTextFailed(code=TranscriptionFailureCode.RESOURCE_LIMIT_EXCEEDED, retryable=False)
    return path


def _seconds_to_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("runtime timestamp must be numeric")
    return round(value * 1000)


class _LocalAdapter(SpeechToTextPort):
    candidate_id: str

    def __init__(
        self,
        manifest: CandidateRuntimeManifest,
        audio_resolver: CandidateAudioResolver,
        *,
        backend: RuntimeBackend | None = None,
    ) -> None:
        problems = validate_runtime_manifest(
            manifest,
            verify_runtime_import_location=backend is None,
        )
        if problems:
            raise ValueError("candidate preflight failed: " + ",".join(problems))
        if manifest.candidate_id != self.candidate_id:
            raise ValueError("runtime manifest candidate identity mismatch")
        self.manifest = manifest
        self.audio_resolver = audio_resolver
        self.backend = backend or self._load_local_backend()
        self.model = self.backend.load(manifest, Path(manifest.asset_root))
        self._inference_lock = asyncio.Lock()

    def _load_local_backend(self) -> RuntimeBackend:
        raise NotImplementedError

    async def transcribe(self, request: SpeechToTextRequest | SpeechToTextRequestV2) -> SpeechToTextResultV2:
        typed_request = _validate_request(request, self.manifest)
        resolved = await self.audio_resolver.resolve(typed_request)
        audio_path = _validate_resolved_audio(resolved, typed_request)
        inference: asyncio.Future[object] | None = None
        runtime_future: ConcurrentFuture[object] | None = None
        inference_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"spine-asr-{self.candidate_id}",
        )
        try:
            async with self._inference_lock:
                runtime_future = inference_executor.submit(
                    self.backend.transcribe,
                    self.model,
                    audio_path,
                    self.manifest,
                )
                inference = asyncio.wrap_future(runtime_future)
                raw = await asyncio.shield(inference)
            segments = self._map_segments(raw, resolved.duration_ms)
        except asyncio.CancelledError as error:
            # Runtime calls are synchronous and cannot be interrupted safely. Do not
            # report cancellation while worker-thread inference is still running.
            # Poll the concurrent future directly: the asyncio wrapper was shielded
            # during cancellation, and awaiting it again can strand loop shutdown on
            # Python's default-executor wakeup path.
            while runtime_future is not None and not runtime_future.done():
                await asyncio.sleep(0.01)
            if runtime_future is not None:
                try:
                    runtime_future.result()
                except BaseException:
                    pass
            raise SpeechToTextCancelled() from error
        except SpeechToTextFailed:
            raise
        except Exception:
            raise SpeechToTextFailed(code=TranscriptionFailureCode.PERMANENT_MODEL_ERROR, retryable=False) from None
        finally:
            # Own the synchronous runtime worker and join it before returning. This
            # keeps cancellation and loop shutdown from inheriting an ambient pool.
            inference_executor.shutdown(wait=True)
        return SpeechToTextResultV2(
            adapter_reference=self.manifest.adapter_reference,
            model_config_reference=self.manifest.model_config_reference,
            detected_language="ru",
            timeline="chunk",
            segments=segments,
        )

    def _map_segments(self, raw: object, duration_ms: int) -> tuple[SpeechToTextSegment, ...]:
        raise NotImplementedError


class GigaAMV3RNNTAdapter(_LocalAdapter):
    candidate_id = "gigaam-v3-rnnt"

    def _load_local_backend(self) -> RuntimeBackend:
        return _GigaAMRuntime()

    def _map_segments(self, raw: object, duration_ms: int) -> tuple[SpeechToTextSegment, ...]:
        text = getattr(raw, "text", None)
        words = getattr(raw, "words", None)
        if not isinstance(text, str):
            raise ValueError("GigaAM result lacks transcript text")
        if words:
            segments = tuple(
                SpeechToTextSegment(
                    start_ms=_seconds_to_ms(getattr(word, "start", None)),
                    end_ms=_seconds_to_ms(getattr(word, "end", None)),
                    text=str(getattr(word, "text", "")).strip(),
                )
                for word in words
                if str(getattr(word, "text", "")).strip()
            )
            return _check_segment_bounds(segments, duration_ms)
        if not text.strip():
            return ()
        return (
            SpeechToTextSegment(
                start_ms=0,
                end_ms=duration_ms,
                text=text.strip(),
                signals=(AdapterSignal(name="timestamp_source", value="chunk_window"),),
            ),
        )


class WhisperLargeV3Adapter(_LocalAdapter):
    candidate_id = "whisper-large-v3-faster-whisper"

    def _load_local_backend(self) -> RuntimeBackend:
        return _FasterWhisperRuntime()

    def _map_segments(self, raw: object, duration_ms: int) -> tuple[SpeechToTextSegment, ...]:
        segments = []
        for item in cast(tuple[object, ...], raw):
            text = getattr(item, "text", None)
            if not isinstance(text, str) or not text.strip():
                continue
            segments.append(
                SpeechToTextSegment(
                    start_ms=_seconds_to_ms(getattr(item, "start", None)),
                    end_ms=_seconds_to_ms(getattr(item, "end", None)),
                    text=text.strip(),
                )
            )
        return _check_segment_bounds(tuple(segments), duration_ms)


def _check_segment_bounds(segments: tuple[SpeechToTextSegment, ...], duration_ms: int) -> tuple[SpeechToTextSegment, ...]:
    previous_start = -1
    for segment in segments:
        if segment.end_ms > duration_ms or segment.start_ms < previous_start:
            raise ValueError("runtime timestamps exceed or reorder the input chunk")
        previous_start = segment.start_ms
    return segments


class _GigaAMRuntime:
    def load(self, manifest: CandidateRuntimeManifest, asset_root: Path) -> object:
        gigaam = importlib.import_module("gigaam")
        return gigaam.load_model(
            str(Path(manifest.checkpoint_path)),
            fp16_encoder=manifest.precision == "fp16_encoder",
            device=manifest.device,
        )

    def transcribe(self, model: object, audio_path: Path, manifest: CandidateRuntimeManifest) -> object:
        return model.transcribe(str(audio_path), word_timestamps=True)


class _FasterWhisperRuntime:
    def load(self, manifest: CandidateRuntimeManifest, asset_root: Path) -> object:
        whisper = importlib.import_module("faster_whisper")
        return whisper.WhisperModel(
            str(asset_root),
            device=manifest.device,
            compute_type=manifest.precision,
            local_files_only=True,
            cpu_threads=1,
            num_workers=1,
        )

    def transcribe(self, model: object, audio_path: Path, manifest: CandidateRuntimeManifest) -> object:
        segments, info = model.transcribe(
            str(audio_path),
            language="ru",
            task="transcribe",
            beam_size=manifest.beam_size,
            temperature=0,
            vad_filter=False,
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        if getattr(info, "language", None) != "ru":
            raise ValueError("faster-whisper ignored the pinned Russian language")
        return tuple(segments)


__all__ = [
    "AssetDigest",
    "CandidateAudioResolver",
    "CandidateRuntimeManifest",
    "CandidateHostEvidence",
    "GigaAMV3RNNTAdapter",
    "NoEgressEvidence",
    "ResolvedAudioChunk",
    "RuntimeBackend",
    "WhisperLargeV3Adapter",
    "sha256_file",
    "validate_runtime_manifest",
    "canonical_configuration_digest",
]
