from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from spine.application.google_manual_authorization import (
    GoogleConsentResult,
    GoogleManualAuthorizationError,
)
from spine.scripts.google_web_oauth_consent import authorize_and_store, main


def _write_client(path, *, redirect_uris=None, client_type="web") -> None:
    path.write_text(
        json.dumps(
            {
                client_type: {
                    "client_id": "synthetic-client-id",
                    "client_secret": "synthetic-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    **(
                        {"redirect_uris": redirect_uris}
                        if redirect_uris is not None
                        else {}
                    ),
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_env(path, client_path) -> None:
    path.write_text(
        f"GOOGLE_OAUTH_CLIENT_FILE={client_path}\n"
        "SPINE_GOOGLE_OAUTH_REDIRECT_URI=http://127.0.0.1:8765/callback\n"
        "SPINE_GOOGLE_OAUTH_CLIENT_ID=synthetic-client-id\n"
        "SPINE_GOOGLE_OAUTH_CLIENT_SECRET=synthetic-client-secret\n"
        "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN=\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


class ConsentFake:
    def __init__(self) -> None:
        self.configuration: dict[str, object] = {}

    async def authorize(self, *, client_configuration: SecretStr):
        self.configuration = json.loads(client_configuration.get_secret_value())
        return GoogleConsentResult(
            client_configuration=client_configuration,
            refresh_token=SecretStr("synthetic-refresh-token"),
        )


@pytest.mark.asyncio
async def test_consent_stores_refresh_token_atomically_without_output(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / ".env.qualification"
    client_path = tmp_path / "client.json"
    _write_client(client_path, redirect_uris=["http://127.0.0.1:8765/callback"])
    _write_env(path, client_path)
    environment = {
        "GOOGLE_OAUTH_CLIENT_FILE": str(client_path),
        "SPINE_GOOGLE_OAUTH_REDIRECT_URI": "http://127.0.0.1:8765/callback",
        "SPINE_GOOGLE_OAUTH_CLIENT_ID": "synthetic-client-id",
        "SPINE_GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN": "",
    }
    consent = ConsentFake()
    monkeypatch.setenv("SPINE_GOOGLE_CONSENT_OPT_IN", "1")

    await authorize_and_store(path, environment, consent=consent)

    content = path.read_text(encoding="utf-8")
    assert "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN=synthetic-refresh-token" in content
    assert path.stat().st_mode & 0o777 == 0o600
    assert consent.configuration["web"]["redirect_uris"] == [
        "http://127.0.0.1:8765/callback"
    ]


def test_consent_cli_refuses_without_separate_opt_in(tmp_path, capsys) -> None:
    path = tmp_path / ".env.qualification"
    client_path = tmp_path / "client.json"
    _write_client(client_path, redirect_uris=["http://127.0.0.1:8765/callback"])
    _write_env(path, client_path)

    with pytest.raises(SystemExit) as raised:
        main(["--env-file", str(path)])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "synthetic-client-secret" not in captured.err
    assert "synthetic-refresh-token" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_consent_refuses_to_overwrite_existing_refresh_token(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / ".env.qualification"
    client_path = tmp_path / "client.json"
    _write_client(client_path, redirect_uris=["http://127.0.0.1:8765/callback"])
    _write_env(path, client_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN=",
            "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN=existing-token",
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    environment = {
        "GOOGLE_OAUTH_CLIENT_FILE": str(client_path),
        "SPINE_GOOGLE_OAUTH_REDIRECT_URI": "http://127.0.0.1:8765/callback",
        "SPINE_GOOGLE_OAUTH_CLIENT_ID": "synthetic-client-id",
        "SPINE_GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN": "existing-token",
    }
    monkeypatch.setenv("SPINE_GOOGLE_CONSENT_OPT_IN", "1")

    with pytest.raises(GoogleManualAuthorizationError):
        await authorize_and_store(path, environment, consent=ConsentFake())

    assert "existing-token" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", ("installed", "web"))
async def test_consent_refuses_actual_client_without_registered_web_callback(
    tmp_path, monkeypatch, client_type: str
) -> None:
    path = tmp_path / ".env.qualification"
    client_path = tmp_path / "client.json"
    _write_client(client_path, client_type=client_type)
    _write_env(path, client_path)
    environment = {
        "GOOGLE_OAUTH_CLIENT_FILE": str(client_path),
        "SPINE_GOOGLE_OAUTH_REDIRECT_URI": "http://127.0.0.1:8765/callback",
        "SPINE_GOOGLE_OAUTH_CLIENT_ID": "synthetic-client-id",
        "SPINE_GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN": "",
    }
    consent = ConsentFake()
    monkeypatch.setenv("SPINE_GOOGLE_CONSENT_OPT_IN", "1")

    with pytest.raises(GoogleManualAuthorizationError):
        await authorize_and_store(path, environment, consent=consent)

    assert consent.configuration == {}


@pytest.mark.asyncio
async def test_consent_refuses_actual_client_that_disagrees_with_private_binding(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / ".env.qualification"
    client_path = tmp_path / "client.json"
    _write_client(client_path, redirect_uris=["http://127.0.0.1:8765/callback"])
    _write_env(path, client_path)
    environment = {
        "GOOGLE_OAUTH_CLIENT_FILE": str(client_path),
        "SPINE_GOOGLE_OAUTH_REDIRECT_URI": "http://127.0.0.1:8765/callback",
        "SPINE_GOOGLE_OAUTH_CLIENT_ID": "different-client-id",
        "SPINE_GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN": "",
    }
    consent = ConsentFake()
    monkeypatch.setenv("SPINE_GOOGLE_CONSENT_OPT_IN", "1")

    with pytest.raises(GoogleManualAuthorizationError):
        await authorize_and_store(path, environment, consent=consent)

    assert consent.configuration == {}
