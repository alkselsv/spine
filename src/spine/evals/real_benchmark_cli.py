"""Explicit local CLI for the deterministic real-dataset benchmark."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from pathlib import Path
import sys
from typing import Callable
from uuid import UUID

from spine.evals.real_benchmark_qualification import (
    RealBenchmarkQualificationRuntime,
)
from spine.evals.real_qa_r1 import (
    LoadedRealDataset,
    RealRuntimeMappingManifest,
    load_real_r1_dataset,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic benchmark on an explicitly selected local real dataset."
    )
    parser.add_argument("--dataset-kind", choices=("real",), required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--mapping-manifest", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--environment-id", type=UUID, required=True)
    parser.add_argument(
        "--composition",
        required=True,
        help="Explicit Python factory as package.module:callable returning a RealBenchmarkRunner.",
    )
    parser.add_argument("--output", type=Path, help="Write metadata-only JSON report to this path.")
    return parser


def _load_factory(
    spec: str,
) -> Callable[[LoadedRealDataset, UUID, UUID], RealBenchmarkRunner]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("--composition must use package.module:callable")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise ValueError("--composition factory is not callable")
    return factory


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dataset_dir = args.dataset_dir.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    manifest_path = args.mapping_manifest.expanduser().resolve()
    if not dataset_dir.is_dir() or not source_root.is_dir() or not manifest_path.is_file():
        parser.error("real dataset, source root, and mapping manifest must be existing local paths")
    try:
        loaded = load_real_r1_dataset(dataset_dir, source_root=source_root)
        manifest = RealRuntimeMappingManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        factory = _load_factory(args.composition)
        runtime = factory(loaded, args.workspace_id, args.environment_id)
        if type(runtime) is not RealBenchmarkQualificationRuntime:
            raise ValueError(
                "composition factory did not return a qualified production runtime"
            )
        qualification, report = asyncio.run(runtime.run(loaded=loaded, manifest=manifest))
    except Exception:
        print(
            "real benchmark could not run: check the selected real inputs and local runtime composition",
            file=sys.stderr,
        )
        return 2
    if report is None:
        if qualification is None:
            print(
                "real benchmark unavailable: trusted runtime preflight did not return a valid report",
                file=sys.stderr,
            )
            return 2
        sys.stderr.write(
            json.dumps(qualification.model_dump(mode="json"), ensure_ascii=False) + "\n"
        )
        return 2
    serialized = json.dumps(
        {
            "qualification": qualification.model_dump(mode="json"),
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
