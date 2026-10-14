"""Canonical ingestion pipeline.

The existing :mod:`spine.ingest` package remains a compatibility boundary
for the phase-one file loaders. New parsing adapters live here.
"""

from .parsers import (
    DocxParser,
    MarkdownParser,
    PdfParser,
    TextParser,
    XlsxParser,
    parse_document,
    resolve_xlsx_locator,
)

__all__ = [
    "MarkdownParser",
    "DocxParser",
    "PdfParser",
    "TextParser",
    "XlsxParser",
    "parse_document",
    "resolve_xlsx_locator",
]
