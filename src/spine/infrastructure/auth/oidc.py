"""Process-owned OIDC discovery and JWKS lifecycle for JWT authentication."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import math
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict

from spine.auth import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthenticationPort,
    AuthenticationUnavailableError,
    InvalidAuthenticationError,
)
from spine.infrastructure.auth.settings import (
    OIDCAuthenticationSettings,
    OIDCReadinessPolicy,
)


Clock = Callable[[], datetime]
KeyCacheFreshness = Literal["empty", "fresh", "stale", "expired"]


class OIDCReadinessStatus(str, Enum):
    """Safe process-readiness state."""

    READY = "ready"
    DEFERRED = "deferred"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class OIDCReadinessCode(str, Enum):
    """Disclosure-safe reason for the current OIDC readiness state."""

    READY = "oidc.ready"
    CHECK_DEFERRED = "oidc.check_deferred"
    STALE_KEYS = "oidc.stale_keys"
    PROVIDER_UNAVAILABLE = "oidc.provider_unavailable"
    PROVIDER_INCOMPATIBLE = "oidc.provider_incompatible"
    RUNTIME_CLOSED = "oidc.runtime_closed"


class OIDCReadiness(BaseModel):
    """Sanitized readiness summary safe for operator diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: OIDCReadinessStatus
    code: OIDCReadinessCode
    configuration_version: str
    mode: Literal["oidc"] = "oidc"
    issuer_origin: str
    key_cache_freshness: KeyCacheFreshness


class _ProviderUnavailable(Exception):
    pass


class _ProviderIncompatible(Exception):
    pass


class _UnknownKey(Exception):
    pass


class _DuplicateJSONMember(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ValidatedJWK:
    kid: str
    document: Mapping[str, Any]
    algorithms: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ProviderCache:
    fetched_at: float
    keys: Mapping[str, _ValidatedJWK]


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname or "", parsed.port


def _origin_text(url: str) -> str:
    parsed = urlsplit(url)
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _discovery_url(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/.well-known/openid-configuration"


def _decode_base64url(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        (value + padding).encode("ascii"), altchars=b"-_", validate=True
    )


def _compatible_key_algorithms(
    document: Mapping[str, Any], allowed: frozenset[str]
) -> frozenset[str]:
    kty = document.get("kty")
    if kty == "RSA":
        try:
            modulus_bits = int.from_bytes(
                _decode_base64url(document.get("n")), byteorder="big"
            ).bit_length()
        except (UnicodeEncodeError, ValueError):
            return frozenset()
        if modulus_bits < 2_048:
            return frozenset()
        return frozenset(algorithm for algorithm in allowed if algorithm == "RS256")
    return frozenset()


class _OIDCJWTAuthenticationAdapter:
    """Private adapter implementation; the runtime exposes it only as the port."""

    def __init__(self, runtime: "OIDCAuthenticationRuntime") -> None:
        self._runtime = runtime

    async def authenticate(
        self,
        credentials: AccessTokenCredential,
        diagnostic_context: Any,
    ) -> AuthenticatedAlias:
        del diagnostic_context
        try:
            token = credentials.access_token.get_secret_value()
            header, unverified_claims = _bounded_token_parts(token)
            typ = header.get("typ")
            algorithm = header.get("alg")
            kid = header.get("kid")
            if (
                typ not in {"at+jwt", "application/at+jwt"}
                or "crit" in header
                or "b64" in header
            ):
                raise InvalidAuthenticationError
            if (
                not isinstance(algorithm, str)
                or algorithm not in self._runtime._settings.allowed_algorithms
            ):
                raise InvalidAuthenticationError
            if (
                not isinstance(kid, str)
                or not kid
                or len(kid) > 256
                or any(ord(character) < 33 or ord(character) == 127 for character in kid)
            ):
                raise InvalidAuthenticationError
            key = await self._runtime._key_for(kid, algorithm)
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                options={
                    "verify_signature": True,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_sub": False,
                    "verify_jti": False,
                },
            )
            if claims != unverified_claims:
                raise InvalidAuthenticationError
            subject = self._runtime._validate_claims(claims)
            return AuthenticatedAlias(
                alias=AuthenticationAlias(
                    issuer=self._runtime._settings.issuer,
                    subject=subject,
                ),
                authentication_configuration_version=(
                    self._runtime._settings.configuration_version
                ),
            )
        except AuthenticationUnavailableError:
            raise
        except InvalidAuthenticationError:
            raise
        except _UnknownKey:
            raise InvalidAuthenticationError from None
        except (_ProviderUnavailable, _ProviderIncompatible):
            raise AuthenticationUnavailableError from None
        except (
            jwt.PyJWTError,
            OverflowError,
            RecursionError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise InvalidAuthenticationError from None


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONMember
        result[key] = value
    return result


def _decode_token_segment(value: str, *, maximum_bytes: int) -> dict[str, Any]:
    if not value or len(value) > ((maximum_bytes + 2) // 3) * 4:
        raise ValueError
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ValueError from None
    if len(decoded) > maximum_bytes:
        raise ValueError
    parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_members)
    if not isinstance(parsed, dict):
        raise ValueError
    return parsed


def _bounded_token_parts(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3 or not parts[2] or len(parts[2]) > 8_192:
        raise ValueError
    return (
        _decode_token_segment(parts[0], maximum_bytes=4_096),
        _decode_token_segment(parts[1], maximum_bytes=8_192),
    )


def _valid_bounded_text(value: object, *, maximum_length: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum_length
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _numeric_date(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidAuthenticationError
    result = float(value)
    if not math.isfinite(result):
        raise InvalidAuthenticationError
    return result


class OIDCAuthenticationRuntime:
    """Own one lazy HTTP client, key cache and authentication port instance."""

    def __init__(
        self,
        settings: OIDCAuthenticationSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._settings = settings
        self._clock = clock
        timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.read_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        )
        self._cache: _ProviderCache | None = None
        self._refresh_lock = asyncio.Lock()
        self._refresh_attempt = 0
        self._last_refresh_error: (
            type[_ProviderUnavailable | _ProviderIncompatible] | None
        ) = None
        self._refresh_retry_not_before = 0.0
        self._closed = False
        self._authentication: AuthenticationPort[Any] = _OIDCJWTAuthenticationAdapter(self)

    @property
    def authentication(self) -> AuthenticationPort[Any]:
        """Expose the concrete implementation only through the parent port."""

        return self._authentication

    async def __aenter__(self) -> "OIDCAuthenticationRuntime":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def startup(self) -> OIDCReadiness:
        """Apply the configured startup policy without hiding provider I/O."""

        if self._settings.readiness_policy is OIDCReadinessPolicy.DEFERRED:
            return self._readiness(
                OIDCReadinessStatus.DEFERRED,
                OIDCReadinessCode.CHECK_DEFERRED,
            )
        return await self.check_readiness()

    def _timestamp(self) -> float:
        current = self._clock()
        if current.tzinfo is None:
            raise RuntimeError("OIDC clock must return a timezone-aware datetime")
        return current.timestamp()

    def _freshness(
        self, cache: _ProviderCache | None = None
    ) -> KeyCacheFreshness:
        selected = self._cache if cache is None else cache
        if selected is None:
            return "empty"
        age = max(0.0, self._timestamp() - selected.fetched_at)
        if age <= self._settings.fresh_cache_ttl_seconds:
            return "fresh"
        if age <= self._settings.maximum_stale_seconds:
            return "stale"
        return "expired"

    def _readiness(
        self,
        status: OIDCReadinessStatus,
        code: OIDCReadinessCode,
    ) -> OIDCReadiness:
        return OIDCReadiness(
            status=status,
            code=code,
            configuration_version=self._settings.configuration_version,
            issuer_origin=_origin_text(self._settings.issuer),
            key_cache_freshness=self._freshness(),
        )

    async def check_readiness(self) -> OIDCReadiness:
        if self._closed:
            return self._readiness(
                OIDCReadinessStatus.UNAVAILABLE,
                OIDCReadinessCode.RUNTIME_CLOSED,
            )
        if self._freshness() == "fresh":
            return self._readiness(OIDCReadinessStatus.READY, OIDCReadinessCode.READY)
        try:
            await self._refresh_if_unchanged(self._refresh_attempt, self._cache)
        except _ProviderIncompatible:
            return self._readiness(
                OIDCReadinessStatus.UNAVAILABLE,
                OIDCReadinessCode.PROVIDER_INCOMPATIBLE,
            )
        except _ProviderUnavailable:
            return self._fallback_readiness(OIDCReadinessCode.PROVIDER_UNAVAILABLE)
        return self._readiness(OIDCReadinessStatus.READY, OIDCReadinessCode.READY)

    def _fallback_readiness(self, empty_code: OIDCReadinessCode) -> OIDCReadiness:
        if self._freshness() == "stale":
            return self._readiness(
                OIDCReadinessStatus.DEGRADED,
                OIDCReadinessCode.STALE_KEYS,
            )
        return self._readiness(OIDCReadinessStatus.UNAVAILABLE, empty_code)

    async def _refresh_if_unchanged(
        self,
        observed_attempt: int,
        observed_cache: _ProviderCache | None,
    ) -> None:
        async with self._refresh_lock:
            if self._cache is not observed_cache:
                return
            if self._refresh_attempt != observed_attempt:
                if self._last_refresh_error is not None:
                    raise self._last_refresh_error
                return
            if (
                self._last_refresh_error is not None
                and self._timestamp() < self._refresh_retry_not_before
            ):
                raise self._last_refresh_error
            self._refresh_attempt += 1
            try:
                cache = await self._fetch_provider_cache()
            except (_ProviderUnavailable, _ProviderIncompatible) as error:
                self._last_refresh_error = type(error)
                self._refresh_retry_not_before = self._timestamp() + min(
                    1.0, self._settings.connect_timeout_seconds
                )
                raise
            self._cache = cache
            self._last_refresh_error = None
            self._refresh_retry_not_before = 0.0

    async def _key_for(self, kid: str, algorithm: str) -> jwt.PyJWK:
        if self._closed:
            raise _ProviderUnavailable
        existing_cache = self._cache
        freshness = self._freshness()
        matching_stale = self._matching_key(existing_cache, kid, algorithm)
        if freshness == "fresh" and matching_stale is not None:
            return self._materialize_key(matching_stale, algorithm)

        observed_attempt = self._refresh_attempt
        try:
            await self._refresh_if_unchanged(observed_attempt, existing_cache)
        except _ProviderUnavailable:
            if freshness == "stale" and matching_stale is not None:
                return self._materialize_key(matching_stale, algorithm)
            raise
        except _ProviderIncompatible:
            raise

        matching = self._matching_key(self._cache, kid, algorithm)
        if matching is None:
            raise _UnknownKey
        return self._materialize_key(matching, algorithm)

    @staticmethod
    def _matching_key(
        cache: _ProviderCache | None,
        kid: str,
        algorithm: str,
    ) -> _ValidatedJWK | None:
        if cache is None:
            return None
        selected = cache.keys.get(kid)
        if selected is None or algorithm not in selected.algorithms:
            return None
        return selected

    @staticmethod
    def _materialize_key(key: _ValidatedJWK, algorithm: str) -> jwt.PyJWK:
        candidate = dict(key.document)
        candidate["alg"] = algorithm
        try:
            return jwt.PyJWK.from_dict(candidate)
        except (jwt.PyJWKError, ValueError, TypeError):
            raise _ProviderIncompatible from None

    def _validate_claims(self, claims: Mapping[str, Any]) -> str:
        if claims.get("iss") != self._settings.issuer:
            raise InvalidAuthenticationError
        audience = claims.get("aud")
        if audience != self._settings.audience and audience != [
            self._settings.audience
        ]:
            raise InvalidAuthenticationError
        subject = claims.get("sub")
        if not _valid_bounded_text(subject, maximum_length=512):
            raise InvalidAuthenticationError
        client_id = claims.get("client_id")
        if (
            not isinstance(client_id, str)
            or client_id not in self._settings.qualified_interactive_client_ids
        ):
            raise InvalidAuthenticationError
        jti = claims.get("jti")
        if not _valid_bounded_text(jti, maximum_length=512):
            raise InvalidAuthenticationError
        expiration = _numeric_date(claims.get("exp"))
        issued_at = _numeric_date(claims.get("iat"))
        not_before_value = claims.get("nbf")
        not_before = (
            None if not_before_value is None else _numeric_date(not_before_value)
        )
        now = self._timestamp()
        skew = self._settings.clock_skew_seconds
        if expiration <= now - skew or issued_at > now + skew or expiration <= issued_at:
            raise InvalidAuthenticationError
        if not_before is not None and (
            not_before > now + skew or not_before >= expiration
        ):
            raise InvalidAuthenticationError
        return subject

    async def _fetch_provider_cache(self) -> _ProviderCache:
        metadata = await self._get_json(
            _discovery_url(self._settings.issuer),
            maximum_bytes=self._settings.maximum_discovery_bytes,
        )
        if metadata.get("issuer") != self._settings.issuer:
            raise _ProviderIncompatible
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not self._eligible_jwks_uri(jwks_uri):
            raise _ProviderIncompatible
        jwks = await self._get_json(
            jwks_uri,
            maximum_bytes=self._settings.maximum_jwks_bytes,
        )
        keys = self._validate_jwks(jwks)
        return _ProviderCache(fetched_at=self._timestamp(), keys=keys)

    def _eligible_jwks_uri(self, value: str) -> bool:
        parsed = urlsplit(value)
        try:
            return (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and not any(
                    ord(character) <= 32 or ord(character) == 127
                    for character in value
                )
                and _origin(value) == _origin(self._settings.issuer)
            )
        except ValueError:
            return False

    async def _get_json(self, url: str, *, maximum_bytes: int) -> dict[str, Any]:
        try:
            async with self._client.stream("GET", url) as response:
                if 300 <= response.status_code < 400:
                    raise _ProviderIncompatible
                if response.status_code != 200:
                    raise _ProviderUnavailable
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > maximum_bytes:
                        raise _ProviderIncompatible
        except (_ProviderIncompatible, _ProviderUnavailable):
            raise
        except (httpx.HTTPError, RuntimeError):
            raise _ProviderUnavailable from None
        try:
            value = json.loads(
                bytes(body), object_pairs_hook=_reject_duplicate_members
            )
        except (RecursionError, UnicodeError, ValueError):
            raise _ProviderIncompatible from None
        if not isinstance(value, dict):
            raise _ProviderIncompatible
        return value

    def _validate_jwks(self, document: dict[str, Any]) -> Mapping[str, _ValidatedJWK]:
        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > 100:
            raise _ProviderIncompatible
        validated: dict[str, _ValidatedJWK] = {}
        for value in raw_keys:
            if not isinstance(value, dict):
                raise _ProviderIncompatible
            kid = value.get("kid")
            if (
                not isinstance(kid, str)
                or not kid
                or len(kid) > 256
                or any(ord(character) < 33 or ord(character) == 127 for character in kid)
                or kid in validated
            ):
                raise _ProviderIncompatible
            if value.get("use", "sig") != "sig":
                raise _ProviderIncompatible
            key_ops = value.get("key_ops")
            if key_ops is not None and (
                not isinstance(key_ops, list)
                or "verify" not in key_ops
                or any(operation != "verify" for operation in key_ops)
            ):
                raise _ProviderIncompatible
            if "d" in value:
                raise _ProviderIncompatible
            kty = value.get("kty")
            if not isinstance(kty, str):
                raise _ProviderIncompatible
            compatible = _compatible_key_algorithms(
                value, self._settings.allowed_algorithms
            )
            declared_algorithm = value.get("alg")
            if declared_algorithm is not None:
                if not isinstance(declared_algorithm, str) or declared_algorithm not in compatible:
                    raise _ProviderIncompatible
                compatible = frozenset({declared_algorithm})
            if not compatible:
                raise _ProviderIncompatible
            try:
                for algorithm in compatible:
                    candidate = dict(value)
                    candidate["alg"] = algorithm
                    jwt.PyJWK.from_dict(candidate)
            except (jwt.PyJWKError, ValueError, TypeError):
                raise _ProviderIncompatible from None
            validated[kid] = _ValidatedJWK(
                kid=kid,
                document=dict(value),
                algorithms=compatible,
            )
        return validated


__all__ = [
    "OIDCAuthenticationRuntime",
    "OIDCReadiness",
    "OIDCReadinessCode",
    "OIDCReadinessStatus",
]
