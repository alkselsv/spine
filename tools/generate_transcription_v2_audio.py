"""Rebuild v2 format/preprocessing fixtures from frozen synthetic v1 audio.

This tool makes only local copies and FFmpeg conversions. It never synthesizes
speech, fetches software or downloads model/voice assets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


DATA_ROOT = Path(__file__).parents[1] / "src/spine/evals/data"
DATASET_ID = "russian-meeting-transcription-synthetic"
DATASET_VERSION = "2.0.0"
BASELINE_SHA256 = "0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7"
_SPLITS = {
    "short-request": "qualification",
    "long-meeting": "qualification",
    "pause-in-turn": "development",
    "background-noise": "development",
    "pure-silence": "qualification",
    "corrupt-media": "development",
    "weak-speech": "development",
    "anonymous-speakers": "qualification",
    "spoken-prompt-injection": "qualification",
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run_ffmpeg(executable: str, source: Path, target: Path, *, codec: str | None, duration_ms: int) -> None:
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:a:0", "-map_metadata", "-1", "-ac", "1"]
    if codec == "mp3":
        command += ["-codec:a", "libmp3lame", "-b:a", "64k", "-write_xing", "0", "-id3v2_version", "0"]
    elif codec == "aac":
        command += ["-codec:a", "aac", "-profile:a", "aac_low", "-b:a", "64k"]
    else:
        command += ["-t", f"{duration_ms / 1000:.3f}", "-ar", "16000", "-codec:a", "pcm_s16le", "-f", "wav"]
    command.append(str(target))
    subprocess.run(command, check=True, capture_output=True, timeout=60)


def _segments(case: dict[str, object], prefix: str) -> list[dict[str, object]]:
    return [
        {
            "segment_id": f"{prefix}-{segment['segment_id']}",
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "reference_text": segment["reference_text"],
            "permissible_variants": segment["permissible_variants"],
            "anonymous_speaker": segment.get("anonymous_speaker") or "speaker-a",
        }
        for segment in case["reference_segments"]
    ]


def _find_span(segment_id: str, text: str, category: str, semantic_value: str) -> dict[str, object]:
    start = text.index(category)
    category_id = semantic_value.split(":", 1)[0]
    return {
        "annotation_id": f"critical-{segment_id}-{category_id}-{start}",
        "segment_id": segment_id,
        "category": category_id,
        "start_char": start,
        "end_char": start + len(category),
        "expected_text": category,
        "semantic_value": semantic_value.split(":", 1)[1],
    }


def _annotations(case_id: str, case: dict[str, object], segments: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    critical: list[dict[str, object]] = []
    adversarial: list[dict[str, object]] = []
    terminology: list[dict[str, object]] = []
    by_old_id = {old["segment_id"]: new for old, new in zip(case["reference_segments"], segments, strict=True)}
    span_specs = {
        "long-meeting": [("seg-long-1", "Подготовим", "obligation:team undertakes to prepare the summary"), ("seg-long-1", "после проверки данных", "condition:prepare only after data verification"), ("seg-long-2", "понедельник", "date_deadline:Monday")],
        "short-request": [("seg-short-1", "Нужен", "obligation:the report is needed"), ("seg-short-1", "пятнице", "date_deadline:Friday")],
        "anonymous-speakers": [("seg-speaker-1", "должен", "obligation:the mockup must work offline"), ("seg-speaker-1", "без сети", "negation:network access is not required"), ("seg-speaker-1", "без сети", "constraint:the mockup must function without a network")],
    }
    for old_id, expected, semantic in span_specs.get(case_id, []):
        segment = by_old_id[old_id]
        critical.append(_find_span(segment["segment_id"], segment["reference_text"], expected, semantic))
    if case_id == "anonymous-speakers":
        segment = by_old_id["seg-speaker-1"]
        start = segment["reference_text"].index("без сети")
        adversarial.append({
            "annotation_id": "inversion-network-negation",
            "segment_id": segment["segment_id"],
            "dimension": "negation",
            "start_char": start,
            "end_char": start + len("без сети"),
            "expected_text": "без сети",
            "reference_interpretation": "The mockup must work without network access.",
            "forbidden_inversion": "The mockup requires network access.",
        })
        obligation_start = segment["reference_text"].index("должен")
        adversarial.append({
            "annotation_id": "inversion-obligation-modality",
            "segment_id": segment["segment_id"],
            "dimension": "obligation",
            "start_char": obligation_start,
            "end_char": obligation_start + len("должен"),
            "expected_text": "должен",
            "reference_interpretation": "Network-independent operation is mandatory.",
            "forbidden_inversion": "Network-independent operation is optional.",
        })
        segment = by_old_id["seg-speaker-2"]
        expected = "тестовом стенде"
        start = segment["reference_text"].index(expected)
        terminology.append({
            "annotation_id": "term-test-environment",
            "segment_id": segment["segment_id"],
            "start_char": start,
            "end_char": start + len(expected),
            "expected_text": expected,
            "canonical_term": "тестовый стенд",
            "permissible_variants": ["на тестовом стенде"],
            "term_category": "test_environment",
        })
    return critical, adversarial, terminology


def _silence(case_id: str, segments: list[dict[str, object]], duration_ms: int) -> list[dict[str, int]]:
    if case_id == "pure-silence":
        return [{"start_ms": 0, "end_ms": duration_ms}]
    if case_id == "long-meeting":
        return [{"start_ms": 4000, "end_ms": 77000}, {"start_ms": 81000, "end_ms": duration_ms}]
    if case_id == "anonymous-speakers":
        return [{"start_ms": 3000, "end_ms": 7000}, {"start_ms": 10000, "end_ms": duration_ms}]
    if case_id == "spoken-prompt-injection":
        return [{"start_ms": 3500, "end_ms": 5500}, {"start_ms": 7800, "end_ms": duration_ms}]
    return []


def _chunks(duration_ms: int, segments: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    chunks = []
    start = 0
    while start < duration_ms:
        end = min(start + 25_000, duration_ms)
        chunks.append({
            "chunk_id": f"chunk-{len(chunks) + 1}",
            "start_ms": start,
            "end_ms": end,
            "overlap_before_ms": 0,
            "expected_outcome": "complete",
            "failure_after_segment_id": None,
        })
        start = end
    if chunks and segments:
        failed = chunks[0]
        failed["expected_outcome"] = "partial_failure"
        failed["failure_after_segment_id"] = segments[0]["segment_id"]
        recovery = [{
            "recovery_id": "recovery-after-first-chunk",
            "failed_chunk_id": failed["chunk_id"],
            "completed_segment_ids": [segments[0]["segment_id"]],
            "resume_from_chunk_id": chunks[1]["chunk_id"] if len(chunks) > 1 else failed["chunk_id"],
            "expected_final_segment_ids": [segment["segment_id"] for segment in segments],
        }]
        return chunks, recovery
    return chunks, []


def _coverage(cases: list[dict[str, object]], required: list[tuple[str, str]]) -> list[dict[str, object]]:
    from spine.evals.transcription_russian_v2 import (
        SyntheticQualificationCase,
        eligibility_rule_for,
        expected_eligible_case_ids,
    )
    typed_cases = tuple(SyntheticQualificationCase.model_validate(case) for case in cases)
    coverage = []
    for gate_id, category_id in required:
        eligible = sorted(expected_eligible_case_ids(gate_id, category_id, typed_cases))
        annotated = []
        for case in cases:
            if case["split"] != "qualification":
                continue
            if gate_id == "critical_business_fragments" and any(item["category"] == category_id for item in case["critical_spans"]):
                annotated.append(case["case_id"])
            elif gate_id == "adversarial_semantic_inversions" and any(item["dimension"] == category_id for item in case["adversarial_labels"]):
                annotated.append(case["case_id"])
            elif gate_id == "terminology" and category_id == "all_annotated_terms" and case["terminology_spans"]:
                annotated.append(case["case_id"])
            elif gate_id == "segment_timestamps" and case["reference_segments"]:
                annotated.append(case["case_id"])
            elif gate_id == "silence" and category_id == "pure_silence" and case["media_category"] == "silence":
                annotated.append(case["case_id"])
            elif gate_id == "silence" and category_id == "verified_silence_intervals" and case["verified_silence_intervals"]:
                annotated.append(case["case_id"])
            elif gate_id == "long_form_chunking" and category_id in {"chunk_boundaries", "pause_handling", "reassembly"} and case["chunks"]:
                annotated.append(case["case_id"])
            elif gate_id == "long_form_chunking" and category_id == "partial_recovery" and case["chunk_recoveries"]:
                annotated.append(case["case_id"])
            elif gate_id == "preprocessing_effects" and category_id == "baseline" and case["preprocessing_pair_id"]:
                annotated.append(case["case_id"])
            elif gate_id == "preprocessing_effects" and category_id == "preprocessing_comparison" and case["preprocessing"]["output_audio_sha256"]:
                annotated.append(case["case_id"])
            elif gate_id == "offline_integrity" and category_id == "supported_wav_pcm" and (case["audio"]["media_format"] == "wav_pcm" or any(variant["media_format"] == "wav_pcm" for variant in case["format_variants"])):
                annotated.append(case["case_id"])
            elif gate_id == "offline_integrity" and category_id == "supported_mp3" and (case["audio"]["media_format"] == "mp3_layer_iii" or any(variant["media_format"] == "mp3_layer_iii" for variant in case["format_variants"])):
                annotated.append(case["case_id"])
            elif gate_id == "offline_integrity" and category_id == "supported_m4a" and (case["audio"]["media_format"] == "m4a_aac_lc" or any(variant["media_format"] == "m4a_aac_lc" for variant in case["format_variants"])):
                annotated.append(case["case_id"])
            elif gate_id == "russian_transcription" and case["media_category"] == category_id and case["reference_segments"]:
                annotated.append(case["case_id"])
        eligible = list(dict.fromkeys(eligible))
        annotated = list(dict.fromkeys(annotated))
        coverage.append({
            "gate_id": gate_id,
            "category_id": category_id,
            "eligibility_rule": eligibility_rule_for(gate_id, category_id),
            "eligible_case_ids": eligible,
            "annotated_case_ids": annotated,
            "minimum_annotation_count": None,
            "minimum_decision_reference": None,
        })
    return coverage


def generate(data_root: Path) -> None:
    source_manifest = json.loads((data_root / "russian_transcription_v1.json").read_text(encoding="utf-8"))
    baseline_bytes = (data_root / "russian_transcription_v1.json").read_bytes()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg must already be installed; this generator does not install it")
    ffmpeg_version = subprocess.run([ffmpeg, "-version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    ffmpeg_sha256 = _sha256(Path(ffmpeg).read_bytes())
    audio_root = data_root / "audio"
    cases: list[dict[str, object]] = []
    source_cases = {case["case_id"]: case for case in source_manifest["cases"]}
    media_inputs: list[tuple[dict[str, object], str, str, str | None, int]] = []
    variant_assets: dict[str, list[dict[str, object]]] = {}
    for source in source_manifest["cases"]:
        source_path = data_root / source["audio_fixture_path"]
        target_name = f"v2-{source['case_id']}.wav"
        target_path = audio_root / target_name
        target_path.write_bytes(source_path.read_bytes())
        media_inputs.append((source, f"audio/{target_name}", "wav_pcm", None, 22050))
    short_case = source_cases["short-request"]
    short_path = data_root / short_case["audio_fixture_path"]
    variant_assets["short-request"] = []
    for extension, codec, media_format in (("mp3", "mp3", "mp3_layer_iii"), ("m4a", "aac", "m4a_aac_lc")):
        target_name = f"v2-short-request.{extension}"
        target_path = audio_root / target_name
        _run_ffmpeg(ffmpeg, short_path, target_path, codec=codec, duration_ms=short_case["duration_ms"])
        variant_assets["short-request"].append({
            "fixture_path": f"audio/{target_name}",
            "sha256": _sha256(target_path.read_bytes()),
            "media_format": media_format,
            "source_sample_rate_hz": 22050,
            "channels": 1,
            "sample_width_bits": 0,
            "derived_from_sha256": short_case["audio_sha256"],
        })
    variant_preprocessing: dict[str, list[dict[str, object]]] = {"short-request": []}
    for variant in variant_assets["short-request"]:
        variant_source = audio_root / Path(variant["fixture_path"]).name
        suffix = "mp3" if variant["media_format"] == "mp3_layer_iii" else "m4a"
        output_name = f"v2-short-request-{suffix}-16k.wav"
        output_target = audio_root / output_name
        _run_ffmpeg(ffmpeg, variant_source, output_target, codec=None, duration_ms=short_case["duration_ms"])
        variant_preprocessing["short-request"].append({
            "profile_id": "ffmpeg-mono-16khz-pcm-s16le:v1",
            "target_sample_rate_hz": 16000,
            "target_channels": 1,
            "source_audio_sha256": variant["sha256"],
            "output_audio_sha256": _sha256(output_target.read_bytes()),
            "derived_audio_path": f"audio/{output_name}",
        })
    for source, fixture_path, media_format, source_hash, sample_rate in media_inputs:
        case_id = f"v2-{source['case_id']}"
        payload = (data_root / fixture_path).read_bytes()
        digest = _sha256(payload)
        segments = _segments(source, case_id)
        critical, adversarial, terminology = _annotations(source["case_id"], source, segments)
        corrupt = source["kind"] == "corruption"
        output_path: str | None = None
        output_hash: str | None = None
        if not corrupt:
            output_name = f"{case_id}-16k.wav"
            output_target = audio_root / output_name
            _run_ffmpeg(ffmpeg, data_root / fixture_path, output_target, codec=None, duration_ms=source["duration_ms"])
            output_path = f"audio/{output_name}"
            output_hash = _sha256(output_target.read_bytes())
        media_category = {
            "short_speech": "clean", "long_speech": "long_form", "pause": "clean",
            "noise": "noisy", "silence": "silence", "corruption": "corruption",
            "weak_speech": "weak_speech", "multiple_speakers": "clean",
            "malicious_instruction": "clean",
        }[source["kind"]]
        chunks: list[dict[str, object]] = []
        recoveries: list[dict[str, object]] = []
        if source["kind"] == "long_speech":
            chunks, recoveries = _chunks(source["duration_ms"], segments)
        generation = {
            "recipe": f"Inherited immutable #674 fixture {source['case_id']} ({source['synthetic_recipe']}); format conversion pinned to {ffmpeg_version} ({ffmpeg_sha256}).",
            "voice_id": "inherited-v1-anonymous-speaker-label",
            "voice_asset_sha256": None,
            "synthesizer_revision": "piper-tts:1.8.0-inherited-v1-audio",
            "license_reference": "See docs/qualification/russian-transcription-v1.md; exact voice weight hashes remain unverified.",
            "source_dataset_id": DATASET_ID,
            "source_dataset_version": "1.0.0",
            "source_fixture_sha256": source["audio_sha256"],
        }
        case = {
            "case_id": case_id,
            "split": _SPLITS[source["case_id"]],
            "lineage_group_id": f"source-{source['case_id']}",
            "duration_ms": source["duration_ms"],
            "audio": {
                "fixture_path": fixture_path,
                "sha256": digest,
                "media_format": media_format,
                "source_sample_rate_hz": sample_rate,
                "channels": 1,
                "sample_width_bits": 16 if media_format == "wav_pcm" else 0,
                "derived_from_sha256": source_hash,
            },
            "format_variants": variant_assets.get(source["case_id"], []),
            "format_variant_preprocessing": variant_preprocessing.get(source["case_id"], []),
            "generation": generation,
            "preprocessing": {
                "profile_id": "ffmpeg-mono-16khz-pcm-s16le:v1",
                "target_sample_rate_hz": 16000,
                "target_channels": 1,
                "source_audio_sha256": digest,
                "output_audio_sha256": output_hash,
                "derived_audio_path": output_path,
            },
            "expected_transcript": " ".join(segment["reference_text"] for segment in segments),
            "reference_segments": segments,
            "critical_spans": critical,
            "adversarial_labels": adversarial,
            "terminology_spans": terminology,
            "verified_silence_intervals": _silence(source["case_id"], segments, source["duration_ms"]),
            "chunks": chunks,
            "chunk_recoveries": recoveries,
            "media_category": media_category,
            "preprocessing_pair_id": "short-request-format-family" if source["case_id"] == "short-request" else None,
        }
        cases.append(case)
    from spine.evals.transcription_russian_v2 import (
        RussianTranscriptionDatasetV2,
        required_gate_categories,
        transcription_dataset_v2_sha256,
    )
    payload = {
        "dataset_id": DATASET_ID,
        "version": DATASET_VERSION,
        "schema_version": "russian-transcription-qualification:v2",
        "asset_digest_algorithm": "sha256",
        "manifest_digest_algorithm": "sha256-canonical-json-v1",
        "timestamp_coordinate_system": "source_audio_milliseconds",
        "text_span_coordinate_system": "unicode_codepoint_offsets",
        "baseline_dataset_id": DATASET_ID,
        "baseline_version": "1.0.0",
        "baseline_sha256": BASELINE_SHA256,
        "generation_recipe_version": "transcription-v2-local-ffmpeg:v1",
        "cases": cases,
        "coverage": _coverage(cases, required_gate_categories()),
    }
    if _sha256(baseline_bytes) != _sha256((data_root / "russian_transcription_v1.json").read_bytes()):
        raise RuntimeError("the frozen v1 baseline changed during dataset generation")
    dataset = RussianTranscriptionDatasetV2.model_validate(payload)
    serialized = json.dumps(dataset.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    (data_root / "russian_transcription_v2.json").write_text(serialized, encoding="utf-8")
    digest = transcription_dataset_v2_sha256(dataset)
    (data_root / "russian_transcription_v2.sha256").write_text(f"{digest}  russian_transcription_v2.json\n", encoding="ascii")
    print(f"dataset={DATASET_ID}@{DATASET_VERSION} sha256={digest} cases={len(cases)} ffmpeg={ffmpeg_version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    arguments = parser.parse_args()
    generate(arguments.data_root)


if __name__ == "__main__":
    main()
