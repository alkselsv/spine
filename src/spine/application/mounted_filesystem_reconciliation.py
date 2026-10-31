"""Conservative reconciliation for path-scoped mounted filesystem sources."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.application.connector_ingestion import ConnectorIngestionCommand
from spine.application.document_lifecycle import (
    ServerDocumentLifecycleComposer,
    TombstoneDocumentCommand,
)
from spine.application.persistence.command_digest import digest_command
from spine.application.lifecycle import (
    CurrentLifecycleEligibilityReader,
    LifecycleAuthorityError,
    LifecycleEligibilityQuery,
    require_current_read_eligibility,
    lifecycle_query_for_source,
)
from spine.application.connections.provider import (
    ProviderCapabilityUnavailableError,
    ProviderConnectionRepository,
    ProviderReadCommand,
    ProviderReadDeniedError,
    ProviderReadError,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import (
    ConstraintConflictError,
    OptimisticConflictError,
)
from spine.application.source_lifecycle import (
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.sources import RevisionKind
from spine.domain.sources.lifecycle import mounted_filesystem_source_identity


class MountedFilesystemUnavailableError(RuntimeError):
    """The configured root cannot currently be inspected."""

    def __init__(self) -> None:
        super().__init__("Mounted filesystem is temporarily unavailable.")


class MountedFilesystemIncompleteScanError(RuntimeError):
    """The root was reachable but complete coverage was not proven."""

    def __init__(self) -> None:
        super().__init__("Mounted filesystem scan coverage is incomplete.")


class MountedFilesystemReconciliationState(str, Enum):
    RUNNING = "running"
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


class MountedFilesystemLifecycleState(str, Enum):
    OBSERVED = "observed"
    CHANGED = "changed"
    CONFIRMED_ABSENT = "confirmed_absent"
    AMBIGUOUS_ABSENCE = "ambiguous_absence"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    QUARANTINED = "quarantined"
    REAPPEARED = "reappeared"


class MountedFilesystemScanItem(DefinitionModel):
    provider_object_reference: str = Field(min_length=1, max_length=1024)
    read_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class MountedFilesystemScanSnapshot(DefinitionModel):
    items: tuple[MountedFilesystemScanItem, ...]
    coverage_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_items(self) -> "MountedFilesystemScanSnapshot":
        paths = [item.provider_object_reference for item in self.items]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("mounted filesystem scan items must be unique and sorted")
        return self


class MountedFilesystemCanonicalSource(DefinitionModel):
    provider_object_reference: str = Field(min_length=1, max_length=1024)
    source_object_id: UUID
    current_revision_id: UUID
    current_revision_kind: RevisionKind
    identity_occurrence: str | None = Field(default=None, min_length=1, max_length=64)


class MountedFilesystemAcceptedItem(DefinitionModel):
    provider_object_reference: str = Field(min_length=1, max_length=1024)
    read_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_object_id: UUID
    source_revision_id: UUID


class MountedFilesystemLifecycleEvidence(DefinitionModel):
    evidence_id: UUID
    state: MountedFilesystemLifecycleState
    provider_object_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    predecessor_source_object_id: UUID | None = None
    predecessor_revision_id: UUID | None = None
    tombstone_revision_id: UUID | None = None
    identity_continuity_proven: bool = False
    canonical_effect_accepted: bool = False
    evidence_reference: str = Field(min_length=1, max_length=128)
    observed_at: datetime


class MountedFilesystemReconciliationCommand(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    access_policy_version_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    lifecycle_query: LifecycleEligibilityQuery | None = None

    @model_validator(mode="after")
    def validate_ids(self) -> "MountedFilesystemReconciliationCommand":
        for field_name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "connection_version_id",
            "resource_scope_reference",
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
            raise ValueError("lifecycle query must bind the exact mounted root scope")
        return self


class MountedFilesystemRemovalPolicy(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_version_id: UUID
    approval_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorize_complete_scope_absence: bool = False
    approved_at: datetime

    @model_validator(mode="after")
    def validate_policy(self) -> "MountedFilesystemRemovalPolicy":
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


class MountedFilesystemReconciliationRun(DefinitionModel):
    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: UUID
    idempotency_key: str
    removal_policy_version_id: UUID
    removal_policy_approval_id: UUID
    state: MountedFilesystemReconciliationState
    connection_available: bool
    baseline_captured: bool = False
    baseline_sources: tuple[MountedFilesystemCanonicalSource, ...] = ()
    accepted_items: tuple[MountedFilesystemAcceptedItem, ...] = ()
    lifecycle_evidence: tuple[MountedFilesystemLifecycleEvidence, ...] = ()
    confirmed_scan_generation: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class MountedFilesystemReconciliationRepository(Protocol):
    async def resolve(
        self, run_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> MountedFilesystemReconciliationRun | None: ...

    async def save(
        self, run: MountedFilesystemReconciliationRun
    ) -> MountedFilesystemReconciliationRun: ...


class InMemoryMountedFilesystemReconciliationRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, MountedFilesystemReconciliationRun] = {}

    async def resolve(
        self, run_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> MountedFilesystemReconciliationRun | None:
        run = self.records.get(run_id)
        if run is not None and (run.workspace_id, run.environment_id) != (
            workspace_id,
            environment_id,
        ):
            return None
        return run

    async def save(
        self, run: MountedFilesystemReconciliationRun
    ) -> MountedFilesystemReconciliationRun:
        current = self.records.get(run.run_id)
        if current is not None and (
            current.workspace_id,
            current.environment_id,
            current.connection_id,
            current.connection_version_id,
            current.resource_scope_reference,
            current.idempotency_key,
            current.removal_policy_version_id,
            current.removal_policy_approval_id,
        ) != (
            run.workspace_id,
            run.environment_id,
            run.connection_id,
            run.connection_version_id,
            run.resource_scope_reference,
            run.idempotency_key,
            run.removal_policy_version_id,
            run.removal_policy_approval_id,
        ):
            raise ValueError("mounted filesystem reconciliation replay conflicts")
        self.records[run.run_id] = run
        return run


class MountedFilesystemScanProvider(Protocol):
    async def scan(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
    ) -> MountedFilesystemScanSnapshot: ...


class MountedFilesystemRemovalPolicyAdmission(Protocol):
    async def admit(
        self,
        command: MountedFilesystemReconciliationCommand,
        requested_policy: MountedFilesystemRemovalPolicy,
        context: TrustedPersistenceContext,
    ) -> MountedFilesystemRemovalPolicy: ...


class InMemoryMountedFilesystemRemovalPolicyAdmission:
    """Immutable approved-policy fake used by application and contract tests."""

    def __init__(self, policies: tuple[MountedFilesystemRemovalPolicy, ...]) -> None:
        self._policies = {policy.policy_version_id: policy for policy in policies}

    async def admit(
        self,
        command: MountedFilesystemReconciliationCommand,
        requested_policy: MountedFilesystemRemovalPolicy,
        context: TrustedPersistenceContext,
    ) -> MountedFilesystemRemovalPolicy:
        del command, context
        approved = self._policies.get(requested_policy.policy_version_id)
        if approved != requested_policy:
            raise ProviderReadDeniedError()
        return approved


class MountedFilesystemScanPort(Protocol):
    async def read_snapshot(
        self,
        command: MountedFilesystemReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> MountedFilesystemScanSnapshot: ...


@dataclass(frozen=True, slots=True)
class AuthorizedMountedFilesystemScanner:
    repository: ProviderConnectionRepository
    provider: MountedFilesystemScanProvider
    lifecycle_reader: CurrentLifecycleEligibilityReader | None = None

    async def read_snapshot(
        self,
        command: MountedFilesystemReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> MountedFilesystemScanSnapshot:
        query = command.lifecycle_query
        if query is None or self.lifecycle_reader is None:
            raise ProviderReadDeniedError()
        else:
            try:
                await require_current_read_eligibility(self.lifecycle_reader, query, context)
            except LifecycleAuthorityError:
                raise ProviderReadDeniedError() from None
        required = (
            ProviderCapabilityKind.ENUMERATION,
            ProviderCapabilityKind.BINARY_READ,
            ProviderCapabilityKind.FULL_RECONCILIATION,
            ProviderCapabilityKind.LIFECYCLE_EVIDENCE,
        )
        request = ProviderReadCommand(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            connection_version_id=command.connection_version_id,
            resource_scope_reference=OpaqueProviderReference(
                value=command.resource_scope_reference
            ),
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            required_capabilities=required,
            resource_object_reference=(
                query.provider_object_reference if query is not None else None
            ),
            lifecycle_query=query,
        )
        async with self.repository.admit_read(request, context) as connection:
            if (
                connection.status is not ProviderConnectionStatus.ACTIVE
                or connection.provider_kind is not ProviderKind.MOUNTED_FILESYSTEM
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
                or request.resource_scope_reference
                not in connection.approved_resource_scope_references
                or query.scope_version_id != connection.approval_id
            ):
                raise ProviderReadDeniedError()
            for capability in required:
                if not connection.supports(capability):
                    raise ProviderCapabilityUnavailableError(capability)
            snapshot = await self.provider.scan(
                connection, request.resource_scope_reference
            )
        if query is not None:
            try:
                await require_current_read_eligibility(self.lifecycle_reader, query, context)
            except LifecycleAuthorityError:
                raise ProviderReadDeniedError() from None
        return snapshot


class MountedFilesystemIngestionPort(Protocol):
    async def ingest(self, command: ConnectorIngestionCommand): ...


class MountedFilesystemSourceReader(Protocol):
    async def list_sources(
        self,
        command: MountedFilesystemReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> tuple[MountedFilesystemCanonicalSource, ...]: ...


class MountedFilesystemRemovalPort(Protocol):
    async def confirm_absent(
        self,
        command: MountedFilesystemReconciliationCommand,
        source: MountedFilesystemCanonicalSource,
        *,
        policy: MountedFilesystemRemovalPolicy,
        idempotency_key: str,
        evidence_reference: str,
        observed_at: datetime,
        context: TrustedPersistenceContext,
    ) -> UUID: ...


class MountedFilesystemTombstoneComposer(Protocol):
    async def compose(
        self,
        *,
        command: MountedFilesystemReconciliationCommand,
        source: MountedFilesystemCanonicalSource,
        policy: MountedFilesystemRemovalPolicy,
        idempotency_key: str,
        evidence_reference: str,
        observed_at: datetime,
        context: TrustedPersistenceContext,
    ) -> SourceLifecycleCommand: ...


@dataclass(frozen=True, slots=True)
class MountedFilesystemSourceLifecycleRemoval:
    """Apply a verified absence through the canonical lifecycle boundary."""

    composer: MountedFilesystemTombstoneComposer
    lifecycle: SourceLifecycleCoordinator

    async def confirm_absent(
        self,
        command: MountedFilesystemReconciliationCommand,
        source: MountedFilesystemCanonicalSource,
        *,
        policy: MountedFilesystemRemovalPolicy,
        idempotency_key: str,
        evidence_reference: str,
        observed_at: datetime,
        context: TrustedPersistenceContext,
    ) -> UUID:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (command.workspace_id, command.environment_id):
            raise ProviderReadDeniedError()
        existing = await self._resolve_existing_tombstone(
            command,
            source,
            policy=policy,
            idempotency_key=idempotency_key,
            context=context,
        )
        if existing is not None:
            return existing
        try:
            lifecycle_command = await self.composer.compose(
                command=command,
                source=source,
                policy=policy,
                idempotency_key=idempotency_key,
                evidence_reference=evidence_reference,
                observed_at=observed_at,
                context=context,
            )
            _validate_tombstone_command(lifecycle_command, source, idempotency_key)
            result = await self.lifecycle.apply(lifecycle_command)
        except (ConstraintConflictError, OptimisticConflictError):
            existing = await self._resolve_existing_tombstone(
                command,
                source,
                policy=policy,
                idempotency_key=idempotency_key,
                context=context,
            )
            if existing is None:
                raise
            return existing
        return result.acceptance.revision.revision_id

    async def _resolve_existing_tombstone(
        self,
        command: MountedFilesystemReconciliationCommand,
        source: MountedFilesystemCanonicalSource,
        *,
        policy: MountedFilesystemRemovalPolicy,
        idempotency_key: str,
        context: TrustedPersistenceContext,
    ) -> UUID | None:
        expected_revision_id = _tombstone_revision_id(
            idempotency_key, policy=policy
        )
        operation_id = _tombstone_operation_id(idempotency_key, policy=policy)
        expected_provenance_id = uuid5(
            NAMESPACE_URL, f"spine:document-lifecycle:provenance:{operation_id}"
        )
        lifecycle = _tombstone_document_command(command, source, idempotency_key)
        expected_digest = digest_command(
            operation=PersistenceOperation("mounted_filesystem_confirm_absent"),
            operation_schema_version=1,
            payload=lifecycle.model_dump(mode="json"),
        ).value
        async with self.lifecycle.uow_factory(context) as uow:
            current = await uow.canonical.resolve_current(source.source_object_id)
            revision = await uow.sources.resolve_revision(expected_revision_id)
            acceptance = await uow.canonical.resolve_acceptance(operation_id)
            provenances = await uow.sources.list_provenance(expected_revision_id)
            await uow.rollback()
        provenance = next(
            (
                item
                for item in provenances
                if item.provenance_id == expected_provenance_id
            ),
            None,
        )
        if (
            current is None
            or revision is None
            or acceptance is None
            or provenance is None
            or current.current_revision_id != expected_revision_id
            or current.acceptance_id != operation_id
            or (current.workspace_id, current.environment_id)
            != (command.workspace_id, command.environment_id)
            or revision.source_object_id != source.source_object_id
            or (revision.workspace_id, revision.environment_id)
            != (command.workspace_id, command.environment_id)
            or revision.kind is not RevisionKind.TOMBSTONE
            or revision.deletion_reason != "provider_deleted"
            or revision.deletion_provenance != "document-lifecycle"
            or acceptance.canonical_acceptance_id != operation_id
            or acceptance.source_object_id != source.source_object_id
            or acceptance.source_revision_id != expected_revision_id
            or acceptance.predecessor_revision_id != source.current_revision_id
            or (acceptance.workspace_id, acceptance.environment_id)
            != (command.workspace_id, command.environment_id)
            or acceptance.provenance_id != expected_provenance_id
            or provenance.source_object_id != source.source_object_id
            or provenance.revision_id != expected_revision_id
            or (provenance.workspace_id, provenance.environment_id)
            != (command.workspace_id, command.environment_id)
            or provenance.producer_kind != "lifecycle"
            or provenance.producer_reference != "document-lifecycle"
            or provenance.observer_service != "document-lifecycle"
            or provenance.event_digest != expected_digest
            or provenance.received_at != revision.observed_at
            or provenance.observed_at != revision.observed_at
            or acceptance.accepted_at != revision.observed_at
        ):
            return None
        return expected_revision_id


@dataclass(frozen=True, slots=True)
class ServerMountedFilesystemTombstoneComposer:
    """Adapt the canonical document lifecycle composer to mounted absences."""

    lifecycle_composer: ServerDocumentLifecycleComposer

    async def compose(
        self,
        *,
        command: MountedFilesystemReconciliationCommand,
        source: MountedFilesystemCanonicalSource,
        policy: MountedFilesystemRemovalPolicy,
        idempotency_key: str,
        evidence_reference: str,
        observed_at: datetime,
        context: TrustedPersistenceContext,
    ) -> SourceLifecycleCommand:
        if not policy.authorize_complete_scope_absence:
            raise ProviderReadDeniedError()
        lifecycle = _tombstone_document_command(command, source, idempotency_key)
        operation_id = _tombstone_operation_id(idempotency_key, policy=policy)
        del evidence_reference
        return await self.lifecycle_composer.compose_tombstone(
            context=context,
            command=lifecycle,
            operation_id=operation_id,
            command_digest=digest_command(
                operation=PersistenceOperation("mounted_filesystem_confirm_absent"),
                operation_schema_version=1,
                payload=lifecycle.model_dump(mode="json"),
            ),
            accepted_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class MountedFilesystemReconciliationCoordinator:
    repository: MountedFilesystemReconciliationRepository
    scanner: MountedFilesystemScanPort
    ingestion: MountedFilesystemIngestionPort
    sources: MountedFilesystemSourceReader
    removals: MountedFilesystemRemovalPort
    removal_policies: MountedFilesystemRemovalPolicyAdmission
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_read_attempts: int = 2

    def __post_init__(self) -> None:
        if self.max_read_attempts < 1 or self.max_read_attempts > 10:
            raise ValueError("mounted filesystem read attempts must be bounded")

    async def run(
        self,
        command: MountedFilesystemReconciliationCommand,
        policy: MountedFilesystemRemovalPolicy,
    ) -> MountedFilesystemReconciliationRun:
        policy = await self.removal_policies.admit(
            command,
            policy,
            self.context_factory(
                PersistenceOperation("mounted_filesystem_removal_policy_admission"),
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
                MountedFilesystemReconciliationRun(
                    run_id=run_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    connection_id=command.connection_id,
                    connection_version_id=command.connection_version_id,
                    resource_scope_reference=command.resource_scope_reference,
                    idempotency_key=command.idempotency_key,
                    removal_policy_version_id=policy.policy_version_id,
                    removal_policy_approval_id=policy.approval_id,
                    state=MountedFilesystemReconciliationState.RUNNING,
                    connection_available=True,
                    started_at=now,
                    updated_at=now,
                )
            )
        if run.state is MountedFilesystemReconciliationState.COMPLETE:
            return run
        if not run.baseline_captured:
            baseline = await self.sources.list_sources(
                command,
                self.context_factory(
                    PersistenceOperation("mounted_filesystem_sources"),
                    uuid5(NAMESPACE_URL, f"{run_id}:sources"),
                ),
            )
            run = await self.repository.save(
                run.model_copy(
                    update={
                        "baseline_captured": True,
                        "baseline_sources": baseline,
                        "updated_at": self._now(),
                    }
                )
            )

        try:
            snapshot = await self._scan(command, run_id, "initial")
        except MountedFilesystemUnavailableError:
            return await self._unavailable(run)
        except MountedFilesystemIncompleteScanError:
            return await self._incomplete(run)

        accepted_by_key = {
            (item.provider_object_reference, item.read_token): item
            for item in run.accepted_items
        }
        accepted: list[MountedFilesystemAcceptedItem] = []
        baseline_by_path = _sources_by_path(run.baseline_sources)
        for item in snapshot.items:
            record = accepted_by_key.get(
                (item.provider_object_reference, item.read_token)
            )
            if record is None:
                prior = baseline_by_path.get(item.provider_object_reference)
                occurrence = None
                if prior is not None:
                    occurrence = (
                        run_id.hex
                        if prior.current_revision_kind is RevisionKind.TOMBSTONE
                        else prior.identity_occurrence
                    )
                for _ in range(self.max_read_attempts):
                    try:
                        result = await self.ingestion.ingest(
                            _item_command(command, item, occurrence)
                        )
                        record = MountedFilesystemAcceptedItem(
                            provider_object_reference=item.provider_object_reference,
                            read_token=item.read_token,
                            source_object_id=result.source.source_object_id,
                            source_revision_id=result.revision.revision_id,
                        )
                        break
                    except ProviderReadError:
                        continue
            if record is None:
                evidence = _evidence(
                    run,
                    MountedFilesystemLifecycleState.QUARANTINED,
                    item.provider_object_reference,
                    now=self._now(),
                )
                return await self.repository.save(
                    run.model_copy(
                        update={
                            "state": MountedFilesystemReconciliationState.INCOMPLETE,
                            "connection_available": True,
                            "accepted_items": tuple(accepted),
                            "lifecycle_evidence": _append_evidence(
                                run.lifecycle_evidence, evidence
                            ),
                            "confirmed_scan_generation": None,
                            "updated_at": self._now(),
                        }
                    )
                )
            accepted.append(record)

        try:
            verified = await self._scan(command, run_id, "verify")
        except MountedFilesystemUnavailableError:
            return await self._unavailable(run)
        except MountedFilesystemIncompleteScanError:
            return await self._incomplete(run)
        if verified != snapshot:
            return await self._incomplete(run)

        run = await self.repository.save(
            run.model_copy(
                update={
                    "state": MountedFilesystemReconciliationState.RUNNING,
                    "connection_available": True,
                    "accepted_items": tuple(accepted),
                    "confirmed_scan_generation": snapshot.coverage_digest,
                    "updated_at": self._now(),
                }
            )
        )
        evidence = list(run.lifecycle_evidence)
        observed_paths: set[str] = set()
        for item in accepted:
            observed_paths.add(item.provider_object_reference)
            prior = baseline_by_path.get(item.provider_object_reference)
            if prior is None:
                state = MountedFilesystemLifecycleState.OBSERVED
            elif prior.current_revision_kind is RevisionKind.TOMBSTONE:
                state = MountedFilesystemLifecycleState.REAPPEARED
            elif prior.current_revision_id != item.source_revision_id:
                state = MountedFilesystemLifecycleState.CHANGED
            else:
                state = MountedFilesystemLifecycleState.OBSERVED
            evidence.append(
                _evidence(
                    run,
                    state,
                    item.provider_object_reference,
                    source_object_id=item.source_object_id,
                    source_revision_id=item.source_revision_id,
                    predecessor_source_object_id=(
                        prior.source_object_id
                        if state is MountedFilesystemLifecycleState.REAPPEARED
                        else None
                    ),
                    predecessor_revision_id=(
                        prior.current_revision_id
                        if state is MountedFilesystemLifecycleState.REAPPEARED
                        else None
                    ),
                    accepted=True,
                    now=self._now(),
                )
            )

        for source in run.baseline_sources:
            if (
                source.provider_object_reference in observed_paths
                or source.current_revision_kind is RevisionKind.TOMBSTONE
            ):
                continue
            state = (
                MountedFilesystemLifecycleState.CONFIRMED_ABSENT
                if policy.authorize_complete_scope_absence
                else MountedFilesystemLifecycleState.AMBIGUOUS_ABSENCE
            )
            item_evidence = _evidence(
                run,
                state,
                source.provider_object_reference,
                source_object_id=source.source_object_id,
                source_revision_id=source.current_revision_id,
                accepted=False,
                now=self._now(),
            )
            if policy.authorize_complete_scope_absence:
                tombstone_id = await self.removals.confirm_absent(
                    command,
                    source,
                    policy=policy,
                    idempotency_key=_removal_idempotency_key(command, policy, source),
                    evidence_reference=item_evidence.evidence_reference,
                    observed_at=item_evidence.observed_at,
                    context=self.context_factory(
                        PersistenceOperation("mounted_filesystem_confirm_absent"),
                        item_evidence.evidence_id,
                    ),
                )
                item_evidence = item_evidence.model_copy(
                    update={
                        "tombstone_revision_id": tombstone_id,
                        "canonical_effect_accepted": True,
                    }
                )
            evidence.append(item_evidence)

        now = self._now()
        return await self.repository.save(
            run.model_copy(
                update={
                    "state": MountedFilesystemReconciliationState.COMPLETE,
                    "lifecycle_evidence": _deduplicate_evidence(tuple(evidence)),
                    "updated_at": now,
                    "completed_at": now,
                }
            )
        )

    async def _scan(
        self,
        command: MountedFilesystemReconciliationCommand,
        run_id: UUID,
        phase: str,
    ) -> MountedFilesystemScanSnapshot:
        return await self.scanner.read_snapshot(
            command,
            self.context_factory(
                PersistenceOperation("mounted_filesystem_scan"),
                uuid5(NAMESPACE_URL, f"{run_id}:scan:{phase}"),
            ),
        )

    async def _unavailable(
        self, run: MountedFilesystemReconciliationRun
    ) -> MountedFilesystemReconciliationRun:
        evidence = _evidence(
            run,
            MountedFilesystemLifecycleState.TEMPORARILY_UNAVAILABLE,
            None,
            now=self._now(),
        )
        return await self.repository.save(
            run.model_copy(
                update={
                    "state": MountedFilesystemReconciliationState.UNAVAILABLE,
                    "connection_available": False,
                    "confirmed_scan_generation": None,
                    "lifecycle_evidence": _append_evidence(
                        run.lifecycle_evidence, evidence
                    ),
                    "updated_at": self._now(),
                }
            )
        )

    async def _incomplete(
        self, run: MountedFilesystemReconciliationRun
    ) -> MountedFilesystemReconciliationRun:
        return await self.repository.save(
            run.model_copy(
                update={
                    "state": MountedFilesystemReconciliationState.INCOMPLETE,
                    "connection_available": True,
                    "confirmed_scan_generation": None,
                    "updated_at": self._now(),
                }
            )
        )

    @staticmethod
    def _validate_policy(
        command: MountedFilesystemReconciliationCommand,
        policy: MountedFilesystemRemovalPolicy,
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
            raise ProviderReadDeniedError()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mounted filesystem reconciliation clock must be aware")
        return value


def _run_id(
    command: MountedFilesystemReconciliationCommand,
    policy: MountedFilesystemRemovalPolicy,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:mounted-filesystem-reconciliation:v1:"
        f"{command.workspace_id}:{command.environment_id}:{command.connection_id}:"
        f"{command.connection_version_id}:{command.resource_scope_reference}:"
        f"{policy.policy_version_id}:{policy.approval_id}:{command.idempotency_key}",
    )


def _sources_by_path(
    sources: tuple[MountedFilesystemCanonicalSource, ...],
) -> dict[str, MountedFilesystemCanonicalSource]:
    result: dict[str, MountedFilesystemCanonicalSource] = {}
    for source in sources:
        existing = result.get(source.provider_object_reference)
        if (
            existing is not None
            and existing.current_revision_kind is RevisionKind.CONTENT
            and source.current_revision_kind is RevisionKind.CONTENT
        ):
            raise ValueError("mounted path maps to multiple current sources")
        if existing is None or source.current_revision_kind is RevisionKind.CONTENT:
            result[source.provider_object_reference] = source
    return result


def _item_command(
    command: MountedFilesystemReconciliationCommand,
    item: MountedFilesystemScanItem,
    occurrence: str | None,
) -> ConnectorIngestionCommand:
    identity_digest = hashlib.sha256(
        f"{item.provider_object_reference}\0{item.read_token}\0{occurrence}".encode()
    ).hexdigest()
    lifecycle_query = None
    if command.lifecycle_query is not None:
        source_identity = mounted_filesystem_source_identity(
            command.connection_version_id,
            command.resource_scope_reference,
            item.provider_object_reference,
            occurrence,
        )
        lifecycle_query = lifecycle_query_for_source(
            command.lifecycle_query,
            provider_object_reference=item.provider_object_reference,
            external_namespace=source_identity.namespace,
            external_generation=source_identity.generation,
            external_object_id=source_identity.object_id,
        )
        identity_digest = source_identity.object_id
    return ConnectorIngestionCommand(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        command_identity=f"mfs.reconcile.{identity_digest[:48]}",
        idempotency_key=f"mounted-reconciliation:item:{identity_digest}",
        connection_id=command.connection_id,
        resource_scope_reference=command.resource_scope_reference,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        resource_object_reference=item.provider_object_reference,
        external_identity_occurrence=occurrence,
        expected_provider_revision=item.read_token,
        access_policy_version_id=command.access_policy_version_id,
        lifecycle_query=lifecycle_query,
    )


def _removal_idempotency_key(
    command: MountedFilesystemReconciliationCommand,
    policy: MountedFilesystemRemovalPolicy,
    source: MountedFilesystemCanonicalSource,
) -> str:
    identity = hashlib.sha256(
        (
            f"{command.workspace_id}:{command.environment_id}:{command.connection_id}:"
            f"{command.connection_version_id}:{command.resource_scope_reference}:"
            f"{policy.policy_version_id}:{policy.approval_id}:"
            f"{source.source_object_id}:{source.current_revision_id}"
        ).encode("ascii")
    ).hexdigest()
    return f"mounted-absence:{identity}"


def _tombstone_operation_id(
    idempotency_key: str, *, policy: MountedFilesystemRemovalPolicy
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:mounted-filesystem:tombstone:{idempotency_key}:"
        f"{policy.policy_version_id}:{policy.approval_id}",
    )


def _tombstone_document_command(
    command: MountedFilesystemReconciliationCommand,
    source: MountedFilesystemCanonicalSource,
    idempotency_key: str,
) -> TombstoneDocumentCommand:
    return TombstoneDocumentCommand(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        source_object_id=source.source_object_id,
        expected_current_revision_id=source.current_revision_id,
        idempotency_key=idempotency_key,
        deletion_reason="provider_deleted",
    )


def _tombstone_revision_id(
    idempotency_key: str, *, policy: MountedFilesystemRemovalPolicy
) -> UUID:
    operation_id = _tombstone_operation_id(idempotency_key, policy=policy)
    return uuid5(
        NAMESPACE_URL,
        f"spine:document-lifecycle:tombstone:{operation_id}",
    )


def _evidence(
    run: MountedFilesystemReconciliationRun,
    state: MountedFilesystemLifecycleState,
    path: str | None,
    *,
    source_object_id: UUID | None = None,
    source_revision_id: UUID | None = None,
    predecessor_source_object_id: UUID | None = None,
    predecessor_revision_id: UUID | None = None,
    accepted: bool = False,
    now: datetime,
) -> MountedFilesystemLifecycleEvidence:
    protected_identity = path or f"scope:{run.resource_scope_reference}"
    path_digest = hashlib.sha256(protected_identity.encode()).hexdigest()
    evidence_id = uuid5(
        NAMESPACE_URL,
        "spine:mounted-filesystem-evidence:v1:"
        f"{run.run_id}:{state.value}:{path_digest}:{source_object_id}:"
        f"{source_revision_id}",
    )
    return MountedFilesystemLifecycleEvidence(
        evidence_id=evidence_id,
        state=state,
        provider_object_reference_digest=path_digest,
        source_object_id=source_object_id,
        source_revision_id=source_revision_id,
        predecessor_source_object_id=predecessor_source_object_id,
        predecessor_revision_id=predecessor_revision_id,
        identity_continuity_proven=False,
        canonical_effect_accepted=accepted,
        evidence_reference=f"mounted-reconciliation:{evidence_id.hex}",
        observed_at=now,
    )


def _append_evidence(
    values: tuple[MountedFilesystemLifecycleEvidence, ...],
    evidence: MountedFilesystemLifecycleEvidence,
) -> tuple[MountedFilesystemLifecycleEvidence, ...]:
    if any(item.evidence_id == evidence.evidence_id for item in values):
        return values
    return (*values, evidence)


def _deduplicate_evidence(
    values: tuple[MountedFilesystemLifecycleEvidence, ...],
) -> tuple[MountedFilesystemLifecycleEvidence, ...]:
    result: list[MountedFilesystemLifecycleEvidence] = []
    for value in values:
        if not any(item.evidence_id == value.evidence_id for item in result):
            result.append(value)
    return tuple(result)


def _validate_tombstone_command(
    command: SourceLifecycleCommand,
    source: MountedFilesystemCanonicalSource,
    idempotency_key: str,
) -> None:
    if command.kind is not SourceLifecycleKind.DELETION or (
        command.source.source_object_id != source.source_object_id
        or command.canonical_acceptance.expected_current_revision_id
        != source.current_revision_id
        or command.canonical_acceptance.idempotency_key.value != idempotency_key
        or command.observation_idempotency_key.value
        != f"{idempotency_key}:observation"
    ):
        raise ValueError("mounted filesystem tombstone command changed evidence")


__all__ = [name for name in globals() if name.startswith("MountedFilesystem")]
