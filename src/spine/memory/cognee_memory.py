"""Cognee memory helpers for Spine."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sys
from threading import Lock
from typing import Any

from spine.config import settings


_COGNEE_DATABASE_ENVIRONMENT_KEYS = (
    "DB_PROVIDER",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USERNAME",
    "DB_PASSWORD",
    "DATABASE_CONNECT_ARGS",
    "POOL_ARGS",
    "MIGRATION_DB_PROVIDER",
    "MIGRATION_DB_PATH",
    "MIGRATION_DB_HOST",
    "MIGRATION_DB_PORT",
    "MIGRATION_DB_NAME",
    "MIGRATION_DB_USERNAME",
    "MIGRATION_DB_PASSWORD",
    "VECTOR_DB_PROVIDER",
    "VECTOR_DB_HOST",
    "VECTOR_DB_PORT",
    "VECTOR_DB_NAME",
    "VECTOR_DB_USERNAME",
    "VECTOR_DB_PASSWORD",
    "VECTOR_DB_URL",
    "VECTOR_DB_KEY",
    "VECTOR_DATASET_DATABASE_HANDLER",
    "VECTOR_POOL_ARGS",
    "GRAPH_DATABASE_PROVIDER",
    "GRAPH_DATABASE_HOST",
    "GRAPH_DATABASE_PORT",
    "GRAPH_DATABASE_NAME",
    "GRAPH_DATABASE_USERNAME",
    "GRAPH_DATABASE_PASSWORD",
    "GRAPH_DATABASE_URL",
    "GRAPH_DATABASE_KEY",
    "GRAPH_DATASET_DATABASE_HANDLER",
)
_cognee_binding_lock = Lock()
_cognee_database_binding_digest: str | None = None
_cognee_database_binding_frozen = False


class CogneeEnvironmentBindingError(RuntimeError):
    """Bounded refusal to retarget an initialized Cognee process."""

    reason_code = "cognee_runtime_binding_mismatch"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


def _absolute(path_value: str, root: Path) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def configure_environment() -> None:
    """Point Cognee storage at project-local dirs and export LLM settings.

    Cognee's BaseConfig requires absolute paths. ``uv run`` loads ``.env`` into
    the process environment first, so relative ``*_ROOT_DIRECTORY`` values must
    be overwritten (not setdefault) before ``import cognee``.
    """
    root = settings.project_root
    os.environ["SYSTEM_ROOT_DIRECTORY"] = _absolute(settings.system_root_directory, root)
    os.environ["DATA_ROOT_DIRECTORY"] = _absolute(settings.data_root_directory, root)
    os.environ["CACHE_ROOT_DIRECTORY"] = _absolute(settings.cache_root_directory, root)
    os.environ["COGNEE_LOGS_DIR"] = str((root / ".spine" / "logs").resolve())

    if settings.llm_api_key:
        os.environ.setdefault("LLM_API_KEY", settings.llm_api_key)

    for key in (
        "SYSTEM_ROOT_DIRECTORY",
        "DATA_ROOT_DIRECTORY",
        "CACHE_ROOT_DIRECTORY",
        "COGNEE_LOGS_DIR",
    ):
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)

    _refresh_loaded_cognee_configuration()


def _refresh_loaded_cognee_configuration() -> None:
    """Invalidate Cognee's cached binding after the process target changes.

    Cognee 1.5.4 memoizes its relational, vector and graph settings. A recovery
    runner may perform an offline preflight before selecting its fresh target,
    so the public SDK boundary must discard settings created for the earlier
    target. The digest retains no configuration or credential values.
    """

    global _cognee_database_binding_digest

    binding = "\0".join(
        f"{key}={os.environ.get(key, '')}"
        for key in _COGNEE_DATABASE_ENVIRONMENT_KEYS
    )
    binding_digest = sha256(binding.encode("utf-8")).hexdigest()
    with _cognee_binding_lock:
        if binding_digest == _cognee_database_binding_digest:
            return
        if _cognee_database_binding_frozen:
            raise CogneeEnvironmentBindingError
        if "cognee" in sys.modules:
            for cached_configuration in _cognee_configuration_getters():
                cached_configuration.cache_clear()
        _cognee_database_binding_digest = binding_digest


def freeze_cognee_environment() -> None:
    """Materialize and pin the selected database target for this process."""

    global _cognee_database_binding_frozen

    with _cognee_binding_lock:
        if _cognee_database_binding_digest is None or "cognee" not in sys.modules:
            raise CogneeEnvironmentBindingError
        for cached_configuration in _cognee_configuration_getters():
            cached_configuration()
        _cognee_database_binding_frozen = True


def _cognee_configuration_getters() -> tuple[Any, ...]:
    from cognee.base_config import get_base_config
    from cognee.infrastructure.databases.graph.config import get_graph_config
    from cognee.infrastructure.databases.relational.config import (
        get_migration_config,
        get_relational_config,
    )
    from cognee.infrastructure.databases.vector.config import get_vectordb_config

    return (
        get_base_config,
        get_relational_config,
        get_migration_config,
        get_vectordb_config,
        get_graph_config,
    )


def load_configured_cognee() -> Any:
    """Import Cognee lazily after selecting and freezing its runtime binding."""

    configure_environment()
    import cognee

    freeze_cognee_environment()
    return cognee


async def remember_texts(
    texts: list[str],
    *,
    dataset: str | None = None,
    node_set: list[str] | None = None,
) -> Any:
    cognee = load_configured_cognee()

    ds = dataset or settings.spine_dataset
    return await cognee.remember(
        texts,
        dataset_name=ds,
        node_set=node_set,
    )


async def ask(
    question: str,
    *,
    dataset: str | None = None,
    session_id: str | None = None,
) -> list[Any]:
    cognee = load_configured_cognee()

    kwargs: dict[str, Any] = {
        "datasets": dataset or settings.spine_dataset,
    }
    if session_id:
        kwargs["session_id"] = session_id
    return await cognee.recall(question, **kwargs)


def format_answers(results: list[Any]) -> str:
    """Render recall results as plain text for CLI / API."""
    lines: list[str] = []
    for i, item in enumerate(results, start=1):
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text") or item.get("answer") or str(item)
        if text is None:
            text = str(item)
        source = getattr(item, "source", None)
        if source is None and isinstance(item, dict):
            source = item.get("source")
        header = f"[{i}]"
        if source:
            header += f" source={source}"
        lines.append(f"{header}\n{text}")
    return "\n\n".join(lines) if lines else "(нет результатов)"
