from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/test.yml"
SUPPORTED_PYTHONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]


def _jobs() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document["jobs"]


def _commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_python_compatibility_matrix_covers_every_supported_minor() -> None:
    matrix = _jobs()["python-compatibility"]["strategy"]["matrix"]

    assert matrix["python-version"] == SUPPORTED_PYTHONS


def test_python_compatibility_matrix_installs_the_locked_environment() -> None:
    commands = _commands(_jobs()["python-compatibility"])

    assert "uv sync --extra dev --locked" in commands


def test_python_compatibility_matrix_compiles_all_python_sources() -> None:
    commands = _commands(_jobs()["python-compatibility"])

    assert "uv run python -m compileall -q src tests" in commands


@pytest.mark.parametrize(
    "test_path",
    (
        "tests/application/persistence/test_command_digest.py",
        "tests/application/persistence/test_source_observation.py",
        "tests/infrastructure/auth/test_dependency_contract.py",
        "tests/infrastructure/db/test_dependency_contract.py",
        "tests/infrastructure/memory/test_cognee_dependency_contract.py",
        "tests/qualification/test_issue592_qualification.py",
        "tests/test_architecture_scaffold.py",
    ),
)
def test_python_compatibility_suite_exercises_version_sensitive_contract(
    test_path: str,
) -> None:
    commands = "\n".join(_commands(_jobs()["python-compatibility"]))

    assert test_path in commands


def test_python_compatibility_matrix_does_not_repeat_the_full_suite() -> None:
    commands = _commands(_jobs()["python-compatibility"])

    assert not any(
        re.search(r"(?m)^\s*uv run pytest -q\s*(?:>|$)", command)
        for command in commands
    )


def test_r1_gate_is_the_only_full_repository_suite_owner() -> None:
    owners = [
        name
        for name, job in _jobs().items()
        if any(
            re.search(r"(?m)^\s*uv run pytest -q\s*(?:>|$)", command)
            for command in _commands(job)
        )
    ]

    assert owners == ["r1-regression-gate"]


def test_full_repository_suite_uses_canonical_python() -> None:
    setup_python = next(
        step
        for step in _jobs()["r1-regression-gate"]["steps"]
        if step.get("uses", "").startswith("actions/setup-python@")
    )

    assert setup_python["with"]["python-version"] == "3.12"


def test_full_repository_gate_compiles_all_python_sources() -> None:
    commands = _commands(_jobs()["r1-regression-gate"])

    assert "uv run python -m compileall -q src tests" in commands


def test_declared_python_support_range_remains_unchanged() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.10,<3.15"


def test_supported_runtime_has_a_working_toml_parser() -> None:
    if sys.version_info < (3, 11):
        import tomli as runtime_toml
    else:
        import tomllib as runtime_toml

    assert runtime_toml.loads("value = 592") == {"value": 592}


def test_web_gate_remains_part_of_ci() -> None:
    commands = _commands(_jobs()["web"])

    assert "pnpm web:test:e2e" in commands
