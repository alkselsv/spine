"""CLI for the manually opted-in Google Drive/Sheets qualification harness."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
import importlib
import os
from pathlib import Path
import sys

from spine.connectors.google_live_qualification import (
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationRunner,
    write_google_live_report,
)

GoogleLiveCompositionFactory = Callable[
    [GoogleLiveQualificationConfig], GoogleLiveQualificationRunner
]


def _load_composition(reference: str) -> GoogleLiveCompositionFactory:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise GoogleLiveQualificationRefused(
            "live composition must use the module:function form"
        )
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError):
        raise GoogleLiveQualificationRefused(
            "the approved live composition is unavailable"
        ) from None
    if not callable(factory):
        raise GoogleLiveQualificationRefused(
            "the approved live composition is not callable"
        )
    return factory


async def _run(
    config: GoogleLiveQualificationConfig,
    report_path: Path,
    *,
    environment_opt_in: bool,
    composition_reference: str,
) -> int:
    if not config.opt_in or not environment_opt_in:
        raise GoogleLiveQualificationRefused(
            "live Google qualification requires both config and environment opt-in"
        )
    try:
        runner = _load_composition(composition_reference)(config)
    except GoogleLiveQualificationRefused:
        raise
    except Exception:
        raise GoogleLiveQualificationRefused(
            "the approved live composition is unavailable"
        ) from None
    if not isinstance(runner, GoogleLiveQualificationRunner):
        raise GoogleLiveQualificationRefused(
            "the approved live composition returned an invalid runner"
        )
    if runner.config != config:
        raise GoogleLiveQualificationRefused(
            "the approved live composition does not match the requested manifest"
        )
    report = await runner.run(environment_opt_in=environment_opt_in)
    write_google_live_report(report_path, report)
    return 0 if report.status == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the explicitly approved Google live qualification harness."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--composition",
        required=True,
        help="approved deployment composition factory in module:function form",
    )
    args = parser.parse_args(argv)
    try:
        config = GoogleLiveQualificationConfig.model_validate_json(
            args.config.read_text(encoding="utf-8")
        )
        return asyncio.run(
            _run(
                config,
                args.report,
                environment_opt_in=(os.environ.get("SPINE_GOOGLE_LIVE_OPT_IN") == "1"),
                composition_reference=args.composition,
            )
        )
    except (GoogleLiveQualificationRefused, ValueError, OSError):
        print(
            "Google live qualification refused: approved opt-in or prerequisites are missing.",
            file=sys.stderr,
        )
        return 2


if (
    __name__ == "__main__"
):  # pragma: no cover - exercised through the script entry point
    raise SystemExit(main())
