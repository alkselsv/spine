"""Independent source-canonicalization vectors shared by both adapters."""

from __future__ import annotations


UNICODE_VECTORS = (
    ("Cafe\u0301", "Caf\u00e9"),
    ("\u1100\u1161", "\uac00"),
    ("\uac00\u11a8", "\uac01"),
    ("A\u030a", "\u00c5"),
    ("\u00a0 Cafe\u0301 \u2003", "\u00a0 Caf\u00e9 \u2003"),
)

LANGUAGE_VECTORS = (
    ("zh-cmn-Hans-CN", "zh-cmn-hans-cn"),
    ("sl-ROZAJ-BISKE", "sl-rozaj-biske"),
    ("en-fonipa", "en-fonipa"),
    ("en-t-en", "en-t-en"),
    ("en-t-de", "en-t-de"),
    ("en-t-de-Latn-US", "en-t-de-latn-us"),
    ("en-t-sl-Rozaj-Biske", "en-t-sl-rozaj-biske"),
    ("en-t-de-d0-ascii", "en-t-de-d0-ascii"),
    ("en-u-ca-gregory", "en-u-ca-gregory"),
    ("en-u-nu-latn", "en-u-nu-latn"),
    ("en-t-en-latn-us", "en-t-en-latn-us"),
    ("en-u-ca-gregory-t-en", "en-u-ca-gregory-t-en"),
    ("de-DE-u-ca-gregory-nu-latn", "de-de-u-ca-gregory-nu-latn"),
    ("sr-Latn-RS-u-co-phonebk", "sr-latn-rs-u-co-phonebk"),
    ("en-t-d0-ascii", "en-t-d0-ascii"),
    ("x-private", "x-private"),
    ("i-klingon", "i-klingon"),
)

INVALID_LANGUAGE_TAGS = (
    "en-zz",
    "de-biske",
    "en-z-foo",
    "en-a-foo",
    "en-t-foo",
    "en-t-de-foo",
    "en-t-de-zz",
    "en-t-de-rozaj",
    "en-u-zz",
    "en-u-ca-unknown",
    "en-u-ca-gregory-ca-buddhist",
    "en-t-d0-unknown",
    "en-t-d0-ascii-d0-upper",
)
