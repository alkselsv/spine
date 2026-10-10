"""Pinned tables and identifiers used by source canonicalization."""

from __future__ import annotations

import re
import hashlib
import json

REVISION_PROFILE = "r1-c14n-2026-10"
REVISION_SCHEMA = "source-revision:v1"
REVISION_METADATA_SCHEMA = "revision-metadata:r1-document-v1"
OBSERVATION_SCHEMA = "source-observation-command:v1"
UNICODE_TABLE_VERSION = "15.1.0"

# This is deliberately a repository-owned table rather than a locale-library
# lookup.  The profile is extended only by changing the pinned table version.
PRIMARY_LANGUAGE_TAGS = frozenset(
    "ar de en es fa fi fr he hi it ja ko nl no pl pt ro ru sr sv th tr uk vi zh und"
    " af am az be bg bn ca cs da el et eu gl hr hu id is lt lv ms sk sl sw ta te"
    " ur uz cy eo ga la mk mn sq hy ka kk km lo ne pa si so zu"
    " eng deu fra ita spa por rus jpn kor zho"
    .split()
)
SCRIPT_TAGS = frozenset({"Arab", "Cyrl", "Deva", "Ethi", "Hans", "Hant", "Hebr", "Jpan", "Kore", "Latn", "Thai"})
REGION_TAGS = frozenset(
    "001 US GB CA AU NZ IE FR DE ES IT PT BR MX AR IN CN JP KR RU UA PL SE NO FI DK NL BE AT CH RS ZA IL SA AE SG TW HK CZ GR RO HU BG SK SI HR"
    .split()
)
GRANDFATHERED_TAGS = frozenset(
    {
        "art-lojban", "cel-gaulish", "en-gb-oed", "i-ami", "i-bnn", "i-default",
        "i-enochian", "i-hak", "i-klingon", "i-lux", "i-mingo", "i-navajo",
        "i-pwn", "i-tao", "i-tay", "i-tsu", "no-bok", "no-nyn", "sgn-be-fr",
        "sgn-be-nl", "sgn-ch-de", "zh-guoyu", "zh-hakka", "zh-min", "zh-min-nan",
        "zh-xiang",
    }
)
_SCRIPT = re.compile(r"^[A-Za-z]{4}$")
_REGION = re.compile(r"^(?:[A-Za-z]{2}|[0-9]{3})$")
_VARIANT = re.compile(r"^(?:[0-9][A-Za-z0-9]{3}|[A-Za-z0-9]{5,8})$")
_EXTENSION = re.compile(r"^[A-Za-z0-9]$")
_SUBTAG = re.compile(r"^[A-Za-z0-9]{2,8}$")
UNICODE_WHITESPACE_CODEPOINTS = (
    0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x0020, 0x0085, 0x00A0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007,
    0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
)

_UNICODE_TABLE_PAYLOAD = {
    "unicode_version": UNICODE_TABLE_VERSION,
    "whitespace_codepoints": UNICODE_WHITESPACE_CODEPOINTS,
}
_BCP47_TABLE_PAYLOAD = {
    "primary_languages": PRIMARY_LANGUAGE_TAGS,
    "scripts": SCRIPT_TAGS,
    "regions": REGION_TAGS,
    "grandfathered": GRANDFATHERED_TAGS,
}


def _table_digest(payload: dict[str, object]) -> str:
    serializable = {
        key: sorted(value) if isinstance(value, (set, frozenset, tuple)) else value
        for key, value in payload.items()
    }
    return hashlib.sha256(
        json.dumps(serializable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


BCP47_TABLE_DIGEST = _table_digest(_BCP47_TABLE_PAYLOAD)

# Profile-owned NFC composition data for the canonical text forms used by the
# R1 source metadata contract. The table is intentionally immutable and does
# not consult the host Unicode database.
_NFC_COMPOSITIONS = {
    "A\u0300": "À", "A\u0301": "Á", "A\u0302": "Â", "A\u0303": "Ã", "A\u0308": "Ä", "A\u030A": "Å",
    "C\u0327": "Ç", "E\u0300": "È", "E\u0301": "É", "E\u0302": "Ê", "E\u0308": "Ë",
    "I\u0300": "Ì", "I\u0301": "Í", "I\u0302": "Î", "I\u0308": "Ï", "N\u0303": "Ñ",
    "O\u0300": "Ò", "O\u0301": "Ó", "O\u0302": "Ô", "O\u0303": "Õ", "O\u0308": "Ö",
    "U\u0300": "Ù", "U\u0301": "Ú", "U\u0302": "Û", "U\u0308": "Ü", "Y\u0301": "Ý",
    "a\u0300": "à", "a\u0301": "á", "a\u0302": "â", "a\u0303": "ã", "a\u0308": "ä", "a\u030A": "å",
    "c\u0327": "ç", "e\u0300": "è", "e\u0301": "é", "e\u0302": "ê", "e\u0308": "ë",
    "i\u0300": "ì", "i\u0301": "í", "i\u0302": "î", "i\u0308": "ï", "n\u0303": "ñ",
    "o\u0300": "ò", "o\u0301": "ó", "o\u0302": "ô", "o\u0303": "õ", "o\u0308": "ö",
    "u\u0300": "ù", "u\u0301": "ú", "u\u0302": "û", "u\u0308": "ü", "y\u0301": "ý", "y\u0308": "ÿ",
    "S\u030C": "Š", "Z\u030C": "Ž", "s\u030C": "š", "z\u030C": "ž", "D\u030C": "Ď", "d\u030C": "ď",
    "L\u0301": "Ĺ", "l\u0301": "ĺ", "R\u0301": "Ŕ", "r\u0301": "ŕ", "T\u030C": "Ť", "t\u030C": "ť",
    "G\u0306": "Ğ", "g\u0306": "ğ", "I\u0304": "Ī", "i\u0304": "ī", "U\u0304": "Ū", "u\u0304": "ū",
}
_NFC_SINGLETONS = {"\u212B": "Å", "\u0340": "\u0300", "\u0341": "\u0301"}
_COMBINING_CLASS = {"\u0300": 230, "\u0301": 230, "\u0302": 230, "\u0303": 230, "\u0308": 230, "\u030A": 230, "\u030C": 230, "\u0327": 202, "\u0304": 230, "\u0306": 230}
_NFC_COMPOSITIONS = {
    "A\u0300": "\u00c0", "A\u0301": "\u00c1", "A\u0302": "\u00c2", "A\u0303": "\u00c3", "A\u0308": "\u00c4", "A\u030A": "\u00c5",
    "C\u0327": "\u00c7", "E\u0300": "\u00c8", "E\u0301": "\u00c9", "E\u0302": "\u00ca", "E\u0308": "\u00cb",
    "I\u0300": "\u00cc", "I\u0301": "\u00cd", "I\u0302": "\u00ce", "I\u0308": "\u00cf", "N\u0303": "\u00d1",
    "O\u0300": "\u00d2", "O\u0301": "\u00d3", "O\u0302": "\u00d4", "O\u0303": "\u00d5", "O\u0308": "\u00d6",
    "U\u0300": "\u00d9", "U\u0301": "\u00da", "U\u0302": "\u00db", "U\u0308": "\u00dc", "Y\u0301": "\u00dd",
    "a\u0300": "\u00e0", "a\u0301": "\u00e1", "a\u0302": "\u00e2", "a\u0303": "\u00e3", "a\u0308": "\u00e4", "a\u030A": "\u00e5",
    "c\u0327": "\u00e7", "e\u0300": "\u00e8", "e\u0301": "\u00e9", "e\u0302": "\u00ea", "e\u0308": "\u00eb",
    "i\u0300": "\u00ec", "i\u0301": "\u00ed", "i\u0302": "\u00ee", "i\u0308": "\u00ef", "n\u0303": "\u00f1",
    "o\u0300": "\u00f2", "o\u0301": "\u00f3", "o\u0302": "\u00f4", "o\u0303": "\u00f5", "o\u0308": "\u00f6",
    "u\u0300": "\u00f9", "u\u0301": "\u00fa", "u\u0302": "\u00fb", "u\u0308": "\u00fc", "y\u0301": "\u00fd", "y\u0308": "\u00ff",
    "S\u030C": "\u0160", "Z\u030C": "\u017d", "s\u030C": "\u0161", "z\u030C": "\u017e", "D\u030C": "\u010e", "d\u030C": "\u010f",
    "L\u0301": "\u0139", "l\u0301": "\u013a", "R\u0301": "\u0154", "r\u0301": "\u0155", "T\u030C": "\u0164", "t\u030C": "\u0165",
    "G\u0306": "\u011e", "g\u0306": "\u011f", "I\u0304": "\u012a", "i\u0304": "\u012b", "U\u0304": "\u016a", "u\u0304": "\u016b",
}
_NFC_SINGLETONS = {"\u212B": "\u00c5", "\u0340": "\u0300", "\u0341": "\u0301"}
_UNICODE_TABLE_PAYLOAD["composition_pairs"] = sorted(_NFC_COMPOSITIONS.items())
_UNICODE_TABLE_PAYLOAD["singleton_mappings"] = sorted(_NFC_SINGLETONS.items())
UNICODE_TABLE_DIGEST = _table_digest(_UNICODE_TABLE_PAYLOAD)


def normalize_nfc(value: str) -> str:
    """Apply the profile's vendored normalization table deterministically."""

    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(_NFC_SINGLETONS.get(character, character) for character in value)
    ordered: list[str] = []
    for character in value:
        if character in _COMBINING_CLASS:
            combining_class = _COMBINING_CLASS[character]
            position = len(ordered)
            while position and _COMBINING_CLASS.get(ordered[position - 1], 0) > combining_class:
                position -= 1
            ordered.insert(position, character)
        else:
            ordered.append(character)
    result: list[str] = []
    for character in ordered:
        if result and character in _COMBINING_CLASS:
            pair = result[-1] + character
            composed = _NFC_COMPOSITIONS.get(pair)
            if composed is not None:
                result[-1] = composed
                continue
        result.append(character)
    return "".join(result)


def normalize_language_tag(value: str) -> str:
    if value.lower() in GRANDFATHERED_TAGS:
        return value.lower()
    pieces = value.split("-")
    if pieces and pieces[0].lower() == "x":
        if len(pieces) < 2 or any(not 1 <= len(part) <= 8 or not part.isalnum() for part in pieces[1:]):
            raise ValueError("document_language is not a valid BCP 47 tag")
        return value.lower()
    if not pieces or pieces[0].lower() not in PRIMARY_LANGUAGE_TAGS:
        raise ValueError("document_language is not a valid BCP 47 tag")
    primary = pieces[0].lower()
    index = 1
    while index < len(pieces) and len(pieces[index]) == 3 and pieces[index].isalpha() and primary in {"zh", "en", "de", "fr", "es"}:
        index += 1
    if index < len(pieces) and _SCRIPT.fullmatch(pieces[index]):
        script = pieces[index].title()
        if script not in SCRIPT_TAGS:
            raise ValueError("document_language is not a valid BCP 47 tag")
        index += 1
    if index < len(pieces) and _REGION.fullmatch(pieces[index]):
        region = pieces[index].upper()
        if region not in REGION_TAGS:
            raise ValueError("document_language is not a valid BCP 47 tag")
        index += 1
    variants_seen: set[str] = set()
    while index < len(pieces) and _VARIANT.fullmatch(pieces[index]):
        variant = pieces[index].lower()
        if variant in variants_seen:
            raise ValueError("document_language is not a valid BCP 47 tag")
        variants_seen.add(variant)
        index += 1
    extension_singletons: set[str] = set()
    while index < len(pieces):
        if pieces[index].lower() == "x":
            index += 1
            if index >= len(pieces) or any(not 1 <= len(part) <= 8 or not part.isalnum() for part in pieces[index:]):
                raise ValueError("document_language is not a valid BCP 47 tag")
            return value.lower()
        if not _EXTENSION.fullmatch(pieces[index]):
            raise ValueError("document_language is not a valid BCP 47 tag")
        singleton = pieces[index].lower()
        if singleton == "x" or singleton in extension_singletons:
            raise ValueError("document_language is not a valid BCP 47 tag")
        extension_singletons.add(singleton)
        index += 1
        start = index
        while index < len(pieces) and _SUBTAG.fullmatch(pieces[index]) and len(pieces[index]) >= 2:
            index += 1
        if index == start:
            raise ValueError("document_language is not a valid BCP 47 tag")
    return value.lower()


__all__ = [
    "BCP47_TABLE_DIGEST",
    "UNICODE_WHITESPACE_CODEPOINTS",
    "UNICODE_TABLE_VERSION",
    "OBSERVATION_SCHEMA",
    "REVISION_PROFILE",
    "REVISION_METADATA_SCHEMA",
    "REVISION_SCHEMA",
    "UNICODE_TABLE_DIGEST",
    "normalize_language_tag",
    "normalize_nfc",
]
