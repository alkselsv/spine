"""Cognee memory helpers for Spine."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from spine.config import settings


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


async def remember_texts(
    texts: list[str],
    *,
    dataset: str | None = None,
    node_set: list[str] | None = None,
) -> Any:
    configure_environment()
    import cognee

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
    configure_environment()
    import cognee

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
