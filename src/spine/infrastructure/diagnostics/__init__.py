"""Infrastructure adapters for trusted diagnostic ingress and safe sinks."""

from spine.infrastructure.diagnostics.sinks import (
    JsonLoggingDiagnosticSink,
    RecordingDiagnosticSink,
)

from spine.infrastructure.diagnostics.traceparent import (
    InvalidTraceParentError,
    TraceParent,
    context_from_traceparent,
    format_traceparent,
    parse_traceparent,
)

__all__ = [
    "InvalidTraceParentError",
    "JsonLoggingDiagnosticSink",
    "RecordingDiagnosticSink",
    "TraceParent",
    "context_from_traceparent",
    "format_traceparent",
    "parse_traceparent",
]
