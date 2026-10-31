from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest
from pydantic import SecretStr

from spine.application.google_manual_authorization import GoogleManualAuthorizationError
from spine.infrastructure.google_desktop_oauth import GoogleDesktopLoopbackConsent

CLIENT_CONFIGURATION = json.dumps(
    {
        "installed": {
            "client_id": "synthetic-client-id",
            "client_secret": "synthetic-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
)
SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)


class LoopbackBrowserFake:
    def __init__(self) -> None:
        self.authorization_parameters: dict[str, list[str]] = {}

    def open(self, url: str) -> bool:
        parsed = urlsplit(url)
        self.authorization_parameters = parse_qs(parsed.query)
        callback = self.authorization_parameters["redirect_uri"][0]
        request_url = f"{callback}?{urlencode({'code': 'synthetic-auth-code', 'state': self.authorization_parameters['state'][0]})}"
        with urlopen(request_url, timeout=2) as response:
            assert response.status == 200
            response.read()
        return True


class TokenExchangeFake:
    def __init__(self) -> None:
        self.arguments: dict[str, str] = {}

    async def exchange(self, **kwargs: str) -> SecretStr:
        self.arguments = kwargs
        return SecretStr("synthetic-refresh-token")


@pytest.mark.asyncio
async def test_desktop_consent_uses_loopback_and_fake_token_exchange() -> None:
    browser = LoopbackBrowserFake()
    token_exchange = TokenExchangeFake()
    consent = GoogleDesktopLoopbackConsent(
        approved_scopes=SCOPES,
        browser=browser,
        token_exchange=token_exchange,
        timeout_seconds=2,
    )

    result = await consent.authorize(
        client_configuration=SecretStr(CLIENT_CONFIGURATION)
    )

    parameters = browser.authorization_parameters
    assert parameters["response_type"] == ["code"]
    assert parameters["scope"] == [SCOPES[0]]
    assert parameters["access_type"] == ["offline"]
    assert parameters["prompt"] == ["consent"]
    assert parameters["state"]
    assert urlsplit(token_exchange.arguments["redirect_uri"]).hostname == "127.0.0.1"
    assert token_exchange.arguments["code"] == "synthetic-auth-code"
    assert token_exchange.arguments["client_secret"] == "synthetic-client-secret"
    assert result.refresh_token.get_secret_value() == "synthetic-refresh-token"
    assert "synthetic-refresh-token" not in repr(result)


class RefusingBrowser:
    def open(self, url: str) -> bool:
        del url
        return False


@pytest.mark.asyncio
async def test_desktop_consent_refusal_is_sanitized() -> None:
    consent = GoogleDesktopLoopbackConsent(
        approved_scopes=SCOPES,
        browser=RefusingBrowser(),
        token_exchange=TokenExchangeFake(),
        timeout_seconds=1,
    )

    with pytest.raises(GoogleManualAuthorizationError) as raised:
        await consent.authorize(client_configuration=SecretStr(CLIENT_CONFIGURATION))

    assert str(raised.value) == "Google manual authorization could not be completed."


def test_desktop_consent_rejects_unapproved_scope_configuration() -> None:
    with pytest.raises(ValueError):
        GoogleDesktopLoopbackConsent(approved_scopes=())
