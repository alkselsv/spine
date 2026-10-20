"""Executable traceability checks for Issue #507 qualification."""

from __future__ import annotations

from pathlib import Path
import re

from spine.memory.cognee.production_qualification import (
    Issue212QualificationReport,
    required_gate_ids,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = (
    PROJECT_ROOT
    / "docs/qualification/issue-507-canonical-ingestion-physical-projection.md"
)
PHYSICAL_REPORT = (
    PROJECT_ROOT / "docs/qualification/issue-212-cognee-production-report.json"
)


def test_issue507_traceability_covers_every_acceptance_criterion() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")
    criteria = tuple(
        int(value)
        for value in re.findall(r"^\| AC(\d+) \|", content, re.MULTILINE)
    )

    assert "Status: **blocked" in content
    assert criteria == tuple(range(1, 9))
    for format_name in ("PDF", "DOCX", "XLSX", "Markdown", "TXT"):
        assert format_name in content
    for reference in (
        "issue-194-document-ingestion-report.json",
        "issue-364-projection-recovery-report.json",
        "issue-212-cognee-production-report.json",
        "test_production_qualification_records_one_coherent_run",
        "ADR-0030",
        "Issue #514",
        "Issue #515",
        "Issue #516",
    ):
        assert reference in content


def test_issue507_physical_report_is_one_complete_safe_no_go_run() -> None:
    report = Issue212QualificationReport.model_validate_json(
        PHYSICAL_REPORT.read_text(encoding="utf-8")
    )

    assert report.qualification_run_id == "issue507-blocked-001"
    assert report.decision.value == "no_go"
    assert report.production_approval is False
    assert report.failed_gate_ids == ("canonical_lifecycle_separation",)
    assert report.not_run_gate_ids == ()
    assert tuple(item.gate_id for item in report.observations) == required_gate_ids()
    statuses = {item.gate_id: item.status.value for item in report.observations}
    assert all(
        statuses[gate_id] == "passed"
        for gate_id in required_gate_ids()
        if gate_id != "canonical_lifecycle_separation"
    )
    assert statuses["canonical_lifecycle_separation"] == "failed"
    assert report.canonical_lifecycle_separation == "not_verified"


def test_issue507_traceability_names_exact_runtime_without_sensitive_values() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")

    for identity in (
        "Cognee 1.5.4",
        "PostgreSQL 17",
        "Neo4j 5.26.32",
        "PGVector 0.8.7",
        "openai/gpt-5.4-nano",
        "openai/text-embedding-3-small",
    ):
        assert identity in content
    forbidden = (
        "DB_PASSWORD",
        "GRAPH_DATABASE_PASSWORD",
        "OPENAI_API_KEY",
        "postgresql+psycopg://",
        "127.0.0.1",
    )
    assert all(value not in content for value in forbidden)
