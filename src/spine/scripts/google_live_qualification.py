"""CLI for the manually opted-in Google Drive/Sheets qualification harness."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

import httpx

from spine.connectors.google_api import (
    GoogleApiService,
    GoogleOAuthClientCredentials,
    GoogleOAuthRefreshGrant,
    HttpxGoogleApiTransport,
    ReferenceGoogleCredentialResolver,
)
from spine.connectors.google_live_qualification import (
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationRunner,
    write_google_live_report,
)
from spine.domain.connections.google import OpaqueGoogleReference


class _EnvironmentCredentialStore:
    def __init__(self, config: GoogleLiveQualificationConfig) -> None:
        self._credential_reference = config.connection.credential_reference
        self._grant_reference = config.connection.grant_reference
        self._environment = config.credential_environment

    async def resolve_client(
        self, reference: OpaqueGoogleReference
    ) -> GoogleOAuthClientCredentials:
        if reference != self._credential_reference:
            raise GoogleLiveQualificationRefused(
                "credential reference does not match the approved connection"
            )
        return GoogleOAuthClientCredentials(
            _required_environment(self._environment.client_id),
            _required_environment(self._environment.client_secret),
        )

    async def resolve_grant(
        self, reference: OpaqueGoogleReference
    ) -> GoogleOAuthRefreshGrant:
        if reference != self._grant_reference:
            raise GoogleLiveQualificationRefused(
                "grant reference does not match the approved connection"
            )
        return GoogleOAuthRefreshGrant(
            _required_environment(self._environment.refresh_token)
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise GoogleLiveQualificationRefused(
            "an approved live credential environment reference is unavailable"
        )
    return value


async def _run(
    config: GoogleLiveQualificationConfig,
    report_path: Path,
    *,
    environment_opt_in: bool,
) -> int:
    # Preflight is deliberately before client construction and secret resolution.
    runner_without_service = GoogleLiveQualificationRunner(
        config=config, service=object()  # type: ignore[arg-type]
    )
    runner_without_service.preflight(environment_opt_in=environment_opt_in)
    async with httpx.AsyncClient() as client:
        service = GoogleApiService(
            endpoints=config.endpoints,
            request_policy=config.request_policy,
            resource_scopes=(config.resource_scope,),
            credential_resolver=ReferenceGoogleCredentialResolver(
                _EnvironmentCredentialStore(config)
            ),
            transport=HttpxGoogleApiTransport(client),
            sheet_profiles=tuple(target.profile for target in config.sheet_targets),
            sleep=asyncio.sleep,
        )
        report = await GoogleLiveQualificationRunner(config, service).run(
            environment_opt_in=environment_opt_in
        )
    write_google_live_report(report_path, report)
    return 0 if report.status == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the explicitly approved Google live qualification harness."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = GoogleLiveQualificationConfig.model_validate_json(
            args.config.read_text(encoding="utf-8")
        )
        return asyncio.run(
            _run(
                config,
                args.report,
                environment_opt_in=(
                    os.environ.get("SPINE_GOOGLE_LIVE_OPT_IN") == "1"
                ),
            )
        )
    except (GoogleLiveQualificationRefused, ValueError, OSError):
        print(
            "Google live qualification refused: approved opt-in or prerequisites are missing.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through the script entry point
    raise SystemExit(main())
