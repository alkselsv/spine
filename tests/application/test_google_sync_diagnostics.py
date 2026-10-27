from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.google_sync_diagnostics import GoogleSyncDiagnosticSnapshot


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def test_google_sync_diagnostic_snapshot_contains_only_allowlisted_operational_data():
    run = SimpleNamespace(
        sync_run_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        connection_id=_id(4),
        authorization_generation=7,
        state="suspended",
        failure_code="authorization_revoked",
        proposed_cursor="private-provider-page-token",
    )
    page = SimpleNamespace(
        items=(SimpleNamespace(state="succeeded", identity="customer-row-value"),)
    )

    snapshot = GoogleSyncDiagnosticSnapshot.from_run(run, (page,))
    payload = snapshot.model_dump(mode="json")

    assert payload == {
        "schema_version": "google-sync-diagnostic:v1",
        "correlation_id": str(_id(1)),
        "workspace_id": str(_id(2)),
        "environment_id": str(_id(3)),
        "connection_id": str(_id(4)),
        "sync_run_id": str(_id(1)),
        "authorization_generation": 7,
        "state": "suspended",
        "failure_code": "authorization_revoked",
        "retryable": False,
        "provider_retry_attempts": 0,
        "provider_retry_limit": 3,
        "page_count": 1,
        "item_count": 1,
        "accepted_count": 1,
        "failed_count": 0,
        "quarantined_count": 0,
    }
    assert "private-provider-page-token" not in repr(snapshot)
    assert "customer-row-value" not in repr(snapshot)


def test_google_sync_diagnostic_snapshot_rejects_unapproved_provider_details():
    with pytest.raises(ValidationError, match="extra"):
        GoogleSyncDiagnosticSnapshot(
            correlation_id=_id(1),
            workspace_id=_id(2),
            environment_id=_id(3),
            connection_id=_id(4),
            sync_run_id=_id(5),
            authorization_generation=7,
            state="resumable",
            retryable=True,
            provider_retry_attempts=0,
            provider_retry_limit=3,
            page_count=0,
            item_count=0,
            accepted_count=0,
            failed_count=0,
            quarantined_count=0,
            provider_error="private URL https://drive.google.test/a.xlsx",
        )
