# Diagnostic Event contracts and sinks

`spine.application.diagnostics` exposes non-canonical operational observations
through `DiagnosticEventRegistry` and the async `DiagnosticSink.emit(event)`
interface. Application callers do not import `logging`, OpenTelemetry or a
provider SDK.

## Safety contract

- Producers register one frozen Pydantic payload model with `extra="forbid"`
  for each event type and schema version, then seal the registry.
- Payloads may contain only deeply immutable safe values: bounded diagnostic
  identifiers, safe enums, counts, durations, timestamps, UUIDs and opaque
  versioned object references. Dictionaries, mutable collections, exceptions,
  free text and content-bearing fields are rejected.
- The registry builds a detached immutable event snapshot from an injected
  timestamp and the exact `DiagnosticContext`. Tenant scope is optional until it
  is trusted; an Environment cannot be recorded without its Workspace.
- Every sink revalidates the registry-issued context snapshot, so otherwise
  valid trace, correlation, causation or tenant substitutions fail before
  observation. `validate_diagnostic_event_for_context` additionally compares an
  event with the caller's exact trusted context and scope.

The default registry currently owns `failure.observed` and `outbox.delivery`
schema version 1. Their failure codes and delivery states use closed enums;
counts and durations remain typed and bounded. They do not accept raw errors,
provider payloads, computed fields or custom serializers.

## Adapters and failure behavior

`RecordingDiagnosticSink` retains validated detached snapshots for deterministic
tests. `JsonLoggingDiagnosticSink` writes one standard-library JSON record at the
event severity without arbitrary logging attributes, `exc_info` or traceback
serialization. Both adapters revalidate the event at the sink seam.

Diagnostic Events are best-effort observations, not evidence that a business
decision or state change occurred. Call `emit_diagnostic_safely` only after a
canonical outcome is established when sink unavailability must not rewrite that
outcome. Meaningful tenant-scoped commands, decisions and transitions continue
to use append-only Audit Events and their required durability rules.
