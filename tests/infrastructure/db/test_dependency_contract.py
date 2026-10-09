from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


def test_postgresql_stack_is_declared_as_direct_project_dependencies() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    runtime = tuple(project["dependencies"])
    development = tuple(project["optional-dependencies"]["dev"])

    assert any(item.startswith("sqlalchemy") for item in runtime)
    assert any(item.startswith("alembic") for item in runtime)
    assert any(item.startswith("psycopg[binary]") for item in runtime)
    assert any(item.startswith("testcontainers[postgres]") for item in development)


def test_lockfile_contains_direct_postgresql_stack() -> None:
    lockfile = Path("uv.lock").read_text(encoding="utf-8")

    for package in ("alembic", "sqlalchemy", "psycopg", "testcontainers"):
        assert f'{{ name = "{package}"' in lockfile
