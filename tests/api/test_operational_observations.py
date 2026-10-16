from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.operational_observations import install_operational_observation_routes
from spine.application.diagnostics import DiagnosticContext
from spine.application.telemetry import (
    InMemoryOperationalObservationStore,
    OperationalComponent,
    OperationalObservationOutcome,
    OperationalObservationReader,
    OperationalObservationRegistry,
    OperationalOperation,
    OperationalStage,
    SafeOperationalFailureCode,
)
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    TRACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


NOW = datetime(2026, 10, 16, 15, tzinfo=timezone.utc)


def headers(workspace_id: UUID = WORKSPACE_ID) -> dict[str, str]:
    return {
        "Authorization": "Bearer synthetic-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_operational_reads_are_administrator_and_scope_bound() -> None:
    registry = OperationalObservationRegistry()
    store = InMemoryOperationalObservationStore(registry)
    await store.emit(
        registry.build(
            observation_id=UUID(int=361),
            observed_at=NOW,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            diagnostic_context=DiagnosticContext(trace_id=TRACE_ID),
            component=OperationalComponent.RETRIEVAL,
            stage=OperationalStage.RETRIEVAL,
            operation=OperationalOperation.CONTEXT_RETRIEVAL,
            outcome=OperationalObservationOutcome.FAILED,
            safe_failure_code=SafeOperationalFailureCode.RETRIEVAL_UNAVAILABLE,
        )
    )
    application = authorized_app([])
    install_operational_observation_routes(
        application,
        reader=OperationalObservationReader(store=store, clock=lambda: NOW),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        allowed = await client.get(
            "/api/v1/operational-observations",
            headers=headers(),
        )
        denied = await client.get(
            "/api/v1/operational-observations",
            headers=headers(OTHER_WORKSPACE_ID),
        )

    assert allowed.status_code == 200
    assert allowed.json()["observations"][0]["trace_id"] == str(TRACE_ID)
    assert allowed.json()["observations"][0]["safe_failure_code"] == "retrieval_unavailable"
    assert denied.status_code == 403
    assert "retrieval_unavailable" not in denied.text
    assert str(UUID(int=361)) not in denied.text
