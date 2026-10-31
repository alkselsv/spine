"""Operator-invoked synthetic-only offline ASR qualification runner.

No candidate runtime is imported until every source, asset, lock, dataset,
license, and no-egress preflight has passed. The current frozen #759 dataset
has no approved coverage minima, so it must return NOT_VERIFIED without model
inference.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import sys
import subprocess
import tempfile
import time
import wave
import threading
from uuid import NAMESPACE_URL, UUID, uuid5
from typing import Sequence, cast

from spine.domain.artifacts.transcription import (
    AudioChunkBoundary,
    AudioChunkHandle,
    AudioRecordingHandle,
    SpeechToTextRequestV2,
    SpeechToTextSegment,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.evals.asr_candidate_qualification import (
    ASR_CANDIDATE_QUALIFICATION_SCHEMA,
    AnnotationCoverageEvidence,
    AssetEvidence,
    CandidateCaseObservation,
    CandidateProfile,
    CandidateQualificationReport,
    Coverage,
    GateResult,
    HostEvidence,
    MetricObservation,
    PrerequisiteCheck,
    QualificationStatus,
    SegmentObservation,
    _GATE_DEFINITIONS,
    _METRIC_DEFINITIONS,
    candidate_profiles,
)
from spine.evals.transcription_russian_v2 import (
    QualificationSplit,
    RussianTranscriptionDatasetV2,
    SyntheticQualificationCase,
    load_russian_transcription_v2_dataset,
    transcription_dataset_v2_sha256,
    validate_transcription_v2_assets,
)
from spine.evals.transcription_russian_v2 import TRANSCRIPTION_V1_SHA256
from spine.infrastructure.asr_candidates import (
    CandidateAudioResolver,
    CandidateHostEvidence,
    CandidateRuntimeManifest,
    GigaAMV3RNNTAdapter,
    ResolvedAudioChunk,
    WhisperLargeV3Adapter,
    canonical_configuration_digest,
    sha256_file,
    validate_runtime_manifest,
)


def _runtime_installed(candidate_id: str) -> bool:
    name = "gigaam" if candidate_id == "gigaam-v3-rnnt" else "faster_whisper"
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _host_profile_mismatches(manifest: CandidateRuntimeManifest) -> tuple[str, ...]:
    mismatches: list[str] = []
    host = manifest.host
    if host.python_version != platform.python_version():
        mismatches.append("python_version_mismatch")
    if platform.system().lower() not in host.operating_system.lower():
        mismatches.append("operating_system_mismatch")
    try:
        actual_ram_mb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except (AttributeError, OSError, ValueError):
        actual_ram_mb = None
    if actual_ram_mb is None or actual_ram_mb != host.ram_mb:
        mismatches.append("ram_profile_mismatch")
    if manifest.device == "cuda":
        executable = shutil.which("nvidia-smi")
        if executable is None:
            mismatches.append("requested_cuda_device_unavailable")
        else:
            try:
                output = subprocess.run(
                    [executable, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.splitlines()
            except (OSError, subprocess.SubprocessError):
                mismatches.append("requested_cuda_device_unavailable")
            else:
                if not output:
                    mismatches.append("requested_cuda_device_unavailable")
                else:
                    fields = [field.strip() for field in output[0].split(",")]
                    if host.gpu is None or fields[0] != host.gpu:
                        mismatches.append("gpu_profile_mismatch")
                    try:
                        actual_vram_mb = int(fields[1])
                    except (IndexError, ValueError):
                        mismatches.append("vram_profile_unavailable")
                    else:
                        if host.vram_mb != actual_vram_mb:
                            mismatches.append("vram_profile_mismatch")
    elif host.gpu is not None:
        mismatches.append("unexpected_gpu_profile")
    return tuple(mismatches)


def _network_boundary_mismatches(manifest: CandidateRuntimeManifest) -> tuple[str, ...]:
    evidence = manifest.no_egress
    if not evidence.network_egress_disabled:
        return ("no_egress_attestation_denied",)
    if evidence.enforcement != "network_namespace":
        return ("no_egress_boundary_not_independently_observable",)
    mismatches: list[str] = []
    try:
        current_inode = os.stat("/proc/self/ns/net").st_ino
    except OSError:
        mismatches.append("network_namespace_identity_unavailable")
    else:
        if current_inode != evidence.network_namespace_inode:
            mismatches.append("network_namespace_identity_mismatch")
    try:
        interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
    except OSError:
        mismatches.append("network_interface_inventory_unavailable")
    else:
        if interfaces - {"lo"}:
            mismatches.append("non_loopback_network_interface_present")
    try:
        routes = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        mismatches.append("ipv4_route_inventory_unavailable")
    else:
        if any(len(row.split()) > 1 and row.split()[1] == "00000000" for row in routes):
            mismatches.append("ipv4_default_route_present")
    try:
        ipv6_routes = Path("/proc/net/ipv6_route").read_text(encoding="ascii").splitlines()
    except OSError:
        mismatches.append("ipv6_route_inventory_unavailable")
    else:
        if any(
            len(fields := row.split()) >= 10
            and fields[0] == "0" * 32
            and fields[1] == "00"
            for row in ipv6_routes
        ):
            mismatches.append("ipv6_default_route_present")
    return tuple(mismatches)


def _asset_inventory_digest(manifest: CandidateRuntimeManifest) -> str:
    payload = [item.model_dump(mode="json") for item in manifest.asset_inventory]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _GpuMemorySampler:
    """Sample whole-device VRAM; the benchmark host must be dedicated to this run."""

    def __init__(self) -> None:
        self.executable = shutil.which("nvidia-smi")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline_mb: int | None = None
        self.peak_mb: int | None = None

    def _sample(self) -> int | None:
        if self.executable is None:
            return None
        try:
            output = subprocess.run(
                [self.executable, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.splitlines()
            return int(output[0].strip()) if output else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def start(self) -> None:
        self.baseline_mb = self._sample()
        if self.baseline_mb is None:
            return

        def poll() -> None:
            while not self._stop.wait(0.25):
                sample = self._sample()
                if sample is not None:
                    self.peak_mb = max(self.peak_mb or sample, sample)

        self._thread = threading.Thread(target=poll, name="spine-asr-vram-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> int | None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        final = self._sample()
        if final is not None:
            self.peak_mb = max(self.peak_mb or final, final)
        if self.baseline_mb is None or self.peak_mb is None:
            return None
        return max(0, self.peak_mb - self.baseline_mb)

    def __del__(self) -> None:
        self._stop.set()


def _candidate_profile(manifest: CandidateRuntimeManifest) -> CandidateProfile:
    profile = next(item for item in candidate_profiles() if item.candidate_id == manifest.candidate_id)
    configuration = profile.configuration.model_copy(
        update={
            "language": manifest.language,
            "translation_enabled": manifest.translation_enabled,
            "language_detection_enabled": manifest.language_detection_enabled,
            "precision": manifest.precision,
            "configuration_digest": manifest.configuration_digest,
        }
    )
    return profile.model_copy(
        update={
            "adapter_reference": manifest.adapter_reference,
            "source_revision": manifest.source_revision,
            "runtime_reference": manifest.runtime_reference,
            "runtime_revision": manifest.runtime_revision,
            "checkpoint_reference": "v3_rnnt" if manifest.candidate_id == "gigaam-v3-rnnt" else "large-v3",
            "checkpoint_sha256": manifest.checkpoint_sha256,
            "asset_sha256": _asset_inventory_digest(manifest),
            "dependency_lock_digest": manifest.dependency_lock_sha256,
            "configuration": configuration,
        }
    )


def _host_evidence(
    host: CandidateHostEvidence,
    *,
    no_egress_verified: bool | None,
) -> HostEvidence:
    return HostEvidence(
        cpu=host.cpu,
        gpu=host.gpu,
        ram_mb=host.ram_mb,
        vram_mb=host.vram_mb,
        operating_system=host.operating_system,
        software_versions=(
            {"name": "python", "version": host.python_version},
            {"name": "platform", "version": platform.platform()},
        ),
        precision=None,
        network_egress_disabled=no_egress_verified,
        production_capacity_claim=False,
    )


def _not_verified_report(
    candidate: CandidateProfile,
    *,
    dataset_id: str,
    dataset_version: str,
    dataset_sha256: str,
    host: HostEvidence,
    reason: str,
    prerequisites: tuple[PrerequisiteCheck, ...],
    asset_inventory: tuple[AssetEvidence, ...] = (),
    annotation_coverage: tuple[AnnotationCoverageEvidence, ...] = (),
    no_egress_sha256: str | None = None,
) -> CandidateQualificationReport:
    gates = tuple(
        GateResult(
            gate_id=gate_id,
            categories=categories,
            status=QualificationStatus.NOT_VERIFIED,
            error_count=None,
            coverage=Coverage(annotated=0, eligible=0),
            observed_value=None,
            threshold=threshold,
            detail=reason,
        )
        for gate_id, categories, threshold in _GATE_DEFINITIONS
    )
    metrics = {
        metric_id: MetricObservation(
            status=QualificationStatus.NOT_VERIFIED,
            value=None,
            unit=unit,
            error_count=None,
            coverage=Coverage(annotated=0, eligible=0),
            detail=reason,
        )
        for metric_id, unit in _METRIC_DEFINITIONS
    }
    return CandidateQualificationReport(
        candidate=candidate,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_sha256=dataset_sha256,
        host=host,
        prerequisites=prerequisites,
        gates=gates,
        metrics=metrics,
        asset_inventory=asset_inventory,
        annotation_coverage=annotation_coverage,
        no_egress_evidence_sha256=no_egress_sha256,
        unsupported_capabilities=(
            "speaker_diarization",
            "speaker_identification",
            "confidence_scores",
        ),
        overall_status=QualificationStatus.NOT_VERIFIED,
        downstream_boundary=(
            "Downstream requirements extraction is a separate qualification owned by #646/#682; "
            "this report contains ASR evidence only."
        ),
        limitations=(
            reason,
            "No model inference was performed unless every preflight and coverage prerequisite passed.",
            "This synthetic benchmark report does not establish production host capacity or approval.",
        ),
    )


def preflight_candidate(
    manifest: CandidateRuntimeManifest,
    dataset: RussianTranscriptionDatasetV2,
    *,
    asset_root: Path,
) -> tuple[CandidateQualificationReport, bool]:
    """Return a report and true only when real inference is permitted to start."""

    problems = list(validate_runtime_manifest(manifest, verify_runtime_import_location=True))
    problems.extend(_host_profile_mismatches(manifest))
    problems.extend(_network_boundary_mismatches(manifest))
    observed_dataset_digest = transcription_dataset_v2_sha256(dataset)
    if (
        manifest.dataset_id != dataset.dataset_id
        or manifest.dataset_version != dataset.version
        or manifest.dataset_sha256 != observed_dataset_digest
    ):
        problems.append("frozen_dataset_identity_mismatch")
    try:
        validate_transcription_v2_assets(dataset, asset_root)
    except Exception:
        problems.append("dataset_asset_integrity_failed")
    if not _runtime_installed(manifest.candidate_id):
        problems.append("candidate_runtime_not_installed")
    measured_gates = {
        "russian_transcription",
        "critical_business_fragments",
        "adversarial_semantic_inversions",
        "terminology",
        "segment_timestamps",
        "silence",
        "long_form_chunking",
        "preprocessing_effects",
        "performance",
        "offline_integrity",
    }
    missing_coverage = tuple(
        item for item in dataset.coverage
        if item.gate_id in measured_gates
        and item.eligibility_rule != "operator_evidence_not_fixture_cases"
        and (item.minimum_annotation_count is None or item.annotated_count < item.minimum_annotation_count)
    )
    if missing_coverage:
        problems.append("qualification_coverage_not_sufficient")
    if canonical_configuration_digest(manifest) != manifest.configuration_digest:
        problems.append("frozen_configuration_digest_mismatch")
    network_errors = tuple(
        item for item in problems
        if item.startswith(("no_egress", "network_", "non_loopback_", "ipv4_", "ipv6_"))
    )
    host = _host_evidence(manifest.host, no_egress_verified=not network_errors)
    prerequisites = (
        PrerequisiteCheck(
            name="candidate_identity_pinned",
            status=QualificationStatus.PASS if not any("identity" in item or "revision" in item for item in problems) else QualificationStatus.NOT_VERIFIED,
            detail="Candidate source and runtime revisions are explicit SHA-1 commit identities.",
        ),
        PrerequisiteCheck(
            name="asset_inventory_verified",
            status=QualificationStatus.PASS if not any("asset" in item or "checkpoint" in item for item in problems) else QualificationStatus.NOT_VERIFIED,
            detail="Every staged asset must exactly match the explicit local inventory.",
        ),
        PrerequisiteCheck(
            name="dependency_lock_verified",
            status=QualificationStatus.PASS if not any("dependency_lock" in item or "installed_dependency" in item for item in problems) else QualificationStatus.NOT_VERIFIED,
            detail="The candidate-specific lock schema, digest, Python version, installed distribution set, versions and file-tree hashes are checked before runtime import.",
        ),
        PrerequisiteCheck(
            name="no_egress_attestation_verified",
            status=QualificationStatus.PASS if not network_errors else QualificationStatus.NOT_VERIFIED,
            detail="The operator no-egress attestation file is hash-checked; this is not a production host approval.",
        ),
        PrerequisiteCheck(
            name="license_review",
            status=QualificationStatus.PASS if manifest.license_commercial_use_approved else QualificationStatus.NOT_VERIFIED,
            detail="An attributable license review reference is required before model loading.",
        ),
        PrerequisiteCheck(
            name="frozen_dataset_identity",
            status=QualificationStatus.PASS if "frozen_dataset_identity_mismatch" not in problems and "dataset_asset_integrity_failed" not in problems else QualificationStatus.NOT_VERIFIED,
            detail="The manifest dataset ID/version/digest and every input asset are validated.",
        ),
        PrerequisiteCheck(
            name="qualification_coverage_sufficient",
            status=QualificationStatus.NOT_VERIFIED if missing_coverage else QualificationStatus.PASS,
            detail=f"{len(missing_coverage)} gate categories lack approved, met annotation minima.",
        ),
        PrerequisiteCheck(
            name="candidate_runtime_available",
            status=QualificationStatus.NOT_VERIFIED if any(item in problems for item in ("candidate_runtime_not_installed", "candidate_runtime_module_not_from_pinned_checkout", "candidate_runtime_checkout_revision_mismatch")) else QualificationStatus.PASS,
            detail="The installed candidate module must resolve from the clean, locally verified runtime checkout before runtime import.",
        ),
        PrerequisiteCheck(
            name="configuration_digest_verified",
            status=QualificationStatus.NOT_VERIFIED if "frozen_configuration_digest_mismatch" in problems else QualificationStatus.PASS,
            detail="Decoder, precision, language, source, lock, assets, dataset and host boundary are frozen by digest.",
        ),
    )
    asset_inventory = tuple(
        AssetEvidence(relative_path=item.path, sha256=item.sha256, byte_length=item.byte_length)
        for item in manifest.asset_inventory
    )
    annotation_coverage = _annotation_coverage(dataset)
    reason = "Candidate inference was not exercised: " + ",".join(dict.fromkeys(problems)) if problems else "All preflight checks passed."
    report = _not_verified_report(
        _candidate_profile(manifest),
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_sha256=observed_dataset_digest,
        host=host,
        reason=reason,
        prerequisites=prerequisites,
        asset_inventory=asset_inventory,
        annotation_coverage=annotation_coverage,
        no_egress_sha256=manifest.no_egress_evidence_sha256 if "no_egress_evidence_missing_or_digest_mismatch" not in problems else None,
    )
    return report, not problems


def save_immutable_report(report: CandidateQualificationReport, output_path: Path) -> str:
    """Write one report once and return the canonical report SHA-256."""

    payload = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    digest = hashlib.sha256(payload).hexdigest()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    digest_path = output_path.with_suffix(output_path.suffix + ".sha256")
    digest_descriptor = os.open(digest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(digest_descriptor, "w", encoding="ascii") as output:
        output.write(f"{digest}  {output_path.name}\n")
    return digest


def run_preflight_from_file(manifest_path: Path, asset_root: Path) -> CandidateQualificationReport:
    manifest = CandidateRuntimeManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if manifest.dataset_version == "1.0.0":
        return _not_verified_report(
            _candidate_profile(manifest),
            dataset_id=manifest.dataset_id,
            dataset_version="1.0.0",
            dataset_sha256=TRANSCRIPTION_V1_SHA256,
            host=_host_evidence(manifest.host, no_egress_verified=None),
            reason=(
                "Historical v1.0.0 dataset is recognized and immutable, but lacks the v2 typed "
                "coverage and evidence contract required for qualification; inference was not exercised."
            ),
            prerequisites=(PrerequisiteCheck(
                name="historical_dataset_compatibility",
                status=QualificationStatus.NOT_VERIFIED,
                detail="v1.0.0 identity is preserved; qualification requires the extended v2 annotation contract.",
            ),),
        )
    dataset = load_russian_transcription_v2_dataset()
    report, ready = preflight_candidate(manifest, dataset, asset_root=asset_root)
    if ready:
        try:
            return asyncio.run(_run_real_candidate(manifest, dataset, asset_root=asset_root))
        except Exception as error:
            failure = PrerequisiteCheck(
                name="inference_execution",
                status=QualificationStatus.NOT_VERIFIED,
                detail=f"Real local inference aborted with {type(error).__name__}; partial outputs were not qualified.",
            )
            return report.model_copy(update={
                "prerequisites": (*report.prerequisites, failure),
                "limitations": (*report.limitations, "Inference aborted before a complete measured report; raw exception details were omitted."),
            })
    return report


class _MappedAudioResolver(CandidateAudioResolver):
    def __init__(self, audio: ResolvedAudioChunk) -> None:
        self.audio = audio

    async def resolve(self, request: SpeechToTextRequestV2) -> ResolvedAudioChunk:
        if request.chunk.decoded_sha256 != self.audio.sha256:
            raise ValueError("request chunk does not match the staged local chunk")
        return self.audio


def _reference(digest: str, byte_length: int, key: str) -> ObjectReference:
    return ObjectReference(
        schema_version=1,
        object_id=uuid5(NAMESPACE_URL, f"spine:asr-qualification:object:{key}"),
        storage_generation=uuid5(NAMESPACE_URL, f"spine:asr-qualification:generation:{key}"),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=byte_length,
    )


def _write_pcm_chunks(source_path: Path, output_root: Path, max_chunk_ms: int) -> tuple[tuple[Path, int, int], ...]:
    chunks: list[tuple[Path, int, int]] = []
    with wave.open(str(source_path), "rb") as source:
        if source.getnchannels() != 1 or source.getframerate() != 16_000 or source.getsampwidth() != 2:
            raise ValueError("qualification preprocessing output must be 16 kHz mono PCM16")
        frames_per_chunk = max_chunk_ms * source.getframerate() // 1000
        total_frames = source.getnframes()
        offset = 0
        index = 0
        while offset < total_frames:
            frame_count = min(frames_per_chunk, total_frames - offset)
            source.setpos(offset)
            payload = source.readframes(frame_count)
            target = output_root / f"chunk-{index + 1:04d}.wav"
            with wave.open(str(target), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(payload)
            start_ms = round(offset * 1000 / 16_000)
            end_ms = round((offset + frame_count) * 1000 / 16_000)
            chunks.append((target, start_ms, end_ms))
            offset += frame_count
            index += 1
    return tuple(chunks)


def _input_assets(case: SyntheticQualificationCase, data_root: Path) -> tuple[tuple[str, str, str, Path], ...]:
    primary_preprocessing = case.preprocessing
    if primary_preprocessing.derived_audio_path is None:
        return ()
    result = [(
        case.audio.media_format.value,
        case.audio.sha256,
        primary_preprocessing.output_audio_sha256 or "",
        data_root.joinpath(*Path(case.audio.fixture_path).parts),
    )]
    by_source = {item.source_audio_sha256: item for item in case.format_variant_preprocessing}
    for variant in case.format_variants:
        evidence = by_source[variant.sha256]
        if evidence.derived_audio_path is not None and evidence.output_audio_sha256 is not None:
            result.append((
                variant.media_format.value,
                variant.sha256,
                evidence.output_audio_sha256,
                data_root.joinpath(*Path(variant.fixture_path).parts),
            ))
    return tuple(result)


def _preprocess_audio(
    manifest: CandidateRuntimeManifest,
    source: Path,
    target: Path,
    duration_ms: int,
) -> None:
    command = [
        manifest.preprocessing_executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-ac",
        "1",
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-ar",
        "16000",
        "-codec:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(target),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=120)


async def _run_real_candidate(
    manifest: CandidateRuntimeManifest,
    dataset: RussianTranscriptionDatasetV2,
    *,
    asset_root: Path,
) -> CandidateQualificationReport:
    """Run real local inference after the complete fail-closed preflight."""

    data_root = asset_root
    cold_started = time.perf_counter()
    gpu_sampler = _GpuMemorySampler() if manifest.device == "cuda" else None
    if gpu_sampler is not None:
        gpu_sampler.start()
    adapter = (
        GigaAMV3RNNTAdapter(manifest, _EmptyResolver())
        if manifest.candidate_id == "gigaam-v3-rnnt"
        else WhisperLargeV3Adapter(manifest, _EmptyResolver())
    )
    cold_start_ms = round((time.perf_counter() - cold_started) * 1000)
    observations: list[CandidateCaseObservation] = []
    all_inference_ms = 0
    all_preprocessing_ms = 0
    all_reassembly_ms = 0
    all_duration_ms = 0
    warm_inference_ms = 0
    warm_duration_ms = 0
    inference_run_count = 0
    prediction_by_case: dict[str, str] = {}
    predicted_segments_by_case: dict[str, list[SegmentObservation]] = {}
    variant_case_count = 0
    with tempfile.TemporaryDirectory(prefix="spine-asr-qualification-") as temporary:
        temp_root = Path(temporary)
        for case in dataset.cases:
            if case.split is not QualificationSplit.QUALIFICATION:
                continue
            for input_format, input_sha256, preprocessed_sha256, raw_input_path in _input_assets(case, data_root):
                case_root = temp_root / f"{case.case_id}-{input_format}"
                case_root.mkdir()
                started_preprocessing = time.perf_counter()
                prepared_path = case_root / "preprocessed.wav"
                _preprocess_audio(manifest, raw_input_path, prepared_path, case.duration_ms)
                if not prepared_path.is_file() or sha256_file(prepared_path) != preprocessed_sha256:
                    raise ValueError(f"preprocessing output does not reproduce frozen bytes for {case.case_id}/{input_format}")
                chunks = _write_pcm_chunks(prepared_path, case_root, manifest.chunk_duration_limit_ms)
                preprocessing_ms = round((time.perf_counter() - started_preprocessing) * 1000)
                assembled: list[SpeechToTextSegment] = []
                inference_ms = 0
                original_audio = raw_input_path.read_bytes()
                recording_reference = _reference(input_sha256, len(original_audio), f"{case.case_id}:{input_format}:recording")
                recording = AudioRecordingHandle(
                    workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
                    environment_id=UUID("20000000-0000-4000-8000-000000000001"),
                    source_object_id=UUID("30000000-0000-4000-8000-000000000001"),
                    source_revision_id=uuid5(NAMESPACE_URL, f"spine:asr-qualification:revision:{case.case_id}"),
                    source_revision_digest=hashlib.sha256(f"{dataset.version}:{case.case_id}".encode()).hexdigest(),
                    original_sha256=input_sha256,
                    original_reference=recording_reference,
                    media_type={"wav_pcm": "audio/wav", "mp3_layer_iii": "audio/mpeg", "m4a_aac_lc": "audio/mp4"}[input_format],
                    recording_duration_ms=case.duration_ms,
                )
                for ordinal, (chunk_path, start_ms, end_ms) in enumerate(chunks, start=1):
                    chunk_bytes = chunk_path.read_bytes()
                    chunk_digest = hashlib.sha256(chunk_bytes).hexdigest()
                    chunk_reference = _reference(chunk_digest, len(chunk_bytes), f"{case.case_id}:{input_format}:{ordinal}")
                    boundary = AudioChunkBoundary(
                        ordinal=ordinal,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        overlap_before_ms=0,
                    )
                    chunk_handle = AudioChunkHandle.create(
                        boundary=boundary,
                        decoder_reference="ffmpeg-16khz-mono-pcm16:v1",
                        decoded_reference=chunk_reference,
                        decoded_sha256=chunk_digest,
                    )
                    request = SpeechToTextRequestV2(
                        recording=recording,
                        processing_profile_reference="asr-offline-qualification:v1",
                        adapter_reference=manifest.adapter_reference,
                        model_config_reference=manifest.model_config_reference,
                        language_hint="ru",
                        chunk=chunk_handle,
                    )
                    duration_ms = end_ms - start_ms
                    resolver = _MappedAudioResolver(ResolvedAudioChunk(
                        path=str(chunk_path),
                        sha256=chunk_digest,
                        byte_length=len(chunk_bytes),
                        duration_ms=duration_ms,
                    ))
                    adapter.audio_resolver = resolver
                    started = time.perf_counter()
                    output = await adapter.transcribe(request)
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    inference_ms += elapsed_ms
                    chunk_expectation = next(
                        (item for item in case.chunks if item.start_ms == start_ms and item.end_ms == end_ms),
                        None,
                    )
                    failure_cutoff_ms = (
                        next(segment.end_ms for segment in case.reference_segments if segment.segment_id == chunk_expectation.failure_after_segment_id)
                        if chunk_expectation is not None and chunk_expectation.expected_outcome == "partial_failure"
                        else None
                    )
                    for segment in output.segments:
                        absolute_segment = segment.model_copy(update={
                            "start_ms": segment.start_ms + start_ms,
                            "end_ms": segment.end_ms + start_ms,
                        })
                        if failure_cutoff_ms is None or absolute_segment.end_ms <= failure_cutoff_ms:
                            assembled.append(absolute_segment)
                reassembly_started = time.perf_counter()
                ordered = _reassemble_segments(assembled)
                transcript = " ".join(item.text for item in ordered)
                matched_segments = 0
                matched_segment_ids: set[str] = set()
                used_predictions: set[int] = set()
                for expected_segment in case.reference_segments:
                    overlapping = [
                        (index, actual)
                        for index, actual in enumerate(ordered)
                        if actual.start_ms < expected_segment.end_ms and expected_segment.start_ms < actual.end_ms
                    ]
                    actual_text = " ".join(_normalized_tokens(" ".join(item.text for _, item in overlapping)))
                    expected_text = " ".join(_normalized_tokens(expected_segment.reference_text))
                    if expected_text and expected_text in actual_text:
                        matched_segments += 1
                        matched_segment_ids.add(expected_segment.segment_id)
                        used_predictions.update(index for index, _ in overlapping)
                reference_matches = min(matched_segments, len(case.reference_segments))
                actual_chunk_intervals = tuple((start, end) for _, start, end in chunks)
                expected_chunk_intervals = tuple((item.start_ms, item.end_ms) for item in case.chunks)
                chunk_boundaries_verified = bool(case.chunks) and actual_chunk_intervals == expected_chunk_intervals and all(
                    item.overlap_before_ms == 0 for item in case.chunks
                )
                chunks_by_id = {item.chunk_id: item for item in case.chunks}
                references_by_id = {item.segment_id: item for item in case.reference_segments}
                partial_recovery_verified = bool(case.chunk_recoveries) and all(
                    recovery.completed_segment_ids
                    and all(
                        segment_id in matched_segment_ids
                        and references_by_id[segment_id].end_ms <= chunks_by_id[recovery.failed_chunk_id].end_ms
                        for segment_id in recovery.completed_segment_ids
                    )
                    and all(segment_id in matched_segment_ids for segment_id in recovery.expected_final_segment_ids)
                    and chunks_by_id[recovery.resume_from_chunk_id].start_ms >= chunks_by_id[recovery.failed_chunk_id].end_ms
                    for recovery in case.chunk_recoveries
                )
                reassembly_ms = round((time.perf_counter() - reassembly_started) * 1000)
                observations.append(CandidateCaseObservation(
                    case_id=case.case_id,
                    input_format=input_format,
                    input_sha256=input_sha256,
                    transcript=transcript,
                    segments=tuple(SegmentObservation(start_ms=item.start_ms, end_ms=item.end_ms, text=item.text) for item in ordered),
                    preprocessing_ms=preprocessing_ms,
                    inference_ms=inference_ms,
                    reassembly_ms=reassembly_ms,
                    matched_reference_segments=reference_matches,
                    missing_reference_segments=max(0, len(case.reference_segments) - reference_matches),
                    unmatched_prediction_segments=max(0, len(ordered) - len(used_predictions)),
                    timestamps_supported=not any(
                        signal.name == "timestamp_source" and signal.value == "chunk_window"
                        for segment in assembled for signal in segment.signals
                    ),
                    chunk_boundaries_verified=chunk_boundaries_verified,
                    partial_recovery_verified=partial_recovery_verified,
                ))
                if input_format == case.audio.media_format.value:
                    prediction_by_case[case.case_id] = transcript
                    predicted_segments_by_case[case.case_id] = list(observations[-1].segments)
                else:
                    variant_case_count += 1
                all_preprocessing_ms += preprocessing_ms
                all_inference_ms += inference_ms
                all_reassembly_ms += reassembly_ms
                all_duration_ms += case.duration_ms
                if inference_run_count > 0:
                    warm_inference_ms += inference_ms
                    warm_duration_ms += case.duration_ms
                inference_run_count += 1
    candidate = _candidate_profile(manifest)
    peak_vram_mb = gpu_sampler.stop() if gpu_sampler is not None else 0
    try:
        import resource

        peak_ram_mb = max(0, round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024))
        peak_ram_observed = True
    except (ImportError, AttributeError, OSError):
        peak_ram_mb = 0
        peak_ram_observed = False
    metrics, gates = _evaluate_observations(
        dataset,
        observations,
        prediction_by_case,
        predicted_segments_by_case,
        manifest,
        offline_integrity_observed=True,
        performance_observed=(
            peak_vram_mb is not None
            and peak_ram_observed
            and warm_duration_ms > 0
            and bool(observations)
        ),
    )
    metrics.update({
        "preprocessing_ms": _observed_metric(float(all_preprocessing_ms), "milliseconds", len(observations), len(observations)),
        "inference_ms": _observed_metric(float(all_inference_ms), "milliseconds", len(observations), len(observations)),
        "reassembly_ms": _observed_metric(float(all_reassembly_ms), "milliseconds", len(observations), len(observations)),
        "cold_start_ms": _observed_metric(float(cold_start_ms), "milliseconds", 1, 1),
        "warm_inference_rtf": _observed_metric(float(warm_inference_ms / warm_duration_ms), "ratio", max(0, len(observations) - 1), max(0, len(observations) - 1)) if warm_duration_ms else MetricObservation(status=QualificationStatus.NOT_VERIFIED, value=None, unit="ratio", coverage=Coverage(annotated=0, eligible=max(0, len(observations) - 1)), detail="A warm run after the first inference was not available."),
        "end_to_end_rtf": _observed_metric(float((all_preprocessing_ms + all_inference_ms + all_reassembly_ms) / max(all_duration_ms, 1)), "ratio", len(observations), len(observations)),
        "peak_ram_mb": _observed_metric(float(peak_ram_mb), "megabytes", len(observations), len(observations)) if peak_ram_observed else MetricObservation(status=QualificationStatus.NOT_VERIFIED, value=None, unit="megabytes", coverage=Coverage(annotated=0, eligible=len(observations)), detail="Peak RAM measurement is unavailable on this host."),
        "peak_vram_mb": _observed_metric(float(peak_vram_mb), "megabytes", len(observations), len(observations)) if peak_vram_mb is not None else MetricObservation(status=QualificationStatus.NOT_VERIFIED, value=None, unit="megabytes", coverage=Coverage(annotated=0, eligible=len(observations)), detail="NVIDIA VRAM sampling was unavailable; the performance gate remains NOT_VERIFIED."),
    })
    prerequisite_checks = tuple(
        PrerequisiteCheck(name=name, status=QualificationStatus.PASS, detail=detail)
        for name, detail in (
            ("candidate_identity_pinned", "Candidate source/runtime and all staged asset digests were verified."),
            ("dependency_lock_verified", "Candidate dependency lock digest was verified before lazy runtime import."),
            ("no_egress_attestation_verified", "The operator supplied a hash-verified no-egress evidence record."),
            ("license_review", "The candidate manifest references an approved license review."),
            ("frozen_dataset_identity", "All runs used the same digest-verified qualification split."),
            ("qualification_coverage_sufficient", "Every required gate category met an attributable coverage minimum."),
        )
    )
    return CandidateQualificationReport(
        candidate=candidate,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_sha256=transcription_dataset_v2_sha256(dataset),
        host=_host_evidence(manifest.host, no_egress_verified=True),
        prerequisites=prerequisite_checks,
        gates=gates,
        metrics=metrics,
        asset_inventory=tuple(AssetEvidence(relative_path=item.path, sha256=item.sha256, byte_length=item.byte_length) for item in manifest.asset_inventory),
        annotation_coverage=_annotation_coverage(dataset),
        no_egress_evidence_sha256=manifest.no_egress_evidence_sha256,
        case_observations=tuple(observations),
        unsupported_capabilities=("speaker_diarization", "speaker_identification", "confidence_scores"),
        overall_status=QualificationStatus.PASS if gates and all(item.status is QualificationStatus.PASS for item in gates) else QualificationStatus.NOT_VERIFIED,
        downstream_boundary="Downstream requirements extraction is separate under #646/#682; this report is ASR only.",
        limitations=(f"Benchmark host only; no production capacity is implied. Measured {variant_case_count} supported-format variants.",),
    )


class _EmptyResolver(CandidateAudioResolver):
    async def resolve(self, request: SpeechToTextRequestV2) -> ResolvedAudioChunk:
        raise RuntimeError("runner must inject an explicit verified chunk for every request")


def _reassemble_segments(segments: Sequence[SpeechToTextSegment]) -> tuple[SpeechToTextSegment, ...]:
    """Restore deterministic original-timeline ordering after chunk inference."""

    return tuple(sorted(segments, key=lambda item: (item.start_ms, item.end_ms, item.text)))


def _observed_metric(value: float, unit: str, annotated: int, eligible: int) -> MetricObservation:
    return MetricObservation(
        status=QualificationStatus.PASS,
        value=value,
        unit=unit,
        error_count=None,
        coverage=Coverage(annotated=annotated, eligible=eligible),
        detail="Measured from actual local candidate inference on the synthetic qualification split.",
    )


def _annotation_coverage(dataset: RussianTranscriptionDatasetV2) -> tuple[AnnotationCoverageEvidence, ...]:
    return tuple(
        AnnotationCoverageEvidence(
            gate_id=item.gate_id,
            category_id=item.category_id,
            evidence_source=(
                "operator_evidence"
                if item.eligibility_rule == "operator_evidence_not_fixture_cases"
                else "fixture_annotations"
            ),
            annotated_count=item.annotated_count,
            eligible_count=item.eligible_count,
            minimum_annotation_count=item.minimum_annotation_count,
            status=(
                QualificationStatus.PASS
                if item.minimum_annotation_count is not None and item.annotated_count >= item.minimum_annotation_count
                else QualificationStatus.NOT_VERIFIED
            ),
        )
        for item in dataset.coverage
    )


def _edit_distance(left: Sequence[str], right: Sequence[str]) -> int:
    previous = list(range(len(right) + 1))
    for index, left_item in enumerate(left, start=1):
        current = [index]
        for column, right_item in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left_item != right_item)))
        previous = current
    return previous[-1]


def _normalized_tokens(text: str) -> list[str]:
    import re

    return re.findall(r"[0-9a-zа-яё]+", text.lower().replace("ё", "е"))


def _phrase_occurrences(phrase: str, transcript: str) -> int:
    normalized_phrase = " ".join(_normalized_tokens(phrase))
    if not normalized_phrase:
        return 0
    padded_transcript = f" {transcript} "
    padded_phrase = f" {normalized_phrase} "
    count = 0
    offset = 0
    while (position := padded_transcript.find(padded_phrase, offset)) >= 0:
        count += 1
        offset = position + len(padded_phrase)
    return count


def _evaluate_observations(
    dataset: RussianTranscriptionDatasetV2,
    observations: Sequence[CandidateCaseObservation],
    prediction_by_case: dict[str, str],
    predicted_segments_by_case: dict[str, list[SegmentObservation]],
    manifest: CandidateRuntimeManifest,
    *,
    offline_integrity_observed: bool = False,
    performance_observed: bool = False,
) -> tuple[dict[str, MetricObservation], tuple[GateResult, ...]]:
    references = {case.case_id: case for case in dataset.cases if case.split is QualificationSplit.QUALIFICATION}
    all_qualification_cases_observed = set(references).issubset(prediction_by_case)
    word_errors = word_count = char_errors = char_count = 0
    for case_id, reference_case in references.items():
        if case_id not in prediction_by_case:
            continue
        expected_words = _normalized_tokens(reference_case.expected_transcript)
        observed_words = _normalized_tokens(prediction_by_case[case_id])
        word_errors += _edit_distance(expected_words, observed_words)
        word_count += len(expected_words)
        expected_chars = "".join(expected_words)
        observed_chars = "".join(observed_words)
        char_errors += _edit_distance(list(expected_chars), list(observed_chars))
        char_count += len(expected_chars)
    wer = word_errors / max(word_count, 1)
    cer = char_errors / max(char_count, 1)
    critical_total = critical_exact = 0
    term_total = term_exact = term_predictions = 0
    adversarial_total = adversarial_observed = semantic_inversions = 0
    silence_words = 0
    silence_segments = 0
    timestamp_errors: list[int] = []
    terminology_lexicon = {
        " ".join(_normalized_tokens(term))
        for case in references.values()
        for annotation in case.terminology_spans
        for term in (annotation.expected_text, annotation.canonical_term, *annotation.permissible_variants)
        if _normalized_tokens(term)
    }
    reference_segments_by_id = {
        segment.segment_id: segment
        for case in references.values()
        for segment in case.reference_segments
    }
    def hypothesis_for_segment(case_id: str, segment_id: str) -> str:
        reference_segment = reference_segments_by_id[segment_id]
        peer_segments = [
            item for item in references[case_id].reference_segments
            if item.segment_id != segment_id
        ]
        overlapping = [
            segment
            for segment in predicted_segments_by_case.get(case_id, ())
            if segment.start_ms < reference_segment.end_ms
            and reference_segment.start_ms < segment.end_ms
            and not any(
                segment.start_ms < peer.end_ms and peer.start_ms < segment.end_ms
                for peer in peer_segments
            )
        ]
        return " ".join(_normalized_tokens(" ".join(item.text for item in overlapping)))

    def localization_complete(attribute: str) -> bool:
        identities = {
            (case.case_id, annotation.segment_id)
            for case in references.values()
            for annotation in getattr(case, attribute)
        }
        return bool(identities) and all(bool(hypothesis_for_segment(case_id, segment_id)) for case_id, segment_id in identities)

    critical_localization_complete = localization_complete("critical_spans")
    adversarial_localization_complete = localization_complete("adversarial_labels")
    terminology_localization_complete = localization_complete("terminology_spans")

    for case_id, reference_case in references.items():
        hypothesis = _normalized_tokens(prediction_by_case.get(case_id, ""))
        transcript = " ".join(hypothesis)
        for annotation in reference_case.critical_spans:
            critical_total += 1
            expected = " ".join(_normalized_tokens(annotation.expected_text))
            local_transcript = hypothesis_for_segment(case_id, annotation.segment_id)
            critical_exact += int(bool(expected) and expected in local_transcript)
        case_terms = 0
        for annotation in reference_case.terminology_spans:
            term_total += 1
            acceptable = {annotation.expected_text, annotation.canonical_term, *annotation.permissible_variants}
            local_transcript = hypothesis_for_segment(case_id, annotation.segment_id)
            matched = int(any(" ".join(_normalized_tokens(term)) in local_transcript for term in acceptable))
            case_terms += matched
        case_term_predictions = sum(_phrase_occurrences(term, transcript) for term in terminology_lexicon)
        term_predictions += case_term_predictions
        term_exact += min(case_terms, case_term_predictions)
        for annotation in reference_case.adversarial_labels:
            adversarial_total += 1
            expected = " ".join(_normalized_tokens(annotation.expected_text))
            forbidden = " ".join(_normalized_tokens(annotation.forbidden_inversion))
            local_transcript = hypothesis_for_segment(case_id, annotation.segment_id)
            if forbidden and forbidden in local_transcript:
                semantic_inversions += 1
                adversarial_observed += 1
            elif expected and expected in local_transcript:
                adversarial_observed += 1
        if reference_case.media_category == "silence":
            silence_words += len(hypothesis)
            silence_segments += len(predicted_segments_by_case.get(case_id, ()))
        for interval in reference_case.verified_silence_intervals:
            silence_segments += sum(
                1 for item in predicted_segments_by_case.get(case_id, ())
                if item.text.strip() and item.start_ms >= interval.start_ms and item.end_ms <= interval.end_ms
            )
        predicted = predicted_segments_by_case.get(case_id, [])
        for expected_segment in reference_case.reference_segments:
            overlapping = [
                item for item in predicted
                if item.start_ms < expected_segment.end_ms and expected_segment.start_ms < item.end_ms
            ]
            observed_text = " ".join(_normalized_tokens(" ".join(item.text for item in overlapping)))
            expected_text = " ".join(_normalized_tokens(expected_segment.reference_text))
            if overlapping and expected_text and expected_text in observed_text:
                observed_start = min(item.start_ms for item in overlapping)
                observed_end = max(item.end_ms for item in overlapping)
                timestamp_errors.extend((abs(expected_segment.start_ms - observed_start), abs(expected_segment.end_ms - observed_end)))
    has_minima = { (item.gate_id, item.category_id): item.minimum_annotation_count is not None and item.annotated_count >= item.minimum_annotation_count for item in dataset.coverage }
    russian_coverage_sufficient = all(has_minima.get(("russian_transcription", category), False) for category in ("clean", "noisy", "overlap", "long_form"))
    critical_coverage_sufficient = all(has_minima.get(("critical_business_fragments", category), False) for category in ("amount_currency", "quantity", "date_deadline", "unit", "negation", "obligation", "permission", "condition", "constraint"))
    adversarial_coverage_sufficient = all(has_minima.get(("adversarial_semantic_inversions", category), False) for category in ("negation", "bounds", "permission", "inclusion", "obligation"))
    timestamp_coverage_sufficient = all(has_minima.get(("segment_timestamps", category), False) for category in ("boundary_error", "alignment_coverage"))
    silence_coverage_sufficient = all(has_minima.get(("silence", category), False) for category in ("pure_silence", "verified_silence_intervals"))
    timestamp_alignment_complete = bool(observations) and all(
        item.timestamps_supported
        and item.missing_reference_segments == 0
        and item.matched_reference_segments == len(references[item.case_id].reference_segments)
        for item in observations
    )
    term_coverage_sufficient = has_minima.get(("terminology", "all_annotated_terms"), False)
    metric_values: dict[str, tuple[float | None, str, int | None, Coverage, QualificationStatus]] = {
        "wer": (wer, "ratio", word_errors, Coverage(annotated=len(prediction_by_case), eligible=len(references)), QualificationStatus.FAIL if all_qualification_cases_observed and wer > 0.15 else QualificationStatus.PASS if all_qualification_cases_observed and russian_coverage_sufficient else QualificationStatus.NOT_VERIFIED),
        "cer": (cer, "ratio", char_errors, Coverage(annotated=len(prediction_by_case), eligible=len(references)), QualificationStatus.FAIL if all_qualification_cases_observed and cer > 0.08 else QualificationStatus.PASS if all_qualification_cases_observed and russian_coverage_sufficient else QualificationStatus.NOT_VERIFIED),
        "critical_fragment_accuracy": (critical_exact / critical_total if critical_total else None, "ratio", critical_total-critical_exact if critical_total else None, Coverage(annotated=critical_total, eligible=sum(len(c.critical_spans) for c in references.values())), QualificationStatus.FAIL if all_qualification_cases_observed and critical_localization_complete and critical_total and critical_exact / critical_total < 0.98 else QualificationStatus.PASS if all_qualification_cases_observed and critical_localization_complete and critical_coverage_sufficient and critical_total else QualificationStatus.NOT_VERIFIED),
        "semantic_inversions": (float(semantic_inversions), "count", semantic_inversions, Coverage(annotated=adversarial_observed, eligible=adversarial_total), QualificationStatus.FAIL if semantic_inversions else QualificationStatus.PASS if adversarial_coverage_sufficient and adversarial_observed == adversarial_total and adversarial_localization_complete and adversarial_total > 0 else QualificationStatus.NOT_VERIFIED),
        "terminology_precision": (term_exact / term_predictions if term_predictions else 0.0 if term_total else None, "ratio", term_predictions-term_exact if term_total else None, Coverage(annotated=term_exact, eligible=term_predictions), QualificationStatus.FAIL if term_predictions and term_exact / term_predictions < 0.95 else QualificationStatus.PASS if term_coverage_sufficient and all_qualification_cases_observed and terminology_localization_complete and term_predictions > 0 else QualificationStatus.NOT_VERIFIED),
        "terminology_recall": (term_exact / term_total if term_total else None, "ratio", term_total-term_exact if term_total else None, Coverage(annotated=term_total, eligible=sum(len(c.terminology_spans) for c in references.values())), QualificationStatus.FAIL if term_total and term_exact / term_total < 0.95 else QualificationStatus.PASS if term_coverage_sufficient and all_qualification_cases_observed and terminology_localization_complete else QualificationStatus.NOT_VERIFIED),
        "segment_timestamp_p50_ms": (statistics.median(timestamp_errors) if timestamp_errors else None, "milliseconds", None, Coverage(annotated=len(timestamp_errors)//2, eligible=sum(len(c.reference_segments) for c in references.values())), QualificationStatus.FAIL if timestamp_errors and statistics.median(timestamp_errors) > 500 else QualificationStatus.PASS if timestamp_coverage_sufficient and timestamp_alignment_complete and timestamp_errors else QualificationStatus.NOT_VERIFIED),
        "segment_timestamp_p95_ms": (sorted(timestamp_errors)[min(len(timestamp_errors)-1, round((len(timestamp_errors)-1)*0.95))] if timestamp_errors else None, "milliseconds", None, Coverage(annotated=len(timestamp_errors)//2, eligible=sum(len(c.reference_segments) for c in references.values())), QualificationStatus.FAIL if timestamp_errors and sorted(timestamp_errors)[min(len(timestamp_errors)-1, round((len(timestamp_errors)-1)*0.95))] > 1500 else QualificationStatus.PASS if timestamp_coverage_sufficient and timestamp_alignment_complete and timestamp_errors else QualificationStatus.NOT_VERIFIED),
        "silence_hallucinations": (float(silence_words + silence_segments), "count", silence_words + silence_segments, Coverage(annotated=sum(c.media_category == "silence" for c in references.values()), eligible=sum(c.media_category == "silence" for c in references.values())), QualificationStatus.FAIL if silence_words + silence_segments else QualificationStatus.PASS if silence_coverage_sufficient and all_qualification_cases_observed else QualificationStatus.NOT_VERIFIED),
    }
    metrics = {
        name: MetricObservation(status=status, value=value, unit=unit, error_count=errors, coverage=coverage, detail="Computed from real candidate outputs; NOT_VERIFIED remains until coverage sufficiency is approved and met.")
        for name, (value, unit, errors, coverage, status) in metric_values.items()
    }
    for name, unit in _METRIC_DEFINITIONS:
        metrics.setdefault(name, MetricObservation(status=QualificationStatus.NOT_VERIFIED, value=None, unit=unit, error_count=None, coverage=Coverage(annotated=0, eligible=0), detail="Metric is not derivable from this observation contract."))
    observations_by_case_format = {(item.case_id, item.input_format): item for item in observations}
    format_categories = {
        "wav_pcm": "supported_wav_pcm",
        "mp3_layer_iii": "supported_mp3",
        "m4a_aac_lc": "supported_m4a",
    }
    for input_format in format_categories:
        eligible_case_ids = {
            case.case_id
            for case in references.values()
            if any(asset.media_format.value == input_format for asset in (case.audio, *case.format_variants))
        }
        observed_case_ids = {
            case_id for case_id in eligible_case_ids
            if (case_id, input_format) in observations_by_case_format
        }
        errors = 0
        words = 0
        for case_id in observed_case_ids:
            expected = _normalized_tokens(references[case_id].expected_transcript)
            actual = _normalized_tokens(observations_by_case_format[(case_id, input_format)].transcript)
            errors += _edit_distance(expected, actual)
            words += len(expected)
        metrics[f"wer_{input_format}"] = MetricObservation(
            status=QualificationStatus.PASS if eligible_case_ids and observed_case_ids == eligible_case_ids else QualificationStatus.NOT_VERIFIED,
            value=errors / max(words, 1) if eligible_case_ids else None,
            unit="ratio",
            error_count=errors if eligible_case_ids else None,
            coverage=Coverage(annotated=len(observed_case_ids), eligible=len(eligible_case_ids)),
            detail="Per-input-format transcription error is reported to expose deterministic preprocessing and codec effects.",
        )
    paired_cases = {
        case.case_id: case
        for case in references.values()
        if case.preprocessing_pair_id is not None
    }
    preprocessing_baseline_observed = bool(paired_cases) and all(
        (case_id, case.audio.media_format.value) in observations_by_case_format
        for case_id, case in paired_cases.items()
    )
    preprocessing_comparison_observed = bool(paired_cases) and all(
        (case_id, asset.media_format.value) in observations_by_case_format
        for case_id, case in paired_cases.items()
        for asset in case.format_variants
    )
    long_form_cases = {
        case.case_id: case
        for case in references.values()
        if case.media_category == "long_form"
    }
    long_form_observations = {
        case_id: observations_by_case_format.get((case_id, case.audio.media_format.value))
        for case_id, case in long_form_cases.items()
    }
    long_form_chunks_observed = bool(long_form_cases) and all(
        item is not None and item.chunk_boundaries_verified
        for item in long_form_observations.values()
    )
    long_form_recovery_observed = bool(long_form_cases) and all(
        item is not None and item.partial_recovery_verified
        for item in long_form_observations.values()
    )
    long_form_pause_handling_observed = bool(long_form_cases) and all(
        (case_id, case.audio.media_format.value) in observations_by_case_format
        and bool(case.verified_silence_intervals)
        and not any(
            segment.text.strip()
            and segment.start_ms >= interval.start_ms
            and segment.end_ms <= interval.end_ms
            for segment in observations_by_case_format[(case_id, case.audio.media_format.value)].segments
            for interval in case.verified_silence_intervals
        )
        for case_id, case in long_form_cases.items()
    )
    gate_results = []
    for gate_id, categories, threshold in _GATE_DEFINITIONS:
        required_coverage = []
        if gate_id == "russian_transcription":
            required_coverage = [has_minima.get(("russian_transcription", category), False) for category in ("clean", "noisy", "overlap", "long_form")]
            required_coverage.append(all_qualification_cases_observed)
            measured_failure = all_qualification_cases_observed and (wer > 0.15 or cer > 0.08)
        elif gate_id == "critical_business_fragments":
            critical_categories = ("amount_currency", "quantity", "date_deadline", "unit", "negation", "obligation", "permission", "condition", "constraint")
            required_coverage = [has_minima.get(("critical_business_fragments", category), False) for category in critical_categories]
            required_coverage.append(all_qualification_cases_observed)
            required_coverage.append(critical_localization_complete)
            measured_failure = all_qualification_cases_observed and critical_localization_complete and bool(critical_total) and critical_exact / critical_total < 0.98
        elif gate_id == "adversarial_semantic_inversions":
            required_coverage = [has_minima.get(("adversarial_semantic_inversions", category), False) for category in ("negation", "bounds", "permission", "inclusion", "obligation")]
            measured_failure = semantic_inversions > 0
            required_coverage.append(adversarial_observed == adversarial_total and adversarial_total > 0)
            required_coverage.append(adversarial_localization_complete)
        elif gate_id == "terminology":
            required_coverage = [has_minima.get(("terminology", "all_annotated_terms"), False)]
            required_coverage.append(all_qualification_cases_observed)
            measured_failure = all_qualification_cases_observed and bool(term_total) and (
                term_exact / term_total < 0.95
                or (term_predictions > 0 and term_exact / term_predictions < 0.95)
            )
            required_coverage.append(terminology_localization_complete)
        elif gate_id == "segment_timestamps":
            required_coverage = [has_minima.get(("segment_timestamps", category), False) for category in ("boundary_error", "alignment_coverage")]
            required_coverage.append(timestamp_alignment_complete)
            measured_failure = bool(timestamp_errors) and (statistics.median(timestamp_errors) > 500 or sorted(timestamp_errors)[min(len(timestamp_errors)-1, round((len(timestamp_errors)-1)*0.95))] > 1500)
            required_coverage.append(all(item.timestamps_supported for item in observations))
        elif gate_id == "silence":
            required_coverage = [has_minima.get(("silence", category), False) for category in ("pure_silence", "verified_silence_intervals")]
            required_coverage.append(all_qualification_cases_observed)
            measured_failure = all_qualification_cases_observed and bool(silence_words + silence_segments)
        elif gate_id == "long_form_chunking":
            chunk_categories = ("chunk_boundaries", "pause_handling", "reassembly", "partial_recovery")
            required_coverage = [has_minima.get(("long_form_chunking", category), False) for category in chunk_categories]
            required_coverage.extend((
                long_form_chunks_observed,
                long_form_pause_handling_observed,
                all(item is not None and item.missing_reference_segments == 0 for item in long_form_observations.values()),
                long_form_recovery_observed,
            ))
            measured_failure = False
        elif gate_id == "preprocessing_effects":
            required_coverage = [
                has_minima.get(("preprocessing_effects", "baseline"), False),
                has_minima.get(("preprocessing_effects", "preprocessing_comparison"), False),
                preprocessing_baseline_observed,
                preprocessing_comparison_observed,
            ]
            measured_failure = False
        elif gate_id == "performance":
            performance_categories = ("cold_start", "warm_inference", "preprocessing", "inference", "reassembly", "end_to_end", "resources")
            required_coverage = [performance_observed and all_qualification_cases_observed for _ in performance_categories]
            measured_failure = False
        elif gate_id == "offline_integrity":
            formats_present = {item.input_format for item in observations}
            required_coverage = [
                offline_integrity_observed
                and has_minima.get(("offline_integrity", category), False)
                and expected_format in formats_present
                for category, expected_format in (
                    ("supported_wav_pcm", "wav_pcm"),
                    ("supported_mp3", "mp3_layer_iii"),
                    ("supported_m4a", "m4a_aac_lc"),
                )
            ]
            required_coverage.extend(
                offline_integrity_observed
                for _ in ("asset_loading", "source_provenance", "dependency_provenance", "network_egress")
            )
            measured_failure = False
        else:
            required_coverage = []
            measured_failure = False
        status = QualificationStatus.FAIL if measured_failure else QualificationStatus.PASS if required_coverage and all(required_coverage) else QualificationStatus.NOT_VERIFIED
        gate_results.append(GateResult(
            gate_id=gate_id,
            categories=categories,
            status=status,
            error_count=None,
            coverage=Coverage(annotated=sum(1 for value in required_coverage if value), eligible=len(required_coverage)),
            observed_value=None,
            threshold=threshold,
            detail=(
                "Long-form chunks and recovery expectations were checked from real synthetic inference outputs; recovery is an annotation-driven replay, not a production worker failure."
                if gate_id == "long_form_chunking" and status is QualificationStatus.PASS
                else "Preprocessing comparison uses real per-format transcriptions and byte-verified deterministic outputs."
                if gate_id == "preprocessing_effects" and status is QualificationStatus.PASS
                else "Independent #661 gate evaluated; missing or insufficient category coverage remains NOT_VERIFIED."
            ),
        ))
    return metrics, tuple(gate_results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--dataset-assets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    report = run_preflight_from_file(arguments.manifest, arguments.dataset_assets)
    digest = save_immutable_report(report, arguments.output)
    print(json.dumps({"report": arguments.output.name, "sha256": digest, "status": report.overall_status.value}))


__all__ = ["main", "preflight_candidate", "run_preflight_from_file", "save_immutable_report", "_reassemble_segments"]


if __name__ == "__main__":
    main()
