from __future__ import annotations

import json
import socket
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest
from pydantic import SecretStr

from spine.application.google_manual_authorization import GoogleManualAuthorizationError
from spine.infrastructure.google_web_oauth import GoogleWebLoopbackConsent


SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)


def _available_redirect_uri() -> str:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return f"http://127.0.0.1:{port}/callback"


def _web_client(redirect_uri: str) -> SecretStr:
    return SecretStr(
        json.dumps(
            {
                "web": {
                    "client_id": "synthetic-web-client-id",
                    "client_secret": "synthetic-web-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            }
        )
    )


class WebCallbackBrowserFake:
    def __init__(self, *, override_state: str | None = None) -> None:
        self.authorization_parameters: dict[str, list[str]] = {}
        self.override_state = override_state

    def open(self, url: str) -> bool:
        parsed = urlsplit(url)
        self.authorization_parameters = parse_qs(parsed.query)
        callback = self.authorization_parameters["redirect_uri"][0]
        state = self.override_state or self.authorization_parameters["state"][0]
        request_url = f"{callback}?{urlencode({'code': 'synthetic-code', 'state': state})}"
        try:
            with urlopen(request_url, timeout=2) as response:
                response.read()
        except Exception:
            if self.override_state is None:
                raise
        return True


class TokenExchangeFake:
    def __init__(self) -> None:
        self.arguments: dict[str, str] = {}

    async def exchange(self, **kwargs: str) -> SecretStr:
        self.arguments = kwargs
        return SecretStr("synthetic-refresh-token")


@pytest.mark.asyncio
async def test_web_consent_uses_exact_registered_fixed_callback() -> None:
    redirect_uri = _available_redirect_uri()
    browser = WebCallbackBrowserFake()
    exchange = TokenExchangeFake()
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=redirect_uri,
        browser=browser,
        token_exchange=exchange,
        timeout_seconds=2,
    )

    result = await consent.authorize(client_configuration=_web_client(redirect_uri))

    assert browser.authorization_parameters["redirect_uri"] == [redirect_uri]
    assert browser.authorization_parameters["access_type"] == ["offline"]
    assert browser.authorization_parameters["prompt"] == ["consent"]
    assert exchange.arguments["redirect_uri"] == redirect_uri
    assert exchange.arguments["client_secret"] == "synthetic-web-client-secret"
    assert result.refresh_token.get_secret_value() == "synthetic-refresh-token"
    assert "synthetic-web-client-secret" not in repr(consent)
    assert "synthetic-refresh-token" not in repr(result)


@pytest.mark.asyncio
async def test_web_consent_rejects_installed_client_before_browser_open() -> None:
    redirect_uri = _available_redirect_uri()
    browser = WebCallbackBrowserFake()
    installed = SecretStr(
        json.dumps(
            {
                "installed": {
                    "client_id": "desktop-client",
                    "client_secret": "desktop-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=redirect_uri,
        browser=browser,
        token_exchange=TokenExchangeFake(),
    )

    with pytest.raises(GoogleManualAuthorizationError):
        await consent.authorize(client_configuration=installed)

    assert browser.authorization_parameters == {}


@pytest.mark.asyncio
async def test_web_consent_rejects_unregistered_callback_before_browser_open() -> None:
    redirect_uri = _available_redirect_uri()
    browser = WebCallbackBrowserFake()
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=redirect_uri,
        browser=browser,
        token_exchange=TokenExchangeFake(),
    )

    with pytest.raises(GoogleManualAuthorizationError):
        await consent.authorize(
            client_configuration=_web_client("http://127.0.0.1:65530/callback")
        )

    assert browser.authorization_parameters == {}


@pytest.mark.asyncio
async def test_web_consent_rejects_state_mismatch_with_sanitized_error() -> None:
    redirect_uri = _available_redirect_uri()
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=redirect_uri,
        browser=WebCallbackBrowserFake(override_state="wrong-state"),
        token_exchange=TokenExchangeFake(),
        timeout_seconds=2,
    )

    with pytest.raises(GoogleManualAuthorizationError) as raised:
        await consent.authorize(client_configuration=_web_client(redirect_uri))

    assert str(raised.value) == "Google manual authorization could not be completed."


@pytest.mark.parametrize(
    "redirect_uri",
    (
        "https://127.0.0.1:8765/callback",
        "http://example.test:8765/callback",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:8765/other",
        "http://127.0.0.1:8765/callback?value=1",
    ),
)
def test_web_consent_rejects_unsafe_callback_configuration(redirect_uri: str) -> None:
    with pytest.raises(ValueError):
        GoogleWebLoopbackConsent(
            approved_scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
