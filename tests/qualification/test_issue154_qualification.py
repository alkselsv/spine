"""Executable traceability checks for Issue #154."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[2]
QUALIFICATION = PROJECT_ROOT / "docs/qualification/issue-154-document-ingestion.md"


def test_issue154_qualification_record_covers_the_five_r1_formats() -> None:
    content = QUALIFICATION.read_text(encoding="utf-8")

    for format_name in ("TXT", "Markdown", "PDF", "DOCX", "XLSX"):
        assert format_name in content
    assert "test_document_ingestion_completes_each_r1_format_with_parser_metadata" in content


def test_issue154_qualification_record_preserves_follow_up_boundaries() -> None:
    content = QUALIFICATION.read_text(encoding="utf-8")

    assert "Batch coordination" in content
    assert "tombstone/reappearance" in content
    assert "projection delivery" in content
    assert "follow-up qualification ticket #194" in content


def test_issue154_qualification_record_names_external_prerequisite_limits() -> None:
    content = QUALIFICATION.read_text(encoding="utf-8")

    assert "opt-in PostgreSQL integration suite" in content
    assert "not silently presented" in content
