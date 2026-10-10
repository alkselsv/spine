"""Framework-independent W3C Trace Context parsing at an adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import UUID

from spine.application.diagnostics.context import DiagnosticContext, IdentifierSource


_TRACEPARENT_PATTERN = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})"
    r"(?P<extra>(?:-[0-9a-f]+)*)$"
)


class InvalidTraceParentError(ValueError):
    """A traceparent failed strict W3C validation without echoing input."""

    def __init__(self) -> None:
        super().__init__("The traceparent value is invalid.")


@dataclass(frozen=True, slots=True)
class TraceParent:
    """Validated W3C traceparent data independent of any web framework."""

    version: str
    trace_id: UUID
    parent_id: str
    trace_flags: int
    extra: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{2}", self.version) or self.version == "ff":
            raise ValueError("invalid traceparent version")
        if self.trace_id.int == 0:
            raise ValueError("traceparent trace_id must be non-zero")
        if not re.fullmatch(r"[0-9a-f]{16}", self.parent_id) or int(self.parent_id, 16) == 0:
            raise ValueError("traceparent parent_id must be a non-zero lowercase identifier")
        if not 0 <= self.trace_flags <= 0xFF:
            raise ValueError("traceparent flags must fit in one byte")
        if any(not re.fullmatch(r"[0-9a-f]+", field) for field in self.extra):
            raise ValueError("traceparent extension fields must be lowercase hexadecimal")
        if self.version == "00" and self.extra:
            raise ValueError("version 00 traceparent cannot contain extension fields")


def parse_traceparent(value: str) -> TraceParent:
    """Parse one strict version-00 W3C traceparent value."""

    if not isinstance(value, str):
        raise InvalidTraceParentError()
    match = _TRACEPARENT_PATTERN.fullmatch(value)
    if match is None or match.group("version") == "ff":
        raise InvalidTraceParentError()
    try:
        return TraceParent(
            version=match.group("version"),
            trace_id=UUID(hex=match.group("trace_id")),
            parent_id=match.group("parent_id"),
            trace_flags=int(match.group("flags"), 16),
            extra=tuple(filter(None, match.group("extra").split("-"))),
        )
    except (TypeError, ValueError) as error:
        raise InvalidTraceParentError() from error


def format_traceparent(value: TraceParent) -> str:
    """Format validated traceparent data in canonical lowercase form."""

    extension = "".join(f"-{field}" for field in value.extra)
    return f"{value.version}-{value.trace_id.hex}-{value.parent_id}-{value.trace_flags:02x}{extension}"


def context_from_traceparent(
    traceparent: str | None,
    *,
    id_source: IdentifierSource,
) -> DiagnosticContext:
    """Continue a valid trusted trace or create a fresh trusted trace.

    This adapter accepts only the explicitly supplied traceparent value. HTTP
    request composition, authorization and tenant context remain outside it.
    """

    if traceparent is not None:
        try:
            return DiagnosticContext(trace_id=parse_traceparent(traceparent).trace_id)
        except InvalidTraceParentError:
            pass
    return DiagnosticContext.create(id_source=id_source)
