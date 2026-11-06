from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/qualification/issue-981-postgresql-race-matrix.md"
MATRIX_TEST_NAME = re.compile(r"`(test_[a-z0-9_]+)`")
SOURCE_TEST_NAME = re.compile(r"\btest_[a-z0-9_]+\b")


def _matrix() -> str:
    return MATRIX.read_text(encoding="utf-8")


def _repository_test_names() -> set[str]:
    return {
        match.group(0)
        for path in (ROOT / "tests").rglob("test_*.py")
        for match in SOURCE_TEST_NAME.finditer(path.read_text(encoding="utf-8"))
    }


def test_postgresql_race_matrix_has_exactly_ten_ordered_scenarios() -> None:
    rows = [
        line
        for line in _matrix().splitlines()
        if re.match(r"^\| [A-J] \|", line)
    ]

    assert len(rows) == 10
    assert [re.match(r"^\| ([A-J]) \|", row).group(1) for row in rows] == list(
        "ABCDEFGHIJ"
    )


def test_postgresql_race_matrix_names_existing_focused_test_families() -> None:
    referenced = set(MATRIX_TEST_NAME.findall(_matrix()))

    assert len(referenced) >= 20
    assert referenced <= _repository_test_names()


def test_postgresql_race_matrix_records_the_synthetic_security_boundary() -> None:
    content = _matrix().casefold()

    for term in (
        "spine_runtime",
        "two-session",
        "deterministic barrier",
        "no live provider calls",
        "tenant",
        "environment",
        "purpose",
        "idempotency",
        "rls",
        "unsupported",
        "no content or secrets",
    ):
        assert term in content
