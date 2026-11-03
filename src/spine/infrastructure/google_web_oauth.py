"""Explicit Web OAuth consent for the isolated Google qualification."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import SecretStr

from spine.application.google_manual_authorization import (
    GoogleConsentResult,
    GoogleManualAuthorizationError,
)
from spine.infrastructure.google_desktop_oauth import (
    GoogleAuthorizationBrowserPort,
    GoogleOAuthTokenExchangePort,
    HttpxGoogleOAuthTokenExchange,
    SystemAuthorizationBrowser,
)


class GoogleWebLoopbackConsent:
    """Run a Web-client grant through one exact registered localhost callback."""

    __slots__ = (
        "_browser",
        "_callback_server",
        "_exchange",
        "_redirect_uri",
        "_scopes",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        approved_scopes: tuple[str, ...],
        redirect_uri: str,
        browser: GoogleAuthorizationBrowserPort | None = None,
        callback_server: GoogleOAuthCallbackServerPort | None = None,
        token_exchange: GoogleOAuthTokenExchangePort | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        if (
            not approved_scopes
            or len(set(approved_scopes)) != len(approved_scopes)
            or any(
                urlsplit(scope).scheme != "https"
                or urlsplit(scope).hostname != "www.googleapis.com"
                for scope in approved_scopes
            )
            or timeout_seconds <= 0
        ):
            raise ValueError("approved Google Web OAuth configuration is invalid")
        _parse_loopback_redirect(redirect_uri)
        self._scopes = tuple(sorted(approved_scopes))
        self._redirect_uri = redirect_uri
        self._browser = browser or SystemAuthorizationBrowser()
        self._callback_server = callback_server or AsyncioGoogleOAuthCallbackServer()
        self._exchange = token_exchange or HttpxGoogleOAuthTokenExchange()
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(approved_scope_count={len(self._scopes)}, "
            f"redirect_uri={self._redirect_uri!r})"
        )

    async def authorize(
        self, *, client_configuration: SecretStr
    ) -> GoogleConsentResult:
        try:
            client_id, client_secret, authorize_url, token_url = (
                _parse_web_client_configuration(
                    client_configuration, expected_redirect_uri=self._redirect_uri
                )
            )
            host, port, callback_path = _parse_loopback_redirect(self._redirect_uri)
            state = secrets.token_urlsafe(32)
            url = _authorization_url(
                authorize_url=authorize_url,
                client_id=client_id,
                redirect_uri=self._redirect_uri,
                scopes=self._scopes,
                state=state,
            )
            code = await self._callback_server.receive_code(
                host=host,
                port=port,
                callback_path=callback_path,
                expected_state=state,
                authorization_url=url,
                browser=self._browser,
                timeout_seconds=self._timeout_seconds,
            )
            refresh_token = await self._exchange.exchange(
                token_url=token_url,
                code=code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=self._redirect_uri,
            )
            return GoogleConsentResult(
                client_configuration=client_configuration,
                refresh_token=refresh_token,
            )
        except asyncio.CancelledError:
            raise
        except GoogleManualAuthorizationError:
            raise
        except Exception:
            raise GoogleManualAuthorizationError() from None

class GoogleOAuthCallbackServerPort(Protocol):
    async def receive_code(
        self,
        *,
        host: str,
        port: int,
        callback_path: str,
        expected_state: str,
        authorization_url: str,
        browser: GoogleAuthorizationBrowserPort,
        timeout_seconds: float,
    ) -> str: ...


class AsyncioGoogleOAuthCallbackServer:
    """Receive one bounded loopback callback after the listener is active."""

    async def receive_code(
        self,
        *,
        host: str,
        port: int,
        callback_path: str,
        expected_state: str,
        authorization_url: str,
        browser: GoogleAuthorizationBrowserPort,
        timeout_seconds: float,
    ) -> str:
        loop = asyncio.get_running_loop()
        callback: asyncio.Future[str] = loop.create_future()
        server = await asyncio.start_server(
            lambda reader, writer: self._handle_callback(
                reader,
                writer,
                callback=callback,
                expected_state=expected_state,
                callback_path=callback_path,
            ),
            host=host,
            port=port,
            limit=8192,
        )
        try:
            opened = await asyncio.to_thread(browser.open, authorization_url)
            if not opened:
                raise GoogleManualAuthorizationError()
            return await asyncio.wait_for(callback, timeout=timeout_seconds)
        finally:
            server.close()
            await server.wait_closed()

    async def _handle_callback(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        callback: asyncio.Future[str],
        expected_state: str,
        callback_path: str,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if len(request_line) > 4096:
                raise GoogleManualAuthorizationError()
            code = parse_google_oauth_callback(
                request_line,
                expected_state=expected_state,
                callback_path=callback_path,
            )
            while await asyncio.wait_for(reader.readline(), timeout=5.0) not in (
                b"\r\n",
                b"\n",
                b"",
            ):
                pass
            if not callback.done():
                callback.set_result(code)
            writer.write(
                _loopback_response(
                    status="200 OK",
                    body=b"Authorization received. You may close this tab.",
                )
            )
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not callback.done():
                callback.set_exception(GoogleManualAuthorizationError())
            writer.write(
                _loopback_response(
                    status="400 Bad Request", body=b"Authorization was rejected."
                )
            )
            try:
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def parse_google_oauth_callback(
    request_line: bytes,
    *,
    expected_state: str,
    callback_path: str,
) -> str:
    """Parse one provider callback without starting a listener or disclosing input."""

    try:
        if len(request_line) > 4096:
            raise ValueError
        parts = request_line.decode("ascii").strip().split(" ")
        if len(parts) != 3 or parts[0] != "GET" or parts[2] != "HTTP/1.1":
            raise ValueError
        target = urlsplit(parts[1])
        values = parse_qs(target.query, strict_parsing=True)
        state = values.get("state", [""])[0]
        code = values.get("code", [""])[0]
        if (
            target.path != callback_path
            or not hmac.compare_digest(state, expected_state)
            or not code
            or len(code) > 4096
            or "error" in values
        ):
            raise ValueError
        return code
    except (UnicodeError, TypeError, ValueError):
        raise GoogleManualAuthorizationError() from None


def _parse_loopback_redirect(value: str) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("approved Google Web OAuth configuration is invalid") from None
    if (
        not isinstance(value, str)
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.path != "/callback"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("approved Google Web OAuth configuration is invalid")
    return parsed.hostname, port, parsed.path


def _parse_web_client_configuration(
    client_configuration: SecretStr,
    *,
    expected_redirect_uri: str,
) -> tuple[str, str, str, str]:
    try:
        payload = json.loads(client_configuration.get_secret_value())
        web = payload["web"]
        client_id = web["client_id"]
        client_secret = web["client_secret"]
        authorize_url = web["auth_uri"]
        token_url = web["token_uri"]
        redirect_uris = web["redirect_uris"]
        if (
            not all(
                isinstance(item, str) and item
                for item in (client_id, client_secret)
            )
            or len(client_id) > 255
            or len(client_secret) > 255
            or not isinstance(redirect_uris, list)
            or any(not isinstance(item, str) for item in redirect_uris)
            or len(set(redirect_uris)) != len(redirect_uris)
            or expected_redirect_uri not in redirect_uris
            or not _is_google_endpoint(
                authorize_url, "accounts.google.com", "/o/oauth2/auth"
            )
            or not _is_google_endpoint(token_url, "oauth2.googleapis.com", "/token")
        ):
            raise ValueError
        return client_id, client_secret, authorize_url, token_url
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise GoogleManualAuthorizationError() from None


def _is_google_endpoint(value: object, hostname: str, path: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == hostname
        and parsed.path == path
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _authorization_url(
    *,
    authorize_url: str,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str,
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{authorize_url}?{query}"


def _loopback_response(*, status: str, body: bytes) -> bytes:
    return (
        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n"
    ).encode("ascii") + body


__all__ = [
    "AsyncioGoogleOAuthCallbackServer",
    "GoogleOAuthCallbackServerPort",
    "GoogleWebLoopbackConsent",
    "parse_google_oauth_callback",
]
