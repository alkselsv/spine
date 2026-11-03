from __future__ import annotations

import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from pydantic import SecretStr

from spine.application.google_manual_authorization import GoogleManualAuthorizationError
from spine.infrastructure.google_web_oauth import (
    GoogleWebLoopbackConsent,
    parse_google_oauth_callback,
)


SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)


REDIRECT_URI = "http://127.0.0.1:8765/callback"


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


class CallbackServerFake:
    def __init__(
        self,
        *,
        override_state: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.authorization_parameters: dict[str, list[str]] = {}
        self.override_state = override_state
        self.error = error

    async def receive_code(self, **kwargs) -> str:
        if self.error is not None:
            raise self.error
        parsed = urlsplit(kwargs["authorization_url"])
        self.authorization_parameters = parse_qs(parsed.query)
        expected_state = kwargs["expected_state"]
        state = self.override_state or expected_state
        target = f"/callback?{urlencode({'code': 'synthetic-code', 'state': state})}"
        return parse_google_oauth_callback(
            f"GET {target} HTTP/1.1\r\n".encode(),
            expected_state=expected_state,
            callback_path=kwargs["callback_path"],
        )


class TokenExchangeFake:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.arguments: dict[str, str] = {}
        self.error = error

    async def exchange(self, **kwargs: str) -> SecretStr:
        self.arguments = kwargs
        if self.error is not None:
            raise self.error
        return SecretStr("synthetic-refresh-token")


@pytest.mark.asyncio
async def test_web_consent_uses_exact_registered_fixed_callback() -> None:
    callback_server = CallbackServerFake()
    exchange = TokenExchangeFake()
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        callback_server=callback_server,
        token_exchange=exchange,
        timeout_seconds=2,
    )

    result = await consent.authorize(client_configuration=_web_client(REDIRECT_URI))

    assert callback_server.authorization_parameters["redirect_uri"] == [REDIRECT_URI]
    assert callback_server.authorization_parameters["access_type"] == ["offline"]
    assert callback_server.authorization_parameters["prompt"] == ["consent"]
    assert exchange.arguments["redirect_uri"] == REDIRECT_URI
    assert exchange.arguments["client_secret"] == "synthetic-web-client-secret"
    assert result.refresh_token.get_secret_value() == "synthetic-refresh-token"
    assert "synthetic-web-client-secret" not in repr(consent)
    assert "synthetic-refresh-token" not in repr(result)


@pytest.mark.asyncio
async def test_web_consent_rejects_installed_client_before_browser_open() -> None:
    callback_server = CallbackServerFake()
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
        redirect_uri=REDIRECT_URI,
        callback_server=callback_server,
        token_exchange=TokenExchangeFake(),
    )

    with pytest.raises(GoogleManualAuthorizationError):
        await consent.authorize(client_configuration=installed)

    assert callback_server.authorization_parameters == {}


@pytest.mark.asyncio
async def test_web_consent_rejects_unregistered_callback_before_browser_open() -> None:
    callback_server = CallbackServerFake()
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        callback_server=callback_server,
        token_exchange=TokenExchangeFake(),
    )

    with pytest.raises(GoogleManualAuthorizationError):
        await consent.authorize(
            client_configuration=_web_client("http://127.0.0.1:65530/callback")
        )

    assert callback_server.authorization_parameters == {}


@pytest.mark.asyncio
async def test_web_consent_rejects_state_mismatch_with_sanitized_error() -> None:
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        callback_server=CallbackServerFake(override_state="wrong-state"),
        token_exchange=TokenExchangeFake(),
        timeout_seconds=2,
    )

    with pytest.raises(GoogleManualAuthorizationError) as raised:
        await consent.authorize(client_configuration=_web_client(REDIRECT_URI))

    assert str(raised.value) == "Google manual authorization could not be completed."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (TimeoutError(), GoogleManualAuthorizationError()),
)
async def test_web_consent_rejects_callback_failure_without_token_exchange(
    error: Exception,
) -> None:
    exchange = TokenExchangeFake()
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        callback_server=CallbackServerFake(error=error),
        token_exchange=exchange,
    )

    with pytest.raises(GoogleManualAuthorizationError):
        await consent.authorize(client_configuration=_web_client(REDIRECT_URI))

    assert exchange.arguments == {}


@pytest.mark.asyncio
async def test_web_consent_rejects_exchange_without_refresh_token() -> None:
    exchange = TokenExchangeFake(error=GoogleManualAuthorizationError())
    consent = GoogleWebLoopbackConsent(
        approved_scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        callback_server=CallbackServerFake(),
        token_exchange=exchange,
    )

    with pytest.raises(GoogleManualAuthorizationError) as raised:
        await consent.authorize(client_configuration=_web_client(REDIRECT_URI))

    assert exchange.arguments["code"] == "synthetic-code"
    assert str(raised.value) == "Google manual authorization could not be completed."


@pytest.mark.parametrize(
    "request_line",
    (
        b"POST /callback?code=value&state=expected HTTP/1.1\r\n",
        b"GET /other?code=value&state=expected HTTP/1.1\r\n",
        b"GET /callback?error=denied&state=expected HTTP/1.1\r\n",
        b"GET /callback?code=value&state=wrong HTTP/1.1\r\n",
        b"malformed\r\n",
    ),
)
def test_web_callback_parser_rejects_malformed_or_refused_callback(
    request_line: bytes,
) -> None:
    with pytest.raises(GoogleManualAuthorizationError):
        parse_google_oauth_callback(
            request_line,
            expected_state="expected",
            callback_path="/callback",
        )


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
