from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.google_sheets_lifecycle import (
    GoogleSheetsLifecycleCoordinator,
    GoogleSheetsLifecycleSource,
    GoogleSheetsRemovalPolicy,
    GoogleSheetsRemovalState,
    InMemoryGoogleSheetsLifecycleRepository,
    _scope_set_digest,
)
from spine.application.google_sheets_reconciliation import (
    GoogleSheetsReconciliationResult,
)
from spine.application.source_lifecycle import SourceLifecycleKind
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsSyncItem,
    GoogleSheetsSyncItemState,
    GoogleSheetsSyncPage,
    GoogleSheetsSyncPageState,
    GoogleSheetsSyncRunState,
)
from spine.domain.sources import IdentityMode, SourceObject
from spine.domain.sources.google import GoogleRegisterRowKey, google_source_identity
from tests.application.test_google_sheets_reconciliation import (
    command,
    observation,
)

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
SOURCE_ID = UUID("77777777-7777-4777-8777-777777777777")
REVISION_ID = UUID("88888888-8888-4888-8888-888888888888")
TOMBSTONE_ID = UUID("99999999-9999-4999-8999-999999999999")


def lifecycle_source(business_id: str) -> GoogleSheetsLifecycleSource:
    key = GoogleRegisterRowKey(
        connection_id=UUID("33333333-3333-4333-8333-333333333333"),
        spreadsheet_id="estimates",
        sheet_id=7,
        immutable_business_id=business_id,
    )
    identity = google_source_identity(key)
    source = SourceObject(
        source_object_id=SOURCE_ID,
        workspace_id=UUID("11111111-1111-4111-8111-111111111111"),
        environment_id=UUID("22222222-2222-4222-8222-222222222222"),
        source_kind="record",
        identity_mode=IdentityMode.CONNECTOR,
        connection_id=key.connection_id,
        external_namespace=identity.external_namespace,
        external_generation=identity.external_generation,
        external_object_id=identity.external_object_id,
        created_at=NOW,
    )
    return GoogleSheetsLifecycleSource(
        key=key,
        source=source,
        current_revision_id=REVISION_ID,
        is_tombstoned=False,
    )


class Sources:
    def __init__(self, values):
        self.values = values

    async def list_current_sources(self, command, context):
        del command, context
        return self.values


class Tombstones:
    def __init__(self):
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        idem = kwargs["idempotency_key"]
        return SimpleNamespace(
            kind=SourceLifecycleKind.DELETION,
            source=kwargs["source"].source,
            observation_idempotency_key=SimpleNamespace(value=idem),
            revision=SimpleNamespace(revision_id=TOMBSTONE_ID),
            canonical_acceptance=SimpleNamespace(
                expected_current_revision_id=REVISION_ID,
                idempotency_key=SimpleNamespace(value=idem),
            ),
        )


class Lifecycle:
    def __init__(self):
        self.commands = []

    async def apply(self, command):
        self.commands.append(command)
        return SimpleNamespace(
            acceptance=SimpleNamespace(revision=SimpleNamespace(revision_id=TOMBSTONE_ID))
        )


class SyncLedger:
    def __init__(self, request, result):
        self.run = SimpleNamespace(
            state=result.state,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            connection_id=request.connection_id,
            resource_scope_reference=request.resource_scope_reference,
            service_principal_id=request.service_principal_id,
            access_policy_version_id=request.access_policy_version_id,
            authorization_generation=request.authorization_generation,
            configuration_generation=request.configuration_generation,
            scope_set_digest=_scope_set_digest(request),
        )
        self.pages = result.pages

    async def resolve_run(self, sync_run_id):
        del sync_run_id
        return self.run

    async def list_pages(self, sync_run_id):
        del sync_run_id
        return self.pages


def complete_result(request, *, items=()):
    scope = request.scopes[0]
    page = GoogleSheetsSyncPage(
        page_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        sync_run_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        scope_identity=scope.identity,
        page_token=None,
        next_page_token=None,
        provider_version="10",
        state=GoogleSheetsSyncPageState.COMPLETED,
        items=tuple(
            GoogleSheetsSyncItem(
                item_id=UUID(int=100 + index),
                page_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                identity=item,
                input_ordinal=index,
                state=GoogleSheetsSyncItemState.SUCCEEDED,
                revision_id=UUID(int=200 + index),
                updated_at=NOW,
            )
            for index, item in enumerate(items, 1)
        ),
        created_at=NOW,
        completed_at=NOW,
    )
    return GoogleSheetsReconciliationResult(
        sync_run_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        state=GoogleSheetsSyncRunState.SUCCEEDED,
        pages=(page,),
        accepted_rows=len(items),
        failure_code=None,
        committed_cursor="cursor-2",
    )


def policy(request, *, authorize: bool) -> GoogleSheetsRemovalPolicy:
    return GoogleSheetsRemovalPolicy(
        policy_version_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        version="sheets-removal:v1",
        workspace_id=request.workspace_id,
        environment_id=request.environment_id,
        connection_id=request.connection_id,
        resource_scope_reference=request.resource_scope_reference,
        authorized_scope_identities=(
            frozenset({request.scopes[0].identity}) if authorize else frozenset()
        ),
    )


def coordinator(sources, request, result):
    repository = InMemoryGoogleSheetsLifecycleRepository()
    tombstones = Tombstones()
    lifecycle = Lifecycle()
    instance = GoogleSheetsLifecycleCoordinator(
        repository=repository,
        sync_repository=SyncLedger(request, result),
        sources=Sources(sources),
        tombstones=tombstones,
        source_lifecycle=lifecycle,
        context_factory=lambda trace: SimpleNamespace(trace_id=trace),
        clock=lambda: NOW,
    )
    return instance, repository, tombstones, lifecycle


@pytest.mark.asyncio
async def test_complete_authorized_absence_records_candidate_and_tombstones_once():
    request = command(trigger="scheduled_full")
    row = lifecycle_source("EST-1")
    result = complete_result(request)
    service, repository, tombstones, lifecycle = coordinator((row,), request, result)

    first = await service.reconcile(request, result, policy(request, authorize=True))
    replay = await service.reconcile(request, result, policy(request, authorize=True))

    assert len(first.candidates) == 1
    assert first.candidates[0].external_object_id == google_source_identity(row.key).external_object_id
    assert len(first.decisions) == 1
    assert first.decisions[0].state is GoogleSheetsRemovalState.TOMBSTONED
    assert len(repository.candidates) == 1
    assert len(repository.decisions) == 1
    assert len(tombstones.calls) == len(lifecycle.commands) == 1
    assert replay.decisions[0].tombstone_revision_id == TOMBSTONE_ID


@pytest.mark.asyncio
async def test_policy_denial_retains_candidate_without_source_lifecycle_effect():
    request = command(trigger="scheduled_full")
    result = complete_result(request)
    service, repository, tombstones, lifecycle = coordinator(
        (lifecycle_source("EST-1"),), request, result
    )

    result = await service.reconcile(
        request,
        result,
        policy(request, authorize=False),
    )

    assert len(result.candidates) == 1
    assert result.decisions[0].state is GoogleSheetsRemovalState.RETAINED
    assert result.decisions[0].policy_authorized is False
    assert len(repository.candidates) == len(repository.decisions) == 1
    assert tombstones.calls == lifecycle.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [GoogleSheetsSyncRunState.RESUMABLE, GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED])
async def test_incomplete_scan_cannot_create_candidate_or_tombstone(state):
    request = command(trigger="scheduled_full")
    result = GoogleSheetsReconciliationResult(
        sync_run_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        state=state,
        pages=(),
        accepted_rows=0,
        failure_code="sheets_incomplete_page",
        committed_cursor="cursor-1",
    )
    service, repository, tombstones, lifecycle = coordinator(
        (lifecycle_source("EST-1"),), request, result
    )

    outcome = await service.reconcile(request, result, policy(request, authorize=True))

    assert outcome.retained_reason == "reconciliation_incomplete"
    assert repository.candidates == repository.decisions == {}
    assert tombstones.calls == lifecycle.commands == []


@pytest.mark.asyncio
async def test_observed_identity_is_not_an_absence_candidate():
    request = command(trigger="scheduled_full")
    present = observation("EST-1").external_identity.external_object_id
    result = complete_result(request, items=(present,))
    service, repository, tombstones, lifecycle = coordinator(
        (lifecycle_source("EST-1"),), request, result
    )

    outcome = await service.reconcile(
        request,
        result,
        policy(request, authorize=True),
    )

    assert outcome.candidates == outcome.decisions == ()
    assert repository.candidates == repository.decisions == {}
    assert tombstones.calls == lifecycle.commands == []
