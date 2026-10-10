from __future__ import annotations

import inspect
from uuid import UUID

import pytest

import spine.infrastructure.diagnostics as diagnostics
from spine.infrastructure.diagnostics.traceparent import (
    InvalidTraceParentError,
    TraceParent,
    context_from_traceparent,
    format_traceparent,
    parse_traceparent,
)

from ...contracts.diagnostics.fixtures import deterministic_id_source, synthetic_uuid


VALID_TRACEPARENT = "00-00000000000000000000000000000001-0000000000000002-01"


def test_diagnostics_package_exports_trace_and_sink_adapters() -> None:
    assert set(diagnostics.__all__) == {
        "InvalidTraceParentError",
        "JsonLoggingDiagnosticSink",
        "RecordingDiagnosticSink",
        "TraceParent",
        "context_from_traceparent",
        "format_traceparent",
        "parse_traceparent",
    }


def test_valid_traceparent_continues_trace_identity_and_formats_canonically() -> None:
    parsed = parse_traceparent(VALID_TRACEPARENT)

    assert parsed.trace_id == synthetic_uuid(1)
    assert parsed.parent_id == "0000000000000002"
    assert format_traceparent(parsed) == VALID_TRACEPARENT

    context = context_from_traceparent(VALID_TRACEPARENT, id_source=deterministic_id_source(9))
    assert context.trace_id == synthetic_uuid(1)


def test_future_traceparent_version_preserves_trace_identity_and_extensions() -> None:
    value = "01-00000000000000000000000000000001-0000000000000002-01-deadbeef"

    parsed = parse_traceparent(value)

    assert parsed.trace_id == synthetic_uuid(1)
    assert parsed.extra == ("deadbeef",)
    assert format_traceparent(parsed) == value


@pytest.mark.parametrize(
    "traceparent",
    [
        None,
        "",
        "not-a-traceparent",
        "00-00000000000000000000000000000000-0000000000000002-01",
        "00-00000000000000000000000000000001-0000000000000000-01",
        "ff-00000000000000000000000000000001-0000000000000002-01",
        "00-00000000000000000000000000000001-0000000000000002-0g",
    ],
)
def test_missing_or_malformed_traceparent_falls_back_to_injected_trusted_trace(traceparent: str | None) -> None:
    context = context_from_traceparent(traceparent, id_source=deterministic_id_source(7))

    assert context.trace_id == synthetic_uuid(7)


def test_parser_rejects_invalid_traceparent_without_exposing_input() -> None:
    with pytest.raises(InvalidTraceParentError) as raised:
        parse_traceparent("00-00000000000000000000000000000000-secret-provider-error-01")

    assert str(raised.value) == "The traceparent value is invalid."


def test_traceparent_rejects_zero_identifiers_when_constructed() -> None:
    with pytest.raises(ValueError):
        TraceParent(
            version="00",
            trace_id=UUID(int=0),
            parent_id="0000000000000002",
            trace_flags=1,
        )


def test_trusted_trace_adapter_has_no_request_body_query_or_custom_header_surface() -> None:
    assert set(inspect.signature(context_from_traceparent).parameters) == {
        "traceparent",
        "id_source",
    }
