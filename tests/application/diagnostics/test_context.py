from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.diagnostics import DiagnosticContext

from ...contracts.diagnostics.fixtures import diagnostic_context, deterministic_id_source, synthetic_uuid


def test_context_requires_non_zero_trace_id() -> None:
    with pytest.raises(ValidationError):
        DiagnosticContext(trace_id=UUID(int=0))


def test_context_rejects_zero_optional_identifiers() -> None:
    with pytest.raises(ValidationError):
        DiagnosticContext(trace_id=synthetic_uuid(1), correlation_id=UUID(int=0))

    with pytest.raises(ValidationError):
        DiagnosticContext(trace_id=synthetic_uuid(1), causation_id=UUID(int=0))


def test_context_rejects_extra_fields_and_is_immutable() -> None:
    with pytest.raises(ValidationError):
        DiagnosticContext(trace_id=synthetic_uuid(1), unexpected="value")

    context = diagnostic_context()
    with pytest.raises(ValidationError):
        context.trace_id = synthetic_uuid(2)


def test_context_creation_uses_injected_identifier_source() -> None:
    source = deterministic_id_source(9)

    context = DiagnosticContext.create(id_source=source)

    assert context.trace_id == synthetic_uuid(9)


def test_nested_context_preserves_trace_and_correlation_and_replaces_causation() -> None:
    parent = diagnostic_context(trace=1, correlation=2, causation=3)

    child = parent.nested(synthetic_uuid(4))

    assert child.trace_id == parent.trace_id
    assert child.correlation_id == parent.correlation_id
    assert child.causation_id == synthetic_uuid(4)


def test_context_can_transition_correlation_without_changing_trace_or_causation() -> None:
    parent = diagnostic_context(trace=1, correlation=2, causation=3)

    transitioned = parent.with_correlation(synthetic_uuid(5))
    cleared = transitioned.with_correlation(None)

    assert transitioned.trace_id == parent.trace_id
    assert transitioned.causation_id == parent.causation_id
    assert transitioned.correlation_id == synthetic_uuid(5)
    assert cleared.correlation_id is None
