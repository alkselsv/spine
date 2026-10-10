"""Pinned tables and identifiers used by source canonicalization."""

from __future__ import annotations

import re

REVISION_PROFILE = "r1-c14n-2026-10"
REVISION_SCHEMA = "source-revision:v1"
REVISION_METADATA_SCHEMA = "revision-metadata:r1-document-v1"
OBSERVATION_SCHEMA = "source-observation-command:v1"
UNICODE_TABLE_DIGEST = "unicode15.1-whitespace-nfc-v1"
BCP47_TABLE_DIGEST = "bcp47-r1-2026-10"

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
    "OBSERVATION_SCHEMA",
    "REVISION_PROFILE",
    "REVISION_METADATA_SCHEMA",
    "REVISION_SCHEMA",
    "UNICODE_TABLE_DIGEST",
    "normalize_language_tag",
]
