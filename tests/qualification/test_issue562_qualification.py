from __future__ import annotations

import json
from pathlib import Path

from spine.evaluations.release_gates.projection_publication import (
    ProjectionPublicationQualificationReport,
    required_projection_publication_scenarios,
)

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-562-projection-publication-report.json"
Q16_PATH = ROOT / "docs/qualification/issue-560-q16-01-runtime-readiness-v1.json"
R4_PATH = ROOT / "docs/qualification/issue-561-five-format-ingestion-report.json"
PHYSICAL_PATH = ROOT / "docs/qualification/issue-212-cognee-production-report.json"
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
    physical = _json(PHYSICAL_PATH)
    assert report.provider_compatibility.source_run_id == "issue562-r6-001"
    assert report.provider_compatibility.evidence_kind == "fresh_physical_run"
    assert report.provider_compatibility.physical_report_sha256 is None
    assert (
        report.provider_compatibility.source_run_id
        != physical["qualification_run_id"]
    )
    assert report.provider_compatibility.run_authorization_reference == (
        "https://github.com/alkselsv/spine/issues/562#issuecomment-6382335412"
    )


def test_issue562_report_cannot_hide_missing_live_scenarios_or_sensitive_data() -> None:
    report = ProjectionPublicationQualificationReport.model_validate_json(
        REPORT_PATH.read_text(encoding="utf-8")
    )

    assert report.status == "not_verified"
    assert report.reason_codes == ("provider_compatibility_incompatible",)
    assert report.provider_compatibility is not None
    assert report.provider_compatibility.status == "not_verified"
    assert {item.scenario_id for item in report.observations} == {
        "changed_source_head",
        "rebuild_failure_preserves_active",
        "safe_diagnostics",
    }
    assert all(item.status == "passed" for item in report.observations)
    assert len(required_projection_publication_scenarios()) == 13

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


def test_issue562_runbook_names_every_scenario_and_stops_before_unapproved_calls() -> (
    None
):
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    summary = SUMMARY_PATH.read_text(encoding="utf-8")

    for scenario_id in required_projection_publication_scenarios():
        assert f"`{scenario_id}`" in runbook
    assert "do not start the dispatcher" in runbook.lower()
    assert "NOT_VERIFIED" in summary
    assert "issue507-composed-002" in summary
