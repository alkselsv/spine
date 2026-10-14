"""Executable traceability checks for Cognee production qualification #212."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

from spine.memory.cognee.production_qualification import (
    GateObservation,
    GateStatus,
    Issue212QualificationReport,
    build_issue212_report,
    required_gate_ids,
    write_issue212_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT = PROJECT_ROOT / "docs/qualification/issue-212-cognee-production-report.json"
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-212-cognee-production.md"
RUNBOOK = PROJECT_ROOT / "docs/runbooks/cognee-production-qualification.md"
LIVE_TARGET = "tests/integration/cognee/test_production_profile.py"


def _report() -> Issue212QualificationReport:
    return Issue212QualificationReport.model_validate_json(
        REPORT.read_text(encoding="utf-8")
    )


def test_issue212_report_is_explicit_no_go_without_live_evidence() -> None:
    report = _report()

    assert report.schema_version == "cognee-production-qualification:v1"
    assert report.ticket == 212
    assert report.qualification_run_id == "not-run"
    assert report.profile.model_dump() == {
        "relational": "postgresql",
        "graph": "neo4j",
        "vector": "pgvector",
    }
    assert report.mode.value == "live_real_backends"
    assert report.decision.value == "no_go"
    assert report.production_approval is False
    assert report.safe_reason_code == "gate_failure_or_missing_evidence"
    assert [item.gate_id for item in report.observations] == list(
        required_gate_ids()
    )
    assert all(item.status.value == "not_run" for item in report.observations)
    assert report.fallback == "owned_postgresql_projection_mapping"


def test_issue212_report_does_not_promote_issue211_fake_evidence() -> None:
    report = _report()

    assert report.issue_211_fake_qualification == "not_production_evidence"
    assert report.postgres_demo == "rejected"
    assert report.canonical_lifecycle_owner == "postgresql"
    assert report.physical_calls_inside_postgresql_unit_of_work is False


def test_issue212_report_is_generated_from_one_run_observation_set(tmp_path: Path) -> None:
    observations = tuple(
        GateObservation(
            gate_id=gate_id,
            status=GateStatus.NOT_RUN,
            safe_code="live_prerequisite_unavailable",
        )
        for gate_id in required_gate_ids()
    )

    expected = build_issue212_report(
        qualification_run_id="run-212",
        observations=observations,
    )
    written = write_issue212_report(
        tmp_path / "report.json",
        qualification_run_id="run-212",
        observations=observations,
    )

    assert written == expected
    assert Issue212QualificationReport.model_validate_json(
        (tmp_path / "report.json").read_text(encoding="utf-8")
    ) == expected


def test_issue212_live_target_is_collectable_and_skipped_by_default() -> None:
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", LIVE_TARGET],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    output = collected.stdout + collected.stderr
    assert collected.returncode == 0, output
    assert "test_production_profile_preflight_requires_exact_backends" in output
    assert "test_public_cognee_profile_exercises_structured_sdk_lifecycle" in output
    assert "test_complete_context_projection_round_trips_public_cognee_profile" in output


def test_issue212_runbook_names_exact_prerequisites_and_command() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "PostgreSQL",
        "Neo4j",
        "PGVector",
        "Cognee 1.5.4",
        "SPINE_COGNEE_PRODUCTION_QUALIFICATION=1",
        "-m cognee_production",
        LIVE_TARGET,
    ):
        assert required in content
    assert "postgres_demo" in content
    assert "no-go" in content.lower()


def test_issue212_traceability_covers_every_acceptance_criterion() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    criteria = tuple(
        int(value)
        for value in re.findall(r"^\| AC(\d+) \|", content, re.MULTILINE)
    )

    assert criteria == tuple(range(1, 10))
    assert "Issue #211" in content
    assert "not production evidence" in content
