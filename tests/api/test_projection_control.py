from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx

from spine.api.projection_control import install_projection_control_routes
from spine.application.projection_control import (
    ProjectionAsOfSnapshot,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestResult,
    ProjectionSuccessorStatusSnapshot,
    ProjectionStatusSnapshot,
)
from spine.domain.projections import ProjectionHealth, ProjectionFreshness
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


SNAPSHOT_ID = UUID("50000000-0000-0000-0000-000000000001")
ACTIVATION_ID = UUID("50000000-0000-0000-0000-000000000002")
BOUNDARY_ID = UUID("50000000-0000-0000-0000-000000000003")
CONFIG_ID = UUID("50000000-0000-0000-0000-000000000004")
COMMAND_ID = UUID("50000000-0000-0000-0000-000000000005")
INTENT_ID = UUID("50000000-0000-0000-0000-000000000006")


class FakeProjectionControl:
    async def status(self, *, context, projection_kind: str) -> ProjectionStatusSnapshot:
        assert context.scope.workspace_id == WORKSPACE_ID
        assert context.scope.environment_id == ENVIRONMENT_ID
        assert projection_kind == "qa"
        return ProjectionStatusSnapshot(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="qa",
            publication_status="published",
            snapshot_id=SNAPSHOT_ID,
            activation_id=ACTIVATION_ID,
            canonical_boundary_id=BOUNDARY_ID,
            as_of=ProjectionAsOfSnapshot(
                canonical_boundary_id=BOUNDARY_ID,
                accepted_through_generation=12,
                boundary_time=datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
            ),
            activation_generation=4,
            projection_config_version_id=CONFIG_ID,
            projection_config_version=3,
            freshness=ProjectionFreshness.CURRENT,
            health=ProjectionHealth.HEALTHY,
            drift_count=0,
            retryable=False,
            recovery_state="not_requested",
        )

    async def request(
        self, *, context, projection_kind: str, action: ProjectionRecoveryAction, idempotency_key
    ) -> ProjectionRecoveryRequestResult:
        assert context.scope.workspace_id == WORKSPACE_ID
        assert context.scope.environment_id == ENVIRONMENT_ID
        assert projection_kind == "qa"
        assert idempotency_key.value == "recovery-1"
        return ProjectionRecoveryRequestResult(
            command_id=COMMAND_ID,
            projection_kind=projection_kind,
            action=action,
            replay=False,
        )

    async def successor_status(
        self, *, context, projection_kind: str, command_id: UUID
    ) -> ProjectionSuccessorStatusSnapshot:
        assert context.scope.workspace_id == WORKSPACE_ID
        assert context.scope.environment_id == ENVIRONMENT_ID
        assert projection_kind == "qa"
        assert command_id == COMMAND_ID
        return ProjectionSuccessorStatusSnapshot(
            command_id=command_id,
            intent_id=INTENT_ID,
            projection_kind=projection_kind,
            status="running",
            run_id=UUID(int=71),
            candidate_id=UUID(int=72),
            receipt_ids=(UUID(int=73),),
        )


async def test_projection_status_is_scoped_and_recovery_returns_typed_command_result() -> None:
    application = authorized_app([])
    control = FakeProjectionControl()
    install_projection_control_routes(
        application,
        status_service=control,
        recovery_service=control,
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        status = await client.get(
            "/api/v1/projections/qa/status",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )
        recovery = await client.post(
            "/api/v1/projections/qa/recovery",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
                "Idempotency-Key": "recovery-1",
            },
            json={"action": "rebuild"},
        )

    assert status.status_code == 200
    assert status.json()["data"] == {
        "schema_version": 1,
        "projection_kind": "qa",
        "publication_status": "published",
        "reason_code": None,
        "snapshot_id": str(SNAPSHOT_ID),
        "activation_id": str(ACTIVATION_ID),
        "canonical_boundary_id": str(BOUNDARY_ID),
        "as_of": {
            "canonical_boundary_id": str(BOUNDARY_ID),
            "accepted_through_generation": 12,
            "boundary_time": "2026-10-15T12:00:00Z",
        },
        "activation_generation": 4,
        "projection_config_version_id": str(CONFIG_ID),
        "projection_config_version": 3,
        "freshness": "current",
        "health": "healthy",
        "drift_count": 0,
        "reconciliation": None,
        "last_operation": None,
        "retryable": False,
        "recovery_state": "not_requested",
    }
    assert recovery.status_code == 202
    assert recovery.json()["result"] == {
        "schema_version": 1,
        "command_id": str(COMMAND_ID),
        "projection_kind": "qa",
        "action": "rebuild",
        "state": "accepted",
    }
    assert recovery.json()["status"] == "accepted"
    assert recovery.json()["replay"] is False


async def test_projection_control_accepts_typed_successor_action() -> None:
    application = authorized_app([])
    control = FakeProjectionControl()
    install_projection_control_routes(
        application,
        status_service=control,
        recovery_service=control,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/projections/qa/recovery",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
                "Idempotency-Key": "recovery-1",
            },
            json={"action": "successor"},
        )

    assert response.status_code == 202
    assert response.json()["result"]["action"] == "successor"


async def test_projection_successor_status_exposes_bounded_safe_correlation() -> None:
    application = authorized_app([])
    control = FakeProjectionControl()
    install_projection_control_routes(
        application,
        status_service=control,
        recovery_service=control,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/projections/qa/successors/{COMMAND_ID}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "schema_version": 1,
        "command_id": str(COMMAND_ID),
        "intent_id": str(INTENT_ID),
        "projection_kind": "qa",
        "status": "running",
        "run_id": str(UUID(int=71)),
        "candidate_id": str(UUID(int=72)),
        "receipt_ids": [str(UUID(int=73))],
        "snapshot_id": None,
        "activation_id": None,
    }


async def test_projection_control_rejects_foreign_scope_before_service_execution() -> None:
    application = authorized_app([])
    control = FakeProjectionControl()
    install_projection_control_routes(
        application,
        status_service=control,
        recovery_service=control,
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get(
            "/api/v1/projections/qa/status",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
            },
        )
        denied_successor = await client.post(
            "/api/v1/projections/qa/recovery",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
                "Idempotency-Key": "recovery-1",
            },
            json={"action": "successor"},
        )
        denied_diagnostics = await client.get(
            f"/api/v1/projections/qa/successors/{COMMAND_ID}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
            },
        )

    assert denied.status_code == 403
    assert "qa" not in denied.text
    assert str(WORKSPACE_ID) not in denied.text
    assert denied_successor.status_code == 403
    assert denied_diagnostics.status_code == 403
    assert str(COMMAND_ID) not in denied_diagnostics.text


async def test_projection_recovery_rejects_missing_idempotency_key_safely() -> None:
    application = authorized_app([])
    control = FakeProjectionControl()
    install_projection_control_routes(
        application,
        status_service=control,
        recovery_service=control,
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/projections/qa/recovery",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
            json={"action": "rebuild"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "spine.projection.invalid_request"
    assert "Idempotency" not in response.text


async def test_projection_recovery_is_unavailable_when_worker_service_is_not_composed() -> None:
    application = authorized_app([])
    install_projection_control_routes(
        application,
        status_service=FakeProjectionControl(),
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/projections/qa/recovery",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
                "Idempotency-Key": "recovery-1",
            },
            json={"action": "retry"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "spine.projection.unavailable"
    assert "worker" not in response.text.lower()
