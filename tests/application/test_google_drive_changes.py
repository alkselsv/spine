from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.google_drive_changes import (
    DriveChangeCommand,
    DriveChangeCoordinator,
    DriveReconciliationCommand,
)
from spine.application.persistence.drive_changes import (
    DriveChangeItemState,
    DriveChangeRunState,
    InMemoryDriveChangeRepository,
)
from spine.connectors.google_drive import (
    DriveChange,
    DriveChangePage,
    GoogleDriveAccessOutcomeError,
    GoogleDriveRetryExhaustedError,
    GoogleDriveTokenExpiredError,
)
from spine.domain.sources.google import GoogleDriveLifecycleSignal

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
SCOPE = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
POLICY = UUID("66666666-6666-4666-8666-666666666666")


def command() -> DriveChangeCommand:
    return DriveChangeCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=POLICY,
        command_identity="drive.changes",
        idempotency_key="changes-1",
        last_committed_cursor="cursor-7",
    )


class FakeReader:
    def __init__(self, pages: dict[str, DriveChangePage]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def list_changes(self, request, page_token, context):
        del request, context
        self.calls.append(page_token)
        result = self.pages[page_token]
        if isinstance(result, Exception):
            raise result
        return result


class FakeIngestion:
    def __init__(self, *, fail_file_id: str | None = None) -> None:
        self.fail_file_id = fail_file_id
        self.calls: list[str] = []
        self.keys: list[str] = []
        self.revisions: dict[str, UUID] = {}

    async def ingest(self, request):
        file_id = request.resource_object_reference
        assert file_id is not None
        self.calls.append(file_id)
        self.keys.append(request.idempotency_key)
        if file_id == self.fail_file_id:
            self.fail_file_id = None
            raise RuntimeError("canonical handoff failed")
        revision_id = self.revisions.setdefault(
            file_id, UUID(int=100 + len(self.revisions))
        )
        return SimpleNamespace(revision=SimpleNamespace(revision_id=revision_id))


class FakeReconciliation:
    def __init__(self) -> None:
        self.commands: list[DriveReconciliationCommand] = []

    async def initiate(self, request):
        self.commands.append(request)


class FakeSheetsWakeup:
    def __init__(self) -> None:
        self.calls = []

    async def reconcile_change(self, request, item):
        self.calls.append((request, item.file_id, item.change_key))
        return UUID(int=808)


def coordinator(
    reader, ingestion, repository=None, reconciliation=None, sheets_wakeup=None
):
    return DriveChangeCoordinator(
        repository=repository or InMemoryDriveChangeRepository(),
        reader=reader,
        ingestion=ingestion,
        reconciliation=reconciliation or FakeReconciliation(),
        sheets_wakeup=sheets_wakeup,
        context_factory=lambda operation, trace_id: SimpleNamespace(
            operation=operation, trace_id=trace_id
        ),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_spreadsheet_change_waits_for_full_sheets_reconciliation_before_cursor_commit():
    reader = FakeReader(
        {
            "cursor-7": DriveChangePage(
                page_token="cursor-7",
                new_start_page_token="cursor-8",
                changes=(DriveChange(file_id="spreadsheet-1", provider_version="8"),),
            )
        }
    )
    ingestion = FakeIngestion()
    wakeup = FakeSheetsWakeup()

    result = await coordinator(reader, ingestion, sheets_wakeup=wakeup).run(command())

    assert result.state is DriveChangeRunState.SUCCEEDED
    assert result.committed_cursor == "cursor-8"
    assert ingestion.calls == []
    assert wakeup.calls[0][1] == "spreadsheet-1"
    item = result.pages[0].items[0]
    assert item.state is DriveChangeItemState.RECONCILED
    assert item.outcome_reconciliation_id == UUID(int=808)


@pytest.mark.asyncio
async def test_incremental_changes_drains_every_page_before_committing_terminal_cursor():
    reader = FakeReader(
        {
            "cursor-7": DriveChangePage(
                page_token="cursor-7",
                next_page_token="cursor-8",
                changes=(DriveChange(file_id="file-a", provider_version="8"),),
            ),
            "cursor-8": DriveChangePage(
                page_token="cursor-8",
                new_start_page_token="cursor-9",
                changes=(DriveChange(file_id="file-b", provider_version="9"),),
            ),
        }
    )
    ingestion = FakeIngestion()
    reconciliation = FakeReconciliation()

    result = await coordinator(reader, ingestion, reconciliation=reconciliation).run(
        command()
    )

    assert result.state is DriveChangeRunState.SUCCEEDED
    assert result.committed_cursor == "cursor-9"
    assert reader.calls == ["cursor-7", "cursor-8"]
    assert ingestion.calls == ["file-a", "file-b"]
    assert reconciliation.commands == []


@pytest.mark.asyncio
async def test_duplicate_delivery_replays_idempotently_and_removed_change_is_terminal():
    reader = FakeReader(
        {
            "cursor-7": DriveChangePage(
                page_token="cursor-7",
                new_start_page_token="cursor-8",
                changes=(
                    DriveChange(file_id="file-a", provider_version="8"),
                    DriveChange(file_id="file-a", provider_version="8"),
                    DriveChange(file_id="file-deleted", removed=True),
                ),
            )
        }
    )
    ingestion = FakeIngestion()
    result = await coordinator(reader, ingestion).run(command())

    assert result.state is DriveChangeRunState.SUCCEEDED
    assert ingestion.calls == ["file-a", "file-a"]
    assert ingestion.keys[0] == ingestion.keys[1]
    assert [item.state for item in result.pages[0].items] == [
        DriveChangeItemState.SUCCEEDED,
        DriveChangeItemState.SUCCEEDED,
        DriveChangeItemState.IGNORED,
    ]
    assert result.pages[0].items[-1].signal is GoogleDriveLifecycleSignal.REMOVED


@pytest.mark.parametrize(
    ("signal", "reason"),
    [
        (GoogleDriveLifecycleSignal.HTTP_401, "provider_http_401"),
        (GoogleDriveLifecycleSignal.HTTP_403, "provider_http_403"),
        (GoogleDriveLifecycleSignal.HTTP_404, "provider_http_404"),
    ],
)
@pytest.mark.asyncio
async def test_drive_access_outcomes_keep_cursor_and_enter_reconciliation(signal, reason):
    reader = FakeReader({"cursor-7": GoogleDriveAccessOutcomeError(signal)})
    reconciliation = FakeReconciliation()

    result = await coordinator(
        reader, FakeIngestion(), reconciliation=reconciliation
    ).run(command())

    assert result.state is DriveChangeRunState.RECONCILIATION_REQUIRED
    assert result.committed_cursor == "cursor-7"
    assert result.run.reconciliation_reason == reason
    assert reconciliation.commands[0].reason == reason


@pytest.mark.asyncio
async def test_crash_after_canonical_effect_replays_stable_change_without_cursor_advance():
    page = DriveChangePage(
        page_token="cursor-7",
        new_start_page_token="cursor-8",
        changes=(DriveChange(file_id="file-a", provider_version="8"),),
    )
    repository = InMemoryDriveChangeRepository(fail_next_item_checkpoint=True)
    reader = FakeReader({"cursor-7": page})
    ingestion = FakeIngestion()
    service = coordinator(reader, ingestion, repository)

    interrupted = await service.run(command())
    resumed = await service.run(command())

    assert interrupted.state is DriveChangeRunState.RESUMABLE
    assert interrupted.committed_cursor == "cursor-7"
    assert resumed.state is DriveChangeRunState.SUCCEEDED
    assert resumed.committed_cursor == "cursor-8"
    assert ingestion.calls == ["file-a", "file-a"]


@pytest.mark.asyncio
async def test_token_expiry_keeps_cursor_and_starts_bounded_reconciliation():
    reader = FakeReader({"cursor-7": GoogleDriveTokenExpiredError("expired")})
    reconciliation = FakeReconciliation()

    result = await coordinator(
        reader, FakeIngestion(), reconciliation=reconciliation
    ).run(command())

    assert result.state is DriveChangeRunState.RECONCILIATION_REQUIRED
    assert result.committed_cursor == "cursor-7"
    assert len(reconciliation.commands) == 1
    assert reconciliation.commands[0].reason == "provider_token_expired"


@pytest.mark.asyncio
async def test_retry_exhaustion_keeps_cursor_and_classifies_reconciliation():
    reader = FakeReader({"cursor-7": GoogleDriveRetryExhaustedError("retry")})
    reconciliation = FakeReconciliation()

    result = await coordinator(
        reader, FakeIngestion(), reconciliation=reconciliation
    ).run(command())

    assert result.state is DriveChangeRunState.RECONCILIATION_REQUIRED
    assert result.committed_cursor == "cursor-7"
    assert reconciliation.commands[0].reason == "provider_retry_exhausted"


@pytest.mark.asyncio
async def test_missing_terminal_token_retains_cursor_and_reconciles_after_page_effects():
    reader = FakeReader(
        {
            "cursor-7": DriveChangePage(
                page_token="cursor-7",
                changes=(DriveChange(file_id="file-a", provider_version="8"),),
            )
        }
    )
    reconciliation = FakeReconciliation()

    result = await coordinator(
        reader, FakeIngestion(), reconciliation=reconciliation
    ).run(command())

    assert result.state is DriveChangeRunState.RECONCILIATION_REQUIRED
    assert result.committed_cursor == "cursor-7"
    assert result.pages[0].state.value == "completed"
    assert reconciliation.commands[0].reason == "missing_terminal_cursor"


@pytest.mark.asyncio
async def test_incomplete_page_reconciles_without_advancing_cursor():
    reader = FakeReader(
        {
            "cursor-7": DriveChangePage(
                page_token="cursor-7",
                new_start_page_token="cursor-8",
                incomplete_search=True,
                changes=(DriveChange(file_id="file-a", provider_version="8"),),
            )
        }
    )
    reconciliation = FakeReconciliation()

    result = await coordinator(
        reader, FakeIngestion(), reconciliation=reconciliation
    ).run(command())

    assert result.state is DriveChangeRunState.RECONCILIATION_REQUIRED
    assert result.committed_cursor == "cursor-7"
    assert result.pages[0].state.value == "incomplete"
    assert reconciliation.commands[0].reason == "incomplete_change_page"


@pytest.mark.asyncio
async def test_generation_change_fences_resumed_run_before_provider_call():
    reader = FakeReader(
        {
            "cursor-7": DriveChangePage(
                page_token="cursor-7",
                new_start_page_token="cursor-8",
                changes=(),
            )
        }
    )
    repository = InMemoryDriveChangeRepository()
    service = coordinator(reader, FakeIngestion(), repository)
    await service.run(command())

    with pytest.raises(Exception, match="fenced"):
        await service.run(command().model_copy(update={"authorization_generation": 5}))
