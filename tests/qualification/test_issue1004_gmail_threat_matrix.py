from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/qualification/issue-1004-gmail-threat-qualification-matrix-v1.md"


def test_threat_matrix_has_thirteen_cases_all_not_run() -> None:
    content = MATRIX.read_text(encoding="utf-8")
    rows = [line for line in content.splitlines() if re.match(r"^\| GOB-T\d+ \|", line)]

    assert len(rows) == 13
    assert [re.match(r"^\| (GOB-T\d+) \|", row).group(1) for row in rows] == [
        f"GOB-T{number:02d}" for number in range(1, 14)
    ]
    assert all(row.endswith("NOT_RUN |") for row in rows)


def test_threat_matrix_covers_required_failure_families_and_layers() -> None:
    content = MATRIX.read_text(encoding="utf-8")

    for term in (
        "revoked",
        "publication",
        "partial",
        "retry",
        "CANCEL",
        "UNKNOWN",
        "cross-tenant",
        "Restore",
        "forbidden",
        "uncovered",
        "PostgreSQL",
        "object-store",
        "projection/cache",
        "backup/restore",
        "#812",
        "#813",
        "#976",
    ):
        assert term.casefold() in content.casefold()


def test_threat_matrix_does_not_claim_test_or_security_passes() -> None:
    content = MATRIX.read_text(encoding="utf-8")

    assert "NOT_RUN" in content
    assert re.search(r"no PASS\s+claims", content)
    assert "Security may accept only" in content
