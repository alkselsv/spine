from __future__ import annotations

import ast
from pathlib import Path

import spine.auth as auth
from spine.auth import AuthorizedRequestContext


AUTH_ROOT = Path(__file__).parents[2] / "src" / "spine" / "auth"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_auth_seam_has_only_inward_framework_independent_dependencies() -> None:
    forbidden_roots = {
        "alembic",
        "cognee",
        "fastapi",
        "httpx",
        "jwt",
        "psycopg",
        "sqlalchemy",
        "temporalio",
    }
    forbidden_spine_prefixes = (
        "spine.api",
        "spine.connectors",
        "spine.infrastructure",
        "spine.memory",
        "spine.workers",
        "spine.workflows",
    )

    for path in AUTH_ROOT.glob("*.py"):
        modules = imported_modules(path)
        roots = {module.split(".", 1)[0] for module in modules}
        assert roots.isdisjoint(forbidden_roots), f"framework import in {path}"
        assert not any(
            module.startswith(forbidden_spine_prefixes) for module in modules
        ), f"outward dependency in {path}"


def test_authorized_context_contract_exposes_no_public_issuance_capability() -> None:
    public_names = {name for name in vars(AuthorizedRequestContext) if not name.startswith("_")}

    assert "issue" not in public_names
    assert "verify" not in public_names
    assert "from_request" not in public_names


def test_auth_seam_does_not_export_context_provenance() -> None:
    assert not any("provenance" in name.lower() for name in auth.__all__)
