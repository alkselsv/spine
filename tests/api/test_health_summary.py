from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.authorization import ApiAuthorizationReadiness
from spine.application.health_summary import (
    HealthComponentSnapshot,
    HealthOperationSnapshot,
    HealthStatus,
)
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    InMemoryAuthorizationDirectory,
    authorization_entry,
    authorized_app,
)


class FakeHealthStatusServices:
    async def publication_status(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthComponentSnapshot:
        assert (workspace_id, environment_id) == (WORKSPACE_ID, ENVIRONMENT_ID)
        return HealthComponentSnapshot(
            status=HealthStatus.READY,
            code="publication.ready",
            provider_name="postgresql",
        )

    async def ingestion_status(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthComponentSnapshot:
        assert (workspace_id, environment_id) == (WORKSPACE_ID, ENVIRONMENT_ID)
        return HealthComponentSnapshot(
            status=HealthStatus.DEGRADED,
            code="ingestion.partial",
            provider_name="postgresql",
        )

    async def last_operation(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthOperationSnapshot:
        assert (workspace_id, environment_id) == (WORKSPACE_ID, ENVIRONMENT_ID)
        return HealthOperationSnapshot(
            operation="ingestion",
            status="completed",
            completed_at=datetime(2026, 10, 15, 9, tzinfo=timezone.utc),
        )


async def test_administrator_health_summary_is_scoped_and_discloses_only_safe_metadata() -> None:
    application = authorized_app([])
    application.state.health_status_services = FakeHealthStatusServices()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/health/summary",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "workspace_id": str(WORKSPACE_ID),
        "environment_id": str(ENVIRONMENT_ID),
        "trace_id": "50000000-0000-0000-0000-000000000073",
        "data": {
            "status": "degraded",
            "build_version": "0.1.0",
            "authentication_mode": None,
            "configuration_version": None,
            "authorization": {
                "status": "ready",
                "code": "authorization.ready",
                "provider_name": None,
            },
            "publication": {
                "status": "ready",
                "code": "publication.ready",
                "provider_name": "postgresql",
            },
            "ingestion": {
                "status": "degraded",
                "code": "ingestion.partial",
                "provider_name": "postgresql",
            },
            "providers": {
                "authentication": None,
                "publication": "postgresql",
                "ingestion": "postgresql",
            },
            "last_operation": {
                "operation": "ingestion",
                "status": "completed",
                "completed_at": "2026-10-15T09:00:00Z",
            },
        },
    }
    assert "protected" not in response.text
    assert "provider-token" not in response.text


async def test_health_summary_requires_administrator_authentication_and_scope() -> None:
    application = authorized_app([])
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/health/summary",
            headers={
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )
        denied = await client.get(
            "/api/v1/health/summary",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 401
    assert response.json()["code"] == "spine.authentication.invalid"
    assert str(WORKSPACE_ID) not in response.text
    assert str(ENVIRONMENT_ID) not in response.text
    assert denied.status_code == 403
    assert denied.json()["code"] == "spine.authorization.denied"
    assert str(WORKSPACE_ID) not in denied.text
    assert str(ENVIRONMENT_ID) not in denied.text


async def test_public_readiness_timeout_returns_the_existing_safe_failure_shape() -> None:
    never = asyncio.Event()

    async def hanging_readiness() -> ApiAuthorizationReadiness:
        await never.wait()
        raise AssertionError("unreachable")

    application = authorized_app(
        [],
        readiness_probe=hanging_readiness,
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "service": "spine",
        "code": "authorization.unavailable",
    }
    readiness_operation = application.openapi()["paths"]["/api/v1/health/ready"]["get"]
    assert "503" in readiness_operation["responses"]


class FailingHealthStatusServices(FakeHealthStatusServices):
    async def publication_status(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthComponentSnapshot:
        del workspace_id, environment_id
        raise RuntimeError("provider-secret-and-protected-content")


async def test_health_summary_normalizes_dependency_failures_without_raw_details() -> None:
    application = authorized_app([])
    application.state.health_status_services = FailingHealthStatusServices()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/health/summary",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unavailable"
    assert response.json()["data"]["publication"] == {
        "status": "unavailable",
        "code": "dependency.unavailable",
        "provider_name": None,
    }
    assert "provider-secret-and-protected-content" not in response.text


def test_health_operation_rejects_unallowlisted_names() -> None:
    with pytest.raises(ValueError, match="operation name is not allowlisted"):
        HealthOperationSnapshot(operation="workspace-secret", status="completed")


async def test_health_summary_uses_safe_auth_configuration_metadata() -> None:
    async def readiness() -> ApiAuthorizationReadiness:
        return ApiAuthorizationReadiness(
            ready=True,
            code="oidc.ready",
            authentication_mode="oidc",
            authentication_configuration_version="oidc-2026-10-15",
        )

    application = authorized_app([], readiness_probe=readiness)
    application.state.health_status_services = FakeHealthStatusServices()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/health/summary",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["authentication_mode"] == "oidc"
    assert data["configuration_version"] == "oidc-2026-10-15"
    assert data["providers"]["authentication"] == "oidc"


def test_health_summary_openapi_is_protected_and_scoped() -> None:
    application = authorized_app([])
    operation = application.openapi()["paths"]["/api/v1/health/summary"]["get"]

    assert operation["operationId"] == "system.health.summary"
    assert operation["security"] == [{"BearerAuth": []}]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "X-Workspace-ID",
        "X-Environment-ID",
    }


async def test_health_summary_isolated_for_two_authorized_workspace_environments() -> None:
    calls: list[tuple[UUID, UUID]] = []
    operation_calls: list[tuple[UUID, UUID]] = []

    class IsolatedHealthStatusServices(FakeHealthStatusServices):
        async def publication_status(
            self, *, workspace_id: UUID, environment_id: UUID
        ) -> HealthComponentSnapshot:
            calls.append((workspace_id, environment_id))
            return HealthComponentSnapshot(
                status=HealthStatus.READY,
                code="publication.ready",
                provider_name="postgresql",
            )

        async def ingestion_status(
            self, *, workspace_id: UUID, environment_id: UUID
        ) -> HealthComponentSnapshot:
            calls.append((workspace_id, environment_id))
            return HealthComponentSnapshot(
                status=HealthStatus.READY,
                code="ingestion.ready",
                provider_name="postgresql",
            )

        async def last_operation(
            self, *, workspace_id: UUID, environment_id: UUID
        ) -> HealthOperationSnapshot:
            operation_calls.append((workspace_id, environment_id))
            return HealthOperationSnapshot(
                operation="ingestion",
                status="completed",
                completed_at=datetime(
                    2026,
                    10,
                    15,
                    9 if workspace_id == WORKSPACE_ID else 10,
                    tzinfo=timezone.utc,
                ),
            )

    second_scope_entry = authorization_entry().model_copy(
        update={
            "workspace_id": OTHER_WORKSPACE_ID,
            "environment_id": OTHER_ENVIRONMENT_ID,
            "environment_workspace_id": OTHER_WORKSPACE_ID,
        }
    )
    application = authorized_app(
        [],
        directory=InMemoryAuthorizationDirectory(
            entries=(authorization_entry(), second_scope_entry)
        ),
    )
    application.state.health_status_services = IsolatedHealthStatusServices()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/health/summary",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )
        second = await client.get(
            "/api/v1/health/summary",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
            },
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["workspace_id"] == str(WORKSPACE_ID)
    assert second.json()["workspace_id"] == str(OTHER_WORKSPACE_ID)
    assert first.json()["environment_id"] == str(ENVIRONMENT_ID)
    assert second.json()["environment_id"] == str(OTHER_ENVIRONMENT_ID)
    assert calls == [
        (WORKSPACE_ID, ENVIRONMENT_ID),
        (WORKSPACE_ID, ENVIRONMENT_ID),
        (OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
        (OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
    ]
    assert operation_calls == [
        (WORKSPACE_ID, ENVIRONMENT_ID),
        (OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
    ]
    assert first.json()["data"]["last_operation"]["completed_at"] == (
        "2026-10-15T09:00:00Z"
    )
    assert second.json()["data"]["last_operation"]["completed_at"] == (
        "2026-10-15T10:00:00Z"
    )
