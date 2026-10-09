"""Trusted, immutable persistence contexts.

Only server-side composition roots, workers, and tests should hold a
``TrustedContextAuthority``. Transport payloads and provider claims must first be
authenticated and mapped by their owning boundary; this module intentionally has
no dictionary/header/token parser.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from enum import Enum
from typing import TypeAlias
from uuid import UUID

from spine.application.persistence.errors import InvalidPersistenceContextError


_TRUSTED_CONTEXT_SEAL = object()
_TRUSTED_AUTHORITY_SEAL = object()


def _require_identifier(value: object) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise InvalidPersistenceContextError("Persistence context is invalid.")
    return value


def _require_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPersistenceContextError("Persistence context is invalid.")
    return value


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """Authority restricted to exactly one Workspace identity."""

    workspace_id: UUID

    def __post_init__(self) -> None:
        _require_identifier(self.workspace_id)


@dataclass(frozen=True, slots=True)
class EnvironmentScope:
    """Authority restricted to one Environment in one Workspace."""

    workspace_id: UUID
    environment_id: UUID

    def __post_init__(self) -> None:
        _require_identifier(self.workspace_id)
        _require_identifier(self.environment_id)


PersistenceScope: TypeAlias = WorkspaceScope | EnvironmentScope


class ContextOrigin(str, Enum):
    INTERACTIVE = "interactive"
    WORKER = "worker"


class TrustedPersistenceContext:
    """Validated authority selected by a trusted server-side issuer."""

    __slots__ = (
        "scope",
        "origin",
        "purpose",
        "operation",
        "trace_id",
        "acting_subject_id",
        "service_principal_id",
    )

    scope: PersistenceScope
    origin: ContextOrigin
    purpose: str
    operation: str
    trace_id: UUID
    acting_subject_id: UUID | None
    service_principal_id: UUID | None

    def __init__(
        self,
        *,
        scope: PersistenceScope,
        origin: ContextOrigin,
        purpose: str,
        operation: str,
        trace_id: UUID,
        acting_subject_id: UUID | None = None,
        service_principal_id: UUID | None = None,
        _seal: object = None,
    ) -> None:
        if _seal is not _TRUSTED_CONTEXT_SEAL:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        if not isinstance(scope, (WorkspaceScope, EnvironmentScope)):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        _require_label(purpose)
        _require_label(operation)
        _require_identifier(trace_id)
        if origin is ContextOrigin.INTERACTIVE:
            _require_identifier(acting_subject_id)
        elif origin is ContextOrigin.WORKER:
            _require_identifier(service_principal_id)
        else:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "acting_subject_id", acting_subject_id)
        object.__setattr__(self, "service_principal_id", service_principal_id)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError(f"cannot delete field {name!r}")


class TrustedContextAuthority:
    """Capability held by trusted composition roots, workers, and tests."""

    __slots__ = ("_seal",)

    def __init__(self, seal: object) -> None:
        if seal is not _TRUSTED_AUTHORITY_SEAL:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        self._seal = seal

    def interactive(
        self,
        *,
        scope: PersistenceScope,
        acting_subject_id: UUID | None,
        purpose: str,
        operation: str,
        trace_id: UUID,
    ) -> TrustedPersistenceContext:
        self._validate_authority()
        return TrustedPersistenceContext(
            scope=scope,
            origin=ContextOrigin.INTERACTIVE,
            acting_subject_id=acting_subject_id,
            purpose=purpose,
            operation=operation,
            trace_id=trace_id,
            _seal=_TRUSTED_CONTEXT_SEAL,
        )

    def worker(
        self,
        *,
        scope: PersistenceScope,
        service_principal_id: UUID | None,
        purpose: str,
        operation: str,
        trace_id: UUID,
    ) -> TrustedPersistenceContext:
        self._validate_authority()
        return TrustedPersistenceContext(
            scope=scope,
            origin=ContextOrigin.WORKER,
            service_principal_id=service_principal_id,
            purpose=purpose,
            operation=operation,
            trace_id=trace_id,
            _seal=_TRUSTED_CONTEXT_SEAL,
        )

    def _validate_authority(self) -> None:
        if getattr(self, "_seal", None) is not _TRUSTED_AUTHORITY_SEAL:
            raise InvalidPersistenceContextError("Persistence context is invalid.")


def issue_trusted_context_authority() -> TrustedContextAuthority:
    """Authorize a server-side composition root to construct trusted contexts.

    Calling this function is itself a privileged composition-root decision. It
    accepts no client fields, claims, or tenant identifiers.
    """

    return TrustedContextAuthority(_TRUSTED_AUTHORITY_SEAL)
