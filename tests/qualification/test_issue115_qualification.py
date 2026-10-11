from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = (
    PROJECT_ROOT / "docs/qualification/issue-115-local-agent-runtime.md"
)


def traceability_rows() -> list[str]:
    return [
        line
        for line in TRACEABILITY.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\| AC\d+ \|", line)
    ]


def evidence_nodes() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            node_id
            for row in traceability_rows()
            for node_id in re.findall(r"`(tests/[^`]+::test_[^`]+)`", row)
        )
    )


def test_issue115_traceability_maps_exactly_ac1_to_ac20() -> None:
    rows = traceability_rows()

    assert [
        int(match.group(1))
        for row in rows
        if (match := re.match(r"^\| AC(\d+) \|", row))
    ] == list(range(1, 21))
    assert all(re.findall(r"`(tests/[^`]+::test_[^`]+)`", row) for row in rows)


def test_issue115_qualification_names_one_primary_runtime_suite() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")

    assert "Primary behavioral contract suite" in content
    assert "`tests/agents/test_local_runtime.py`" in content


def test_issue115_qualification_disclaims_production_provider_qualification() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")

    assert "not production integration qualification" in content
    assert "real model/provider qualification: not run" in content.lower()


def test_issue115_traceability_evidence_nodes_are_collectable() -> None:
    nodes = evidence_nodes()
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *nodes],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert collected.returncode == 0, collected.stdout + collected.stderr
    output = collected.stdout + collected.stderr
    assert all(node_id in output for node_id in nodes)


def test_issue115_qualification_declares_runtime_workflow_ownership_boundary() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")

    for local_concern in (
        "bounded technical retries",
        "typed validation",
        "Artifact lineage",
        "idempotency",
        "cooperative cancellation",
    ):
        assert local_concern in content
    for temporal_concern in (
        "durable retries",
        "fallback",
        "approvals",
        "signal waiting",
        "workflow recovery",
    ):
        assert temporal_concern in content


def test_issue115_qualification_records_exact_repository_gates() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")

    for command in (
        "uv sync --extra dev",
        "uv run pytest -q",
        "uv run python -m compileall -q src tests",
    ):
        assert f"`{command}`" in content
