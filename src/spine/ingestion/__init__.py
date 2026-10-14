"""Canonical ingestion pipeline.

The existing :mod:`spine.ingest` package remains a compatibility boundary
for the phase-one file loaders. New parsing adapters live here.
"""

from .parsers import MarkdownParser, PdfParser, TextParser, parse_document

__all__ = ["MarkdownParser", "PdfParser", "TextParser", "parse_document"]
