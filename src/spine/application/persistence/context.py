"""Immutable persistence context data and verification contract.

Context data is not authority by itself. A persistence adapter accepts it only
after a composition-root-provided verifier authenticates its provenance and all
bound fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias
from uuid import UUID

from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.domain.common import ContextOrigin


_POLICY_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")


def _invalid_context() -> InvalidPersistenceContextError:
    return InvalidPersistenceContextError("Persistence context is invalid.")


def _require_identifier(value: object) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise _invalid_context()
    return value


def _require_policy_identifier(value: object) -> str:
    if not isinstance(value, str) or _POLICY_IDENTIFIER.fullmatch(value) is None:
        raise _invalid_context()
    return value


@dataclass(frozen=True, slots=True)
class PersistencePurpose:
    """Bounded server-selected purpose identifier."""

    value: str

    def __post_init__(self) -> None:
        _require_policy_identifier(self.value)


@dataclass(frozen=True, slots=True)
class PersistenceOperation:
    """Bounded server-selected operation identifier."""

    value: str

    def __post_init__(self) -> None:
        _require_policy_identifier(self.value)


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """Scope restricted to exactly one Workspace identity."""

    workspace_id: UUID

    def __post_init__(self) -> None:
        _require_identifier(self.workspace_id)


@dataclass(frozen=True, slots=True)
class EnvironmentScope:
    """Scope restricted to one Environment in one Workspace."""

    workspace_id: UUID
    environment_id: UUID

    def __post_init__(self) -> None:
        _require_identifier(self.workspace_id)
        _require_identifier(self.environment_id)


PersistenceScope: TypeAlias = WorkspaceScope | EnvironmentScope


@dataclass(frozen=True, slots=True)
class TrustedContextProvenance:
    """Opaque proof that a particular trusted boundary issued the context."""

    issuer_id: UUID
    signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require_identifier(self.issuer_id)
        if not isinstance(self.signature, bytes) or not self.signature:
            raise _invalid_context()


@dataclass(frozen=True, slots=True)
class TrustedPersistenceContext:
    """Context data whose authority must be verified before every UoW opens."""

    scope: PersistenceScope
    origin: ContextOrigin
    purpose: PersistencePurpose
    operation: PersistenceOperation
    trace_id: UUID
    acting_subject_id: UUID | None
    service_principal_id: UUID | None
    provenance: TrustedContextProvenance = field(repr=False)

    def validate_shape(self) -> None:
        """Revalidate every field without treating frozen data as trustworthy."""

        try:
            if not isinstance(self.scope, (WorkspaceScope, EnvironmentScope)):
                raise _invalid_context()
            _require_identifier(self.scope.workspace_id)
            if isinstance(self.scope, EnvironmentScope):
                _require_identifier(self.scope.environment_id)
            if not isinstance(self.purpose, PersistencePurpose):
                raise _invalid_context()
            if not isinstance(self.operation, PersistenceOperation):
                raise _invalid_context()
            _require_policy_identifier(self.purpose.value)
            _require_policy_identifier(self.operation.value)
            _require_identifier(self.trace_id)
            if not isinstance(self.provenance, TrustedContextProvenance):
                raise _invalid_context()
            _require_identifier(self.provenance.issuer_id)
            if not isinstance(self.provenance.signature, bytes) or not self.provenance.signature:
                raise _invalid_context()
            if self.origin is ContextOrigin.INTERACTIVE:
                _require_identifier(self.acting_subject_id)
                if self.service_principal_id is not None:
                    _require_identifier(self.service_principal_id)
            elif self.origin is ContextOrigin.WORKER:
                if self.acting_subject_id is not None:
                    raise _invalid_context()
                _require_identifier(self.service_principal_id)
            else:
                raise _invalid_context()
        except (AttributeError, TypeError):
            raise _invalid_context() from None


class TrustedContextVerifier(Protocol):
    """Authenticate source context and return a detached trusted snapshot."""

    def verify(self, context: TrustedPersistenceContext) -> TrustedPersistenceContext: ...
