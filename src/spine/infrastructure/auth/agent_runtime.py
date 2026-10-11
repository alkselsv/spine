"""Issue #72 bridge for the local agent execution admission port."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spine.agents.catalog.registry import HandlerRegistry
from spine.agents.runtime.contracts import AgentExecutionContext
from spine.agents.runtime.context import (
    AgentExecutionContextAdmission,
    AgentExecutionContextFactory,
)
from spine.agents.runtime.protocol import AgentHandler
from spine.agents.runtime.ports import (
    CanonicalTrustedAuthorizationContextVerifier,
    ExecutionScopeValidator,
)
from spine.application.diagnostics import DiagnosticContext
from spine.auth.contracts import (
    AuthorizedRequestContext,
    AuthorizationResolution,
    RouteAuthorizationPolicy,
)
from spine.infrastructure.auth.trusted_context import (
    AuthorizedRequest,
    TrustedRequestContextBoundary,
)

if TYPE_CHECKING:
    from spine.agents.catalog.resolver import ResolvedAgentBinding


class RequestAgentAuthorizationVerifier(CanonicalTrustedAuthorizationContextVerifier):
    """Revalidate one exact active #72 request before agent execution.

    The bridge retains neither credentials nor a serialized proof. It holds the
    request-local #72 objects and delegates lifecycle/provenance validation to
    ``TrustedRequestContextBoundary`` on every call.
    """

    __slots__ = ("_authorized", "_boundary")

    def __init__(
        self,
        *,
        boundary: TrustedRequestContextBoundary,
        authorized: AuthorizedRequest,
    ) -> None:
        if type(boundary) is not TrustedRequestContextBoundary:
            raise TypeError("request boundary must use the canonical implementation")
        if type(authorized) is not AuthorizedRequest:
            raise TypeError("authorized request must use the canonical contract")
        if not isinstance(authorized.request_context, AuthorizedRequestContext):
            raise TypeError("authorized request context is invalid")
        self._boundary = boundary
        self._authorized = authorized

    def verify(
        self,
        context: AuthorizedRequestContext[DiagnosticContext],
        resolution: AuthorizationResolution,
    ) -> None:
        """Raise unless the exact #72 request is active and consistent."""

        try:
            if context is not self._authorized.request_context:
                raise ValueError
            self._boundary.verify(self._authorized.persistence_context)
            request_context = self._authorized.request_context
            if (
                request_context.acting_subject_id != resolution.acting_subject_id
                or request_context.scope != resolution.scope
                or request_context.roles != resolution.roles
                or request_context.authorization_generation
                != resolution.authorization_generation
                or request_context.authentication_configuration_version
                != resolution.authentication_configuration_version
            ):
                raise ValueError
        except Exception:
            raise ValueError("authorization context is invalid") from None


class TrustedAgentExecutionRuntime:
    """Atomic #72-backed composition of one factory and one registry gate."""

    __slots__ = ("_factory", "_registry")

    def __init__(
        self,
        *,
        boundary: TrustedRequestContextBoundary,
        authorized: AuthorizedRequest,
        scope_validator: ExecutionScopeValidator,
        expected_route_policy: RouteAuthorizationPolicy,
        registry: HandlerRegistry,
    ) -> None:
        if type(boundary) is not TrustedRequestContextBoundary:
            raise TypeError("request boundary must use the canonical implementation")
        if type(authorized) is not AuthorizedRequest:
            raise TypeError("authorized request must use the canonical contract")
        if type(registry) is not HandlerRegistry:
            raise TypeError("handler registry must use the canonical implementation")
        self._factory = _create_factory(
            boundary=boundary,
            authorized=authorized,
            scope_validator=scope_validator,
            expected_route_policy=expected_route_policy,
        )
        self._registry = registry

    @property
    def factory(self) -> AgentExecutionContextFactory:
        """Return the plain-data factory paired with this protected gate."""

        return self._factory

    def bind_for_execution(
        self,
        *,
        handler: AgentHandler[Any, Any],
        context: AgentExecutionContext,
        admission: AgentExecutionContextAdmission,
        resolved_binding: ResolvedAgentBinding,
    ) -> AgentHandler[Any, Any]:
        """Bind one exact registry selection to the live #72 request."""

        return self._registry._bind_composed_execution(
            handler=handler,
            context=context,
            admission=admission,
            resolved_binding=resolved_binding,
            trusted_factory=self._factory,
        )


def _create_factory(
    *,
    boundary: TrustedRequestContextBoundary,
    authorized: AuthorizedRequest,
    scope_validator: ExecutionScopeValidator,
    expected_route_policy: RouteAuthorizationPolicy,
) -> AgentExecutionContextFactory:
    verifier = RequestAgentAuthorizationVerifier(
        boundary=boundary,
        authorized=authorized,
    )
    return AgentExecutionContextFactory(
        authorization_verifier=verifier,
        scope_validator=scope_validator,
        expected_route_policy=expected_route_policy,
    )


def compose_agent_execution_context_factory(
    *,
    boundary: TrustedRequestContextBoundary,
    authorized: AuthorizedRequest,
    scope_validator: ExecutionScopeValidator,
    expected_route_policy: RouteAuthorizationPolicy,
) -> AgentExecutionContextFactory:
    """Create a #72-backed plain-data factory for one request session.

    Use ``compose_agent_execution_runtime`` when the factory must be paired
    with a protected registry execution gate.
    """

    return _create_factory(
        boundary=boundary,
        authorized=authorized,
        scope_validator=scope_validator,
        expected_route_policy=expected_route_policy,
    )


def compose_agent_execution_runtime(
    *,
    boundary: TrustedRequestContextBoundary,
    authorized: AuthorizedRequest,
    scope_validator: ExecutionScopeValidator,
    expected_route_policy: RouteAuthorizationPolicy,
    registry: HandlerRegistry,
) -> TrustedAgentExecutionRuntime:
    """Create the only supported production factory/registry pairing."""

    return TrustedAgentExecutionRuntime(
        boundary=boundary,
        authorized=authorized,
        scope_validator=scope_validator,
        expected_route_policy=expected_route_policy,
        registry=registry,
    )


__all__ = [
    "RequestAgentAuthorizationVerifier",
    "TrustedAgentExecutionRuntime",
    "compose_agent_execution_context_factory",
    "compose_agent_execution_runtime",
]
