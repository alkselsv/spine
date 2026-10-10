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
    ("en-t-foo", "en-t-foo"),
    ("x-private", "x-private"),
    ("i-klingon", "i-klingon"),
)

INVALID_LANGUAGE_TAGS = ("en-zz", "de-biske", "en-z-foo", "en-a-foo")
