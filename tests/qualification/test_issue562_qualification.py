from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from spine.evaluations.release_gates.projection_publication import (
    ProjectionPublicationQualificationReport,
    required_projection_publication_scenarios,
)
from spine.memory.cognee.production_qualification import Issue212QualificationReport

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-562-projection-publication-report.json"
Q16_PATH = ROOT / "docs/qualification/issue-560-q16-01-runtime-readiness-v1.json"
R4_PATH = ROOT / "docs/qualification/issue-561-five-format-ingestion-report.json"
PHYSICAL_PATH = ROOT / "docs/qualification/issue-562-cognee-production-report.json"
SUMMARY_PATH = ROOT / "docs/qualification/issue-562-projection-publication.md"
RUNBOOK_PATH = ROOT / "docs/runbooks/r1-projection-publication-qualification.md"


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_issue562_report_is_derived_from_exact_q16_target_and_provider_evidence() -> (
    None
):
    report = ProjectionPublicationQualificationReport.model_validate_json(
        REPORT_PATH.read_text(encoding="utf-8")
    )
    q16 = _json(Q16_PATH)
    r4 = _json(R4_PATH)

    assert report.target.commit_sha == q16["target"]["commit_sha"]
    assert (
        report.target.deployment_fingerprint == q16["target"]["deployment_fingerprint"]
    )
    assert str(report.target.workspace_id) == q16["target"]["workspace_id"]
    assert str(report.target.environment_id) == q16["target"]["environment_id"]
    assert (
        report.target.authorization_generation
        == q16["target"]["authorization_generation"]
    )
    assert report.target.provider_profile_id == q16["manifest"]["provider_profile_id"]
    assert (
        report.target.provider_profile_digest
        == q16["manifest"]["provider_profile_digest"]
    )
    assert report.target.commit_sha == r4["deployment"]["commit_sha"]
    assert (
        report.target.deployment_fingerprint
        == r4["deployment"]["deployment_fingerprint"]
    )
    assert report.provider_compatibility is not None
    physical_report = Issue212QualificationReport.model_validate_json(
        PHYSICAL_PATH.read_text(encoding="utf-8")
    )
    physical = physical_report.model_dump(mode="json")
    assert report.provider_compatibility.source_run_id == "issue562-r6-007"
    assert report.provider_compatibility.evidence_kind == "fresh_physical_run"
    assert report.provider_compatibility.status == "compatible"
    assert report.provider_compatibility.source_run_id == physical[
        "qualification_run_id"
    ]
    assert report.provider_compatibility.physical_report_sha256 == sha256(
        PHYSICAL_PATH.read_bytes()
    ).hexdigest()
    assert report.provider_compatibility.passed_physical_gate_ids == tuple(
        item["gate_id"] for item in physical["observations"]
    )
    assert report.provider_compatibility.production_approval is True
    assert physical["decision"] == "go"
    assert physical["production_approval"] is True
    assert report.provider_compatibility.run_authorization_reference == (
        "codex-session-issue562-r6-007-20261024"
    )


def test_issue562_report_covers_every_required_live_scenario() -> None:
    report = ProjectionPublicationQualificationReport.model_validate_json(
        REPORT_PATH.read_text(encoding="utf-8")
    )

    assert report.status == "pass"
    assert report.reason_codes == ()
    assert report.provider_compatibility is not None
    assert report.provider_compatibility.status == "compatible"
    assert {item.scenario_id for item in report.observations} == set(
        required_projection_publication_scenarios()
    )
    assert all(item.status == "passed" for item in report.observations)
    assert len(required_projection_publication_scenarios()) == 13


def test_issue562_report_contains_no_sensitive_data() -> None:
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()
    for forbidden in (
        "access_token",
        "bearer ",
        '"filename":',
        '"document_text":',
        '"raw_error":',
        '"endpoint":',
        '"credential":',
    ):
        assert forbidden not in serialized


def test_issue562_runbook_names_every_required_scenario() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    for scenario_id in required_projection_publication_scenarios():
        assert f"`{scenario_id}`" in runbook


def test_issue562_runbook_stops_before_unapproved_provider_calls() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "do not start the dispatcher" in runbook.lower()


def test_cognee_runbook_preserves_operator_environment_binding() -> None:
    assert "PYTHON_DOTENV_DISABLED=1" in (
        ROOT / "docs/runbooks/cognee-production-qualification.md"
    ).read_text(encoding="utf-8")


def test_issue562_summary_records_qualified_run() -> None:
    summary = SUMMARY_PATH.read_text(encoding="utf-8")

    assert "Status: **PASS**" in summary
    assert "issue562-r6-007" in summary
