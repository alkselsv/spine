"""Deterministic transaction-aware in-memory persistence adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from types import TracebackType
from typing import NoReturn, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from spine.application.diagnostics.audit import (
    AuditEvent,
    AuditEventRegistry,
    same_logical_audit_event,
    validate_audit_event_for_context,
)
from spine.application.persistence.bootstrap import (
    InitialWorkspaceBootstrapAuthority,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistenceScope,
    TrustedPersistenceContext,
    TrustedContextVerifier,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    AuditConflictError,
    CanonicalAuthorityConflictError,
    CanonicalEligibilityError,
    ConstraintConflictError,
    ContextProfileApprovalError,
    ContextProfileDigestMismatchError,
    ContextProfileMalformedError,
    ContextProfileNotFoundError,
    ContextProfileRevokedError,
    ContextBundleConflictError,
    ContextBundleDigestMismatchError,
    ContextBundleEvidenceUnavailableError,
    ContextBundleMalformedError,
    ContextBundleNotFoundError,
    IdempotencyConflictError,
    InvalidBootstrapAuthorityError,
    InvalidPersistenceContextError,
    OutboxConflictError,
    PersistenceError,
    PersistenceUnavailableError,
    ObservationIntegrityConflictError,
    ObservationCommandDigestMismatchError,
    OptimisticConflictError,
    UnexpectedPersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.application.persistence.errors import RevisionDigestMismatchError
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
    idempotency_conflict,
)
from spine.application.persistence.repositories import (
    SourceObservationCommand,
    SourceObservationResult,
)
from spine.application.persistence.canonical import (
    ALLOWED_CANONICAL_ADMISSION_PROFILES,
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceResult,
    SourceOrderComparison,
    SourceOrderingPolicyError,
    SourceOrderingPolicyRegistry,
    validate_canonical_acceptance_effects,
)
from spine.application.persistence.projection import (
    PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
    PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    ProjectionActivationCommand,
    ProjectionActivationCommitted,
    ProjectionActivationRejected,
    ProjectionActivationReplay,
    ProjectionActivationUnavailable,
    ProjectionActivatedOutboxPayload,
    ProjectionCatchUpRequestedOutboxPayload,
    register_projection_outbox_events,
)
from spine.application.persistence.boundary import (
    CANONICAL_BOUNDARY_RESULT_TYPE,
    CANONICAL_BOUNDARY_SCHEMA_VERSION,
    CanonicalBoundaryCaptureCommand,
    CanonicalBoundaryCaptureResult,
)
from spine.infrastructure.persistence.access_policies import (
    InMemoryAccessPolicyRepository,
)
from spine.application.persistence.ingestion import (
    IngestionBindingCommand,
    IngestionMutationResult,
    IngestionRepository,
    IngestionRunCommand,
    IngestionRunStateView,
    ProcessingReceiptCommand,
    validate_transition,
)
from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
    OutboxIntent,
    validate_outbox_intent_for_context,
)
from spine.application.persistence.delivery import (
    ClaimedOutboxDelivery,
    OutboxDelivery,
    OutboxDeliveryRouteRegistry,
    OutboxDeliveryStatus,
    StaleDeliveryLeaseError,
)
from spine.application.outbox.dispatch import (
    OutboxDeliveryFinalization,
    validate_outbox_delivery_audit,
)
from spine.infrastructure.persistence.backup_recovery_in_memory import (
    InMemoryRecoveryRepository,
)
from spine.domain.workspaces import Environment, Workspace
from spine.domain.audit import AuditObjectReference, AuditOutcome, CanonicalTransitionAuditPayload
from spine.domain.sources import (
    AdmissionDecision,
    AdmissionDisposition,
    RevisionKind,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
    CanonicalAcceptance,
    CanonicalOrdering,
    CurrentSourceHead,
    CanonicalBoundary,
    CanonicalBoundaryMember,
    canonical_boundary_manifest_digest,
)
from spine.domain.sources.canonicalization import assert_revision_digest
from spine.domain.sources.errors import RevisionDigestMismatchError as DomainRevisionDigestMismatchError
from spine.domain.ingestion import (
    IngestionFailure,
    IngestionItemState,
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    IngestionWarning,
    ProcessingReceipt,
)
from spine.domain.projections import (
    ActiveProjection,
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionDrift,
    ProjectionDriftDetectedBy,
    ProjectionFreshness,
    ProjectionHealth,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)
from spine.domain.context_profiles import ContextProfileVersion
from spine.domain.context_bundles import ContextBundle
from spine.infrastructure.persistence.projection import InMemoryProjectionRepository


_ResultT = TypeVar("_ResultT")
_UNEXPECTED_FAILURE_MESSAGE = "Persistence operation failed."


@dataclass(slots=True)
class _StoreState:
    workspaces: dict[UUID, Workspace]
    environments: dict[UUID, Environment]
    context_profiles: dict[tuple[UUID, UUID, UUID, int], ContextProfileVersion]
    context_bundles: dict[tuple[UUID, UUID, UUID, int], ContextBundle]
    idempotency_receipts: dict["_ReceiptKey", "_IdempotencyReceipt"]
    outbox_intents: dict[UUID, OutboxIntent]
    source_objects: dict[UUID, SourceObject]
    source_revisions: dict[UUID, SourceRevision]
    source_provenance: dict[UUID, SourceRevisionProvenance]
    source_receipt_associations: dict[UUID, SourceRevisionReceiptAssociation]
    admission_decisions: dict[UUID, AdmissionDecision]
    audit_events: dict[UUID, AuditEvent]
    ingestion_runs: dict[UUID, IngestionRun]
    ingestion_items: dict[UUID, IngestionRunSource]
    ingestion_bindings: dict[UUID, IngestionRunSourceBinding]
    ingestion_transitions: dict[UUID, IngestionTransition]
    ingestion_warnings: dict[UUID, IngestionWarning]
    ingestion_failures: dict[UUID, IngestionFailure]
    processing_receipts: dict[UUID, ProcessingReceipt]
    canonical_acceptances: dict[UUID, CanonicalAcceptance]
    current_source_heads: dict[UUID, CurrentSourceHead]
    canonical_generations: dict[tuple[UUID, UUID], int]
    canonical_boundaries: dict[UUID, CanonicalBoundary]
    canonical_boundary_members: dict[tuple[UUID, UUID], CanonicalBoundaryMember]
    ontology_versions: dict[UUID, object]
    projection_configs: dict[UUID, object]
    projection_runs: dict[UUID, object]
    projection_transitions: dict[UUID, object]
    projection_partitions: dict[UUID, object]
    projection_receipts: dict[UUID, object]
    projection_references: dict[UUID, object]
    projection_validations: dict[UUID, object]
    projection_snapshots: dict[UUID, object]
    projection_snapshot_members: dict[UUID, object]
    projection_reconciliation_results: dict[UUID, object]
    projection_activations: dict[UUID, ProjectionActivation]
    active_projections: dict[tuple[UUID, UUID, str], ActiveProjection]
    projection_drifts: dict[UUID, ProjectionDrift]


@dataclass(frozen=True, slots=True)
class _ReceiptKey:
    workspace_id: UUID
    environment_id: UUID | None
    operation_name: str
    operation_schema_version: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _IdempotencyReceipt:
    receipt_id: UUID
    key: _ReceiptKey
    digest: CommandDigest
    result: OpaqueResultReference | None


class _Store:
    def __init__(
        self,
        *,
        transaction_lock: asyncio.Lock | None = None,
        canonical_policy_repository: InMemoryAccessPolicyRepository | None = None,
        canonical_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        boundary_id_factory: Callable[[], UUID] = uuid4,
        boundary_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.state = _StoreState(
            workspaces={},
            environments={},
            context_profiles={},
            context_bundles={},
            idempotency_receipts={},
            outbox_intents={},
            source_objects={},
            source_revisions={},
            source_provenance={},
            source_receipt_associations={},
            admission_decisions={},
            audit_events={},
            ingestion_runs={},
            ingestion_items={},
            ingestion_bindings={},
            ingestion_transitions={},
            ingestion_warnings={},
            ingestion_failures={},
            processing_receipts={},
            canonical_acceptances={},
            current_source_heads={},
            canonical_generations={},
            canonical_boundaries={},
            canonical_boundary_members={},
            ontology_versions={},
            projection_configs={},
            projection_runs={},
            projection_transitions={},
            projection_partitions={},
            projection_receipts={},
            projection_references={},
            projection_validations={},
            projection_snapshots={},
            projection_snapshot_members={},
            projection_reconciliation_results={},
            projection_activations={},
            active_projections={},
            projection_drifts={},
        )
        self.lock = transaction_lock or asyncio.Lock()
        self.observation_lock = asyncio.Lock()
        self.canonical_lock = asyncio.Lock()
        self.initialized = False
        self.bootstrap_sealed = False
        self.fail_next_audit_append = False
        self.canonical_policy_repository = canonical_policy_repository
        self.canonical_clock = canonical_clock
        self.boundary_id_factory = boundary_id_factory
        self.boundary_clock = boundary_clock
        self.outbox_deliveries: dict[UUID, OutboxDelivery] = {}

    @property
    def workspaces(self) -> dict[UUID, Workspace]:
        return self.state.workspaces

    @property
    def environments(self) -> dict[UUID, Environment]:
        return self.state.environments

    @property
    def context_profiles(self) -> dict[tuple[UUID, UUID, UUID, int], ContextProfileVersion]:
        return self.state.context_profiles

    @property
    def context_bundles(self) -> dict[tuple[UUID, UUID, UUID, int], ContextBundle]:
        return self.state.context_bundles

    @property
    def idempotency_receipts(self) -> dict[_ReceiptKey, _IdempotencyReceipt]:
        return self.state.idempotency_receipts

    @property
    def outbox_intents(self) -> dict[UUID, OutboxIntent]:
        return self.state.outbox_intents

    @property
    def source_objects(self) -> dict[UUID, SourceObject]:
        return self.state.source_objects

    @property
    def source_revisions(self) -> dict[UUID, SourceRevision]:
        return self.state.source_revisions

    @property
    def source_provenance(self) -> dict[UUID, SourceRevisionProvenance]:
        return self.state.source_provenance

    @property
    def source_receipt_associations(self) -> dict[UUID, SourceRevisionReceiptAssociation]:
        return self.state.source_receipt_associations

    @property
    def admission_decisions(self) -> dict[UUID, AdmissionDecision]:
        return self.state.admission_decisions

    @property
    def audit_events(self) -> dict[UUID, AuditEvent]:
        return self.state.audit_events

    @property
    def ingestion_runs(self) -> dict[UUID, IngestionRun]:
        return self.state.ingestion_runs

    @property
    def ingestion_items(self) -> dict[UUID, IngestionRunSource]:
        return self.state.ingestion_items

    @property
    def ingestion_bindings(self) -> dict[UUID, IngestionRunSourceBinding]:
        return self.state.ingestion_bindings

    @property
    def ingestion_transitions(self) -> dict[UUID, IngestionTransition]:
        return self.state.ingestion_transitions

    @property
    def ingestion_warnings(self) -> dict[UUID, IngestionWarning]:
        return self.state.ingestion_warnings

    @property
    def ingestion_failures(self) -> dict[UUID, IngestionFailure]:
        return self.state.ingestion_failures

    @property
    def processing_receipts(self) -> dict[UUID, ProcessingReceipt]:
        return self.state.processing_receipts

    @property
    def canonical_acceptances(self) -> dict[UUID, CanonicalAcceptance]:
        return self.state.canonical_acceptances

    @property
    def current_source_heads(self) -> dict[UUID, CurrentSourceHead]:
        return self.state.current_source_heads

    @property
    def canonical_generations(self) -> dict[tuple[UUID, UUID], int]:
        return self.state.canonical_generations

    @property
    def canonical_boundaries(self) -> dict[UUID, CanonicalBoundary]:
        return self.state.canonical_boundaries

    @property
    def canonical_boundary_members(self) -> dict[tuple[UUID, UUID], CanonicalBoundaryMember]:
        return self.state.canonical_boundary_members

    @property
    def ontology_versions(self) -> dict[UUID, object]:
        return self.state.ontology_versions

    @property
    def projection_configs(self) -> dict[UUID, object]:
        return self.state.projection_configs

    @property
    def projection_runs(self) -> dict[UUID, object]:
        return self.state.projection_runs

    @property
    def projection_transitions(self) -> dict[UUID, object]:
        return self.state.projection_transitions

    @property
    def projection_partitions(self) -> dict[UUID, object]:
        return self.state.projection_partitions

    @property
    def projection_receipts(self) -> dict[UUID, object]:
        return self.state.projection_receipts

    @property
    def projection_references(self) -> dict[UUID, object]:
        return self.state.projection_references

    @property
    def projection_validations(self) -> dict[UUID, object]:
        return self.state.projection_validations

    @property
    def projection_snapshots(self) -> dict[UUID, object]:
        return self.state.projection_snapshots

    @property
    def projection_snapshot_members(self) -> dict[UUID, object]:
        return self.state.projection_snapshot_members

    @property
    def projection_reconciliation_results(self) -> dict[UUID, object]:
        return self.state.projection_reconciliation_results

    @property
    def projection_activations(self) -> dict[UUID, ProjectionActivation]:
        return self.state.projection_activations

    @property
    def active_projections(self) -> dict[tuple[UUID, UUID, str], ActiveProjection]:
        return self.state.active_projections

    @property
    def projection_drifts(self) -> dict[UUID, ProjectionDrift]:
        return self.state.projection_drifts


def _workspace_copy(value: Workspace) -> Workspace:
    """Rebuild a canonical Workspace without invoking overridable copy hooks."""
    if type(value) is not Workspace:
        raise TypeError("Unsupported Workspace implementation.")
    return Workspace(id=value.id, slug=value.slug, display_name=value.display_name)


def _environment_copy(value: Environment) -> Environment:
    """Rebuild a canonical Environment without invoking overridable copy hooks."""
    if type(value) is not Environment:
        raise TypeError("Unsupported Environment implementation.")
    return Environment(
        id=value.id,
        workspace_id=value.workspace_id,
        kind=value.kind,
        display_name=value.display_name,
    )


def _audit_event_copy(value: AuditEvent) -> AuditEvent:
    if type(value) is not AuditEvent:
        raise TypeError("Unsupported Audit Event implementation.")
    return value.detached_snapshot()


class _Lifecycle(Enum):
    NEW = auto()
    ENTERING = auto()
    ACTIVE = auto()
    COMMITTED = auto()
    ROLLED_BACK = auto()
    CLOSED = auto()


class _WorkspaceRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def add(self, workspace: Workspace) -> None:
        self._uow._guard_active()

        def operation() -> None:
            scope = self._uow.scope
            if not isinstance(scope, WorkspaceScope) or workspace.id != scope.workspace_id:
                self._uow._fail(
                    InvalidPersistenceContextError("Persistence context is invalid.")
                )
            if not self._uow._initialized:
                self._uow._fail(
                    InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
                )
            if self._uow._resolve_workspace(workspace.id) is not None:
                self._uow._fail(
                    ConstraintConflictError("Workspace identity already exists.")
                )
            self._uow._pending_workspaces[workspace.id] = _workspace_copy(workspace)

        self._uow._repository_call(operation)

    async def resolve(self, workspace_id: UUID) -> Workspace | None:
        self._uow._guard_active()

        def operation() -> Workspace | None:
            if workspace_id != self._uow.scope.workspace_id:
                return None
            workspace = self._uow._resolve_workspace(workspace_id)
            return _workspace_copy(workspace) if workspace is not None else None

        return self._uow._repository_call(operation)

class _EnvironmentRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def add(self, environment: Environment) -> None:
        self._uow._guard_active()

        def operation() -> None:
            scope = self._uow.scope
            if not isinstance(scope, WorkspaceScope) or environment.workspace_id != scope.workspace_id:
                self._uow._fail(
                    InvalidPersistenceContextError("Persistence context is invalid.")
                )
            if self._uow._resolve_workspace(scope.workspace_id) is None:
                self._uow._fail(ConstraintConflictError("Owning Workspace does not exist."))
            if self._uow._resolve_environment(environment.id) is not None:
                self._uow._fail(
                    ConstraintConflictError("Environment identity already exists.")
                )
            self._uow._pending_environments[environment.id] = _environment_copy(environment)

        self._uow._repository_call(operation)

    async def resolve(self, environment_id: UUID) -> Environment | None:
        self._uow._guard_active()

        def operation() -> Environment | None:
            environment = self._uow._resolve_environment(environment_id)
            if environment is None:
                return None
            scope = self._uow.scope
            if environment.workspace_id != scope.workspace_id:
                return None
            if isinstance(scope, EnvironmentScope) and environment.id != scope.environment_id:
                return None
            return _environment_copy(environment)

        return self._uow._repository_call(operation)


class _ContextProfileRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    @staticmethod
    def _key(profile: ContextProfileVersion) -> tuple[UUID, UUID, UUID, int]:
        return (
            profile.workspace_id,
            profile.environment_id,
            profile.profile_id,
            profile.profile_version,
        )

    def _profiles(self) -> dict[tuple[UUID, UUID, UUID, int], ContextProfileVersion]:
        return self._uow._base_context_profiles | self._uow._pending_context_profiles

    def _validate_scope(self, profile: ContextProfileVersion) -> None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != profile.workspace_id
            or scope.environment_id != profile.environment_id
        ):
            self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))

    async def add(self, profile: ContextProfileVersion) -> None:
        self._uow._guard_active()

        def operation() -> None:
            self._validate_scope(profile)
            if not profile.server_approved:
                self._uow._fail(
                    ContextProfileApprovalError("Context profile is not server-approved.")
                )
            if self._uow._resolve_environment(profile.environment_id) is None:
                self._uow._fail(ConstraintConflictError("Owning Environment does not exist."))
            key = self._key(profile)
            if key in self._profiles():
                self._uow._fail(
                    ConstraintConflictError("Context profile immutable version already exists.")
                )
            self._uow._pending_context_profiles[key] = profile.model_copy(deep=True)

        self._uow._repository_call(operation)

    async def create(self, profile: ContextProfileVersion) -> ContextProfileVersion:
        self._uow._guard_active()

        def operation() -> ContextProfileVersion:
            self._validate_scope(profile)
            if not profile.server_approved:
                self._uow._fail(
                    ContextProfileApprovalError("Context profile is not server-approved.")
                )
            if self._uow._resolve_environment(profile.environment_id) is None:
                self._uow._fail(ConstraintConflictError("Owning Environment does not exist."))
            existing = self._profiles().get(self._key(profile))
            if existing is not None:
                if existing != profile:
                    self._uow._fail(
                        ConstraintConflictError(
                            "Context profile immutable version conflicts with history."
                        )
                    )
                return existing.model_copy(deep=True)
            self._uow._pending_context_profiles[self._key(profile)] = profile.model_copy(
                deep=True
            )
            return profile.model_copy(deep=True)

        return self._uow._repository_call(operation)

    async def resolve(
        self,
        *,
        profile_id: UUID,
        profile_version: int,
        profile_digest: str,
    ) -> ContextProfileVersion:
        self._uow._guard_active()

        def operation() -> ContextProfileVersion:
            scope = self._uow.scope
            if not isinstance(scope, EnvironmentScope):
                self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))
            key = (scope.workspace_id, scope.environment_id, profile_id, profile_version)
            stored = self._profiles().get(key)
            if stored is None:
                self._uow._fail(ContextProfileNotFoundError("Context profile is unavailable."))
            if stored.profile_digest != profile_digest:
                self._uow._fail(ContextProfileDigestMismatchError("Context profile identity is invalid."))
            try:
                restored = ContextProfileVersion.model_validate(stored.model_dump(mode="python"))
            except (TypeError, ValueError):
                self._uow._fail(ContextProfileMalformedError("Context profile is unavailable."))
            if restored.revoked:
                self._uow._fail(ContextProfileRevokedError("Context profile is unavailable."))
            return restored

        return self._uow._repository_call(operation)

    async def resolve_for_scope(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        profile_id: UUID,
        profile_version: int,
        profile_digest: str,
    ) -> ContextProfileVersion:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != workspace_id or scope.environment_id != environment_id
        ):
            self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))
        return await self.resolve(
            profile_id=profile_id,
            profile_version=profile_version,
            profile_digest=profile_digest,
        )


class _ContextBundleRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    @staticmethod
    def _key(bundle: ContextBundle) -> tuple[UUID, UUID, UUID, int]:
        return (
            bundle.workspace_id,
            bundle.environment_id,
            bundle.bundle_id,
            bundle.bundle_version,
        )

    def _bundles(self) -> dict[tuple[UUID, UUID, UUID, int], ContextBundle]:
        return self._uow._base_context_bundles | self._uow._pending_context_bundles

    def _validate_scope(self, bundle: ContextBundle) -> None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != bundle.workspace_id
            or scope.environment_id != bundle.environment_id
        ):
            self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))

    def _validate_bundle(self, bundle: ContextBundle) -> None:
        self._validate_scope(bundle)
        if self._uow._resolve_environment(bundle.environment_id) is None:
            self._uow._fail(ConstraintConflictError("Owning Environment does not exist."))

    async def add(self, bundle: ContextBundle) -> None:
        self._uow._guard_active()

        def operation() -> None:
            self._validate_bundle(bundle)
            existing = self._bundles().get(self._key(bundle))
            if existing is not None:
                self._uow._fail(
                    ContextBundleConflictError(
                        "Context bundle immutable identity already exists."
                    )
                )
            attempt = next(
                (
                    item
                    for item in self._bundles().values()
                    if item.workspace_id == bundle.workspace_id
                    and item.environment_id == bundle.environment_id
                    and item.run_id == bundle.run_id
                    and item.attempt_id == bundle.attempt_id
                    and item.idempotency_key == bundle.idempotency_key
                ),
                None,
            )
            if attempt is not None:
                self._uow._fail(
                    ContextBundleConflictError(
                        "Context bundle attempt identity conflicts with history."
                    )
                )
            self._uow._pending_context_bundles[self._key(bundle)] = bundle.model_copy(deep=True)

        self._uow._repository_call(operation)

    async def create(self, bundle: ContextBundle) -> ContextBundle:
        self._uow._guard_active()

        def operation() -> ContextBundle:
            self._validate_bundle(bundle)
            existing = self._bundles().get(self._key(bundle))
            if existing is not None:
                if existing != bundle:
                    self._uow._fail(
                        ContextBundleConflictError(
                            "Context bundle immutable identity conflicts with history."
                        )
                    )
                return existing.model_copy(deep=True)
            attempt = next(
                (
                    item
                    for item in self._bundles().values()
                    if item.workspace_id == bundle.workspace_id
                    and item.environment_id == bundle.environment_id
                    and item.run_id == bundle.run_id
                    and item.attempt_id == bundle.attempt_id
                    and item.idempotency_key == bundle.idempotency_key
                ),
                None,
            )
            if attempt is not None:
                if attempt == bundle:
                    return attempt.model_copy(deep=True)
                self._uow._fail(
                    ContextBundleConflictError(
                        "Context bundle attempt identity conflicts with history."
                    )
                )
            self._uow._pending_context_bundles[self._key(bundle)] = bundle.model_copy(deep=True)
            return bundle.model_copy(deep=True)

        return self._uow._repository_call(operation)

    def _restore(self, stored: ContextBundle) -> ContextBundle:
        try:
            return ContextBundle.model_validate(stored.model_dump(mode="python"))
        except (TypeError, ValueError):
            self._uow._fail(ContextBundleMalformedError("Context bundle is unavailable."))

    def _safe_read(self, stored: ContextBundle) -> ContextBundle:
        bundle = self._restore(stored)
        if bundle.outcome.value in {"allowed", "degraded"} and any(
            evidence.status.value != "valid" for evidence in bundle.evidence_references
        ):
            self._uow._fail(
                ContextBundleEvidenceUnavailableError(
                    "Context bundle evidence is unavailable."
                )
            )
        return bundle

    async def resolve(
        self,
        *,
        bundle_id: UUID,
        bundle_version: int,
        bundle_digest: str,
    ) -> ContextBundle:
        self._uow._guard_active()

        def operation() -> ContextBundle:
            scope = self._uow.scope
            if not isinstance(scope, EnvironmentScope):
                self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))
            stored = self._bundles().get(
                (scope.workspace_id, scope.environment_id, bundle_id, bundle_version)
            )
            if stored is None:
                self._uow._fail(ContextBundleNotFoundError("Context bundle is unavailable."))
            if stored.bundle_digest != bundle_digest:
                self._uow._fail(ContextBundleDigestMismatchError("Context bundle identity is invalid."))
            return self._safe_read(stored)

        return self._uow._repository_call(operation)

    async def resolve_attempt(
        self,
        *,
        run_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
    ) -> ContextBundle:
        self._uow._guard_active()

        def operation() -> ContextBundle:
            scope = self._uow.scope
            if not isinstance(scope, EnvironmentScope):
                self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))
            matches = tuple(
                item
                for item in self._bundles().values()
                if item.workspace_id == scope.workspace_id
                and item.environment_id == scope.environment_id
                and item.run_id == run_id
                and item.attempt_id == attempt_id
                and item.idempotency_key == idempotency_key
            )
            if not matches:
                self._uow._fail(ContextBundleNotFoundError("Context bundle is unavailable."))
            return self._safe_read(matches[0])

        return self._uow._repository_call(operation)


class _SourceObservationRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    def _scope_check(self, workspace_id: UUID, environment_id: UUID) -> None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != workspace_id or scope.environment_id != environment_id
        ):
            self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))

    def _objects(self) -> dict[UUID, SourceObject]:
        return self._uow._base_source_objects | self._uow._pending_source_objects

    def _revisions(self) -> dict[UUID, SourceRevision]:
        return self._uow._base_source_revisions | self._uow._pending_source_revisions

    def _provenance(self) -> dict[UUID, SourceRevisionProvenance]:
        return self._uow._base_source_provenance | self._uow._pending_source_provenance

    def _receipt_associations(self) -> dict[UUID, SourceRevisionReceiptAssociation]:
        return (
            self._uow._base_source_receipt_associations
            | self._uow._pending_source_receipt_associations
        )

    async def resolve_source(self, source_object_id: UUID) -> SourceObject | None:
        self._uow._guard_active()

        def operation() -> SourceObject | None:
            scope = self._uow.scope
            if not isinstance(scope, EnvironmentScope):
                return None
            source = self._objects().get(source_object_id)
            if source is None or (
                source.workspace_id,
                source.environment_id,
            ) != (scope.workspace_id, scope.environment_id):
                return None
            return source.model_copy(deep=True)

        return self._uow._repository_call(operation)

    async def resolve_or_create_source(self, source: SourceObject) -> SourceObject:
        self._uow._guard_active()

        def operation() -> SourceObject:
            self._scope_check(source.workspace_id, source.environment_id)
            by_id = self._objects().get(source.source_object_id)
            if by_id is not None and by_id != source:
                self._uow._fail(ConstraintConflictError("Source identity conflict."))
            for existing in self._objects().values():
                if (
                    existing.workspace_id == source.workspace_id
                    and existing.environment_id == source.environment_id
                    and existing.identity_mode == source.identity_mode
                    and existing.connection_id == source.connection_id
                    and existing.external_namespace == source.external_namespace
                    and existing.external_generation == source.external_generation
                    and existing.external_object_id == source.external_object_id
                    and existing.upload_identity == source.upload_identity
                ):
                    return existing.model_copy(deep=True)
            self._uow._pending_source_objects[source.source_object_id] = source.model_copy(deep=True)
            return source.model_copy(deep=True)

        return self._uow._repository_call(operation)

    async def record_observation(self, command: SourceObservationCommand) -> SourceObservationResult:
        self._uow._guard_active()

        if not self._uow._source_observation_lock_held:
            await self._uow._store.observation_lock.acquire()
            self._uow._source_observation_lock_held = True

        claim = await self._uow.idempotency.claim(
            operation_schema_version=1,
            key=command.idempotency_key,
            digest=command.digest,
        )
        expected_digest = command.expected_digest(self._uow._operation_snapshot())
        if expected_digest != command.digest:
            self._uow._fail(ObservationCommandDigestMismatchError("Source observation command digest is invalid."))
        if isinstance(claim, IdempotencyReplay):
            def replay_operation() -> SourceObservationResult:
                revision = self._revisions().get(claim.result.result_id)
                if revision is None:
                    self._uow._fail(ConstraintConflictError("Observation replay is unavailable."))
                source = self._objects().get(revision.source_object_id)
                provenance = next(
                    (
                        item for item in self._provenance().values()
                        if item.revision_id == revision.revision_id
                        and item.producer_kind == command.provenance.producer_kind
                        and item.producer_reference == command.provenance.producer_reference
                        and item.event_identity == command.provenance.event_identity
                    ),
                    None,
                )
                if source is None or provenance is None:
                    self._uow._fail(ConstraintConflictError("Observation replay is unavailable."))
                association = next(
                    (
                        item
                        for item in self._receipt_associations().values()
                        if item.provenance_id == provenance.provenance_id
                        and item.revision_id == revision.revision_id
                        and item.workspace_id == revision.workspace_id
                        and item.environment_id == revision.environment_id
                    ),
                    None,
                )
                expected_association = (
                    command.receipt_association.model_copy(update={"revision_id": revision.revision_id})
                    if command.receipt_association is not None
                    else None
                )
                if (
                    expected_association is not None
                    and (
                        association is None
                        or association
                        != expected_association.model_copy(
                            update={"associated_at": association.associated_at}
                        )
                    )
                ):
                    self._uow._fail(ObservationIntegrityConflictError("Source receipt association conflicts."))
                return SourceObservationResult(
                    source=source.model_copy(deep=True), revision=revision.model_copy(deep=True),
                    provenance=provenance.model_copy(deep=True), replay=True,
                    claim=claim, result_reference=claim.result,
                    receipt_association=(
                        association.model_copy(deep=True) if association is not None else None
                    ),
                )
            return self._uow._repository_call(replay_operation)

        revision = command.revision
        provenance = command.provenance
        source_input = command.source
        resolved_source = await self.resolve_or_create_source(source_input)
        if resolved_source.source_object_id != revision.source_object_id:
            self._uow._fail(ConstraintConflictError("Source observation is invalid."))

        def operation() -> SourceObservationResult:
            try:
                assert_revision_digest(revision)
            except DomainRevisionDigestMismatchError as error:
                self._uow._fail(RevisionDigestMismatchError(str(error)))
            self._scope_check(revision.workspace_id, revision.environment_id)
            if revision.source_object_id != source_input.source_object_id:
                self._uow._fail(ConstraintConflictError("Source observation is invalid."))
            if (
                provenance.workspace_id != revision.workspace_id
                or provenance.environment_id != revision.environment_id
                or provenance.source_object_id != revision.source_object_id
                or provenance.revision_id != revision.revision_id
            ):
                self._uow._fail(ConstraintConflictError("Source observation is invalid."))
            source = self._objects().get(revision.source_object_id)
            if source is None:
                self._uow._fail(ConstraintConflictError("Source observation is invalid."))
            if revision.reappearance_after_tombstone_revision_id is not None:
                predecessor = self._revisions().get(revision.reappearance_after_tombstone_revision_id)
                if predecessor is None or predecessor.kind.value != "tombstone" or predecessor.source_object_id != revision.source_object_id:
                    self._uow._fail(ConstraintConflictError("Source observation is invalid."))
            association = command.receipt_association
            if association is not None:
                if (
                    association.source_object_id != revision.source_object_id
                    or association.revision_id != revision.revision_id
                    or association.provenance_id != provenance.provenance_id
                    or association.workspace_id != revision.workspace_id
                    or association.environment_id != revision.environment_id
                    or association.original_sha256 != revision.original_sha256
                    or association.byte_length != revision.byte_length
                    or revision.kind is not RevisionKind.CONTENT
                ):
                    self._uow._fail(ConstraintConflictError("Source receipt association is invalid."))
                existing_association = self._receipt_associations().get(association.association_id)
                if (
                    existing_association is not None
                    and existing_association
                    != association.model_copy(
                        update={"associated_at": existing_association.associated_at}
                    )
                ):
                    self._uow._fail(ConstraintConflictError("Source receipt association conflicts."))
                if any(
                    item.original_receipt_id == association.original_receipt_id
                    and item.workspace_id == association.workspace_id
                    and item.environment_id == association.environment_id
                    and item.association_id != association.association_id
                    for item in self._receipt_associations().values()
                ):
                    self._uow._fail(ConstraintConflictError("Original receipt is already associated."))
            event_key = (provenance.producer_kind, provenance.producer_reference, provenance.event_identity)
            for existing_provenance in self._provenance().values():
                existing_key = (
                    existing_provenance.producer_kind,
                    existing_provenance.producer_reference,
                    existing_provenance.event_identity,
                )
                if existing_key == event_key:
                    if existing_provenance.event_digest != provenance.event_digest:
                        self._uow._fail(ObservationIntegrityConflictError("Source observation conflicts."))
                    existing_revision = self._revisions()[existing_provenance.revision_id]
                    existing_association = next(
                        (
                            item
                            for item in self._receipt_associations().values()
                            if item.provenance_id == existing_provenance.provenance_id
                        ),
                        None,
                    )
                    if association is not None and (
                        existing_association is None
                        or existing_association
                        != association.model_copy(
                            update={
                                "revision_id": existing_revision.revision_id,
                                "associated_at": existing_association.associated_at,
                            }
                        )
                    ):
                        self._uow._fail(ObservationIntegrityConflictError("Source receipt association conflicts."))
                    result = SourceObservationResult(
                        source=source.model_copy(deep=True),
                        revision=existing_revision.model_copy(deep=True),
                        provenance=existing_provenance.model_copy(deep=True),
                        replay=False, claim=claim,
                        result_reference=OpaqueResultReference(
                            result_type="source_revision", result_id=existing_revision.revision_id, schema_version=1
                        ),
                        receipt_association=(
                            existing_association.model_copy(deep=True)
                            if existing_association is not None
                            else None
                        ),
                    )
                    return result
            for existing_revision in self._revisions().values():
                if (
                    existing_revision.workspace_id == revision.workspace_id
                    and existing_revision.environment_id == revision.environment_id
                    and existing_revision.source_object_id == revision.source_object_id
                    and existing_revision.revision_digest == revision.revision_digest
                ):
                    revision_to_return = existing_revision
                    break
            else:
                by_id = self._revisions().get(revision.revision_id)
                if by_id is not None and by_id != revision:
                    self._uow._fail(ConstraintConflictError("Source revision conflict."))
                self._uow._pending_source_revisions[revision.revision_id] = revision.model_copy(deep=True)
                revision_to_return = revision
            if association is not None and association.revision_id != revision_to_return.revision_id:
                association = association.model_copy(update={"revision_id": revision_to_return.revision_id})
            existing_provenance = self._provenance().get(provenance.provenance_id)
            if existing_provenance is not None and existing_provenance != provenance:
                self._uow._fail(ConstraintConflictError("Source provenance conflict."))
            self._uow._pending_source_provenance[provenance.provenance_id] = provenance.model_copy(deep=True)
            if association is not None:
                self._uow._pending_source_receipt_associations[association.association_id] = association.model_copy(deep=True)
            return SourceObservationResult(
                source=source.model_copy(deep=True),
                revision=revision_to_return.model_copy(deep=True),
                provenance=provenance.model_copy(deep=True),
                replay=False, claim=claim,
                result_reference=OpaqueResultReference(
                    result_type="source_revision", result_id=revision_to_return.revision_id, schema_version=1
                ),
                receipt_association=(association.model_copy(deep=True) if association is not None else None),
            )
        result = self._uow._repository_call(operation)
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def resolve_revision(self, revision_id: UUID) -> SourceRevision | None:
        self._uow._guard_active()
        return self._uow._repository_call(
            lambda: (
                self._revisions()[revision_id].model_copy(deep=True)
                if revision_id in self._revisions()
                and self._revisions()[revision_id].workspace_id == self._uow.scope.workspace_id
                and isinstance(self._uow.scope, EnvironmentScope)
                and self._revisions()[revision_id].environment_id == self._uow.scope.environment_id
                else None
            )
        )

    async def resolve_receipt_associations(
        self, revision_id: UUID
    ) -> tuple[SourceRevisionReceiptAssociation, ...]:
        self._uow._guard_active()

        def operation() -> tuple[SourceRevisionReceiptAssociation, ...]:
            scope = self._uow.scope
            if not isinstance(scope, EnvironmentScope):
                return ()
            return tuple(
                item.model_copy(deep=True)
                for item in sorted(
                    self._receipt_associations().values(),
                    key=lambda value: (value.associated_at, value.association_id),
                )
                if item.revision_id == revision_id
                and item.workspace_id == scope.workspace_id
                and item.environment_id == scope.environment_id
            )

        return self._uow._repository_call(operation)


class _AdmissionRepository:
    """In-memory implementation of the append-only admission CAS chain."""

    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    def _decisions(self) -> dict[UUID, AdmissionDecision]:
        return self._uow._base_admission_decisions | self._uow._pending_admission_decisions

    def _scope_check(self, decision: AdmissionDecision) -> None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != decision.workspace_id
            or scope.environment_id != decision.environment_id
        ):
            self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))

    async def append_decision(self, decision: AdmissionDecision) -> AdmissionDecision:
        self._uow._guard_active()

        def operation() -> AdmissionDecision:
            self._scope_check(decision)
            revision = self._uow._base_source_revisions.get(decision.revision_id) or self._uow._pending_source_revisions.get(decision.revision_id)
            if revision is None or (
                revision.source_object_id != decision.source_object_id
                or revision.workspace_id != decision.workspace_id
                or revision.environment_id != decision.environment_id
            ):
                self._uow._fail(ConstraintConflictError("Admission decision target is invalid."))
            if decision.disposition is AdmissionDisposition.ADMITTED and not any(
                item.revision_id == decision.revision_id
                and item.workspace_id == decision.workspace_id
                and item.environment_id == decision.environment_id
                for item in (
                    *self._uow._base_source_provenance.values(),
                    *self._uow._pending_source_provenance.values(),
                )
            ):
                self._uow._fail(ConstraintConflictError("Admission provenance is incomplete."))
            if decision.disposition is AdmissionDisposition.ADMITTED and revision.kind is not RevisionKind.CONTENT:
                self._uow._fail(ConstraintConflictError("Tombstone admission requires deletion-specific checks."))
            existing_by_id = self._decisions().get(decision.decision_id)
            if existing_by_id is not None:
                if existing_by_id != decision:
                    self._uow._fail(ConstraintConflictError("Admission decision identity conflict."))
                return existing_by_id.model_copy(deep=True)
            decisions = tuple(
                item for item in self._decisions().values()
                if item.workspace_id == decision.workspace_id
                and item.environment_id == decision.environment_id
                and item.revision_id == decision.revision_id
            )
            if decision.predecessor_decision_id is None:
                if decision.decision_version != 1 or any(item.predecessor_decision_id is None for item in decisions):
                    self._uow._fail(ConstraintConflictError("Admission decision root already exists."))
            else:
                predecessor = self._decisions().get(decision.predecessor_decision_id)
                if predecessor is None or predecessor.revision_id != decision.revision_id:
                    self._uow._fail(ConstraintConflictError("Admission decision predecessor is invalid."))
                if decision.decision_version != predecessor.decision_version + 1:
                    self._uow._fail(ConstraintConflictError("Admission decision version is invalid."))
                if any(item.predecessor_decision_id == decision.predecessor_decision_id for item in decisions):
                    self._uow._fail(ConstraintConflictError("Admission decision successor already exists."))
            if any(item.decision_version == decision.decision_version for item in decisions):
                self._uow._fail(ConstraintConflictError("Admission decision version already exists."))
            self._uow._pending_admission_decisions[decision.decision_id] = decision.model_copy(deep=True)
            return decision.model_copy(deep=True)

        return self._uow._repository_call(operation)

    async def resolve(self, decision_id: UUID) -> AdmissionDecision | None:
        self._uow._guard_active()
        return self._uow._repository_call(
            lambda: (
                self._decisions()[decision_id].model_copy(deep=True)
                if decision_id in self._decisions()
                and self._decisions()[decision_id].workspace_id == self._uow.scope.workspace_id
                and isinstance(self._uow.scope, EnvironmentScope)
                and self._decisions()[decision_id].environment_id == self._uow.scope.environment_id
                else None
            )
        )

    async def resolve_effective(self, revision_id: UUID) -> AdmissionDecision | None:
        self._uow._guard_active()

        def operation() -> AdmissionDecision | None:
            scope = self._uow.scope
            if not isinstance(scope, EnvironmentScope):
                return None
            decisions = [
                item for item in self._decisions().values()
                if item.revision_id == revision_id
                and item.workspace_id == scope.workspace_id
                and item.environment_id == scope.environment_id
            ]
            if not decisions:
                return None
            return max(decisions, key=lambda item: item.decision_version).model_copy(deep=True)

        return self._uow._repository_call(operation)


class _CanonicalAcceptanceRepository:
    """In-memory implementation of the canonical source-head protocol."""

    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow
        self._ordering = SourceOrderingPolicyRegistry.r1()

    def _acceptances(self) -> dict[UUID, CanonicalAcceptance]:
        return (
            self._uow._base_canonical_acceptances
            | self._uow._pending_canonical_acceptances
        )

    def _heads(self) -> dict[UUID, CurrentSourceHead]:
        return (
            self._uow._base_current_source_heads
            | self._uow._pending_current_source_heads
        )

    def _revisions(self) -> dict[UUID, SourceRevision]:
        return self._uow._base_source_revisions | self._uow._pending_source_revisions

    def _provenance(self) -> dict[UUID, SourceRevisionProvenance]:
        return self._uow._base_source_provenance | self._uow._pending_source_provenance

    def _receipt_associations(self) -> dict[UUID, SourceRevisionReceiptAssociation]:
        return (
            self._uow._base_source_receipt_associations
            | self._uow._pending_source_receipt_associations
        )

    async def _eligible_policy(self, source_id: UUID, policy_id: UUID) -> bool:
        repository = self._uow._store.canonical_policy_repository
        if repository is None:
            return False
        context = self._uow._context_snapshot_active()
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            return False
        current = await repository.resolve_current(
            source_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        policy = await repository.resolve_policy(
            policy_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        return bool(
            current is not None
            and current.access_policy_version_id == policy_id
            and policy is not None
            and context.service_principal_id is not None
            and policy.allows_service_processing(
                context.service_principal_id,
                context.purpose.value,
                context.operation.value,
                self._uow._store.canonical_clock(),
            )
        )

    async def resolve_current(self, source_object_id: UUID) -> CurrentSourceHead | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        head = self._heads().get(source_object_id)
        if head is None or (head.workspace_id, head.environment_id) != (
            scope.workspace_id,
            scope.environment_id,
        ):
            return None
        return head.model_copy(deep=True)

    async def resolve_acceptance(
        self, canonical_acceptance_id: UUID
    ) -> CanonicalAcceptance | None:
        self._uow._guard_active()
        value = self._acceptances().get(canonical_acceptance_id)
        if value is None or not isinstance(self._uow.scope, EnvironmentScope):
            return None
        if (value.workspace_id, value.environment_id) != (
            self._uow.scope.workspace_id,
            self._uow.scope.environment_id,
        ):
            return None
        return value.model_copy(deep=True)

    async def accept(
        self, command: CanonicalAcceptanceCommand
    ) -> CanonicalAcceptanceResult:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            command.source.workspace_id,
            command.source.environment_id,
        ) != (scope.workspace_id, scope.environment_id):
            self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        if command.expected_digest() != command.digest:
            self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical acceptance command digest is invalid."
                )
            )
        if (
            command.revision.source_object_id != command.source.source_object_id
            or command.provenance.source_object_id != command.source.source_object_id
            or command.provenance.revision_id != command.revision.revision_id
        ):
            self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical acceptance references are invalid."
                )
            )
        if not self._uow._canonical_lock_held:
            await self._uow._store.canonical_lock.acquire()
            self._uow._canonical_lock_held = True
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            current = await self.resolve_current(command.source.source_object_id)
            if current is None:
                self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            acceptance = (
                await self.resolve_acceptance(claim.result.result_id)
                if claim.result.result_type == "canonical_acceptance"
                else None
            )
            if acceptance is None:
                self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            revision = self._revisions().get(acceptance.source_revision_id)
            source = self._uow._base_source_objects.get(
                acceptance.source_object_id
            ) or self._uow._pending_source_objects.get(acceptance.source_object_id)
            if (
                revision is None
                or source is None
                or acceptance.source_object_id != current.source_object_id
            ):
                self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            decisions = [
                value
                for value in (
                    *self._uow._base_admission_decisions.values(),
                    *self._uow._pending_admission_decisions.values(),
                )
                if value.workspace_id == current.workspace_id
                and value.environment_id == current.environment_id
                and value.source_object_id == acceptance.source_object_id
                and value.revision_id == acceptance.source_revision_id
            ]
            admission = (
                max(decisions, key=lambda value: value.decision_version)
                if decisions
                else None
            )
            content_is_eligible = (
                current.current_revision_kind is RevisionKind.CONTENT
                and revision.kind is RevisionKind.CONTENT
                and acceptance.admission_decision_id is not None
                and admission is not None
                and admission.decision_id == acceptance.admission_decision_id
                and admission.disposition is AdmissionDisposition.ADMITTED
                and admission.validator_profile_version
                in ALLOWED_CANONICAL_ADMISSION_PROFILES
                and acceptance.access_policy_version_id is not None
                and await self._eligible_policy(
                    source.source_object_id,
                    acceptance.access_policy_version_id,
                )
            )
            tombstone_is_eligible = (
                revision.kind is RevisionKind.TOMBSTONE
                and acceptance.admission_decision_id is None
                and acceptance.access_policy_version_id is None
            )
            eligible = content_is_eligible or tombstone_is_eligible
            if not eligible:
                self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            return CanonicalAcceptanceResult(
                source.model_copy(deep=True),
                revision.model_copy(deep=True),
                current,
                acceptance,
                True,
                False,
                claim,
                claim.result,
            )
        revision = self._revisions().get(command.revision.revision_id)
        provenance = self._provenance().get(command.provenance.provenance_id)
        source = self._uow._base_source_objects.get(
            command.source.source_object_id
        ) or self._uow._pending_source_objects.get(command.source.source_object_id)
        if revision is None or provenance is None or source is None:
            self._uow._fail(
                CanonicalEligibilityError(
                    "Canonical acceptance evidence is incomplete."
                )
            )
        if (
            command.source != source
            or command.revision != revision
            or command.provenance != provenance
        ):
            self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical acceptance evidence conflicts."
                )
            )
        current = self._heads().get(command.source.source_object_id)
        comparison: SourceOrderComparison | None = None
        if provenance.order_scheme is not None:
            try:
                self._ordering.compare(
                    str(provenance.order_scheme),
                    str(provenance.order_token),
                    str(provenance.order_token),
                )
            except SourceOrderingPolicyError:
                self._uow._fail(
                    OptimisticConflictError("Trusted source order is invalid.")
                )
        if current is None:
            if (
                provenance.order_scheme is None
                and command.expected_current_revision_id is not None
            ):
                self._uow._fail(
                    OptimisticConflictError("The expected source head is stale.")
                )
        elif (
            provenance.order_scheme is not None
            and current.ordering_scheme == provenance.order_scheme
            and current.ordering_token is not None
        ):
            try:
                comparison = self._ordering.compare(
                    str(provenance.order_scheme),
                    str(provenance.order_token),
                    current.ordering_token,
                )
            except SourceOrderingPolicyError:
                self._uow._fail(
                    OptimisticConflictError("Trusted source order is invalid.")
                )
        elif provenance.order_scheme is not None:
            self._uow._fail(
                OptimisticConflictError("Trusted source order is incomparable.")
            )
        elif command.expected_current_revision_id != current.current_revision_id:
            self._uow._fail(
                OptimisticConflictError("The expected source head is stale.")
            )
        if comparison is SourceOrderComparison.EQUAL and current is not None:
            current_revision = self._revisions().get(current.current_revision_id)
            if (
                current_revision is None
                or current_revision.revision_digest != revision.revision_digest
            ):
                self._uow._fail(
                    OptimisticConflictError(
                        "Trusted source order conflicts with a different revision."
                    )
                )
            acceptance = self._acceptances().get(current.acceptance_id)
            if acceptance is None:
                self._uow._fail(
                    CanonicalEligibilityError(
                        "Current source acceptance is unavailable."
                    )
                )
            decisions = [
                value
                for value in (
                    *self._uow._base_admission_decisions.values(),
                    *self._uow._pending_admission_decisions.values(),
                )
                if value.workspace_id == current.workspace_id
                and value.environment_id == current.environment_id
                and value.source_object_id == current.source_object_id
                and value.revision_id == current.current_revision_id
            ]
            admission = max(decisions, key=lambda value: value.decision_version) if decisions else None
            if current.current_revision_kind is RevisionKind.CONTENT:
                if not (
                    acceptance.admission_decision_id is not None
                    and admission is not None
                    and admission.decision_id == acceptance.admission_decision_id
                    and admission.disposition is AdmissionDisposition.ADMITTED
                    and admission.validator_profile_version in ALLOWED_CANONICAL_ADMISSION_PROFILES
                    and acceptance.access_policy_version_id is not None
                    and await self._eligible_policy(
                        source.source_object_id,
                        acceptance.access_policy_version_id,
                    )
                ):
                    self._uow._fail(
                        CanonicalEligibilityError("Canonical replay is unavailable.")
                    )
            elif not (
                current.current_revision_kind is RevisionKind.TOMBSTONE
                and acceptance.admission_decision_id is None
                and acceptance.access_policy_version_id is None
            ):
                self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            reference = OpaqueResultReference(
                result_type="canonical_acceptance",
                result_id=acceptance.canonical_acceptance_id,
                schema_version=1,
            )
            await self._uow.idempotency.complete(claim, reference)
            return CanonicalAcceptanceResult(
                source.model_copy(deep=True),
                revision.model_copy(deep=True),
                current.model_copy(deep=True),
                acceptance.model_copy(deep=True),
                True,
                False,
                claim,
                reference,
            )
        if comparison is SourceOrderComparison.LOWER:
            self._uow._fail(OptimisticConflictError("Trusted source order is stale."))
        if (
            current is not None
            and current.current_revision_kind is RevisionKind.TOMBSTONE
            and revision.kind is RevisionKind.CONTENT
        ):
            if (
                revision.reappearance_after_tombstone_revision_id
                != current.current_revision_id
            ):
                self._uow._fail(
                    CanonicalEligibilityError(
                        "Source reappearance continuity is invalid."
                    )
                )
        elif revision.reappearance_after_tombstone_revision_id is not None:
            self._uow._fail(
                CanonicalEligibilityError("Source reappearance continuity is invalid.")
            )
        if revision.kind is RevisionKind.CONTENT:
            association = next(
                (
                    value
                    for value in self._receipt_associations().values()
                    if value.workspace_id == scope.workspace_id
                    and value.environment_id == scope.environment_id
                    and value.source_object_id == source.source_object_id
                    and value.revision_id == revision.revision_id
                    and value.provenance_id == provenance.provenance_id
                    and value.verification_status == "verified"
                    and value.storage_status == "finalized_verified"
                    and value.original_sha256 == revision.original_sha256
                    and value.byte_length == revision.byte_length
                    and value.original_reference == revision.original_reference
                ),
                None,
            )
            if association is None:
                self._uow._fail(
                    CanonicalEligibilityError(
                        "Canonical acceptance evidence is incomplete."
                    )
                )
            decisions = [
                value
                for value in (
                    *self._uow._base_admission_decisions.values(),
                    *self._uow._pending_admission_decisions.values(),
                )
                if value.revision_id == revision.revision_id
                and value.source_object_id == source.source_object_id
                and (value.workspace_id, value.environment_id)
                == (scope.workspace_id, scope.environment_id)
            ]
            admission = (
                max(decisions, key=lambda value: value.decision_version)
                if decisions
                else None
            )
            if (
                admission is None
                or admission.disposition is not AdmissionDisposition.ADMITTED
                or admission.validator_profile_version
                not in ALLOWED_CANONICAL_ADMISSION_PROFILES
                or (
                    command.admission_decision_id is None
                    or admission.decision_id != command.admission_decision_id
                )
            ):
                self._uow._fail(
                    CanonicalEligibilityError("Content admission is not eligible.")
                )
            if (
                command.access_policy_version_id is None
                or not await self._eligible_policy(
                    source.source_object_id, command.access_policy_version_id
                )
            ):
                self._uow._fail(
                    CanonicalEligibilityError(
                        "An eligible current access policy is required."
                    )
                )
            admission_id = admission.decision_id
            policy_id = command.access_policy_version_id
        else:
            admission_id = None
            policy_id = None
        generation_key = (scope.workspace_id, scope.environment_id)
        generation = self._uow._base_canonical_generations.get(generation_key, 0)
        generation = (
            self._uow._pending_canonical_generations.get(generation_key, generation) + 1
        )
        ordering = (
            CanonicalOrdering.TRUSTED
            if provenance.order_scheme is not None
            else CanonicalOrdering.EXPECTED_CURRENT
        )
        acceptance = CanonicalAcceptance(
            canonical_acceptance_id=command.canonical_acceptance_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            predecessor_revision_id=current.current_revision_id if current else None,
            predecessor_acceptance_id=current.acceptance_id if current else None,
            accepted_generation=generation,
            ordering=ordering,
            ordering_scheme=provenance.order_scheme,
            ordering_token=provenance.order_token,
            admission_decision_id=admission_id,
            access_policy_version_id=policy_id,
            provenance_id=provenance.provenance_id,
            idempotency_receipt_id=claim.receipt_id,
            audit_event_id=command.audit_event_id,
            outbox_event_id=command.outbox_event_id,
            accepted_at=command.accepted_at,
        )
        self._uow._pending_canonical_acceptances[acceptance.canonical_acceptance_id] = acceptance
        self._uow._pending_canonical_generations[generation_key] = generation
        head = CurrentSourceHead(
            source_object_id=source.source_object_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            current_revision_id=revision.revision_id,
            current_revision_kind=revision.kind,
            acceptance_id=acceptance.canonical_acceptance_id,
            accepted_generation=generation,
            cas_version=current.cas_version + 1 if current else 1,
            ordering_scheme=acceptance.ordering_scheme,
            ordering_token=acceptance.ordering_token,
            changed_at=command.accepted_at,
        )
        self._uow._pending_current_source_heads[source.source_object_id] = head
        try:
            validate_canonical_acceptance_effects(
                command=command,
                acceptance=acceptance,
                context=self._uow._context_snapshot_active(),
                audit_registry=self._uow._audit_events,
                outbox_registry=self._uow._outbox_events,
            )
        except Exception:
            self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical audit or outbox evidence conflicts."
                )
            )
        # The acceptance transaction is the narrow R1 sink for changes after
        # an active publication.  It runs before acceptance audit/outbox and
        # therefore rolls back together with canonical state on failure.
        active_pointers = (
            self._uow._base_active_projections | self._uow._pending_active_projections
        )
        snapshots = self._uow._base_projection_snapshots | self._uow._pending_projection_snapshots
        boundaries = self._uow._base_canonical_boundaries | self._uow._pending_canonical_boundaries
        for pointer in active_pointers.values():
            if (pointer.workspace_id, pointer.environment_id) != (
                acceptance.workspace_id,
                acceptance.environment_id,
            ):
                continue
            snapshot = snapshots.get(pointer.projection_snapshot_id)
            boundary = boundaries.get(snapshot.canonical_boundary_id) if snapshot else None
            if (
                snapshot is None
                or boundary is None
                or acceptance.accepted_generation <= boundary.accepted_through_generation
            ):
                continue
            if boundary.scope_version != "r1":
                self._uow._fail(ConstraintConflictError("Projection boundary scope is unsupported."))
            drift = ProjectionDrift(
                projection_drift_id=uuid4(),
                workspace_id=acceptance.workspace_id,
                environment_id=acceptance.environment_id,
                projection_kind=pointer.projection_kind,
                projection_snapshot_id=pointer.projection_snapshot_id,
                projection_activation_id=pointer.projection_activation_id,
                activation_generation=pointer.activation_generation,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
                source_object_id=acceptance.source_object_id,
                source_revision_id=acceptance.source_revision_id,
                accepted_generation=acceptance.accepted_generation,
                detected_by=ProjectionDriftDetectedBy.CANONICAL_ACCEPTANCE,
                detected_at=command.accepted_at,
            )
            self._uow._pending_projection_drifts[drift.projection_drift_id] = drift
            context = self._uow._context_snapshot_active()
            await self._uow.outbox.append(
                self._uow._outbox_events.build_intent(
                    event_id=uuid4(),
                    workspace_id=acceptance.workspace_id,
                    environment_id=acceptance.environment_id,
                    event_type=PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
                    schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
                    payload=ProjectionCatchUpRequestedOutboxPayload(
                        projection_drift_id=drift.projection_drift_id,
                        projection_activation_id=drift.projection_activation_id,
                        projection_kind=drift.projection_kind,
                        activation_generation=drift.activation_generation,
                        canonical_acceptance_id=acceptance.canonical_acceptance_id,
                        source_object_id=acceptance.source_object_id,
                        source_revision_id=acceptance.source_revision_id,
                        accepted_generation=acceptance.accepted_generation,
                    ),
                    aggregate=OpaqueObjectReference(
                        object_type="projection_drift",
                        object_id=drift.projection_drift_id,
                        schema_version=1,
                    ),
                    producer_deduplication_id=f"projection-catch-up:{drift.projection_drift_id}",
                    trace_id=context.trace_id,
                )
            )
        audit_id = await self._uow.audit.append(command.audit_event)
        event_id = await self._uow.outbox.append(command.outbox_intent)
        if audit_id != command.audit_event_id or event_id != command.outbox_event_id:
            self._uow._fail(ConstraintConflictError("Canonical evidence identity is invalid."))
        reference = OpaqueResultReference(result_type="canonical_acceptance", result_id=acceptance.canonical_acceptance_id, schema_version=1)
        await self._uow.idempotency.complete(claim, reference)
        return CanonicalAcceptanceResult(source.model_copy(deep=True), revision.model_copy(deep=True), head, acceptance, False, True, claim, reference)


class _AuditWriter:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def append(self, event: AuditEvent) -> UUID:
        self._uow._guard_active()

        def operation() -> UUID:
            if self._uow._store.fail_next_audit_append:
                self._uow._store.fail_next_audit_append = False
                self._uow._fail(PersistenceUnavailableError("Persistence is temporarily unavailable."))
            canonical = validate_audit_event_for_context(
                event,
                registry=self._uow._audit_events,
                context=self._uow._context_snapshot_active(),
            )
            duplicate = self._uow._resolve_audit_producer_identity(canonical)
            if duplicate is not None:
                if same_logical_audit_event(duplicate, canonical):
                    assert duplicate.audit_event_id is not None
                    return duplicate.audit_event_id
                self._uow._fail(AuditConflictError("Audit producer identity conflicts with an event."))
            audit_event_id = canonical.audit_event_id or self._uow._audit_event_id_factory()
            if self._uow._resolve_audit_event(audit_event_id) is not None:
                self._uow._fail(AuditConflictError("Audit Event identity already exists."))
            self._uow._pending_audit_events[audit_event_id] = canonical.detached_snapshot(
                update={"audit_event_id": audit_event_id, "appended_at": self._uow._audit_clock()}
            )
            return audit_event_id

        return self._uow._repository_call(operation)


class _CanonicalBoundaryRepository:
    """In-memory implementation of the counter-first boundary protocol."""

    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    def _boundaries(self) -> dict[UUID, CanonicalBoundary]:
        return self._uow._base_canonical_boundaries | self._uow._pending_canonical_boundaries

    def _members(self) -> dict[tuple[UUID, UUID], CanonicalBoundaryMember]:
        return self._uow._base_canonical_boundary_members | self._uow._pending_canonical_boundary_members

    def _heads(self) -> dict[UUID, CurrentSourceHead]:
        return self._uow._base_current_source_heads | self._uow._pending_current_source_heads

    def _revisions(self) -> dict[UUID, SourceRevision]:
        return self._uow._base_source_revisions | self._uow._pending_source_revisions

    async def resolve(self, boundary_id: UUID) -> CanonicalBoundary | None:
        self._uow._guard_active()
        value = self._boundaries().get(boundary_id)
        if value is None or not isinstance(self._uow.scope, EnvironmentScope):
            return None
        if (value.workspace_id, value.environment_id) != (
            self._uow.scope.workspace_id,
            self._uow.scope.environment_id,
        ):
            return None
        return value.model_copy(deep=True)

    async def resolve_members(self, boundary_id: UUID) -> tuple[CanonicalBoundaryMember, ...]:
        boundary = await self.resolve(boundary_id)
        if boundary is None:
            return ()
        return tuple(
            member.model_copy(deep=True)
            for (candidate_id, _), member in sorted(
                self._members().items(), key=lambda item: item[0][1].hex
            )
            if candidate_id == boundary_id
        )

    async def capture(
        self, command: CanonicalBoundaryCaptureCommand
    ) -> CanonicalBoundaryCaptureResult:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))
        if command.expected_digest() != command.digest:
            self._uow._fail(ConstraintConflictError("Canonical boundary command digest is invalid."))
        if not self._uow._canonical_lock_held:
            await self._uow._store.canonical_lock.acquire()
            self._uow._canonical_lock_held = True
        claim = await self._uow.idempotency.claim(
            operation_schema_version=CANONICAL_BOUNDARY_SCHEMA_VERSION,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            boundary = await self.resolve(claim.result.result_id)
            if boundary is None:
                self._uow._fail(ConstraintConflictError("Canonical boundary replay is unavailable."))
            members = await self.resolve_members(boundary.boundary_id)
            if canonical_boundary_manifest_digest(boundary, members) != boundary.manifest_digest:
                self._uow._fail(ConstraintConflictError("Canonical boundary replay is unavailable."))
            return CanonicalBoundaryCaptureResult(
                boundary, members, True, claim, claim.result
            )

        generation_key = (scope.workspace_id, scope.environment_id)
        accepted_through_generation = max(
            self._uow._base_canonical_generations.get(generation_key, 0),
            self._uow._pending_canonical_generations.get(generation_key, 0),
        )
        boundary_id = self._uow._store.boundary_id_factory()
        boundary_time = self._uow._store.boundary_clock()
        heads = sorted(
            (
                head
                for head in self._heads().values()
                if (head.workspace_id, head.environment_id)
                == (scope.workspace_id, scope.environment_id)
                and head.accepted_generation <= accepted_through_generation
            ),
            key=lambda head: head.source_object_id.hex,
        )
        members = tuple(
            CanonicalBoundaryMember(
                boundary_id=boundary_id,
                source_object_id=head.source_object_id,
                source_revision_id=head.current_revision_id,
                acceptance_id=head.acceptance_id,
                workspace_id=head.workspace_id,
                environment_id=head.environment_id,
                revision_kind=head.current_revision_kind,
                accepted_generation=head.accepted_generation,
            )
            for head in heads
            if head.current_revision_id in self._revisions()
        )
        if len(members) != len(heads):
            self._uow._fail(ConstraintConflictError("Canonical boundary membership is incomplete."))
        boundary_without_digest = CanonicalBoundary(
            boundary_id=boundary_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            projection_kind=command.projection_kind,
            scope_version=command.scope_version,
            accepted_through_generation=accepted_through_generation,
            boundary_time=boundary_time,
            manifest_digest="0" * 64,
            idempotency_receipt_id=claim.receipt_id,
        )
        boundary = boundary_without_digest.model_copy(
            update={"manifest_digest": canonical_boundary_manifest_digest(boundary_without_digest, members)}
        )
        self._uow._pending_canonical_boundaries[boundary.boundary_id] = boundary
        for member in members:
            self._uow._pending_canonical_boundary_members[
                (member.boundary_id, member.source_object_id)
            ] = member
        reference = OpaqueResultReference(
            result_type=CANONICAL_BOUNDARY_RESULT_TYPE,
            result_id=boundary.boundary_id,
            schema_version=CANONICAL_BOUNDARY_SCHEMA_VERSION,
        )
        await self._uow.idempotency.complete(claim, reference)
        return CanonicalBoundaryCaptureResult(boundary, members, False, claim, reference)


class _InMemoryIngestionRepository:
    """Transactional in-memory implementation of the operational ledger."""

    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    def _scope_check(self, workspace_id: UUID, environment_id: UUID) -> None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (scope.workspace_id, scope.environment_id) != (workspace_id, environment_id):
            self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))

    def _runs(self) -> dict[UUID, IngestionRun]:
        return self._uow._base_ingestion_runs | self._uow._pending_ingestion_runs

    def _items(self) -> dict[UUID, IngestionRunSource]:
        return self._uow._base_ingestion_items | self._uow._pending_ingestion_items

    def _revisions(self) -> dict[UUID, SourceRevision]:
        return self._uow._base_source_revisions | self._uow._pending_source_revisions

    def _bindings(self) -> dict[UUID, IngestionRunSourceBinding]:
        return self._uow._base_ingestion_bindings | self._uow._pending_ingestion_bindings

    def _transitions(self) -> dict[UUID, IngestionTransition]:
        return self._uow._base_ingestion_transitions | self._uow._pending_ingestion_transitions

    def _warnings(self) -> dict[UUID, IngestionWarning]:
        return self._uow._base_ingestion_warnings | self._uow._pending_ingestion_warnings

    def _failures(self) -> dict[UUID, IngestionFailure]:
        return self._uow._base_ingestion_failures | self._uow._pending_ingestion_failures

    def _processing_receipts(self) -> dict[UUID, ProcessingReceipt]:
        return self._uow._base_processing_receipts | self._uow._pending_processing_receipts

    async def create_run(self, command: IngestionRunCommand) -> IngestionMutationResult:
        self._uow._guard_active()
        self._scope_check(command.run.workspace_id, command.run.environment_id)
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            run = self._runs().get(claim.result.result_id)
            if run is None:
                self._uow._fail(ConstraintConflictError("Ingestion run replay is unavailable."))
            return IngestionMutationResult(run.model_copy(deep=True), True, claim, claim.result)
        run = command.run
        run = run.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        if run.ingestion_run_id in self._runs() and self._runs()[run.ingestion_run_id] != run:
            self._uow._fail(ConstraintConflictError("Ingestion run identity conflicts."))
        if any(
            existing.workspace_id == run.workspace_id
            and existing.environment_id == run.environment_id
            and existing.command_identity == run.command_identity
            and existing.ingestion_run_id != run.ingestion_run_id
            for existing in self._runs().values()
        ):
            self._uow._fail(ConstraintConflictError("Ingestion run command identity conflicts."))
        self._uow._pending_ingestion_runs[run.ingestion_run_id] = run.model_copy(deep=True)
        await self.append_transition(
            IngestionTransition(
                ingestion_transition_id=uuid5(NAMESPACE_URL, f"ingestion-run-root:{run.ingestion_run_id}"),
                workspace_id=run.workspace_id, environment_id=run.environment_id,
                target_kind=IngestionTargetKind.RUN, ingestion_run_id=run.ingestion_run_id,
                state=IngestionRunState.REQUESTED, attempt=1,
                actor_or_service="ingestion-ledger", trace_id=self._uow._trace_id_snapshot(),
                occurred_at=run.requested_at,
            )
        )
        result = OpaqueResultReference(result_type="ingestion_run", result_id=run.ingestion_run_id, schema_version=1)
        await self._uow.idempotency.complete(claim, result)
        return IngestionMutationResult(run.model_copy(deep=True), False, claim, result)

    async def create_item(self, item: IngestionRunSource) -> IngestionRunSource:
        self._uow._guard_active()
        self._scope_check(item.workspace_id, item.environment_id)
        runs = self._runs()
        run = runs.get(item.ingestion_run_id)
        if run is None or (run.workspace_id, run.environment_id) != (item.workspace_id, item.environment_id):
            self._uow._fail(ConstraintConflictError("Ingestion item run reference is invalid."))
        existing = self._items().get(item.ingestion_run_source_id)
        if existing is not None:
            if existing != item:
                self._uow._fail(ConstraintConflictError("Ingestion item identity conflicts."))
            return existing.model_copy(deep=True)
        if any(existing.ingestion_run_id == item.ingestion_run_id and existing.input_ordinal == item.input_ordinal for existing in self._items().values()):
            self._uow._fail(ConstraintConflictError("Ingestion item ordinal already exists."))
        if any(existing.ingestion_run_id == item.ingestion_run_id and existing.input_identity == item.input_identity for existing in self._items().values()):
            self._uow._fail(ConstraintConflictError("Ingestion item identity already exists."))
        if any(
            existing.workspace_id == item.workspace_id
            and existing.environment_id == item.environment_id
            and existing.command_identity == item.command_identity
            and existing.ingestion_run_source_id != item.ingestion_run_source_id
            for existing in self._items().values()
        ):
            self._uow._fail(ConstraintConflictError("Ingestion item command identity conflicts."))
        self._uow._pending_ingestion_items[item.ingestion_run_source_id] = item.model_copy(deep=True)
        await self.append_transition(
            IngestionTransition(
                ingestion_transition_id=uuid5(NAMESPACE_URL, f"ingestion-item-root:{item.ingestion_run_source_id}"), workspace_id=item.workspace_id, environment_id=item.environment_id,
                target_kind=IngestionTargetKind.ITEM, ingestion_run_id=item.ingestion_run_id,
                ingestion_run_source_id=item.ingestion_run_source_id, state=IngestionItemState.RECEIVED,
                attempt=1, actor_or_service="ingestion-ledger", trace_id=self._uow._trace_id_snapshot(),
                occurred_at=item.created_at,
            )
        )
        return item.model_copy(deep=True)

    async def bind_source(self, command: IngestionBindingCommand) -> IngestionMutationResult:
        self._uow._guard_active()
        self._scope_check(command.item.workspace_id, command.item.environment_id)
        if command.observation.revision.kind.value == "content":
            association = command.observation.receipt_association
            binding = command.binding
            if (
                association is None
                or binding.original_receipt_id != association.original_receipt_id
                or binding.original_reference != association.original_reference
                or binding.original_receipt_digest != association.original_sha256
                or binding.original_receipt_kind != association.original_receipt_kind
            ):
                self._uow._fail(
                    ConstraintConflictError(
                        "Original receipt verification is unavailable or incomplete."
                    )
                )
        if command.observation.revision.kind.value != "content" and command.binding.original_receipt_id is not None:
            self._uow._fail(
                ConstraintConflictError(
                    "Contentless ingestion binding cannot include an original receipt."
                )
            )
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        item = self._items().get(command.item.ingestion_run_source_id)
        if item is None or item != command.item:
            self._uow._fail(ConstraintConflictError("Ingestion binding item is invalid."))
        existing = next((value for value in self._bindings().values() if value.ingestion_run_source_id == item.ingestion_run_source_id), None)
        if isinstance(claim, IdempotencyReplay):
            binding = self._bindings().get(claim.result.result_id)
            if binding is None:
                self._uow._fail(ConstraintConflictError("Ingestion binding replay is unavailable."))
            return IngestionMutationResult(binding.model_copy(deep=True), True, claim, claim.result)
        binding = command.binding
        if (binding.workspace_id, binding.environment_id) != (item.workspace_id, item.environment_id) or binding.ingestion_run_source_id != item.ingestion_run_source_id:
            self._uow._fail(ConstraintConflictError("Ingestion binding scope is invalid."))
        if existing is not None:
            if (
                existing.source_object_id,
                existing.source_revision_id,
                existing.original_reference,
                existing.original_receipt_kind,
                existing.original_receipt_id,
                existing.original_receipt_digest,
            ) != (
                binding.source_object_id,
                binding.source_revision_id,
                binding.original_reference,
                binding.original_receipt_kind,
                binding.original_receipt_id,
                binding.original_receipt_digest,
            ):
                self._uow._fail(ConstraintConflictError("Ingestion item is already bound to another source."))
            result = OpaqueResultReference(result_type="ingestion_binding", result_id=existing.binding_id, schema_version=1)
            await self._uow.idempotency.complete(claim, result)
            return IngestionMutationResult(existing.model_copy(deep=True), True, claim, result)
        if any(
            value.workspace_id == binding.workspace_id
            and value.environment_id == binding.environment_id
            and value.binding_command_identity == binding.binding_command_identity
            and value.binding_id != binding.binding_id
            for value in self._bindings().values()
        ):
            self._uow._fail(ConstraintConflictError("Ingestion binding command identity conflicts."))
        observation = command.observation
        if observation.source.source_object_id != binding.source_object_id or observation.revision.revision_id != binding.source_revision_id:
            self._uow._fail(ConstraintConflictError("Ingestion binding observation is invalid."))
        # The source observation is recorded in the same transaction. Its
        # operation is adapted to the trusted binding seam so it cannot commit
        # a claim for a different operation; the binding has its own claim.
        from dataclasses import replace
        adapted_digest = observation.expected_digest(PersistenceOperation("ingestion_binding"))
        observation = replace(observation, digest=adapted_digest)
        observed = await self._uow.sources.record_observation(observation)
        if binding.source_object_id != observed.source.source_object_id or binding.source_revision_id != observed.revision.revision_id:
            self._uow._fail(ConstraintConflictError("Ingestion binding observation result is invalid."))
        binding = binding.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        self._uow._pending_ingestion_bindings[binding.binding_id] = binding.model_copy(deep=True)
        item_head = self._head(item.ingestion_run_source_id, IngestionTargetKind.ITEM)
        if item_head is None:
            self._uow._fail(ConstraintConflictError("Ingestion item transition head is unavailable."))
        await self.append_transition(
            IngestionTransition(
                ingestion_transition_id=uuid4(), workspace_id=item.workspace_id, environment_id=item.environment_id,
                target_kind=IngestionTargetKind.ITEM, ingestion_run_id=item.ingestion_run_id,
                ingestion_run_source_id=item.ingestion_run_source_id, state=IngestionItemState.OBSERVED,
                predecessor_transition_id=item_head.ingestion_transition_id,
                attempt=1, actor_or_service="ingestion-ledger", trace_id=self._uow._trace_id_snapshot(),
                occurred_at=binding.bound_at,
            )
        )
        result = OpaqueResultReference(result_type="ingestion_binding", result_id=binding.binding_id, schema_version=1)
        await self._uow.idempotency.complete(claim, result)
        return IngestionMutationResult(binding.model_copy(deep=True), False, claim, result)

    def _head(self, target_id: UUID, target_kind: IngestionTargetKind) -> IngestionTransition | None:
        candidates = [
            transition for transition in self._transitions().values()
            if transition.target_kind is target_kind
            and ((target_kind is IngestionTargetKind.RUN and transition.ingestion_run_id == target_id) or (target_kind is IngestionTargetKind.ITEM and transition.ingestion_run_source_id == target_id))
        ]
        if not candidates:
            return None
        child_ids = {transition.predecessor_transition_id for transition in candidates if transition.predecessor_transition_id is not None}
        heads = [transition for transition in candidates if transition.ingestion_transition_id not in child_ids]
        if len(heads) != 1:
            self._uow._fail(ConstraintConflictError("Ingestion transition history has multiple heads."))
        return heads[0]

    async def append_transition(self, transition: IngestionTransition) -> IngestionTransition:
        self._uow._guard_active()
        self._scope_check(transition.workspace_id, transition.environment_id)
        existing = self._transitions().get(transition.ingestion_transition_id)
        if existing is not None:
            if existing != transition:
                self._uow._fail(ConstraintConflictError("Ingestion transition identity conflicts."))
            return existing.model_copy(deep=True)
        target_id = transition.ingestion_run_id if transition.target_kind is IngestionTargetKind.RUN else transition.ingestion_run_source_id
        assert target_id is not None
        if transition.target_kind is IngestionTargetKind.RUN:
            run = self._runs().get(transition.ingestion_run_id)
            if run is None or (run.workspace_id, run.environment_id) != (transition.workspace_id, transition.environment_id):
                self._uow._fail(ConstraintConflictError("Ingestion transition run reference is invalid."))
        else:
            item = self._items().get(target_id)
            if item is None or (item.workspace_id, item.environment_id) != (transition.workspace_id, transition.environment_id) or item.ingestion_run_id != transition.ingestion_run_id:
                self._uow._fail(ConstraintConflictError("Ingestion transition item reference is invalid."))
        previous = self._head(target_id, transition.target_kind)
        try:
            validate_transition(previous, transition)
        except ValueError as error:
            self._uow._fail(ConstraintConflictError(str(error)))
        self._uow._pending_ingestion_transitions[transition.ingestion_transition_id] = transition.model_copy(deep=True)
        return transition.model_copy(deep=True)

    async def append_warning(self, warning: IngestionWarning) -> IngestionWarning:
        self._uow._guard_active()
        self._scope_check(warning.workspace_id, warning.environment_id)
        item = self._items().get(warning.ingestion_run_source_id)
        if item is None or (item.workspace_id, item.environment_id) != (warning.workspace_id, warning.environment_id):
            self._uow._fail(ConstraintConflictError("Ingestion warning item reference is invalid."))
        existing = self._warnings().get(warning.ingestion_warning_id)
        if existing is not None and existing != warning:
            self._uow._fail(ConstraintConflictError("Ingestion warning identity conflicts."))
        if any(
            value.workspace_id == warning.workspace_id
            and value.environment_id == warning.environment_id
            and value.ingestion_run_source_id == warning.ingestion_run_source_id
            and value.attempt == warning.attempt
            and value.code == warning.code
            and value.safe_detail == warning.safe_detail
            and value.ingestion_warning_id != warning.ingestion_warning_id
            for value in self._warnings().values()
        ):
            self._uow._fail(ConstraintConflictError("Duplicate ingestion warning."))
        self._uow._pending_ingestion_warnings[warning.ingestion_warning_id] = warning.model_copy(deep=True)
        return warning.model_copy(deep=True)

    async def append_failure(self, failure: IngestionFailure) -> IngestionFailure:
        self._uow._guard_active()
        self._scope_check(failure.workspace_id, failure.environment_id)
        item = self._items().get(failure.ingestion_run_source_id)
        if item is None or (item.workspace_id, item.environment_id) != (failure.workspace_id, failure.environment_id):
            self._uow._fail(ConstraintConflictError("Ingestion failure item reference is invalid."))
        existing = self._failures().get(failure.ingestion_failure_id)
        if existing is not None and existing != failure:
            self._uow._fail(ConstraintConflictError("Ingestion failure identity conflicts."))
        if any(
            value.workspace_id == failure.workspace_id
            and value.environment_id == failure.environment_id
            and value.ingestion_run_source_id == failure.ingestion_run_source_id
            and value.attempt == failure.attempt
            and value.code == failure.code
            and value.safe_detail == failure.safe_detail
            and value.ingestion_failure_id != failure.ingestion_failure_id
            for value in self._failures().values()
        ):
            self._uow._fail(ConstraintConflictError("Duplicate ingestion failure."))
        self._uow._pending_ingestion_failures[failure.ingestion_failure_id] = failure.model_copy(deep=True)
        return failure.model_copy(deep=True)

    async def record_processing_receipt(self, command: ProcessingReceiptCommand) -> IngestionMutationResult:
        self._uow._guard_active()
        receipt = command.receipt
        self._scope_check(receipt.workspace_id, receipt.environment_id)
        claim = await self._uow.idempotency.claim(operation_schema_version=command.digest.operation_schema_version, key=command.idempotency_key, digest=command.digest)
        if isinstance(claim, IdempotencyReplay):
            existing = self._processing_receipts().get(claim.result.result_id)
            if existing is None:
                self._uow._fail(ConstraintConflictError("Processing receipt replay is unavailable."))
            return IngestionMutationResult(existing.model_copy(deep=True), True, claim, claim.result)
        receipt = receipt.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        item = self._items().get(receipt.ingestion_run_source_id)
        binding = self._bindings().get(receipt.binding_id)
        revision = self._revisions().get(receipt.source_revision_id)
        if (
            item is None
            or binding is None
            or revision is None
            or binding.ingestion_run_source_id != receipt.ingestion_run_source_id
            or binding.source_object_id != receipt.source_object_id
            or binding.source_revision_id != receipt.source_revision_id
            or revision.original_sha256 != receipt.original_sha256
        ):
            self._uow._fail(ConstraintConflictError("Processing receipt references are invalid."))
        if any(
            value.workspace_id == receipt.workspace_id
            and value.environment_id == receipt.environment_id
            and value.command_identity == receipt.command_identity
            and value.processing_receipt_id != receipt.processing_receipt_id
            for value in self._processing_receipts().values()
        ):
            self._uow._fail(ConstraintConflictError("Processing receipt command identity conflicts."))
        existing = self._processing_receipts().get(receipt.processing_receipt_id)
        if existing is not None and existing != receipt:
            self._uow._fail(ConstraintConflictError("Processing receipt identity conflicts."))
        self._uow._pending_processing_receipts[receipt.processing_receipt_id] = receipt.model_copy(deep=True)
        result = OpaqueResultReference(result_type="processing_receipt", result_id=receipt.processing_receipt_id, schema_version=1)
        await self._uow.idempotency.complete(claim, result)
        return IngestionMutationResult(receipt.model_copy(deep=True), False, claim, result)

    async def derive_run_state(self, run_id: UUID) -> IngestionRunStateView | None:
        self._uow._guard_active()
        if not isinstance(self._uow.scope, EnvironmentScope):
            return None
        run = self._runs().get(run_id)
        if run is None or (run.workspace_id, run.environment_id) != (self._uow.scope.workspace_id, self._uow.scope.environment_id):
            return None
        run_head = self._head(run_id, IngestionTargetKind.RUN)
        if run_head is None:
            self._uow._fail(ConstraintConflictError("Ingestion run transition head is unavailable."))
        items = tuple(
            sorted(
                (
                    item
                    for item in self._items().values()
                    if item.ingestion_run_id == run_id
                ),
                key=lambda item: item.input_ordinal,
            )
        )
        item_heads = tuple(
            self._head(item.ingestion_run_source_id, IngestionTargetKind.ITEM)
            for item in items
        )
        item_states = tuple(head.state for head in item_heads if head is not None)
        state = run_head.state
        if state is IngestionRunState.PROCESSING and item_states and all(item_state in {IngestionItemState.PROCESSED, IngestionItemState.PROJECTION_REQUESTED} for item_state in item_states):
            state = IngestionRunState.SUCCEEDED
        elif state is IngestionRunState.PROCESSING and any(item_state in {IngestionItemState.QUARANTINED, IngestionItemState.REJECTED, IngestionItemState.PROCESSING_FAILED} for item_state in item_states) and any(item_state in {IngestionItemState.PROCESSED, IngestionItemState.PROJECTION_REQUESTED} for item_state in item_states):
            state = IngestionRunState.PARTIALLY_SUCCEEDED
        elif state is IngestionRunState.PROCESSING and item_states and all(item_state in {IngestionItemState.QUARANTINED, IngestionItemState.REJECTED, IngestionItemState.PROCESSING_FAILED} for item_state in item_states):
            state = IngestionRunState.FAILED
        return IngestionRunStateView(
            run=run.model_copy(deep=True),
            state=state,
            items=tuple(item.model_copy(deep=True) for item in items),
            item_states=item_states,
        )

    async def resolve_item(self, item_id: UUID) -> IngestionRunSource | None:
        self._uow._guard_active()
        item = self._items().get(item_id)
        if item is None or (item.workspace_id, item.environment_id) != (self._uow.scope.workspace_id, self._uow.scope.environment_id):
            return None
        return item.model_copy(deep=True)

    async def resolve_binding(self, item_id: UUID) -> IngestionRunSourceBinding | None:
        self._uow._guard_active()
        binding = next(
            (
                value
                for value in self._bindings().values()
                if value.ingestion_run_source_id == item_id
                and value.workspace_id == self._uow.scope.workspace_id
                and value.environment_id == self._uow.scope.environment_id
            ),
            None,
        )
        return binding.model_copy(deep=True) if binding is not None else None

    async def resolve_run_head(self, run_id: UUID) -> IngestionTransition | None:
        self._uow._guard_active()
        run = self._runs().get(run_id)
        if run is None or not isinstance(self._uow.scope, EnvironmentScope) or (
            run.workspace_id,
            run.environment_id,
        ) != (self._uow.scope.workspace_id, self._uow.scope.environment_id):
            return None
        head = self._head(run_id, IngestionTargetKind.RUN)
        return head.model_copy(deep=True) if head is not None else None

    async def resolve_item_head(self, item_id: UUID) -> IngestionTransition | None:
        self._uow._guard_active()
        item = self._items().get(item_id)
        if item is None or not isinstance(self._uow.scope, EnvironmentScope) or (
            item.workspace_id,
            item.environment_id,
        ) != (self._uow.scope.workspace_id, self._uow.scope.environment_id):
            return None
        head = self._head(item_id, IngestionTargetKind.ITEM)
        return head.model_copy(deep=True) if head is not None else None

    async def resolve_processing_receipt(self, item_id: UUID) -> ProcessingReceipt | None:
        self._uow._guard_active()
        receipts = tuple(
            value for value in self._processing_receipts().values()
            if value.ingestion_run_source_id == item_id
        )
        if not receipts:
            return None
        return max(receipts, key=lambda value: value.completed_at).model_copy(deep=True)

    async def resolve_warnings(self, item_id: UUID) -> tuple[IngestionWarning, ...]:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return ()
        return tuple(
            value.model_copy(deep=True)
            for value in sorted(
                self._warnings().values(),
                key=lambda value: (value.attempt, value.occurred_at, str(value.ingestion_warning_id)),
            )
            if value.ingestion_run_source_id == item_id
            and value.workspace_id == scope.workspace_id
            and value.environment_id == scope.environment_id
        )

    async def resolve_failures(self, item_id: UUID) -> tuple[IngestionFailure, ...]:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return ()
        return tuple(
            value.model_copy(deep=True)
            for value in sorted(
                self._failures().values(),
                key=lambda value: (value.attempt, value.occurred_at, str(value.ingestion_failure_id)),
            )
            if value.ingestion_run_source_id == item_id
            and value.workspace_id == scope.workspace_id
            and value.environment_id == scope.environment_id
        )


class _IdempotencyRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def claim(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> IdempotencyClaimResult:
        self._uow._guard_active()

        def repository_operation() -> IdempotencyClaimResult:
            operation = self._uow._operation_snapshot()
            if (
                digest.operation != operation
                or digest.operation_schema_version != operation_schema_version
            ):
                self._uow._fail(
                    IdempotencyConflictError(
                        "Idempotency digest does not match the trusted operation."
                    )
                )
            receipt_key = self._uow._receipt_key(
                operation=operation,
                operation_schema_version=operation_schema_version,
                key=key,
            )
            existing = self._uow._resolve_receipt(receipt_key)
            if existing is not None:
                if existing.digest != digest:
                    self._uow._fail(idempotency_conflict())
                if existing.result is None:
                    self._uow._fail(
                        IdempotencyConflictError("Idempotency key is already claimed.")
                    )
                return IdempotencyReplay(
                    kind="replay",
                    receipt_id=existing.receipt_id,
                    operation=operation,
                    operation_schema_version=operation_schema_version,
                    key=key,
                    result=existing.result,
                )
            receipt_id = _receipt_id(receipt_key)
            claim = OwnedIdempotencyClaim(
                kind="owned",
                receipt_id=receipt_id,
                operation=operation,
                operation_schema_version=operation_schema_version,
                key=key,
                digest=digest,
            )
            self._uow._pending_receipts[receipt_key] = _IdempotencyReceipt(
                receipt_id=receipt_id,
                key=receipt_key,
                digest=digest,
                result=None,
            )
            return claim

        return self._uow._repository_call(repository_operation)

    async def complete(
        self,
        claim: OwnedIdempotencyClaim,
        result: OpaqueResultReference,
    ) -> None:
        self._uow._guard_active()

        def repository_operation() -> None:
            if not isinstance(claim, OwnedIdempotencyClaim):
                self._uow._fail(
                    IdempotencyConflictError("Only an owned idempotency claim can complete.")
                )
            if claim.operation != self._uow._operation_snapshot():
                self._uow._fail(
                    IdempotencyConflictError("Idempotency claim cannot be completed.")
                )
            if (
                claim.digest.operation != claim.operation
                or claim.digest.operation_schema_version != claim.operation_schema_version
            ):
                self._uow._fail(
                    IdempotencyConflictError("Idempotency claim cannot be completed.")
                )
            receipt_key = self._uow._receipt_key(
                operation=claim.operation,
                operation_schema_version=claim.operation_schema_version,
                key=claim.key,
            )
            pending = self._uow._pending_receipts.get(receipt_key)
            if (
                pending is None
                or pending.receipt_id != claim.receipt_id
                or pending.digest != claim.digest
                or pending.result is not None
            ):
                self._uow._fail(
                    IdempotencyConflictError("Idempotency claim cannot be completed.")
                )
            self._uow._pending_receipts[receipt_key] = _IdempotencyReceipt(
                receipt_id=pending.receipt_id,
                key=pending.key,
                digest=pending.digest,
                result=result,
            )

        self._uow._repository_call(repository_operation)


class _OutboxWriter:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def append(self, intent: OutboxIntent) -> UUID:
        self._uow._guard_active()

        def repository_operation() -> UUID:
            canonical = validate_outbox_intent_for_context(
                intent,
                registry=self._uow._outbox_events,
                scope=self._uow.scope,
                trace_id=self._uow._trace_id_snapshot(),
            )
            event_id = canonical.event_id or self._uow._event_id_factory()
            if self._uow._resolve_outbox_intent(event_id) is not None:
                self._uow._fail(
                    OutboxConflictError("Outbox event identity already exists.")
                )
            if self._uow._has_producer_identity(canonical):
                self._uow._fail(
                    OutboxConflictError(
                        "Outbox producer identity already exists."
                    )
                )
            copied = canonical.model_copy(deep=True, update={"event_id": event_id})
            self._uow._pending_outbox_intents[event_id] = copied
            return event_id

        return self._uow._repository_call(repository_operation)


class InMemoryUnitOfWork:
    def __init__(
        self,
        store: _Store,
        source_context: TrustedPersistenceContext,
        context_snapshot: TrustedPersistenceContext,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        event_id_factory: Callable[[], UUID],
        audit_events: AuditEventRegistry,
        audit_clock: Callable[[], datetime],
        audit_event_id_factory: Callable[[], UUID],
    ) -> None:
        if not isinstance(source_context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        self._store = store
        self._source_context = source_context
        self._context_snapshot = context_snapshot
        self._active_context: TrustedPersistenceContext | None = None
        self._context_verifier = context_verifier
        self._outbox_events = outbox_events
        self._event_id_factory = event_id_factory
        self._audit_events = audit_events
        self._audit_clock = audit_clock
        self._audit_event_id_factory = audit_event_id_factory
        self._scope: PersistenceScope | None = None
        self._operation: PersistenceOperation | None = None
        self._trace_id: UUID | None = None
        self._initialized = False
        self._lifecycle = _Lifecycle.NEW
        self._owner: asyncio.Task[object] | None = None
        self._base_workspaces: dict[UUID, Workspace] = {}
        self._base_environments: dict[UUID, Environment] = {}
        self._base_context_profiles: dict[tuple[UUID, UUID, UUID, int], ContextProfileVersion] = {}
        self._base_context_bundles: dict[tuple[UUID, UUID, UUID, int], ContextBundle] = {}
        self._base_receipts: dict[_ReceiptKey, _IdempotencyReceipt] = {}
        self._base_outbox_intents: dict[UUID, OutboxIntent] = {}
        self._base_source_objects: dict[UUID, SourceObject] = {}
        self._base_source_revisions: dict[UUID, SourceRevision] = {}
        self._base_source_provenance: dict[UUID, SourceRevisionProvenance] = {}
        self._base_source_receipt_associations: dict[UUID, SourceRevisionReceiptAssociation] = {}
        self._base_admission_decisions: dict[UUID, AdmissionDecision] = {}
        self._base_audit_events: dict[UUID, AuditEvent] = {}
        self._base_ingestion_runs: dict[UUID, IngestionRun] = {}
        self._base_ingestion_items: dict[UUID, IngestionRunSource] = {}
        self._base_ingestion_bindings: dict[UUID, IngestionRunSourceBinding] = {}
        self._base_ingestion_transitions: dict[UUID, IngestionTransition] = {}
        self._base_ingestion_warnings: dict[UUID, IngestionWarning] = {}
        self._base_ingestion_failures: dict[UUID, IngestionFailure] = {}
        self._base_processing_receipts: dict[UUID, ProcessingReceipt] = {}
        self._base_canonical_acceptances: dict[UUID, CanonicalAcceptance] = {}
        self._base_current_source_heads: dict[UUID, CurrentSourceHead] = {}
        self._base_canonical_generations: dict[tuple[UUID, UUID], int] = {}
        self._base_canonical_boundaries: dict[UUID, CanonicalBoundary] = {}
        self._base_canonical_boundary_members: dict[tuple[UUID, UUID], CanonicalBoundaryMember] = {}
        self._base_ontology_versions: dict[UUID, object] = {}
        self._base_projection_configs: dict[UUID, object] = {}
        self._base_projection_runs: dict[UUID, object] = {}
        self._base_projection_transitions: dict[UUID, object] = {}
        self._base_projection_partitions: dict[UUID, object] = {}
        self._base_projection_receipts: dict[UUID, object] = {}
        self._base_projection_references: dict[UUID, object] = {}
        self._base_projection_validations: dict[UUID, object] = {}
        self._base_projection_snapshots: dict[UUID, object] = {}
        self._base_projection_snapshot_members: dict[UUID, object] = {}
        self._base_projection_reconciliation_results: dict[UUID, object] = {}
        self._base_projection_activations: dict[UUID, ProjectionActivation] = {}
        self._base_active_projections: dict[tuple[UUID, UUID, str], ActiveProjection] = {}
        self._base_projection_drifts: dict[UUID, ProjectionDrift] = {}
        self._pending_workspaces: dict[UUID, Workspace] = {}
        self._pending_environments: dict[UUID, Environment] = {}
        self._pending_context_profiles: dict[tuple[UUID, UUID, UUID, int], ContextProfileVersion] = {}
        self._pending_context_bundles: dict[tuple[UUID, UUID, UUID, int], ContextBundle] = {}
        self._pending_receipts: dict[_ReceiptKey, _IdempotencyReceipt] = {}
        self._pending_outbox_intents: dict[UUID, OutboxIntent] = {}
        self._pending_source_objects: dict[UUID, SourceObject] = {}
        self._pending_source_revisions: dict[UUID, SourceRevision] = {}
        self._pending_source_provenance: dict[UUID, SourceRevisionProvenance] = {}
        self._pending_source_receipt_associations: dict[UUID, SourceRevisionReceiptAssociation] = {}
        self._pending_admission_decisions: dict[UUID, AdmissionDecision] = {}
        self._pending_audit_events: dict[UUID, AuditEvent] = {}
        self._pending_ingestion_runs: dict[UUID, IngestionRun] = {}
        self._pending_ingestion_items: dict[UUID, IngestionRunSource] = {}
        self._pending_ingestion_bindings: dict[UUID, IngestionRunSourceBinding] = {}
        self._pending_ingestion_transitions: dict[UUID, IngestionTransition] = {}
        self._pending_ingestion_warnings: dict[UUID, IngestionWarning] = {}
        self._pending_ingestion_failures: dict[UUID, IngestionFailure] = {}
        self._pending_processing_receipts: dict[UUID, ProcessingReceipt] = {}
        self._pending_canonical_acceptances: dict[UUID, CanonicalAcceptance] = {}
        self._pending_current_source_heads: dict[UUID, CurrentSourceHead] = {}
        self._pending_canonical_generations: dict[tuple[UUID, UUID], int] = {}
        self._pending_canonical_boundaries: dict[UUID, CanonicalBoundary] = {}
        self._pending_canonical_boundary_members: dict[tuple[UUID, UUID], CanonicalBoundaryMember] = {}
        self._pending_ontology_versions: dict[UUID, object] = {}
        self._pending_projection_configs: dict[UUID, object] = {}
        self._pending_projection_runs: dict[UUID, object] = {}
        self._pending_projection_transitions: dict[UUID, object] = {}
        self._pending_projection_partitions: dict[UUID, object] = {}
        self._pending_projection_receipts: dict[UUID, object] = {}
        self._pending_projection_references: dict[UUID, object] = {}
        self._pending_projection_validations: dict[UUID, object] = {}
        self._pending_projection_snapshots: dict[UUID, object] = {}
        self._pending_projection_snapshot_members: dict[UUID, object] = {}
        self._pending_projection_reconciliation_results: dict[UUID, object] = {}
        self._pending_projection_activations: dict[UUID, ProjectionActivation] = {}
        self._pending_active_projections: dict[tuple[UUID, UUID, str], ActiveProjection] = {}
        self._pending_projection_drifts: dict[UUID, ProjectionDrift] = {}
        self._pending_projection_run_claims: dict[UUID, OwnedIdempotencyClaim] = {}
        self._source_observation_lock_held = False
        self._canonical_lock_held = False
        self._workspaces = _WorkspaceRepository(self)
        self._environments = _EnvironmentRepository(self)
        self._context_profiles = _ContextProfileRepository(self)
        self._context_bundles = _ContextBundleRepository(self)
        self._idempotency = _IdempotencyRepository(self)
        self._outbox = _OutboxWriter(self)
        self._sources = _SourceObservationRepository(self)
        self._ingestion = _InMemoryIngestionRepository(self)
        self._admissions = _AdmissionRepository(self)
        self._canonical = _CanonicalAcceptanceRepository(self)
        self._boundaries = _CanonicalBoundaryRepository(self)
        self._projections = InMemoryProjectionRepository(self)
        self._audit = _AuditWriter(self)

    @property
    def workspaces(self) -> _WorkspaceRepository:
        self._guard_active()
        return self._workspaces

    @property
    def environments(self) -> _EnvironmentRepository:
        self._guard_active()
        return self._environments

    @property
    def context_profiles(self) -> _ContextProfileRepository:
        self._guard_active()
        return self._context_profiles

    @property
    def context_bundles(self) -> _ContextBundleRepository:
        self._guard_active()
        return self._context_bundles

    @property
    def idempotency(self) -> _IdempotencyRepository:
        self._guard_active()
        return self._idempotency

    @property
    def outbox(self) -> _OutboxWriter:
        self._guard_active()
        return self._outbox

    @property
    def sources(self) -> _SourceObservationRepository:
        self._guard_active()
        return self._sources

    @property
    def ingestion(self) -> IngestionRepository:
        self._guard_active()
        return self._ingestion

    @property
    def admissions(self) -> _AdmissionRepository:
        self._guard_active()
        return self._admissions

    @property
    def canonical(self) -> "_CanonicalAcceptanceRepository":
        self._guard_active()
        return self._canonical

    @property
    def boundaries(self) -> _CanonicalBoundaryRepository:
        self._guard_active()
        return self._boundaries

    @property
    def canonical_boundaries(self) -> _CanonicalBoundaryRepository:
        self._guard_active()
        return self._boundaries

    @property
    def projections(self) -> InMemoryProjectionRepository:
        self._guard_active()
        return self._projections

    @property
    def projection(self) -> InMemoryProjectionRepository:
        self._guard_active()
        return self._projections

    @property
    def access_policies(self) -> InMemoryAccessPolicyRepository:
        self._guard_active()
        repository = self._store.canonical_policy_repository
        if repository is None:
            raise UnitOfWorkLifecycleError(
                "Access Policy persistence is not configured."
            )
        return repository

    @property
    def audit(self) -> _AuditWriter:
        self._guard_active()
        return self._audit

    def _operation_snapshot(self) -> PersistenceOperation:
        self._guard_active()
        if self._operation is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._operation

    def _context_snapshot_active(self) -> TrustedPersistenceContext:
        self._guard_active()
        if self._active_context is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._active_context

    def _trace_id_snapshot(self) -> UUID:
        self._guard_active()
        if self._trace_id is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._trace_id

    @property
    def scope(self) -> PersistenceScope:
        self._guard_active()
        if self._scope is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._scope

    async def __aenter__(self) -> "InMemoryUnitOfWork":
        if self._lifecycle is not _Lifecycle.NEW:
            raise UnitOfWorkLifecycleError("Unit of Work cannot be entered more than once.")
        self._lifecycle = _Lifecycle.ENTERING
        self._owner = asyncio.current_task()
        try:
            async with self._store.lock:
                context_snapshot = self._context_verifier.verify(self._source_context)
                if context_snapshot != self._context_snapshot:
                    raise InvalidPersistenceContextError(
                        "Persistence context is invalid."
                    )
                self._base_workspaces = {
                    key: _workspace_copy(value) for key, value in self._store.workspaces.items()
                }
                self._base_environments = {
                    key: _environment_copy(value)
                    for key, value in self._store.environments.items()
                }
                self._base_context_profiles = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.context_profiles.items()
                }
                self._base_context_bundles = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.context_bundles.items()
                }
                self._base_receipts = {
                    key: _receipt_copy(value)
                    for key, value in self._store.idempotency_receipts.items()
                }
                self._base_outbox_intents = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.outbox_intents.items()
                }
                self._base_source_objects = {
                    key: value.model_copy(deep=True) for key, value in self._store.source_objects.items()
                }
                self._base_source_revisions = {
                    key: value.model_copy(deep=True) for key, value in self._store.source_revisions.items()
                }
                self._base_source_provenance = {
                    key: value.model_copy(deep=True) for key, value in self._store.source_provenance.items()
                }
                self._base_source_receipt_associations = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.source_receipt_associations.items()
                }
                self._base_admission_decisions = {
                    key: value.model_copy(deep=True) for key, value in self._store.admission_decisions.items()
                }
                self._base_audit_events = {
                    key: _audit_event_copy(value) for key, value in self._store.audit_events.items()
                }
                self._base_ingestion_runs = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_runs.items()}
                self._base_ingestion_items = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_items.items()}
                self._base_ingestion_bindings = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_bindings.items()}
                self._base_ingestion_transitions = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_transitions.items()}
                self._base_ingestion_warnings = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_warnings.items()}
                self._base_ingestion_failures = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_failures.items()}
                self._base_processing_receipts = {key: value.model_copy(deep=True) for key, value in self._store.processing_receipts.items()}
                self._base_canonical_acceptances = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.canonical_acceptances.items()
                }
                self._base_current_source_heads = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.current_source_heads.items()
                }
                self._base_canonical_generations = dict(self._store.canonical_generations)
                self._base_canonical_boundaries = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.canonical_boundaries.items()
                }
                self._base_canonical_boundary_members = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.canonical_boundary_members.items()
                }
                self._base_ontology_versions = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.ontology_versions.items()
                }
                self._base_projection_configs = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_configs.items()
                }
                self._base_projection_runs = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_runs.items()
                }
                self._base_projection_transitions = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_transitions.items()
                }
                self._base_projection_partitions = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_partitions.items()
                }
                self._base_projection_receipts = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_receipts.items()
                }
                self._base_projection_references = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_references.items()
                }
                self._base_projection_validations = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_validations.items()
                }
                self._base_projection_snapshots = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_snapshots.items()
                }
                self._base_projection_snapshot_members = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_snapshot_members.items()
                }
                self._base_projection_reconciliation_results = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_reconciliation_results.items()
                }
                self._base_projection_activations = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_activations.items()
                }
                self._base_active_projections = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.active_projections.items()
                }
                self._base_projection_drifts = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_drifts.items()
                }
                self._initialized = self._store.initialized
                if not self._initialized:
                    raise InvalidBootstrapAuthorityError(
                        "Initial bootstrap is not authorized."
                    )
                self._validate_context(context_snapshot)
                self._active_context = context_snapshot
        except BaseException as error:
            self._raise_terminal(error)
        self._lifecycle = _Lifecycle.ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._lifecycle is _Lifecycle.CLOSED:
            return
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ACTIVE:
            await self.rollback()
        self._lifecycle = _Lifecycle.CLOSED
        self._clear_transaction()

    async def commit(self) -> None:
        self._guard_active()
        try:
            async with self._store.lock:
                self._validate_commit()
                prepared_workspaces = {
                    key: _workspace_copy(value)
                    for key, value in self._store.workspaces.items()
                }
                prepared_workspaces.update(
                    {
                        key: _workspace_copy(value)
                        for key, value in self._pending_workspaces.items()
                    }
                )
                prepared_environments = {
                    key: _environment_copy(value)
                    for key, value in self._store.environments.items()
                }
                prepared_environments.update(
                    {
                        key: _environment_copy(value)
                        for key, value in self._pending_environments.items()
                    }
                )
                prepared_context_profiles = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.context_profiles.items()
                }
                prepared_context_profiles.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_context_profiles.items()
                    }
                )
                prepared_context_bundles = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.context_bundles.items()
                }
                prepared_context_bundles.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_context_bundles.items()
                    }
                )
                prepared_receipts = {
                    key: _receipt_copy(value)
                    for key, value in self._store.idempotency_receipts.items()
                }
                prepared_receipts.update(
                    {
                        key: _receipt_copy(value)
                        for key, value in self._pending_receipts.items()
                    }
                )
                prepared_outbox_intents = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.outbox_intents.items()
                }
                prepared_outbox_intents.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_outbox_intents.items()
                    }
                )
                prepared_source_objects = {
                    key: value.model_copy(deep=True) for key, value in self._store.source_objects.items()
                }
                prepared_source_objects.update(
                    {key: value.model_copy(deep=True) for key, value in self._pending_source_objects.items()}
                )
                prepared_source_revisions = {
                    key: value.model_copy(deep=True) for key, value in self._store.source_revisions.items()
                }
                prepared_source_revisions.update(
                    {key: value.model_copy(deep=True) for key, value in self._pending_source_revisions.items()}
                )
                prepared_source_provenance = {
                    key: value.model_copy(deep=True) for key, value in self._store.source_provenance.items()
                }
                prepared_source_provenance.update(
                    {key: value.model_copy(deep=True) for key, value in self._pending_source_provenance.items()}
                )
                prepared_source_receipt_associations = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.source_receipt_associations.items()
                }
                prepared_source_receipt_associations.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_source_receipt_associations.items()
                    }
                )
                prepared_admission_decisions = {
                    key: value.model_copy(deep=True) for key, value in self._store.admission_decisions.items()
                }
                prepared_admission_decisions.update(
                    {key: value.model_copy(deep=True) for key, value in self._pending_admission_decisions.items()}
                )
                prepared_audit_events = {
                    key: _audit_event_copy(value) for key, value in self._store.audit_events.items()
                }
                prepared_audit_events.update(
                    {key: _audit_event_copy(value) for key, value in self._pending_audit_events.items()}
                )
                prepared_ingestion_runs = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_runs.items()}
                prepared_ingestion_runs.update({key: value.model_copy(deep=True) for key, value in self._pending_ingestion_runs.items()})
                prepared_ingestion_items = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_items.items()}
                prepared_ingestion_items.update({key: value.model_copy(deep=True) for key, value in self._pending_ingestion_items.items()})
                prepared_ingestion_bindings = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_bindings.items()}
                prepared_ingestion_bindings.update({key: value.model_copy(deep=True) for key, value in self._pending_ingestion_bindings.items()})
                prepared_ingestion_transitions = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_transitions.items()}
                prepared_ingestion_transitions.update({key: value.model_copy(deep=True) for key, value in self._pending_ingestion_transitions.items()})
                prepared_ingestion_warnings = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_warnings.items()}
                prepared_ingestion_warnings.update({key: value.model_copy(deep=True) for key, value in self._pending_ingestion_warnings.items()})
                prepared_ingestion_failures = {key: value.model_copy(deep=True) for key, value in self._store.ingestion_failures.items()}
                prepared_ingestion_failures.update({key: value.model_copy(deep=True) for key, value in self._pending_ingestion_failures.items()})
                prepared_processing_receipts = {key: value.model_copy(deep=True) for key, value in self._store.processing_receipts.items()}
                prepared_processing_receipts.update({key: value.model_copy(deep=True) for key, value in self._pending_processing_receipts.items()})
                prepared_canonical_acceptances = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.canonical_acceptances.items()
                }
                prepared_canonical_acceptances.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_canonical_acceptances.items()
                    }
                )
                prepared_current_source_heads = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.current_source_heads.items()
                }
                prepared_current_source_heads.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_current_source_heads.items()
                    }
                )
                prepared_canonical_generations = dict(self._store.canonical_generations)
                prepared_canonical_generations.update(self._pending_canonical_generations)
                prepared_canonical_boundaries = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.canonical_boundaries.items()
                }
                prepared_canonical_boundaries.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_canonical_boundaries.items()
                    }
                )
                prepared_canonical_boundary_members = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.canonical_boundary_members.items()
                }
                prepared_canonical_boundary_members.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_canonical_boundary_members.items()
                    }
                )
                prepared_ontology_versions = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.ontology_versions.items()
                }
                prepared_ontology_versions.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_ontology_versions.items()
                    }
                )
                prepared_projection_configs = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_configs.items()
                }
                prepared_projection_configs.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_configs.items()
                    }
                )
                prepared_projection_runs = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_runs.items()
                }
                prepared_projection_runs.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_runs.items()
                    }
                )
                prepared_projection_transitions = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_transitions.items()
                }
                prepared_projection_transitions.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_transitions.items()
                    }
                )
                prepared_projection_partitions = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_partitions.items()
                }
                prepared_projection_partitions.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_partitions.items()
                    }
                )
                prepared_projection_receipts = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_receipts.items()
                }
                prepared_projection_receipts.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_receipts.items()
                    }
                )
                prepared_projection_references = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_references.items()
                }
                prepared_projection_references.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_references.items()
                    }
                )
                prepared_projection_validations = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_validations.items()
                }
                prepared_projection_validations.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_validations.items()
                    }
                )
                prepared_projection_snapshots = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_snapshots.items()
                }
                prepared_projection_snapshots.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_snapshots.items()
                    }
                )
                prepared_projection_snapshot_members = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_snapshot_members.items()
                }
                prepared_projection_snapshot_members.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_snapshot_members.items()
                    }
                )
                prepared_projection_reconciliation_results = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_reconciliation_results.items()
                }
                prepared_projection_reconciliation_results.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_reconciliation_results.items()
                    }
                )
                prepared_projection_activations = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_activations.items()
                }
                prepared_projection_activations.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_activations.items()
                    }
                )
                prepared_active_projections = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.active_projections.items()
                }
                prepared_active_projections.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_active_projections.items()
                    }
                )
                prepared_projection_drifts = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.projection_drifts.items()
                }
                prepared_projection_drifts.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_projection_drifts.items()
                    }
                )
                self._store.state = _StoreState(
                    workspaces=prepared_workspaces,
                    environments=prepared_environments,
                    context_profiles=prepared_context_profiles,
                    context_bundles=prepared_context_bundles,
                    idempotency_receipts=prepared_receipts,
                    outbox_intents=prepared_outbox_intents,
                    source_objects=prepared_source_objects,
                    source_revisions=prepared_source_revisions,
                    source_provenance=prepared_source_provenance,
                    source_receipt_associations=prepared_source_receipt_associations,
                    admission_decisions=prepared_admission_decisions,
                    audit_events=prepared_audit_events,
                    ingestion_runs=prepared_ingestion_runs,
                    ingestion_items=prepared_ingestion_items,
                    ingestion_bindings=prepared_ingestion_bindings,
                    ingestion_transitions=prepared_ingestion_transitions,
                    ingestion_warnings=prepared_ingestion_warnings,
                    ingestion_failures=prepared_ingestion_failures,
                    processing_receipts=prepared_processing_receipts,
                    canonical_acceptances=prepared_canonical_acceptances,
                    current_source_heads=prepared_current_source_heads,
                    canonical_generations=prepared_canonical_generations,
                    canonical_boundaries=prepared_canonical_boundaries,
                    canonical_boundary_members=prepared_canonical_boundary_members,
                    ontology_versions=prepared_ontology_versions,
                    projection_configs=prepared_projection_configs,
                    projection_runs=prepared_projection_runs,
                    projection_transitions=prepared_projection_transitions,
                    projection_partitions=prepared_projection_partitions,
                    projection_receipts=prepared_projection_receipts,
                    projection_references=prepared_projection_references,
                    projection_validations=prepared_projection_validations,
                    projection_snapshots=prepared_projection_snapshots,
                    projection_snapshot_members=prepared_projection_snapshot_members,
                    projection_reconciliation_results=prepared_projection_reconciliation_results,
                    projection_activations=prepared_projection_activations,
                    active_projections=prepared_active_projections,
                    projection_drifts=prepared_projection_drifts,
                )
        except BaseException as error:
            self._raise_terminal(error)
        self._lifecycle = _Lifecycle.COMMITTED
        self._release_source_observation_lock()
        self._release_canonical_lock()
        self._clear_transaction()

    async def rollback(self) -> None:
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ROLLED_BACK:
            return
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        self._pending_workspaces.clear()
        self._pending_environments.clear()
        self._pending_context_profiles.clear()
        self._pending_context_bundles.clear()
        self._pending_receipts.clear()
        self._pending_outbox_intents.clear()
        self._pending_source_objects.clear()
        self._pending_source_revisions.clear()
        self._pending_source_provenance.clear()
        self._pending_source_receipt_associations.clear()
        self._pending_admission_decisions.clear()
        self._pending_audit_events.clear()
        self._pending_ingestion_runs.clear()
        self._pending_ingestion_items.clear()
        self._pending_ingestion_bindings.clear()
        self._pending_ingestion_transitions.clear()
        self._pending_ingestion_warnings.clear()
        self._pending_ingestion_failures.clear()
        self._pending_processing_receipts.clear()
        self._pending_canonical_acceptances.clear()
        self._pending_current_source_heads.clear()
        self._pending_canonical_generations.clear()
        self._pending_canonical_boundaries.clear()
        self._pending_canonical_boundary_members.clear()
        self._pending_ontology_versions.clear()
        self._pending_projection_configs.clear()
        self._pending_projection_runs.clear()
        self._pending_projection_transitions.clear()
        self._pending_projection_partitions.clear()
        self._pending_projection_receipts.clear()
        self._pending_projection_references.clear()
        self._pending_projection_validations.clear()
        self._pending_projection_snapshots.clear()
        self._pending_projection_snapshot_members.clear()
        self._pending_projection_reconciliation_results.clear()
        self._pending_projection_activations.clear()
        self._pending_active_projections.clear()
        self._pending_projection_drifts.clear()
        self._pending_projection_run_claims.clear()
        self._lifecycle = _Lifecycle.ROLLED_BACK
        self._release_source_observation_lock()
        self._release_canonical_lock()

    def _guard_owner(self) -> None:
        if self._owner is None or asyncio.current_task() is not self._owner:
            raise UnitOfWorkLifecycleError("Unit of Work may only be used by its owning task.")

    def _guard_active(self) -> None:
        self._guard_owner()
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")

    def _repository_call(self, operation: Callable[[], _ResultT]) -> _ResultT:
        try:
            return operation()
        except BaseException as error:
            self._raise_terminal(error)

    def _raise_terminal(self, error: BaseException) -> NoReturn:
        self._release_source_observation_lock()
        self._release_canonical_lock()
        self._lifecycle = _Lifecycle.CLOSED
        self._clear_transaction()
        if isinstance(error, asyncio.CancelledError):
            raise error
        if isinstance(error, PersistenceError):
            raise error
        if isinstance(error, Exception):
            raise UnexpectedPersistenceError(_UNEXPECTED_FAILURE_MESSAGE) from None
        raise error

    def _validate_context(self, context_snapshot: TrustedPersistenceContext) -> None:
        scope = context_snapshot.scope
        if isinstance(scope, WorkspaceScope):
            self._scope = WorkspaceScope(workspace_id=scope.workspace_id)
            self._operation = PersistenceOperation(context_snapshot.operation.value)
            self._trace_id = context_snapshot.trace_id
            return
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        environment = self._base_environments.get(scope.environment_id)
        if environment is None or environment.workspace_id != scope.workspace_id:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        self._scope = EnvironmentScope(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        self._operation = PersistenceOperation(context_snapshot.operation.value)
        self._trace_id = context_snapshot.trace_id

    def _validate_commit(self) -> None:
        if self._pending_workspaces and not self._store.initialized:
            raise InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
        if self._pending_workspaces.keys() & self._store.workspaces.keys():
            raise ConstraintConflictError("Workspace identity already exists.")
        if self._pending_environments.keys() & self._store.environments.keys():
            raise ConstraintConflictError("Environment identity already exists.")
        if self._pending_context_profiles.keys() & self._store.context_profiles.keys():
            raise ConstraintConflictError("Context profile immutable version already exists.")
        if self._pending_context_bundles.keys() & self._store.context_bundles.keys():
            raise ContextBundleConflictError("Context bundle immutable identity already exists.")
        committed_attempts = {
            (
                bundle.workspace_id,
                bundle.environment_id,
                bundle.run_id,
                bundle.attempt_id,
                bundle.idempotency_key,
            )
            for bundle in self._store.context_bundles.values()
        }
        pending_attempts: set[tuple[UUID, UUID, UUID, UUID, str]] = set()
        for bundle in self._pending_context_bundles.values():
            attempt = (
                bundle.workspace_id,
                bundle.environment_id,
                bundle.run_id,
                bundle.attempt_id,
                bundle.idempotency_key,
            )
            if attempt in committed_attempts or attempt in pending_attempts:
                raise ContextBundleConflictError(
                    "Context bundle attempt identity conflicts with history."
                )
            pending_attempts.add(attempt)
        if self._pending_receipts.keys() & self._store.idempotency_receipts.keys():
            raise IdempotencyConflictError("Idempotency key conflicts with existing command.")
        if self._pending_outbox_intents.keys() & self._store.outbox_intents.keys():
            raise OutboxConflictError("Outbox event identity already exists.")
        if self._pending_admission_decisions.keys() & self._store.admission_decisions.keys():
            raise ConstraintConflictError("Admission decision identity already exists.")
        if self._pending_canonical_boundaries.keys() & self._store.canonical_boundaries.keys():
            raise ConstraintConflictError("Canonical boundary identity already exists.")
        if self._pending_canonical_boundary_members.keys() & self._store.canonical_boundary_members.keys():
            raise ConstraintConflictError("Canonical boundary membership identity already exists.")
        committed_decisions = tuple(self._store.admission_decisions.values())
        pending_decisions = tuple(self._pending_admission_decisions.values())
        for decision in pending_decisions:
            same_revision = tuple(
                item for item in (*committed_decisions, *pending_decisions)
                if item.workspace_id == decision.workspace_id
                and item.environment_id == decision.environment_id
                and item.revision_id == decision.revision_id
            )
            if any(item.decision_id != decision.decision_id and item.decision_version == decision.decision_version for item in same_revision):
                raise ConstraintConflictError("Admission decision version already exists.")
            if decision.predecessor_decision_id is None and any(
                item.decision_id != decision.decision_id and item.predecessor_decision_id is None
                for item in same_revision
            ):
                raise ConstraintConflictError("Admission decision root already exists.")
            if decision.predecessor_decision_id is not None and any(
                item.decision_id != decision.decision_id and item.predecessor_decision_id == decision.predecessor_decision_id
                for item in same_revision
            ):
                raise ConstraintConflictError("Admission decision successor already exists.")
        if self._pending_audit_events.keys() & self._store.audit_events.keys():
            raise AuditConflictError("Audit Event identity already exists.")
        self._validate_ingestion_commit()
        self._validate_projection_commit()
        for intent in self._pending_outbox_intents.values():
            if self._store_has_producer_identity(intent):
                raise OutboxConflictError("Outbox producer identity already exists.")
        for event in self._pending_audit_events.values():
            duplicate = self._store_audit_producer_identity(event)
            if duplicate is not None:
                raise AuditConflictError("Audit producer identity already exists.")
        incomplete = [
            receipt for receipt in self._pending_receipts.values() if receipt.result is None
        ]
        if incomplete:
            raise UnitOfWorkLifecycleError("Owned idempotency claims must be completed before commit.")
        if any(
            receipt.key.operation_name != receipt.digest.operation.value
            or receipt.key.operation_schema_version != receipt.digest.operation_schema_version
            for receipt in self._pending_receipts.values()
        ):
            raise IdempotencyConflictError(
                "Idempotency digest does not match the trusted operation."
            )
        available_workspaces = self._store.workspaces.keys() | self._pending_workspaces.keys()
        if any(
            intent.workspace_id not in available_workspaces
            for intent in self._pending_outbox_intents.values()
        ):
            raise ConstraintConflictError("Owning Workspace does not exist.")
        if any(
            environment.workspace_id not in available_workspaces
            for environment in self._pending_environments.values()
        ):
            raise ConstraintConflictError("Owning Workspace does not exist.")

    def _validate_projection_commit(self) -> None:
        maps = (
            (self._pending_ontology_versions, self._store.ontology_versions),
            (self._pending_projection_configs, self._store.projection_configs),
            (self._pending_projection_transitions, self._store.projection_transitions),
            (self._pending_projection_partitions, self._store.projection_partitions),
            (self._pending_projection_receipts, self._store.projection_receipts),
            (self._pending_projection_references, self._store.projection_references),
            (self._pending_projection_validations, self._store.projection_validations),
            (self._pending_projection_snapshots, self._store.projection_snapshots),
            (self._pending_projection_snapshot_members, self._store.projection_snapshot_members),
            (
                self._pending_projection_reconciliation_results,
                self._store.projection_reconciliation_results,
            ),
            (self._pending_projection_activations, self._store.projection_activations),
            (self._pending_projection_drifts, self._store.projection_drifts),
        )
        if any(pending.keys() & stored.keys() for pending, stored in maps):
            raise ConstraintConflictError("Projection ledger identity already exists.")
        if self._pending_active_projections:
            for key, pointer in self._pending_active_projections.items():
                existing = self._store.active_projections.get(key)
                if existing is not None and pointer.cas_version <= existing.cas_version:
                    raise OptimisticConflictError("Active projection CAS version regressed.")
        for run_id, pending_run in self._pending_projection_runs.items():
            stored_run = self._store.projection_runs.get(run_id)
            if stored_run is not None:
                if not (
                    stored_run.candidate_state.value == "open"
                    and pending_run.candidate_state.value == "sealed"
                    and stored_run.projection_candidate_id == pending_run.projection_candidate_id
                ):
                    raise ConstraintConflictError("Projection run identity already exists.")
        stored_runs = tuple(self._store.projection_runs.values())
        for run in self._pending_projection_runs.values():
            if any(
                existing.projection_run_id != run.projection_run_id
                and
                existing.workspace_id == run.workspace_id
                and existing.environment_id == run.environment_id
                and (
                    existing.projection_candidate_id == run.projection_candidate_id
                    or existing.command_identity == run.command_identity
                )
                for existing in stored_runs
            ):
                raise ConstraintConflictError("Projection candidate or command identity already exists.")
        stored_receipts = tuple(self._store.projection_receipts.values())
        for receipt in self._pending_projection_receipts.values():
            if any(
                existing.workspace_id == receipt.workspace_id
                and existing.environment_id == receipt.environment_id
                and existing.command_identity == receipt.command_identity
                and existing.command_digest != receipt.command_digest
                for existing in stored_receipts
            ):
                raise IdempotencyConflictError("Projection command identity conflicts.")
        stored_transitions = tuple(self._store.projection_transitions.values())
        for transition in self._pending_projection_transitions.values():
            if any(
                existing.workspace_id == transition.workspace_id
                and existing.environment_id == transition.environment_id
                and existing.projection_run_id == transition.projection_run_id
                and existing.predecessor_transition_id == transition.predecessor_transition_id
                for existing in stored_transitions
            ):
                raise ConstraintConflictError("Projection transition predecessor already has a successor.")
    def _validate_ingestion_commit(self) -> None:
        """Apply the database uniqueness/append-only checks at commit time.

        Repository methods validate against the transaction snapshot.  A second
        transaction may have committed after that snapshot, so these checks are
        deliberately repeated while holding the store commit lock.
        """
        maps = (
            (self._pending_ingestion_runs, self._store.ingestion_runs),
            (self._pending_ingestion_items, self._store.ingestion_items),
            (self._pending_ingestion_bindings, self._store.ingestion_bindings),
            (self._pending_ingestion_transitions, self._store.ingestion_transitions),
            (self._pending_ingestion_warnings, self._store.ingestion_warnings),
            (self._pending_ingestion_failures, self._store.ingestion_failures),
            (self._pending_processing_receipts, self._store.processing_receipts),
        )
        if any(pending.keys() & stored.keys() for pending, stored in maps):
            raise ConstraintConflictError("Ingestion ledger identity already exists.")

        stored_items = tuple(self._store.ingestion_items.values())
        for item in self._pending_ingestion_items.values():
            if any(
                existing.ingestion_run_id == item.ingestion_run_id
                and existing.input_ordinal == item.input_ordinal
                for existing in stored_items
            ) or any(
                existing.ingestion_run_id == item.ingestion_run_id
                and existing.input_identity == item.input_identity
                for existing in stored_items
            ):
                raise ConstraintConflictError("Ingestion item uniqueness conflicts with a concurrent commit.")
            if any(
                existing.workspace_id == item.workspace_id
                and existing.environment_id == item.environment_id
                and existing.command_identity == item.command_identity
                for existing in stored_items
            ):
                raise ConstraintConflictError("Ingestion item command identity conflicts with a concurrent commit.")

        stored_bindings = tuple(self._store.ingestion_bindings.values())
        for binding in self._pending_ingestion_bindings.values():
            if any(existing.ingestion_run_source_id == binding.ingestion_run_source_id for existing in stored_bindings):
                raise ConstraintConflictError("Ingestion item is already bound by a concurrent commit.")
            if any(
                existing.workspace_id == binding.workspace_id
                and existing.environment_id == binding.environment_id
                and existing.binding_command_identity == binding.binding_command_identity
                for existing in stored_bindings
            ):
                raise ConstraintConflictError("Ingestion binding command identity conflicts with a concurrent commit.")

        for pending, stored, label in (
            (self._pending_ingestion_runs, self._store.ingestion_runs, "run"),
            (self._pending_processing_receipts, self._store.processing_receipts, "processing receipt"),
        ):
            for value in pending.values():
                if any(
                    existing.workspace_id == value.workspace_id
                    and existing.environment_id == value.environment_id
                    and existing.command_identity == value.command_identity
                    for existing in stored.values()
                ):
                    raise ConstraintConflictError(f"Ingestion {label} command identity conflicts with a concurrent commit.")

        stored_transitions = tuple(self._store.ingestion_transitions.values())
        for transition in self._pending_ingestion_transitions.values():
            if transition.predecessor_transition_id is None and any(
                existing.workspace_id == transition.workspace_id
                and existing.environment_id == transition.environment_id
                and existing.target_kind is transition.target_kind
                and existing.ingestion_run_id == transition.ingestion_run_id
                and existing.ingestion_run_source_id == transition.ingestion_run_source_id
                and existing.predecessor_transition_id is None
                for existing in stored_transitions
            ):
                raise ConstraintConflictError("Ingestion transition root already exists.")
            if transition.predecessor_transition_id is not None and any(
                existing.workspace_id == transition.workspace_id
                and existing.environment_id == transition.environment_id
                and existing.predecessor_transition_id == transition.predecessor_transition_id
                for existing in stored_transitions
            ):
                raise ConstraintConflictError("Ingestion transition predecessor already has a successor.")

    def _resolve_workspace(self, workspace_id: UUID) -> Workspace | None:
        return self._pending_workspaces.get(workspace_id) or self._base_workspaces.get(workspace_id)

    def _resolve_environment(self, environment_id: UUID) -> Environment | None:
        return self._pending_environments.get(environment_id) or self._base_environments.get(
            environment_id
        )

    def _resolve_context_profile(
        self, key: tuple[UUID, UUID, UUID, int]
    ) -> ContextProfileVersion | None:
        return self._pending_context_profiles.get(key) or self._base_context_profiles.get(key)

    def _resolve_context_bundle(
        self, key: tuple[UUID, UUID, UUID, int]
    ) -> ContextBundle | None:
        return self._pending_context_bundles.get(key) or self._base_context_bundles.get(key)

    def _resolve_receipt(self, key: _ReceiptKey) -> _IdempotencyReceipt | None:
        return self._pending_receipts.get(key) or self._base_receipts.get(key)

    def _resolve_outbox_intent(self, event_id: UUID) -> OutboxIntent | None:
        return self._pending_outbox_intents.get(event_id) or self._base_outbox_intents.get(
            event_id
        )

    def _resolve_audit_event(self, audit_event_id: UUID) -> AuditEvent | None:
        return self._pending_audit_events.get(audit_event_id) or self._base_audit_events.get(audit_event_id)

    @staticmethod
    def _same_audit_producer_identity(left: AuditEvent, right: AuditEvent) -> bool:
        return (
            left.producer_deduplication_id is not None
            and left.workspace_id == right.workspace_id
            and left.environment_id == right.environment_id
            and left.event_type == right.event_type
            and left.producer_deduplication_id == right.producer_deduplication_id
        )

    def _resolve_audit_producer_identity(self, event: AuditEvent) -> AuditEvent | None:
        if event.producer_deduplication_id is None:
            return None
        return next(
            (existing for existing in (*self._base_audit_events.values(), *self._pending_audit_events.values())
             if self._same_audit_producer_identity(existing, event)),
            None,
        )

    def _store_audit_producer_identity(self, event: AuditEvent) -> AuditEvent | None:
        if event.producer_deduplication_id is None:
            return None
        return next(
            (existing for existing in self._store.audit_events.values()
             if self._same_audit_producer_identity(existing, event)),
            None,
        )

    @staticmethod
    def _same_producer_identity(left: OutboxIntent, right: OutboxIntent) -> bool:
        return (
            left.producer_deduplication_id is not None
            and left.workspace_id == right.workspace_id
            and left.environment_id == right.environment_id
            and left.event_type == right.event_type
            and left.producer_deduplication_id == right.producer_deduplication_id
        )

    def _has_producer_identity(self, intent: OutboxIntent) -> bool:
        return any(
            self._same_producer_identity(existing, intent)
            for existing in (
                *self._base_outbox_intents.values(),
                *self._pending_outbox_intents.values(),
            )
        )

    def _store_has_producer_identity(self, intent: OutboxIntent) -> bool:
        return any(
            self._same_producer_identity(existing, intent)
            for existing in self._store.outbox_intents.values()
        )

    def _receipt_key(
        self,
        *,
        operation: PersistenceOperation,
        operation_schema_version: int,
        key: IdempotencyKey,
    ) -> _ReceiptKey:
        scope = self.scope
        environment_id = scope.environment_id if isinstance(scope, EnvironmentScope) else None
        return _ReceiptKey(
            workspace_id=scope.workspace_id,
            environment_id=environment_id,
            operation_name=operation.value,
            operation_schema_version=operation_schema_version,
            idempotency_key=key.value,
        )

    def _clear_transaction(self) -> None:
        self._base_workspaces.clear()
        self._base_environments.clear()
        self._base_context_profiles.clear()
        self._base_context_bundles.clear()
        self._base_receipts.clear()
        self._base_outbox_intents.clear()
        self._base_source_objects.clear()
        self._base_source_revisions.clear()
        self._base_source_provenance.clear()
        self._base_source_receipt_associations.clear()
        self._base_admission_decisions.clear()
        self._base_audit_events.clear()
        self._base_ingestion_runs.clear()
        self._base_ingestion_items.clear()
        self._base_ingestion_bindings.clear()
        self._base_ingestion_transitions.clear()
        self._base_ingestion_warnings.clear()
        self._base_ingestion_failures.clear()
        self._base_processing_receipts.clear()
        self._base_canonical_acceptances.clear()
        self._base_current_source_heads.clear()
        self._base_canonical_generations.clear()
        self._base_canonical_boundaries.clear()
        self._base_canonical_boundary_members.clear()
        self._base_ontology_versions.clear()
        self._base_projection_configs.clear()
        self._base_projection_runs.clear()
        self._base_projection_transitions.clear()
        self._base_projection_partitions.clear()
        self._base_projection_receipts.clear()
        self._base_projection_references.clear()
        self._base_projection_validations.clear()
        self._base_projection_snapshots.clear()
        self._base_projection_snapshot_members.clear()
        self._base_projection_reconciliation_results.clear()
        self._base_projection_activations.clear()
        self._base_active_projections.clear()
        self._base_projection_drifts.clear()
        self._pending_workspaces.clear()
        self._pending_environments.clear()
        self._pending_context_profiles.clear()
        self._pending_context_bundles.clear()
        self._pending_receipts.clear()
        self._pending_outbox_intents.clear()
        self._pending_source_objects.clear()
        self._pending_source_revisions.clear()
        self._pending_source_provenance.clear()
        self._pending_source_receipt_associations.clear()
        self._pending_admission_decisions.clear()
        self._pending_audit_events.clear()
        self._pending_ingestion_runs.clear()
        self._pending_ingestion_items.clear()
        self._pending_ingestion_bindings.clear()
        self._pending_ingestion_transitions.clear()
        self._pending_ingestion_warnings.clear()
        self._pending_ingestion_failures.clear()
        self._pending_processing_receipts.clear()
        self._pending_canonical_acceptances.clear()
        self._pending_current_source_heads.clear()
        self._pending_canonical_generations.clear()
        self._pending_canonical_boundaries.clear()
        self._pending_canonical_boundary_members.clear()
        self._pending_ontology_versions.clear()
        self._pending_projection_configs.clear()
        self._pending_projection_runs.clear()
        self._pending_projection_transitions.clear()
        self._pending_projection_partitions.clear()
        self._pending_projection_receipts.clear()
        self._pending_projection_references.clear()
        self._pending_projection_validations.clear()
        self._pending_projection_snapshots.clear()
        self._pending_projection_snapshot_members.clear()
        self._pending_projection_reconciliation_results.clear()
        self._pending_projection_activations.clear()
        self._pending_active_projections.clear()
        self._pending_projection_drifts.clear()
        self._pending_projection_run_claims.clear()
        self._scope = None
        self._operation = None
        self._trace_id = None
        self._active_context = None

    def _fail(self, error: Exception) -> NoReturn:
        self._raise_terminal(error)

    def _release_source_observation_lock(self) -> None:
        if self._source_observation_lock_held:
            self._source_observation_lock_held = False
            self._store.observation_lock.release()

    def _release_canonical_lock(self) -> None:
        if self._canonical_lock_held:
            self._canonical_lock_held = False
            self._store.canonical_lock.release()


def _receipt_id(key: _ReceiptKey) -> UUID:
    environment_id = str(key.environment_id) if key.environment_id is not None else "-"
    return uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "spine-idempotency-receipt-v1",
                str(key.workspace_id),
                environment_id,
                key.operation_name,
                str(key.operation_schema_version),
                key.idempotency_key,
            )
        ),
    )


def _receipt_copy(value: _IdempotencyReceipt) -> _IdempotencyReceipt:
    if type(value) is not _IdempotencyReceipt:
        raise TypeError("Unsupported idempotency receipt implementation.")
    return _IdempotencyReceipt(
        receipt_id=value.receipt_id,
        key=value.key,
        digest=value.digest,
        result=value.result,
    )


class _InMemoryUnitOfWorkFactory:
    def __init__(
        self,
        store: _Store,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        event_id_factory: Callable[[], UUID],
        audit_events: AuditEventRegistry,
        audit_clock: Callable[[], datetime],
        audit_event_id_factory: Callable[[], UUID],
    ) -> None:
        self._store = store
        self._context_verifier = context_verifier
        self._outbox_events = outbox_events
        self._event_id_factory = event_id_factory
        self._audit_events = audit_events
        self._audit_clock = audit_clock
        self._audit_event_id_factory = audit_event_id_factory

    def __call__(self, context: TrustedPersistenceContext) -> InMemoryUnitOfWork:
        if not isinstance(context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        context_snapshot = self._context_verifier.verify(context)
        return InMemoryUnitOfWork(
            self._store,
            context,
            context_snapshot,
            self._context_verifier,
            self._outbox_events,
            self._event_id_factory,
            self._audit_events,
            self._audit_clock,
            self._audit_event_id_factory,
        )


class _InMemoryAuditReader:
    def __init__(self, store: _Store, context_verifier: TrustedContextVerifier) -> None:
        self._store = store
        self._context_verifier = context_verifier

    async def resolve(
        self, context: TrustedPersistenceContext, audit_event_id: UUID
    ) -> AuditEvent | None:
        snapshot = self._context_verifier.verify(context)
        snapshot.validate_shape()
        async with self._store.lock:
            event = self._store.audit_events.get(audit_event_id)
            if event is None:
                return None
            scope = snapshot.scope
            if isinstance(scope, WorkspaceScope):
                allowed = event.workspace_id == scope.workspace_id and event.environment_id is None
            elif isinstance(scope, EnvironmentScope):
                allowed = event.workspace_id == scope.workspace_id and event.environment_id == scope.environment_id
            else:
                allowed = False
            return _audit_event_copy(event) if allowed else None


class _InMemoryInitialWorkspaceBootstrap:
    def __init__(
        self,
        store: _Store,
        expected_authority: InitialWorkspaceBootstrapAuthority | None,
    ) -> None:
        self._store = store
        self._expected_authority = expected_authority

    async def create_initial_workspace(
        self,
        authority: InitialWorkspaceBootstrapAuthority,
        workspace: Workspace,
    ) -> None:
        if self._expected_authority is None or authority is not self._expected_authority:
            raise InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
        async with self._store.lock:
            if self._store.bootstrap_sealed or self._store.workspaces:
                raise ConstraintConflictError("Initial Workspace bootstrap is sealed.")
            try:
                copied_workspace = _workspace_copy(workspace)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise UnexpectedPersistenceError(_UNEXPECTED_FAILURE_MESSAGE) from None
            self._store.workspaces[workspace.id] = copied_workspace
            self._store.initialized = True
            self._store.bootstrap_sealed = True


class _InMemoryOutboxDeliveryRepository:
    def __init__(
        self,
        *,
        store: _Store,
        routes: OutboxDeliveryRouteRegistry,
        lease_token_factory: Callable[[], UUID],
        audit_events: AuditEventRegistry,
        audit_clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._routes = routes
        self._lease_token_factory = lease_token_factory
        self._audit_events = audit_events
        self._audit_clock = audit_clock

    async def materialize(
        self,
        *,
        limit: int,
        materialized_at: datetime,
    ) -> tuple[OutboxDelivery, ...]:
        if isinstance(limit, bool) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        materialized: list[OutboxDelivery] = []
        async with self._store.lock:
            for event_id, intent in sorted(
                self._store.outbox_intents.items(), key=lambda item: item[0].int
            ):
                if len(materialized) == limit:
                    break
                if event_id in self._store.outbox_deliveries:
                    continue
                route = self._routes.resolve_optional(
                    intent.event_type,
                    intent.schema_version,
                )
                delivery = OutboxDelivery(
                    event_id=event_id,
                    workspace_id=intent.workspace_id,
                    environment_id=intent.environment_id,
                    consumer_name=(
                        route.consumer_name if route is not None else None
                    ),
                    status=OutboxDeliveryStatus.PENDING,
                    attempt_count=0,
                    next_attempt_at=materialized_at,
                    version=0,
                )
                self._store.outbox_deliveries[event_id] = delivery
                materialized.append(delivery.model_copy(deep=True))
        return tuple(materialized)

    async def claim_due(
        self,
        *,
        limit: int,
        worker_identity: str,
        claimed_at: datetime,
        lease_duration: timedelta,
    ) -> tuple[ClaimedOutboxDelivery, ...]:
        if isinstance(limit, bool) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        lease_expires_at = claimed_at + lease_duration
        claims: list[ClaimedOutboxDelivery] = []
        async with self._store.lock:
            for event_id, intent in sorted(
                self._store.outbox_intents.items(), key=lambda item: item[0].int
            ):
                if len(claims) == limit:
                    break
                existing = self._store.outbox_deliveries.get(event_id)
                if existing is None:
                    attempt_number = 1
                    version = 1
                elif (
                    existing.status
                    in (
                        OutboxDeliveryStatus.PENDING,
                        OutboxDeliveryStatus.RETRY_SCHEDULED,
                    )
                    and existing.next_attempt_at is not None
                    and existing.next_attempt_at <= claimed_at
                ) or (
                    existing.status is OutboxDeliveryStatus.IN_FLIGHT
                    and existing.lease_expires_at is not None
                    and existing.lease_expires_at <= claimed_at
                ):
                    attempt_number = existing.attempt_count + 1
                    version = existing.version + 1
                else:
                    continue
                route = self._routes.resolve_optional(
                    intent.event_type,
                    intent.schema_version,
                )
                consumer_name = route.consumer_name if route is not None else None
                lease_token = self._lease_token_factory()
                delivery = OutboxDelivery(
                    event_id=event_id,
                    workspace_id=intent.workspace_id,
                    environment_id=intent.environment_id,
                    consumer_name=consumer_name,
                    status=OutboxDeliveryStatus.IN_FLIGHT,
                    attempt_count=attempt_number,
                    lease_owner=worker_identity,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    version=version,
                )
                claim = ClaimedOutboxDelivery(
                    intent=intent.model_copy(deep=True),
                    consumer_name=consumer_name,
                    worker_identity=worker_identity,
                    lease_token=lease_token,
                    claimed_at=claimed_at,
                    lease_expires_at=lease_expires_at,
                    attempt_number=attempt_number,
                    delivery_version=version,
                )
                self._store.outbox_deliveries[event_id] = delivery
                claims.append(claim)
        return tuple(claims)

    async def resolve(self, event_id: UUID) -> OutboxDelivery | None:
        if not isinstance(event_id, UUID) or event_id.int == 0:
            raise ValueError("event_id must be a non-zero UUID.")
        async with self._store.lock:
            delivery = self._store.outbox_deliveries.get(event_id)
            return delivery.model_copy(deep=True) if delivery is not None else None

    def _active_delivery(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        transition_at: datetime,
    ) -> OutboxDelivery:
        event_id = claim.intent.event_id
        delivery = (
            self._store.outbox_deliveries.get(event_id)
            if event_id is not None
            else None
        )
        if (
            delivery is None
            or delivery.status is not OutboxDeliveryStatus.IN_FLIGHT
            or delivery.lease_owner != claim.worker_identity
            or delivery.lease_token != claim.lease_token
            or delivery.version != claim.delivery_version
            or delivery.attempt_count != claim.attempt_number
            or delivery.lease_expires_at is None
            or transition_at >= delivery.lease_expires_at
        ):
            raise StaleDeliveryLeaseError("Outbox delivery lease is not active.")
        return delivery

    async def acknowledge(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        completed_at: datetime,
    ) -> OutboxDelivery:
        async with self._store.lock:
            delivery = self._active_delivery(claim, transition_at=completed_at)
            delivered = OutboxDelivery(
                event_id=delivery.event_id,
                workspace_id=delivery.workspace_id,
                environment_id=delivery.environment_id,
                consumer_name=delivery.consumer_name,
                status=OutboxDeliveryStatus.DELIVERED,
                attempt_count=delivery.attempt_count,
                delivered_at=completed_at,
                version=delivery.version + 1,
            )
            self._store.outbox_deliveries[delivery.event_id] = delivered
            return delivered.model_copy(deep=True)

    async def reschedule(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        failed_at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> OutboxDelivery:
        if next_attempt_at <= failed_at:
            raise ValueError("next_attempt_at must follow failed_at.")
        async with self._store.lock:
            delivery = self._active_delivery(claim, transition_at=failed_at)
            retry = OutboxDelivery(
                event_id=delivery.event_id,
                workspace_id=delivery.workspace_id,
                environment_id=delivery.environment_id,
                consumer_name=delivery.consumer_name,
                status=OutboxDeliveryStatus.RETRY_SCHEDULED,
                attempt_count=delivery.attempt_count,
                next_attempt_at=next_attempt_at,
                last_error_code=error_code,
                version=delivery.version + 1,
            )
            self._store.outbox_deliveries[delivery.event_id] = retry
            return retry.model_copy(deep=True)

    async def quarantine(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        quarantined_at: datetime,
        error_code: str,
    ) -> OutboxDelivery:
        async with self._store.lock:
            delivery = self._active_delivery(
                claim,
                transition_at=quarantined_at,
            )
            quarantined = OutboxDelivery(
                event_id=delivery.event_id,
                workspace_id=delivery.workspace_id,
                environment_id=delivery.environment_id,
                consumer_name=delivery.consumer_name,
                status=OutboxDeliveryStatus.QUARANTINED,
                attempt_count=delivery.attempt_count,
                last_error_code=error_code,
                quarantined_at=quarantined_at,
                version=delivery.version + 1,
            )
            self._store.outbox_deliveries[delivery.event_id] = quarantined
            return quarantined.model_copy(deep=True)

    async def renew(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        renewed_at: datetime,
        lease_duration: timedelta,
    ) -> ClaimedOutboxDelivery:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        async with self._store.lock:
            delivery = self._active_delivery(claim, transition_at=renewed_at)
            lease_token = self._lease_token_factory()
            lease_expires_at = renewed_at + lease_duration
            renewed_version = delivery.version + 1
            renewed_delivery = OutboxDelivery(
                event_id=delivery.event_id,
                workspace_id=delivery.workspace_id,
                environment_id=delivery.environment_id,
                consumer_name=delivery.consumer_name,
                status=OutboxDeliveryStatus.IN_FLIGHT,
                attempt_count=delivery.attempt_count,
                lease_owner=claim.worker_identity,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                version=renewed_version,
            )
            renewed_claim = ClaimedOutboxDelivery(
                intent=claim.intent.model_copy(deep=True),
                consumer_name=claim.consumer_name,
                worker_identity=claim.worker_identity,
                lease_token=lease_token,
                claimed_at=renewed_at,
                lease_expires_at=lease_expires_at,
                attempt_number=claim.attempt_number,
                delivery_version=renewed_version,
            )
            self._store.outbox_deliveries[delivery.event_id] = renewed_delivery
            return renewed_claim

    async def finalize(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        finalization: OutboxDeliveryFinalization,
        audit_event: AuditEvent,
    ) -> OutboxDelivery:
        async with self._store.lock:
            delivery = self._active_delivery(
                claim,
                transition_at=finalization.occurred_at,
            )
            canonical_audit = validate_outbox_delivery_audit(
                claim,
                finalization,
                audit_event,
                registry=self._audit_events,
            )
            if self._store.fail_next_audit_append:
                self._store.fail_next_audit_append = False
                raise UnexpectedPersistenceError(_UNEXPECTED_FAILURE_MESSAGE)
            assert canonical_audit.audit_event_id is not None
            if canonical_audit.audit_event_id in self._store.audit_events:
                raise AuditConflictError("Audit Event identity already exists.")
            if finalization.status is OutboxDeliveryStatus.DELIVERED:
                finalized = OutboxDelivery(
                    event_id=delivery.event_id,
                    workspace_id=delivery.workspace_id,
                    environment_id=delivery.environment_id,
                    consumer_name=delivery.consumer_name,
                    status=OutboxDeliveryStatus.DELIVERED,
                    attempt_count=delivery.attempt_count,
                    delivered_at=finalization.occurred_at,
                    version=delivery.version + 1,
                )
            elif finalization.status is OutboxDeliveryStatus.RETRY_SCHEDULED:
                finalized = OutboxDelivery(
                    event_id=delivery.event_id,
                    workspace_id=delivery.workspace_id,
                    environment_id=delivery.environment_id,
                    consumer_name=delivery.consumer_name,
                    status=OutboxDeliveryStatus.RETRY_SCHEDULED,
                    attempt_count=delivery.attempt_count,
                    next_attempt_at=finalization.next_attempt_at,
                    last_error_code=finalization.error_code,
                    version=delivery.version + 1,
                )
            else:
                finalized = OutboxDelivery(
                    event_id=delivery.event_id,
                    workspace_id=delivery.workspace_id,
                    environment_id=delivery.environment_id,
                    consumer_name=delivery.consumer_name,
                    status=OutboxDeliveryStatus.QUARANTINED,
                    attempt_count=delivery.attempt_count,
                    last_error_code=finalization.error_code,
                    quarantined_at=finalization.occurred_at,
                    version=delivery.version + 1,
                )
            stored_audit = canonical_audit.detached_snapshot(
                update={"appended_at": self._audit_clock()}
            )
            self._store.outbox_deliveries[delivery.event_id] = finalized
            self._store.audit_events[canonical_audit.audit_event_id] = stored_audit
            return finalized.model_copy(deep=True)


class InMemoryPersistence:
    """Process-local deterministic adapter and its explicitly scoped entry points."""

    def __init__(
        self,
        *,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        bootstrap_authority: InitialWorkspaceBootstrapAuthority | None = None,
        transaction_lock: asyncio.Lock | None = None,
        event_id_factory: Callable[[], UUID] = uuid4,
        audit_events: AuditEventRegistry | None = None,
        audit_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        audit_event_id_factory: Callable[[], UUID] = uuid4,
        delivery_routes: OutboxDeliveryRouteRegistry | None = None,
        delivery_lease_token_factory: Callable[[], UUID] = uuid4,
        canonical_policy_repository: InMemoryAccessPolicyRepository | None = None,
        canonical_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        boundary_id_factory: Callable[[], UUID] = uuid4,
        boundary_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        selected_audit_events = (
            audit_events or AuditEventRegistry.with_default_families()
        )
        register_projection_outbox_events(outbox_events)
        selected_audit_events.require_sealed()
        store = _Store(
            transaction_lock=transaction_lock,
            canonical_policy_repository=canonical_policy_repository,
            canonical_clock=canonical_clock,
            boundary_id_factory=boundary_id_factory,
            boundary_clock=boundary_clock,
        )
        self.uow_factory = _InMemoryUnitOfWorkFactory(
            store,
            context_verifier,
            outbox_events,
            event_id_factory,
            selected_audit_events,
            audit_clock,
            audit_event_id_factory,
        )
        self.initial_workspace_bootstrap = _InMemoryInitialWorkspaceBootstrap(
            store, bootstrap_authority
        )
        self.audit_reader = _InMemoryAuditReader(store, context_verifier)
        selected_delivery_routes = delivery_routes or OutboxDeliveryRouteRegistry(
            version="v1"
        )
        if delivery_routes is None:
            selected_delivery_routes.seal()
        selected_delivery_routes.require_sealed()
        self.delivery_repository = _InMemoryOutboxDeliveryRepository(
            store=store,
            routes=selected_delivery_routes,
            lease_token_factory=delivery_lease_token_factory,
            audit_events=selected_audit_events,
            audit_clock=audit_clock,
        )
        self.backup_recovery_repository = InMemoryRecoveryRepository()

    def fail_next_audit_append(self) -> None:
        self.uow_factory._store.fail_next_audit_append = True
