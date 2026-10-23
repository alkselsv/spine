"""Bounded failure signals crossing qualification adapters into ingestion."""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from uuid import UUID


class IngestionFaultBoundary(str, Enum):
    PARSER = "parser"
    ORIGINAL_STORAGE = "original_storage"
    PRE_ACCEPTANCE = "pre_acceptance"


class IngestionFaultReason(str, Enum):
    PARSER_RETRYABLE = "parser_retryable"
    ORIGINAL_STORAGE_UNAVAILABLE = "original_storage_unavailable"
    PRE_ACCEPTANCE_RETRYABLE = "pre_acceptance_retryable"


class IngestionFaultInjected(RuntimeError):
    """An injected fault with a bounded reason and no arbitrary detail."""

    def __init__(self, reason_code: IngestionFaultReason) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


class IngestionFaultInjector(Protocol):
    """Application port for exact-identity, qualification-only fault gates."""

    def inject_if_bound(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        command_identity: str,
        boundary: IngestionFaultBoundary,
    ) -> None: ...


__all__ = [
    "IngestionFaultBoundary",
    "IngestionFaultInjected",
    "IngestionFaultInjector",
    "IngestionFaultReason",
]
