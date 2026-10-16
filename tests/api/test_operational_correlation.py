from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.operational_correlation import install_operational_correlation_routes
from spine.application.operational_correlation import (
    InMemoryOperationalCorrelationStore,
    OperationalCorrelationProjector,
    OperationalCorrelationReader,
)
from spine.domain.metrics import OperationalMetricEvent, OperationalMeasurement
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


TRACE_ID = UUID("50000000-0000-0000-0000-000000000366")
CORRELATION_ID = UUID("60000000-0000-0000-0000-000000000366")
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_operational_correlation_api_returns_versioned_safe_view() -> None:
    store = InMemoryOperationalCorrelationStore()
    await OperationalCorrelationProjector(store).observe_metric(
        OperationalMetricEvent(
            event_id=UUID(int=1),
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            trace_id=TRACE_ID,
            correlation_id=CORRELATION_ID,
            source_identity="o01.operational-observation:v1",
            measurement=OperationalMeasurement.RETRIEVAL_LATENCY,
            event_at=NOW,
            deduplication_key="api-correlation-event",
        )
    )
    application = authorized_app([])
    install_operational_correlation_routes(
        application,
        reader=OperationalCorrelationReader(store=store),
    )
    headers = {
        "Authorization": "Bearer synthetic-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/diagnostics/operational-correlation",
            params={
                "reference_kind": "correlation",
                "reference_id": str(CORRELATION_ID),
            },
            headers=headers,
        )
        denied = await client.get(
            "/api/v1/diagnostics/operational-correlation",
            params={
                "reference_kind": "correlation",
                "reference_id": str(CORRELATION_ID),
            },
            headers={**headers, "X-Workspace-ID": str(OTHER_WORKSPACE_ID)},
        )

    assert response.status_code == 200
    assert response.json()["data"]["schema_version"] == "operational-correlation-view:v1"
    assert response.json()["data"]["coverage"] == "incomplete"
    assert "question" not in response.text
    assert denied.status_code == 403
    assert "operational-correlation" not in denied.text
