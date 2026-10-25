from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/test.yml"


def _job(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"  {name}:\n", maxsplit=1)[1].split(
        f"\n  {next_name}:\n", maxsplit=1
    )[0]


def test_python_compatibility_matrix_covers_declared_runtime_range() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    compatibility = _job(workflow, "python-compatibility", "python")

    assert 'python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]' in compatibility
    assert "uv sync --extra dev --locked" in compatibility
    assert "uv run python -m compileall -q src tests" in compatibility
    assert "Run version-sensitive compatibility suite" in compatibility
    for test_path in (
        "tests/application/persistence/test_command_digest.py",
        "tests/application/persistence/test_source_observation.py",
        "tests/infrastructure/auth/test_dependency_contract.py",
        "tests/infrastructure/db/test_dependency_contract.py",
        "tests/infrastructure/memory/test_cognee_dependency_contract.py",
        "tests/test_architecture_scaffold.py",
    ):
        assert test_path in compatibility


def test_full_repository_suite_runs_once_on_canonical_python() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    repository = _job(workflow, "python", "web")

    assert "matrix:" not in repository
    assert 'python-version: "3.12"' in repository
    assert "uv run pytest -q" in repository
    assert "uv run python -m compileall -q src tests" in repository


def test_existing_web_and_r1_release_gates_remain_present() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "  web:\n" in workflow
    assert "  r1-regression-gate:\n" in workflow
    assert "Run mandatory repository and PostgreSQL suite" in workflow
