"""Ask Spine a question from the CLI."""

from __future__ import annotations

import argparse
import asyncio
import sys

from spine.memory.cognee_memory import ask, format_answers


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ask Spine (Cognee recall)")
    p.add_argument("question", nargs="+", help="Question text")
    p.add_argument("--dataset", type=str, default=None)
    p.add_argument("--session-id", type=str, default=None)
    return p


async def _run(question: str, dataset: str | None, session_id: str | None) -> int:
    results = await ask(question, dataset=dataset, session_id=session_id)
    print(format_answers(list(results)))
    return 0


def main(argv: list[str] | None = None) -> None:
    from spine.memory.cognee_memory import configure_environment

    configure_environment()

    args = build_parser().parse_args(argv)
    question = " ".join(args.question).strip()
    if not question:
        print("Empty question", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_run(question, args.dataset, args.session_id)))


if __name__ == "__main__":
    main()
