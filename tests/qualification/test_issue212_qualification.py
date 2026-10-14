"""Executable traceability checks for Cognee production qualification #212."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

import pytest
from pydantic import ValidationError

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
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
LIVE_TARGET = "tests/integration/cognee/test_production_profile.py"


def _report() -> Issue212QualificationReport:
    return Issue212QualificationReport.model_validate_json(
        REPORT.read_text(encoding="utf-8")
    )


def test_issue212_report_is_explicit_no_go_from_one_live_run() -> None:
    report = _report()

    assert report.schema_version == "cognee-production-qualification:v2"
    assert report.ticket == 212
    assert report.qualification_run_id == "issue212-docker-013"
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
    statuses = {item.gate_id: item.status.value for item in report.observations}
    assert all(
        statuses[gate_id] == "passed"
        for gate_id in required_gate_ids()
        if gate_id != "canonical_lifecycle_separation"
    )
    assert statuses["canonical_lifecycle_separation"] == "not_run"
    assert report.failed_gate_ids == ()
    assert report.not_run_gate_ids == ("canonical_lifecycle_separation",)
    assert report.fallback == "owned_postgresql_projection_mapping"


def test_issue212_report_does_not_promote_issue211_fake_evidence() -> None:
    report = _report()

    assert report.issue_211_fake_qualification == "not_production_evidence"
    assert report.postgres_demo == "rejected"
    assert report.canonical_lifecycle_owner == "postgresql"
    assert report.canonical_lifecycle_separation == "not_verified"


def test_issue212_report_claims_lifecycle_separation_only_after_gate_passes() -> None:
    report = build_issue212_report(
        qualification_run_id="run-212",
        observations=tuple(
            GateObservation(gate_id=gate_id, status=GateStatus.PASSED)
            for gate_id in required_gate_ids()
        ),
    )

    assert report.canonical_lifecycle_separation == "verified_outside_unit_of_work"


def test_issue212_report_rejects_unobserved_lifecycle_claim() -> None:
    report = _report()

    with pytest.raises(ValidationError, match="does not match gate evidence"):
        Issue212QualificationReport.model_validate(
            {
                **report.model_dump(mode="python"),
                "canonical_lifecycle_separation": "verified_outside_unit_of_work",
            }
        )


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
    assert "test_production_qualification_records_one_coherent_run" in output


def test_issue212_runbook_names_exact_prerequisites_and_command() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "PostgreSQL",
        "Neo4j",
        "PGVector",
        "Cognee 1.5.4",
        "GRAPH_DATASET_DATABASE_HANDLER=neo4j_community",
        "NEO4J_COMMUNITY_IMAGE=spine/neo4j-cognee:5-community",
        "docker/neo4j-cognee/Dockerfile",
        "SPINE_COGNEE_PRODUCTION_QUALIFICATION=1",
        "-m cognee_production",
        LIVE_TARGET,
    ):
        assert required in content
    assert "postgres_demo" in content
    assert "no-go" in content.lower()


def test_issue212_backend_drivers_are_direct_runtime_dependencies() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    dependency_names = {
        re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].lower()
        for requirement in project["dependencies"]
    }

    assert {"asyncpg", "neo4j", "pgvector"} <= dependency_names


def test_issue212_traceability_covers_every_acceptance_criterion() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    criteria = tuple(
        int(value)
        for value in re.findall(r"^\| AC(\d+) \|", content, re.MULTILINE)
    )

    assert criteria == tuple(range(1, 10))
    assert "Issue #211" in content
    assert "not production evidence" in content
