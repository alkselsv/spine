"""Framework-independent diagnostic context and structured error contracts."""

from spine.application.diagnostics.audit import (
    AccessDecisionAuditPayload,
    AuditEvent,
    AuditEventRegistry,
    AuditIdentifier,
    AuditOutcome,
    AuditReader,
    AuditWriter,
    CanonicalTransitionAuditPayload,
    CommandAuditPayload,
    FeedbackAuditPayload,
    OutboxDeliveryAuditPayload,
    RequiredAuditCoordinator,
    UnsupportedAuditEventError,
)
from spine.application.diagnostics.context import DiagnosticContext, IdentifierSource
from spine.application.diagnostics.errors import (
    DuplicateErrorCodeError,
    DuplicateExceptionMappingError,
    ErrorRegistryConfigurationError,
    ErrorMapping,
    Retryability,
    StructuredError,
    StructuredErrorRegistry,
    build_default_error_registry,
)

__all__ = [
    "AccessDecisionAuditPayload",
    "AuditEvent",
    "AuditEventRegistry",
    "AuditIdentifier",
    "AuditOutcome",
    "AuditReader",
    "AuditWriter",
    "CanonicalTransitionAuditPayload",
    "CommandAuditPayload",
    "DiagnosticContext",
    "DuplicateErrorCodeError",
    "DuplicateExceptionMappingError",
    "ErrorMapping",
    "ErrorRegistryConfigurationError",
    "IdentifierSource",
    "FeedbackAuditPayload",
    "OutboxDeliveryAuditPayload",
    "Retryability",
    "RequiredAuditCoordinator",
    "StructuredError",
    "StructuredErrorRegistry",
    "UnsupportedAuditEventError",
    "build_default_error_registry",
]
