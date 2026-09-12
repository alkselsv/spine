"""Seed Cognee from fixtures or an arbitrary path."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from spine.config import settings
from spine.ingest.loaders import documents_to_remember_payloads, load_path
from spine.memory.cognee_memory import remember_texts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ingest documents into Spine (Cognee)")
    p.add_argument(
        "--path",
        type=str,
        default=None,
        help="File or directory to ingest (default: data/fixtures)",
    )
    p.add_argument("--dataset", type=str, default=None)
    p.add_argument(
        "--node-set",
        dest="node_set",
        action="append",
        default=None,
        help="Optional Cognee node_set tag (repeatable)",
    )
    return p


async def _run(path: Path, dataset: str | None, node_set: list[str] | None) -> int:
    docs = load_path(path)
    if not docs:
        print(f"No documents found under {path}", file=sys.stderr)
        return 1
    payloads = documents_to_remember_payloads(docs)
    print(f"Ingesting {len(payloads)} document(s) from {path} …")
    await remember_texts(payloads, dataset=dataset, node_set=node_set)
    print("Done.")
    return 0


def main(argv: list[str] | None = None) -> None:
    # Resolve Cognee paths before any cognee import (uv run injects relative .env).
    from spine.memory.cognee_memory import configure_environment

    configure_environment()

    args = build_parser().parse_args(argv)
    if args.path:
        path = Path(args.path)
        if not path.is_absolute():
            path = settings.project_root / path
    else:
        path = settings.project_root / "data" / "fixtures"
    raise SystemExit(asyncio.run(_run(path, args.dataset, args.node_set)))


if __name__ == "__main__":
    main()
