"""Immutable diagnostic identity propagated across application boundaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, field_validator


class IdentifierSource(Protocol):
    """Source of identifiers, injectable for deterministic contract tests."""

    def __call__(self) -> UUID:
        ...


class DiagnosticContext(BaseModel):
    """Trusted diagnostic metadata that carries no authorization authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @field_validator("trace_id", "correlation_id", "causation_id")
    @classmethod
    def reject_zero_identifier(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("diagnostic identifiers must be non-zero")
        return value

    @classmethod
    def create(
        cls,
        *,
        id_source: IdentifierSource | Callable[[], UUID] = uuid4,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
    ) -> DiagnosticContext:
        """Create a context with a caller-supplied or production ID source."""

        return cls(
            trace_id=id_source(),
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def nested(self, causation_id: UUID) -> DiagnosticContext:
        """Create child work while preserving trace and correlation identity."""

        return type(self)(
            trace_id=self.trace_id,
            correlation_id=self.correlation_id,
            causation_id=causation_id,
        )

    def with_correlation(self, correlation_id: UUID | None) -> DiagnosticContext:
        """Set or clear the higher-level correlation identity."""

        return type(self)(
            trace_id=self.trace_id,
            correlation_id=correlation_id,
            causation_id=self.causation_id,
        )
