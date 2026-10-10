"""Pinned tables and identifiers used by source canonicalization."""

from __future__ import annotations

import re

REVISION_PROFILE = "r1-c14n-2026-10"
REVISION_SCHEMA = "source-revision:v1"
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
_SCRIPT = re.compile(r"^[A-Za-z]{4}$")
_REGION = re.compile(r"^(?:[A-Za-z]{2}|[0-9]{3})$")


def normalize_language_tag(value: str) -> str:
    pieces = value.split("-")
    if not pieces or pieces[0].lower() not in PRIMARY_LANGUAGE_TAGS:
        raise ValueError("document_language is not a valid BCP 47 tag")
    primary = pieces[0].lower()
    index = 1
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
    for piece in pieces[index:]:
        if not (2 <= len(piece) <= 8 and piece.isalnum() and piece.isascii()):
            raise ValueError("document_language is not a valid BCP 47 tag")
    return "-".join((primary, *pieces[1:])).lower() if len(pieces) == 1 else "-".join(
        [primary]
        + [
            (part.title() if _SCRIPT.fullmatch(part) else part.upper() if _REGION.fullmatch(part) else part.lower())
            for part in pieces[1:]
        ]
    )


__all__ = [
    "BCP47_TABLE_DIGEST",
    "OBSERVATION_SCHEMA",
    "REVISION_PROFILE",
    "REVISION_SCHEMA",
    "UNICODE_TABLE_DIGEST",
    "normalize_language_tag",
]
