"""HTTP composition roots for deployable authentication modes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

from spine.api.authorization import ApiAuthorizationRuntime
from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticEventRegistry,
    DiagnosticSink,
)
from spine.auth import RoutePolicyRegistry
from spine.infrastructure.auth import (
    LocalAuthenticationAdapter,
    LocalAuthenticationConfigurationError,
    LocalAuthenticationSettings,
    RequestAuthorizationComposer,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.auth.composition import RequiredAuditGate
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_authorization import (
    PostgreSQLAuthorizationDirectory,
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

    composer = RequestAuthorizationComposer(
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

    async def local_readiness() -> bool:
        return True

    return ApiAuthorizationRuntime(
        composer=composer,
        route_policies=route_policies,
        readiness_probe=local_readiness,
    )


__all__ = ["compose_local_api_authorization"]
