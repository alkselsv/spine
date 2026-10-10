from __future__ import annotations

import inspect
import ast
from pathlib import Path

from spine.infrastructure.object_storage import (
    AuthorizedOriginalReadService,
    FenceActive,
    ObjectUnavailable,
    OriginalObjectStore,
    StorageErrorCategory,
    StorageRetryability,
)


def test_error_taxonomy_is_stable_retryable_and_redacted() -> None:
    error = ObjectUnavailable()
    assert error.category is StorageErrorCategory.OBJECT_UNAVAILABLE
    assert error.retryability is StorageRetryability.RETRYABLE
    assert error.retryable is True
    rendered = error.redacted()
    assert rendered["category"] == "object_unavailable"
    assert "provider" not in repr(error)
    assert "path" not in repr(error)
    assert "bucket" not in repr(error)

    assert FenceActive().retryable is True


def test_storage_port_is_public_but_has_no_raw_reference_read() -> None:
    members = inspect.getmembers(OriginalObjectStore)
    names = {name for name, _ in members if not name.startswith("_")}
    assert {
        "begin_or_resume_upload",
        "write_chunks",
        "abort_upload",
        "finalize_upload",
        "open_bounded_read",
        "verify",
        "reconciliation_inventory",
        "prepare_deletion",
        "delete_controlled",
    } <= names
    assert "read" not in names
    assert "read_reference" not in names
    assert "read_original" in {
        name
        for name, _ in inspect.getmembers(AuthorizedOriginalReadService)
        if not name.startswith("_")
    }

def test_contract_modules_have_only_provider_neutral_imports() -> None:
    package = Path(__file__).parents[3] / "src" / "spine" / "infrastructure" / "object_storage"
    forbidden = {"fastapi", "sqlalchemy", "cognee", "temporalio", "boto3", "azure", "google"}
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        imported = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert imported.isdisjoint(forbidden), (path, imported & forbidden)
