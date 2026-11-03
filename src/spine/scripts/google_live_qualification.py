"""CLI for the manually opted-in Google Drive/Sheets qualification harness."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
import importlib
import os
from pathlib import Path
import stat
import sys

from dotenv import dotenv_values

from spine.connectors.google_live_qualification import (
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationRunner,
    write_google_live_report,
)

GoogleLiveCompositionFactory = Callable[
    [GoogleLiveQualificationConfig], GoogleLiveQualificationRunner
]


def _load_private_environment(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise GoogleLiveQualificationRefused(
                "the private qualification environment is unavailable"
            )
        values = dotenv_values(path, interpolate=False)
    except GoogleLiveQualificationRefused:
        raise
    except (OSError, UnicodeError, ValueError):
        raise GoogleLiveQualificationRefused(
            "the private qualification environment is unavailable"
        ) from None
    if any(value is None for value in values.values()):
        raise GoogleLiveQualificationRefused(
            "the private qualification environment is unavailable"
        )
    return {key: value for key, value in values.items() if value is not None}


def _config_from_private_environment(
    environment: dict[str, str],
) -> GoogleLiveQualificationConfig:
    from spine.infrastructure.google_live_runtime import (
        build_google_live_config_from_environment,
    )

    return build_google_live_config_from_environment(environment)


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


async def _preflight(
    config: GoogleLiveQualificationConfig,
    *,
    composition_reference: str,
    preflight_opt_in: bool,
) -> int:
    if not preflight_opt_in:
        raise GoogleLiveQualificationRefused(
            "Google private preflight requires explicit preflight opt-in"
        )
    try:
        runner = _load_composition(composition_reference)(config)
    except GoogleLiveQualificationRefused:
        raise
    except Exception:
        raise GoogleLiveQualificationRefused(
            "the approved live composition is unavailable"
        ) from None
    if not isinstance(runner, GoogleLiveQualificationRunner) or runner.config != config:
        raise GoogleLiveQualificationRefused(
            "the approved live composition returned an invalid runner"
        )
    await runner.preflight(environment_opt_in=True)
    print(
        "Google live qualification preflight passed: "
        f"effective_configuration_fingerprint={config.effective_configuration_fingerprint}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the explicitly approved Google live qualification harness."
    )
    config_source = parser.add_mutually_exclusive_group(required=True)
    config_source.add_argument("--config", type=Path)
    config_source.add_argument("--env-file", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--composition",
        required=False,
        help="approved deployment composition factory in module:function form",
    )
    parser.add_argument("--inspect-fingerprints", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        private_environment: dict[str, str] = {}
        if args.env_file is not None:
            private_environment = _load_private_environment(args.env_file)
            config = _config_from_private_environment(private_environment)
        else:
            config = GoogleLiveQualificationConfig.model_validate_json(
                args.config.read_text(encoding="utf-8")
            )
        if args.inspect_fingerprints:
            if args.preflight_only or args.report is not None or args.composition:
                raise GoogleLiveQualificationRefused(
                    "fingerprint inspection cannot execute a composition"
                )
            print(
                "Google private qualification fingerprints: "
                f"manifest_sha256={private_environment.get('SPINE_GOOGLE_QUALIFICATION_MANIFEST_SHA256', 'external-config')};"
                f"effective_configuration_fingerprint={config.effective_configuration_fingerprint}"
            )
            return 0
        if not args.composition:
            raise GoogleLiveQualificationRefused(
                "the approved live composition is unavailable"
            )
        previous = {key: os.environ.get(key) for key in private_environment}
        os.environ.update(private_environment)
        try:
            if args.preflight_only:
                if args.report is not None:
                    raise GoogleLiveQualificationRefused(
                        "preflight-only mode does not write a report"
                    )
                return asyncio.run(
                    _preflight(
                        config,
                        composition_reference=args.composition,
                        preflight_opt_in=(
                            os.environ.get("SPINE_GOOGLE_PREFLIGHT_OPT_IN") == "1"
                        ),
                    )
                )
            if args.report is None:
                raise GoogleLiveQualificationRefused(
                    "the approved live report path is unavailable"
                )
            return asyncio.run(
                _run(
                    config,
                    args.report,
                    environment_opt_in=(
                        os.environ.get("SPINE_GOOGLE_LIVE_OPT_IN") == "1"
                    ),
                    composition_reference=args.composition,
                )
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
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
