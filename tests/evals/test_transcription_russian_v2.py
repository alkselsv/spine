from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import wave

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from spine.evals.transcription_russian_v2 import (
    SyntheticQualificationCase,
    RussianTranscriptionDatasetV2,
    GateCoverageCategory,
    build_gate_coverage_report,
    evaluate_gate_coverage,
    load_russian_transcription_v2_dataset,
    required_gate_categories,
    eligibility_rule_for,
    expected_eligible_case_ids,
    _annotated_case_ids,
    validate_transcription_v2_assets,
)
from spine.evals import transcription_russian_v2 as transcription_v2
from spine.evals.transcription_russian_v1 import (
    canonical_russian_transcription_dataset_json,
    load_russian_transcription_v1_dataset,
)


def _dataset_payload() -> dict[str, object]:
    payload = {
        "dataset_id": "russian-meeting-transcription-synthetic",
        "version": "2.0.0",
        "schema_version": "russian-transcription-qualification:v2",
        "asset_digest_algorithm": "sha256",
        "manifest_digest_algorithm": "sha256-canonical-json-v1",
        "timestamp_coordinate_system": "source_audio_milliseconds",
        "text_span_coordinate_system": "unicode_codepoint_offsets",
        "baseline_dataset_id": "russian-meeting-transcription-synthetic",
        "baseline_version": "1.0.0",
        "baseline_sha256": "0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7",
        "generation_recipe_version": "piper-tts-1.8.0-local-assets:v1",
        "cases": [
            {
                "case_id": "case-money",
                "split": "qualification",
                "lineage_group_id": "utterance-money",
                "duration_ms": 5000,
                "audio": {
                    "fixture_path": "audio/case-money.wav",
                    "sha256": "1" * 64,
                    "media_format": "wav_pcm",
                    "source_sample_rate_hz": 22050,
                    "channels": 1,
                    "sample_width_bits": 16,
                    "derived_from_sha256": None,
                },
                "generation": {
                    "recipe": "piper-tts:1.8.0; voice:ru_RU-denis-medium; seed:0",
                    "voice_id": "ru_RU-denis-medium",
                    "voice_asset_sha256": "2" * 64,
                    "synthesizer_revision": "piper-tts:1.8.0",
                    "license_reference": "approved synthetic fixture voice license record",
                    "source_dataset_id": "russian-meeting-transcription-synthetic",
                    "source_dataset_version": "1.0.0",
                    "source_fixture_sha256": "1" * 64,
                },
                "preprocessing": {
                    "profile_id": "pcm-s16le-mono-16khz:v1",
                    "target_sample_rate_hz": 16000,
                    "target_channels": 1,
                    "source_audio_sha256": "1" * 64,
                    "output_audio_sha256": "3" * 64,
                    "derived_audio_path": "audio/case-money-16k.wav",
                },
                "reference_segments": [
                    {
                        "segment_id": "seg-money",
                        "start_ms": 500,
                        "end_ms": 3200,
                        "reference_text": "Сумма составит сто рублей",
                        "permissible_variants": ["Итоговая сумма составит сто рублей"],
                        "anonymous_speaker": "speaker-a",
                    }
                ],
                "expected_transcript": "Сумма составит сто рублей",
                "critical_spans": [
                    {
                        "annotation_id": "critical-amount",
                        "segment_id": "seg-money",
                        "category": "amount_currency",
                        "start_char": 15,
                        "end_char": 25,
                        "expected_text": "сто рублей",
                        "semantic_value": "100 RUB",
                    }
                ],
                "adversarial_labels": [],
                "terminology_spans": [],
                "verified_silence_intervals": [
                    {"start_ms": 0, "end_ms": 400},
                    {"start_ms": 3300, "end_ms": 5000},
                ],
                "chunks": [
                    {
                        "chunk_id": "chunk-1",
                        "start_ms": 0,
                        "end_ms": 5000,
                        "overlap_before_ms": 0,
                        "expected_outcome": "complete",
                    }
                ],
                "media_category": "long_form",
            }
        ],
        "coverage": [
            {
                "gate_id": gate_id,
                "category_id": category_id,
                "eligibility_rule": f"Fixture eligibility rule for {gate_id}/{category_id}.",
                "eligible_case_ids": ["case-money"] if (
                    category_id == "amount_currency"
                    or (gate_id == "russian_transcription" and category_id == "clean")
                    or (gate_id == "segment_timestamps" and category_id in {"boundary_error", "alignment_coverage"})
                    or (gate_id == "silence" and category_id == "verified_silence_intervals")
                    or (gate_id == "long_form_chunking" and category_id in {"chunk_boundaries", "pause_handling", "reassembly"})
                    or (gate_id == "offline_integrity" and category_id == "supported_wav_pcm")
                    or (gate_id == "preprocessing_effects" and category_id == "preprocessing_comparison")
                ) else [],
                "annotated_case_ids": ["case-money"] if (
                    category_id == "amount_currency"
                    or (gate_id == "russian_transcription" and category_id == "clean")
                    or (gate_id == "segment_timestamps" and category_id in {"boundary_error", "alignment_coverage"})
                    or (gate_id == "silence" and category_id == "verified_silence_intervals")
                    or (gate_id == "long_form_chunking" and category_id in {"chunk_boundaries", "pause_handling", "reassembly"})
                    or (gate_id == "offline_integrity" and category_id == "supported_wav_pcm")
                    or (gate_id == "preprocessing_effects" and category_id == "preprocessing_comparison")
                ) else [],
                "minimum_annotation_count": None,
                "minimum_decision_reference": None,
            }
            for gate_id, category_id in required_gate_categories()
        ],
    }
    qualification_case = SyntheticQualificationCase.model_validate(payload["cases"][0])  # type: ignore[index]
    for item in payload["coverage"]:  # type: ignore[union-attr]
        item["eligibility_rule"] = eligibility_rule_for(item["gate_id"], item["category_id"])
        item["eligible_case_ids"] = sorted(
            expected_eligible_case_ids(item["gate_id"], item["category_id"], (qualification_case,))
        )
        item["annotated_case_ids"] = sorted(
            _annotated_case_ids(item["gate_id"], item["category_id"], (qualification_case,))
        )
    development_case = deepcopy(payload["cases"][0])  # type: ignore[index]
    development_case["case_id"] = "case-money-development"
    development_case["split"] = "development"
    development_case["lineage_group_id"] = "utterance-money-development"
    development_case["audio"]["fixture_path"] = "audio/case-money-development.wav"
    development_case["audio"]["sha256"] = "4" * 64
    development_case["preprocessing"]["source_audio_sha256"] = "4" * 64
    payload["cases"].append(development_case)  # type: ignore[union-attr]
    return payload


def test_v2_dataset_validates_typed_annotations_and_keeps_coverage_unverified() -> None:
    dataset = RussianTranscriptionDatasetV2.model_validate(_dataset_payload())

    assert dataset.version == "2.0.0"
    assert dataset.cases[0].critical_spans[0].expected_text == "сто рублей"
    assert len(dataset.coverage) == 43
    assert next(
        result for result in evaluate_gate_coverage(dataset.coverage)
        if result.category_id == "amount_currency"
    ).status.value == "NOT_VERIFIED"


def test_v2_dataset_rejects_critical_span_that_does_not_match_ground_truth() -> None:
    payload = _dataset_payload()
    payload["cases"][0]["critical_spans"][0]["expected_text"] = "двадцать рублей"  # type: ignore[index]

    with pytest.raises(ValidationError, match="span text must match"):
        RussianTranscriptionDatasetV2.model_validate(payload)


def test_v2_dataset_rejects_conflicting_annotations_for_the_same_span() -> None:
    payload = _dataset_payload()
    conflicting = deepcopy(payload["cases"][0]["critical_spans"][0])  # type: ignore[index]
    conflicting["annotation_id"] = "critical-conflict"
    conflicting["semantic_value"] = "200 RUB"
    payload["cases"][0]["critical_spans"].append(conflicting)  # type: ignore[index]

    with pytest.raises(ValidationError, match="conflicting critical span annotation"):
        RussianTranscriptionDatasetV2.model_validate(payload)


def test_v2_dataset_rejects_derived_audio_leakage_across_splits() -> None:
    payload = _dataset_payload()
    development_case = payload["cases"][1]  # type: ignore[index]
    development_case["lineage_group_id"] = "utterance-money"

    with pytest.raises(ValidationError, match="lineage group cannot cross splits"):
        RussianTranscriptionDatasetV2.model_validate(payload)


def test_v2_dataset_rejects_derived_audio_hash_reused_across_splits() -> None:
    payload = _dataset_payload()
    development_case = deepcopy(payload["cases"][0])  # type: ignore[index]
    development_case["case_id"] = "case-money-derived-development"
    development_case["split"] = "development"
    development_case["lineage_group_id"] = "independent-conversion-record"
    development_case["audio"]["fixture_path"] = "audio/case-money-derived-development.wav"
    development_case["audio"]["sha256"] = "5" * 64
    development_case["audio"]["derived_from_sha256"] = "1" * 64
    development_case["preprocessing"]["source_audio_sha256"] = "5" * 64
    development_case["generation"]["source_fixture_sha256"] = "1" * 64
    payload["cases"].append(development_case)  # type: ignore[union-attr]

    with pytest.raises(ValidationError, match="lineage group cannot cross splits"):
        RussianTranscriptionDatasetV2.model_validate(payload)


def test_v2_dataset_rejects_time_ranges_overlapping_verified_silence() -> None:
    payload = _dataset_payload()
    payload["cases"][0]["verified_silence_intervals"][0]["end_ms"] = 600  # type: ignore[index]

    with pytest.raises(ValidationError, match="silence interval overlaps speech"):
        RussianTranscriptionDatasetV2.model_validate(payload)


def test_v2_dataset_preserves_the_frozen_v1_canonical_digest() -> None:
    dataset = load_russian_transcription_v1_dataset()
    digest = hashlib.sha256(canonical_russian_transcription_dataset_json(dataset).encode()).hexdigest()

    assert digest == "0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7"


def test_gate_coverage_requires_a_protocol_owner_decision_for_numeric_minimum() -> None:
    with pytest.raises(ValidationError, match="attributable Protocol Owner decision"):
        GateCoverageCategory(
            gate_id="terminology",
            category_id="all_annotated_terms",
            eligibility_rule="A case is eligible when its transcript has a labelled term span.",
            eligible_case_ids=("case-money",),
            annotated_case_ids=("case-money",),
            minimum_annotation_count=5,
        )


def test_frozen_v2_dataset_has_offline_assets_and_reports_unapproved_minima() -> None:
    dataset = load_russian_transcription_v2_dataset()

    assert dataset.version == "2.0.0"
    assert len(dataset.cases) == 9
    assert len(dataset.coverage) == 43
    assert {case.split.value for case in dataset.cases} == {"development", "qualification"}
    assert {variant.media_format.value for case in dataset.cases for variant in case.format_variants} == {
        "mp3_layer_iii",
        "m4a_aac_lc",
    }
    assert all(result.status.value == "NOT_VERIFIED" for result in evaluate_gate_coverage(dataset.coverage))
    assert all(item.minimum_annotation_count is None for item in dataset.coverage)
    format_case = next(case for case in dataset.cases if case.format_variants)
    assert {item.source_audio_sha256 for item in format_case.format_variant_preprocessing} == {
        variant.sha256 for variant in format_case.format_variants
    }


def test_coverage_report_is_bound_to_the_frozen_dataset_and_never_claims_quality_pass() -> None:
    dataset = load_russian_transcription_v2_dataset()
    report = build_gate_coverage_report(dataset)

    assert report.dataset_id == dataset.dataset_id
    assert report.dataset_version == "2.0.0"
    assert report.dataset_sha256 == "cb3c36145df14a2cbe4880dd2e5f64424cb4d45cddb504e44833a03dd6931d83"
    assert len(report.coverage) == 43
    assert {item.status.value for item in report.coverage} == {"NOT_VERIFIED"}


def test_compressed_asset_validation_rejects_non_lc_aac_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(transcription_v2.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(
        transcription_v2.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"streams":[{"codec_name":"aac","profile":"HE-AAC","sample_rate":"22050","channels":1}],"format":{"duration":"4.2"}}'
        ),
    )

    with pytest.raises(ValueError, match="codec/stream mismatch"):
        transcription_v2._validate_compressed_asset(
            tmp_path / "fixture.m4a", transcription_v2.SyntheticMediaFormat.M4A_AAC_LC, 22050, 4200, "fixture"
        )


def test_verified_silence_validation_uses_declared_pcm_sample_width(tmp_path: Path) -> None:
    fixture = tmp_path / "silence-24bit.wav"
    with wave.open(str(fixture), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(3)
        audio.setframerate(1000)
        audio.writeframes(bytes([0, 0, 0] * 1000))

    transcription_v2._validate_verified_silence(
        fixture,
        (transcription_v2.TimeInterval(start_ms=500, end_ms=1000),),
        "silence-24bit",
    )


def test_v2_dataset_rejects_fabricated_coverage_case_ids() -> None:
    payload = _dataset_payload()
    coverage = next(
        item for item in payload["coverage"]
        if item["gate_id"] == "critical_business_fragments" and item["category_id"] == "amount_currency"
    )
    coverage["eligible_case_ids"] = ["made-up-case"]
    coverage["annotated_case_ids"] = []

    with pytest.raises(ValidationError, match="coverage references a fabricated eligible case"):
        RussianTranscriptionDatasetV2.model_validate(payload)


def test_v2_dataset_rejects_omitted_cases_required_by_eligibility_rule() -> None:
    payload = _dataset_payload()
    coverage = next(
        item for item in payload["coverage"]
        if item["gate_id"] == "russian_transcription" and item["category_id"] == "long_form"
    )
    coverage["eligible_case_ids"] = []
    coverage["annotated_case_ids"] = []

    with pytest.raises(ValidationError, match="eligible coverage does not match"):
        RussianTranscriptionDatasetV2.model_validate(payload)


def test_v2_dataset_rejects_candidate_hypotheses_in_ground_truth_manifest() -> None:
    payload = _dataset_payload()
    payload["cases"][0]["fake_asr_transcript"] = "Сумма составит сто рублей"  # type: ignore[index]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RussianTranscriptionDatasetV2.model_validate(payload)
