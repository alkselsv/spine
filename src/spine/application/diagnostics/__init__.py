"""Framework-independent diagnostic context and structured error contracts."""

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
    "DiagnosticContext",
    "DuplicateErrorCodeError",
    "DuplicateExceptionMappingError",
    "ErrorMapping",
    "ErrorRegistryConfigurationError",
    "IdentifierSource",
    "Retryability",
    "StructuredError",
    "StructuredErrorRegistry",
    "build_default_error_registry",
]
