"""Offline contract tests for Issue #683 ASR candidate qualification."""

from __future__ import annotations

import pytest

from spine.evals.asr_candidate_qualification import (
    ASR_CANDIDATE_QUALIFICATION_SCHEMA,
    CandidateQualificationReport,
    QualificationStatus,
    candidate_profiles,
    run_offline_candidate_qualification,
)


def test_candidate_profiles_pin_independent_model_identities_and_config_digests() -> (
    None
):
    profiles = candidate_profiles()

    assert {profile.candidate_id for profile in profiles} == {
        "gigaam-v3-rnnt",
        "whisper-large-v3-faster-whisper",
    }
    assert {profile.language for profile in profiles} == {"ru"}
    assert all(not profile.translation_enabled for profile in profiles)
    assert all(not profile.language_detection_enabled for profile in profiles)
    assert (
        len({profile.configuration.configuration_digest for profile in profiles}) == 2
    )
    assert all(
        len(profile.configuration.configuration_digest) == 64 for profile in profiles
    )


def test_offline_qualification_reports_each_candidate_and_every_independent_gate() -> (
    None
):
    reports = run_offline_candidate_qualification()

    assert len(reports) == 2
    assert all(
        report.schema_version == ASR_CANDIDATE_QUALIFICATION_SCHEMA
        for report in reports
    )
    assert {report.candidate.candidate_id for report in reports} == {
        "gigaam-v3-rnnt",
        "whisper-large-v3-faster-whisper",
    }
    assert all(
        report.overall_status is QualificationStatus.NOT_VERIFIED for report in reports
    )
    assert all(
        {gate.gate_id for gate in report.gates}
        == {
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
        for report in reports
    )
    assert all(
        gate.status is QualificationStatus.NOT_VERIFIED
        and gate.error_count is None
        and gate.coverage.annotated == 0
        and gate.coverage.eligible == 0
        for report in reports
        for gate in report.gates
    )


def test_offline_qualification_keeps_asr_and_downstream_metrics_separate() -> None:
    reports = run_offline_candidate_qualification()

    expected_metrics = {
        "wer",
        "cer",
        "critical_fragment_accuracy",
        "semantic_inversions",
        "terminology_precision",
        "terminology_recall",
        "segment_timestamp_p50_ms",
        "segment_timestamp_p95_ms",
        "silence_hallucinations",
        "preprocessing_ms",
        "inference_ms",
        "reassembly_ms",
        "cold_start_ms",
        "warm_inference_rtf",
        "end_to_end_rtf",
        "peak_ram_mb",
        "peak_vram_mb",
    }
    for report in reports:
        assert set(report.metrics) == expected_metrics
        assert all(
            metric.status is QualificationStatus.NOT_VERIFIED
            for metric in report.metrics.values()
        )
        assert "requirements extraction" in report.downstream_boundary.lower()


def test_report_rejects_production_or_external_execution_claims() -> None:
    report = run_offline_candidate_qualification()[0]
    payload = report.model_dump(mode="json")

    payload["production_recordings_used"] = True
    with pytest.raises(ValueError, match="production recordings"):
        CandidateQualificationReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["external_asr_calls"] = True
    with pytest.raises(ValueError, match="external ASR"):
        CandidateQualificationReport.model_validate(payload)


def test_report_serialization_is_repeatable_and_preserves_not_verified_boundary() -> (
    None
):
    first = run_offline_candidate_qualification()
    second = run_offline_candidate_qualification()

    assert first == second
    assert [report.model_dump(mode="json") for report in first] == [
        report.model_dump(mode="json") for report in second
    ]
    assert all("PASS" not in report.model_dump_json() for report in first)
