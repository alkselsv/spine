from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Any

import yaml

from spine.evaluations.r1_repository_evidence import REPOSITORY_TEST_COMMAND


ROOT = Path(__file__).parents[2]
CLEAN_TARGET_NODE = (
    "tests/integration/postgresql/test_r1_migration_targets.py::"
    "test_current_graph_applies_to_clean_target"
)
UPGRADE_TARGET_NODE = (
    "tests/integration/postgresql/test_initial_tenancy_migration.py::"
    "test_every_retained_revision_upgrades_to_one_final_head"
)


def _r1_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    )
    return workflow["jobs"]["r1-regression-gate"]["steps"]


def _step(name: str) -> dict[str, Any]:
    return next(item for item in _r1_steps() if item.get("name") == name)


def _repository_test_argv() -> tuple[str, ...]:
    step = _step("Run mandatory repository and PostgreSQL suite")
    flattened = str(step["run"]).replace("\\\n", " ")
    invocation = re.search(r"uv run pytest -q .*?(?=\s+>)", flattened)

    assert invocation is not None
    return tuple(shlex.split(invocation.group()))


def test_repository_suite_deselects_only_separately_receipted_targets() -> None:
    command = _repository_test_argv()
    deselections = {
        token.removeprefix("--deselect=")
        for token in command
        if token.startswith("--deselect=")
    }

    assert deselections == {CLEAN_TARGET_NODE, UPGRADE_TARGET_NODE}


def test_deselected_targets_run_as_dedicated_receipted_steps() -> None:
    clean = _step("Qualify clean PostgreSQL migration target")
    upgrade = _step("Qualify retained-revision PostgreSQL migration target")

    assert CLEAN_TARGET_NODE in str(clean["run"]).split()
    assert clean["env"]["SPINE_R1_TARGET_ROLE"] == "clean"
    assert "SPINE_R1_TARGET_RECEIPT" in clean["env"]
    assert UPGRADE_TARGET_NODE in str(upgrade["run"]).split()
    assert upgrade["env"]["SPINE_R1_TARGET_ROLE"] == "upgrade"
    assert "SPINE_R1_TARGET_RECEIPT" in upgrade["env"]


def test_repository_suite_reports_slowest_tests_without_payloads() -> None:
    assert "--durations=50" in _repository_test_argv()


def test_repository_evidence_records_exact_ci_command() -> None:
    assert _repository_test_argv() == REPOSITORY_TEST_COMMAND
