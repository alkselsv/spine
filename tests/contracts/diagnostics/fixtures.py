"""Deterministic fixtures shared by diagnostics contract consumers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from uuid import UUID

from spine.application.diagnostics import DiagnosticContext, StructuredError


def synthetic_uuid(value: int) -> UUID:
    """Return a stable non-zero UUID for a contract scenario."""

    return UUID(int=value)


def deterministic_id_source(*values: int) -> Callable[[], UUID]:
    """Yield stable identifiers for code that accepts an ID source."""

    identifiers = iter(synthetic_uuid(value) for value in values)
    return lambda: next(identifiers)


def diagnostic_context(
    trace: int = 1,
    correlation: int | None = None,
    causation: int | None = None,
) -> DiagnosticContext:
    """Build a valid context without wall-clock or random input."""

    return DiagnosticContext(
        trace_id=synthetic_uuid(trace),
        correlation_id=(synthetic_uuid(correlation) if correlation is not None else None),
        causation_id=(synthetic_uuid(causation) if causation is not None else None),
    )


ERROR_LEAK_CORPUS = (
    "SELECT password FROM credentials WHERE token = 'secret'",
    "Bearer production-token",
    "protected customer document excerpt",
    "provider raw error https://provider.invalid/v1/responses",
    "Traceback (most recent call last):\n  File '/srv/app.py', line 1",
)

DIAGNOSTIC_LEAK_CORPUS = ERROR_LEAK_CORPUS + (
    "production-token",
    "raw failure from provider",
    "system prompt containing customer instructions",
    "answer copied from a protected source",
    "chain_of_thought hidden model reasoning",
)


def assert_safe_structured_error(error: StructuredError) -> None:
    """Assert the public envelope has only its allowlisted fields."""

    assert set(error.model_dump()) == {
        "schema_version",
        "code",
        "safe_message",
        "retryability",
        "trace_id",
    }


def assert_downstream_diagnostic_context(
    context: DiagnosticContext,
    *,
    trace_id: UUID,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> None:
    """Apply the shared trace-lineage contract at a downstream public seam."""

    assert type(context) is DiagnosticContext
    assert context == DiagnosticContext(
        trace_id=trace_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


def assert_no_diagnostic_leaks(
    captures: Iterable[str],
    *,
    leak_corpus: Iterable[str] = DIAGNOSTIC_LEAK_CORPUS,
) -> None:
    """Prove protected fixture values do not cross a public/diagnostic seam."""

    combined = "\n".join(captures)
    for protected_value in leak_corpus:
        assert protected_value not in combined
