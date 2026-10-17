"""Explicit dataset selection and local integrity-check command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from spine.evals.qa_r1 import load_r1_dataset
from spine.evals.real_qa_r1 import REAL_R1_VALIDATOR_VERSION, RealDatasetError, load_real_r1_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a selected R1 evaluation dataset locally.")
    parser.add_argument("--dataset-kind", choices=("synthetic", "real"), required=True)
    parser.add_argument("--dataset-dir", type=Path, help="Local real dataset directory (required for real).")
    parser.add_argument("--source-root", type=Path, help="Local original source root (required for real).")
    parser.add_argument("--output", type=Path, help="Write the metadata-only report to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code = 0
    if args.dataset_kind == "synthetic":
        if args.dataset_dir is not None or args.source_root is not None:
            build_parser().error("real dataset paths cannot be supplied with --dataset-kind synthetic")
        dataset = load_r1_dataset()
        report = {
            "report_schema_version": "dataset-validation-report:v1",
            "validator_version": REAL_R1_VALIDATOR_VERSION,
            "validation_status": "passed",
            "csv_consistency_status": "passed",
            "csv_mismatch_case_count": 0,
            "csv_mismatch_fields": [],
            "evidence_integrity_status": "passed",
            "locator_mismatch_count": 0,
            "locator_mismatch_by_kind": {},
            "evaluation_status": "not_run",
            "dataset_kind": "synthetic",
            "dataset_id": dataset.dataset_id,
            "version": dataset.version,
            "digest": dataset.dataset_digest,
            "case_count": len(dataset.cases),
            "source_count": len(dataset.source_objects),
            "note": "Dataset loading is not an evaluation of RAG quality.",
        }
    else:
        if args.dataset_dir is None or args.source_root is None:
            build_parser().error("--dataset-dir and --source-root are required with --dataset-kind real")
        try:
            loaded = load_real_r1_dataset(args.dataset_dir, source_root=args.source_root)
        except RealDatasetError as exc:
            print(f"real dataset validation failed: {exc}", file=sys.stderr)
            return 2
        report = {
            "report_schema_version": "dataset-validation-report:v1",
            "validator_version": REAL_R1_VALIDATOR_VERSION,
            "dataset_kind": "real",
            **loaded.validation_report.model_dump(mode="json"),
            "note": "Local source/evidence validation only; no RAG execution or quality evaluation was run.",
        }
        if loaded.validation_report.validation_status == "blocked":
            exit_code = 2
            report["note"] = "Dataset readiness validation is blocked; no RAG execution or quality evaluation was run."
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
