from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


def test_oidc_stack_is_declared_as_direct_runtime_dependencies() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    runtime = tuple(project["dependencies"])

    assert "PyJWT[crypto]>=2.14,<3" in runtime
    assert "httpx>=0.27,<1" in runtime


def test_lockfile_records_oidc_stack_as_direct_spine_dependencies() -> None:
    lockfile = Path("uv.lock").read_text(encoding="utf-8")
    spine_start = lockfile.index('name = "spine"')
    spine_end = lockfile.index("\n[[package]]", spine_start)
    spine_package = lockfile[spine_start:spine_end]

    assert '{ name = "httpx" }' in spine_package
    assert '{ name = "pyjwt", extra = ["crypto"] }' in spine_package
