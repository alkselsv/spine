"""Pinned IANA BCP 47 extension registry used by the R1 profile.

The language-subtag registry is stored in ``profile_data.py``.  IANA's
extension registry is a separate, much smaller registry; keeping it as a
separate signed artifact prevents the validator from accepting arbitrary
singleton extensions merely because they satisfy the BCP 47 grammar.
"""

from __future__ import annotations

import hashlib
import json


EXTENSION_REGISTRY_VERSION = "2026-09-17"
EXTENSION_DATA = {
    "extensions": ["t", "u"],
    "registry_version": EXTENSION_REGISTRY_VERSION,
}
EXTENSION_TABLE_DIGEST = "ffa82e8366c930f65f7bace633603d3eaae85436c24853d465edda71193958a1"


def _verify() -> None:
    canonical = json.dumps(
        EXTENSION_DATA,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != EXTENSION_TABLE_DIGEST:
        raise RuntimeError("Pinned BCP47 extension profile digest mismatch.")


_verify()

REGISTERED_EXTENSION_SINGLETONS = frozenset(EXTENSION_DATA["extensions"])

__all__ = [
    "EXTENSION_DATA",
    "EXTENSION_REGISTRY_VERSION",
    "EXTENSION_TABLE_DIGEST",
    "REGISTERED_EXTENSION_SINGLETONS",
]
