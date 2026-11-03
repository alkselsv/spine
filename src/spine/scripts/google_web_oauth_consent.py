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
from spine.connectors.google_qualification_policy import (
    GOOGLE_PHASE1_READONLY_SCOPES,
)
from spine.infrastructure.google_web_oauth import GoogleWebLoopbackConsent
from spine.scripts.google_live_qualification import _load_private_environment


_REFRESH_TOKEN_KEY = "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN"
def _required(environment: dict[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise GoogleManualAuthorizationError()
    return value


def _client_configuration(environment: dict[str, str]) -> SecretStr:
    path = Path(_required(environment, "GOOGLE_OAUTH_CLIENT_FILE"))
    redirect_uri = _required(environment, "SPINE_GOOGLE_OAUTH_REDIRECT_URI")
    try:
        stat = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or stat.st_uid != os.getuid()
            or stat.st_mode & 0o077
            or stat.st_size > 65_536
        ):
            raise GoogleManualAuthorizationError()
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        web = payload["web"]
        redirect_uris = web["redirect_uris"]
        if (
            set(payload) != {"web"}
            or not isinstance(web, dict)
            or not isinstance(redirect_uris, list)
            or not redirect_uris
            or any(not isinstance(value, str) for value in redirect_uris)
            or len(set(redirect_uris)) != len(redirect_uris)
            or web["client_id"]
            != _required(environment, "SPINE_GOOGLE_OAUTH_CLIENT_ID")
            or web["client_secret"]
            != _required(environment, "SPINE_GOOGLE_OAUTH_CLIENT_SECRET")
            or redirect_uri not in redirect_uris
        ):
            raise GoogleManualAuthorizationError()
        return SecretStr(raw)
    except GoogleManualAuthorizationError:
        raise
    except (KeyError, OSError, TypeError, UnicodeError, ValueError):
        raise GoogleManualAuthorizationError() from None


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
        approved_scopes=GOOGLE_PHASE1_READONLY_SCOPES,
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
