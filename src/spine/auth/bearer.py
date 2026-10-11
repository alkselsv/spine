"""Framework-independent HTTP Bearer credential grammar."""

from __future__ import annotations

import re


_BEARER_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


def is_http_bearer_token(value: str) -> bool:
    """Return whether *value* is one complete RFC 6750 ``b64token`` value."""

    return bool(value) and _BEARER_TOKEN.fullmatch(value) is not None


__all__ = ["is_http_bearer_token"]
