from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.google_drive_snapshot import (
    DriveInitialSnapshotCommand,
    DriveInitialSnapshotCoordinator,
)
from spine.application.lifecycle import LifecycleEligibilityQuery
from spine.application.connections.google import (
    GoogleProviderAuthorizationError,
    GoogleProviderQuotaError,
)
from spine.application.persistence.drive_sync import (
    DriveSnapshotItemState,
    DriveSnapshotRunState,
    InMemoryDriveSyncRepository,
)
from spine.connectors.google_drive import DriveSnapshotItem, DriveSnapshotPage
from spine.domain.sources.google import GoogleDriveFileKey, google_source_identity


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
SCOPE = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
POLICY = UUID("66666666-6666-4666-8666-666666666666")


def command() -> DriveInitialSnapshotCommand:
    return DriveInitialSnapshotCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=POLICY,
        command_identity="drive.initial-snapshot",
        idempotency_key="snapshot-1",
    )


def item(file_id: str) -> DriveSnapshotItem:
    return DriveSnapshotItem(file_id=file_id)


class FakeReader:
    def __init__(self, pages: dict[str | None, DriveSnapshotPage], *, start_error=None) -> None:
        self.pages = pages
        self.start_error = start_error
        self.calls: list[tuple[str, str | None]] = []
        self.start_token_calls = 0

    async def get_start_page_token(self, request, context):
        del context
        self.start_token_calls += 1
        self.calls.append(("start", None))
        if self.start_error is not None:
            raise self.start_error
        return "start-token-7"

    async def list_page(self, request, page_token, context):
        del context
        self.calls.append(("list", page_token))
        result = self.pages[page_token]
        if isinstance(result, Exception):
            raise result
        return result


class FakeIngestion:
    def __init__(self, *, fail_file_id: str | None = None) -> None:
        self.fail_file_id = fail_file_id
        self.calls: list[str] = []
        self.requests = []
        self.revisions: dict[str, UUID] = {}

    async def ingest(self, request):
        file_id = request.resource_object_reference
        assert file_id is not None
        self.calls.append(file_id)
        self.requests.append(request)
        if file_id == self.fail_file_id:
            self.fail_file_id = None
            raise RuntimeError("provider detail must not escape")
        revision_id = self.revisions.setdefault(file_id, UUID(int=100 + len(self.revisions)))
        return SimpleNamespace(revision=SimpleNamespace(revision_id=revision_id))


class FailOnceBeforeItemCheckpoint(InMemoryDriveSyncRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def mark_item_succeeded(self, item_id, *, revision_id, now):
        if not self.failed:
            self.failed = True
            raise RuntimeError("checkpoint acknowledgement lost")
        return await super().mark_item_succeeded(item_id, revision_id=revision_id, now=now)


def coordinator(reader, ingestion, repository=None):
    return DriveInitialSnapshotCoordinator(
        repository=repository or InMemoryDriveSyncRepository(),
        reader=reader,
        ingestion=ingestion,
        context_factory=lambda operation, trace_id: SimpleNamespace(
            operation=operation, trace_id=trace_id
        ),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_initial_snapshot_commits_proposed_start_token_after_all_pages_are_durable():
    reader = FakeReader(
        {
            None: DriveSnapshotPage(
                page_token=None,
                next_page_token="page-2",
                incomplete_search=False,
                items=(item("file-a"), item("file-b")),
            ),
            "page-2": DriveSnapshotPage(
                page_token="page-2",
                next_page_token=None,
                incomplete_search=False,
                items=(item("file-c"),),
            ),
        }
    )
    ingestion = FakeIngestion()
    repository = InMemoryDriveSyncRepository()

    result = await coordinator(reader, ingestion, repository).run(command())

    assert result.state is DriveSnapshotRunState.SUCCEEDED
    assert result.proposed_start_page_token == "start-token-7"
    assert result.committed_cursor == "start-token-7"
    assert ingestion.calls == ["file-a", "file-b", "file-c"]
    assert reader.calls == [("start", None), ("list", None), ("list", "page-2")]
    assert reader.start_token_calls == 1
    assert all(
        item.state is DriveSnapshotItemState.SUCCEEDED
        for page in result.pages
        for item in page.items
    )


@pytest.mark.asyncio
async def test_interrupted_page_resumes_without_repeating_durable_items():
    page = DriveSnapshotPage(
        page_token=None,
        next_page_token=None,
        incomplete_search=False,
        items=(item("file-a"), item("file-b")),
    )
    reader = FakeReader({None: page})
    ingestion = FakeIngestion(fail_file_id="file-b")
    repository = InMemoryDriveSyncRepository()
    service = coordinator(reader, ingestion, repository)

    interrupted = await service.run(command())
    resumed = await service.run(command())

    assert interrupted.state is DriveSnapshotRunState.RESUMABLE
    assert interrupted.committed_cursor is None
    assert resumed.state is DriveSnapshotRunState.SUCCEEDED
    assert ingestion.calls == ["file-a", "file-b", "file-b"]
    assert resumed.committed_cursor == "start-token-7"


@pytest.mark.asyncio
async def test_incomplete_search_requires_reconciliation_and_keeps_last_cursor():
    reader = FakeReader(
        {
            None: DriveSnapshotPage(
                page_token=None,
                next_page_token=None,
                incomplete_search=True,
                items=(item("file-a"),),
            )
        }
    )
    repository = InMemoryDriveSyncRepository()

    result = await coordinator(reader, FakeIngestion(), repository).run(
        command().model_copy(update={"last_committed_cursor": "previous-token"})
    )

    assert result.state is DriveSnapshotRunState.RECONCILIATION_REQUIRED
    assert result.committed_cursor == "previous-token"
    assert result.pages[0].incomplete_search is True
    assert result.pages[0].state.value == "incomplete"


@pytest.mark.asyncio
async def test_cancellation_keeps_snapshot_cursor_and_resume_commits_once():
    class CancelOnceReader(FakeReader):
        def __init__(self):
            super().__init__(
                {
                    None: DriveSnapshotPage(
                        page_token=None,
                        next_page_token=None,
                        incomplete_search=False,
                        items=(item("shared-file"),),
                    )
                }
            )
            self.cancel_once = True

        async def list_page(self, request, page_token, context):
            if self.cancel_once:
                self.cancel_once = False
                raise asyncio.CancelledError
            return await super().list_page(request, page_token, context)

    reader = CancelOnceReader()
    repository = InMemoryDriveSyncRepository()
    ingestion = FakeIngestion()
    service = coordinator(reader, ingestion, repository)

    with pytest.raises(asyncio.CancelledError):
        await service.run(command())

    sync_run_id = uuid5(
        NAMESPACE_URL,
        "spine:drive-initial-snapshot:v1:"
        f"{WORKSPACE}:{ENVIRONMENT}:{CONNECTION}:{SCOPE}:snapshot-1",
    )
    interrupted = await repository.resolve_run(sync_run_id)
    assert interrupted is not None
    assert interrupted.proposed_start_page_token == "start-token-7"
    assert interrupted.committed_cursor is None
    assert await repository.resolve_page(sync_run_id, None) is None

    resumed = await service.run(command())

    assert resumed.state is DriveSnapshotRunState.SUCCEEDED
    assert resumed.committed_cursor == "start-token-7"
    assert ingestion.calls == ["shared-file"]


@pytest.mark.asyncio
async def test_revoked_grant_suspends_snapshot_before_starting_source_ingestion():
    reader = FakeReader({}, start_error=GoogleProviderAuthorizationError())
    ingestion = FakeIngestion()

    result = await coordinator(reader, ingestion).run(command())

    assert result.state is DriveSnapshotRunState.SUSPENDED
    assert result.committed_cursor is None
    assert ingestion.calls == []
    assert result.diagnostic_snapshot.failure_code == "authorization_revoked"


@pytest.mark.asyncio
async def test_quota_retry_limit_requires_reconciliation_and_stops_provider_reads():
    reader = FakeReader({}, start_error=GoogleProviderQuotaError())
    sync = coordinator(reader, FakeIngestion())

    for _ in range(3):
        result = await sync.run(command())

    assert result.state is DriveSnapshotRunState.RECONCILIATION_REQUIRED
    assert result.diagnostic_snapshot.provider_retry_attempts == 3
    calls_at_limit = reader.start_token_calls

    replay = await sync.run(command())

    assert replay.state is DriveSnapshotRunState.RECONCILIATION_REQUIRED
    assert reader.start_token_calls == calls_at_limit


@pytest.mark.asyncio
async def test_replay_after_canonical_effect_before_item_checkpoint_reuses_stable_item_command():
    reader = FakeReader(
        {
            None: DriveSnapshotPage(
                page_token=None,
                next_page_token=None,
                incomplete_search=False,
                items=(item("file-a"),),
            )
        }
    )
    repository = FailOnceBeforeItemCheckpoint()
    ingestion = FakeIngestion()
    service = coordinator(reader, ingestion, repository)

    interrupted = await service.run(command())
    resumed = await service.run(command())

    assert interrupted.state is DriveSnapshotRunState.RESUMABLE
    assert resumed.state is DriveSnapshotRunState.SUCCEEDED
    assert ingestion.calls == ["file-a", "file-a"]
    assert resumed.pages[0].items[0].outcome_revision_id == UUID(int=100)


@pytest.mark.asyncio
async def test_snapshot_derives_each_file_fence_from_the_exact_root_authority():
    root_query = LifecycleEligibilityQuery(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=UUID("77777777-7777-4777-8777-777777777777"),
        connection_id=CONNECTION,
        connection_version_id=UUID("88888888-8888-4888-8888-888888888888"),
        scope_reference=SCOPE,
        scope_version_id=UUID("99999999-9999-4999-8999-999999999999"),
        scope_digest="a" * 64,
        provider_object_reference=str(SCOPE),
        source_external_namespace="google",
        source_external_generation="google-source-key:v1",
        source_external_object_id="gsk1:" + "a" * 52,
    )
    request = command().model_copy(update={"lifecycle_query": root_query})
    ingestion = FakeIngestion()
    result = await coordinator(
        FakeReader(
            {
                None: DriveSnapshotPage(
                    page_token=None,
                    next_page_token=None,
                    incomplete_search=False,
                    items=(item("drive-file-1"),),
                )
            }
        ),
        ingestion,
    ).run(request)

    identity = google_source_identity(
        GoogleDriveFileKey(connection_id=CONNECTION, file_id="drive-file-1")
    )
    query = ingestion.requests[0].lifecycle_query
    assert result.state is DriveSnapshotRunState.SUCCEEDED
    assert query.provider_object_reference == "drive-file-1"
    assert query.source_external_object_id == identity.external_object_id
    assert query.scope_digest == root_query.scope_digest
