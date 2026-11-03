"""Explicit operator command for one Web OAuth qualification grant."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile

from pydantic import SecretStr

from spine.application.google_manual_authorization import GoogleManualAuthorizationError
from spine.infrastructure.google_web_oauth import GoogleWebLoopbackConsent
from spine.scripts.google_live_qualification import _load_private_environment


_REFRESH_TOKEN_KEY = "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN"
_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)


def _required(environment: dict[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise GoogleManualAuthorizationError()
    return value


def _client_configuration(environment: dict[str, str]) -> SecretStr:
    redirect_uri = _required(environment, "SPINE_GOOGLE_OAUTH_REDIRECT_URI")
    return SecretStr(
        json.dumps(
            {
                "web": {
                    "client_id": _required(
                        environment, "SPINE_GOOGLE_OAUTH_CLIENT_ID"
                    ),
                    "client_secret": _required(
                        environment, "SPINE_GOOGLE_OAUTH_CLIENT_SECRET"
                    ),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _store_refresh_token(path: Path, refresh_token: SecretStr) -> None:
    token = refresh_token.get_secret_value()
    if not token or "\n" in token or "\r" in token:
        raise GoogleManualAuthorizationError()
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        indexes = [
            index
            for index, line in enumerate(lines)
            if line.startswith(f"{_REFRESH_TOKEN_KEY}=")
        ]
        if len(indexes) != 1:
            raise GoogleManualAuthorizationError()
        current = lines[indexes[0]].partition("=")[2].rstrip("\r\n")
        if current:
            raise GoogleManualAuthorizationError()
        newline = "\n" if lines[indexes[0]].endswith("\n") else ""
        lines[indexes[0]] = f"{_REFRESH_TOKEN_KEY}={token}{newline}"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent, text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.writelines(lines)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            temporary_name = ""
            path.chmod(0o600)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except OSError:
                    pass
    except GoogleManualAuthorizationError:
        raise
    except (OSError, UnicodeError):
        raise GoogleManualAuthorizationError() from None


async def authorize_and_store(
    path: Path,
    environment: dict[str, str],
    *,
    consent=None,
) -> None:
    if os.environ.get("SPINE_GOOGLE_CONSENT_OPT_IN") != "1":
        raise GoogleManualAuthorizationError()
    redirect_uri = _required(environment, "SPINE_GOOGLE_OAUTH_REDIRECT_URI")
    adapter = consent or GoogleWebLoopbackConsent(
        approved_scopes=_SCOPES,
        redirect_uri=redirect_uri,
    )
    result = await adapter.authorize(
        client_configuration=_client_configuration(environment)
    )
    _store_refresh_token(path, result.refresh_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Obtain one explicitly approved Google Web OAuth refresh grant."
    )
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        environment = _load_private_environment(args.env_file)
        asyncio.run(authorize_and_store(args.env_file, environment))
    except (GoogleManualAuthorizationError, OSError, ValueError):
        parser.exit(2, "Google Web OAuth consent was refused or could not be completed.\n")
    print("Google Web OAuth refresh grant stored in the private qualification env.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
