from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypeVar
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from spine.api.authorization import (
    ApiAuthorizationReadiness,
    ApiAuthorizationRuntime,
)
from spine.api.composition import (
    compose_local_api_authorization,
    compose_oidc_api_authorization,
)
from spine.api.main import create_app
from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    DiagnosticEventRegistry,
)
from spine.application.persistence import TrustedPersistenceContext
from spine.auth import RoutePolicyRegistry
from spine.infrastructure.auth import (
    InteractiveClientQualification,
    LocalAuthenticationSettings,
    OIDCAuthenticationSettings,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


_ResultT = TypeVar("_ResultT")


class ImmediateAuditGate:
    async def release_after_audit(
        self,
        *,
        context: TrustedPersistenceContext,
        event: AuditEvent,
        release: Callable[[UUID], Awaitable[_ResultT]],
    ) -> _ResultT:
        del context, event
        return await release(UUID("80000000-0000-0000-0000-000000000074"))


class UnusedComposer:
    def bind_route(self, route_id: str) -> object:
        if route_id in {
            "system.health.summary",
            "outbox.deliveries.quarantined",
        }:
            return object()
        raise AssertionError(f"unexpected route binding: {route_id}")


class UnavailableSessionFactory:
    def __call__(self) -> AsyncSession:
        raise RuntimeError("synthetic authorization store failure")


def _build_oidc_runtime(
    provider: Callable[[httpx.Request], httpx.Response],
) -> ApiAuthorizationRuntime:
    settings = OIDCAuthenticationSettings(
        configuration_version="oidc-qualification-v1",
        issuer="https://identity.example.test/tenant",
        audience="https://api.spine.example.test",
        qualified_interactive_clients=(
            InteractiveClientQualification(
                client_id="spine-web",
                configuration_version="oidc-qualification-v1",
                issuer="https://identity.example.test/tenant",
                audience="https://api.spine.example.test",
                client_credentials_for_audience_disabled=True,
                evidence_reference="qualification-fixture",
            ),
        ),
    )
    diagnostic_registry = DiagnosticEventRegistry.with_default_families()
    persistence_boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("60000000-0000-0000-0000-000000000074"),
        secret=b"ticket-74-persistence-boundary-secret",
    )
    request_boundary = TrustedRequestContextBoundary.for_testing(
        persistence_boundary=persistence_boundary,
        issuer_id=UUID("61000000-0000-0000-0000-000000000074"),
        secret=b"ticket-74-request-boundary-secret-01",
    )
    return compose_oidc_api_authorization(
        settings=settings,
        session_factory=async_sessionmaker(),
        route_policies=RoutePolicyRegistry(),
        request_boundary=request_boundary,
        audit_registry=AuditEventRegistry.with_default_families(),
        audit_gate=ImmediateAuditGate(),
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=RecordingDiagnosticSink(diagnostic_registry),
        transport=httpx.MockTransport(provider),
        clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc),
    )


async def test_oidc_api_composition_reports_safe_provider_readiness_code() -> None:
    requests: list[str] = []

    def unavailable_provider(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(503)

    runtime = _build_oidc_runtime(unavailable_provider)

    await runtime.startup()

    readiness = await runtime.check_readiness()

    assert readiness.ready is False
    assert readiness.code == "oidc.provider_unavailable"
    assert requests

    await runtime.aclose()


async def test_closed_oidc_api_runtime_performs_no_provider_io() -> None:
    requests: list[str] = []

    def unavailable_provider(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(503)

    runtime = _build_oidc_runtime(unavailable_provider)

    await runtime.aclose()

    requests_before_closed_probe = list(requests)
    assert await runtime.is_ready() is False
    assert requests == requests_before_closed_probe


async def test_local_api_readiness_reports_authorization_store_unavailable() -> None:
    diagnostic_registry = DiagnosticEventRegistry.with_default_families()
    persistence_boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("60000000-0000-0000-0000-000000000076"),
        secret=b"ticket-74-local-persistence-boundary",
    )
    request_boundary = TrustedRequestContextBoundary.for_testing(
        persistence_boundary=persistence_boundary,
        issuer_id=UUID("61000000-0000-0000-0000-000000000076"),
        secret=b"ticket-74-local-request-boundary-0001",
    )
    runtime = compose_local_api_authorization(
        settings=LocalAuthenticationSettings(
            deployment_mode="development",
            configuration_version="local-qualification-v1",
            authentication_alias_issuer="spine-local://development",
            authentication_alias_subject="configured-administrator",
            bearer_credential="local-qualification-credential-0000001",
        ),
        session_factory=UnavailableSessionFactory(),
        route_policies=RoutePolicyRegistry(),
        request_boundary=request_boundary,
        audit_registry=AuditEventRegistry.with_default_families(),
        audit_gate=ImmediateAuditGate(),
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=RecordingDiagnosticSink(diagnostic_registry),
    )

    readiness = await runtime.check_readiness()

    assert readiness.ready is False
    assert readiness.code == "authorization.store_unavailable"
    assert readiness.authentication_configuration_version == "local-qualification-v1"


async def test_app_closes_authorization_runtime_when_startup_fails() -> None:
    closed: list[bool] = []

    async def ready() -> ApiAuthorizationReadiness:
        return ApiAuthorizationReadiness(
            ready=False,
            code="authorization.unavailable",
        )

    async def fail_startup() -> None:
        raise RuntimeError("synthetic startup failure")

    async def close() -> None:
        closed.append(True)

    runtime = ApiAuthorizationRuntime(
        composer=UnusedComposer(),
        route_policies=RoutePolicyRegistry(),
        readiness_probe=ready,
        startup_hook=fail_startup,
        close_hook=close,
    )
    application = create_app(authorization_runtime=runtime)

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        async with application.router.lifespan_context(application):
            raise AssertionError("startup failure must prevent application release")

    assert closed == [True]
