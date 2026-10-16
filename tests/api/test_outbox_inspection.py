from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from spine.application.outbox_inspection import (
    OutboxDeliveryInspectionItem,
    OutboxDeliveryInspectionPage,
)
from spine.application.persistence.context import TrustedPersistenceContext
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


EVENT_ID = UUID("90000000-0000-0000-0000-000000000365")
CORRELATION_ID = UUID("91000000-0000-0000-0000-000000000365")
QUARANTINED_AT = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)


def inspection_item(event_id: UUID = EVENT_ID) -> OutboxDeliveryInspectionItem:
    return OutboxDeliveryInspectionItem(
        event_id=event_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        status="quarantined",
        attempt_count=3,
        created_at=datetime(2026, 10, 15, 8, tzinfo=timezone.utc),
        updated_at=QUARANTINED_AT,
        quarantined_at=QUARANTINED_AT,
        last_error_code="consumer.unavailable",
        correlation_id=CORRELATION_ID,
    )


class FakeInspectionService:
    def __init__(self, pages: tuple[OutboxDeliveryInspectionPage, ...]) -> None:
        self.pages = pages
        self.calls: list[tuple[TrustedPersistenceContext, int, object]] = []

    async def list_quarantined(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> OutboxDeliveryInspectionPage:
        self.calls.append((context, limit, after))
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


def headers(
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
) -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(environment_id),
    }


def inspection_app(service: FakeInspectionService):
    application = authorized_app([])
    application.state.outbox_delivery_inspection_service = service
    return application


async def test_quarantine_inspection_is_bounded_scoped_and_keyset_paginated() -> None:
    service = FakeInspectionService(
        (
            OutboxDeliveryInspectionPage(
                state="partial", items=(inspection_item(),), has_more=True
            ),
            OutboxDeliveryInspectionPage(
                state="complete",
                items=(inspection_item(UUID("90000000-0000-0000-0000-000000000366")),),
            ),
        )
    )
    application = inspection_app(service)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/admin/outbox/deliveries/quarantined",
            params={"limit": 1},
            headers=headers(),
        )
        second = await client.get(
            "/api/v1/admin/outbox/deliveries/quarantined",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers=headers(),
        )

    assert first.status_code == 200
    assert first.json()["schema_version"] == 1
    assert first.json()["workspace_id"] == str(WORKSPACE_ID)
    assert first.json()["environment_id"] == str(ENVIRONMENT_ID)
    assert first.json()["trace_id"] == "50000000-0000-0000-0000-000000000073"
    assert first.json()["authorization_audit_event_id"] == (
        "80000000-0000-0000-0000-000000000073"
    )
    assert first.json()["state"] == "partial"
    assert first.json()["has_more"] is True
    assert first.json()["items"][0]["event_id"] == str(EVENT_ID)
    assert first.json()["items"][0]["correlation_id"] == str(CORRELATION_ID)
    assert "payload" not in first.text
    assert second.status_code == 200
    assert second.json()["state"] == "complete"
    assert service.calls[0][0].scope.workspace_id == WORKSPACE_ID
    assert service.calls[0][0].scope.environment_id == ENVIRONMENT_ID
    assert service.calls[0][1] == 1
    assert service.calls[1][2] == (QUARANTINED_AT, EVENT_ID)


async def test_quarantine_inspection_distinguishes_empty_and_unavailable() -> None:
    for page, expected in (
        (OutboxDeliveryInspectionPage(state="empty"), "empty"),
        (OutboxDeliveryInspectionPage(state="unavailable"), "unavailable"),
    ):
        application = inspection_app(FakeInspectionService((page,)))
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/admin/outbox/deliveries/quarantined",
                headers=headers(),
            )
        assert response.status_code == 200
        assert response.json()["state"] == expected
        assert response.json()["items"] == []


async def test_quarantine_inspection_denies_missing_and_foreign_scope() -> None:
    service = FakeInspectionService((OutboxDeliveryInspectionPage(state="empty"),))
    application = inspection_app(service)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_auth = await client.get(
            "/api/v1/admin/outbox/deliveries/quarantined",
            headers={
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )
        denied_scope = await client.get(
            "/api/v1/admin/outbox/deliveries/quarantined",
            headers=headers(OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
        )

    assert missing_auth.status_code == 401
    assert denied_scope.status_code == 403
    assert WORKSPACE_ID.hex not in denied_scope.text.replace("-", "")
    assert service.calls == []


def test_inspection_item_rejects_unbounded_or_untrusted_error_codes() -> None:
    with pytest.raises(ValidationError):
        OutboxDeliveryInspectionItem.model_validate(
            inspection_item().model_dump() | {"last_error_code": "provider secret"}
        )


async def test_quarantine_inspection_cursor_rejects_another_workspace() -> None:
    service = FakeInspectionService(
        (OutboxDeliveryInspectionPage(state="partial", items=(inspection_item(),), has_more=True),)
    )
    application = inspection_app(service)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/v1/admin/outbox/deliveries/quarantined?limit=1",
            headers=headers(),
        )
        other_scope = await client.get(
            "/api/v1/admin/outbox/deliveries/quarantined",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers=headers(OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
        )

    assert first.status_code == 200
    assert other_scope.status_code == 403


async def test_quarantine_inspection_does_not_disclose_service_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingInspectionService:
        async def list_quarantined(self, **kwargs: object) -> OutboxDeliveryInspectionPage:
            del kwargs
            raise RuntimeError("protected customer payload and provider secret")

    application = inspection_app(FailingInspectionService())
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/admin/outbox/deliveries/quarantined",
            headers=headers(),
        )

    assert response.status_code == 200
    assert response.json()["state"] == "unavailable"
    assert "protected customer payload" not in response.text
    assert "provider secret" not in response.text
    assert "protected customer payload" not in caplog.text
    assert "provider secret" not in caplog.text
    diagnostic_events = application.state.diagnostic_sink.events
    diagnostic_text = " ".join(
        repr((event.model_dump(mode="json"), event.payload_json()))
        for event in diagnostic_events
    )
    assert "persistence.unavailable" in diagnostic_text
    assert "protected customer payload" not in diagnostic_text
    assert "provider secret" not in diagnostic_text
