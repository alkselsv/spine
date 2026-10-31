"""Explicit Google Desktop OAuth loopback consent; never used by ordinary tests."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import httpx
from pydantic import SecretStr

from spine.application.google_manual_authorization import (
    GoogleConsentResult,
    GoogleManualAuthorizationError,
)


class GoogleOAuthTokenExchangePort(Protocol):
    async def exchange(
        self,
        *,
        token_url: str,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> SecretStr: ...


class GoogleAuthorizationBrowserPort(Protocol):
    def open(self, url: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class SystemAuthorizationBrowser:
    """Open the consent URL in the operator's local browser."""

    def open(self, url: str) -> bool:
        return webbrowser.open(url, new=1, autoraise=True)


class HttpxGoogleOAuthTokenExchange:
    """Exchange one manually approved authorization code with bounded HTTP."""

    async def exchange(
        self,
        *,
        token_url: str,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> SecretStr:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0), follow_redirects=False
            ) as client:
                response = await client.post(
                    token_url,
                    data={
                        "code": code,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
                if response.status_code != 200:
                    raise GoogleManualAuthorizationError()
                payload = response.json()
                token = payload.get("refresh_token")
                if not isinstance(token, str) or not token:
                    raise GoogleManualAuthorizationError()
                return SecretStr(token)
        except asyncio.CancelledError:
            raise
        except GoogleManualAuthorizationError:
            raise
        except Exception:
            raise GoogleManualAuthorizationError() from None


class GoogleDesktopLoopbackConsent:
    """Run explicit loopback consent and exchange its code through a fakeable port."""

    __slots__ = ("_scopes", "_browser", "_exchange", "_timeout_seconds")

    def __init__(
        self,
        *,
        approved_scopes: tuple[str, ...],
        browser: GoogleAuthorizationBrowserPort | None = None,
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
            raise ValueError("approved Google OAuth loopback configuration is invalid")
        self._scopes = tuple(sorted(approved_scopes))
        self._browser = browser or SystemAuthorizationBrowser()
        self._exchange = token_exchange or HttpxGoogleOAuthTokenExchange()
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"{type(self).__name__}(approved_scope_count={len(self._scopes)})"

    async def authorize(
        self, *, client_configuration: SecretStr
    ) -> GoogleConsentResult:
        try:
            client_id, client_secret, authorize_url, token_url = (
                _parse_desktop_client_configuration(client_configuration)
            )
            loop = asyncio.get_running_loop()
            callback: asyncio.Future[str] = loop.create_future()
            state = secrets.token_urlsafe(32)
            server = await asyncio.start_server(
                lambda reader, writer: self._handle_callback(
                    reader, writer, callback=callback, expected_state=state
                ),
                host="127.0.0.1",
                port=0,
                limit=8192,
            )
            try:
                socket = server.sockets[0]
                port = socket.getsockname()[1]
                redirect_uri = f"http://127.0.0.1:{port}/callback"
                url = _authorization_url(
                    authorize_url=authorize_url,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    scopes=self._scopes,
                    state=state,
                )
                opened = await asyncio.to_thread(self._browser.open, url)
                if not opened:
                    raise GoogleManualAuthorizationError()
                code = await asyncio.wait_for(callback, timeout=self._timeout_seconds)
            finally:
                server.close()
                await server.wait_closed()
            refresh_token = await self._exchange.exchange(
                token_url=token_url,
                code=code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
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

    async def _handle_callback(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        callback: asyncio.Future[str],
        expected_state: str,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if len(request_line) > 4096:
                raise GoogleManualAuthorizationError()
            parts = request_line.decode("ascii").strip().split(" ")
            if len(parts) != 3 or parts[0] != "GET" or parts[2] != "HTTP/1.1":
                raise GoogleManualAuthorizationError()
            target = urlsplit(parts[1])
            values = parse_qs(target.query, strict_parsing=True)
            state = values.get("state", [""])[0]
            code = values.get("code", [""])[0]
            if (
                target.path != "/callback"
                or not hmac.compare_digest(state, expected_state)
                or not code
                or len(code) > 4096
                or "error" in values
            ):
                raise GoogleManualAuthorizationError()
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


def _parse_desktop_client_configuration(
    client_configuration: SecretStr,
) -> tuple[str, str, str, str]:
    try:
        payload = json.loads(client_configuration.get_secret_value())
        installed = payload["installed"]
        client_id = installed["client_id"]
        client_secret = installed["client_secret"]
        authorize_url = installed["auth_uri"]
        token_url = installed["token_uri"]
        if (
            not all(isinstance(item, str) and item for item in (client_id, client_secret))
            or len(client_id) > 255
            or len(client_secret) > 255
            or _is_google_endpoint(authorize_url, "accounts.google.com", "/o/oauth2/auth")
            is False
            or _is_google_endpoint(token_url, "oauth2.googleapis.com", "/token") is False
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
    "GoogleAuthorizationBrowserPort",
    "GoogleDesktopLoopbackConsent",
    "GoogleOAuthTokenExchangePort",
    "HttpxGoogleOAuthTokenExchange",
    "SystemAuthorizationBrowser",
]
