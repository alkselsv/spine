"""FastAPI enforcement for trusted request authentication and authorization."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol, cast
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import Headers
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from spine.api.diagnostics import get_diagnostic_context
from spine.application.diagnostics import DiagnosticContext, StructuredError
from spine.auth import (
    AccessTokenCredential,
    AuthorizationDeniedError,
    AuthorizedRequestContext,
    InvalidAuthenticationError,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
    RoutePolicyRegistry,
    is_http_bearer_token,
)

if TYPE_CHECKING:
    from spine.application.persistence.context import TrustedPersistenceContext


WORKSPACE_ID_HEADER = "X-Workspace-ID"
ENVIRONMENT_ID_HEADER = "X-Environment-ID"
_AUTHORIZED_CONTEXT_STATE_KEY = "authorized_request_context"
_AUTHORIZED_PERSISTENCE_CONTEXT_STATE_KEY = "authorized_persistence_context"
_AUTHORIZATION_AUDIT_EVENT_ID_STATE_KEY = "authorization_audit_event_id"
_BEARER = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
_PROTECTED_OPENAPI = {
    "security": [{"BearerAuth": []}],
    "parameters": [
        {
            "name": WORKSPACE_ID_HEADER,
            "in": "header",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        },
        {
            "name": ENVIRONMENT_ID_HEADER,
            "in": "header",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        },
    ],
}
_PROTECTED_ERROR_RESPONSES = {
    400: {"model": StructuredError, "description": "Safe request error."},
    401: {"model": StructuredError, "description": "Authentication is invalid."},
    403: {"model": StructuredError, "description": "Authorization is denied."},
    404: {"model": StructuredError, "description": "Resource is unavailable."},
    409: {"model": StructuredError, "description": "Command conflict."},
    413: {"model": StructuredError, "description": "Request payload is too large."},
    422: {"model": StructuredError, "description": "Request validation failed."},
    500: {"model": StructuredError, "description": "Unexpected internal error."},
    503: {"model": StructuredError, "description": "Dependency is unavailable."},
}


class _AuthorizedRequestResult(Protocol):
    @property
    def request_context(self) -> AuthorizedRequestContext[DiagnosticContext]: ...


class _AuthorizationSession(Protocol):
    async def __aenter__(self) -> _AuthorizedRequestResult: ...

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None: ...


class _BoundRouteAuthorization(Protocol):
    def authorize_with_scope_resolver(
        self,
        *,
        credentials: AccessTokenCredential,
        requested_scope_resolver: Callable[[], RequestedAuthorizationScope],
        diagnostic_context: DiagnosticContext,
    ) -> _AuthorizationSession: ...


class RouteAuthorizationComposer(Protocol):
    """Minimal application-facing seam supplied by an auth composition root."""

    def bind_route(self, route_id: str) -> _BoundRouteAuthorization: ...


class ApiAuthorizationReadinessCode(str, Enum):
    AUTHORIZATION_READY = "authorization.ready"
    AUTHORIZATION_UNAVAILABLE = "authorization.unavailable"
    AUTHORIZATION_STORE_UNAVAILABLE = "authorization.store_unavailable"
    LOCAL_READY = "local.ready"
    OIDC_READY = "oidc.ready"
    OIDC_CHECK_DEFERRED = "oidc.check_deferred"
    OIDC_STALE_KEYS = "oidc.stale_keys"
    OIDC_PROVIDER_UNAVAILABLE = "oidc.provider_unavailable"
    OIDC_PROVIDER_INCOMPATIBLE = "oidc.provider_incompatible"
    OIDC_RUNTIME_CLOSED = "oidc.runtime_closed"


class ApiAuthorizationReadiness(BaseModel):
    """Disclosure-safe aggregate readiness for authentication and authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ready: bool
    code: ApiAuthorizationReadinessCode
    authentication_configuration_version: str | None = None
    authentication_mode: Literal["local", "oidc"] | None = None


ReadinessProbe = Callable[[], Awaitable[ApiAuthorizationReadiness]]
LifecycleHook = Callable[[], Awaitable[object]]


class ApiAuthorizationRuntime:
    """Register and bind static route policy before request data arrives."""

    __slots__ = (
        "_close_hook",
        "_composer",
        "_readiness_probe",
        "_route_policies",
        "_startup_hook",
    )

    def __init__(
        self,
        *,
        composer: RouteAuthorizationComposer,
        route_policies: RoutePolicyRegistry,
        readiness_probe: ReadinessProbe,
        startup_hook: LifecycleHook | None = None,
        close_hook: LifecycleHook | None = None,
    ) -> None:
        if type(route_policies) is not RoutePolicyRegistry:
            raise TypeError("route_policies must use the canonical registry")
        if not callable(getattr(composer, "bind_route", None)):
            raise TypeError("composer must implement the route authorization seam")
        if not callable(readiness_probe):
            raise TypeError("readiness_probe must be callable")
        if startup_hook is not None and not callable(startup_hook):
            raise TypeError("startup_hook must be callable")
        if close_hook is not None and not callable(close_hook):
            raise TypeError("close_hook must be callable")
        self._composer = composer
        self._route_policies = route_policies
        self._readiness_probe = readiness_probe
        self._startup_hook = startup_hook
        self._close_hook = close_hook

    def register_and_bind(
        self,
        route_id: str,
        policy: RouteAuthorizationPolicy,
    ) -> _BoundRouteAuthorization:
        self._route_policies.register(route_id, policy)
        return self._composer.bind_route(route_id)

    def require_policy(self, route_id: str) -> RouteAuthorizationPolicy:
        return self._route_policies.require(route_id)

    async def is_ready(self) -> bool:
        """Return a safe aggregate authentication readiness signal."""

        return (await self.check_readiness()).ready

    async def check_readiness(self) -> ApiAuthorizationReadiness:
        """Return a safe reason code without exposing provider or store detail."""

        try:
            result = await self._readiness_probe()
            if type(result) is not ApiAuthorizationReadiness:
                raise TypeError("readiness probe returned a non-canonical result")
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        return ApiAuthorizationReadiness(
            ready=False,
            code=ApiAuthorizationReadinessCode.AUTHORIZATION_UNAVAILABLE,
        )

    async def startup(self) -> None:
        """Start process-owned authentication resources, when configured."""

        if self._startup_hook is not None:
            await self._startup_hook()

    async def aclose(self) -> None:
        """Close process-owned authentication resources, when configured."""

        if self._close_hook is not None:
            await self._close_hook()


class RouteAuthorizationConfigurationError(RuntimeError):
    """The assembled `/api/v1` surface is not statically classified."""


class ApiRouteKind(str, Enum):
    PUBLIC = "public"
    PROTECTED = "protected"


@dataclass(frozen=True, slots=True)
class ApiRouteClassification:
    kind: ApiRouteKind
    policy_id: str | None = None


class ApiRouteClassificationRegistry:
    """Classify exact route objects without inheritable names or attributes."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[
            int,
            tuple[APIRoute, ApiRouteClassification],
        ] = {}

    def register_public(self, route: APIRoute) -> None:
        self._register(route, ApiRouteClassification(ApiRouteKind.PUBLIC))

    def register_protected(self, route: APIRoute, policy_id: str) -> None:
        self._register(
            route,
            ApiRouteClassification(ApiRouteKind.PROTECTED, policy_id),
        )

    def classification(self, route: APIRoute) -> ApiRouteClassification | None:
        entry = self._entries.get(id(route))
        if entry is None or entry[0] is not route:
            return None
        return entry[1]

    def _register(
        self,
        route: APIRoute,
        classification: ApiRouteClassification,
    ) -> None:
        if self.classification(route) is not None:
            raise RouteAuthorizationConfigurationError(
                "API route is already classified."
            )
        self._entries[id(route)] = (route, classification)


class _ProtectedDependencyOverrideProvider:
    """Pass ordinary test overrides through while sealing trusted context lookup."""

    __slots__ = ("_provider",)

    def __init__(self, provider: object) -> None:
        self._provider = provider

    @property
    def dependency_overrides(self) -> dict[Callable[..., Any], Callable[..., Any]]:
        configured = getattr(self._provider, "dependency_overrides", {})
        return {
            dependency: override
            for dependency, override in configured.items()
            if dependency is not get_authorized_request_context
        }


async def get_authorized_request_context(
    request: Request,
) -> AuthorizedRequestContext[DiagnosticContext]:
    """Return only the trusted context issued by the protected route wrapper."""

    context = getattr(request.state, _AUTHORIZED_CONTEXT_STATE_KEY, None)
    if not isinstance(context, AuthorizedRequestContext):
        raise RuntimeError("Trusted authorized request context is unavailable.")
    return cast(AuthorizedRequestContext[DiagnosticContext], context)


async def get_authorization_audit_event_id(request: Request) -> UUID:
    """Return the immutable allow-audit reference for this authorized request."""

    audit_event_id = getattr(request.state, _AUTHORIZATION_AUDIT_EVENT_ID_STATE_KEY, None)
    if not isinstance(audit_event_id, UUID):
        raise RuntimeError("Trusted authorization audit reference is unavailable.")
    return UUID(int=audit_event_id.int)


async def get_authorized_persistence_context(request: Request) -> TrustedPersistenceContext:
    """Return the trusted persistence context released for a protected route."""

    context = getattr(request.state, _AUTHORIZED_PERSISTENCE_CONTEXT_STATE_KEY, None)
    if context is None:
        raise RuntimeError("Trusted authorized persistence context is unavailable.")
    return cast("TrustedPersistenceContext", context)


def add_protected_api_route(
    application: FastAPI,
    path: str,
    endpoint: Callable[..., Any],
    *,
    route_id: str,
    policy: RouteAuthorizationPolicy,
    methods: Iterable[str],
    **route_options: Any,
) -> None:
    """Add one `/api/v1` route with a statically bound authorization policy."""

    if not (path == "/api/v1" or path.startswith("/api/v1/")):
        raise ValueError("Protected API routes must use the /api/v1 boundary.")
    runtime = getattr(application.state, "authorization_runtime", None)
    if type(runtime) is not ApiAuthorizationRuntime:
        raise RuntimeError("Protected routes require an authorization runtime.")
    bound = runtime.register_and_bind(route_id, policy)
    before = {id(route) for route in application.routes}
    caller_openapi = route_options.pop("openapi_extra", None)
    caller_responses = route_options.pop("responses", None)
    openapi_extra = dict(caller_openapi or {})
    openapi_extra["security"] = _PROTECTED_OPENAPI["security"]
    openapi_extra["parameters"] = [
        *(openapi_extra.get("parameters") or []),
        *_PROTECTED_OPENAPI["parameters"],
    ]
    route_options.setdefault("operation_id", route_id)
    responses = dict(_PROTECTED_ERROR_RESPONSES)
    responses.update(caller_responses or {})
    route_options["responses"] = responses
    override_provider = application.router.dependency_overrides_provider
    application.router.dependency_overrides_provider = (
        _ProtectedDependencyOverrideProvider(override_provider)
    )
    try:
        application.add_api_route(
            path,
            endpoint,
            methods=list(methods),
            name=route_id,
            openapi_extra=openapi_extra,
            **route_options,
        )
    finally:
        application.router.dependency_overrides_provider = override_provider
    route = _require_one_added_route(application, before)
    route.app = _authorized_route_app(route.app, bound)
    _route_classifications(application).register_protected(route, route_id)
    application.openapi_schema = None


def add_public_api_route(
    application: FastAPI,
    path: str,
    endpoint: Callable[..., Any],
    *,
    route_id: str,
    methods: Iterable[str],
    **route_options: Any,
) -> None:
    """Add one exact unauthenticated liveness or readiness route."""

    if path not in {"/api/v1/health/live", "/api/v1/health/ready"}:
        raise RouteAuthorizationConfigurationError(
            "Only liveness and readiness may be registered as public API routes."
        )
    registry = _route_classifications(application)
    before = {id(route) for route in application.routes}
    application.add_api_route(
        path,
        endpoint,
        methods=list(methods),
        name=route_id,
        **route_options,
    )
    registry.register_public(_require_one_added_route(application, before))
    application.openapi_schema = None


def validate_api_route_authorization(application: FastAPI) -> None:
    """Fail startup unless every concrete `/api/v1` route has one classification."""

    classifications = getattr(application.state, "route_classifications", None)
    runtime = getattr(application.state, "authorization_runtime", None)
    if type(classifications) is not ApiRouteClassificationRegistry:
        raise RouteAuthorizationConfigurationError(
            "API route classification registry is unavailable."
        )
    _validate_routes(
        application.routes,
        path_prefix="",
        classifications=classifications,
        runtime=runtime,
    )


def _validate_routes(
    routes: Iterable[object],
    *,
    path_prefix: str,
    classifications: ApiRouteClassificationRegistry,
    runtime: object,
) -> None:
    for route in routes:
        route_path = getattr(route, "path", "")
        path = _join_route_path(path_prefix, route_path)
        if isinstance(route, APIRoute):
            _validate_api_route(
                route,
                path=path,
                classifications=classifications,
                runtime=runtime,
            )
            continue
        if isinstance(route, Mount):
            mounted_routes = getattr(route, "routes", None)
            if mounted_routes:
                _validate_routes(
                    mounted_routes,
                    path_prefix=path,
                    classifications=classifications,
                    runtime=runtime,
                )
            elif _mount_may_contain_versioned_api(path):
                raise RouteAuthorizationConfigurationError(
                    f"Route {path} must have exactly one authorization classification."
                )
            continue
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_route_contexts):
            for context in effective_route_contexts():
                original_route = getattr(context, "original_route", None)
                context_path = _join_route_path(
                    path_prefix,
                    getattr(context, "path", ""),
                )
                if isinstance(original_route, APIRoute):
                    _validate_api_route(
                        original_route,
                        path=context_path,
                        classifications=classifications,
                        runtime=runtime,
                    )


def install_authorization_openapi(application: FastAPI) -> None:
    """Add the reusable HTTP Bearer scheme when protected routes are present."""

    original_openapi = application.openapi

    def authorized_openapi() -> dict[str, Any]:
        schema = original_openapi()
        classifications = _route_classifications(application)
        if any(
            isinstance(route, APIRoute)
            and (
                classification := classifications.classification(route)
            ) is not None
            and classification.kind is ApiRouteKind.PROTECTED
            for route in application.routes
        ):
            components = schema.setdefault("components", {})
            security_schemes = components.setdefault("securitySchemes", {})
            security_schemes["BearerAuth"] = {
                "type": "http",
                "scheme": "bearer",
            }
        return schema

    application.openapi = authorized_openapi  # type: ignore[method-assign]


def _require_one_added_route(
    application: FastAPI,
    prior_route_ids: set[int],
) -> APIRoute:
    added = [
        route
        for route in application.routes
        if id(route) not in prior_route_ids and isinstance(route, APIRoute)
    ]
    if len(added) != 1:
        raise RouteAuthorizationConfigurationError(
            "API route registration is ambiguous."
        )
    return added[0]


def _is_versioned_api_path(path: str) -> bool:
    return path == "/api/v1" or path.startswith("/api/v1/")


def _mount_may_contain_versioned_api(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return (
        normalized == "/"
        or _is_versioned_api_path(normalized)
        or "/api/v1".startswith(f"{normalized}/")
    )


def _join_route_path(prefix: str, path: str) -> str:
    if not prefix or prefix == "/":
        base = ""
    else:
        base = prefix.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base}{suffix}" or "/"


def _route_classifications(
    application: FastAPI,
) -> ApiRouteClassificationRegistry:
    registry = getattr(application.state, "route_classifications", None)
    if type(registry) is not ApiRouteClassificationRegistry:
        raise RouteAuthorizationConfigurationError(
            "API route classification registry is unavailable."
        )
    return registry


def _validate_api_route(
    route: APIRoute,
    *,
    path: str,
    classifications: ApiRouteClassificationRegistry,
    runtime: object,
) -> None:
    if not _is_versioned_api_path(path):
        return
    classification = classifications.classification(route)
    if classification is None:
        raise RouteAuthorizationConfigurationError(
            f"Route {path} must have exactly one authorization classification."
        )
    if classification.kind is ApiRouteKind.PROTECTED:
        if type(runtime) is not ApiAuthorizationRuntime:
            raise RouteAuthorizationConfigurationError(
                "Protected route authorization runtime is unavailable."
            )
        policy_id = classification.policy_id
        if policy_id is None:
            raise RouteAuthorizationConfigurationError(
                f"Route {path} has no registered authorization policy."
            )
        try:
            runtime.require_policy(policy_id)
        except Exception:
            raise RouteAuthorizationConfigurationError(
                f"Route {path} has no registered authorization policy."
            ) from None


def _authorized_route_app(
    route_app: ASGIApp,
    bound: _BoundRouteAuthorization,
) -> ASGIApp:
    async def authorized_app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive=receive)
        credentials = await get_bearer_credential(request)
        diagnostic_context = await get_diagnostic_context(request)
        async with bound.authorize_with_scope_resolver(
            credentials=credentials,
            requested_scope_resolver=lambda: _requested_scope(request),
            diagnostic_context=diagnostic_context,
        ) as authorized:
            state = scope.setdefault("state", {})
            state[_AUTHORIZED_CONTEXT_STATE_KEY] = authorized.request_context
            state[_AUTHORIZED_PERSISTENCE_CONTEXT_STATE_KEY] = authorized.persistence_context
            audit_event_id = getattr(authorized, "authorization_audit_event_id", None)
            if isinstance(audit_event_id, UUID):
                state[_AUTHORIZATION_AUDIT_EVENT_ID_STATE_KEY] = audit_event_id
            try:
                await route_app(scope, receive, send)
            finally:
                state.pop(_AUTHORIZED_CONTEXT_STATE_KEY, None)
                state.pop(_AUTHORIZED_PERSISTENCE_CONTEXT_STATE_KEY, None)
                state.pop(_AUTHORIZATION_AUDIT_EVENT_ID_STATE_KEY, None)

    return authorized_app


async def get_bearer_credential(request: Request) -> AccessTokenCredential:
    """Extract one opaque RFC 6750 Bearer credential for protected routes."""

    if len(request.headers.getlist("authorization")) != 1:
        raise InvalidAuthenticationError()
    extracted: HTTPAuthorizationCredentials | None = await _BEARER(request)
    if (
        extracted is None
        or extracted.scheme.lower() != "bearer"
        or not is_http_bearer_token(extracted.credentials)
    ):
        raise InvalidAuthenticationError()
    try:
        return AccessTokenCredential(access_token=extracted.credentials)
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise InvalidAuthenticationError() from None


def _requested_scope(request: Request) -> RequestedAuthorizationScope:
    headers = Headers(scope=request.scope)
    workspace_values = headers.getlist(WORKSPACE_ID_HEADER)
    environment_values = headers.getlist(ENVIRONMENT_ID_HEADER)
    if len(workspace_values) != 1 or len(environment_values) != 1:
        raise AuthorizationDeniedError()
    try:
        return RequestedAuthorizationScope(
            workspace_id=_canonical_uuid(workspace_values[0]),
            environment_id=_canonical_uuid(environment_values[0]),
        )
    except (TypeError, ValidationError, ValueError):
        raise AuthorizationDeniedError() from None


def _canonical_uuid(value: str) -> UUID:
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("UUID selector is not canonical")
    return parsed


__all__ = [
    "ApiAuthorizationReadiness",
    "ApiAuthorizationReadinessCode",
    "ApiAuthorizationRuntime",
    "ApiRouteClassificationRegistry",
    "ENVIRONMENT_ID_HEADER",
    "RouteAuthorizationComposer",
    "RouteAuthorizationConfigurationError",
    "WORKSPACE_ID_HEADER",
    "add_public_api_route",
    "add_protected_api_route",
    "get_authorized_request_context",
    "get_authorized_persistence_context",
    "get_authorization_audit_event_id",
    "get_bearer_credential",
    "install_authorization_openapi",
    "validate_api_route_authorization",
]
