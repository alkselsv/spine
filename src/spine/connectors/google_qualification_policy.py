"""Immutable provider policy shared by Google Phase 1 qualification paths."""

from __future__ import annotations


GOOGLE_PHASE1_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
)


__all__ = ["GOOGLE_PHASE1_READONLY_SCOPES"]
