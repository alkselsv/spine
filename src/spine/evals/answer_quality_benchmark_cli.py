"""Explicit local CLI for the approved answer-quality benchmark."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from pathlib import Path
import sys
from typing import Callable
from uuid import UUID

from spine.evals.answer_quality_dataset import (
    APPROVED_ANSWER_QUALITY_DATASET,
    AnswerQualityDatasetError,
    LoadedAnswerQualityDataset,
    load_answer_quality_dataset,
)
from spine.evals.real_benchmark_qualification import (
    RealBenchmarkQualificationRuntime,
)
from spine.evals.real_qa_r1 import RealRuntimeMappingManifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the pinned answer-quality dataset through production Q&A."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--mapping-manifest", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--environment-id", type=UUID, required=True)
    parser.add_argument(
        "--composition",
        required=True,
        help="Explicit factory as package.module:callable.",
    )
    parser.add_argument("--output", type=Path)
    return parser


def _load_factory(spec: str) -> Callable[..., RealBenchmarkQualificationRuntime]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("composition must use package.module:callable")
    value = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise TypeError("composition factory is not callable")
    return value


async def _run(args: argparse.Namespace) -> tuple[object | None, object | None, object | None]:
    loaded = load_answer_quality_dataset(
        args.dataset,
        source_root=args.source_root,
        source_manifest_path=args.source_manifest,
        identity=APPROVED_ANSWER_QUALITY_DATASET,
    )
    manifest = RealRuntimeMappingManifest.model_validate_json(
        args.mapping_manifest.read_text(encoding="utf-8")
    )
    factory = _load_factory(args.composition)
    runtime = factory(loaded, args.workspace_id, args.environment_id)
    if type(runtime) is not RealBenchmarkQualificationRuntime:
        raise TypeError("composition did not return the qualification runtime")
    qualification, report = await runtime.run(loaded=loaded, manifest=manifest)
    return qualification, runtime.readiness_report, report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected_paths = (
        args.dataset,
        args.source_root,
        args.source_manifest,
        args.mapping_manifest,
    )
    if not all(Path(path).expanduser().resolve().exists() for path in selected_paths):
        build_parser().error("all private benchmark inputs must be existing local paths")
    try:
        qualification, readiness, report = asyncio.run(_run(args))
    except (AnswerQualityDatasetError, OSError, ValueError, TypeError, ImportError):
        sys.stderr.write(
            json.dumps(
                {
                    "schema_version": "answer-quality-benchmark-cli-failure:v1",
                    "outcome": "unavailable",
                    "reason_codes": ["private_input_or_composition_invalid"],
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    if qualification is None or report is None:
        failure = (
            qualification.model_dump(mode="json")
            if qualification is not None
            else {
                "schema_version": "answer-quality-benchmark-cli-failure:v1",
                "outcome": "unavailable",
                "reason_codes": ["preflight_unavailable"],
            }
        )
        sys.stderr.write(json.dumps(failure, sort_keys=True) + "\n")
        return 2
    serialized = json.dumps(
        {
            "qualification": qualification.model_dump(mode="json"),
            "runtime_readiness": (
                readiness.model_dump(mode="json") if readiness is not None else None
            ),
            "benchmark": report.model_dump(mode="json"),
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    if args.output is None:
        sys.stdout.write(serialized)
    else:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
