"""CLI for the explicitly opted-in Yandex identity-only preflight."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

import httpx

from spine.connectors.yandex_disk_api import HttpxYandexDiskApiTransport
from spine.connectors.yandex_identity_preflight import YandexIdentityProbeAdapter
from spine.application.yandex_identity_preflight import (
    YandexIdentityPreflightConfig,
    YandexIdentityPreflightRefused,
    YandexIdentityPreflightReport,
    YandexIdentityPreflightRunner,
    write_yandex_identity_preflight_report,
)
from spine.infrastructure.yandex_qualification_credentials import (
    LocalYandexQualificationCredentialResolver,
)
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)


async def _run(
    *,
    config: YandexIdentityPreflightConfig,
    env_file: Path,
    candidate_path: Path,
    report_path: Path,
    environment_opt_in: bool,
) -> int:
    resolver = LocalYandexQualificationCredentialResolver(
        env_file=env_file,
        binding=config.credential_binding,
    )
    async with httpx.AsyncClient() as client:
        report = await YandexIdentityPreflightRunner(
            config=config,
            identity_probe=YandexIdentityProbeAdapter(
                credential_resolver=resolver,
                transport=HttpxYandexDiskApiTransport(client=client),
            ),
            artifact_writer=FilePrivateQualificationArtifactWriter(),
        ).run(
            environment_opt_in=environment_opt_in,
            candidate_path=candidate_path,
        )
    _write_report_with_candidate_rollback(
        report_path=report_path,
        candidate_path=candidate_path,
        report=report,
    )
    return 0 if report.status == "ready_for_human_approval" else 1


def _write_report_with_candidate_rollback(
    *,
    report_path: Path,
    candidate_path: Path,
    report: YandexIdentityPreflightReport,
) -> None:
    try:
        write_yandex_identity_preflight_report(
            report_path,
            report,
            artifact_writer=FilePrivateQualificationArtifactWriter(),
        )
    except OSError:
        if report.identity_candidate_written:
            try:
                candidate_path.unlink()
            except OSError:
                pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the approved Yandex OAuth identity without Disk access."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--identity-candidate", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = YandexIdentityPreflightConfig.model_validate_json(
            args.config.read_text(encoding="utf-8")
        )
        return asyncio.run(
            _run(
                config=config,
                env_file=args.env_file,
                candidate_path=args.identity_candidate,
                report_path=args.report,
                environment_opt_in=(
                    os.environ.get("SPINE_YANDEX_IDENTITY_PREFLIGHT_OPT_IN") == "1"
                ),
            )
        )
    except (YandexIdentityPreflightRefused, OSError, ValueError):
        print(
            "Yandex identity preflight refused: approved opt-in or prerequisites are missing.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through the entry point
    raise SystemExit(main())
