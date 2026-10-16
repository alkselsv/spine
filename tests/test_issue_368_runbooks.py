from __future__ import annotations

import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNBOOKS = (
    "docs/runbooks/r1-backup-evidence.md",
    "docs/runbooks/r1-isolated-restore.md",
    "docs/runbooks/r1-projection-recovery.md",
    "docs/runbooks/r1-quarantine-inspection.md",
    "docs/runbooks/r1-telemetry-metrics-diagnosis.md",
)
QUALIFICATION = PROJECT_ROOT / "docs/qualification/issue-368-r1-runbook-exercises.json"


def test_r1_operator_runbooks_state_the_safe_execution_contract() -> None:
    required_sections = (
        "## Prerequisites",
        "## Permissions",
        "## Procedure",
        "## Evidence",
        "## Stop and escalate",
        "## Recovery boundary",
        "## Exercise",
    )

    for relative_path in RUNBOOKS:
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert all(section in content for section in required_sections), relative_path
        assert "Production qualification: not approved" in content, relative_path


def test_issue_368_runbook_links_resolve_within_the_repository() -> None:
    markdown_files = [PROJECT_ROOT / path for path in RUNBOOKS]
    markdown_files.append(
        PROJECT_ROOT / "docs/qualification/issue-368-r1-runbook-exercises.md"
    )

    for markdown_file in markdown_files:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", markdown_file.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (markdown_file.parent / target).resolve().is_file(), (
                markdown_file.relative_to(PROJECT_ROOT),
                target,
            )


def test_issue_368_exercise_record_is_versioned_content_free_and_resolvable() -> None:
    record = json.loads(QUALIFICATION.read_text(encoding="utf-8"))

    assert record["schema_version"] == "r1-runbook-exercise:v1"
    assert record["ticket"] == 368
    assert record["production_qualification"] is False
    assert record["fixture_kind"] == "synthetic"
    assert record["evidence"]

    for evidence in record["evidence"]:
        assert evidence["result"] in {"passed", "partial", "not_run"}
        assert evidence["passed_references"] or evidence["result"] == "not_run"
        if evidence["result"] == "partial":
            assert evidence["not_run_references"]
            assert evidence["unverified_reason"]
        assert set(evidence["passed_references"]) | set(evidence["not_run_references"]) == set(
            evidence["references"]
        )
        for reference in evidence["references"]:
            relative_path, test_node = reference.split("::", maxsplit=1)
            source = PROJECT_ROOT / relative_path
            assert source.is_file(), reference
            assert re.search(rf"(?:async )?def {re.escape(test_node)}\b", source.read_text(encoding="utf-8")), reference
