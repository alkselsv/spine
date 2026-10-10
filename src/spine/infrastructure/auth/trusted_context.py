"""Request-local authorized context issuance and persistence verification."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from uuid import UUID, uuid4

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.auth.contracts import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
    _AUTHORIZED_REQUEST_CONTEXT_IMPLEMENTATION_KEY,
)
from spine.domain.common import ContextOrigin
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


_REQUEST_PROOF_SIZE = hashlib.sha256().digest_size


def _invalid_context() -> InvalidPersistenceContextError:
    return InvalidPersistenceContextError("Persistence context is invalid.")


@dataclass(frozen=True, slots=True)
class _RequestSnapshot:
    request_id: UUID
    acting_subject_id: UUID
    workspace_id: UUID
    environment_id: UUID
    roles: frozenset[AuthorizationRole]
    authorization_generation: int
    authentication_configuration_version: str
    purpose: str
    operation: str
    service_principal_id: UUID | None
    trace_id: UUID
    correlation_id: UUID | None
    causation_id: UUID | None


@dataclass(frozen=True, slots=True)
class _RequestProvenance:
    issuer_id: UUID
    signature: bytes


class _IssuedAuthorizedRequestContext(
    AuthorizedRequestContext[DiagnosticContext],
    _implementation_key=_AUTHORIZED_REQUEST_CONTEXT_IMPLEMENTATION_KEY,
):
    __slots__ = ("_guard", "_provenance", "_snapshot")

    def __init__(
        self,
        *,
        snapshot: _RequestSnapshot,
        provenance: _RequestProvenance,
        guard: Callable[["_IssuedAuthorizedRequestContext"], None],
    ) -> None:
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_provenance", provenance)

    def _require_active(self) -> _RequestSnapshot:
        self._guard(self)
        return self._snapshot

    @property
    def acting_subject_id(self) -> UUID:
        return UUID(int=self._require_active().acting_subject_id.int)

    @property
    def scope(self) -> RequestedAuthorizationScope:
        snapshot = self._require_active()
        return RequestedAuthorizationScope(
            workspace_id=snapshot.workspace_id,
            environment_id=snapshot.environment_id,
        )

    @property
    def roles(self) -> frozenset[AuthorizationRole]:
        return frozenset(self._require_active().roles)

    @property
    def authorization_generation(self) -> int:
        return self._require_active().authorization_generation

    @property
    def authentication_configuration_version(self) -> str:
        return self._require_active().authentication_configuration_version

    @property
    def route_policy(self) -> RouteAuthorizationPolicy:
        snapshot = self._require_active()
        return RouteAuthorizationPolicy(
            required_roles=frozenset(snapshot.roles),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose(snapshot.purpose),
            operation=PersistenceOperation(snapshot.operation),
            service_principal_id=snapshot.service_principal_id,
        )

    @property
    def diagnostic_context(self) -> DiagnosticContext:
        snapshot = self._require_active()
        return DiagnosticContext(
            trace_id=snapshot.trace_id,
            correlation_id=snapshot.correlation_id,
            causation_id=snapshot.causation_id,
        )


@dataclass(frozen=True, slots=True)
class AuthorizedRequest:
    """The exact request and persistence snapshots released after allow audit."""

    request_context: AuthorizedRequestContext[DiagnosticContext]
    persistence_context: TrustedPersistenceContext
    authorization_audit_event_id: UUID


class _RequestPhase(Enum):
    AUDITING = auto()
    ACTIVE = auto()


@dataclass(slots=True)
class _ActiveRequest:
    task: asyncio.Task[object]
    request_context: _IssuedAuthorizedRequestContext
    expected_snapshot: _RequestSnapshot
    persistence_context: TrustedPersistenceContext
    phase: _RequestPhase


class TrustedRequestContextBoundary(TrustedContextVerifier):
    """Verify interactive contexts against request task and lifecycle state."""

    __slots__ = (
        "_active",
        "_issuer_id",
        "_persistence_boundary",
        "_requests",
        "_secret",
    )

    def __init__(
        self,
        *,
        persistence_boundary: TrustedContextBoundary,
        issuer_id: UUID,
        secret: bytes,
    ) -> None:
        if type(persistence_boundary) is not TrustedContextBoundary:
            raise TypeError("persistence_boundary must use the canonical boundary")
        if not isinstance(issuer_id, UUID) or issuer_id.int == 0:
            raise ValueError("issuer_id must be a non-nil UUID")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("secret must contain at least 32 bytes")
        self._persistence_boundary = persistence_boundary
        self._issuer_id = UUID(int=issuer_id.int)
        self._secret = bytes(secret)
        self._active: dict[int, _ActiveRequest] = {}
        self._requests: dict[int, _ActiveRequest] = {}

    @classmethod
    def create(
        cls,
        *,
        persistence_boundary: TrustedContextBoundary,
    ) -> TrustedRequestContextBoundary:
        return cls(
            persistence_boundary=persistence_boundary,
            issuer_id=uuid4(),
            secret=secrets.token_bytes(32),
        )

    @classmethod
    def for_testing(
        cls,
        *,
        persistence_boundary: TrustedContextBoundary,
        issuer_id: UUID,
        secret: bytes,
    ) -> TrustedRequestContextBoundary:
        return cls(
            persistence_boundary=persistence_boundary,
            issuer_id=issuer_id,
            secret=secret,
        )

    def verify(self, context: TrustedPersistenceContext) -> TrustedPersistenceContext:
        try:
            canonical = self._persistence_boundary.verify(context)
            if canonical.origin is ContextOrigin.WORKER:
                return canonical
            if canonical.origin is not ContextOrigin.INTERACTIVE:
                raise _invalid_context()
            active = self._active.get(id(context))
            if active is None or context is not active.persistence_context:
                raise _invalid_context()
            if _current_task() is not active.task:
                raise _invalid_context()
            if active.phase not in {_RequestPhase.AUDITING, _RequestPhase.ACTIVE}:
                raise _invalid_context()
            if canonical != active.persistence_context:
                raise _invalid_context()
            self._verify_request_context(active)
            return canonical
        except InvalidPersistenceContextError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise _invalid_context() from None

    def _issue(
        self,
        *,
        snapshot: _RequestSnapshot,
    ) -> tuple[_IssuedAuthorizedRequestContext, TrustedPersistenceContext]:
        task = _current_task()
        request_context = _IssuedAuthorizedRequestContext(
            snapshot=snapshot,
            provenance=_RequestProvenance(
                issuer_id=self._issuer_id,
                signature=self._signature(snapshot),
            ),
            guard=self._guard_request,
        )
        persistence_context = self._persistence_boundary.interactive(
            scope=EnvironmentScope(
                workspace_id=snapshot.workspace_id,
                environment_id=snapshot.environment_id,
            ),
            acting_subject_id=snapshot.acting_subject_id,
            purpose=PersistencePurpose(snapshot.purpose),
            operation=PersistenceOperation(snapshot.operation),
            trace_id=snapshot.trace_id,
            service_principal_id=snapshot.service_principal_id,
        )
        if id(persistence_context) in self._active:
            raise RuntimeError("Persistence context identity collision.")
        active = _ActiveRequest(
            task=task,
            request_context=request_context,
            expected_snapshot=snapshot,
            persistence_context=persistence_context,
            phase=_RequestPhase.AUDITING,
        )
        self._active[id(persistence_context)] = active
        self._requests[id(request_context)] = active
        return request_context, persistence_context

    def _activate(self, context: TrustedPersistenceContext) -> None:
        self._require_owned(context).phase = _RequestPhase.ACTIVE

    def _close(self, context: TrustedPersistenceContext) -> None:
        active = self._require_owned(context)
        del self._active[id(context)]
        del self._requests[id(active.request_context)]

    def _discard(self, context: TrustedPersistenceContext) -> None:
        active = self._active.get(id(context))
        if active is not None and active.persistence_context is context:
            del self._active[id(context)]
            self._requests.pop(id(active.request_context), None)

    def _guard_request(self, context: _IssuedAuthorizedRequestContext) -> None:
        active = self._requests.get(id(context))
        if (
            active is None
            or context is not active.request_context
            or active.phase is not _RequestPhase.ACTIVE
            or _current_task() is not active.task
        ):
            raise _invalid_context()
        self._verify_request_context(active)

    def _require_owned(self, context: TrustedPersistenceContext) -> _ActiveRequest:
        active = self._active.get(id(context))
        if (
            active is None
            or context is not active.persistence_context
            or _current_task() is not active.task
        ):
            raise _invalid_context()
        return active

    def _verify_request_context(self, active: _ActiveRequest) -> None:
        context = active.request_context
        if type(context) is not _IssuedAuthorizedRequestContext:
            raise _invalid_context()
        snapshot = context._snapshot
        provenance = context._provenance
        if (
            type(snapshot) is not _RequestSnapshot
            or type(provenance) is not _RequestProvenance
            or provenance.issuer_id != self._issuer_id
            or snapshot != active.expected_snapshot
            or not hmac.compare_digest(
                provenance.signature,
                self._signature(snapshot),
            )
        ):
            raise _invalid_context()

    def _signature(self, snapshot: _RequestSnapshot) -> bytes:
        return hmac.digest(self._secret, _canonical_request(snapshot), "sha256")


def _current_task() -> asyncio.Task[object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        raise _invalid_context() from None
    if task is None:
        raise _invalid_context()
    return task  # type: ignore[return-value]


def _canonical_request(snapshot: _RequestSnapshot) -> bytes:
    values = (
        "spine-authorized-request-v1",
        snapshot.request_id.hex,
        snapshot.acting_subject_id.hex,
        snapshot.workspace_id.hex,
        snapshot.environment_id.hex,
        ",".join(sorted(role.value for role in snapshot.roles)),
        str(snapshot.authorization_generation),
        snapshot.authentication_configuration_version,
        snapshot.purpose,
        snapshot.operation,
        (
            snapshot.service_principal_id.hex
            if snapshot.service_principal_id is not None
            else "-"
        ),
        snapshot.trace_id.hex,
        snapshot.correlation_id.hex if snapshot.correlation_id is not None else "-",
        snapshot.causation_id.hex if snapshot.causation_id is not None else "-",
    )
    return "\x1f".join(values).encode("ascii")


__all__ = ["AuthorizedRequest", "TrustedRequestContextBoundary"]
