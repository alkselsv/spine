from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from spine.api.operational_metrics import install_operational_metrics_routes
from spine.application.telemetry import InMemoryOperationalMetricsStore, OperationalMetricsReader
from spine.domain.metrics import MetricWindow, OperationalMeasurement
from tests.api.test_authorization import ENVIRONMENT_ID, OTHER_WORKSPACE_ID, WORKSPACE_ID, authorized_app


@pytest.mark.asyncio
async def test_operational_metrics_api_returns_structured_window_and_403_is_safe() -> None:
    now = datetime(2026, 10, 16, 15, tzinfo=timezone.utc)
    application = authorized_app([])
    install_operational_metrics_routes(
        application,
        reader=OperationalMetricsReader(
            store=InMemoryOperationalMetricsStore(),
            clock=lambda: now,
            refresh_interval=timedelta(minutes=5),
        ),
    )
    params = {
        "window_start": (now - timedelta(hours=1)).isoformat(),
        "window_end": now.isoformat(),
        "measurement": OperationalMeasurement.ANSWER_RATE.value,
    }
    headers = {
        "Authorization": "Bearer synthetic-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        allowed = await client.get("/api/v1/metrics/operational", params=params, headers=headers)
        denied = await client.get(
            "/api/v1/metrics/operational",
            params=params,
            headers={**headers, "X-Workspace-ID": str(OTHER_WORKSPACE_ID)},
        )

    assert allowed.status_code == 200
    assert allowed.json()["measurements"][0]["coverage"] == "unavailable"
    assert denied.status_code == 403
    assert "operational-metrics" not in denied.text
