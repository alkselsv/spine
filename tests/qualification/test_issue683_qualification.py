"""Executable traceability checks for Issue #683 qualification evidence."""

from __future__ import annotations

import json
from pathlib import Path

from spine.evals.asr_candidate_qualification import (
    ASR_CANDIDATE_QUALIFICATION_SCHEMA,
    QualificationStatus,
    run_offline_candidate_qualification,
)

ROOT = Path(__file__).parents[2]
REPORT = ROOT / "docs/qualification/issue-683-asr-candidate-qualification.json"
RUNBOOK = ROOT / "docs/runbooks/r2-asr-candidate-qualification.md"


def test_issue683_report_tracks_both_candidates_without_claiming_qualification() -> (
    None
):
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    runtime_reports = run_offline_candidate_qualification()

    assert report["schema_version"] == ASR_CANDIDATE_QUALIFICATION_SCHEMA
    assert report["ticket"] == 683
    assert report["execution_boundary"] == {
        "mode": "synthetic-only-offline",
        "production_recordings_used": False,
        "external_asr_calls": False,
        "production_capacity_claim": False,
        "status_rule": "Missing assets, host evidence or measurements remain NOT_VERIFIED. Synthetic/fake execution is not real candidate qualification.",
    }
    assert {item["candidate_id"] for item in report["candidates"]} == {
        runtime.candidate.candidate_id for runtime in runtime_reports
    }
    assert all(
        item["status"] == QualificationStatus.NOT_VERIFIED.value
        for item in report["candidates"]
    )
    assert report["independent_gates"] == [
        gate.gate_id for gate in runtime_reports[0].gates
    ]
    assert report["reported_metrics"] == list(runtime_reports[0].metrics)
    assert all(
        runtime.overall_status is QualificationStatus.NOT_VERIFIED
        for runtime in runtime_reports
    )


def test_issue683_runbook_documents_frozen_identity_and_not_verified_boundaries() -> (
    None
):
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "source commit",
        "SHA-256",
        'language="ru"',
        "no-egress",
        "WER/CER",
        "semantic inversions",
        "NOT_VERIFIED",
        "#690",
        "#646/#682",
    ):
        assert required in runbook
