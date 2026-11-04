from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from spine.api.provider_connections import install_provider_connection_routes
from spine.application.connections.management import (
    MailboxConnectionSyncState,
    MailboxConnectionSyncStatus,
    ProviderConnectionConfigurationResult,
    ProviderConnectionInspection,
    ProviderConnectionManager,
    ProviderConnectionNotFoundError,
    ProviderConnectionPage,
    build_provider_connection_audit,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionApproval,
    ProviderConnectionHealth,
    ProviderConnectionStatus,
    ProviderDiagnosticCode,
    ProviderKind,
)
from spine.domain.connections.email_scope import EmailProviderOperation
from spine.infrastructure.connections.fake_provider import FakeProviderConnectionObserver
from spine.infrastructure.connections.provider_management import (
    InMemoryProviderConnectionManagementRepository,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from tests.api.test_authorization import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    PERSISTENCE_ISSUER_ID,
    WORKSPACE_ID,
    authorized_app,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


NOW = datetime(2026, 10, 29, 17, tzinfo=timezone.utc)


def _inspection() -> ProviderConnectionInspection:
    return ProviderConnectionInspection(
        connection=ProviderConnection(
            connection_version_id=_id(1),
            connection_id=_id(2),
            approval_id=_id(3),
            version=1,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            service_principal_id=_id(4),
            provider_kind=ProviderKind.YANDEX_DISK,
            credential_reference=OpaqueProviderReference(value=_id(5)),
            grant_reference=OpaqueProviderReference(value=_id(6)),
            approved_resource_scope_references=(OpaqueProviderReference(value=_id(7)),),
            authorization_generation=2,
            configuration_generation=3,
            status=ProviderConnectionStatus.ACTIVE,
            capabilities=(
                ProviderCapability(
                    kind=ProviderCapabilityKind.BINARY_READ,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=OpaqueProviderReference(value=_id(8)),
                ),
                ProviderCapability(
                    kind=ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
                    status=ProviderCapabilityStatus.UNQUALIFIED,
                ),
                ProviderCapability(
                    kind=ProviderCapabilityKind.LIFECYCLE_EVIDENCE,
                    status=ProviderCapabilityStatus.UNSUPPORTED,
                ),
            ),
        ),
        health=ProviderConnectionHealth.HEALTHY,
        diagnostic_codes=(ProviderDiagnosticCode.CONNECTION_READY,),
        observed_at=NOW,
        cursor_lag_seconds=4,
        last_successful_reconciliation_at=NOW,
    )


def _mail_inspection(
    state: MailboxConnectionSyncState = MailboxConnectionSyncState.PARTIAL,
) -> ProviderConnectionInspection:
    base = _inspection()
    connection = base.connection.model_copy(
        update={
            "provider_kind": ProviderKind.YANDEX_MAIL,
            "permitted_operations": (
                EmailProviderOperation.ENUMERATE_MAILBOX,
                EmailProviderOperation.READ_MESSAGE_METADATA,
                EmailProviderOperation.SYNCHRONIZE,
            ),
            "capabilities": (
                ProviderCapability(
                    kind=ProviderCapabilityKind.MAILBOX_ENUMERATION,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=OpaqueProviderReference(value=_id(8)),
                ),
                ProviderCapability(
                    kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                    status=ProviderCapabilityStatus.UNQUALIFIED,
                ),
            ),
        }
    )
    return ProviderConnectionInspection(
        connection=connection,
        health=ProviderConnectionHealth.DEGRADED,
        diagnostic_codes=(ProviderDiagnosticCode.PROVIDER_DEGRADED,),
        observed_at=NOW,
        cursor_lag_seconds=75,
        mailbox_sync=MailboxConnectionSyncStatus(
            state=state,
            last_complete_at=NOW,
            lag_seconds=75,
        ),
    )


class _Service:
    def __init__(self) -> None:
        self.inspection = _inspection()
        self.configure_calls = []

    async def configure(self, request, context):
        self.configure_calls.append((request, context))
        return ProviderConnectionConfigurationResult(
            receipt_id=_id(9),
            inspection=self.inspection,
            audit=build_provider_connection_audit(
                request=request,
                connection=self.inspection.connection,
                context=context,
                receipt_id=_id(9),
            ),
            replay=False,
        )

    async def list(self, *, context, after_connection_id=None, limit=100):
        assert context.operation.value == "provider_connection_list"
        assert after_connection_id is None
        assert limit > 0
        return ProviderConnectionPage(items=(self.inspection,), has_more=False)

    async def inspect(self, *, connection_id, context):
        assert connection_id == _id(2)
        assert context.operation.value == "provider_connection_inspect"
        return self.inspection


def _headers(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(environment_id),
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def test_operator_configures_and_inspects_safe_connection_summary() -> None:
    service = _Service()
    application = authorized_app([])
    install_provider_connection_routes(application, service=service, cursor_secret=b"provider-connections-test-secret", clock=lambda: NOW)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/source-connections",
            headers=_headers(idempotency_key="configure-yandex-1"),
            json={
                "approval_id": str(_id(3)),
                "connection_id": str(_id(2)),
                "expected_version": 0,
                "expected_predecessor_version_id": None,
            },
        )
        listed = await client.get("/api/v1/source-connections", headers=_headers())
        inspected = await client.get(
            f"/api/v1/source-connections/{_id(2)}", headers=_headers()
        )

    assert created.status_code == 201
    assert created.json()["command_id"] == str(_id(9))
    assert created.json()["result"]["connection_id"] == str(_id(2))
    assert created.json()["result"]["health"] == "healthy"
    assert listed.status_code == 200
    assert listed.json()["items"][0] == inspected.json()["data"]
    assert inspected.json()["data"]["capabilities"] == [
        {
            "kind": "binary_read",
            "status": "supported",
            "qualification_reference": str(_id(8)),
        },
        {
            "kind": "incremental_change_discovery",
            "status": "unqualified",
            "qualification_reference": None,
        },
        {
            "kind": "lifecycle_evidence",
            "status": "unsupported",
            "qualification_reference": None,
        },
    ]
    safe_output = created.text + listed.text + inspected.text
    assert str(_id(5)) not in safe_output
    assert str(_id(6)) not in safe_output
    assert str(_id(4)) not in safe_output
    assert service.configure_calls[0][0].idempotency_key == "configure-yandex-1"
    assert service.configure_calls[0][1].operation.value == "provider_connection_configure"


async def test_mailbox_response_exposes_only_approved_operations_and_safe_sync_status() -> None:
    service = _Service()
    service.inspection = _mail_inspection()
    application = authorized_app([])
    install_provider_connection_routes(
        application,
        service=service,
        cursor_secret=b"provider-connections-test-secret",
        clock=lambda: NOW,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/source-connections/{_id(2)}", headers=_headers()
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["schema_version"] == 2
    assert data["provider_kind"] == "yandex_mail"
    assert data["permitted_operations"] == [
        "enumerate_mailbox",
        "read_message_metadata",
        "synchronize",
    ]
    assert data["capabilities"] == [
        {
            "kind": "mailbox_enumeration",
            "status": "supported",
            "qualification_reference": str(_id(8)),
        },
        {
            "kind": "raw_message_read",
            "status": "unqualified",
            "qualification_reference": None,
        },
    ]
    assert data["mailbox_sync"] == {
        "state": "partial",
        "last_complete_at": NOW.isoformat().replace("+00:00", "Z"),
        "lag_seconds": 75,
    }
    protected = (
        "subject: acquisition",
        "person@example.test",
        "INBOX/private",
        "proposal-secret.docx",
        "oauth-token-secret",
        "provider raw failure",
    )
    assert all(value not in response.text for value in protected)


@pytest.mark.parametrize(
    "state",
    (
        MailboxConnectionSyncState.PARTIAL,
        MailboxConnectionSyncState.STALE,
        MailboxConnectionSyncState.REVOKED,
        MailboxConnectionSyncState.RECONCILIATION_REQUIRED,
    ),
)
async def test_mailbox_sync_states_remain_distinct_and_content_free(
    state: MailboxConnectionSyncState,
) -> None:
    service = _Service()
    service.inspection = _mail_inspection(state)
    application = authorized_app([])
    install_provider_connection_routes(
        application,
        service=service,
        cursor_secret=b"provider-connections-test-secret",
        clock=lambda: NOW,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/source-connections/{_id(2)}", headers=_headers()
        )

    assert response.status_code == 200
    assert response.json()["data"]["mailbox_sync"]["state"] == state.value
    assert "provider raw failure" not in response.text


async def test_mailbox_configuration_rejects_client_supplied_authority() -> None:
    service = _Service()
    application = authorized_app([])
    install_provider_connection_routes(
        application,
        service=service,
        cursor_secret=b"provider-connections-test-secret",
        clock=lambda: NOW,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/source-connections",
            headers=_headers(idempotency_key="client-authority-forbidden"),
            json={
                "approval_id": str(_id(3)),
                "connection_id": str(_id(2)),
                "expected_version": 0,
                "expected_predecessor_version_id": None,
                "permitted_operations": ["read_raw_message"],
                "credential": "oauth-token-secret",
                "mailbox": "person@example.test",
            },
        )

    assert response.status_code == 422
    assert service.configure_calls == []
    assert "oauth-token-secret" not in response.text
    assert "person@example.test" not in response.text


async def test_connection_routes_deny_foreign_scope_before_service_execution() -> None:
    service = _Service()
    application = authorized_app([])
    install_provider_connection_routes(application, service=service, cursor_secret=b"provider-connections-test-secret", clock=lambda: NOW)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        denied = await client.post(
            "/api/v1/source-connections",
            headers=_headers(
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
                idempotency_key="denied-connection",
            ),
            json={
                "approval_id": str(_id(3)),
                "connection_id": str(_id(2)),
                "expected_version": 0,
                "expected_predecessor_version_id": None,
            },
        )

    assert denied.status_code == 403
    assert service.configure_calls == []
    assert str(_id(2)) not in denied.text


async def test_configuration_requires_idempotency_key_with_safe_error() -> None:
    service = _Service()
    application = authorized_app([])
    install_provider_connection_routes(application, service=service, cursor_secret=b"provider-connections-test-secret", clock=lambda: NOW)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/source-connections",
            headers=_headers(),
            json={
                "approval_id": str(_id(3)),
                "connection_id": str(_id(2)),
                "expected_version": 0,
                "expected_predecessor_version_id": None,
            },
        )

    assert response.status_code == 400
    assert response.json()["code"] == "spine.source_connections.invalid_request"
    assert service.configure_calls == []


async def test_missing_connection_is_disclosure_safe_not_found() -> None:
    class Missing(_Service):
        async def inspect(self, *, connection_id, context):
            del connection_id, context
            raise ProviderConnectionNotFoundError()

    application = authorized_app([])
    install_provider_connection_routes(application, service=Missing(), cursor_secret=b"provider-connections-test-secret", clock=lambda: NOW)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/source-connections/{_id(99)}", headers=_headers()
        )

    assert response.status_code == 404
    assert response.json()["code"] == "spine.source_connections.not_found"
    assert str(_id(99)) not in response.text


async def test_connection_list_uses_scope_bound_cursor_pagination() -> None:
    class Paged(_Service):
        def __init__(self) -> None:
            super().__init__()
            self.positions = []

        async def list(self, *, context, after_connection_id=None, limit=100):
            del context, limit
            self.positions.append(after_connection_id)
            return ProviderConnectionPage(
                items=(self.inspection,) if after_connection_id is None else (),
                has_more=after_connection_id is None,
            )

    service = Paged()
    application = authorized_app([])
    install_provider_connection_routes(
        application,
        service=service,
        cursor_secret=b"provider-connections-test-secret",
        clock=lambda: NOW,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        first = await client.get(
            "/api/v1/source-connections?limit=1", headers=_headers()
        )
        second = await client.get(
            "/api/v1/source-connections",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
            headers=_headers(),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert service.positions == [None, _id(2)]


async def test_http_flow_crosses_application_repository_and_provider_fake() -> None:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=PERSISTENCE_ISSUER_ID,
        secret=b"ticket-73-persistence-context-secret",
    )
    approval = ProviderConnectionApproval(
        approval_id=_id(30),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        service_principal_id=_id(31),
        provider_kind=ProviderKind.MOUNTED_FILESYSTEM,
        credential_reference=OpaqueProviderReference(value=_id(32)),
        grant_reference=OpaqueProviderReference(value=_id(33)),
        approved_resource_scope_references=(OpaqueProviderReference(value=_id(34)),),
        authorization_generation=1,
        configuration_generation=1,
        status=ProviderConnectionStatus.ACTIVE,
    )
    service = ProviderConnectionManager(
        repository=InMemoryProviderConnectionManagementRepository(
            context_verifier=boundary,
            approvals=(approval,),
        ),
        provider=FakeProviderConnectionObserver(clock=lambda: NOW),
    )
    application = authorized_app([])
    install_provider_connection_routes(
        application,
        service=service,
        cursor_secret=b"provider-connections-test-secret",
        clock=lambda: NOW,
    )
    body = {
        "approval_id": str(approval.approval_id),
        "connection_id": str(_id(35)),
        "expected_version": 0,
        "expected_predecessor_version_id": None,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/source-connections",
            headers=_headers(idempotency_key="api-provider-fake-contract"),
            json=body,
        )
        replay = await client.post(
            "/api/v1/source-connections",
            headers=_headers(idempotency_key="api-provider-fake-contract"),
            json=body,
        )
        idempotency_conflict = await client.post(
            "/api/v1/source-connections",
            headers=_headers(idempotency_key="api-provider-fake-contract"),
            json={**body, "connection_id": str(_id(36))},
        )
        optimistic_conflict = await client.post(
            "/api/v1/source-connections",
            headers=_headers(idempotency_key="api-provider-fake-stale-version"),
            json=body,
        )
        inspected = await client.get(
            f"/api/v1/source-connections/{_id(35)}", headers=_headers()
        )

    assert created.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["replay"] is True
    assert replay.json()["command_id"] == created.json()["command_id"]
    assert idempotency_conflict.status_code == 409
    assert idempotency_conflict.json()["code"] == (
        "spine.persistence.idempotency_conflict"
    )
    assert optimistic_conflict.status_code == 409
    assert optimistic_conflict.json()["code"] == "spine.persistence.optimistic_conflict"
    assert "connection_id" not in idempotency_conflict.text
    assert "connection_id" not in optimistic_conflict.text
    assert inspected.status_code == 200
    assert inspected.json()["data"]["health"] == "healthy"
    assert inspected.json()["data"]["capabilities"][0]["kind"] == "health_diagnostics"
