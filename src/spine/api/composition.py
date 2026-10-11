"""HTTP composition roots for deployable authentication modes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from spine.api.authorization import ApiAuthorizationReadiness, ApiAuthorizationRuntime
from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticSink,
)
from spine.auth import AuthenticationPort, AuthorizationDirectory, RoutePolicyRegistry
from spine.infrastructure.auth import (
    LocalAuthenticationAdapter,
    LocalAuthenticationConfigurationError,
    LocalAuthenticationSettings,
    OIDCAuthenticationRuntime,
    OIDCAuthenticationSettings,
    OIDCReadinessStatus,
    RequestAuthorizationComposer,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.auth.composition import RequiredAuditGate
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_authorization import (
    PostgreSQLAuthorizationDirectory,
    check_postgresql_authorization_store_readiness,
)


def _compose_request_authorization(
    *,
    authentication: AuthenticationPort[DiagnosticContext],
    authorization_directory: AuthorizationDirectory,
    route_policies: RoutePolicyRegistry,
    request_boundary: TrustedRequestContextBoundary,
    audit_registry: AuditEventRegistry,
    audit_gate: RequiredAuditGate,
    diagnostic_registry: DiagnosticEventRegistry,
    diagnostic_sink: DiagnosticSink,
    clock: Callable[[], datetime],
    request_id_source: Callable[[], UUID],
) -> RequestAuthorizationComposer:
    return RequestAuthorizationComposer(
        authentication=authentication,
        authorization_directory=authorization_directory,
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=audit_gate,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=clock,
        request_id_source=request_id_source,
    )


def compose_local_api_authorization(
    *,
    session_factory: SessionFactory,
    route_policies: RoutePolicyRegistry,
    request_boundary: TrustedRequestContextBoundary,
    audit_registry: AuditEventRegistry,
    audit_gate: RequiredAuditGate,
    diagnostic_registry: DiagnosticEventRegistry,
    diagnostic_sink: DiagnosticSink,
    settings: LocalAuthenticationSettings | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    request_id_source: Callable[[], UUID] = uuid4,
) -> ApiAuthorizationRuntime:
    """Build local HTTP auth with the production PostgreSQL authority lookup."""

    try:
        resolved_settings = settings or LocalAuthenticationSettings()
        authentication = LocalAuthenticationAdapter(resolved_settings)
    except (LocalAuthenticationConfigurationError, ValidationError):
        raise LocalAuthenticationConfigurationError() from None

    composer = _compose_request_authorization(
        authentication=authentication,
        authorization_directory=PostgreSQLAuthorizationDirectory(session_factory),
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=audit_gate,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=clock,
        request_id_source=request_id_source,
    )

    async def local_readiness() -> ApiAuthorizationReadiness:
        store_ready = await check_postgresql_authorization_store_readiness(
            session_factory
        )
        return ApiAuthorizationReadiness(
            ready=store_ready,
            code=(
                "local.ready"
                if store_ready
                else "authorization.store_unavailable"
            ),
            authentication_configuration_version=(
                resolved_settings.configuration_version
            ),
            authentication_mode="local",
        )

    return ApiAuthorizationRuntime(
        composer=composer,
        route_policies=route_policies,
        readiness_probe=local_readiness,
    )


def compose_oidc_api_authorization(
    *,
    settings: OIDCAuthenticationSettings,
    session_factory: SessionFactory,
    route_policies: RoutePolicyRegistry,
    request_boundary: TrustedRequestContextBoundary,
    audit_registry: AuditEventRegistry,
    audit_gate: RequiredAuditGate,
    diagnostic_registry: DiagnosticEventRegistry,
    diagnostic_sink: DiagnosticSink,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    request_id_source: Callable[[], UUID] = uuid4,
) -> ApiAuthorizationRuntime:
    """Build production OIDC HTTP auth with canonical PostgreSQL authority."""

    oidc_runtime = OIDCAuthenticationRuntime(
        settings,
        transport=transport,
        clock=clock,
    )
    composer = _compose_request_authorization(
        authentication=oidc_runtime.authentication,
        authorization_directory=PostgreSQLAuthorizationDirectory(session_factory),
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=audit_gate,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=clock,
        request_id_source=request_id_source,
    )

    async def oidc_readiness() -> ApiAuthorizationReadiness:
        readiness = await oidc_runtime.check_readiness()
        authentication_ready = readiness.status in {
            OIDCReadinessStatus.READY,
            OIDCReadinessStatus.DEGRADED,
        }
        if not authentication_ready:
            return ApiAuthorizationReadiness(
                ready=False,
                code=readiness.code.value,
                authentication_configuration_version=readiness.configuration_version,
                authentication_mode=readiness.mode,
            )
        store_ready = await check_postgresql_authorization_store_readiness(
            session_factory
        )
        return ApiAuthorizationReadiness(
            ready=store_ready,
            code=(
                readiness.code.value
                if store_ready
                else "authorization.store_unavailable"
            ),
            authentication_configuration_version=readiness.configuration_version,
            authentication_mode=readiness.mode,
        )

    return ApiAuthorizationRuntime(
        composer=composer,
        route_policies=route_policies,
        readiness_probe=oidc_readiness,
        startup_hook=oidc_runtime.startup,
        close_hook=oidc_runtime.aclose,
    )


__all__ = ["compose_local_api_authorization", "compose_oidc_api_authorization"]
