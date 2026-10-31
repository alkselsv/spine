from __future__ import annotations

from datetime import datetime, timezone
from contextlib import asynccontextmanager
import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from spine.application.yandex_disk_reconciliation import (
    InMemoryYandexDiskReconciliationRepository,
    InMemoryYandexDiskRemovalPolicyAdmission,
    YandexDiskLifecycleSource,
    YandexDiskLifecycleState,
    YandexDiskIncompleteScanError,
    AuthorizedYandexDiskScanner,
    YandexDiskReconciliationCommand,
    YandexDiskReconciliationCoordinator,
    YandexDiskReconciliationState,
    YandexDiskRemovalPolicy,
    YandexDiskScanItem,
    YandexDiskScanPage,
)
from spine.application.source_lifecycle import SourceLifecycleKind
from spine.connectors.yandex_disk import (
    YandexDiskAuthorizationRevokedError,
    YandexDiskPermissionLostError,
    YandexDiskResourceNotFoundError,
    YandexDiskScopeOutOfScopeError,
    YandexDiskThrottleError,
    YandexDiskTransientError,
    YandexDiskScanItem as ProviderScanItem,
    YandexDiskScanPage as ProviderScanPage,
    yandex_disk_capabilities,
)
from spine.application.connections.provider import (
    ProviderCapabilityUnavailableError,
    ProviderReadDeniedError,
)
from spine.application.lifecycle import LifecycleEligibilityQuery
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.sources import RevisionKind
from spine.domain.sources.lifecycle import (
    CurrentLifecycleEligibility,
    LifecycleEligibilityStatus,
)


NOW = datetime(2026, 10, 29, 15, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
CONNECTION_VERSION = UUID("34343434-3434-4434-8434-343434343434")
SCOPE = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
ACCESS_POLICY = UUID("66666666-6666-4666-8666-666666666666")
REMOVAL_POLICY = UUID("77777777-7777-4777-8777-777777777777")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"test:yandex-reconciliation:{value}")


def command() -> YandexDiskReconciliationCommand:
    return YandexDiskReconciliationCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        connection_version_id=CONNECTION_VERSION,
        resource_scope_reference=SCOPE,
        service_principal_id=PRINCIPAL,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=ACCESS_POLICY,
        command_identity="yandex-reconciliation-670",
        idempotency_key="issue670-yandex-reconciliation",
        lifecycle_query=LifecycleEligibilityQuery(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=_id("root-source"),
            connection_id=CONNECTION,
            connection_version_id=CONNECTION_VERSION,
            scope_reference=SCOPE,
            scope_version_id=_id("approval"),
            scope_digest="a" * 64,
            provider_object_reference=str(SCOPE),
            source_external_namespace="yandex-disk",
            source_external_generation="yandex-disk-path:v1",
            source_external_object_id="root",
        ),
    )


class _LifecycleReader:
    async def current_eligibility(self, query, context):
        return CurrentLifecycleEligibility(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            source_object_id=query.source_object_id,
            connection_id=query.connection_id,
            connection_version_id=query.connection_version_id,
            scope_reference=query.scope_reference,
            scope_version_id=query.scope_version_id,
            scope_digest=query.scope_digest,
            current_decision_id=_id("eligible-decision"),
            generation=1,
            status=LifecycleEligibilityStatus.ELIGIBLE,
            legal_hold_active=False,
        )


def policy(*, authorize_absence: bool = True) -> YandexDiskRemovalPolicy:
    return YandexDiskRemovalPolicy(
        policy_version_id=REMOVAL_POLICY,
        approval_id=_id("removal-policy-approval"),
        approved_at=NOW,
        version="yandex-removal-policy:v1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorize_complete_scope_absence=authorize_absence,
    )


def approved_policies(
    *, authorize_absence: bool = True
) -> InMemoryYandexDiskRemovalPolicyAdmission:
    return InMemoryYandexDiskRemovalPolicyAdmission(
        (policy(authorize_absence=authorize_absence),)
    )


def connection(*, full_reconciliation: bool = True) -> ProviderConnection:
    qualification = OpaqueProviderReference(value=_id("qualification"))
    capabilities = list(
        yandex_disk_capabilities(qualification_reference=qualification)
    )
    if not full_reconciliation:
        capabilities = [
            (
                ProviderCapability(
                    kind=item.kind,
                    status=ProviderCapabilityStatus.UNSUPPORTED,
                )
                if item.kind is ProviderCapabilityKind.FULL_RECONCILIATION
                else item
            )
            for item in capabilities
        ]
    return ProviderConnection(
        connection_version_id=CONNECTION_VERSION,
        connection_id=CONNECTION,
        approval_id=_id("approval"),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=PRINCIPAL,
        provider_kind=ProviderKind.YANDEX_DISK,
        credential_reference=OpaqueProviderReference(value=_id("credential")),
        grant_reference=OpaqueProviderReference(value=_id("grant")),
        approved_resource_scope_references=(OpaqueProviderReference(value=SCOPE),),
        authorization_generation=3,
        configuration_generation=4,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=tuple(capabilities),
    )


class Scanner:
    def __init__(
        self,
        pages: tuple[YandexDiskScanPage, ...],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.pages = list(pages)
        self.failure = failure
        self.checkpoints: list[str | None] = []

    async def read_page(self, request, checkpoint, context):
        del request, context
        self.checkpoints.append(checkpoint)
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        return self.pages.pop(0)


class ConnectionRepository:
    def __init__(self, value: ProviderConnection) -> None:
        self.value = value

    @asynccontextmanager
    async def admit_read(self, request, context):
        del request, context
        yield self.value


class ScanProvider:
    def __init__(self) -> None:
        self.calls = []

    async def scan_page(self, provider_connection, scope, *, checkpoint):
        self.calls.append((provider_connection, scope, checkpoint))
        return ProviderScanPage(
            page_identity="provider-page-1",
            requested_checkpoint=checkpoint,
            checkpoint="opaque-terminal",
            next_checkpoint=None,
            items=(
                ProviderScanItem(
                    provider_object_reference="disk:/approved/file.pdf",
                    provider_revision="provider-revision-1",
                ),
            ),
        )


class Ingestion:
    def __init__(
        self,
        repository: InMemoryYandexDiskReconciliationRepository,
        *,
        fail_once_for: str | None = None,
    ) -> None:
        self.repository = repository
        self.fail_once_for = fail_once_for
        self.commands = []
        self.committed_at_call: list[str | None] = []

    async def ingest(self, request):
        self.commands.append(request)
        run = next(iter(self.repository.records.values()))
        self.committed_at_call.append(run.committed_checkpoint)
        if request.resource_object_reference == self.fail_once_for:
            self.fail_once_for = None
            raise RuntimeError("synthetic interrupted canonical acceptance")
        occurrence = getattr(request, "external_identity_occurrence", None)
        source_id = _id(
            f"source:{request.resource_object_reference}:{occurrence or 'initial'}"
        )
        revision_id = _id(f"revision:{request.resource_object_reference}")
        return SimpleNamespace(
            source=SimpleNamespace(source_object_id=source_id),
            revision=SimpleNamespace(
                revision_id=revision_id,
                kind=RevisionKind.CONTENT,
            ),
            canonical_acceptance=SimpleNamespace(revision=SimpleNamespace(revision_id=revision_id)),
        )


class Sources:
    def __init__(self, values: tuple[YandexDiskLifecycleSource, ...] = ()) -> None:
        self.values = values

    async def list_sources(self, request, context):
        del request, context
        return self.values


class Tombstones:
    def __init__(self) -> None:
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        source = kwargs["source"]
        revision_id = _id(f"tombstone:{source.source_object_id}")
        return SimpleNamespace(
            kind=SourceLifecycleKind.DELETION,
            source=SimpleNamespace(source_object_id=source.source_object_id),
            revision=SimpleNamespace(revision_id=revision_id),
            observation_idempotency_key=SimpleNamespace(
                value=kwargs["idempotency_key"]
            ),
            canonical_acceptance=SimpleNamespace(
                idempotency_key=SimpleNamespace(value=kwargs["idempotency_key"]),
                expected_current_revision_id=source.current_revision_id,
            ),
        )


class Lifecycle:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.commands = []
        self.fail_once = fail_once

    async def apply(self, request):
        self.commands.append(request)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic lifecycle interruption")
        return SimpleNamespace(
            acceptance=SimpleNamespace(revision=request.revision)
        )


def page(
    identity: str,
    *,
    requested: str | None,
    checkpoint: str,
    next_checkpoint: str | None,
    paths: tuple[str, ...],
    expected_count: int | None = None,
) -> YandexDiskScanPage:
    expected_count = len(paths) if expected_count is None else expected_count
    return YandexDiskScanPage(
        page_identity=identity,
        requested_checkpoint=requested,
        checkpoint=checkpoint,
        next_checkpoint=next_checkpoint,
        items=tuple(
            YandexDiskScanItem(
                provider_object_reference=path,
                provider_revision=f"revision-{index}",
            )
            for index, path in enumerate(paths)
        ),
        coverage_entry_digests=tuple(
            hashlib.sha256(f"{identity}:{path}".encode()).hexdigest()
            for path in paths
        ),
        expected_entry_count=expected_count,
        confirmed_scan_generation=(
            hashlib.sha256(identity.encode()).hexdigest()
            if next_checkpoint is None
            else None
        ),
    )


@pytest.mark.asyncio
async def test_pages_commit_opaque_checkpoint_only_after_canonical_effects_are_accepted():
    repository = InMemoryYandexDiskReconciliationRepository()
    scanner = Scanner(
        (
            page(
                "page-1",
                requested=None,
                checkpoint="opaque-after-page-1",
                next_checkpoint="opaque-page-2",
                paths=("disk:/approved/a.pdf",),
                expected_count=2,
            ),
            page(
                "page-2",
                requested="opaque-page-2",
                checkpoint="opaque-terminal",
                next_checkpoint=None,
                paths=("disk:/approved/b.pdf",),
                expected_count=2,
            ),
        )
    )
    ingestion = Ingestion(repository)
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=ingestion,
        sources=Sources(),
        tombstones=Tombstones(),
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy())

    assert result.state is YandexDiskReconciliationState.COMPLETE
    assert result.committed_checkpoint == "opaque-terminal"
    assert scanner.checkpoints == [None, "opaque-page-2"]
    assert ingestion.committed_at_call == [None, "opaque-after-page-1"]
    assert [record.checkpoint for record in result.pages] == [
        "opaque-after-page-1",
        "opaque-terminal",
    ]


@pytest.mark.asyncio
async def test_interrupted_page_replays_with_stable_item_identity_and_keeps_prior_checkpoint():
    repository = InMemoryYandexDiskReconciliationRepository()
    first = page(
        "page-1",
        requested=None,
        checkpoint="opaque-after-page-1",
        next_checkpoint="opaque-page-2",
        paths=("disk:/approved/a.pdf",),
        expected_count=2,
    )
    second = page(
        "page-2",
        requested="opaque-page-2",
        checkpoint="opaque-terminal",
        next_checkpoint=None,
        paths=("disk:/approved/b.pdf",),
        expected_count=2,
    )
    scanner = Scanner((first, second, second))
    ingestion = Ingestion(
        repository,
        fail_once_for="disk:/approved/b.pdf",
    )
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=ingestion,
        sources=Sources(),
        tombstones=Tombstones(),
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    interrupted = await service.run(command(), policy())
    recovered = await service.run(command(), policy())

    assert interrupted.state is YandexDiskReconciliationState.INTERRUPTED
    assert interrupted.committed_checkpoint == "opaque-after-page-1"
    assert recovered.state is YandexDiskReconciliationState.COMPLETE
    assert scanner.checkpoints == [None, "opaque-page-2", "opaque-page-2"]
    duplicate_commands = [
        request
        for request in ingestion.commands
        if request.resource_object_reference == "disk:/approved/b.pdf"
    ]
    assert len(duplicate_commands) == 2
    assert duplicate_commands[0].idempotency_key == duplicate_commands[1].idempotency_key


@pytest.mark.asyncio
async def test_provider_failure_recovery_preserves_baseline_and_emits_recovered_outcomes():
    prior = YandexDiskLifecycleSource(
        provider_object_reference="disk:/approved/changed.pdf",
        source_object_id=_id("recovery-prior-source"),
        current_revision_id=_id("recovery-prior-revision"),
        current_revision_kind=RevisionKind.CONTENT,
        current_provider_revision="provider-revision-before",
    )
    repository = InMemoryYandexDiskReconciliationRepository()
    scanner = Scanner(
        (
            page(
                "recovery-page",
                requested=None,
                checkpoint="opaque-recovery-terminal",
                next_checkpoint=None,
                paths=("disk:/approved/changed.pdf",),
            ),
        ),
        failure=YandexDiskTransientError(),
    )
    sources = Sources((prior,))
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=Ingestion(repository),
        sources=sources,
        tombstones=Tombstones(),
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    interrupted = await service.run(command(), policy())
    sources.values = ()  # canonical state may have advanced after partial work
    recovered = await service.run(command(), policy())

    assert interrupted.state is YandexDiskReconciliationState.INTERRUPTED
    assert [event.state for event in recovered.lifecycle_evidence] == [
        YandexDiskLifecycleState.TEMPORARILY_UNAVAILABLE,
        YandexDiskLifecycleState.CHANGED,
    ]
    assert recovered.baseline_sources == (prior,)


@pytest.mark.asyncio
async def test_complete_scope_rename_records_new_path_and_tombstones_old_path_without_continuity():
    old_source = YandexDiskLifecycleSource(
        provider_object_reference="disk:/approved/old-name.pdf",
        source_object_id=_id("source:disk:/approved/old-name.pdf"),
        current_revision_id=_id("revision:disk:/approved/old-name.pdf"),
        current_revision_kind=RevisionKind.CONTENT,
        current_provider_revision="revision-0",
    )
    repository = InMemoryYandexDiskReconciliationRepository()
    scanner = Scanner(
        (
            page(
                "rename-page",
                requested=None,
                checkpoint="opaque-rename-complete",
                next_checkpoint=None,
                paths=("disk:/approved/new-name.pdf",),
            ),
        )
    )
    ingestion = Ingestion(repository)
    tombstones = Tombstones()
    lifecycle = Lifecycle()
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=ingestion,
        sources=Sources((old_source,)),
        tombstones=tombstones,
        source_lifecycle=lifecycle,
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy())

    assert result.state is YandexDiskReconciliationState.COMPLETE
    assert [event.state for event in result.lifecycle_evidence] == [
        YandexDiskLifecycleState.OBSERVED,
        YandexDiskLifecycleState.CONFIRMED_REMOVED,
    ]
    observed, removed = result.lifecycle_evidence
    assert observed.provider_object_reference_digest != removed.provider_object_reference_digest
    assert observed.source_object_id != removed.source_object_id
    assert observed.identity_continuity_proven is False
    assert removed.canonical_effect_accepted is True
    assert removed.tombstone_revision_id == lifecycle.commands[0].revision.revision_id
    assert result.committed_checkpoint == "opaque-rename-complete"
    assert len(tombstones.calls) == 1
    assert tombstones.calls[0]["source"] == old_source


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        (
            YandexDiskIncompleteScanError(),
            YandexDiskLifecycleState.AMBIGUOUS_ABSENCE,
        ),
        (
            YandexDiskThrottleError(retry_after_seconds=1),
            YandexDiskLifecycleState.TEMPORARILY_UNAVAILABLE,
        ),
        (
            YandexDiskTransientError(),
            YandexDiskLifecycleState.TEMPORARILY_UNAVAILABLE,
        ),
        (
            YandexDiskResourceNotFoundError(),
            YandexDiskLifecycleState.AMBIGUOUS_ABSENCE,
        ),
        (
            YandexDiskPermissionLostError(),
            YandexDiskLifecycleState.ACCESS_LOST,
        ),
        (
            YandexDiskScopeOutOfScopeError(),
            YandexDiskLifecycleState.OUT_OF_SCOPE,
        ),
        (
            YandexDiskAuthorizationRevokedError(),
            YandexDiskLifecycleState.CONNECTION_REVOKED,
        ),
    ],
)
@pytest.mark.asyncio
async def test_incomplete_and_provider_failures_record_distinct_state_without_tombstone(
    failure,
    expected_state,
):
    repository = InMemoryYandexDiskReconciliationRepository()
    scanner = Scanner((), failure=failure)
    tombstones = Tombstones()
    lifecycle = Lifecycle()
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=Ingestion(repository),
        sources=Sources(),
        tombstones=tombstones,
        source_lifecycle=lifecycle,
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy())

    assert result.state is YandexDiskReconciliationState.INTERRUPTED
    assert result.committed_checkpoint is None
    assert result.pages == ()
    assert [event.state for event in result.lifecycle_evidence] == [expected_state]
    assert result.lifecycle_evidence[0].canonical_effect_accepted is False
    assert tombstones.calls == []
    assert lifecycle.commands == []


@pytest.mark.asyncio
async def test_repeated_identical_failure_is_idempotent_in_lifecycle_history():
    repository = InMemoryYandexDiskReconciliationRepository()
    scanner = Scanner((), failure=YandexDiskTransientError())
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=Ingestion(repository),
        sources=Sources(),
        tombstones=Tombstones(),
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    first = await service.run(command(), policy())
    scanner.failure = YandexDiskTransientError()
    second = await service.run(command(), policy())

    assert first.lifecycle_evidence == second.lifecycle_evidence
    assert len(second.lifecycle_evidence) == 1


@pytest.mark.asyncio
async def test_removal_policy_admission_rejects_caller_mutation_before_scan():
    approved = policy(authorize_absence=False)
    scanner = Scanner(())
    service = YandexDiskReconciliationCoordinator(
        repository=InMemoryYandexDiskReconciliationRepository(),
        scanner=scanner,
        ingestion=SimpleNamespace(),
        sources=Sources(),
        tombstones=Tombstones(),
        source_lifecycle=Lifecycle(),
        removal_policies=InMemoryYandexDiskRemovalPolicyAdmission((approved,)),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(ProviderReadDeniedError):
        await service.run(
            command(), approved.model_copy(update={"authorize_complete_scope_absence": True})
        )

    assert scanner.checkpoints == []


@pytest.mark.asyncio
async def test_scan_admission_rejects_unqualified_full_reconciliation_before_provider_call():
    provider = ScanProvider()
    scanner = AuthorizedYandexDiskScanner(
        repository=ConnectionRepository(connection(full_reconciliation=False)),
        provider=provider,
        lifecycle_reader=_LifecycleReader(),
    )

    with pytest.raises(ProviderCapabilityUnavailableError) as error:
        await scanner.read_page(command(), None, SimpleNamespace())

    assert error.value.capability is ProviderCapabilityKind.FULL_RECONCILIATION
    assert provider.calls == []


@pytest.mark.asyncio
async def test_scan_admission_maps_qualified_provider_page_without_interpreting_checkpoint():
    provider = ScanProvider()
    scanner = AuthorizedYandexDiskScanner(
        repository=ConnectionRepository(connection()),
        provider=provider,
        lifecycle_reader=_LifecycleReader(),
    )

    result = await scanner.read_page(command(), "opaque-input", SimpleNamespace())

    assert result.requested_checkpoint == "opaque-input"
    assert result.checkpoint == "opaque-terminal"
    assert result.items[0].provider_object_reference == "disk:/approved/file.pdf"
    assert provider.calls[0][2] == "opaque-input"


@pytest.mark.asyncio
async def test_path_scoped_reappearance_creates_new_source_and_records_non_equivalence():
    prior = YandexDiskLifecycleSource(
        provider_object_reference="disk:/approved/reappeared.pdf",
        source_object_id=_id("prior-path-scoped-source"),
        current_revision_id=_id("prior-path-scoped-tombstone"),
        current_revision_kind=RevisionKind.TOMBSTONE,
        current_provider_revision=None,
    )
    repository = InMemoryYandexDiskReconciliationRepository()
    ingestion = Ingestion(repository)
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=Scanner(
            (
                page(
                    "reappearance-page",
                    requested=None,
                    checkpoint="opaque-reappearance-complete",
                    next_checkpoint=None,
                    paths=("disk:/approved/reappeared.pdf",),
                ),
            )
        ),
        ingestion=ingestion,
        sources=Sources((prior,)),
        tombstones=Tombstones(),
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy())

    event = result.lifecycle_evidence[0]
    assert event.state is YandexDiskLifecycleState.REAPPEARED
    assert event.predecessor_source_object_id == prior.source_object_id
    assert event.predecessor_revision_id == prior.current_revision_id
    assert event.source_object_id != prior.source_object_id
    assert event.identity_continuity_proven is False
    assert ingestion.commands[0].external_identity_occurrence == result.run_id.hex


@pytest.mark.asyncio
async def test_duplicate_page_item_reuses_accepted_effect_and_emits_one_lifecycle_record():
    repository = InMemoryYandexDiskReconciliationRepository()
    scanner = Scanner(
        (
            page(
                "duplicate-page-1",
                requested=None,
                checkpoint="opaque-duplicate-1",
                next_checkpoint="opaque-duplicate-2",
                paths=("disk:/approved/duplicate.pdf",),
                expected_count=2,
            ),
            page(
                "duplicate-page-2",
                requested="opaque-duplicate-2",
                checkpoint="opaque-duplicate-terminal",
                next_checkpoint=None,
                paths=("disk:/approved/duplicate.pdf",),
                expected_count=2,
            ),
        )
    )
    ingestion = Ingestion(repository)
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=ingestion,
        sources=Sources(),
        tombstones=Tombstones(),
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy())

    assert result.state is YandexDiskReconciliationState.COMPLETE
    assert len(ingestion.commands) == 1
    assert len(result.pages) == 2
    assert len(result.lifecycle_evidence) == 1
    assert result.lifecycle_evidence[0].state is YandexDiskLifecycleState.OBSERVED


@pytest.mark.asyncio
async def test_provider_reordering_cannot_certify_complete_coverage_or_tombstone():
    missing = YandexDiskLifecycleSource(
        provider_object_reference="disk:/approved/c.pdf",
        source_object_id=_id("reordered-missing-source"),
        current_revision_id=_id("reordered-missing-revision"),
        current_revision_kind=RevisionKind.CONTENT,
        current_provider_revision="revision-c",
    )
    repository = InMemoryYandexDiskReconciliationRepository()
    first = page(
        "reordered-page-1",
        requested=None,
        checkpoint="opaque-reordered-1",
        next_checkpoint="opaque-reordered-2",
        paths=("disk:/approved/a.pdf", "disk:/approved/b.pdf"),
    ).model_copy(
        update={
            "coverage_entry_digests": ("a" * 64, "b" * 64),
            "expected_entry_count": 3,
        }
    )
    terminal = page(
        "reordered-page-2",
        requested="opaque-reordered-2",
        checkpoint="opaque-reordered-terminal",
        next_checkpoint=None,
        paths=("disk:/approved/b.pdf",),
    ).model_copy(
        update={
            "coverage_entry_digests": ("b" * 64,),
            "expected_entry_count": 3,
        }
    )
    tombstones = Tombstones()
    lifecycle = Lifecycle()
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=Scanner((first, terminal)),
        ingestion=Ingestion(repository),
        sources=Sources((missing,)),
        tombstones=tombstones,
        source_lifecycle=lifecycle,
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy())

    assert result.state is YandexDiskReconciliationState.INTERRUPTED
    assert result.committed_checkpoint == "opaque-reordered-1"
    assert result.proposed_terminal_checkpoint is None
    assert result.lifecycle_evidence[-1].state is YandexDiskLifecycleState.AMBIGUOUS_ABSENCE
    assert tombstones.calls == []
    assert lifecycle.commands == []


@pytest.mark.asyncio
async def test_interrupted_tombstone_resumes_without_rescanning_or_committing_terminal_checkpoint():
    missing = YandexDiskLifecycleSource(
        provider_object_reference="disk:/approved/removed.pdf",
        source_object_id=_id("removed-source"),
        current_revision_id=_id("removed-revision"),
        current_revision_kind=RevisionKind.CONTENT,
        current_provider_revision="removed-provider-revision",
    )
    repository = InMemoryYandexDiskReconciliationRepository()
    scanner = Scanner(
        (
            page(
                "empty-terminal-page",
                requested=None,
                checkpoint="opaque-empty-terminal",
                next_checkpoint=None,
                paths=(),
            ),
        )
    )
    tombstones = Tombstones()
    lifecycle = Lifecycle(fail_once=True)
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=scanner,
        ingestion=Ingestion(repository),
        sources=Sources((missing,)),
        tombstones=tombstones,
        source_lifecycle=lifecycle,
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    interrupted = await service.run(command(), policy())
    recovered = await service.run(command(), policy())

    assert interrupted.state is YandexDiskReconciliationState.INTERRUPTED
    assert interrupted.committed_checkpoint is None
    assert interrupted.proposed_terminal_checkpoint == "opaque-empty-terminal"
    assert interrupted.lifecycle_evidence[0].canonical_effect_accepted is False
    assert recovered.state is YandexDiskReconciliationState.COMPLETE
    assert recovered.committed_checkpoint == "opaque-empty-terminal"
    assert recovered.lifecycle_evidence[0].canonical_effect_accepted is True
    assert scanner.checkpoints == [None]
    assert len(tombstones.calls) == 2
    assert (
        tombstones.calls[0]["idempotency_key"]
        == tombstones.calls[1]["idempotency_key"]
    )


@pytest.mark.asyncio
async def test_changed_revision_records_history_without_lifecycle_deletion():
    current = YandexDiskLifecycleSource(
        provider_object_reference="disk:/approved/changed.pdf",
        source_object_id=_id("changed-source"),
        current_revision_id=_id("changed-old-revision"),
        current_revision_kind=RevisionKind.CONTENT,
        current_provider_revision="provider-revision-old",
    )
    repository = InMemoryYandexDiskReconciliationRepository()
    tombstones = Tombstones()
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=Scanner(
            (
                page(
                    "changed-page",
                    requested=None,
                    checkpoint="opaque-changed-terminal",
                    next_checkpoint=None,
                    paths=("disk:/approved/changed.pdf",),
                ),
            )
        ),
        ingestion=Ingestion(repository),
        sources=Sources((current,)),
        tombstones=tombstones,
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy())

    assert [event.state for event in result.lifecycle_evidence] == [
        YandexDiskLifecycleState.CHANGED
    ]
    assert result.lifecycle_evidence[0].canonical_effect_accepted is True
    assert tombstones.calls == []


@pytest.mark.asyncio
async def test_complete_absence_without_deletion_policy_remains_ambiguous():
    current = YandexDiskLifecycleSource(
        provider_object_reference="disk:/approved/ambiguous.pdf",
        source_object_id=_id("ambiguous-source"),
        current_revision_id=_id("ambiguous-revision"),
        current_revision_kind=RevisionKind.CONTENT,
        current_provider_revision="provider-revision-old",
    )
    repository = InMemoryYandexDiskReconciliationRepository()
    tombstones = Tombstones()
    service = YandexDiskReconciliationCoordinator(
        repository=repository,
        scanner=Scanner(
            (
                page(
                    "ambiguous-page",
                    requested=None,
                    checkpoint="opaque-ambiguous-terminal",
                    next_checkpoint=None,
                    paths=(),
                ),
            )
        ),
        ingestion=Ingestion(repository),
        sources=Sources((current,)),
        tombstones=tombstones,
        source_lifecycle=Lifecycle(),
        removal_policies=approved_policies(authorize_absence=False),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
    )

    result = await service.run(command(), policy(authorize_absence=False))

    assert result.state is YandexDiskReconciliationState.COMPLETE
    assert result.lifecycle_evidence[0].state is YandexDiskLifecycleState.AMBIGUOUS_ABSENCE
    assert result.lifecycle_evidence[0].canonical_effect_accepted is False
    assert tombstones.calls == []
