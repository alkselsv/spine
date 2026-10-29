"""Contract and repeatability tests for the synthetic Russian transcription eval."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from spine.evals.transcription_russian_v1 import (
    RussianTranscriptionDataset,
    load_russian_transcription_v1_dataset,
    run_russian_transcription_v1,
)


def test_versioned_dataset_covers_required_synthetic_scenarios() -> None:
    dataset = load_russian_transcription_v1_dataset()

    assert dataset.version == "1.0.0"
    assert {case.kind for case in dataset.cases} == {
        "short_speech", "long_speech", "pause", "noise", "silence",
        "corruption", "multiple_speakers", "malicious_instruction",
    }
    assert all(case.synthetic_only and not case.external_call_allowed for case in dataset.cases)
    assert all(case.anonymous_speaker_policy == "labels_only_never_resolve_identity" for case in dataset.cases)


def test_fake_baseline_is_repeatable_and_reports_asr_separately_from_requirements() -> None:
    first = run_russian_transcription_v1()
    second = run_russian_transcription_v1()

    assert first == second
    assert first.dataset_sha256
    assert first.model_version == "synthetic-fake-asr:v1"
    assert first.config_version == "ru-meeting-fake-config:v1"
    assert set(first.asr) == {"wer", "cer", "exact_transcript_rate"}
    assert set(first.requirements) == {
        "precision", "recall", "f1", "evidence_precision", "evidence_recall", "outcome_accuracy"
    }
    assert first.asr["wer"].value > 0
    assert first.requirements["evidence_precision"].value == 1
    assert first.requirements["evidence_recall"].value < 1


def test_dataset_rejects_fabricated_evidence_and_invalid_time_ranges() -> None:
    raw = deepcopy(load_russian_transcription_v1_dataset().model_dump(mode="json"))
    raw["cases"][0]["requirement_labels"][0]["evidence_segment_ids"] = ["made-up-segment"]

    with pytest.raises(ValidationError, match="fabricated segment"):
        RussianTranscriptionDataset.model_validate(raw)

    raw = deepcopy(load_russian_transcription_v1_dataset().model_dump(mode="json"))
    raw["cases"][0]["reference_segments"][0]["end_ms"] = 5000
    with pytest.raises(ValidationError, match="exceeds audio duration"):
        RussianTranscriptionDataset.model_validate(raw)


def test_dataset_rejects_private_fixture_markers_and_actionable_attack_evidence() -> None:
    raw = deepcopy(load_russian_transcription_v1_dataset().model_dump(mode="json"))
    raw["cases"][0]["reference_segments"][0]["reference_text"] = "Свяжитесь с qa@example.test"
    raw["cases"][0]["expected_transcript"] = "Свяжитесь с qa@example.test"
    raw["cases"][0]["audio_sha256"] = __import__("hashlib").sha256(
        raw["cases"][0]["synthetic_recipe"].encode()
    ).hexdigest()
    with pytest.raises(ValidationError, match="private-data marker"):
        RussianTranscriptionDataset.model_validate(raw)

    raw = deepcopy(load_russian_transcription_v1_dataset().model_dump(mode="json"))
    attack = next(case for case in raw["cases"] if case["kind"] == "malicious_instruction")
    attack["requirement_labels"][0]["evidence_segment_ids"] = ["seg-attack"]
    with pytest.raises(ValidationError, match="cannot support requirements"):
        RussianTranscriptionDataset.model_validate(raw)
