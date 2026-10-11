from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypeVar
from uuid import UUID

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    ApiAuthorizationRuntime,
    RouteAuthorizationConfigurationError,
    add_protected_api_route,
    get_authorized_request_context,
)
from spine.api.main import create_app
from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    DiagnosticContext,
    DiagnosticEventRegistry,
)
from spine.application.persistence import (
    InvalidPersistenceContextError,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.auth import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthenticationUnavailableError,
    AuthorizationDeniedError,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    InMemoryAuthorizationDirectory,
    InMemoryAuthorizationEntry,
    InvalidAuthenticationError,
    RouteAuthorizationPolicy,
    RoutePolicyRegistry,
    AuthorizationUnavailableError,
)
from spine.auth.in_memory import AuthorizationRecordState
from spine.infrastructure.auth import (
    LocalAuthenticationAdapter,
    LocalAuthenticationSettings,
    RequestAuthorizationComposer,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from tests.contracts.diagnostics.fixtures import DIAGNOSTIC_LEAK_CORPUS


TRACE_ID = UUID("50000000-0000-0000-0000-000000000073")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000073")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000073")
IDENTITY_ID = UUID("10000000-0000-0000-0000-000000000073")
SERVICE_PRINCIPAL_ID = UUID("40000000-0000-0000-0000-000000000073")
REQUEST_ISSUER_ID = UUID("61000000-0000-0000-0000-000000000073")
PERSISTENCE_ISSUER_ID = UUID("60000000-0000-0000-0000-000000000073")
AUDIT_EVENT_ID = UUID("80000000-0000-0000-0000-000000000073")
NOW = datetime(2026, 10, 11, 15, tzinfo=timezone.utc)
ROUTE_ID = "test.authorization.inspect"
CONFIGURATION_VERSION = "oidc-2026-10-11"
AUTHENTICATION_ALIAS = AuthenticationAlias(
    issuer="https://identity.example.test",
    subject="synthetic-administrator",
)


class StaticAuthentication:
    def __init__(self, result: AuthenticatedAlias | Exception | None = None) -> None:
        self.result = result or AuthenticatedAlias(
            alias=AUTHENTICATION_ALIAS,
            authentication_configuration_version=CONFIGURATION_VERSION,
        )

    async def authenticate(
        self,
        credentials: AccessTokenCredential,
        diagnostic_context: DiagnosticContext,
    ) -> AuthenticatedAlias:
        del credentials, diagnostic_context
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RaisingDirectory:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def resolve_request_authority(self, *args: object) -> None:
        del args
        raise self.failure


class SyntheticCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1)


_ResultT = TypeVar("_ResultT")


class ImmediateAuditGate:
    async def release_after_audit(
        self,
        *,
        context: object,
        event: AuditEvent,
        release: Callable[[UUID], Awaitable[_ResultT]],
    ) -> _ResultT:
        del context, event
        return await release(AUDIT_EVENT_ID)


async def authorization_ready() -> bool:
    return True


def route_policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_authorization"),
        service_principal_id=SERVICE_PRINCIPAL_ID,
    )


def authorization_entry(
    alias: AuthenticationAlias = AUTHENTICATION_ALIAS,
) -> InMemoryAuthorizationEntry:
    return InMemoryAuthorizationEntry(
        alias=alias,
        canonical_human_identity_id=IDENTITY_ID,
        identity_state=AuthorizationRecordState.ACTIVE,
        alias_state=AuthorizationRecordState.ACTIVE,
        workspace_id=WORKSPACE_ID,
        workspace_membership_state=AuthorizationRecordState.ACTIVE,
        environment_id=ENVIRONMENT_ID,
        environment_workspace_id=WORKSPACE_ID,
        environment_membership_state=AuthorizationRecordState.ACTIVE,
        roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        role_binding_state=AuthorizationRecordState.ACTIVE,
        snapshot_generation=1,
        current_generation=1,
    )


def authorized_app(
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]],
    *,
    authentication: object | None = None,
    directory: object | None = None,
    readiness_probe: Callable[[], Awaitable[bool]] = authorization_ready,
) -> FastAPI:
    diagnostic_registry = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_registry)
    persistence_boundary = TrustedContextBoundary.for_testing(
        issuer_id=PERSISTENCE_ISSUER_ID,
        secret=b"ticket-73-persistence-context-secret",
    )
    request_boundary = TrustedRequestContextBoundary.for_testing(
        persistence_boundary=persistence_boundary,
        issuer_id=REQUEST_ISSUER_ID,
        secret=b"ticket-73-request-context-secret-0001",
    )
    route_policies = RoutePolicyRegistry()
    composer = RequestAuthorizationComposer(
        authentication=authentication or StaticAuthentication(),  # type: ignore[arg-type]
        authorization_directory=(
            directory
            or InMemoryAuthorizationDirectory(entries=(authorization_entry(),))
        ),  # type: ignore[arg-type]
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=AuditEventRegistry.with_default_families(),
        audit_gate=ImmediateAuditGate(),
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=lambda: NOW,
        request_id_source=lambda: UUID("70000000-0000-0000-0000-000000000073"),
    )
    application = create_app(
        trace_id_source=lambda: TRACE_ID,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=lambda: NOW,
        authorization_runtime=ApiAuthorizationRuntime(
            composer=composer,
            route_policies=route_policies,
            readiness_probe=readiness_probe,
        ),
    )

    async def inspect_authorization(
        context: AuthorizedRequestContext[DiagnosticContext] = Depends(
            get_authorized_request_context
        ),
    ) -> dict[str, str]:
        handler_calls.append(context)
        return {
            "actor_id": str(context.acting_subject_id),
            "workspace_id": str(context.scope.workspace_id),
            "environment_id": str(context.scope.environment_id),
            "trace_id": str(context.diagnostic_context.trace_id),
        }

    add_protected_api_route(
        application,
        "/api/v1/test/authorization",
        inspect_authorization,
        route_id=ROUTE_ID,
        policy=route_policy(),
        methods=["GET"],
    )
    return application


async def test_missing_bearer_returns_safe_401_before_handler() -> None:
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]] = []
    application = authorized_app(handler_calls)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            headers={
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 401
    assert response.headers["x-trace-id"] == str(TRACE_ID)
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "schema_version": 1,
        "code": "spine.authentication.invalid",
        "safe_message": "Authentication is invalid.",
        "retryability": "never",
        "trace_id": str(TRACE_ID),
    }
    assert handler_calls == []


@pytest.mark.parametrize(
    "scope_headers",
    [
        {},
        {
            "X-Workspace-ID": "not-a-uuid",
            "X-Environment-ID": "also-not-a-uuid",
        },
    ],
)
async def test_invalid_bearer_is_rejected_before_missing_or_malformed_scope(
    scope_headers: dict[str, str],
) -> None:
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]] = []
    application = authorized_app(
        handler_calls,
        authentication=StaticAuthentication(InvalidAuthenticationError()),
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer invalid-provider-token",
                **scope_headers,
            },
        )

    assert response.status_code == 401
    assert response.json()["code"] == "spine.authentication.invalid"
    assert handler_calls == []


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic opaque-provider-token",
        "Bearer",
        "Bearer token with spaces",
        "Bearer token,second-value",
    ],
)
async def test_malformed_bearer_uses_the_same_safe_401_category(
    authorization: str,
) -> None:
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]] = []
    application = authorized_app(handler_calls)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": authorization,
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 401
    assert response.json()["code"] == "spine.authentication.invalid"
    assert handler_calls == []


async def test_valid_bearer_resolves_trusted_actor_scope_and_trace() -> None:
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]] = []
    application = authorized_app(handler_calls)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            params={
                "actor_id": "10000000-0000-0000-0000-000000000099",
                "role": "administrator",
                "route_policy": "caller-selected",
            },
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
                "X-Actor-ID": "10000000-0000-0000-0000-000000000099",
                "X-Roles": "administrator",
                "X-Route-Policy": "caller-selected",
                "X-Trusted-Issuer": "caller-selected",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == str(TRACE_ID)
    assert response.json() == {
        "actor_id": str(IDENTITY_ID),
        "workspace_id": str(WORKSPACE_ID),
        "environment_id": str(ENVIRONMENT_ID),
        "trace_id": str(TRACE_ID),
    }
    assert len(handler_calls) == 1
    with pytest.raises(InvalidPersistenceContextError):
        _ = handler_calls[0].acting_subject_id


@pytest.mark.parametrize(
    ("authentication", "directory", "expected_status", "expected_code"),
    [
        (
            StaticAuthentication(InvalidAuthenticationError()),
            None,
            401,
            "spine.authentication.invalid",
        ),
        (
            StaticAuthentication(AuthenticationUnavailableError()),
            None,
            503,
            "spine.authentication.unavailable",
        ),
        (
            None,
            RaisingDirectory(AuthorizationDeniedError()),
            403,
            "spine.authorization.denied",
        ),
        (
            None,
            RaisingDirectory(AuthorizationUnavailableError()),
            503,
            "spine.authorization.unavailable",
        ),
        (
            StaticAuthentication(RuntimeError("raw provider failure")),
            None,
            500,
            "spine.internal.unexpected",
        ),
    ],
)
async def test_authentication_and_authorization_failures_are_safe_and_stop_handler(
    authentication: object | None,
    directory: object | None,
    expected_status: int,
    expected_code: str,
) -> None:
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]] = []
    application = authorized_app(
        handler_calls,
        authentication=authentication,
        directory=directory,
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer protected-token-value",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == expected_status
    assert response.headers["x-trace-id"] == str(TRACE_ID)
    assert response.json()["code"] == expected_code
    assert response.json()["trace_id"] == str(TRACE_ID)
    assert "protected-token-value" not in response.text
    assert "raw provider failure" not in response.text
    assert handler_calls == []


@pytest.mark.parametrize("sensitive_value", DIAGNOSTIC_LEAK_CORPUS)
async def test_auth_failure_responses_and_diagnostics_exclude_negative_leak_corpus(
    sensitive_value: str,
) -> None:
    application = authorized_app(
        [],
        authentication=StaticAuthentication(RuntimeError(sensitive_value)),
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer protected-token-value",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    sink = application.state.diagnostic_sink
    captures = response.text + "\n" + "\n".join(
        event.model_dump_json() for event in sink.events
    )
    assert response.status_code == 500
    assert sensitive_value not in captures
    assert "protected-token-value" not in captures


async def test_missing_or_foreign_scope_returns_safe_403_before_handler() -> None:
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]] = []
    application = authorized_app(handler_calls)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get(
            "/api/v1/test/authorization",
            headers={"Authorization": "Bearer opaque-provider-token"},
        )
        foreign = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": "20000000-0000-0000-0000-000000000099",
                "X-Environment-ID": "30000000-0000-0000-0000-000000000099",
            },
        )

    for response in (missing, foreign):
        assert response.status_code == 403
        assert response.json()["code"] == "spine.authorization.denied"
        assert response.headers["x-trace-id"] == str(TRACE_ID)
    assert handler_calls == []


@pytest.mark.parametrize(
    "workspace_value",
    [
        "{" + str(WORKSPACE_ID) + "}",
        WORKSPACE_ID.hex,
        "00000000-0000-0000-0000-000000000000",
    ],
)
async def test_noncanonical_workspace_selector_is_denied_before_handler(
    workspace_value: str,
) -> None:
    handler_calls: list[AuthorizedRequestContext[DiagnosticContext]] = []
    application = authorized_app(handler_calls)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": workspace_value,
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "spine.authorization.denied"
    assert handler_calls == []


async def test_body_validation_runs_only_after_successful_authorization() -> None:
    application = authorized_app([])
    handler_calls: list[int] = []

    async def command(body: SyntheticCommand) -> dict[str, int]:
        handler_calls.append(body.count)
        return {"count": body.count}

    add_protected_api_route(
        application,
        "/api/v1/test/command",
        command,
        route_id="test.authorization.command",
        policy=route_policy(),
        methods=["POST"],
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/test/command",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
            json={"count": "protected customer text"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "spine.request.validation"
    assert "protected customer text" not in response.text
    assert handler_calls == []


async def test_dependency_override_cannot_supply_actor_role_scope_or_policy() -> None:
    application = authorized_app([])

    class CallerControlledContext:
        acting_subject_id = UUID("10000000-0000-0000-0000-000000000099")
        scope = type(
            "CallerScope",
            (),
            {
                "workspace_id": UUID("20000000-0000-0000-0000-000000000099"),
                "environment_id": UUID("30000000-0000-0000-0000-000000000099"),
            },
        )()
        diagnostic_context = DiagnosticContext(
            trace_id=UUID("50000000-0000-0000-0000-000000000099")
        )

    application.dependency_overrides[get_authorized_request_context] = (
        lambda: CallerControlledContext()
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "actor_id": str(IDENTITY_ID),
        "workspace_id": str(WORKSPACE_ID),
        "environment_id": str(ENVIRONMENT_ID),
        "trace_id": str(TRACE_ID),
    }


async def test_local_mode_uses_configured_alias_and_the_same_authorization_directory() -> None:
    local_settings = LocalAuthenticationSettings(
        deployment_mode="development",
        configuration_version="local-2026-10-11",
        authentication_alias_issuer="spine-local://development",
        authentication_alias_subject="configured-local-administrator",
        bearer_credential="local-development-credential-0000001",
    )
    application = authorized_app(
        [],
        authentication=LocalAuthenticationAdapter(local_settings),
        directory=InMemoryAuthorizationDirectory(
            entries=(authorization_entry(local_settings.authentication_alias),)
        ),
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer local-development-credential-0000001",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )
        caller_selected_identity = await client.get(
            "/api/v1/test/authorization",
            headers={
                "Authorization": "Bearer configured-local-administrator",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert allowed.status_code == 200
    assert allowed.json()["actor_id"] == str(IDENTITY_ID)
    assert caller_selected_identity.status_code == 401
    assert caller_selected_identity.json()["code"] == "spine.authentication.invalid"


async def test_public_health_exemptions_are_explicit_and_have_no_security_scope() -> None:
    application = authorized_app([])

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            live = await client.get("/api/v1/health/live")
            ready = await client.get("/api/v1/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 200
    schema = application.openapi()
    for path in ("/api/v1/health/live", "/api/v1/health/ready"):
        operation = schema["paths"][path]["get"]
        assert "security" not in operation
        assert "parameters" not in operation


async def test_readiness_reports_authentication_unavailability_without_details() -> None:
    async def unavailable() -> bool:
        return False

    application = authorized_app([], readiness_probe=unavailable)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "service": "spine"}


async def test_public_exemption_is_not_inherited_by_a_new_route_with_same_name() -> None:
    application = create_app(trace_id_source=lambda: TRACE_ID)
    router = APIRouter(prefix="/api/v1")

    @router.get("/copied-health-name", name="system.health.live")
    async def copied_health_name() -> dict[str, str]:
        return {"status": "unsafe"}

    application.include_router(router)
    with pytest.raises(RouteAuthorizationConfigurationError):
        async with application.router.lifespan_context(application):
            pass


async def test_root_mount_cannot_hide_an_unclassified_versioned_api_route() -> None:
    application = create_app(trace_id_source=lambda: TRACE_ID)
    mounted = FastAPI()

    @mounted.get("/api/v1/unclassified")
    async def unclassified() -> dict[str, str]:
        return {"status": "unsafe"}

    application.mount("/", mounted)

    with pytest.raises(RouteAuthorizationConfigurationError, match="classification"):
        async with application.router.lifespan_context(application):
            pass


async def test_opaque_api_prefix_mount_cannot_hide_versioned_routes() -> None:
    application = create_app(trace_id_source=lambda: TRACE_ID)

    async def opaque_app(scope: object, receive: object, send: object) -> None:
        del scope, receive, send

    application.mount("/api", opaque_app)

    with pytest.raises(RouteAuthorizationConfigurationError, match="classification"):
        async with application.router.lifespan_context(application):
            pass


def test_protected_route_openapi_declares_bearer_and_required_scope_headers() -> None:
    application = authorized_app([])

    schema = application.openapi()

    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    operation = schema["paths"]["/api/v1/test/authorization"]["get"]
    assert operation["operationId"] == ROUTE_ID
    assert operation["security"] == [{"BearerAuth": []}]
    assert [
        {
            "name": parameter["name"],
            "in": parameter["in"],
            "required": parameter["required"],
            "schema": parameter["schema"],
        }
        for parameter in operation["parameters"]
    ] == [
        {
            "name": "X-Workspace-ID",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        },
        {
            "name": "X-Environment-ID",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        },
    ]


async def test_startup_rejects_unclassified_versioned_api_route() -> None:
    application = create_app(trace_id_source=lambda: TRACE_ID)

    @application.get("/api/v1/unclassified")
    async def unclassified() -> dict[str, str]:
        return {"status": "unsafe"}

    with pytest.raises(
        RouteAuthorizationConfigurationError,
        match="classification",
    ):
        async with application.router.lifespan_context(application):
            pass
