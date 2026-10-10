"""Trusted request authorization, audit, and persistence composition.

This module is an infrastructure composition seam, not an HTTP adapter.  A
composition root binds one registered route policy and gives only that bound
authorizer to request extraction code.  The trusted issuer/verifier stays in
the composition root and is also installed as the persistence context verifier.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Generic, Protocol, TypeVar
from uuid import UUID, uuid4

from pydantic import ValidationError

from spine.application.diagnostics.audit import (
    AuditEvent,
    AuditEventRegistry,
    AuditOutcome,
    RequestAccessDecisionAuditPayload,
)
from spine.application.diagnostics.context import DiagnosticContext
from spine.application.diagnostics.events import (
    DiagnosticEventRegistry,
    DiagnosticFailureCode,
    DiagnosticSeverity,
    DiagnosticSink,
    FailureDiagnosticPayload,
    emit_diagnostic_safely,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth.contracts import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationResolution,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
    _snapshot_route_authorization_policy,
)
from spine.auth.errors import (
    AuthError,
    AuthFailureCategory,
    AuthenticationUnavailableError,
    AuthorizationDeniedError,
    AuthorizationUnavailableError,
    InvalidAuthenticationError,
)
from spine.auth.ports import AuthenticationPort, AuthorizationDirectory
from spine.auth.registry import RoutePolicyRegistry
from spine.domain.common import ContextOrigin
from spine.infrastructure.auth.trusted_context import (
    AuthorizedRequest,
    TrustedRequestContextBoundary,
    _RequestSnapshot,
)


_ResultT = TypeVar("_ResultT")


class RequiredAuditGate(Protocol):
    """Small seam implemented by the existing RequiredAuditCoordinator."""

    async def release_after_audit(
        self,
        *,
        context: TrustedPersistenceContext,
        event: AuditEvent,
        release: Callable[[UUID], Awaitable[_ResultT]],
    ) -> _ResultT: ...


class _UnexpectedRequestAuthorizationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Request authorization failed.")


class RequestAuthorizationComposer:
    """Bind registered server policy to trusted request authorization."""

    def __init__(
        self,
        *,
        authentication: AuthenticationPort[DiagnosticContext],
        authorization_directory: AuthorizationDirectory,
        route_policies: RoutePolicyRegistry,
        request_boundary: TrustedRequestContextBoundary,
        audit_registry: AuditEventRegistry,
        audit_gate: RequiredAuditGate,
        diagnostic_registry: DiagnosticEventRegistry,
        diagnostic_sink: DiagnosticSink,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        request_id_source: Callable[[], UUID] = uuid4,
    ) -> None:
        audit_registry.require_sealed()
        if type(request_boundary) is not TrustedRequestContextBoundary:
            raise TypeError("request_boundary must use the canonical boundary")
        self._authentication = authentication
        self._authorization_directory = authorization_directory
        self._route_policies = route_policies
        self._request_boundary = request_boundary
        self._audit_registry = audit_registry
        self._audit_gate = audit_gate
        self._diagnostic_registry = diagnostic_registry
        self._diagnostic_sink = diagnostic_sink
        self._clock = clock
        self._request_id_source = request_id_source

    def bind_route(self, route_id: str) -> BoundRouteAuthorization:
        """Capture one registered policy before caller-controlled data arrives."""

        return BoundRouteAuthorization(
            composer=self,
            policy=self._route_policies.require(route_id),
        )


class BoundRouteAuthorization:
    """One server-bound protected route; request data cannot replace policy."""

    __slots__ = ("_composer", "_policy")

    def __init__(
        self,
        *,
        composer: RequestAuthorizationComposer,
        policy: RouteAuthorizationPolicy,
    ) -> None:
        self._composer = composer
        self._policy = _snapshot_route_authorization_policy(policy)

    def authorize(
        self,
        *,
        credentials: AccessTokenCredential,
        requested_scope: RequestedAuthorizationScope,
        diagnostic_context: DiagnosticContext,
    ) -> _AuthorizationSession:
        return _AuthorizationSession(
            route=self,
            credentials=credentials,
            requested_scope=requested_scope,
            diagnostic_context=diagnostic_context,
        )


class _AuthorizationSession:
    __slots__ = (
        "_authorized",
        "_credentials",
        "_diagnostic_context",
        "_entered",
        "_requested_scope",
        "_route",
    )

    def __init__(
        self,
        *,
        route: BoundRouteAuthorization,
        credentials: AccessTokenCredential,
        requested_scope: RequestedAuthorizationScope,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        self._route = route
        self._credentials = credentials
        self._requested_scope = requested_scope
        self._diagnostic_context = diagnostic_context
        self._authorized: AuthorizedRequest | None = None
        self._entered = False

    async def __aenter__(self) -> AuthorizedRequest:
        if self._entered:
            raise RuntimeError("Authorization session cannot be reused.")
        self._entered = True
        composer = self._route._composer
        diagnostic_context = _snapshot_diagnostic_context(self._diagnostic_context)
        try:
            credentials = _snapshot_credentials(self._credentials)
            requested_scope = _snapshot_requested_scope(self._requested_scope)
            authenticated_alias = await composer._authentication.authenticate(
                credentials,
                diagnostic_context,
            )
            alias_snapshot = _snapshot_authenticated_alias(authenticated_alias)
        except asyncio.CancelledError:
            raise
        except AuthError as failure:
            await _emit_auth_failure(composer, diagnostic_context, failure)
            raise
        except Exception:
            await _emit_failure_code(
                composer,
                diagnostic_context,
                DiagnosticFailureCode.INTERNAL_UNEXPECTED,
            )
            raise _UnexpectedRequestAuthorizationError() from None

        try:
            resolution = await composer._authorization_directory.resolve_request_authority(
                alias_snapshot,
                requested_scope,
                self._route._policy,
            )
            resolution_snapshot = _snapshot_resolution(resolution)
        except asyncio.CancelledError:
            raise
        except AuthError as failure:
            await _emit_auth_failure(composer, diagnostic_context, failure)
            raise
        except Exception:
            await _emit_failure_code(
                composer,
                diagnostic_context,
                DiagnosticFailureCode.INTERNAL_UNEXPECTED,
            )
            raise _UnexpectedRequestAuthorizationError() from None

        if not _resolution_matches(
            resolution_snapshot,
            alias_snapshot,
            requested_scope,
            self._route._policy,
        ):
            if (
                resolution_snapshot.scope == requested_scope
                and resolution_snapshot.roles == self._route._policy.required_roles
            ):
                await self._reject_inconsistent_resolution(
                    alias_snapshot=alias_snapshot,
                    resolution=resolution_snapshot,
                    diagnostic_context=diagnostic_context,
                )
            failure = AuthorizationDeniedError()
            await _emit_auth_failure(composer, diagnostic_context, failure)
            raise failure

        snapshot = _request_snapshot(
            composer=composer,
            resolution=resolution_snapshot,
            policy=self._route._policy,
            diagnostic_context=diagnostic_context,
        )
        request_context, persistence_context = composer._request_boundary._issue(
            snapshot=snapshot,
        )
        event = _build_access_event(
            composer=composer,
            snapshot=snapshot,
            outcome=AuditOutcome.ALLOWED,
            reason="request_authorization_allowed",
        )

        async def release(audit_event_id: UUID) -> AuthorizedRequest:
            composer._request_boundary._activate(persistence_context)
            return AuthorizedRequest(
                request_context=request_context,
                persistence_context=persistence_context,
                authorization_audit_event_id=audit_event_id,
            )

        try:
            released = await composer._audit_gate.release_after_audit(
                context=persistence_context,
                event=event,
                release=release,
            )
            if type(released) is not AuthorizedRequest:
                raise RuntimeError("Required audit gate returned an invalid release.")
            self._authorized = released
            return released
        except BaseException:
            composer._request_boundary._discard(persistence_context)
            raise

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._authorized is not None:
            self._route._composer._request_boundary._close(
                self._authorized.persistence_context
            )
            self._authorized = None

    async def _reject_inconsistent_resolution(
        self,
        *,
        alias_snapshot: AuthenticatedAlias,
        resolution: AuthorizationResolution,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        composer = self._route._composer
        snapshot = _request_snapshot(
            composer=composer,
            resolution=resolution,
            policy=self._route._policy,
            diagnostic_context=diagnostic_context,
            configuration_version=alias_snapshot.authentication_configuration_version,
        )
        _, persistence_context = composer._request_boundary._issue(snapshot=snapshot)
        event = _build_access_event(
            composer=composer,
            snapshot=snapshot,
            outcome=AuditOutcome.DENIED,
            reason="request_authorization_snapshot_mismatch",
        )

        async def never_release(audit_event_id: UUID) -> None:
            del audit_event_id
            raise AssertionError("Denied authorization must not be released.")

        try:
            await composer._audit_gate.release_after_audit(
                context=persistence_context,
                event=event,
                release=never_release,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            composer._request_boundary._discard(persistence_context)
        raise AuthorizationUnavailableError()


def _snapshot_credentials(value: AccessTokenCredential) -> AccessTokenCredential:
    if type(value) is not AccessTokenCredential:
        raise InvalidAuthenticationError()
    return AccessTokenCredential(access_token=value.access_token.get_secret_value())


def _snapshot_diagnostic_context(value: DiagnosticContext) -> DiagnosticContext:
    try:
        if type(value) is not DiagnosticContext:
            raise TypeError
        return DiagnosticContext(
            trace_id=value.trace_id,
            correlation_id=value.correlation_id,
            causation_id=value.causation_id,
        )
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise InvalidAuthenticationError() from None


def _snapshot_requested_scope(
    value: RequestedAuthorizationScope,
) -> RequestedAuthorizationScope:
    try:
        if type(value) is not RequestedAuthorizationScope:
            raise TypeError
        return RequestedAuthorizationScope(
            workspace_id=value.workspace_id,
            environment_id=value.environment_id,
        )
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise AuthorizationDeniedError() from None


def _snapshot_authenticated_alias(value: AuthenticatedAlias) -> AuthenticatedAlias:
    try:
        if type(value) is not AuthenticatedAlias or type(value.alias) is not AuthenticationAlias:
            raise TypeError
        return AuthenticatedAlias(
            alias=AuthenticationAlias(
                issuer=value.alias.issuer,
                subject=value.alias.subject,
            ),
            authentication_configuration_version=value.authentication_configuration_version,
        )
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise InvalidAuthenticationError() from None


def _snapshot_resolution(value: AuthorizationResolution) -> AuthorizationResolution:
    try:
        if type(value) is not AuthorizationResolution:
            raise TypeError
        return AuthorizationResolution(
            acting_subject_id=value.acting_subject_id,
            scope=_snapshot_requested_scope(value.scope),
            roles=frozenset(value.roles),
            authorization_generation=value.authorization_generation,
            authentication_configuration_version=(
                value.authentication_configuration_version
            ),
        )
    except AuthorizationDeniedError:
        raise
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise AuthorizationUnavailableError() from None


def _resolution_matches(
    resolution: AuthorizationResolution,
    authenticated_alias: AuthenticatedAlias,
    requested_scope: RequestedAuthorizationScope,
    policy: RouteAuthorizationPolicy,
) -> bool:
    return (
        resolution.scope == requested_scope
        and resolution.roles == policy.required_roles
        and resolution.authentication_configuration_version
        == authenticated_alias.authentication_configuration_version
    )


def _request_snapshot(
    *,
    composer: RequestAuthorizationComposer,
    resolution: AuthorizationResolution,
    policy: RouteAuthorizationPolicy,
    diagnostic_context: DiagnosticContext,
    configuration_version: str | None = None,
) -> _RequestSnapshot:
    request_id = composer._request_id_source()
    if not isinstance(request_id, UUID) or request_id.int == 0:
        raise RuntimeError("Request identifier source returned an invalid value.")
    return _RequestSnapshot(
        request_id=UUID(int=request_id.int),
        acting_subject_id=UUID(int=resolution.acting_subject_id.int),
        workspace_id=UUID(int=resolution.scope.workspace_id.int),
        environment_id=UUID(int=resolution.scope.environment_id.int),
        roles=frozenset(resolution.roles),
        authorization_generation=resolution.authorization_generation,
        authentication_configuration_version=(
            configuration_version
            if configuration_version is not None
            else resolution.authentication_configuration_version
        ),
        purpose=policy.purpose.value,
        operation=policy.operation.value,
        service_principal_id=(
            UUID(int=policy.service_principal_id.int)
            if policy.service_principal_id is not None
            else None
        ),
        trace_id=UUID(int=diagnostic_context.trace_id.int),
        correlation_id=(
            UUID(int=diagnostic_context.correlation_id.int)
            if diagnostic_context.correlation_id is not None
            else None
        ),
        causation_id=(
            UUID(int=diagnostic_context.causation_id.int)
            if diagnostic_context.causation_id is not None
            else None
        ),
    )


def _build_access_event(
    *,
    composer: RequestAuthorizationComposer,
    snapshot: _RequestSnapshot,
    outcome: AuditOutcome,
    reason: str,
) -> AuditEvent:
    return composer._audit_registry.build_event(
        workspace_id=snapshot.workspace_id,
        environment_id=snapshot.environment_id,
        event_type="access.decision",
        schema_version=2,
        payload=RequestAccessDecisionAuditPayload(
            purpose=snapshot.purpose,
            operation=snapshot.operation,
            authorization_generation=snapshot.authorization_generation,
            authentication_configuration_version=(
                snapshot.authentication_configuration_version
            ),
        ),
        origin=ContextOrigin.INTERACTIVE,
        acting_subject_id=snapshot.acting_subject_id,
        service_principal_id=snapshot.service_principal_id,
        trace_id=snapshot.trace_id,
        correlation_id=snapshot.correlation_id,
        causation_id=snapshot.causation_id,
        occurred_at=_safe_clock_value(composer._clock),
        outcome=outcome,
        reason=reason,
        producer_deduplication_id=f"request_authorization.{snapshot.request_id.hex}",
    )


async def _emit_auth_failure(
    composer: RequestAuthorizationComposer,
    diagnostic_context: DiagnosticContext,
    failure: AuthError,
) -> None:
    await _emit_failure_code(
        composer,
        diagnostic_context,
        _diagnostic_code(failure),
    )


async def _emit_failure_code(
    composer: RequestAuthorizationComposer,
    diagnostic_context: DiagnosticContext,
    code: DiagnosticFailureCode,
) -> None:
    severity = (
        DiagnosticSeverity.WARNING
        if code
        in {
            DiagnosticFailureCode.AUTHENTICATION_INVALID,
            DiagnosticFailureCode.AUTHORIZATION_DENIED,
        }
        else DiagnosticSeverity.ERROR
    )
    event = composer._diagnostic_registry.build_event(
        event_type="failure.observed",
        schema_version=1,
        severity=severity,
        occurred_at=_safe_clock_value(composer._clock),
        diagnostic_context=diagnostic_context,
        payload=FailureDiagnosticPayload(failure_code=code),
    )
    await emit_diagnostic_safely(composer._diagnostic_sink, event)


def _diagnostic_code(failure: AuthError) -> DiagnosticFailureCode:
    mapping = {
        AuthFailureCategory.AUTHENTICATION_INVALID: (
            DiagnosticFailureCode.AUTHENTICATION_INVALID
        ),
        AuthFailureCategory.AUTHENTICATION_UNAVAILABLE: (
            DiagnosticFailureCode.AUTHENTICATION_UNAVAILABLE
        ),
        AuthFailureCategory.AUTHORIZATION_DENIED: (
            DiagnosticFailureCode.AUTHORIZATION_DENIED
        ),
        AuthFailureCategory.AUTHORIZATION_UNAVAILABLE: (
            DiagnosticFailureCode.AUTHORIZATION_UNAVAILABLE
        ),
    }
    try:
        return mapping[failure.category]
    except (AttributeError, KeyError):
        return DiagnosticFailureCode.INTERNAL_UNEXPECTED


def _safe_clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("Authorization clock must return an aware datetime.")
    return value


__all__ = [
    "AuthorizedRequest",
    "BoundRouteAuthorization",
    "RequestAuthorizationComposer",
    "TrustedRequestContextBoundary",
]
