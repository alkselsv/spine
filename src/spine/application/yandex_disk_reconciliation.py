"""Durable conservative reconciliation for a path-scoped Yandex Disk source."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionResult,
)
from spine.application.connections.provider import (
    ProviderCapabilityUnavailableError,
    ProviderConnectionRepository,
    ProviderReadCommand,
    ProviderReadDeniedError,
)
from spine.application.lifecycle import (
    CurrentLifecycleEligibilityReader,
    LifecycleAuthorityError,
    LifecycleEligibilityQuery,
    require_current_read_eligibility,
    lifecycle_query_for_source,
)
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.source_lifecycle import (
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
)
from spine.application.yandex_disk_scan import (
    YandexDiskAuthorizationRevokedError,
    YandexDiskContractError,
    YandexDiskPermissionLostError,
    YandexDiskResourceNotFoundError,
    YandexDiskScopeOutOfScopeError,
    YandexDiskThrottleError,
    YandexDiskTransientError,
    YandexDiskScanItem,
    YandexDiskScanPage,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.sources import RevisionKind


class YandexDiskIncompleteScanError(RuntimeError):
    """A bounded scan ended without complete pagination evidence."""

    def __init__(self) -> None:
        super().__init__("Yandex Disk scan did not cover the complete approved scope.")


class YandexDiskReconciliationState(str, Enum):
    RUNNING = "running"
    RECONCILING = "reconciling"
    INTERRUPTED = "interrupted"
    COMPLETE = "complete"


class YandexDiskLifecycleState(str, Enum):
    OBSERVED = "observed"
    CHANGED = "changed"
    CONFIRMED_REMOVED = "confirmed_removed"
    OUT_OF_SCOPE = "out_of_scope"
    ACCESS_LOST = "access_lost"
    CONNECTION_REVOKED = "connection_revoked"
    AMBIGUOUS_ABSENCE = "ambiguous_absence"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    REAPPEARED = "reappeared"


class YandexDiskPageItemRecord(DefinitionModel):
    provider_object_reference: str = Field(min_length=1, max_length=2048)
    provider_revision: str = Field(min_length=1, max_length=128)
    source_object_id: UUID
    source_revision_id: UUID


class YandexDiskPageRecord(DefinitionModel):
    page_identity: str = Field(min_length=1, max_length=128)
    requested_checkpoint: str | None = Field(default=None, max_length=4096)
    checkpoint: str = Field(min_length=1, max_length=4096)
    next_checkpoint: str | None = Field(default=None, max_length=4096)
    items: tuple[YandexDiskPageItemRecord, ...]
    coverage_entry_digests: tuple[str, ...] = ()
    expected_entry_count: int | None = Field(default=None, ge=0)
    confirmed_scan_generation: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    accepted_at: datetime


class YandexDiskLifecycleSource(DefinitionModel):
    """Current canonical source projected into the exact governed scan scope."""

    provider_object_reference: str = Field(min_length=1, max_length=2048)
    source_object_id: UUID
    current_revision_id: UUID
    current_revision_kind: RevisionKind
    current_provider_revision: str | None = Field(default=None, max_length=128)


class YandexDiskLifecycleEvidence(DefinitionModel):
    """Content-free canonical history for one reconciliation conclusion."""

    evidence_id: UUID
    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    policy_version_id: UUID
    committed_checkpoint: str | None = Field(default=None, max_length=4096)
    proposed_terminal_checkpoint: str | None = Field(default=None, max_length=4096)
    state: YandexDiskLifecycleState
    provider_object_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    predecessor_source_object_id: UUID | None = None
    predecessor_revision_id: UUID | None = None
    tombstone_revision_id: UUID | None = None
    identity_continuity_proven: bool = False
    canonical_effect_accepted: bool = False
    evidence_reference: str = Field(min_length=1, max_length=128)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime


class YandexDiskReconciliationCommand(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    access_policy_version_id: UUID
    command_identity: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=255)
    last_committed_checkpoint: str | None = Field(default=None, max_length=4096)
    lifecycle_query: LifecycleEligibilityQuery | None = None

    @model_validator(mode="after")
    def validate_ids(self) -> "YandexDiskReconciliationCommand":
        for field_name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "connection_version_id",
            "resource_scope_reference",
            "service_principal_id",
            "access_policy_version_id",
        ):
            if getattr(self, field_name).int == 0:
                raise ValueError(f"{field_name} must be a non-zero UUID")
        query = self.lifecycle_query
        if query is not None and (
            query.workspace_id != self.workspace_id
            or query.environment_id != self.environment_id
            or query.connection_id != self.connection_id
            or query.connection_version_id != self.connection_version_id
            or query.scope_reference != self.resource_scope_reference
            or query.provider_object_reference != str(self.resource_scope_reference)
            or query.source_external_namespace is None
        ):
            raise ValueError("lifecycle query must bind the exact Yandex source scope")
        return self


class YandexDiskRemovalPolicy(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_version_id: UUID
    approval_id: UUID
    approved_at: datetime
    version: VersionIdentifier
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorize_complete_scope_absence: bool = False

    @model_validator(mode="after")
    def validate_ids(self) -> "YandexDiskRemovalPolicy":
        for field_name in (
            "policy_version_id",
            "approval_id",
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
        ):
            if getattr(self, field_name).int == 0:
                raise ValueError(f"{field_name} must be a non-zero UUID")
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        return self


class YandexDiskReconciliationRun(DefinitionModel):
    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=255)
    removal_policy_version_id: UUID
    removal_policy_approval_id: UUID
    state: YandexDiskReconciliationState
    committed_checkpoint: str | None = Field(default=None, max_length=4096)
    next_checkpoint: str | None = Field(default=None, max_length=4096)
    proposed_terminal_checkpoint: str | None = Field(default=None, max_length=4096)
    baseline_captured: bool = False
    baseline_sources: tuple[YandexDiskLifecycleSource, ...] = ()
    pages: tuple[YandexDiskPageRecord, ...] = ()
    lifecycle_evidence: tuple[YandexDiskLifecycleEvidence, ...] = ()
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class YandexDiskReconciliationRepository(Protocol):
    async def resolve(
        self, run_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> YandexDiskReconciliationRun | None: ...

    async def save(
        self, run: YandexDiskReconciliationRun
    ) -> YandexDiskReconciliationRun: ...


class InMemoryYandexDiskReconciliationRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, YandexDiskReconciliationRun] = {}

    async def resolve(
        self, run_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> YandexDiskReconciliationRun | None:
        run = self.records.get(run_id)
        if run is not None and (run.workspace_id, run.environment_id) != (
            workspace_id,
            environment_id,
        ):
            return None
        return run

    async def save(
        self, run: YandexDiskReconciliationRun
    ) -> YandexDiskReconciliationRun:
        current = self.records.get(run.run_id)
        if current is not None and (
            current.workspace_id,
            current.environment_id,
            current.connection_id,
            current.connection_version_id,
            current.resource_scope_reference,
            current.idempotency_key,
        ) != (
            run.workspace_id,
            run.environment_id,
            run.connection_id,
            run.connection_version_id,
            run.resource_scope_reference,
            run.idempotency_key,
        ):
            raise ValueError("Yandex Disk reconciliation identity conflicts with replay")
        self.records[run.run_id] = run
        return run


class YandexDiskScanPort(Protocol):
    async def read_page(
        self,
        command: YandexDiskReconciliationCommand,
        checkpoint: str | None,
        context: TrustedPersistenceContext,
    ) -> YandexDiskScanPage: ...


class YandexDiskRemovalPolicyAdmission(Protocol):
    async def admit(
        self,
        command: YandexDiskReconciliationCommand,
        requested_policy: YandexDiskRemovalPolicy,
        context: TrustedPersistenceContext,
    ) -> YandexDiskRemovalPolicy: ...


class InMemoryYandexDiskRemovalPolicyAdmission:
    """Immutable approved-policy fake used by application and contract tests."""

    def __init__(self, policies: tuple[YandexDiskRemovalPolicy, ...]) -> None:
        self._policies = {policy.policy_version_id: policy for policy in policies}

    async def admit(
        self,
        command: YandexDiskReconciliationCommand,
        requested_policy: YandexDiskRemovalPolicy,
        context: TrustedPersistenceContext,
    ) -> YandexDiskRemovalPolicy:
        del command, context
        approved = self._policies.get(requested_policy.policy_version_id)
        if approved != requested_policy:
            raise ProviderReadDeniedError()
        return approved


class YandexDiskScanProvider(Protocol):
    async def scan_page(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        checkpoint: str | None,
    ) -> YandexDiskScanPage: ...


@dataclass(frozen=True, slots=True)
class AuthorizedYandexDiskScanner:
    """Admit one exact connection version before enumerating a provider page."""

    repository: ProviderConnectionRepository
    provider: YandexDiskScanProvider
    lifecycle_reader: CurrentLifecycleEligibilityReader | None = None

    async def read_page(
        self,
        command: YandexDiskReconciliationCommand,
        checkpoint: str | None,
        context: TrustedPersistenceContext,
    ) -> YandexDiskScanPage:
        query = command.lifecycle_query
        if query is None or self.lifecycle_reader is None:
            raise ProviderReadDeniedError()
        else:
            try:
                await require_current_read_eligibility(
                    self.lifecycle_reader, query, context
                )
            except LifecycleAuthorityError:
                raise ProviderReadDeniedError() from None
        read_command = ProviderReadCommand(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            connection_version_id=command.connection_version_id,
            resource_scope_reference=OpaqueProviderReference(
                value=command.resource_scope_reference
            ),
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            required_capabilities=(
                ProviderCapabilityKind.ENUMERATION,
                ProviderCapabilityKind.METADATA_READ,
                ProviderCapabilityKind.FULL_RECONCILIATION,
            ),
            resource_object_reference=(
                query.provider_object_reference if query is not None else None
            ),
            lifecycle_query=query,
        )
        async with self.repository.admit_read(read_command, context) as connection:
            _validate_scan_connection(read_command, connection)
            for capability in read_command.required_capabilities:
                if not connection.supports(capability):
                    raise ProviderCapabilityUnavailableError(capability)
            provider_page = await self.provider.scan_page(
                connection,
                read_command.resource_scope_reference,
                checkpoint=checkpoint,
            )
        if query is not None:
            try:
                await require_current_read_eligibility(
                    self.lifecycle_reader, query, context
                )
            except LifecycleAuthorityError:
                raise ProviderReadDeniedError() from None
        return provider_page


class ConnectorIngestionPort(Protocol):
    async def ingest(
        self, command: ConnectorIngestionCommand
    ) -> ConnectorIngestionResult: ...


class YandexDiskLifecycleSourceReader(Protocol):
    async def list_sources(
        self,
        command: YandexDiskReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> tuple[YandexDiskLifecycleSource, ...]: ...


class YandexDiskTombstoneComposer(Protocol):
    async def compose(
        self,
        *,
        source: YandexDiskLifecycleSource,
        run: YandexDiskReconciliationRun,
        policy: YandexDiskRemovalPolicy,
        idempotency_key: str,
    ) -> SourceLifecycleCommand: ...


@dataclass(frozen=True, slots=True)
class YandexDiskReconciliationCoordinator:
    repository: YandexDiskReconciliationRepository
    scanner: YandexDiskScanPort
    ingestion: ConnectorIngestionPort
    sources: YandexDiskLifecycleSourceReader
    tombstones: YandexDiskTombstoneComposer
    source_lifecycle: SourceLifecycleCoordinator
    removal_policies: YandexDiskRemovalPolicyAdmission
    context_factory: Callable[
        [PersistenceOperation, UUID], TrustedPersistenceContext
    ]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def run(
        self,
        command: YandexDiskReconciliationCommand,
        policy: YandexDiskRemovalPolicy,
    ) -> YandexDiskReconciliationRun:
        policy = await self.removal_policies.admit(
            command,
            policy,
            self.context_factory(
                PersistenceOperation("yandex_disk_removal_policy_admission"),
                uuid5(NAMESPACE_URL, f"{command.idempotency_key}:removal-policy"),
            ),
        )
        self._validate_policy(command, policy)
        run_id = _run_id(command, policy)
        run = await self.repository.resolve(
            run_id, command.workspace_id, command.environment_id
        )
        if run is None:
            now = self._now()
            run = await self.repository.save(
                YandexDiskReconciliationRun(
                    run_id=run_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    connection_id=command.connection_id,
                    connection_version_id=command.connection_version_id,
                    resource_scope_reference=command.resource_scope_reference,
                    authorization_generation=command.authorization_generation,
                    configuration_generation=command.configuration_generation,
                    idempotency_key=command.idempotency_key,
                    removal_policy_version_id=policy.policy_version_id,
                    removal_policy_approval_id=policy.approval_id,
                    state=YandexDiskReconciliationState.RUNNING,
                    committed_checkpoint=command.last_committed_checkpoint,
                    next_checkpoint=command.last_committed_checkpoint,
                    started_at=now,
                    updated_at=now,
                )
            )
        if run.state is YandexDiskReconciliationState.COMPLETE:
            return run
        if (
            run.removal_policy_version_id,
            run.removal_policy_approval_id,
        ) != (policy.policy_version_id, policy.approval_id):
            raise ProviderReadDeniedError()

        if not run.baseline_captured:
            sources = await self.sources.list_sources(
                command,
                self.context_factory(
                    PersistenceOperation("yandex_disk_reconciliation_sources"),
                    uuid5(NAMESPACE_URL, f"{run_id}:sources"),
                ),
            )
            run = await self.repository.save(
                run.model_copy(
                    update={
                        "baseline_captured": True,
                        "baseline_sources": sources,
                        "updated_at": self._now(),
                    }
                )
            )
        else:
            sources = run.baseline_sources
        sources_by_reference = _current_sources_by_reference(sources)

        if (
            run.proposed_terminal_checkpoint is not None
            and run.pages
            and run.pages[-1].next_checkpoint is None
        ):
            run = await self.repository.save(
                run.model_copy(
                    update={
                        "state": YandexDiskReconciliationState.RECONCILING,
                        "updated_at": self._now(),
                    }
                )
            )

        while run.state is not YandexDiskReconciliationState.RECONCILING:
            try:
                page = await self.scanner.read_page(
                    command,
                    run.next_checkpoint,
                    self.context_factory(
                        PersistenceOperation("yandex_disk_reconciliation_scan"),
                        uuid5(NAMESPACE_URL, f"{run_id}:scan:{run.next_checkpoint}"),
                    ),
                )
                if page.requested_checkpoint != run.next_checkpoint:
                    raise YandexDiskIncompleteScanError()
                item_records: list[YandexDiskPageItemRecord] = []
                accepted_items = {
                    item.provider_object_reference: item
                    for accepted_page in run.pages
                    for item in accepted_page.items
                }
                for item in page.items:
                    accepted = accepted_items.get(item.provider_object_reference)
                    if accepted is not None:
                        if accepted.provider_revision != item.provider_revision:
                            raise YandexDiskIncompleteScanError()
                        item_records.append(accepted)
                        continue
                    result = await self.ingestion.ingest(
                        _item_command(
                            command,
                            run_id,
                            item,
                            prior_source=sources_by_reference.get(
                                item.provider_object_reference
                            ),
                        )
                    )
                    item_records.append(
                        YandexDiskPageItemRecord(
                            provider_object_reference=item.provider_object_reference,
                            provider_revision=item.provider_revision,
                            source_object_id=result.source.source_object_id,
                            source_revision_id=result.revision.revision_id,
                        )
                    )
            except (
                YandexDiskIncompleteScanError,
                YandexDiskContractError,
                YandexDiskThrottleError,
                YandexDiskTransientError,
                YandexDiskResourceNotFoundError,
                YandexDiskPermissionLostError,
                YandexDiskScopeOutOfScopeError,
                YandexDiskAuthorizationRevokedError,
            ) as exc:
                now = self._now()
                failure = _failure_lifecycle_evidence(
                    run=run,
                    policy=policy,
                    state=_failure_lifecycle_state(exc),
                    now=now,
                )
                return await self.repository.save(
                    run.model_copy(
                        update={
                            "state": YandexDiskReconciliationState.INTERRUPTED,
                            "lifecycle_evidence": _append_lifecycle_evidence(
                                run.lifecycle_evidence, failure
                            ),
                            "updated_at": now,
                        }
                    )
                )
            except Exception:
                now = self._now()
                return await self.repository.save(
                    run.model_copy(
                        update={
                            "state": YandexDiskReconciliationState.INTERRUPTED,
                            "updated_at": now,
                        }
                    )
                )
            now = self._now()
            record = YandexDiskPageRecord(
                page_identity=page.page_identity,
                requested_checkpoint=page.requested_checkpoint,
                checkpoint=page.checkpoint,
                next_checkpoint=page.next_checkpoint,
                items=tuple(item_records),
                coverage_entry_digests=page.coverage_entry_digests,
                expected_entry_count=page.expected_entry_count,
                confirmed_scan_generation=page.confirmed_scan_generation,
                accepted_at=now,
            )
            if page.next_checkpoint is None:
                try:
                    _validate_complete_coverage((*run.pages, record))
                except YandexDiskIncompleteScanError:
                    failure = _failure_lifecycle_evidence(
                        run=run,
                        policy=policy,
                        state=YandexDiskLifecycleState.AMBIGUOUS_ABSENCE,
                        now=now,
                    )
                    return await self.repository.save(
                        run.model_copy(
                            update={
                                "state": YandexDiskReconciliationState.INTERRUPTED,
                                "lifecycle_evidence": _append_lifecycle_evidence(
                                    run.lifecycle_evidence,
                                    failure,
                                ),
                                "updated_at": now,
                            }
                        )
                    )
                run = await self.repository.save(
                    run.model_copy(
                        update={
                            "state": YandexDiskReconciliationState.RECONCILING,
                            "proposed_terminal_checkpoint": page.checkpoint,
                            "pages": (*run.pages, record),
                            "updated_at": now,
                        }
                    )
                )
                break
            run = await self.repository.save(
                run.model_copy(
                    update={
                        "state": YandexDiskReconciliationState.RUNNING,
                        "committed_checkpoint": page.checkpoint,
                        "next_checkpoint": page.next_checkpoint,
                        "pages": (*run.pages, record),
                        "updated_at": now,
                    }
                )
            )

        # The final checkpoint covers scan-wide lifecycle effects too.  It is
        # committed only after that phase has finished successfully.
        run = await self._reconcile_lifecycle(command, policy, run, sources)
        if run.state is YandexDiskReconciliationState.INTERRUPTED:
            return run
        now = self._now()
        return await self.repository.save(
            run.model_copy(
                update={
                    "state": YandexDiskReconciliationState.COMPLETE,
                    "committed_checkpoint": run.proposed_terminal_checkpoint,
                    "next_checkpoint": None,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
        )

    async def _reconcile_lifecycle(
        self,
        command: YandexDiskReconciliationCommand,
        policy: YandexDiskRemovalPolicy,
        run: YandexDiskReconciliationRun,
        sources: tuple[YandexDiskLifecycleSource, ...],
    ) -> YandexDiskReconciliationRun:
        observed_by_reference: dict[str, YandexDiskPageItemRecord] = {}
        for page in run.pages:
            for item in page.items:
                previous = observed_by_reference.get(item.provider_object_reference)
                if previous is not None and previous.provider_revision != item.provider_revision:
                    raise ValueError("complete Yandex Disk scan has conflicting revisions")
                observed_by_reference[item.provider_object_reference] = item
        observed_items = tuple(observed_by_reference.values())
        observed_references = {
            item.provider_object_reference for item in observed_items
        }
        sources_by_reference = _current_sources_by_reference(sources)
        events = list(run.lifecycle_evidence)
        for item in observed_items:
            if not any(
                event.source_object_id == item.source_object_id
                and event.source_revision_id == item.source_revision_id
                and event.state
                in {
                    YandexDiskLifecycleState.OBSERVED,
                    YandexDiskLifecycleState.CHANGED,
                    YandexDiskLifecycleState.REAPPEARED,
                }
                for event in events
            ):
                prior = sources_by_reference.get(item.provider_object_reference)
                if prior is None:
                    state = YandexDiskLifecycleState.OBSERVED
                elif prior.current_revision_kind is RevisionKind.TOMBSTONE:
                    state = YandexDiskLifecycleState.REAPPEARED
                elif prior.current_provider_revision != item.provider_revision:
                    state = YandexDiskLifecycleState.CHANGED
                else:
                    state = YandexDiskLifecycleState.OBSERVED
                events.append(
                    _lifecycle_evidence(
                        run=run,
                        policy=policy,
                        state=state,
                        provider_object_reference=item.provider_object_reference,
                        source_object_id=item.source_object_id,
                        source_revision_id=item.source_revision_id,
                        predecessor_source_object_id=(
                            prior.source_object_id
                            if state is YandexDiskLifecycleState.REAPPEARED
                            else None
                        ),
                        predecessor_revision_id=(
                            prior.current_revision_id
                            if state is YandexDiskLifecycleState.REAPPEARED
                            else None
                        ),
                        canonical_effect_accepted=True,
                        now=self._now(),
                    )
                )

        for source in sources:
            if (
                source.provider_object_reference in observed_references
                or source.current_revision_kind is RevisionKind.TOMBSTONE
            ):
                continue
            existing = next(
                (
                    event
                    for event in events
                    if event.source_object_id == source.source_object_id
                    and event.state
                    in {
                        YandexDiskLifecycleState.CONFIRMED_REMOVED,
                        YandexDiskLifecycleState.AMBIGUOUS_ABSENCE,
                    }
                ),
                None,
            )
            if existing is not None and existing.canonical_effect_accepted:
                continue
            if not policy.authorize_complete_scope_absence:
                if existing is None:
                    events.append(
                        _lifecycle_evidence(
                            run=run,
                            policy=policy,
                            state=YandexDiskLifecycleState.AMBIGUOUS_ABSENCE,
                            provider_object_reference=source.provider_object_reference,
                            source_object_id=source.source_object_id,
                            source_revision_id=source.current_revision_id,
                            canonical_effect_accepted=False,
                            now=self._now(),
                        )
                    )
                continue

            evidence = existing or _lifecycle_evidence(
                run=run,
                policy=policy,
                state=YandexDiskLifecycleState.CONFIRMED_REMOVED,
                provider_object_reference=source.provider_object_reference,
                source_object_id=source.source_object_id,
                source_revision_id=source.current_revision_id,
                canonical_effect_accepted=False,
                now=self._now(),
            )
            if existing is None:
                events.append(evidence)
                run = await self.repository.save(
                    run.model_copy(
                        update={
                            "lifecycle_evidence": tuple(events),
                            "updated_at": self._now(),
                        }
                    )
                )
            idempotency_key = f"yandex-removal:{evidence.evidence_id.hex}"
            try:
                lifecycle_command = await self.tombstones.compose(
                    source=source,
                    run=run,
                    policy=policy,
                    idempotency_key=idempotency_key,
                )
                _validate_tombstone_command(
                    lifecycle_command, source, idempotency_key
                )
                result = await self.source_lifecycle.apply(lifecycle_command)
            except Exception:
                return await self.repository.save(
                    run.model_copy(
                        update={
                            "state": YandexDiskReconciliationState.INTERRUPTED,
                            "lifecycle_evidence": tuple(events),
                            "updated_at": self._now(),
                        }
                    )
                )
            applied = evidence.model_copy(
                update={
                    "tombstone_revision_id": result.acceptance.revision.revision_id,
                    "canonical_effect_accepted": True,
                }
            )
            events[events.index(evidence)] = applied
            run = await self.repository.save(
                run.model_copy(
                    update={
                        "state": YandexDiskReconciliationState.RECONCILING,
                        "lifecycle_evidence": tuple(events),
                        "updated_at": self._now(),
                    }
                )
            )

        if run.lifecycle_evidence != tuple(events):
            run = await self.repository.save(
                run.model_copy(
                    update={
                        "lifecycle_evidence": tuple(events),
                        "updated_at": self._now(),
                    }
                )
            )
        return run

    @staticmethod
    def _validate_policy(
        command: YandexDiskReconciliationCommand,
        policy: YandexDiskRemovalPolicy,
    ) -> None:
        if (
            policy.workspace_id,
            policy.environment_id,
            policy.connection_id,
            policy.resource_scope_reference,
        ) != (
            command.workspace_id,
            command.environment_id,
            command.connection_id,
            command.resource_scope_reference,
        ):
            raise ValueError("Yandex Disk removal policy scope does not match scan")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Yandex Disk reconciliation clock must be timezone-aware")
        return value


def _run_id(
    command: YandexDiskReconciliationCommand,
    policy: YandexDiskRemovalPolicy,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:yandex-disk-reconciliation:v1:"
        f"{command.workspace_id}:{command.environment_id}:{command.connection_id}:"
        f"{command.connection_version_id}:{command.resource_scope_reference}:"
        f"{command.authorization_generation}:{command.configuration_generation}:"
        f"{policy.policy_version_id}:{policy.approval_id}:{command.idempotency_key}",
    )


def _current_sources_by_reference(
    sources: tuple[YandexDiskLifecycleSource, ...],
) -> dict[str, YandexDiskLifecycleSource]:
    grouped: dict[str, list[YandexDiskLifecycleSource]] = {}
    for source in sources:
        grouped.setdefault(source.provider_object_reference, []).append(source)
    current: dict[str, YandexDiskLifecycleSource] = {}
    for reference, candidates in grouped.items():
        live = [
            source
            for source in candidates
            if source.current_revision_kind is RevisionKind.CONTENT
        ]
        if len(live) > 1:
            raise ValueError("Yandex Disk path maps to multiple current sources")
        current[reference] = live[0] if live else candidates[-1]
    return current


def _validate_scan_connection(
    command: ProviderReadCommand,
    connection: ProviderConnection,
) -> None:
    if (
        connection.status is not ProviderConnectionStatus.ACTIVE
        or connection.provider_kind is not ProviderKind.YANDEX_DISK
        or (
            connection.workspace_id,
            connection.environment_id,
            connection.connection_id,
            connection.connection_version_id,
            connection.authorization_generation,
            connection.configuration_generation,
        )
        != (
            command.workspace_id,
            command.environment_id,
            command.connection_id,
            command.connection_version_id,
            command.authorization_generation,
            command.configuration_generation,
        )
        or command.resource_scope_reference
        not in connection.approved_resource_scope_references
    ):
        raise ProviderReadDeniedError()


def _item_command(
    command: YandexDiskReconciliationCommand,
    run_id: UUID,
    item: YandexDiskScanItem,
    *,
    prior_source: YandexDiskLifecycleSource | None,
) -> ConnectorIngestionCommand:
    digest = hashlib.sha256(
        f"{item.provider_object_reference}\0{item.provider_revision}".encode()
    ).hexdigest()
    lifecycle_query = None
    if command.lifecycle_query is not None:
        external_object_id = "ydx:" + hashlib.sha256(
            f"{command.connection_id}|{item.provider_object_reference}".encode()
        ).hexdigest()[:60]
        lifecycle_query = lifecycle_query_for_source(
            command.lifecycle_query,
            provider_object_reference=item.provider_object_reference,
            external_namespace="yandex-disk",
            external_generation="yandex-disk-path:v1",
            external_object_id=external_object_id,
        )
    return ConnectorIngestionCommand(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        command_identity=f"ydx.reconcile.{digest[:48]}",
        idempotency_key=f"yandex-reconciliation:{run_id.hex}:item:{digest}",
        connection_id=command.connection_id,
        resource_scope_reference=command.resource_scope_reference,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        resource_object_reference=item.provider_object_reference,
        external_identity_occurrence=(
            run_id.hex
            if prior_source is not None
            and prior_source.current_revision_kind is RevisionKind.TOMBSTONE
            else None
        ),
        access_policy_version_id=command.access_policy_version_id,
        lifecycle_query=lifecycle_query,
    )


def _lifecycle_evidence(
    *,
    run: YandexDiskReconciliationRun,
    policy: YandexDiskRemovalPolicy,
    state: YandexDiskLifecycleState,
    provider_object_reference: str,
    source_object_id: UUID | None,
    source_revision_id: UUID | None,
    canonical_effect_accepted: bool,
    now: datetime,
    predecessor_source_object_id: UUID | None = None,
    predecessor_revision_id: UUID | None = None,
) -> YandexDiskLifecycleEvidence:
    if run.proposed_terminal_checkpoint is None:
        raise ValueError("lifecycle evidence requires a complete terminal page")
    reference_digest = hashlib.sha256(provider_object_reference.encode()).hexdigest()
    evidence_id = uuid5(
        NAMESPACE_URL,
        "spine:yandex-disk-lifecycle-evidence:v1:"
        f"{run.run_id}:{policy.policy_version_id}:{state.value}:{reference_digest}:"
        f"{source_object_id}:{source_revision_id}",
    )
    evidence_reference = f"yandex-reconciliation:{evidence_id.hex}"
    evidence_digest = hashlib.sha256(
        "|".join(
            (
                str(run.run_id),
                str(policy.policy_version_id),
                state.value,
                reference_digest,
                str(source_object_id),
                str(source_revision_id),
                run.proposed_terminal_checkpoint,
            )
        ).encode()
    ).hexdigest()
    return YandexDiskLifecycleEvidence(
        evidence_id=evidence_id,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        connection_id=run.connection_id,
        resource_scope_reference=run.resource_scope_reference,
        policy_version_id=policy.policy_version_id,
        committed_checkpoint=run.committed_checkpoint,
        proposed_terminal_checkpoint=run.proposed_terminal_checkpoint,
        state=state,
        provider_object_reference_digest=reference_digest,
        source_object_id=source_object_id,
        source_revision_id=source_revision_id,
        predecessor_source_object_id=predecessor_source_object_id,
        predecessor_revision_id=predecessor_revision_id,
        identity_continuity_proven=False,
        canonical_effect_accepted=canonical_effect_accepted,
        evidence_reference=evidence_reference,
        evidence_digest=evidence_digest,
        observed_at=now,
    )


def _failure_lifecycle_state(exc: Exception) -> YandexDiskLifecycleState:
    if isinstance(exc, (YandexDiskThrottleError, YandexDiskTransientError)):
        return YandexDiskLifecycleState.TEMPORARILY_UNAVAILABLE
    if isinstance(exc, YandexDiskPermissionLostError):
        return YandexDiskLifecycleState.ACCESS_LOST
    if isinstance(exc, YandexDiskScopeOutOfScopeError):
        return YandexDiskLifecycleState.OUT_OF_SCOPE
    if isinstance(exc, YandexDiskAuthorizationRevokedError):
        return YandexDiskLifecycleState.CONNECTION_REVOKED
    return YandexDiskLifecycleState.AMBIGUOUS_ABSENCE


def _failure_lifecycle_evidence(
    *,
    run: YandexDiskReconciliationRun,
    policy: YandexDiskRemovalPolicy,
    state: YandexDiskLifecycleState,
    now: datetime,
) -> YandexDiskLifecycleEvidence:
    scope_reference = f"scope:{run.resource_scope_reference}"
    reference_digest = hashlib.sha256(scope_reference.encode()).hexdigest()
    evidence_id = uuid5(
        NAMESPACE_URL,
        "spine:yandex-disk-scan-failure:v1:"
        f"{run.run_id}:{state.value}:{run.next_checkpoint}:{len(run.pages)}",
    )
    digest = hashlib.sha256(
        f"{evidence_id}|{state.value}|{reference_digest}".encode()
    ).hexdigest()
    return YandexDiskLifecycleEvidence(
        evidence_id=evidence_id,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        connection_id=run.connection_id,
        resource_scope_reference=run.resource_scope_reference,
        policy_version_id=policy.policy_version_id,
        committed_checkpoint=run.committed_checkpoint,
        proposed_terminal_checkpoint=run.proposed_terminal_checkpoint,
        state=state,
        provider_object_reference_digest=reference_digest,
        identity_continuity_proven=False,
        canonical_effect_accepted=False,
        evidence_reference=f"yandex-reconciliation:{evidence_id.hex}",
        evidence_digest=digest,
        observed_at=now,
    )


def _append_lifecycle_evidence(
    existing: tuple[YandexDiskLifecycleEvidence, ...],
    evidence: YandexDiskLifecycleEvidence,
) -> tuple[YandexDiskLifecycleEvidence, ...]:
    if any(item.evidence_id == evidence.evidence_id for item in existing):
        return existing
    return (*existing, evidence)


def _validate_complete_coverage(pages: tuple[YandexDiskPageRecord, ...]) -> None:
    expected_counts = {
        page.expected_entry_count
        for page in pages
        if page.expected_entry_count is not None
    }
    coverage = [digest for page in pages for digest in page.coverage_entry_digests]
    if len(expected_counts) != 1 or None in expected_counts:
        raise YandexDiskIncompleteScanError()
    expected = next(iter(expected_counts))
    terminal_generation = pages[-1].confirmed_scan_generation
    if (
        len(coverage) != expected
        or len(set(coverage)) != expected
        or terminal_generation is None
    ):
        raise YandexDiskIncompleteScanError()


def _validate_tombstone_command(
    command: SourceLifecycleCommand,
    source: YandexDiskLifecycleSource,
    idempotency_key: str,
) -> None:
    if command.kind is not SourceLifecycleKind.DELETION:
        raise ValueError("Yandex Disk tombstone composer must produce deletion")
    if (
        command.source.source_object_id != source.source_object_id
        or command.canonical_acceptance.expected_current_revision_id
        != source.current_revision_id
        or command.canonical_acceptance.idempotency_key.value
        != idempotency_key
        or command.observation_idempotency_key.value
        != idempotency_key
    ):
        raise ValueError("Yandex Disk tombstone command does not preserve evidence")


__all__ = [
    "AuthorizedYandexDiskScanner",
    "InMemoryYandexDiskReconciliationRepository",
    "InMemoryYandexDiskRemovalPolicyAdmission",
    "YandexDiskReconciliationCommand",
    "YandexDiskReconciliationCoordinator",
    "YandexDiskReconciliationRepository",
    "YandexDiskReconciliationRun",
    "YandexDiskReconciliationState",
    "YandexDiskLifecycleEvidence",
    "YandexDiskLifecycleSource",
    "YandexDiskLifecycleState",
    "YandexDiskIncompleteScanError",
    "YandexDiskRemovalPolicy",
    "YandexDiskRemovalPolicyAdmission",
    "YandexDiskScanItem",
    "YandexDiskScanPage",
]
