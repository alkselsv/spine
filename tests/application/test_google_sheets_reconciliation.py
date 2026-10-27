from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.google_sheets_reconciliation import (
    GoogleSheetsReconciliationCommand,
    GoogleSheetsReconciliationCoordinator,
    GoogleSheetsReconciliationPage,
    GoogleSheetsReconciliationScope,
)
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsSyncRunState,
    InMemoryGoogleSheetsSyncRepository,
)
from spine.connectors import ConnectorCoverage, NormalizedConnectorObservation
from spine.domain.sources.google import GoogleRegisterRowKey, google_source_identity


NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
SCOPE_ID = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
POLICY = UUID("66666666-6666-4666-8666-666666666666")


def observation(business_id: str, *, version: str = "10") -> NormalizedConnectorObservation:
    return NormalizedConnectorObservation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE_ID,
        authorization_generation=3,
        configuration_generation=4,
        source_kind="record",
        external_identity=google_source_identity(GoogleRegisterRowKey(
            connection_id=CONNECTION, spreadsheet_id="estimates", sheet_id=7,
            immutable_business_id=business_id,
        )),
        original_bytes=business_id.encode(),
        media_type="application/vnd.spine.google-sheet-row+json",
        extraction_profile="google-sheets-row-extraction:v1",
        locator_contract_version="locator:google-sheet-cell:v1",
        coverage=ConnectorCoverage.COMPLETE,
        producer_reference="google-sheets-reader",
        event_identity=f"sheet.row:{sha256(business_id.encode()).hexdigest()[:32]}",
        event_digest=sha256(business_id.encode()).hexdigest(),
        origin_locator_kind="google-sheet-row",
        origin_locator_value=f"sheet-row:2:a1:a2:b2",
        origin_locator_schema="google-sheet-row-location:v1",
        origin_locator_digest=sha256(b"loc").hexdigest(),
        observer_service="google-sheets-reader",
        received_at=NOW,
        observed_at=NOW,
        provider_media_type="application/vnd.google-apps.spreadsheet",
        provider_version_before=version,
        provider_version_after=version,
    )


def command(*, key: str = "wake-1", trigger: str = "drive_wakeup", proposed: str | None = "cursor-2"):
    return GoogleSheetsReconciliationCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE_ID,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=POLICY,
        command_identity="sheets.reconcile",
        idempotency_key=key,
        prior_committed_cursor="cursor-1",
        proposed_cursor=proposed,
        trigger=trigger,
        scopes=(GoogleSheetsReconciliationScope(
            spreadsheet_id="estimates", sheet_id=7, a1_range="A2:F500",
        ),),
    )


class FakeSheetsReader:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def read_page(self, request, scope, page_token, context):
        del request, context
        self.calls.append((scope.identity, page_token))
        return self.pages[(scope.identity, page_token)]


class FakeCanonicalIngestion:
    def __init__(self, *, fail_once_for: str | None = None):
        self.calls = []
        self.revisions = {}
        self.fail_once_for = fail_once_for

    async def ingest(self, observation, *, command_identity, idempotency_key):
        identity = observation.external_identity.external_object_id
        self.calls.append((identity, command_identity, idempotency_key))
        if self.fail_once_for == identity:
            self.fail_once_for = None
            raise RuntimeError("canonical persistence is unavailable")
        return self.revisions.setdefault(identity, UUID(int=101 + len(self.revisions)))


def coordinator(reader, ingestion, repository=None):
    return GoogleSheetsReconciliationCoordinator(
        repository=repository or InMemoryGoogleSheetsSyncRepository(),
        reader=reader,
        ingestion=ingestion,
        context_factory=lambda operation, trace_id: SimpleNamespace(operation=operation, trace_id=trace_id),
        clock=lambda: NOW,
    )


def page(scope, token, next_token, *rows, version="10", complete=True):
    return GoogleSheetsReconciliationPage(
        scope_identity=scope.identity,
        page_token=token,
        next_page_token=next_token,
        provider_version_before=version,
        provider_version_after=version,
        complete=complete,
        observations=rows,
    )


@pytest.mark.asyncio
async def test_reconcile_reads_every_configured_page_before_canonical_ingestion():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader({
        (scope.identity, None): page(scope, None, "p2", observation("EST-1")),
        (scope.identity, "p2"): page(scope, "p2", None, observation("EST-2")),
    })
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert result.committed_cursor == "cursor-2"
    assert result.accepted_rows == 2
    assert reader.calls == [(scope.identity, None), (scope.identity, "p2")]
    assert len(ingestion.calls) == 2
    assert all(page.state.value == "completed" for page in result.pages)


@pytest.mark.asyncio
async def test_incomplete_late_page_holds_cursor_and_accepts_no_rows():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader({
        (scope.identity, None): page(scope, None, "p2", observation("EST-1")),
        (scope.identity, "p2"): page(scope, "p2", None, observation("EST-2"), complete=False),
    })
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.failure_code == "sheets_incomplete_page"
    assert result.committed_cursor == "cursor-1"
    assert result.accepted_rows == 0
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_version_change_between_pages_prevents_canonical_acceptance():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader({
        (scope.identity, None): page(scope, None, "p2", observation("EST-1"), version="10"),
        (scope.identity, "p2"): page(scope, "p2", None, observation("EST-2", version="11"), version="11"),
    })
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.failure_code == "sheets_version_changed"
    assert result.committed_cursor == "cursor-1"
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_torn_range_read_is_rejected_before_any_row_is_accepted():
    request = command()
    scope = request.scopes[0]
    torn = GoogleSheetsReconciliationPage(
        scope_identity=scope.identity,
        page_token=None,
        next_page_token=None,
        provider_version_before="10",
        provider_version_after="11",
        complete=True,
        observations=(observation("EST-1", version="10"),),
    )
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(
        FakeSheetsReader({(scope.identity, None): torn}), ingestion,
    ).run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.failure_code == "sheets_version_changed"
    assert result.committed_cursor == "cursor-1"
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_every_configured_range_is_read_before_the_first_row_is_accepted():
    first_command = command()
    first_scope = first_command.scopes[0]
    second_scope = GoogleSheetsReconciliationScope(
        spreadsheet_id="estimates", sheet_id=8, a1_range="A2:F200",
    )
    request = first_command.model_copy(update={"scopes": (first_scope, second_scope)})
    reader = FakeSheetsReader({
        (first_scope.identity, None): page(first_scope, None, None, observation("EST-1")),
        (second_scope.identity, None): page(
            second_scope, None, None, complete=False,
        ),
    })
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.failure_code == "sheets_incomplete_page"
    assert reader.calls == [(first_scope.identity, None), (second_scope.identity, None)]
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_duplicate_wakeup_replays_one_durable_row_effect():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader({
        (scope.identity, None): page(scope, None, None, observation("EST-1")),
    })
    ingestion = FakeCanonicalIngestion()
    service = coordinator(reader, ingestion)

    first = await service.run(request)
    replay = await service.run(request)

    assert first.state is replay.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert replay.accepted_rows == 1
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_periodic_full_reconciliation_uses_same_bounded_coordinator_without_cursor():
    request = command(key="nightly-1", trigger="scheduled_full", proposed=None)
    scope = request.scopes[0]
    reader = FakeSheetsReader({
        (scope.identity, None): page(scope, None, None, observation("EST-1")),
    })
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert result.committed_cursor == "cursor-1"
    assert result.accepted_rows == 1


@pytest.mark.asyncio
async def test_retry_skips_durable_row_outcome_after_later_canonical_failure():
    request = command()
    scope = request.scopes[0]
    rows = (observation("EST-1"), observation("EST-2"))
    reader = FakeSheetsReader({(scope.identity, None): page(scope, None, None, *rows)})
    ingestion = FakeCanonicalIngestion(
        fail_once_for=rows[1].external_identity.external_object_id,
    )
    repository = InMemoryGoogleSheetsSyncRepository()
    service = coordinator(reader, ingestion, repository)

    interrupted = await service.run(request)
    resumed = await service.run(request)

    assert interrupted.state is GoogleSheetsSyncRunState.RESUMABLE
    assert interrupted.committed_cursor == "cursor-1"
    assert resumed.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert resumed.committed_cursor == "cursor-2"
    assert [call[0] for call in ingestion.calls] == [
        rows[0].external_identity.external_object_id,
        rows[1].external_identity.external_object_id,
        rows[1].external_identity.external_object_id,
    ]
