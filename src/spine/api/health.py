"""Public and administrator-only health transport contracts."""

from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError, version as package_version
import re
from typing import Literal, TypeAlias
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.api.authorization import (
    ApiAuthorizationReadiness,
    add_protected_api_route,
    get_authorized_request_context,
)
from spine.api.schemas import ApiResponseEnvelope
from spine.application.health_summary import (
    HealthComponentSnapshot,
    HealthOperationSnapshot,
    HealthStatus,
    HealthStatusServices,
    SAFE_HEALTH_CODES,
    SAFE_PROVIDER_NAMES,
    UnavailableHealthStatusServices,
    collect_health_summary,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)


_SAFE_CONFIGURATION_VERSION = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_HEALTH_SUMMARY_OPERATION = "system.health.summary"
_DEFAULT_TIMEOUT_SECONDS = 0.25

HealthReadinessCode: TypeAlias = Literal[
    "authorization.ready",
    "authorization.unavailable",
    "authorization.store_unavailable",
    "local.ready",
    "oidc.ready",
    "oidc.check_deferred",
    "oidc.stale_keys",
    "oidc.provider_unavailable",
    "oidc.provider_incompatible",
    "oidc.runtime_closed",
]

HealthCode: TypeAlias = Literal[
    "authorization.ready",
    "local.ready",
    "oidc.ready",
    "oidc.check_deferred",
    "oidc.stale_keys",
    "oidc.provider_unavailable",
    "oidc.provider_incompatible",
    "oidc.runtime_closed",
    "authorization.degraded",
    "authorization.unavailable",
    "authorization.stale",
    "authorization.store_unavailable",
    "authorization.provider_unavailable",
    "authorization.provider_incompatible",
    "dependency.unavailable",
    "ingestion.ready",
    "ingestion.degraded",
    "ingestion.partial",
    "ingestion.unavailable",
    "ingestion.stale",
    "publication.ready",
    "publication.degraded",
    "publication.unavailable",
    "publication.stale",
]
HealthProviderName: TypeAlias = Literal[
    "local",
    "oidc",
    "postgresql",
    "filesystem",
    "cognee",
    "neo4j",
    "pgvector",
    "memory",
]


class LivenessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"]
    service: Literal["spine"]


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "not_ready"]
    service: Literal["spine"]
    code: HealthReadinessCode


class HealthComponentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: HealthStatus
    code: HealthCode
    provider_name: HealthProviderName | None = None

    @field_validator("code")
    @classmethod
    def _allowlist_code(cls, value: HealthCode) -> HealthCode:
        if value not in SAFE_HEALTH_CODES:
            raise ValueError("health code is not allowlisted")
        return value

    @field_validator("provider_name")
    @classmethod
    def _allowlist_provider(
        cls, value: HealthProviderName | None
    ) -> HealthProviderName | None:
        if value is not None and value not in SAFE_PROVIDER_NAMES:
            raise ValueError("provider name is not allowlisted")
        return value


class HealthProvidersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authentication: HealthProviderName | None = None
    publication: HealthProviderName | None = None
    ingestion: HealthProviderName | None = None

    @field_validator("authentication", "publication", "ingestion")
    @classmethod
    def _allowlist_provider(
        cls, value: HealthProviderName | None
    ) -> HealthProviderName | None:
        if value is not None and value not in SAFE_PROVIDER_NAMES:
            raise ValueError("provider name is not allowlisted")
        return value


class HealthOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["authorization", "ingestion", "publication", "reconciliation"]
    status: Literal["accepted", "running", "completed", "failed", "stale", "unavailable"]
    completed_at: str | None = None


class HealthSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: HealthStatus
    build_version: str = Field(pattern=_SAFE_CONFIGURATION_VERSION)
    authentication_mode: Literal["local", "oidc"] | None = None
    configuration_version: str | None = Field(default=None, pattern=_SAFE_CONFIGURATION_VERSION)
    authorization: HealthComponentResponse
    publication: HealthComponentResponse
    ingestion: HealthComponentResponse
    providers: HealthProvidersResponse
    last_operation: HealthOperationResponse | None = None


def install_health_summary_route(
    application: FastAPI,
    *,
    services: HealthStatusServices | None = None,
    build_version: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Register the protected operational summary after auth is composed."""

    if timeout_seconds <= 0 or timeout_seconds > 5:
        raise ValueError("health summary timeout must be between 0 and 5 seconds")
    resolved_version = build_version or _installed_version()
    application.state.health_status_services = (
        services if services is not None else UnavailableHealthStatusServices()
    )
    application.state.health_summary_build_version = resolved_version
    application.state.health_summary_timeout_seconds = timeout_seconds

    async def summary_endpoint(
        request: Request,
        context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[HealthSummaryResponse]:
        del request
        scope = context.scope
        services = _services(application)
        timeout = application.state.health_summary_timeout_seconds
        authorization, authentication_mode, configuration_version = await _authorization_component(
            application.state.authorization_runtime,
            timeout_seconds=timeout,
        )
        summary = await collect_health_summary(
            services=services,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            authorization=authorization,
            authentication_mode=authentication_mode,
            configuration_version=configuration_version,
            build_version=application.state.health_summary_build_version,
            timeout_seconds=timeout,
        )
        authorization_response = _component_response(summary.authorization)
        publication_response = _component_response(summary.publication)
        ingestion_response = _component_response(summary.ingestion)
        data = HealthSummaryResponse(
            status=summary.status,
            build_version=summary.build_version,
            authentication_mode=summary.authentication_mode,
            configuration_version=summary.configuration_version,
            authorization=authorization_response,
            publication=publication_response,
            ingestion=ingestion_response,
            providers=HealthProvidersResponse(
                authentication=authorization_response.provider_name,
                publication=publication_response.provider_name,
                ingestion=ingestion_response.provider_name,
            ),
            last_operation=_operation_response(summary.last_operation),
        )
        return ApiResponseEnvelope(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            trace_id=context.diagnostic_context.trace_id,
            data=data,
        )

    add_protected_api_route(
        application,
        "/api/v1/health/summary",
        summary_endpoint,
        route_id=_HEALTH_SUMMARY_OPERATION,
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("health_summary"),
            operation=PersistenceOperation("inspect_health_summary"),
        ),
        methods=["GET"],
        response_model=ApiResponseEnvelope[HealthSummaryResponse],
    )


async def _authorization_component(
    runtime: object,
    *,
    timeout_seconds: float,
) -> tuple[
    HealthComponentSnapshot,
    Literal["local", "oidc"] | None,
    str | None,
]:
    try:
        readiness = await asyncio.wait_for(
            runtime.check_readiness(),  # type: ignore[attr-defined]
            timeout=timeout_seconds,
        )
        if type(readiness) is not ApiAuthorizationReadiness:
            raise TypeError("authorization readiness is not canonical")
        code = readiness.code.value
        status = _authorization_status(readiness)
        authentication_mode = _authentication_mode(readiness)
        configuration_version = _configuration_version(readiness)
        if code not in SAFE_HEALTH_CODES:
            code = "authorization.unavailable"
            status = HealthStatus.UNAVAILABLE
        return (
            HealthComponentSnapshot(
                status=status,
                code=code,
                provider_name=authentication_mode,
            ),
            authentication_mode,
            configuration_version,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return (
            HealthComponentSnapshot(
                status=HealthStatus.UNAVAILABLE,
                code="authorization.unavailable",
            ),
            None,
            None,
        )


def _component_response(snapshot: HealthComponentSnapshot) -> HealthComponentResponse:
    try:
        return HealthComponentResponse(
            status=snapshot.status,
            code=snapshot.code,
            provider_name=snapshot.provider_name,
        )
    except Exception:
        return HealthComponentResponse(
            status=HealthStatus.UNAVAILABLE,
            code="dependency.unavailable",
        )


def _operation_response(
    snapshot: HealthOperationSnapshot | None,
) -> HealthOperationResponse | None:
    if snapshot is None:
        return None
    try:
        return HealthOperationResponse(
            operation=snapshot.operation,
            status=snapshot.status,  # type: ignore[arg-type]
            completed_at=(
                snapshot.completed_at.isoformat().replace("+00:00", "Z")
                if snapshot.completed_at is not None
                else None
            ),
        )
    except Exception:
        return None


def _authorization_status(readiness: ApiAuthorizationReadiness) -> HealthStatus:
    if "stale" in readiness.code.value:
        return HealthStatus.STALE
    if not readiness.ready:
        return HealthStatus.UNAVAILABLE
    if readiness.code.value not in {
        "authorization.ready",
        "local.ready",
        "oidc.ready",
    }:
        return HealthStatus.DEGRADED
    return HealthStatus.READY


def _authentication_mode(value: object) -> Literal["local", "oidc"] | None:
    mode = getattr(value, "authentication_mode", None)
    return mode if mode in {"local", "oidc"} else None


def _configuration_version(value: object) -> str | None:
    candidate = getattr(value, "authentication_configuration_version", None)
    if not isinstance(candidate, str):
        return None
    return candidate if re.fullmatch(_SAFE_CONFIGURATION_VERSION, candidate) else None


def _services(application: FastAPI) -> HealthStatusServices:
    services = getattr(application.state, "health_status_services", None)
    if not all(
        callable(getattr(services, name, None))
        for name in ("publication_status", "ingestion_status", "last_operation")
    ):
        return UnavailableHealthStatusServices()
    return services


def _installed_version() -> str:
    try:
        return package_version("spine")
    except PackageNotFoundError:
        return "0.1.0"


__all__ = [
    "HealthCode",
    "HealthComponentResponse",
    "HealthOperationResponse",
    "HealthProviderName",
    "HealthReadinessCode",
    "HealthProvidersResponse",
    "HealthSummaryResponse",
    "LivenessResponse",
    "ReadinessResponse",
    "install_health_summary_route",
]
