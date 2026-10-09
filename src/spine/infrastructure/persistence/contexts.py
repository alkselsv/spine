"""Composition-root-owned trusted context issuance and verification."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import replace
from uuid import UUID, uuid4

from spine.application.persistence.bootstrap import InitialWorkspaceBootstrapAuthority
from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    PersistenceScope,
    TrustedContextProvenance,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import InvalidPersistenceContextError


_SIGNATURE_SIZE = hashlib.sha256().digest_size


def _invalid_context() -> InvalidPersistenceContextError:
    return InvalidPersistenceContextError("Persistence context is invalid.")


class _InitialWorkspaceBootstrapAuthority:
    @property
    def bootstrap_authority_marker(self) -> None:
        return None


def create_initial_workspace_bootstrap_authority() -> InitialWorkspaceBootstrapAuthority:
    """Create a one-adapter bootstrap capability at a trusted composition root."""

    return _InitialWorkspaceBootstrapAuthority()


class TrustedContextBoundary:
    """Issue and verify contexts bound to one server-side authority boundary.

    The boundary belongs in a trusted composition root and must not be exposed to
    request parsing or caller-controlled dependency injection. Its proof detects
    constructed or mutated context data; it is not a sandbox against arbitrary
    code already executing with application-process privileges.
    """

    __slots__ = ("_issuer_id", "_secret")

    def __init__(self, *, issuer_id: UUID, secret: bytes) -> None:
        if not isinstance(issuer_id, UUID) or issuer_id.int == 0:
            raise ValueError("issuer_id must be a non-nil UUID")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("secret must contain at least 32 bytes")
        self._issuer_id = issuer_id
        self._secret = secret

    @classmethod
    def create(cls) -> "TrustedContextBoundary":
        """Create a process-local boundary from a trusted composition root."""

        return cls(issuer_id=uuid4(), secret=secrets.token_bytes(32))

    @classmethod
    def for_testing(cls, *, issuer_id: UUID, secret: bytes) -> "TrustedContextBoundary":
        """Create a deterministic boundary for tests only."""

        return cls(issuer_id=issuer_id, secret=secret)

    def interactive(
        self,
        *,
        scope: PersistenceScope,
        acting_subject_id: UUID | None,
        purpose: PersistencePurpose,
        operation: PersistenceOperation,
        trace_id: UUID,
        service_principal_id: UUID | None = None,
    ) -> TrustedPersistenceContext:
        return self._issue(
            scope=scope,
            origin=ContextOrigin.INTERACTIVE,
            acting_subject_id=acting_subject_id,
            service_principal_id=service_principal_id,
            purpose=purpose,
            operation=operation,
            trace_id=trace_id,
        )

    def worker(
        self,
        *,
        scope: PersistenceScope,
        service_principal_id: UUID | None,
        purpose: PersistencePurpose,
        operation: PersistenceOperation,
        trace_id: UUID,
    ) -> TrustedPersistenceContext:
        return self._issue(
            scope=scope,
            origin=ContextOrigin.WORKER,
            acting_subject_id=None,
            service_principal_id=service_principal_id,
            purpose=purpose,
            operation=operation,
            trace_id=trace_id,
        )

    def verify(self, context: TrustedPersistenceContext) -> TrustedPersistenceContext:
        try:
            snapshot = _capture_context(context)
            if snapshot.provenance.issuer_id != self._issuer_id:
                raise _invalid_context()
            expected = self._signature(snapshot)
            if not hmac.compare_digest(snapshot.provenance.signature, expected):
                raise _invalid_context()
            return snapshot
        except InvalidPersistenceContextError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise _invalid_context() from None

    def _issue(
        self,
        *,
        scope: PersistenceScope,
        origin: ContextOrigin,
        acting_subject_id: UUID | None,
        service_principal_id: UUID | None,
        purpose: PersistencePurpose,
        operation: PersistenceOperation,
        trace_id: UUID,
    ) -> TrustedPersistenceContext:
        unsigned = TrustedPersistenceContext(
            scope=scope,
            origin=origin,
            purpose=purpose,
            operation=operation,
            trace_id=trace_id,
            acting_subject_id=acting_subject_id,
            service_principal_id=service_principal_id,
            provenance=TrustedContextProvenance(
                issuer_id=self._issuer_id,
                signature=b"\x00" * _SIGNATURE_SIZE,
            ),
        )
        snapshot = _capture_context(unsigned)
        return replace(
            snapshot,
            provenance=TrustedContextProvenance(
                issuer_id=self._issuer_id,
                signature=self._signature(snapshot),
            ),
        )

    def _signature(self, context: TrustedPersistenceContext) -> bytes:
        return hmac.digest(self._secret, _canonical_context(context), "sha256")


def _canonical_context(context: TrustedPersistenceContext) -> bytes:
    scope = context.scope
    environment_id = scope.environment_id.hex if isinstance(scope, EnvironmentScope) else "-"
    values = (
        "spine-trusted-context-v1",
        context.provenance.issuer_id.hex,
        type(scope).__name__,
        scope.workspace_id.hex,
        environment_id,
        context.origin.value,
        context.acting_subject_id.hex if context.acting_subject_id is not None else "-",
        context.service_principal_id.hex if context.service_principal_id is not None else "-",
        context.purpose.value,
        context.operation.value,
        context.trace_id.hex,
    )
    return "\x1f".join(values).encode("ascii")


def _capture_uuid(value: object) -> UUID:
    if type(value) is not UUID:
        raise _invalid_context()
    integer = value.int
    if integer == 0:
        raise _invalid_context()
    return UUID(int=integer)


def _capture_optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return _capture_uuid(value)


def _capture_context(context: TrustedPersistenceContext) -> TrustedPersistenceContext:
    """Read untrusted fields once and return the exact data to authenticate.

    Exact-type checks reject polymorphic inputs as defense in depth. Security does
    not depend on those checks: validation, HMAC comparison, and the returned
    authority all consume this one detached snapshot, never the source object.
    """

    if type(context) is not TrustedPersistenceContext:
        raise _invalid_context()

    scope = context.scope
    origin = context.origin
    purpose = context.purpose
    operation = context.operation
    trace_id = context.trace_id
    acting_subject_id = context.acting_subject_id
    service_principal_id = context.service_principal_id
    provenance = context.provenance

    if type(scope) is EnvironmentScope:
        workspace_id = scope.workspace_id
        environment_id = scope.environment_id
        snapshot_scope: PersistenceScope = EnvironmentScope(
            workspace_id=_capture_uuid(workspace_id),
            environment_id=_capture_uuid(environment_id),
        )
    elif type(scope) is WorkspaceScope:
        workspace_id = scope.workspace_id
        snapshot_scope = WorkspaceScope(workspace_id=_capture_uuid(workspace_id))
    else:
        raise _invalid_context()

    if type(origin) is not ContextOrigin:
        raise _invalid_context()
    if type(purpose) is not PersistencePurpose:
        raise _invalid_context()
    purpose_value = purpose.value
    if type(operation) is not PersistenceOperation:
        raise _invalid_context()
    operation_value = operation.value
    if type(provenance) is not TrustedContextProvenance:
        raise _invalid_context()
    issuer_id = provenance.issuer_id
    signature = provenance.signature
    if type(signature) is not bytes:
        raise _invalid_context()

    snapshot = TrustedPersistenceContext(
        scope=snapshot_scope,
        origin=origin,
        purpose=PersistencePurpose(purpose_value),
        operation=PersistenceOperation(operation_value),
        trace_id=_capture_uuid(trace_id),
        acting_subject_id=_capture_optional_uuid(acting_subject_id),
        service_principal_id=_capture_optional_uuid(service_principal_id),
        provenance=TrustedContextProvenance(
            issuer_id=_capture_uuid(issuer_id),
            signature=bytes(signature),
        ),
    )
    snapshot.validate_shape()
    return snapshot
