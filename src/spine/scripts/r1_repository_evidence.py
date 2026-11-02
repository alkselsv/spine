"""Collect the metadata-only R1 repository evidence artifact."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from xml.etree import ElementTree

from pydantic import ValidationError

from spine.evaluations.r1_repository_evidence import collect_r1_repository_evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect canonical metadata-only R1 repository evidence."
    )
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--clean-target", type=Path, required=True)
    parser.add_argument("--upgrade-target", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--skip-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        evidence = collect_r1_repository_evidence(
            head_sha=arguments.head_sha,
            clean_target_path=arguments.clean_target,
            upgrade_target_path=arguments.upgrade_target,
            junit_path=arguments.junit,
            skip_policy_path=arguments.skip_policy,
        )
        arguments.output.write_text(
            evidence.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except (ElementTree.ParseError, OSError, ValidationError, ValueError):
        print("R1 evidence collection refused invalid input.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
