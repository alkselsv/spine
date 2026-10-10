"""The repository-owned ``r1-c14n-2026-10`` canonicalization profile."""

from __future__ import annotations

import hashlib
import json
import re

from .profile_data import (
    BCP47_DATA,
    BCP47_REGISTRY_VERSION,
    BCP47_REGISTRY_DIGEST,
    BCP47_TABLE_DIGEST,
    UNICODE_DATA,
    UNICODE_TABLE_DIGEST,
    UNICODE_TABLE_VERSION,
)
from .profile_extensions import (
    EXTENSION_TABLE_DIGEST,
    REGISTERED_EXTENSION_SINGLETONS,
    TRANSFORMED_EXTENSION_VALUES,
    UNICODE_EXTENSION_VALUES,
)

REVISION_PROFILE = "r1-c14n-2026-10"
REVISION_SCHEMA = "source-revision:v1"
REVISION_METADATA_SCHEMA = "revision-metadata:r1-document-v1"
OBSERVATION_SCHEMA = "source-observation-command:v1"

PRIMARY_LANGUAGE_TAGS = frozenset(BCP47_DATA["languages"])
EXTLANG_TAGS = frozenset(BCP47_DATA["extlangs"])
SCRIPT_TAGS = frozenset(BCP47_DATA["scripts"])
REGION_TAGS = frozenset(BCP47_DATA["regions"])
VARIANT_TAGS = frozenset(BCP47_DATA["variants"])
GRANDFATHERED_TAGS = frozenset(BCP47_DATA["special"])
UNICODE_WHITESPACE_CODEPOINTS = tuple(UNICODE_DATA["whitespace"])

_combined_bcp47_digest = hashlib.sha256(
    json.dumps(
        {"bcp47_registry": BCP47_REGISTRY_DIGEST, "extension_table": EXTENSION_TABLE_DIGEST},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
if _combined_bcp47_digest != BCP47_TABLE_DIGEST:
    raise RuntimeError("Pinned BCP47 profile digest mismatch.")


def _registry_prefixes(record_type: str) -> dict[str, frozenset[str]]:
    prefixes: dict[str, set[str]] = {}
    for record in BCP47_DATA["records"]:
        if record.get("Type") != [record_type]:
            continue
        subtags = record.get("Subtag") or record.get("Tag")
        if not subtags:
            continue
        prefixes[subtags[0].lower()] = {
            prefix.lower() for prefix in record.get("Prefix", [])
        } or {"*"}
    return {subtag: frozenset(values) for subtag, values in prefixes.items()}


_EXTLANG_PREFIXES = _registry_prefixes("extlang")
_VARIANT_PREFIXES = _registry_prefixes("variant")

_DECOMPOSITIONS = {int(key): tuple(value) for key, value in UNICODE_DATA["decomp"].items()}
_COMBINING_CLASSES = {int(key): value for key, value in UNICODE_DATA["ccc"].items()}
_COMPOSITIONS = {
    tuple(int(part) for part in key.split(",")): value
    for key, value in UNICODE_DATA["compose"].items()
}
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}$")
_EXTLANG = re.compile(r"^[A-Za-z]{3}$")
_SCRIPT = re.compile(r"^[A-Za-z]{4}$")
_REGION = re.compile(r"^(?:[A-Za-z]{2}|[0-9]{3})$")
_VARIANT = re.compile(r"^(?:[0-9][A-Za-z0-9]{3}|[A-Za-z0-9]{5,8})$")
_SINGLETON = re.compile(r"^[0-9A-WY-Za-wy-z]$")
_SUBTAG = re.compile(r"^[A-Za-z0-9]{2,8}$")

# Unicode Hangul syllable composition is algorithmic and is not stored in
# UnicodeData.txt.  These constants are part of the Unicode 15.1 algorithm.
_SBASE, _LBASE, _VBASE, _TBASE = 0xAC00, 0x1100, 0x1161, 0x11A7
_LCOUNT, _VCOUNT, _TCOUNT = 19, 21, 28
_NCOUNT, _SCOUNT = _VCOUNT * _TCOUNT, _LCOUNT * _VCOUNT * _TCOUNT


def _hangul_decompose(codepoint: int) -> tuple[int, ...] | None:
    offset = codepoint - _SBASE
    if 0 <= offset < _SCOUNT:
        l = _LBASE + offset // _NCOUNT
        v = _VBASE + (offset % _NCOUNT) // _TCOUNT
        t = offset % _TCOUNT
        return (l, v, _TBASE + t) if t else (l, v)
    return None


def _canonical_decompose(codepoint: int) -> list[int]:
    decomposition = _hangul_decompose(codepoint) or _DECOMPOSITIONS.get(codepoint)
    if decomposition is None:
        return [codepoint]
    result: list[int] = []
    for part in decomposition:
        result.extend(_canonical_decompose(part))
    return result


def _compose_pair(left: int, right: int) -> int | None:
    if _LBASE <= left < _LBASE + _LCOUNT and _VBASE <= right < _VBASE + _VCOUNT:
        return _SBASE + ((left - _LBASE) * _VCOUNT + right - _VBASE) * _TCOUNT
    if _SBASE <= left < _SBASE + _SCOUNT and (left - _SBASE) % _TCOUNT == 0:
        if _TBASE < right < _TBASE + _TCOUNT:
            return left + right - _TBASE
    return _COMPOSITIONS.get((left, right))


def normalize_nfc(value: str) -> str:
    """Normalize with the vendored Unicode 15.1 canonical-decomposition data."""

    value = value.replace("\r\n", "\n").replace("\r", "\n")
    decomposed = [part for codepoint in map(ord, value) for part in _canonical_decompose(codepoint)]
    ordered: list[int] = []
    for codepoint in decomposed:
        combining_class = _COMBINING_CLASSES.get(codepoint, 0)
        position = len(ordered)
        if combining_class:
            while position and _COMBINING_CLASSES.get(ordered[position - 1], 0) > combining_class:
                position -= 1
        ordered.insert(position, codepoint)

    result: list[int] = []
    starter = 0
    last_class = 0
    for codepoint in ordered:
        combining_class = _COMBINING_CLASSES.get(codepoint, 0)
        composed = _compose_pair(result[starter], codepoint) if result and (combining_class == 0 or last_class < combining_class) else None
        if composed is not None:
            result[starter] = composed
        else:
            if combining_class == 0:
                starter = len(result)
            result.append(codepoint)
            last_class = combining_class
    return "".join(map(chr, result))


def normalize_language_tag(value: str) -> str:
    """Validate a BCP 47 tag against the complete vendored IANA subtags."""

    pieces = value.split("-")
    lowered = [piece.lower() for piece in pieces]
    if not pieces or any(not piece for piece in pieces):
        raise ValueError("document_language is not a valid BCP 47 tag")
    if "-".join(lowered) in GRANDFATHERED_TAGS:
        return "-".join(lowered)
    if lowered[0] == "x":
        if len(pieces) < 2 or any(not 1 <= len(part) <= 8 or not part.isalnum() for part in pieces[1:]):
            raise ValueError("document_language is not a valid BCP 47 tag")
        return "-".join(lowered)
    index = _validate_language_core(lowered, 0)
    seen_extensions: set[str] = set()
    while index < len(pieces):
        if lowered[index] == "x":
            index += 1
            if index >= len(pieces) or any(not 1 <= len(part) <= 8 or not part.isalnum() for part in pieces[index:]):
                raise ValueError("document_language is not a valid BCP 47 tag")
            return "-".join(lowered)
        if (
            not _SINGLETON.fullmatch(pieces[index])
            or lowered[index] not in REGISTERED_EXTENSION_SINGLETONS
            or lowered[index] in seen_extensions
        ):
            raise ValueError("document_language is not a valid BCP 47 tag")
        seen_extensions.add(lowered[index])
        extension = lowered[index]
        index += 1
        start = index
        if extension == "u":
            current_key: str | None = None
            seen_keys: set[str] = set()
            while index < len(pieces) and lowered[index] not in REGISTERED_EXTENSION_SINGLETONS | {"x"}:
                token = lowered[index]
                if len(token) == 2:
                    if (
                        token not in UNICODE_EXTENSION_VALUES
                        or token in seen_keys
                        or (seen_keys and token <= max(seen_keys))
                    ):
                        raise ValueError("document_language is not a valid BCP 47 tag")
                    current_key = token
                    seen_keys.add(token)
                elif (
                    current_key is None
                    or token not in UNICODE_EXTENSION_VALUES[current_key]
                ):
                    raise ValueError("document_language is not a valid BCP 47 tag")
                index += 1
        elif extension == "t":
            current_key = None
            if index < len(pieces) and lowered[index] in PRIMARY_LANGUAGE_TAGS:
                index = _validate_language_core(lowered, index)
            seen_keys: set[str] = set()
            while index < len(pieces) and lowered[index] not in REGISTERED_EXTENSION_SINGLETONS | {"x"}:
                token = lowered[index]
                if len(token) != 2 or token not in TRANSFORMED_EXTENSION_VALUES or token in seen_keys:
                    raise ValueError("document_language is not a valid BCP 47 tag")
                seen_keys.add(token)
                current_key = token
                index += 1
                if index >= len(pieces) or lowered[index] not in TRANSFORMED_EXTENSION_VALUES[current_key]:
                    raise ValueError("document_language is not a valid BCP 47 tag")
                index += 1
        else:
            raise ValueError("document_language is not a valid BCP 47 tag")
        if index == start:
            raise ValueError("document_language is not a valid BCP 47 tag")
    return "-".join(lowered)


def _validate_language_core(parts: list[str], start: int) -> int:
    """Validate a BCP47 language sequence and return the next unconsumed index."""

    if start >= len(parts) or not _LANGUAGE.fullmatch(parts[start]) or parts[start] not in PRIMARY_LANGUAGE_TAGS:
        raise ValueError("document_language is not a valid BCP 47 tag")
    index = start + 1
    extlang_count = 0
    while index < len(parts) and _EXTLANG.fullmatch(parts[index]):
        if parts[index] not in EXTLANG_TAGS or extlang_count >= 3:
            raise ValueError("document_language is not a valid BCP 47 tag")
        prefix = "-".join(parts[start:index])
        if (
            prefix not in _EXTLANG_PREFIXES[parts[index]]
            and "*" not in _EXTLANG_PREFIXES[parts[index]]
        ):
            raise ValueError("document_language is not a valid BCP 47 tag")
        extlang_count += 1
        index += 1
    if index < len(parts) and _SCRIPT.fullmatch(parts[index]) and parts[index] in {tag.lower() for tag in SCRIPT_TAGS}:
        index += 1
    elif index < len(parts) and _SCRIPT.fullmatch(parts[index]):
        raise ValueError("document_language is not a valid BCP 47 tag")
    if index < len(parts) and _REGION.fullmatch(parts[index]):
        region = parts[index]
        if region not in REGION_TAGS or region == "zz":
            raise ValueError("document_language is not a valid BCP 47 tag")
        index += 1
    seen_variants: set[str] = set()
    while index < len(parts) and _VARIANT.fullmatch(parts[index]):
        variant = parts[index]
        prefix = "-".join(parts[start:index])
        if (
            variant not in VARIANT_TAGS
            or variant in seen_variants
            or (
                prefix not in _VARIANT_PREFIXES[variant]
                and "*" not in _VARIANT_PREFIXES[variant]
            )
        ):
            raise ValueError("document_language is not a valid BCP 47 tag")
        seen_variants.add(variant)
        index += 1
    return index


__all__ = [
    "BCP47_REGISTRY_VERSION",
    "BCP47_REGISTRY_DIGEST",
    "BCP47_TABLE_DIGEST",
    "GRANDFATHERED_TAGS",
    "OBSERVATION_SCHEMA",
    "PRIMARY_LANGUAGE_TAGS",
    "REGION_TAGS",
    "REVISION_METADATA_SCHEMA",
    "REVISION_PROFILE",
    "REVISION_SCHEMA",
    "SCRIPT_TAGS",
    "UNICODE_TABLE_DIGEST",
    "UNICODE_TABLE_VERSION",
    "UNICODE_WHITESPACE_CODEPOINTS",
    "normalize_language_tag",
    "normalize_nfc",
]
