"""Recompute the final R0/R1 decision from a metadata-only package."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pydantic import ValidationError

from spine.evaluations.release_gates.final_qualification import (
    EvidenceIndex,
    FinalQualificationPackage,
    evaluate_final_qualification,
    verify_evidence_index,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recompute the metadata-only final R0/R1 qualification decision."
    )
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--evidence-index", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        package = FinalQualificationPackage.model_validate_json(
            arguments.package.read_text(encoding="utf-8")
        )
        evidence_index = EvidenceIndex.model_validate_json(
            arguments.evidence_index.read_text(encoding="utf-8")
        )
        verified_index = verify_evidence_index(
            evidence_index, root=arguments.evidence_root
        )
        report = evaluate_final_qualification(package, verified_index)
        arguments.report.write_text(
            report.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValidationError, ValueError):
        print(
            "Final qualification refused: the metadata package is unavailable or invalid.",
            file=sys.stderr,
        )
        return 2

    return 0 if report.decision == "go" else 1


if __name__ == "__main__":
    raise SystemExit(main())
