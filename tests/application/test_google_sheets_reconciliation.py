from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.google_sheets_reconciliation import (
    ConnectorGoogleSheetsCanonicalIngestion,
    GoogleSheetsAdmittedPageReader,
    GoogleSheetsDriveWakeupAdapter,
    GoogleSheetsReconciliationCommand,
    GoogleSheetsReconciliationCoordinator,
    GoogleSheetsReconciliationPage,
    GoogleSheetsReconciliationScope,
    GoogleSheetsReconciliationFenceError,
)
from spine.application.connector_ingestion import ConnectorIngestionCommand
from spine.application.lifecycle import (
    LifecycleEligibilityQuery,
    connector_source_object_id,
)
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsIdentityDisposition,
    GoogleSheetsIdentityDispositionCommand,
    GoogleSheetsIdentityFailureCode,
    GoogleSheetsSyncRunState,
    InMemoryGoogleSheetsSyncRepository,
)
from spine.application.google_drive_changes import (
    DriveChangeCommand,
    DriveChangeCoordinator,
)
from spine.application.connections.google import (
    GoogleProviderAuthorizationError,
    GoogleProviderQuotaError,
)
from spine.application.persistence.drive_changes import (
    DriveChangeItemRecord,
    DriveChangeRunState,
    InMemoryDriveChangeRepository,
)
from spine.connectors.google_drive import DriveChange, DriveChangePage
from spine.connectors.google_sheets import (
    GoogleSheetsCellData,
    GoogleSheetsProviderAdapter,
    GoogleSheetsRangePage,
    GoogleSheetsReadProfile,
    GoogleSheetsReconciliationProviderAdapter,
    GoogleSheetsRowData,
    GoogleSheetsSnapshot,
)
from spine.connectors import ConnectorCoverage, NormalizedConnectorObservation
from spine.domain.sources.google import (
    GoogleRegisterRowKey,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    google_source_identity,
)
from spine.domain.connections.google import OpaqueGoogleReference

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
SCOPE_ID = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
POLICY = UUID("66666666-6666-4666-8666-666666666666")


def observation(
    business_id: str, *, version: str = "10"
) -> NormalizedConnectorObservation:
    return NormalizedConnectorObservation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE_ID,
        authorization_generation=3,
        configuration_generation=4,
        source_kind="record",
        external_identity=google_source_identity(
            GoogleRegisterRowKey(
                connection_id=CONNECTION,
                spreadsheet_id="estimates",
                sheet_id=7,
                immutable_business_id=business_id,
            )
        ),
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


def command(
    *,
    key: str = "wake-1",
    trigger: str = "drive_wakeup",
    proposed: str | None = "cursor-2",
):
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
        scopes=(
            GoogleSheetsReconciliationScope(
                spreadsheet_id="estimates",
                sheet_id=7,
                a1_range="A2:F500",
            ),
        ),
    )


class FakeSheetsReader:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def read_page(self, request, scope, page_token, context):
        del request, context
        self.calls.append((scope.identity, page_token))
        result = self.pages[(scope.identity, page_token)]
        if isinstance(result, Exception):
            raise result
        return result


class FakeGoogleAdmission:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def read(self, request, context):
        self.calls.append((request, context))
        return self.response


class FakeCanonicalIngestion:
    def __init__(self, *, fail_once_for: str | None = None):
        self.calls = []
        self.revisions = {}
        self.fail_once_for = fail_once_for

    async def ingest(
        self,
        observation,
        *,
        command_identity,
        idempotency_key,
        lifecycle_query,
    ):
        identity = observation.external_identity.external_object_id
        self.calls.append((identity, command_identity, idempotency_key, lifecycle_query))
        if self.fail_once_for == identity:
            self.fail_once_for = None
            raise RuntimeError("canonical persistence is unavailable")
        return self.revisions.setdefault(identity, UUID(int=101 + len(self.revisions)))


def coordinator(reader, ingestion, repository=None):
    return GoogleSheetsReconciliationCoordinator(
        repository=repository or InMemoryGoogleSheetsSyncRepository(),
        reader=reader,
        ingestion=ingestion,
        context_factory=lambda operation, trace_id: SimpleNamespace(
            operation=operation, trace_id=trace_id
        ),
        clock=lambda: NOW,
    )


def page(
    scope,
    token,
    next_token,
    *rows,
    version="10",
    complete=True,
    identity_rejection_codes=(),
):
    return GoogleSheetsReconciliationPage(
        scope_identity=scope.identity,
        page_token=token,
        next_page_token=next_token,
        provider_version_before=version,
        provider_version_after=version,
        complete=complete,
        observations=rows,
        identity_rejection_codes=identity_rejection_codes,
    )


@pytest.mark.asyncio
async def test_revoked_grant_suspends_full_scope_before_any_row_is_accepted():
    request = command()
    reader = FakeSheetsReader(
        {(request.scopes[0].identity, None): GoogleProviderAuthorizationError()}
    )
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.SUSPENDED
    assert result.accepted_rows == 0
    assert ingestion.calls == []
    assert result.diagnostic_snapshot.failure_code == "authorization_revoked"


@pytest.mark.asyncio
async def test_quota_retry_limit_requires_reconciliation_and_stops_provider_reads():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader({(scope.identity, None): GoogleProviderQuotaError()})
    sync = coordinator(reader, FakeCanonicalIngestion())

    for _ in range(3):
        result = await sync.run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.diagnostic_snapshot.provider_retry_attempts == 3
    calls_at_limit = len(reader.calls)

    replay = await sync.run(request)

    assert replay.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert len(reader.calls) == calls_at_limit


@pytest.mark.asyncio
async def test_reconcile_reads_every_configured_page_before_canonical_ingestion():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader(
        {
            (scope.identity, None): page(scope, None, "p2", observation("EST-1")),
            (scope.identity, "p2"): page(scope, "p2", None, observation("EST-2")),
        }
    )
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
    reader = FakeSheetsReader(
        {
            (scope.identity, None): page(scope, None, "p2", observation("EST-1")),
            (scope.identity, "p2"): page(
                scope, "p2", None, observation("EST-2"), complete=False
            ),
        }
    )
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.failure_code == "sheets_incomplete_page"
    assert result.committed_cursor == "cursor-1"
    assert result.accepted_rows == 0
    assert ingestion.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code",
    (
        "missing_business_id",
        "duplicate_business_id",
        "conflicting_business_id",
        "developer_metadata_mismatch",
    ),
)
async def test_invalid_identity_scope_is_durably_quarantined_without_acceptance(
    failure_code: str,
):
    request = command()
    scope = request.scopes[0]
    repo = InMemoryGoogleSheetsSyncRepository()
    reader = FakeSheetsReader(
        {
            (scope.identity, None): page(
                scope,
                None,
                None,
                observation("MUST-NOT-BE-ACCEPTED"),
                identity_rejection_codes=(failure_code,),
            )
        }
    )
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion, repo).run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.failure_code == "sheets_identity_quarantined"
    assert result.committed_cursor == "cursor-1"
    assert result.accepted_rows == 0
    assert ingestion.calls == []
    replay = await coordinator(reader, ingestion, repo).run(request)
    assert replay.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert replay.sync_run_id == result.sync_run_id
    events = await repo.list_identity_dispositions(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        scope_identities=(scope.identity,),
    )
    assert len(events) == 1
    assert events[0].disposition is GoogleSheetsIdentityDisposition.QUARANTINED
    assert events[0].failure_code is GoogleSheetsIdentityFailureCode(failure_code)
    assert "MUST-NOT-BE-ACCEPTED" not in events[0].model_dump_json()
    assert len(events) == 1
    other_workspace = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    await repo.record_identity_disposition(
        GoogleSheetsIdentityDispositionCommand(
            sync_run_id=UUID(int=9001),
            workspace_id=other_workspace,
            environment_id=ENVIRONMENT,
            connection_id=CONNECTION,
            scope_identity=scope.identity,
            disposition=GoogleSheetsIdentityDisposition.QUARANTINED,
            failure_code=GoogleSheetsIdentityFailureCode.MISSING_BUSINESS_ID,
            now=NOW,
        )
    )
    foreign_events = await repo.list_identity_dispositions(
        workspace_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        scope_identities=(scope.identity,),
    )
    assert len(foreign_events) == 1
    assert foreign_events[0].workspace_id == other_workspace
    assert (
        len(
            await repo.list_identity_dispositions(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                connection_id=CONNECTION,
                scope_identities=(scope.identity,),
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_corrected_identity_clears_quarantine_with_a_new_audit_event():
    request = command()
    scope = request.scopes[0]
    repo = InMemoryGoogleSheetsSyncRepository()
    ingestion = FakeCanonicalIngestion()
    quarantine_reader = FakeSheetsReader(
        {
            (scope.identity, None): page(
                scope,
                None,
                None,
                observation("EST-1"),
                identity_rejection_codes=("developer_metadata_mismatch",),
            )
        }
    )
    await coordinator(quarantine_reader, ingestion, repo).run(request)

    corrected_request = command(key="wake-2")
    corrected_scope = corrected_request.scopes[0]
    correction = await coordinator(
        FakeSheetsReader(
            {
                (corrected_scope.identity, None): page(
                    corrected_scope, None, None, observation("EST-1")
                )
            }
        ),
        ingestion,
        repo,
    ).run(corrected_request)

    assert correction.state is GoogleSheetsSyncRunState.SUCCEEDED
    events = await repo.list_identity_dispositions(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        scope_identities=(scope.identity,),
    )
    assert [event.disposition for event in events] == [
        GoogleSheetsIdentityDisposition.QUARANTINED,
        GoogleSheetsIdentityDisposition.CLEARED,
    ]
    assert events[0].event_id != events[1].event_id


@pytest.mark.asyncio
async def test_version_change_between_pages_prevents_canonical_acceptance():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader(
        {
            (scope.identity, None): page(
                scope, None, "p2", observation("EST-1"), version="10"
            ),
            (scope.identity, "p2"): page(
                scope, "p2", None, observation("EST-2", version="11"), version="11"
            ),
        }
    )
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
        FakeSheetsReader({(scope.identity, None): torn}),
        ingestion,
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
        spreadsheet_id="estimates",
        sheet_id=8,
        a1_range="A2:F200",
    )
    request = first_command.model_copy(update={"scopes": (first_scope, second_scope)})
    reader = FakeSheetsReader(
        {
            (first_scope.identity, None): page(
                first_scope, None, None, observation("EST-1")
            ),
            (second_scope.identity, None): page(
                second_scope,
                None,
                None,
                complete=False,
            ),
        }
    )
    ingestion = FakeCanonicalIngestion()

    result = await coordinator(reader, ingestion).run(request)

    assert result.state is GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED
    assert result.failure_code == "sheets_incomplete_page"
    assert reader.calls == [(first_scope.identity, None), (second_scope.identity, None)]
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_admitted_page_reader_preserves_range_and_provider_page_token():
    request = command()
    scope = request.scopes[0]
    normalized = observation("EST-1")
    expected = SimpleNamespace(
        spreadsheet_id="estimates",
        sheet_id=7,
        a1_range="A2:F500",
        page_token="p2",
        next_page_token=None,
        provider_version_before="10",
        provider_version_after="10",
        complete=True,
        identity_rejection_codes=(),
        observations=(SimpleNamespace(to_normalized_observation=lambda: normalized),),
    )
    admission = FakeGoogleAdmission(expected)
    context = SimpleNamespace(trace_id=UUID(int=1))

    result = await GoogleSheetsAdmittedPageReader(admission).read_page(
        request, scope, "p2", context
    )

    assert result.observations == (normalized,)
    admitted, admitted_context = admission.calls[0]
    assert admitted.connection_id == CONNECTION
    assert admitted.resource_scope_reference.value == SCOPE_ID
    assert admitted.resource_object_reference == scope.spreadsheet_id
    assert admitted.a1_range == "A2:F500"
    assert admitted.provider_page_token == "p2"
    assert admitted_context is context


@pytest.mark.asyncio
async def test_sparse_reconciliation_page_has_zero_observations_without_identity_failure():
    profile = GoogleSheetsReadProfile(
        spreadsheet_id="estimates",
        sheet_id=7,
        business_id_column="estimate_id",
        columns=({"column_key": "estimate_id", "coordinate": "A"},),
    )

    class SparsePageService:
        async def get_spreadsheet_range_page(
            self,
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        ):
            del connection, resource_scope_reference
            return GoogleSheetsRangePage(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                a1_range=a1_range,
                page_token=page_token,
                next_page_token="page-3",
                complete=False,
                provider_version_before="42",
                provider_version_after="42",
                snapshot=GoogleSheetsSnapshot(
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=sheet_id,
                    sheet_title="Estimates",
                    locale="ru_RU",
                    time_zone="Europe/Moscow",
                    rows=(),
                ),
            )

    result = await GoogleSheetsReconciliationProviderAdapter(
        service=SparsePageService(),
        profiles=(profile,),
        clock=lambda: NOW,
    ).read(
        SimpleNamespace(
            connection_id=CONNECTION,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
        ),
        SimpleNamespace(
            resource_scope_reference=OpaqueGoogleReference(value=SCOPE_ID),
            resource_object_reference="estimates",
            sheet_id=7,
            a1_range="A2:A100",
            provider_page_token="page-2",
        ),
    )

    assert result.observations == ()
    assert result.identity_rejection_codes == ()
    assert result.next_page_token == "page-3"


@pytest.mark.asyncio
async def test_drive_change_wakeup_resolves_all_approved_scopes_and_uses_same_coordinator():
    drive_command = DriveChangeCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE_ID,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=POLICY,
        command_identity="drive.changes",
        idempotency_key="changes-1",
        last_committed_cursor="cursor-1",
    )
    item = DriveChangeItemRecord(
        item_id=UUID(int=1),
        page_id=UUID(int=2),
        change_key="id:drive-change-9",
        file_id="estimates",
        input_ordinal=1,
        updated_at=NOW,
    )
    scope = GoogleSheetsReconciliationScope(
        spreadsheet_id="estimates",
        sheet_id=7,
        a1_range="A2:F500",
    )

    class ApprovedScopes:
        async def resolve_approved_scopes(self, request, spreadsheet_id):
            assert request is drive_command
            assert spreadsheet_id == "estimates"
            return (scope,)

    sheets_reader = FakeSheetsReader(
        {
            (scope.identity, None): page(scope, None, None, observation("EST-1")),
        }
    )
    ingestion = FakeCanonicalIngestion()
    service = coordinator(sheets_reader, ingestion)

    reconciliation_id = await GoogleSheetsDriveWakeupAdapter(
        service, ApprovedScopes()
    ).reconcile_change(drive_command, item)

    assert reconciliation_id is not None
    assert (
        service.repository.runs[reconciliation_id].state
        is GoogleSheetsSyncRunState.SUCCEEDED
    )
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_drive_wakeup_reconciles_sheets_before_committing_drive_cursor():
    drive_command = DriveChangeCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE_ID,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=POLICY,
        command_identity="drive.changes",
        idempotency_key="drive-run-1",
        last_committed_cursor="cursor-1",
    )
    scope = GoogleSheetsReconciliationScope(
        spreadsheet_id="estimates",
        sheet_id=7,
        a1_range="A2:F500",
    )

    class DriveReader:
        async def list_changes(self, request, token, context):
            del request, context
            assert token == "cursor-1"
            return DriveChangePage(
                page_token=token,
                new_start_page_token="cursor-2",
                changes=(DriveChange(file_id="estimates", provider_version="12"),),
            )

    class ApprovedScopes:
        async def resolve_approved_scopes(self, request, spreadsheet_id):
            assert spreadsheet_id == "estimates"
            return (scope,)

    row = observation("EST-1")
    sheets_reader = FakeSheetsReader(
        {
            (scope.identity, None): page(scope, None, None, row),
        }
    )
    canonical = FakeCanonicalIngestion()
    sheets_repository = InMemoryGoogleSheetsSyncRepository()
    sheets_coordinator = coordinator(sheets_reader, canonical, sheets_repository)
    drive_repository = InMemoryDriveChangeRepository()
    drive = DriveChangeCoordinator(
        repository=drive_repository,
        reader=DriveReader(),
        ingestion=canonical,
        sheets_wakeup=GoogleSheetsDriveWakeupAdapter(
            sheets_coordinator, ApprovedScopes()
        ),
        reconciliation=None,
        context_factory=lambda operation, trace_id: SimpleNamespace(
            operation=operation, trace_id=trace_id
        ),
        clock=lambda: NOW,
    )

    result = await drive.run(drive_command)

    assert result.state is DriveChangeRunState.SUCCEEDED
    assert result.committed_cursor == "cursor-2"
    assert result.pages[0].items[0].state.value == "reconciled"
    assert result.pages[0].items[0].outcome_reconciliation_id is not None
    assert canonical.calls


@pytest.mark.asyncio
async def test_drive_wakeup_uses_paginated_sheets_provider_and_canonical_handoff():
    drive_command = DriveChangeCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE_ID,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=POLICY,
        command_identity="drive.changes",
        idempotency_key="drive-run-paged-1",
        last_committed_cursor="cursor-1",
    )
    sheet_scope = GoogleSheetsReconciliationScope(
        spreadsheet_id="estimates",
        sheet_id=7,
        a1_range="A2:A100",
    )
    profile = GoogleSheetsReadProfile(
        spreadsheet_id="estimates",
        sheet_id=7,
        business_id_column="estimate_id",
        columns=({"column_key": "estimate_id", "coordinate": "A"},),
    )

    def snapshot(business_id: str, row_number: int) -> GoogleSheetsSnapshot:
        return GoogleSheetsSnapshot(
            spreadsheet_id="estimates",
            sheet_id=7,
            sheet_title="Estimates",
            locale="ru_RU",
            time_zone="Europe/Moscow",
            rows=(
                GoogleSheetsRowData(
                    row_number=row_number,
                    a1_range=f"A{row_number}:A{row_number}",
                    cells=(
                        GoogleSheetsCellData(
                            column_key="estimate_id",
                            coordinate=f"A{row_number}",
                            entered=GoogleSheetCellValue(
                                kind="string", value=business_id
                            ),
                            effective=GoogleSheetCellValue(
                                kind="string", value=business_id
                            ),
                            formatted=business_id,
                            number_format=GoogleSheetNumberFormat(type="TEXT"),
                        ),
                    ),
                ),
            ),
        )

    class PagedSheetsService:
        def __init__(self):
            self.calls = []

        async def get_spreadsheet_range_page(
            self,
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        ):
            del connection, resource_scope_reference
            self.calls.append(page_token)
            token, next_token, business_id, row_number = (
                (None, "page-2", "EST-1", 2)
                if page_token is None
                else ("page-2", None, "EST-2", 3)
            )
            return GoogleSheetsRangePage(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                a1_range=a1_range,
                page_token=token,
                next_page_token=next_token,
                complete=True,
                provider_version_before="42",
                provider_version_after="42",
                snapshot=snapshot(business_id, row_number),
            )

    provider = GoogleSheetsReconciliationProviderAdapter(
        service=PagedSheetsService(),
        profiles=(profile,),
        clock=lambda: NOW,
    )
    google_connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
    )

    class GoogleAdmission:
        async def read(self, request, context):
            del context
            return await provider.read(google_connection, request)

    class DriveReader:
        async def list_changes(self, request, token, context):
            del request, context
            return DriveChangePage(
                page_token=token,
                new_start_page_token="cursor-2",
                changes=(DriveChange(file_id="estimates", provider_version="42"),),
            )

    class ApprovedScopes:
        async def resolve_approved_scopes(self, request, spreadsheet_id):
            assert spreadsheet_id == "estimates"
            return (sheet_scope,)

    class CanonicalOrchestrator:
        def __init__(self):
            self.calls = []

        async def ingest_observation(self, request, row):
            self.calls.append((request, row))
            return SimpleNamespace(
                revision=SimpleNamespace(revision_id=UUID(int=500 + len(self.calls)))
            )

    canonical_orchestrator = CanonicalOrchestrator()
    sheets_repository = InMemoryGoogleSheetsSyncRepository()
    sheets_coordinator = GoogleSheetsReconciliationCoordinator(
        repository=sheets_repository,
        reader=GoogleSheetsAdmittedPageReader(GoogleAdmission()),
        ingestion=ConnectorGoogleSheetsCanonicalIngestion(
            orchestrator=canonical_orchestrator,
            base_command=ConnectorIngestionCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                command_identity="connector.sheets-row",
                idempotency_key="sheets-base",
                connection_id=CONNECTION,
                resource_scope_reference=SCOPE_ID,
                authorization_generation=3,
                configuration_generation=4,
                access_policy_version_id=POLICY,
            ),
        ),
        context_factory=lambda operation, trace_id: SimpleNamespace(
            operation=operation, trace_id=trace_id
        ),
        clock=lambda: NOW,
    )
    drive = DriveChangeCoordinator(
        repository=InMemoryDriveChangeRepository(),
        reader=DriveReader(),
        ingestion=canonical_orchestrator,
        sheets_wakeup=GoogleSheetsDriveWakeupAdapter(
            sheets_coordinator, ApprovedScopes()
        ),
        reconciliation=None,
        context_factory=lambda operation, trace_id: SimpleNamespace(
            operation=operation, trace_id=trace_id
        ),
        clock=lambda: NOW,
    )

    result = await drive.run(drive_command)

    assert result.state is DriveChangeRunState.SUCCEEDED
    assert result.committed_cursor == "cursor-2"
    assert result.pages[0].items[0].state.value == "reconciled"
    assert [
        row.external_identity.provider_key.immutable_business_id
        for _, row in canonical_orchestrator.calls
    ] == ["EST-1", "EST-2"]
    row_keys = [request.idempotency_key for request, _ in canonical_orchestrator.calls]
    assert len(set(row_keys)) == 2


@pytest.mark.asyncio
async def test_duplicate_wakeup_replays_one_durable_row_effect():
    request = command()
    scope = request.scopes[0]
    reader = FakeSheetsReader(
        {
            (scope.identity, None): page(scope, None, None, observation("EST-1")),
        }
    )
    ingestion = FakeCanonicalIngestion()
    service = coordinator(reader, ingestion)

    first = await service.run(request)
    replay = await service.run(request)

    assert first.state is replay.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert replay.accepted_rows == 1
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_replay_fences_changed_configured_range_set():
    original = command()
    scope = original.scopes[0]
    reader = FakeSheetsReader(
        {
            (scope.identity, None): page(scope, None, None, observation("EST-1")),
        }
    )
    service = coordinator(reader, FakeCanonicalIngestion())
    await service.run(original)
    changed = original.model_copy(
        update={
            "scopes": (
                GoogleSheetsReconciliationScope(
                    spreadsheet_id="estimates",
                    sheet_id=8,
                    a1_range="A2:F200",
                ),
            ),
        }
    )

    with pytest.raises(GoogleSheetsReconciliationFenceError):
        await service.run(changed)


@pytest.mark.asyncio
async def test_periodic_full_reconciliation_uses_same_bounded_coordinator_without_cursor():
    request = command(key="nightly-1", trigger="scheduled_full", proposed=None)
    scope = request.scopes[0]
    reader = FakeSheetsReader(
        {
            (scope.identity, None): page(scope, None, None, observation("EST-1")),
        }
    )
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


@pytest.mark.asyncio
async def test_google_sheets_row_ingestion_derives_child_lifecycle_query():
    row = observation("EST-1")
    root_source_id = UUID("77777777-7777-4777-8777-777777777777")
    root_query = LifecycleEligibilityQuery(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=root_source_id,
        connection_id=CONNECTION,
        connection_version_id=UUID("88888888-8888-4888-8888-888888888888"),
        scope_reference=SCOPE_ID,
        scope_version_id=UUID("99999999-9999-4999-8999-999999999999"),
        scope_digest="a" * 64,
        provider_object_reference="estimates",
    )

    class Orchestrator:
        async def ingest_observation(self, request, observation):
            self.request = request
            return SimpleNamespace(revision=SimpleNamespace(revision_id=UUID(int=91)))

    orchestrator = Orchestrator()
    ingestion = ConnectorGoogleSheetsCanonicalIngestion(
        orchestrator=orchestrator,
        base_command=ConnectorIngestionCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            command_identity="sheets.row",
            idempotency_key="sheets-row-1",
            connection_id=CONNECTION,
            resource_scope_reference=SCOPE_ID,
            authorization_generation=3,
            configuration_generation=4,
            access_policy_version_id=POLICY,
        ),
    )

    await ingestion.ingest(
        row,
        command_identity="sheets.row.1",
        idempotency_key="sheets-row-1",
        lifecycle_query=root_query,
    )

    query = orchestrator.request.lifecycle_query
    identity = row.external_identity
    assert query.source_object_id == connector_source_object_id(
        WORKSPACE,
        ENVIRONMENT,
        CONNECTION,
        identity.external_namespace,
        identity.external_generation,
        identity.external_object_id,
    )
    assert query.scope_authority_source_object_id == root_source_id
    assert query.provider_object_reference == identity.external_object_id
