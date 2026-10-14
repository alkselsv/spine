"""PostgreSQL implementation of the application persistence seam."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
import hashlib
import json
from datetime import datetime, timezone
from contextlib import AbstractContextManager, asynccontextmanager
from enum import Enum, auto
from types import TracebackType
from typing import Any, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Table, func, insert, null, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.application.diagnostics.audit import (
    AuditEventRegistry,
    same_logical_audit_event,
    validate_audit_event_for_context,
)
from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.canonical import (
    ALLOWED_CANONICAL_ADMISSION_PROFILES,
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceResult,
    SourceOrderComparison,
    SourceOrderingPolicyError,
    SourceOrderingPolicyRegistry,
    validate_canonical_acceptance_effects,
)
from spine.application.persistence.boundary import (
    CANONICAL_BOUNDARY_RESULT_TYPE,
    CANONICAL_BOUNDARY_SCHEMA_VERSION,
    CanonicalBoundaryCaptureCommand,
    CanonicalBoundaryCaptureResult,
)
from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    AccessPolicyActivationResult,
    PolicyActivationDraft,
    StalePolicyBaselineError,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistenceScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
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
    InvalidPersistenceContextError,
    OptimisticConflictError,
    ObservationIntegrityConflictError,
    ObservationCommandDigestMismatchError,
    PersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.application.persistence.errors import RevisionDigestMismatchError
from spine.application.persistence.repositories import (
    SourceObservationCommand,
    SourceObservationResult,
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
from spine.application.persistence.projection import (
    PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
    PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    ProjectionActivatedOutboxPayload,
    ProjectionCatchUpRequestedOutboxPayload,
    ProjectionActivationCommand,
    ProjectionActivationCommitted,
    ProjectionActivationReplay,
    ProjectionActivationResult,
    ProjectionPublicationResolution,
    ProjectionCandidateSealCommand,
    ProjectionMutationResult,
    ProjectionReconciliationCommand,
    ProjectionReceiptCommand,
    ProjectionRepository,
    ProjectionRunCommand,
    ProjectionSnapshotCommand,
    ProjectionValidationCommand,
    register_projection_outbox_events,
    has_complete_physical_validation,
    transition_allowed,
)
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
    idempotency_conflict,
)
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
    OutboxIntent,
    validate_outbox_intent_for_context,
)
from spine.application.persistence.unit_of_work_activity import (
    track_unit_of_work_activity,
)
from spine.domain.audit import (
    AuditEvent,
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
)
from spine.domain.common import ContextOrigin, EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.domain.sources import (
    AccessGrant,
    AccessPolicyVersion,
    CurrentAccessPolicy,
    PolicyActivation,
    PolicyApproval,
    PolicyProposal,
    AdmissionDecision,
    CanonicalAcceptance,
    CanonicalOrdering,
    CurrentSourceHead,
    CanonicalBoundary,
    CanonicalBoundaryMember,
    canonical_boundary_manifest_digest,
    RevisionKind,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
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
    OntologyVersion,
    ProjectionCandidateState,
    ProjectionConfigVersion,
    ProjectionDrift,
    ProjectionDriftDetectedBy,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionReconciliationResult,
    ProjectionRun,
    ProjectionRunState,
    ProjectionRunTransition,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    ProjectionValidation,
    PublishedActivePublication,
    SourceProjectionReference,
    UnavailableActivePublication,
    UnpublishedActivePublication,
    projection_evidence_digest,
    projection_snapshot_manifest_digest,
)
from spine.domain.context_profiles import ContextProfileVersion
from spine.domain.context_bundles import ContextBundle
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.backup_recovery import (
    PostgreSQLBackupRecoveryRepository,
)
from spine.infrastructure.persistence.postgresql_errors import (
    translate_persistence_error,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    access_policy_grants as _ACCESS_POLICY_GRANTS,
    access_policy_versions as _ACCESS_POLICY_VERSIONS,
    admission_decisions as _ADMISSION_DECISIONS,
    audit_events as _AUDIT_EVENTS,
    environments as _ENVIRONMENTS,
    context_profiles as _CONTEXT_PROFILES,
    context_bundles as _CONTEXT_BUNDLES,
    idempotency_receipts as _IDEMPOTENCY_RECEIPTS,
    outbox_intents as _OUTBOX_INTENTS,
    source_objects as _SOURCE_OBJECTS,
    source_revisions as _SOURCE_REVISIONS,
    source_revision_provenance as _SOURCE_PROVENANCE,
    source_revision_receipt_associations as _SOURCE_RECEIPT_ASSOCIATIONS,
    ingestion_runs as _INGESTION_RUNS,
    ingestion_run_sources as _INGESTION_ITEMS,
    ingestion_run_source_bindings as _INGESTION_BINDINGS,
    ingestion_transitions as _INGESTION_TRANSITIONS,
    ingestion_warnings as _INGESTION_WARNINGS,
    ingestion_failures as _INGESTION_FAILURES,
    processing_receipts as _PROCESSING_RECEIPTS,
    workspaces as _WORKSPACES,
    current_access_policies as _CURRENT_ACCESS_POLICIES,
    policy_activations as _POLICY_ACTIVATIONS,
    policy_approvals as _POLICY_APPROVALS,
    policy_proposals as _POLICY_PROPOSALS,
    canonical_generation_counters as _CANONICAL_GENERATION_COUNTERS,
    canonical_acceptances as _CANONICAL_ACCEPTANCES,
    current_source_heads as _CURRENT_SOURCE_HEADS,
    canonical_boundaries as _CANONICAL_BOUNDARIES,
    canonical_boundary_members as _CANONICAL_BOUNDARY_MEMBERS,
    ontology_versions as _ONTOLOGY_VERSIONS,
    projection_config_versions as _PROJECTION_CONFIGS,
    projection_runs as _PROJECTION_RUNS,
    projection_run_transitions as _PROJECTION_TRANSITIONS,
    projection_partitions as _PROJECTION_PARTITIONS,
    projection_receipts as _PROJECTION_RECEIPTS,
    source_projection_references as _SOURCE_PROJECTION_REFERENCES,
    projection_validations as _PROJECTION_VALIDATIONS,
    projection_snapshots as _PROJECTION_SNAPSHOTS,
    projection_snapshot_members as _PROJECTION_SNAPSHOT_MEMBERS,
    projection_reconciliation_results as _PROJECTION_RECONCILIATION_RESULTS,
    projection_activations as _PROJECTION_ACTIVATIONS,
    active_projections as _ACTIVE_PROJECTIONS,
    projection_drifts as _PROJECTION_DRIFTS,
)

_BIND_CONTEXT = text(
    "SELECT "
    "set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true), "
    "set_config('spine.acting_subject_id', :acting_subject_id, true), "
    "set_config('spine.service_principal_id', :service_principal_id, true), "
    "set_config('spine.purpose', :purpose, true), "
    "set_config('spine.operation', :operation, true), "
    "set_config('spine.trace_id', :trace_id, true)"
)
_BIND_ENVIRONMENT = text(
    "SELECT set_config('spine.environment_id', :environment_id, true)"
)


def _admission_serialization_key(
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    revision_id: UUID,
) -> tuple[int, int]:
    """Return stable namespaced int4 parts for the admission transaction lock."""

    lock_id = uuid5(
        NAMESPACE_URL,
        "spine.admission-serialization.v1:"
        f"{workspace_id}:{environment_id}:{source_object_id}:{revision_id}",
    )
    return (
        int.from_bytes(lock_id.bytes[:4], byteorder="big", signed=True),
        int.from_bytes(lock_id.bytes[4:8], byteorder="big", signed=True),
    )


async def _lock_admission_revision(
    uow: "PostgreSQLTenantUnitOfWork",
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_object_id: UUID,
    revision_id: UUID,
) -> None:
    key = _admission_serialization_key(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
        revision_id=revision_id,
    )
    await uow._execute(select(func.pg_advisory_xact_lock(*key)))


class _Lifecycle(Enum):
    NEW = auto()
    ENTERING = auto()
    ACTIVE = auto()
    COMMITTED = auto()
    ROLLED_BACK = auto()
    CLOSED = auto()


class PostgreSQLTransactionStage(Enum):
    """Stable transaction boundaries exposed to diagnostics and test probes."""

    BEFORE_AUDIT_IDENTITY = auto()
    AFTER_AUDIT_FLUSH = auto()
    AFTER_CANONICAL_EVIDENCE_LOCKED = auto()
    AFTER_CANONICAL_BOUNDARY_COUNTER_LOCKED = auto()
    BEFORE_COMMIT = auto()


PostgreSQLTransactionProbe = Callable[
    [PostgreSQLTransactionStage], Awaitable[None]
]


def _label_table_columns(table: Table, prefix: str) -> tuple[Any, ...]:
    """Select a table's columns without allowing names to collide in a row."""

    return tuple(
        column.label(f"{prefix}_{column.name}") for column in table.c
    )


def _unlabel_table_row(
    row: Mapping[str, Any], table: Table, prefix: str
) -> dict[str, Any]:
    return {
        column.name: row[f"{prefix}_{column.name}"]
        for column in table.c
    }


async def _ignore_transaction_stage(stage: PostgreSQLTransactionStage) -> None:
    del stage


def _audit_event_from_row(
    row: object,
    *,
    registry: AuditEventRegistry,
) -> AuditEvent:
    payload_snapshot = row.payload
    if not isinstance(payload_snapshot, Mapping):
        raise ValueError("Persisted Audit Event payload is invalid.")
    payload = registry.restore_payload(
        event_type=row.event_type,
        schema_version=row.schema_version,
        payload_snapshot=payload_snapshot,
    )
    target = None
    if row.target_id is not None:
        target = AuditObjectReference(
            object_type=row.target_type,
            object_id=row.target_id,
            schema_version=row.target_schema_version,
        )
    return registry.validate(
        AuditEvent(
            audit_event_id=row.audit_event_id,
            event_type=row.event_type,
            schema_version=row.schema_version,
            workspace_id=row.workspace_id,
            environment_id=row.environment_id,
            origin=ContextOrigin(row.origin),
            acting_subject_id=row.acting_subject_id,
            service_principal_id=row.service_principal_id,
            trace_id=row.trace_id,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            occurred_at=row.occurred_at,
            appended_at=row.appended_at,
            target=target,
            outcome=AuditOutcome(row.outcome),
            reason=row.reason,
            producer_deduplication_id=row.producer_deduplication_id,
            payload=payload,
        )
    )


class _PostgreSQLWorkspaceRepository:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    async def add(self, workspace: Workspace) -> None:
        self._uow._guard_active()
        scope = self._uow.scope
        if (
            type(workspace) is not Workspace
            or not isinstance(scope, WorkspaceScope)
            or workspace.id != scope.workspace_id
        ):
            await self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        await self._uow._execute(
            insert(_WORKSPACES).values(
                id=workspace.id,
                slug=workspace.slug,
                display_name=workspace.display_name,
            )
        )

    async def resolve(self, workspace_id: UUID) -> Workspace | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if workspace_id != scope.workspace_id:
            return None
        result = await self._uow._execute(
            select(
                _WORKSPACES.c.id,
                _WORKSPACES.c.slug,
                _WORKSPACES.c.display_name,
            ).where(
                _WORKSPACES.c.id == workspace_id,
                _WORKSPACES.c.id == scope.workspace_id,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return Workspace(id=row.id, slug=row.slug, display_name=row.display_name)


class _PostgreSQLEnvironmentRepository:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    async def add(self, environment: Environment) -> None:
        self._uow._guard_active()
        scope = self._uow.scope
        if (
            type(environment) is not Environment
            or not isinstance(scope, WorkspaceScope)
            or environment.workspace_id != scope.workspace_id
        ):
            await self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        async with self._uow._temporary_environment_binding(environment.id):
            await self._uow._execute(
                insert(_ENVIRONMENTS).values(
                    id=environment.id,
                    workspace_id=environment.workspace_id,
                    kind=environment.kind.value,
                    display_name=environment.display_name,
                )
            )

    async def resolve(self, environment_id: UUID) -> Environment | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if isinstance(scope, EnvironmentScope) and environment_id != scope.environment_id:
            return None
        binding = (
            self._uow._temporary_environment_binding(environment_id)
            if isinstance(scope, WorkspaceScope)
            else self._uow._preserve_environment_binding()
        )
        async with binding:
            result = await self._uow._execute(
                select(
                    _ENVIRONMENTS.c.id,
                    _ENVIRONMENTS.c.workspace_id,
                    _ENVIRONMENTS.c.kind,
                    _ENVIRONMENTS.c.display_name,
                ).where(
                    _ENVIRONMENTS.c.id == environment_id,
                    _ENVIRONMENTS.c.workspace_id == scope.workspace_id,
                )
            )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return Environment(
            id=row.id,
            workspace_id=row.workspace_id,
            kind=EnvironmentKind(row.kind),
            display_name=row.display_name,
        )


class _PostgreSQLContextProfileRepository:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    @staticmethod
    def _values(profile: ContextProfileVersion) -> dict[str, object]:
        return {
            "profile_id": profile.profile_id,
            "workspace_id": profile.workspace_id,
            "environment_id": profile.environment_id,
            "profile_version": profile.profile_version,
            "profile_digest": profile.profile_digest,
            "schema_version": profile.schema_version,
            "retrieval_strategy": profile.retrieval_strategy.value,
            "max_results": profile.max_results,
            "max_tokens": profile.max_tokens,
            "max_relation_hops": profile.max_relation_hops,
            "freshness_max_age_seconds": profile.freshness_max_age_seconds,
            "completeness_requirement": profile.completeness_requirement.value,
            "partial_answer_fallback": profile.partial_answer_fallback.value,
            "server_approved": profile.server_approved,
            "revoked": profile.revoked,
            "predecessor_profile_id": profile.predecessor_profile_id,
            "created_at": profile.created_at,
        }

    @staticmethod
    def _restore(row: Mapping[str, object]) -> ContextProfileVersion:
        try:
            return ContextProfileVersion.model_validate(dict(row))
        except (TypeError, ValueError):
            raise ContextProfileMalformedError(
                "Context profile is unavailable."
            ) from None

    async def add(self, profile: ContextProfileVersion) -> None:
        self._uow._guard_active()
        scope = self._uow.scope
        if (
            type(profile) is not ContextProfileVersion
            or not isinstance(scope, EnvironmentScope)
            or profile.workspace_id != scope.workspace_id
            or profile.environment_id != scope.environment_id
        ):
            await self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        if not profile.server_approved:
            await self._uow._fail(
                ContextProfileApprovalError("Context profile is not server-approved.")
            )
        await self._uow._execute(
            insert(_CONTEXT_PROFILES).values(self._values(profile))
        )

    async def create(self, profile: ContextProfileVersion) -> ContextProfileVersion:
        self._uow._guard_active()
        scope = self._uow.scope
        if (
            type(profile) is not ContextProfileVersion
            or not isinstance(scope, EnvironmentScope)
            or profile.workspace_id != scope.workspace_id
            or profile.environment_id != scope.environment_id
        ):
            await self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        if not profile.server_approved:
            await self._uow._fail(
                ContextProfileApprovalError("Context profile is not server-approved.")
            )
        inserted = await self._uow._execute(
            postgresql_insert(_CONTEXT_PROFILES)
            .values(self._values(profile))
            .on_conflict_do_nothing(
                index_elements=[
                    _CONTEXT_PROFILES.c.workspace_id,
                    _CONTEXT_PROFILES.c.environment_id,
                    _CONTEXT_PROFILES.c.profile_id,
                    _CONTEXT_PROFILES.c.profile_version,
                ]
            )
            .returning(*_CONTEXT_PROFILES.c)
        )
        inserted_row = inserted.mappings().one_or_none()
        if inserted_row is not None:
            try:
                return self._restore(inserted_row)
            except ContextProfileMalformedError:
                await self._uow._fail(
                    ContextProfileMalformedError("Context profile is unavailable.")
                )
        existing_result = await self._uow._execute(
            select(_CONTEXT_PROFILES).where(
                _CONTEXT_PROFILES.c.profile_id == profile.profile_id,
                _CONTEXT_PROFILES.c.profile_version == profile.profile_version,
                _CONTEXT_PROFILES.c.workspace_id == scope.workspace_id,
                _CONTEXT_PROFILES.c.environment_id == scope.environment_id,
            )
        )
        existing_row = existing_result.mappings().one_or_none()
        if existing_row is None:
            await self._uow._fail(
                ConstraintConflictError(
                    "Context profile immutable version conflicts with history."
                )
            )
        try:
            existing = self._restore(existing_row)
        except ContextProfileMalformedError:
            await self._uow._fail(
                ContextProfileMalformedError("Context profile is unavailable.")
            )
        if existing != profile:
            await self._uow._fail(
                ConstraintConflictError(
                    "Context profile immutable version conflicts with history."
                )
            )
        return existing

    async def resolve(
        self,
        *,
        profile_id: UUID,
        profile_version: int,
        profile_digest: str,
    ) -> ContextProfileVersion:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            await self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        result = await self._uow._execute(
            select(_CONTEXT_PROFILES).where(
                _CONTEXT_PROFILES.c.profile_id == profile_id,
                _CONTEXT_PROFILES.c.profile_version == profile_version,
                _CONTEXT_PROFILES.c.workspace_id == scope.workspace_id,
                _CONTEXT_PROFILES.c.environment_id == scope.environment_id,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            await self._uow._fail(
                ContextProfileNotFoundError("Context profile is unavailable.")
            )
        if row["profile_digest"] != profile_digest:
            await self._uow._fail(
                ContextProfileDigestMismatchError("Context profile identity is invalid.")
            )
        try:
            profile = self._restore(row)
        except ContextProfileMalformedError:
            await self._uow._fail(
                ContextProfileMalformedError("Context profile is unavailable.")
            )
        if profile.revoked:
            await self._uow._fail(
                ContextProfileRevokedError("Context profile is unavailable.")
            )
        if not profile.server_approved:
            await self._uow._fail(
                ContextProfileApprovalError("Context profile is unavailable.")
            )
        return profile


class _PostgreSQLContextBundleRepository:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    @staticmethod
    def _values(bundle: ContextBundle) -> dict[str, object]:
        return {
            "bundle_id": bundle.bundle_id,
            "workspace_id": bundle.workspace_id,
            "environment_id": bundle.environment_id,
            "bundle_version": bundle.bundle_version,
            "bundle_digest": bundle.bundle_digest,
            "schema_version": bundle.schema_version,
            "idempotency_key": bundle.idempotency_key,
            "profile_id": bundle.profile_ref.profile_id,
            "profile_version": bundle.profile_ref.profile_version,
            "profile_digest": bundle.profile_ref.profile_digest,
            "trusted_request_context_ref": bundle.trusted_request_context_ref,
            "authorization_context_ref": bundle.authorization_context_ref,
            "request_id": bundle.request_id,
            "run_id": bundle.run_id,
            "attempt_id": bundle.attempt_id,
            "publication_pin_id": bundle.publication_pin_id,
            "projection_snapshot_id": bundle.projection_snapshot.projection_snapshot_id,
            "projection_activation_id": bundle.projection_activation.projection_activation_id,
            "activation_generation": bundle.activation_generation,
            "canonical_boundary_id": bundle.canonical_boundary.boundary_id,
            "projection_config_version_id": bundle.projection_config_version.projection_config_version_id,
            "freshness": bundle.freshness.value,
            "health": bundle.health.value,
            "retrieval_strategy": bundle.retrieval_strategy,
            "outcome": bundle.outcome.value,
            "reason_code": bundle.reason_code,
            "state": bundle.state.value,
            "reconciliation_evidence": (
                bundle.reconciliation_evidence.model_dump(mode="json")
                if bundle.reconciliation_evidence is not None
                else None
            ),
            "evidence_references": [
                item.model_dump(mode="json") for item in bundle.evidence_references
            ],
            "policy_decision_generations": [
                item.model_dump(mode="json")
                for item in bundle.policy_decision_generations
            ],
            "canonical_boundary": bundle.canonical_boundary.model_dump(mode="json"),
            "canonical_as_of": bundle.canonical_as_of.model_dump(mode="json"),
            "projection_snapshot": bundle.projection_snapshot.model_dump(mode="json"),
            "projection_activation": bundle.projection_activation.model_dump(mode="json"),
            "projection_config_version": bundle.projection_config_version.model_dump(mode="json"),
            "created_at": bundle.created_at,
        }

    @staticmethod
    def _restore(row: Mapping[str, object]) -> ContextBundle:
        try:
            values = dict(row)
            values["profile_ref"] = {
                "profile_id": values.pop("profile_id"),
                "profile_version": values.pop("profile_version"),
                "profile_digest": values.pop("profile_digest"),
            }
            for field_name in (
                "projection_snapshot_id",
                "projection_activation_id",
                "canonical_boundary_id",
                "projection_config_version_id",
            ):
                values.pop(field_name, None)
            values["effective_reconciliation"] = values.pop(
                "reconciliation_evidence", None
            )
            for field_name in (
                "canonical_boundary",
                "canonical_as_of",
                "projection_snapshot",
                "projection_activation",
                "projection_config_version",
            ):
                if not isinstance(values.get(field_name), Mapping):
                    raise ValueError("nested ContextBundle record is not an object")
            return ContextBundle.model_validate(values)
        except (TypeError, ValueError, KeyError):
            raise ContextBundleMalformedError("Context bundle is unavailable.") from None

    async def _find(
        self,
        *,
        bundle_id: UUID | None = None,
        bundle_version: int | None = None,
        run_id: UUID | None = None,
        attempt_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, object] | None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            await self._uow._fail(InvalidPersistenceContextError("Persistence context is invalid."))
        conditions = [
            _CONTEXT_BUNDLES.c.workspace_id == scope.workspace_id,
            _CONTEXT_BUNDLES.c.environment_id == scope.environment_id,
        ]
        if bundle_id is not None:
            conditions.append(_CONTEXT_BUNDLES.c.bundle_id == bundle_id)
        if bundle_version is not None:
            conditions.append(_CONTEXT_BUNDLES.c.bundle_version == bundle_version)
        if run_id is not None:
            conditions.append(_CONTEXT_BUNDLES.c.run_id == run_id)
        if attempt_id is not None:
            conditions.append(_CONTEXT_BUNDLES.c.attempt_id == attempt_id)
        if idempotency_key is not None:
            conditions.append(_CONTEXT_BUNDLES.c.idempotency_key == idempotency_key)
        result = await self._uow._execute(select(_CONTEXT_BUNDLES).where(*conditions))
        return result.mappings().one_or_none()

    def _validate_scope(self, bundle: ContextBundle) -> None:
        scope = self._uow.scope
        if (
            not isinstance(scope, EnvironmentScope)
            or bundle.workspace_id != scope.workspace_id
            or bundle.environment_id != scope.environment_id
        ):
            raise InvalidPersistenceContextError("Persistence context is invalid.")

    async def _safe_read(self, row: Mapping[str, object]) -> ContextBundle:
        bundle = self._restore(row)
        if bundle.outcome.value in {"allowed", "degraded"} and any(
            evidence.status.value != "valid" for evidence in bundle.evidence_references
        ):
            await self._uow._fail(
                ContextBundleEvidenceUnavailableError(
                    "Context bundle evidence is unavailable."
                )
            )
        return bundle

    async def add(self, bundle: ContextBundle) -> None:
        self._uow._guard_active()
        self._validate_scope(bundle)
        if await self._find(
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.bundle_version,
        ) is not None:
            await self._uow._fail(
                ContextBundleConflictError(
                    "Context bundle immutable identity already exists."
                )
            )
        if await self._find(
            run_id=bundle.run_id,
            attempt_id=bundle.attempt_id,
            idempotency_key=bundle.idempotency_key,
        ) is not None:
            await self._uow._fail(
                ContextBundleConflictError(
                    "Context bundle attempt identity conflicts with history."
                )
            )
        await self._uow._execute(insert(_CONTEXT_BUNDLES).values(self._values(bundle)))

    async def create(self, bundle: ContextBundle) -> ContextBundle:
        self._uow._guard_active()
        self._validate_scope(bundle)
        existing = await self._find(
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.bundle_version,
        )
        if existing is not None:
            restored = self._restore(existing)
            if restored != bundle:
                await self._uow._fail(
                    ContextBundleConflictError(
                        "Context bundle immutable identity conflicts with history."
                    )
                )
            return restored
        inserted = await self._uow._execute(
            postgresql_insert(_CONTEXT_BUNDLES)
            .values(self._values(bundle))
            .on_conflict_do_nothing(
                index_elements=[
                    _CONTEXT_BUNDLES.c.workspace_id,
                    _CONTEXT_BUNDLES.c.environment_id,
                    _CONTEXT_BUNDLES.c.run_id,
                    _CONTEXT_BUNDLES.c.attempt_id,
                    _CONTEXT_BUNDLES.c.idempotency_key,
                ]
            )
            .returning(*_CONTEXT_BUNDLES.c)
        )
        inserted_row = inserted.mappings().one_or_none()
        if inserted_row is not None:
            return self._restore(inserted_row)
        existing = await self._find(
            run_id=bundle.run_id,
            attempt_id=bundle.attempt_id,
            idempotency_key=bundle.idempotency_key,
        )
        if existing is None:
            await self._uow._fail(
                ContextBundleConflictError("Context bundle immutable identity conflicts with history.")
            )
        restored = self._restore(existing)
        if restored != bundle:
            await self._uow._fail(
                ContextBundleConflictError(
                    "Context bundle attempt identity conflicts with history."
                )
            )
        return restored

    async def resolve(
        self,
        *,
        bundle_id: UUID,
        bundle_version: int,
        bundle_digest: str,
    ) -> ContextBundle:
        self._uow._guard_active()
        row = await self._find(bundle_id=bundle_id, bundle_version=bundle_version)
        if row is None:
            await self._uow._fail(ContextBundleNotFoundError("Context bundle is unavailable."))
        if row["bundle_digest"] != bundle_digest:
            await self._uow._fail(ContextBundleDigestMismatchError("Context bundle identity is invalid."))
        return await self._safe_read(row)

    async def resolve_attempt(
        self,
        *,
        run_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
    ) -> ContextBundle:
        self._uow._guard_active()
        row = await self._find(
            run_id=run_id,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
        )
        if row is None:
            await self._uow._fail(ContextBundleNotFoundError("Context bundle is unavailable."))
        return await self._safe_read(row)


class _PostgreSQLSourceObservationRepository:
    """Deep source-observation seam; callers never provide an independent scope."""

    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    def _check_scope(self, workspace_id: UUID, environment_id: UUID) -> EnvironmentScope:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or scope.workspace_id != workspace_id or scope.environment_id != environment_id:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        return scope

    @staticmethod
    def _revision_from_row(row: Mapping[str, Any]) -> SourceRevision:
        """Validate a row after PostgreSQL has decoded its JSON columns.

        PostgreSQL's JSON decoder returns UUID members nested inside
        ``original_reference`` as strings, while the Issue #6 reference
        contract intentionally requires UUID instances at the domain seam.
        JSON validation preserves that strict contract without weakening the
        model or introducing a persistence-specific reference type.
        """
        return SourceRevision.model_validate_json(json.dumps(dict(row), default=str))

    @staticmethod
    def _receipt_association_from_row(
        row: Mapping[str, Any],
    ) -> SourceRevisionReceiptAssociation:
        return SourceRevisionReceiptAssociation.model_validate_json(
            json.dumps(dict(row), default=str)
        )

    async def resolve_source(self, source_object_id: UUID) -> SourceObject | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (
            await self._uow._execute(
                select(_SOURCE_OBJECTS).where(
                    _SOURCE_OBJECTS.c.source_object_id == source_object_id,
                    _SOURCE_OBJECTS.c.workspace_id == scope.workspace_id,
                    _SOURCE_OBJECTS.c.environment_id == scope.environment_id,
                )
            )
        ).mappings().one_or_none()
        return SourceObject.model_validate(dict(row)) if row is not None else None

    async def resolve_or_create_source(self, source: SourceObject) -> SourceObject:
        self._uow._guard_active()
        self._check_scope(source.workspace_id, source.environment_id)
        predicates = [
            _SOURCE_OBJECTS.c.workspace_id == source.workspace_id,
            _SOURCE_OBJECTS.c.environment_id == source.environment_id,
            _SOURCE_OBJECTS.c.identity_mode == source.identity_mode.value,
        ]
        if source.identity_mode.value == "connector":
            predicates.extend([
                _SOURCE_OBJECTS.c.connection_id == source.connection_id,
                _SOURCE_OBJECTS.c.external_namespace == source.external_namespace,
                _SOURCE_OBJECTS.c.external_generation == source.external_generation,
                _SOURCE_OBJECTS.c.external_object_id == source.external_object_id,
            ])
            columns = [_SOURCE_OBJECTS.c.workspace_id, _SOURCE_OBJECTS.c.environment_id, _SOURCE_OBJECTS.c.connection_id, _SOURCE_OBJECTS.c.external_namespace, _SOURCE_OBJECTS.c.external_generation, _SOURCE_OBJECTS.c.external_object_id]
        else:
            predicates.append(_SOURCE_OBJECTS.c.upload_identity == source.upload_identity)
            columns = [_SOURCE_OBJECTS.c.workspace_id, _SOURCE_OBJECTS.c.environment_id, _SOURCE_OBJECTS.c.upload_identity]
        row = (await self._uow._execute(select(_SOURCE_OBJECTS).where(*predicates))).mappings().one_or_none()
        if row is None:
            values = source.model_dump(mode="python")
            values["source_kind"] = getattr(source.source_kind, "value", source.source_kind)
            values["identity_mode"] = source.identity_mode.value
            await self._uow._execute(
                postgresql_insert(_SOURCE_OBJECTS).values(**values).on_conflict_do_nothing(index_elements=columns, index_where=_SOURCE_OBJECTS.c.identity_mode == source.identity_mode.value)
            )
            row = (await self._uow._execute(select(_SOURCE_OBJECTS).where(*predicates))).mappings().one_or_none()
        if row is None:
            await self._uow._fail(ConstraintConflictError("Source identity conflict."))
        return SourceObject.model_validate(dict(row))

    async def record_observation(self, command: SourceObservationCommand) -> SourceObservationResult:
        self._uow._guard_active()
        revision = command.revision
        provenance = command.provenance
        self._check_scope(revision.workspace_id, revision.environment_id)
        claim = await self._uow.idempotency.claim(
            operation_schema_version=1,
            key=command.idempotency_key,
            digest=command.digest,
        )
        expected_digest = command.expected_digest(self._uow._operation_snapshot())
        if expected_digest != command.digest:
            await self._uow._fail(ObservationCommandDigestMismatchError("Source observation command digest is invalid."))

        async def resolve(result_reference: OpaqueResultReference, replay: bool) -> SourceObservationResult:
            rev_row = (await self._uow._execute(select(_SOURCE_REVISIONS).where(
                _SOURCE_REVISIONS.c.revision_id == result_reference.result_id,
                _SOURCE_REVISIONS.c.workspace_id == revision.workspace_id,
                _SOURCE_REVISIONS.c.environment_id == revision.environment_id,
            ))).mappings().one_or_none()
            if rev_row is None:
                await self._uow._fail(ConstraintConflictError("Observation replay is unavailable."))
            source_row = (await self._uow._execute(select(_SOURCE_OBJECTS).where(
                _SOURCE_OBJECTS.c.source_object_id == rev_row.source_object_id,
                _SOURCE_OBJECTS.c.workspace_id == revision.workspace_id,
                _SOURCE_OBJECTS.c.environment_id == revision.environment_id,
            ))).mappings().one_or_none()
            event_row = (await self._uow._execute(select(_SOURCE_PROVENANCE).where(
                _SOURCE_PROVENANCE.c.revision_id == rev_row.revision_id,
                _SOURCE_PROVENANCE.c.workspace_id == revision.workspace_id,
                _SOURCE_PROVENANCE.c.environment_id == revision.environment_id,
                _SOURCE_PROVENANCE.c.producer_kind == provenance.producer_kind,
                _SOURCE_PROVENANCE.c.producer_reference == provenance.producer_reference,
                _SOURCE_PROVENANCE.c.event_identity == provenance.event_identity,
            ))).mappings().one_or_none()
            if source_row is None or event_row is None:
                await self._uow._fail(ConstraintConflictError("Observation replay is unavailable."))
            association_row = (await self._uow._execute(select(_SOURCE_RECEIPT_ASSOCIATIONS).where(
                _SOURCE_RECEIPT_ASSOCIATIONS.c.provenance_id == event_row.provenance_id,
                _SOURCE_RECEIPT_ASSOCIATIONS.c.revision_id == rev_row.revision_id,
                _SOURCE_RECEIPT_ASSOCIATIONS.c.workspace_id == revision.workspace_id,
                _SOURCE_RECEIPT_ASSOCIATIONS.c.environment_id == revision.environment_id,
            ))).mappings().one_or_none()
            if command.receipt_association is not None:
                expected_association = command.receipt_association.model_copy(
                    update={
                        "revision_id": rev_row.revision_id,
                        "associated_at": (
                            self._receipt_association_from_row(association_row).associated_at
                            if association_row is not None
                            else command.receipt_association.associated_at
                        ),
                    }
                )
                if association_row is None or self._receipt_association_from_row(association_row) != expected_association:
                    await self._uow._fail(ObservationIntegrityConflictError("Source receipt association conflicts."))
            return SourceObservationResult(
                source=SourceObject.model_validate(dict(source_row)),
                revision=self._revision_from_row(rev_row),
                provenance=SourceRevisionProvenance.model_validate(dict(event_row)),
                replay=replay,
                claim=claim,
                result_reference=result_reference,
                receipt_association=(
                    self._receipt_association_from_row(association_row)
                    if association_row is not None else None
                ),
            )

        if isinstance(claim, IdempotencyReplay):
            return await resolve(claim.result, True)
        try:
            assert_revision_digest(revision)
        except DomainRevisionDigestMismatchError as error:
            await self._uow._fail(RevisionDigestMismatchError(str(error)))
        if (
            command.source.source_object_id != revision.source_object_id
            or provenance.source_object_id != revision.source_object_id
            or provenance.revision_id != revision.revision_id
            or provenance.workspace_id != revision.workspace_id
            or provenance.environment_id != revision.environment_id
        ):
            await self._uow._fail(ConstraintConflictError("Source observation is invalid."))
        association = command.receipt_association
        if association is not None:
            if (
                association.source_object_id != revision.source_object_id
                or association.revision_id != revision.revision_id
                or association.provenance_id != provenance.provenance_id
                or association.workspace_id != revision.workspace_id
                or association.environment_id != revision.environment_id
                or revision.kind is not RevisionKind.CONTENT
                or association.original_sha256 != revision.original_sha256
                or association.byte_length != revision.byte_length
            ):
                await self._uow._fail(ConstraintConflictError("Source receipt association is invalid."))
        source = await self.resolve_or_create_source(command.source)
        if source.source_object_id != revision.source_object_id:
            await self._uow._fail(ConstraintConflictError("Source observation is invalid."))

        event_query = select(_SOURCE_PROVENANCE).where(
            _SOURCE_PROVENANCE.c.workspace_id == provenance.workspace_id,
            _SOURCE_PROVENANCE.c.environment_id == provenance.environment_id,
            _SOURCE_PROVENANCE.c.producer_kind == provenance.producer_kind,
            _SOURCE_PROVENANCE.c.producer_reference == provenance.producer_reference,
            _SOURCE_PROVENANCE.c.event_identity == provenance.event_identity,
        )
        event_row = (await self._uow._execute(event_query)).mappings().one_or_none()
        if event_row is not None:
            if event_row.event_digest != provenance.event_digest:
                await self._uow._fail(ObservationIntegrityConflictError("Source observation conflicts."))
            result = await resolve(
                OpaqueResultReference(result_type="source_revision", result_id=event_row.revision_id, schema_version=1),
                False,
            )
            await self._uow.idempotency.complete(claim, result.result_reference)
            return result

        rev_query = select(_SOURCE_REVISIONS).where(
            _SOURCE_REVISIONS.c.workspace_id == revision.workspace_id,
            _SOURCE_REVISIONS.c.environment_id == revision.environment_id,
            _SOURCE_REVISIONS.c.source_object_id == revision.source_object_id,
            _SOURCE_REVISIONS.c.revision_digest == revision.revision_digest,
        )
        rev_row = (await self._uow._execute(rev_query)).mappings().one_or_none()
        if rev_row is None:
            values = revision.model_dump(mode="python")
            values["kind"] = revision.kind.value
            values["revision_metadata"] = revision.revision_metadata.model_dump(mode="json") if revision.revision_metadata is not None else null()
            values["original_reference"] = revision.original_reference.model_dump(mode="json") if revision.original_reference is not None else null()
            await self._uow._execute(postgresql_insert(_SOURCE_REVISIONS).values(**values).on_conflict_do_nothing(constraint="uq_source_revisions_digest"))
            rev_row = (await self._uow._execute(rev_query)).mappings().one()
        if association is not None and association.revision_id != rev_row.revision_id:
            association = association.model_copy(update={"revision_id": rev_row.revision_id})
        await self._uow._execute(
            postgresql_insert(_SOURCE_PROVENANCE).values(**provenance.model_dump(mode="python")).on_conflict_do_nothing(
                index_elements=[_SOURCE_PROVENANCE.c.workspace_id, _SOURCE_PROVENANCE.c.environment_id, _SOURCE_PROVENANCE.c.producer_kind, _SOURCE_PROVENANCE.c.producer_reference, _SOURCE_PROVENANCE.c.event_identity]
            )
        )
        event_row = (await self._uow._execute(event_query)).mappings().one()
        if event_row.event_digest != provenance.event_digest:
            await self._uow._fail(ObservationIntegrityConflictError("Source observation conflicts."))
        if association is not None:
            existing_association = (await self._uow._execute(select(_SOURCE_RECEIPT_ASSOCIATIONS).where(
                _SOURCE_RECEIPT_ASSOCIATIONS.c.association_id == association.association_id,
                _SOURCE_RECEIPT_ASSOCIATIONS.c.workspace_id == revision.workspace_id,
                _SOURCE_RECEIPT_ASSOCIATIONS.c.environment_id == revision.environment_id,
            ))).mappings().one_or_none()
            if existing_association is not None and self._receipt_association_from_row(existing_association) != association.model_copy(
                update={"associated_at": self._receipt_association_from_row(existing_association).associated_at}
            ):
                await self._uow._fail(ConstraintConflictError("Source receipt association conflicts."))
            receipt_owner = (await self._uow._execute(select(_SOURCE_RECEIPT_ASSOCIATIONS.c.association_id).where(
                _SOURCE_RECEIPT_ASSOCIATIONS.c.original_receipt_id == association.original_receipt_id,
                _SOURCE_RECEIPT_ASSOCIATIONS.c.workspace_id == revision.workspace_id,
                _SOURCE_RECEIPT_ASSOCIATIONS.c.environment_id == revision.environment_id,
            ))).scalar_one_or_none()
            if receipt_owner is not None and receipt_owner != association.association_id:
                await self._uow._fail(ConstraintConflictError("Original receipt is already associated."))
            if existing_association is None:
                values = association.model_dump(mode="python")
                values["original_reference"] = association.original_reference.model_dump(mode="json")
                await self._uow._execute(
                    postgresql_insert(_SOURCE_RECEIPT_ASSOCIATIONS)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[_SOURCE_RECEIPT_ASSOCIATIONS.c.association_id])
                )
        result = await resolve(
            OpaqueResultReference(result_type="source_revision", result_id=event_row.revision_id, schema_version=1),
            False,
        )
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def resolve_revision(self, revision_id: UUID) -> SourceRevision | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (await self._uow._execute(select(_SOURCE_REVISIONS).where(_SOURCE_REVISIONS.c.revision_id == revision_id, _SOURCE_REVISIONS.c.workspace_id == scope.workspace_id, _SOURCE_REVISIONS.c.environment_id == scope.environment_id))).mappings().one_or_none()
        return self._revision_from_row(row) if row is not None else None

    async def resolve_receipt_associations(
        self, revision_id: UUID
    ) -> tuple[SourceRevisionReceiptAssociation, ...]:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return ()
        rows = (await self._uow._execute(select(_SOURCE_RECEIPT_ASSOCIATIONS).where(
            _SOURCE_RECEIPT_ASSOCIATIONS.c.revision_id == revision_id,
            _SOURCE_RECEIPT_ASSOCIATIONS.c.workspace_id == scope.workspace_id,
            _SOURCE_RECEIPT_ASSOCIATIONS.c.environment_id == scope.environment_id,
        ).order_by(
            _SOURCE_RECEIPT_ASSOCIATIONS.c.associated_at,
            _SOURCE_RECEIPT_ASSOCIATIONS.c.association_id,
        ))).mappings().all()
        return tuple(self._receipt_association_from_row(row) for row in rows)


class _PostgreSQLCanonicalAcceptanceRepository:
    """PostgreSQL-owned source-head CAS and canonical generation ledger."""

    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow
        self._ordering = SourceOrderingPolicyRegistry.r1()

    @staticmethod
    def _source(row: Mapping[str, Any]) -> SourceObject:
        return SourceObject.model_validate(dict(row))

    @staticmethod
    def _provenance(row: Mapping[str, Any]) -> SourceRevisionProvenance:
        return SourceRevisionProvenance.model_validate(dict(row))

    async def _current_content_is_eligible(
        self,
        *,
        source_id: UUID,
        revision_id: UUID,
        admission_id: UUID,
        policy_id: UUID,
    ) -> bool:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return False
        admission = (
            (
                await self._uow._execute(
                    select(_ADMISSION_DECISIONS).where(
                        _ADMISSION_DECISIONS.c.workspace_id == scope.workspace_id,
                        _ADMISSION_DECISIONS.c.environment_id == scope.environment_id,
                        _ADMISSION_DECISIONS.c.source_object_id == source_id,
                        _ADMISSION_DECISIONS.c.source_revision_id == revision_id,
                        _ADMISSION_DECISIONS.c.admission_decision_id == admission_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        current_admission = (
            await self._uow._execute(
                select(_ADMISSION_DECISIONS.c.admission_decision_id)
                .where(
                    _ADMISSION_DECISIONS.c.workspace_id == scope.workspace_id,
                    _ADMISSION_DECISIONS.c.environment_id == scope.environment_id,
                    _ADMISSION_DECISIONS.c.source_object_id == source_id,
                    _ADMISSION_DECISIONS.c.source_revision_id == revision_id,
                )
                .order_by(_ADMISSION_DECISIONS.c.decision_version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        current_policy = (
            (
                await self._uow._execute(
                    select(_CURRENT_ACCESS_POLICIES).where(
                        _CURRENT_ACCESS_POLICIES.c.workspace_id == scope.workspace_id,
                        _CURRENT_ACCESS_POLICIES.c.environment_id
                        == scope.environment_id,
                        _CURRENT_ACCESS_POLICIES.c.source_object_id == source_id,
                        _CURRENT_ACCESS_POLICIES.c.access_policy_version_id
                        == policy_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            admission is None
            or current_admission != admission_id
            or admission.disposition != "admitted"
            or admission.validator_profile_version
            not in ALLOWED_CANONICAL_ADMISSION_PROFILES
            or current_policy is None
        ):
            return False
        policy = await self._uow.access_policies.resolve_policy(policy_id)
        context = self._uow._context_snapshot_active()
        return bool(
            policy is not None
            and context.service_principal_id is not None
            and policy.allows_service_processing(
                context.service_principal_id,
                context.purpose.value,
                context.operation.value,
                datetime.now(timezone.utc),
            )
        )

    def _scope(self, command: CanonicalAcceptanceCommand) -> EnvironmentScope:
        scope = self._uow.scope
        values = (
            command.source.workspace_id,
            command.source.environment_id,
        )
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != values:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        return scope

    @staticmethod
    def _head(row: Mapping[str, Any]) -> CurrentSourceHead:
        return CurrentSourceHead.model_validate(dict(row))

    @staticmethod
    def _acceptance(row: Mapping[str, Any]) -> CanonicalAcceptance:
        return CanonicalAcceptance.model_validate(dict(row))

    async def resolve_current(self, source_object_id: UUID) -> CurrentSourceHead | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (
            await self._uow._execute(
                select(_CURRENT_SOURCE_HEADS).where(
                    _CURRENT_SOURCE_HEADS.c.source_object_id == source_object_id,
                    _CURRENT_SOURCE_HEADS.c.workspace_id == scope.workspace_id,
                    _CURRENT_SOURCE_HEADS.c.environment_id == scope.environment_id,
                ).with_for_update()
            )
        ).mappings().one_or_none()
        return self._head(row) if row is not None else None

    async def resolve_acceptance(
        self, canonical_acceptance_id: UUID
    ) -> CanonicalAcceptance | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (
            await self._uow._execute(
                select(_CANONICAL_ACCEPTANCES).where(
                    _CANONICAL_ACCEPTANCES.c.canonical_acceptance_id
                    == canonical_acceptance_id,
                    _CANONICAL_ACCEPTANCES.c.workspace_id == scope.workspace_id,
                    _CANONICAL_ACCEPTANCES.c.environment_id == scope.environment_id,
                )
            )
        ).mappings().one_or_none()
        return self._acceptance(row) if row is not None else None

    async def _replay(
        self,
        command: CanonicalAcceptanceCommand,
        claim: IdempotencyReplay,
    ) -> CanonicalAcceptanceResult:
        if claim.result.result_type == "canonical_acceptance":
            acceptance = await self.resolve_acceptance(claim.result.result_id)
            if acceptance is None:
                await self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            current = await self.resolve_current(command.source.source_object_id)
            if (
                current is None
                or current.source_object_id != acceptance.source_object_id
            ):
                await self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            content_is_eligible = (
                current.current_revision_kind is RevisionKind.CONTENT
                and acceptance.admission_decision_id is not None
                and acceptance.access_policy_version_id is not None
                and await self._current_content_is_eligible(
                    source_id=acceptance.source_object_id,
                    revision_id=acceptance.source_revision_id,
                    admission_id=acceptance.admission_decision_id,
                    policy_id=acceptance.access_policy_version_id,
                )
            )
            tombstone_is_eligible = (
                acceptance.admission_decision_id is None
                and acceptance.access_policy_version_id is None
            )
            if not content_is_eligible and not tombstone_is_eligible:
                await self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            source_row = (
                (
                    await self._uow._execute(
                        select(_SOURCE_OBJECTS).where(
                            _SOURCE_OBJECTS.c.workspace_id == acceptance.workspace_id,
                            _SOURCE_OBJECTS.c.environment_id
                            == acceptance.environment_id,
                            _SOURCE_OBJECTS.c.source_object_id
                            == acceptance.source_object_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            revision_row = (
                (
                    await self._uow._execute(
                        select(_SOURCE_REVISIONS).where(
                            _SOURCE_REVISIONS.c.workspace_id == acceptance.workspace_id,
                            _SOURCE_REVISIONS.c.environment_id
                            == acceptance.environment_id,
                            _SOURCE_REVISIONS.c.source_object_id
                            == acceptance.source_object_id,
                            _SOURCE_REVISIONS.c.revision_id
                            == acceptance.source_revision_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if source_row is None or revision_row is None:
                await self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            replayed_revision = self._uow.sources._revision_from_row(revision_row)
            if (
                content_is_eligible
                and replayed_revision.kind is not RevisionKind.CONTENT
            ) or (
                tombstone_is_eligible
                and replayed_revision.kind is not RevisionKind.TOMBSTONE
            ):
                await self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            return CanonicalAcceptanceResult(
                source=self._source(source_row),
                revision=replayed_revision,
                current=current,
                acceptance=acceptance,
                replay=True,
                advanced=False,
                claim=claim,
                result_reference=claim.result,
            )
        await self._uow._fail(
            CanonicalEligibilityError("Canonical replay is unavailable.")
        )

    async def accept(
        self, command: CanonicalAcceptanceCommand
    ) -> CanonicalAcceptanceResult:
        self._uow._guard_active()
        scope = self._scope(command)
        if command.expected_digest() != command.digest:
            await self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical acceptance command digest is invalid."
                )
            )
        if (
            command.revision.source_object_id != command.source.source_object_id
            or command.provenance.source_object_id != command.source.source_object_id
            or command.provenance.revision_id != command.revision.revision_id
            or command.provenance.workspace_id != scope.workspace_id
            or command.provenance.environment_id != scope.environment_id
        ):
            await self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical acceptance references are invalid."
                )
            )
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            return await self._replay(command, claim)

        revision_row = (
            (
                await self._uow._execute(
                    select(_SOURCE_REVISIONS).where(
                        _SOURCE_REVISIONS.c.revision_id == command.revision.revision_id,
                        _SOURCE_REVISIONS.c.source_object_id
                        == command.source.source_object_id,
                        _SOURCE_REVISIONS.c.workspace_id == scope.workspace_id,
                        _SOURCE_REVISIONS.c.environment_id == scope.environment_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        provenance_row = (
            (
                await self._uow._execute(
                    select(_SOURCE_PROVENANCE).where(
                        _SOURCE_PROVENANCE.c.provenance_id
                        == command.provenance.provenance_id,
                        _SOURCE_PROVENANCE.c.revision_id
                        == command.revision.revision_id,
                        _SOURCE_PROVENANCE.c.workspace_id == scope.workspace_id,
                        _SOURCE_PROVENANCE.c.environment_id == scope.environment_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        source_row = (
            (
                await self._uow._execute(
                    select(_SOURCE_OBJECTS).where(
                        _SOURCE_OBJECTS.c.source_object_id
                        == command.source.source_object_id,
                        _SOURCE_OBJECTS.c.workspace_id == scope.workspace_id,
                        _SOURCE_OBJECTS.c.environment_id == scope.environment_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if revision_row is None or provenance_row is None or source_row is None:
            await self._uow._fail(
                CanonicalEligibilityError(
                    "Canonical acceptance evidence is incomplete."
                )
            )
        revision = self._uow.sources._revision_from_row(revision_row)
        provenance = self._provenance(provenance_row)
        source = self._source(source_row)
        if (
            command.source != source
            or command.revision != revision
            or command.provenance != provenance
        ):
            await self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical acceptance evidence conflicts."
                )
            )

        # The counter is always locked before the source head.  This is the
        # same order used by boundary capture in the next R1 slice.
        await self._uow._execute(
            postgresql_insert(_CANONICAL_GENERATION_COUNTERS)
            .values(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                current_generation=0,
                updated_at=command.accepted_at,
            )
            .on_conflict_do_nothing()
        )
        counter_row = (
            (
                await self._uow._execute(
                    select(_CANONICAL_GENERATION_COUNTERS)
                    .where(
                        _CANONICAL_GENERATION_COUNTERS.c.workspace_id
                        == scope.workspace_id,
                        _CANONICAL_GENERATION_COUNTERS.c.environment_id
                        == scope.environment_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        await _lock_admission_revision(
            self._uow,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            source_object_id=source.source_object_id,
            revision_id=revision.revision_id,
        )
        head_row = (
            (
                await self._uow._execute(
                    select(_CURRENT_SOURCE_HEADS)
                    .where(
                        _CURRENT_SOURCE_HEADS.c.source_object_id
                        == command.source.source_object_id,
                        _CURRENT_SOURCE_HEADS.c.workspace_id == scope.workspace_id,
                        _CURRENT_SOURCE_HEADS.c.environment_id == scope.environment_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        current = self._head(head_row) if head_row is not None else None
        authoritative_revision_row = (
            (
                await self._uow._execute(
                    select(_SOURCE_REVISIONS)
                    .where(
                        _SOURCE_REVISIONS.c.workspace_id == scope.workspace_id,
                        _SOURCE_REVISIONS.c.environment_id == scope.environment_id,
                        _SOURCE_REVISIONS.c.source_object_id == source.source_object_id,
                        _SOURCE_REVISIONS.c.revision_id == revision.revision_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            authoritative_revision_row is None
            or self._uow.sources._revision_from_row(authoritative_revision_row)
            != revision
        ):
            await self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical acceptance evidence conflicts."
                )
            )

        incoming_ordered = provenance.order_scheme is not None
        comparison: SourceOrderComparison | None = None
        if incoming_ordered:
            try:
                self._ordering.compare(
                    str(provenance.order_scheme),
                    str(provenance.order_token),
                    str(provenance.order_token),
                )
            except SourceOrderingPolicyError:
                await self._uow._fail(
                    OptimisticConflictError("Trusted source order is invalid.")
                )
        if current is None:
            if (
                not incoming_ordered
                and command.expected_current_revision_id is not None
            ):
                await self._uow._fail(
                    OptimisticConflictError("The expected source head is stale.")
                )
        elif (
            incoming_ordered
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
                await self._uow._fail(
                    OptimisticConflictError("Trusted source order is invalid.")
                )
        elif incoming_ordered:
            await self._uow._fail(
                OptimisticConflictError("Trusted source order is incomparable.")
            )
        else:
            if command.expected_current_revision_id != current.current_revision_id:
                await self._uow._fail(
                    OptimisticConflictError("The expected source head is stale.")
                )

        if comparison is SourceOrderComparison.EQUAL and current is not None:
            current_revision = self._uow.sources._revision_from_row(
                (
                    await self._uow._execute(
                        select(_SOURCE_REVISIONS).where(
                            _SOURCE_REVISIONS.c.workspace_id == scope.workspace_id,
                            _SOURCE_REVISIONS.c.environment_id == scope.environment_id,
                            _SOURCE_REVISIONS.c.source_object_id
                            == source.source_object_id,
                            _SOURCE_REVISIONS.c.revision_id
                            == current.current_revision_id,
                        )
                    )
                )
                .mappings()
                .one()
            )
            if current_revision.revision_digest != revision.revision_digest:
                await self._uow._fail(
                    OptimisticConflictError(
                        "Trusted source order conflicts with a different revision."
                    )
                )
            acceptance = await self.resolve_acceptance(current.acceptance_id)
            if acceptance is None:
                await self._uow._fail(
                    CanonicalEligibilityError(
                        "Current source acceptance is unavailable."
                    )
                )
            content_is_eligible = (
                current.current_revision_kind is RevisionKind.CONTENT
                and acceptance.admission_decision_id is not None
                and acceptance.access_policy_version_id is not None
                and await self._current_content_is_eligible(
                    source_id=acceptance.source_object_id,
                    revision_id=acceptance.source_revision_id,
                    admission_id=acceptance.admission_decision_id,
                    policy_id=acceptance.access_policy_version_id,
                )
            )
            tombstone_is_eligible = (
                current.current_revision_kind is RevisionKind.TOMBSTONE
                and acceptance.admission_decision_id is None
                and acceptance.access_policy_version_id is None
            )
            if not content_is_eligible and not tombstone_is_eligible:
                await self._uow._fail(
                    CanonicalEligibilityError("Canonical replay is unavailable.")
                )
            result_reference = OpaqueResultReference(
                result_type="canonical_acceptance",
                result_id=acceptance.canonical_acceptance_id,
                schema_version=1,
            )
            await self._uow.idempotency.complete(claim, result_reference)
            return CanonicalAcceptanceResult(
                source,
                revision,
                current,
                acceptance,
                True,
                False,
                claim,
                result_reference,
            )
        if comparison is SourceOrderComparison.LOWER:
            await self._uow._fail(
                OptimisticConflictError("Trusted source order is stale.")
            )
        if (
            current is not None
            and current.current_revision_kind is RevisionKind.TOMBSTONE
            and revision.kind is RevisionKind.CONTENT
        ):
            if (
                revision.reappearance_after_tombstone_revision_id
                != current.current_revision_id
            ):
                await self._uow._fail(
                    CanonicalEligibilityError(
                        "Source reappearance continuity is invalid."
                    )
                )
        elif revision.reappearance_after_tombstone_revision_id is not None:
            await self._uow._fail(
                CanonicalEligibilityError("Source reappearance continuity is invalid.")
            )

        if revision.kind is RevisionKind.CONTENT:
            association = (
                (
                    await self._uow._execute(
                        select(_SOURCE_RECEIPT_ASSOCIATIONS).where(
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.workspace_id
                            == scope.workspace_id,
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.environment_id
                            == scope.environment_id,
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.source_object_id
                            == source.source_object_id,
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.revision_id
                            == revision.revision_id,
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.provenance_id
                            == provenance.provenance_id,
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.verification_status
                            == "verified",
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.storage_status
                            == "finalized_verified",
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.original_sha256
                            == revision.original_sha256,
                            _SOURCE_RECEIPT_ASSOCIATIONS.c.byte_length
                            == revision.byte_length,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if association is None:
                await self._uow._fail(
                    CanonicalEligibilityError(
                        "Canonical acceptance evidence is incomplete."
                    )
                )
            if (
                self._uow.sources._receipt_association_from_row(association)
                .original_reference
                != revision.original_reference
            ):
                await self._uow._fail(
                    CanonicalEligibilityError(
                        "Canonical acceptance evidence is incomplete."
                    )
                )
            admission = (
                (
                    await self._uow._execute(
                        select(_ADMISSION_DECISIONS)
                        .where(
                            _ADMISSION_DECISIONS.c.source_object_id
                            == source.source_object_id,
                            _ADMISSION_DECISIONS.c.source_revision_id
                            == revision.revision_id,
                            _ADMISSION_DECISIONS.c.workspace_id == scope.workspace_id,
                            _ADMISSION_DECISIONS.c.environment_id
                            == scope.environment_id,
                        )
                        .order_by(_ADMISSION_DECISIONS.c.decision_version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                admission is None
                or admission.disposition != "admitted"
                or admission.validator_profile_version
                not in ALLOWED_CANONICAL_ADMISSION_PROFILES
                or (
                    command.admission_decision_id is None
                    or admission.admission_decision_id != command.admission_decision_id
                )
            ):
                await self._uow._fail(
                    CanonicalEligibilityError("Content admission is not eligible.")
                )
            policy = (
                (
                    await self._uow._execute(
                        select(_CURRENT_ACCESS_POLICIES)
                        .where(
                            _CURRENT_ACCESS_POLICIES.c.source_object_id
                            == source.source_object_id,
                            _CURRENT_ACCESS_POLICIES.c.workspace_id
                            == scope.workspace_id,
                            _CURRENT_ACCESS_POLICIES.c.environment_id
                            == scope.environment_id,
                            _CURRENT_ACCESS_POLICIES.c.access_policy_version_id
                            == command.access_policy_version_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            policy_version = (
                await self._uow.access_policies.resolve_policy(
                    command.access_policy_version_id
                )
                if command.access_policy_version_id is not None
                else None
            )
            context = self._uow._context_snapshot_active()
            if (
                policy is None
                or policy_version is None
                or context.service_principal_id is None
                or not policy_version.allows_service_processing(
                    context.service_principal_id,
                    context.purpose.value,
                    context.operation.value,
                    datetime.now(timezone.utc),
                )
            ):
                await self._uow._fail(
                    CanonicalEligibilityError(
                        "An eligible current access policy is required."
                    )
                )
            policy_id = policy.access_policy_version_id
            admission_id = admission.admission_decision_id
        else:
            policy_id = None
            admission_id = None

        await self._uow._run_transaction_probe(
            PostgreSQLTransactionStage.AFTER_CANONICAL_EVIDENCE_LOCKED
        )

        generation = int(counter_row.current_generation) + 1
        ordering = (
            CanonicalOrdering.TRUSTED
            if incoming_ordered
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
        try:
            validate_canonical_acceptance_effects(
                command=command,
                acceptance=acceptance,
                context=self._uow._context_snapshot_active(),
                audit_registry=self._uow._audit_events,
                outbox_registry=self._uow._outbox_events,
            )
        except Exception:
            await self._uow._fail(
                CanonicalAuthorityConflictError(
                    "Canonical audit or outbox evidence conflicts."
                )
            )
        await self._uow._execute(insert(_CANONICAL_ACCEPTANCES).values(**acceptance.model_dump(mode="python")))
        await self._uow._execute(
            update(_CANONICAL_GENERATION_COUNTERS)
            .where(
                _CANONICAL_GENERATION_COUNTERS.c.workspace_id == scope.workspace_id,
                _CANONICAL_GENERATION_COUNTERS.c.environment_id == scope.environment_id,
            )
            .values(current_generation=generation, updated_at=command.accepted_at)
        )
        head = CurrentSourceHead(
            source_object_id=source.source_object_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            current_revision_id=revision.revision_id,
            current_revision_kind=revision.kind,
            acceptance_id=acceptance.canonical_acceptance_id,
            accepted_generation=generation,
            cas_version=(current.cas_version + 1 if current else 1),
            ordering_scheme=acceptance.ordering_scheme,
            ordering_token=acceptance.ordering_token,
            changed_at=command.accepted_at,
        )
        await self._uow._execute(
            postgresql_insert(_CURRENT_SOURCE_HEADS)
            .values(**head.model_dump(mode="python"))
            .on_conflict_do_update(
                index_elements=[_CURRENT_SOURCE_HEADS.c.source_object_id],
                set_=head.model_dump(mode="python", exclude={"source_object_id"}),
            )
        )
        # Serialize the acceptance-to-drift sink after the canonical head is
        # visible and before acceptance audit/outbox completion.
        active_rows = (
            await self._uow._execute(
                select(_ACTIVE_PROJECTIONS).where(
                    _ACTIVE_PROJECTIONS.c.workspace_id == scope.workspace_id,
                    _ACTIVE_PROJECTIONS.c.environment_id == scope.environment_id,
                ).with_for_update()
            )
        ).mappings().all()
        for active_row in active_rows:
            active = self._projections._pointer(active_row)
            snapshot_row = (
                await self._uow._execute(
                    select(_PROJECTION_SNAPSHOTS).where(
                        _PROJECTION_SNAPSHOTS.c.projection_snapshot_id == active.projection_snapshot_id,
                        _PROJECTION_SNAPSHOTS.c.workspace_id == scope.workspace_id,
                        _PROJECTION_SNAPSHOTS.c.environment_id == scope.environment_id,
                    )
                )
            ).mappings().one_or_none()
            boundary_row = (
                await self._uow._execute(
                    select(_CANONICAL_BOUNDARIES).where(
                        _CANONICAL_BOUNDARIES.c.boundary_id
                        == snapshot_row["canonical_boundary_id"],
                        _CANONICAL_BOUNDARIES.c.workspace_id == scope.workspace_id,
                        _CANONICAL_BOUNDARIES.c.environment_id == scope.environment_id,
                    )
                )
            ).mappings().one_or_none() if snapshot_row is not None else None
            if (
                snapshot_row is None
                or boundary_row is None
                or acceptance.accepted_generation <= boundary_row["accepted_through_generation"]
            ):
                continue
            if boundary_row["scope_version"] != "r1":
                await self._uow._fail(ConstraintConflictError("Projection boundary scope is unsupported."))
            drift = ProjectionDrift(
                projection_drift_id=uuid4(),
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                projection_kind=active.projection_kind,
                projection_snapshot_id=active.projection_snapshot_id,
                projection_activation_id=active.projection_activation_id,
                activation_generation=active.activation_generation,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
                source_object_id=acceptance.source_object_id,
                source_revision_id=acceptance.source_revision_id,
                accepted_generation=acceptance.accepted_generation,
                detected_by=ProjectionDriftDetectedBy.CANONICAL_ACCEPTANCE,
                detected_at=command.accepted_at,
            )
            drift_values = drift.model_dump(mode="python")
            drift_values["detected_by"] = drift.detected_by.value
            await self._uow._execute(insert(_PROJECTION_DRIFTS).values(**drift_values))
            context = self._uow._context_snapshot_active()
            await self._uow.outbox.append(
                self._uow._outbox_events.build_intent(
                    event_id=uuid4(),
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    event_type=PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
                    schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
                    payload=ProjectionCatchUpRequestedOutboxPayload(
                        projection_drift_id=drift.projection_drift_id,
                        projection_activation_id=drift.projection_activation_id,
                        projection_kind=drift.projection_kind,
                        activation_generation=drift.activation_generation,
                        canonical_acceptance_id=drift.canonical_acceptance_id,
                        source_object_id=drift.source_object_id,
                        source_revision_id=drift.source_revision_id,
                        accepted_generation=drift.accepted_generation,
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
        if command.audit_event.audit_event_id not in (None, command.audit_event_id):
            await self._uow._fail(
                ConstraintConflictError("Canonical audit identity is invalid.")
            )
        if command.outbox_intent.event_id not in (None, command.outbox_event_id):
            await self._uow._fail(
                ConstraintConflictError("Canonical outbox identity is invalid.")
            )
        audit_id = await self._uow.audit.append(command.audit_event)
        event_id = await self._uow.outbox.append(command.outbox_intent)
        if audit_id != command.audit_event_id or event_id != command.outbox_event_id:
            await self._uow._fail(
                ConstraintConflictError("Canonical evidence identity is invalid.")
            )
        result_reference = OpaqueResultReference(
            result_type="canonical_acceptance",
            result_id=acceptance.canonical_acceptance_id,
            schema_version=1,
        )
        await self._uow.idempotency.complete(claim, result_reference)
        return CanonicalAcceptanceResult(
            source, revision, head, acceptance, False, True, claim, result_reference
        )


class _PostgreSQLCanonicalBoundaryRepository:
    """PostgreSQL implementation of the counter-first immutable cut."""

    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    def _scope(self) -> EnvironmentScope:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        return scope

    @staticmethod
    def _boundary(row: Mapping[str, Any]) -> CanonicalBoundary:
        return CanonicalBoundary.model_validate(dict(row))

    @staticmethod
    def _member(row: Mapping[str, Any]) -> CanonicalBoundaryMember:
        return CanonicalBoundaryMember.model_validate(dict(row))

    async def resolve(self, boundary_id: UUID) -> CanonicalBoundary | None:
        scope = self._scope()
        row = (
            await self._uow._execute(
                select(_CANONICAL_BOUNDARIES).where(
                    _CANONICAL_BOUNDARIES.c.boundary_id == boundary_id,
                    _CANONICAL_BOUNDARIES.c.workspace_id == scope.workspace_id,
                    _CANONICAL_BOUNDARIES.c.environment_id == scope.environment_id,
                )
            )
        ).mappings().one_or_none()
        return self._boundary(row) if row is not None else None

    async def resolve_members(self, boundary_id: UUID) -> tuple[CanonicalBoundaryMember, ...]:
        scope = self._scope()
        rows = (
            await self._uow._execute(
                select(_CANONICAL_BOUNDARY_MEMBERS)
                .where(
                    _CANONICAL_BOUNDARY_MEMBERS.c.boundary_id == boundary_id,
                    _CANONICAL_BOUNDARY_MEMBERS.c.workspace_id == scope.workspace_id,
                    _CANONICAL_BOUNDARY_MEMBERS.c.environment_id == scope.environment_id,
                )
                .order_by(_CANONICAL_BOUNDARY_MEMBERS.c.source_object_id)
            )
        ).mappings().all()
        return tuple(self._member(row) for row in rows)

    async def capture(
        self, command: CanonicalBoundaryCaptureCommand
    ) -> CanonicalBoundaryCaptureResult:
        scope = self._scope()
        if command.expected_digest() != command.digest:
            await self._uow._fail(
                ConstraintConflictError("Canonical boundary command digest is invalid.")
            )

        # This is the global serialization barrier. It must precede all source
        # head reads so READ COMMITTED cannot mix generations.
        counter_updated_at = (
            self._uow._boundary_clock()
            if self._uow._boundary_clock is not None
            else func.current_timestamp()
        )
        await self._uow._execute(
            postgresql_insert(_CANONICAL_GENERATION_COUNTERS)
            .values(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                current_generation=0,
                updated_at=counter_updated_at,
            )
            .on_conflict_do_nothing()
        )
        counter_row = (
            await self._uow._execute(
                select(_CANONICAL_GENERATION_COUNTERS)
                .where(
                    _CANONICAL_GENERATION_COUNTERS.c.workspace_id == scope.workspace_id,
                    _CANONICAL_GENERATION_COUNTERS.c.environment_id == scope.environment_id,
                )
                .with_for_update()
            )
        ).mappings().one()
        accepted_through_generation = int(counter_row.current_generation)
        if self._uow._boundary_clock is not None:
            boundary_time = self._uow._boundary_clock()
        else:
            boundary_time = (await self._uow._execute(select(func.current_timestamp()))).scalar_one()
        await self._uow._run_transaction_probe(
            PostgreSQLTransactionStage.AFTER_CANONICAL_BOUNDARY_COUNTER_LOCKED
        )

        claim = await self._uow.idempotency.claim(
            operation_schema_version=CANONICAL_BOUNDARY_SCHEMA_VERSION,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            boundary = await self.resolve(claim.result.result_id)
            if boundary is None:
                await self._uow._fail(
                    ConstraintConflictError("Canonical boundary replay is unavailable.")
                )
            members = await self.resolve_members(boundary.boundary_id)
            if canonical_boundary_manifest_digest(boundary, members) != boundary.manifest_digest:
                await self._uow._fail(
                    ConstraintConflictError("Canonical boundary replay is unavailable.")
                )
            return CanonicalBoundaryCaptureResult(boundary, members, True, claim, claim.result)

        boundary_id = self._uow._boundary_id_factory()
        heads = (
            await self._uow._execute(
                select(_CURRENT_SOURCE_HEADS)
                .where(
                    _CURRENT_SOURCE_HEADS.c.workspace_id == scope.workspace_id,
                    _CURRENT_SOURCE_HEADS.c.environment_id == scope.environment_id,
                    _CURRENT_SOURCE_HEADS.c.accepted_generation <= accepted_through_generation,
                )
                .order_by(_CURRENT_SOURCE_HEADS.c.source_object_id)
            )
        ).mappings().all()
        members = tuple(
            CanonicalBoundaryMember(
                boundary_id=boundary_id,
                source_object_id=row["source_object_id"],
                source_revision_id=row["current_revision_id"],
                acceptance_id=row["acceptance_id"],
                workspace_id=row["workspace_id"],
                environment_id=row["environment_id"],
                revision_kind=row["current_revision_kind"],
                accepted_generation=row["accepted_generation"],
            )
            for row in heads
        )
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
        await self._uow._execute(
            insert(_CANONICAL_BOUNDARIES).values(**boundary.model_dump(mode="python"))
        )
        if members:
            await self._uow._execute(
                insert(_CANONICAL_BOUNDARY_MEMBERS).values(
                    [member.model_dump(mode="python") for member in members]
                )
            )
        reference = OpaqueResultReference(
            result_type=CANONICAL_BOUNDARY_RESULT_TYPE,
            result_id=boundary.boundary_id,
            schema_version=CANONICAL_BOUNDARY_SCHEMA_VERSION,
        )
        await self._uow.idempotency.complete(claim, reference)
        return CanonicalBoundaryCaptureResult(boundary, members, False, claim, reference)


class _PostgreSQLIngestionRepository:
    """Tenant-scoped PostgreSQL implementation of the append-only ledger."""

    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    def _check_scope(self, workspace_id: UUID, environment_id: UUID) -> None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (scope.workspace_id, scope.environment_id) != (workspace_id, environment_id):
            raise InvalidPersistenceContextError("Persistence context is invalid.")

    @staticmethod
    def _detail_values(detail: object) -> tuple[str, dict[str, object], str]:
        payload = detail.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return payload["schema_version"], payload, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _transition_from_row(row: Mapping[str, Any]) -> IngestionTransition:
        values = dict(row)
        safe_metrics_schema = values.pop("safe_metrics_schema", None)
        safe_metrics = values.get("safe_metrics")
        if safe_metrics is not None and "schema_version" not in safe_metrics:
            safe_metrics = {"schema_version": safe_metrics_schema, **safe_metrics}
        values["safe_metrics"] = safe_metrics
        return IngestionTransition.model_validate(values)

    @staticmethod
    def _warning_from_row(row: Mapping[str, Any]) -> IngestionWarning:
        values = dict(row)
        detail_schema = values.pop("safe_detail_schema", None)
        detail = values.pop("safe_detail", None)
        values.pop("safe_detail_digest", None)
        values.pop("retryable", None)
        if detail is not None and "schema_version" not in detail:
            detail = {"schema_version": detail_schema, **detail}
        values["safe_detail"] = detail
        return IngestionWarning.model_validate_json(json.dumps(values, default=str))

    @staticmethod
    def _failure_from_row(row: Mapping[str, Any]) -> IngestionFailure:
        values = dict(row)
        detail_schema = values.pop("safe_detail_schema", None)
        detail = values.pop("safe_detail", None)
        values.pop("safe_detail_digest", None)
        if detail is not None and "schema_version" not in detail:
            detail = {"schema_version": detail_schema, **detail}
        values["safe_detail"] = detail
        return IngestionFailure.model_validate_json(json.dumps(values, default=str))

    @staticmethod
    def _binding_from_row(row: Mapping[str, Any]) -> IngestionRunSourceBinding:
        # asyncpg decodes JSONB UUID members as strings.  JSON validation lets
        # the strict domain model parse those wire values without weakening the
        # model's in-memory type guarantees.
        return IngestionRunSourceBinding.model_validate_json(
            json.dumps(dict(row), default=str)
        )

    async def _head(self, *, run_id: UUID, item_id: UUID | None) -> IngestionTransition | None:
        predicates = [
            _INGESTION_TRANSITIONS.c.workspace_id == self._uow.scope.workspace_id,
            _INGESTION_TRANSITIONS.c.environment_id == self._uow.scope.environment_id,
            _INGESTION_TRANSITIONS.c.ingestion_run_id == run_id,
            _INGESTION_TRANSITIONS.c.ingestion_run_source_id == item_id if item_id is not None else _INGESTION_TRANSITIONS.c.ingestion_run_source_id.is_(None),
        ]
        rows = (await self._uow._execute(select(_INGESTION_TRANSITIONS).where(*predicates))).mappings().all()
        if not rows:
            return None
        transitions = tuple(self._transition_from_row(row) for row in rows)
        predecessor_ids = {transition.predecessor_transition_id for transition in transitions if transition.predecessor_transition_id is not None}
        heads = tuple(transition for transition in transitions if transition.ingestion_transition_id not in predecessor_ids)
        if len(heads) != 1:
            await self._uow._fail(ConstraintConflictError("Ingestion transition history has multiple heads."))
        return heads[0]

    async def create_run(self, command: IngestionRunCommand) -> IngestionMutationResult:
        self._uow._guard_active()
        run = command.run
        self._check_scope(run.workspace_id, run.environment_id)
        claim = await self._uow.idempotency.claim(operation_schema_version=command.digest.operation_schema_version, key=command.idempotency_key, digest=command.digest)
        if isinstance(claim, IdempotencyReplay):
            row = (await self._uow._execute(select(_INGESTION_RUNS).where(_INGESTION_RUNS.c.ingestion_run_id == claim.result.result_id, _INGESTION_RUNS.c.workspace_id == run.workspace_id, _INGESTION_RUNS.c.environment_id == run.environment_id))).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Ingestion run replay is unavailable."))
            return IngestionMutationResult(IngestionRun.model_validate(dict(row)), True, claim, claim.result)
        run = run.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        await self._uow._execute(
            insert(_INGESTION_RUNS).values(
                ingestion_run_id=run.ingestion_run_id, workspace_id=run.workspace_id, environment_id=run.environment_id,
                command_identity=run.command_identity, command_digest=run.command_digest, idempotency_receipt_id=run.idempotency_receipt_id,
                trigger_kind=run.trigger_kind, trigger_reference=run.trigger_reference, requested_scope_schema=run.requested_scope_schema,
                requested_scope_digest=run.requested_scope_digest, requested_at=run.requested_at,
            )
        )
        await self.append_transition(IngestionTransition(
            ingestion_transition_id=uuid5(NAMESPACE_URL, f"ingestion-run-root:{run.ingestion_run_id}"), workspace_id=run.workspace_id, environment_id=run.environment_id,
            target_kind=IngestionTargetKind.RUN, ingestion_run_id=run.ingestion_run_id, state=IngestionRunState.REQUESTED, attempt=1,
            actor_or_service="ingestion-ledger", trace_id=self._uow._trace_id_snapshot(), occurred_at=run.requested_at,
        ))
        result = OpaqueResultReference(result_type="ingestion_run", result_id=run.ingestion_run_id, schema_version=1)
        await self._uow.idempotency.complete(claim, result)
        return IngestionMutationResult(run, False, claim, result)

    async def create_item(self, item: IngestionRunSource) -> IngestionRunSource:
        self._uow._guard_active()
        self._check_scope(item.workspace_id, item.environment_id)
        run_row = (await self._uow._execute(select(_INGESTION_RUNS.c.ingestion_run_id).where(_INGESTION_RUNS.c.ingestion_run_id == item.ingestion_run_id, _INGESTION_RUNS.c.workspace_id == item.workspace_id, _INGESTION_RUNS.c.environment_id == item.environment_id))).first()
        if run_row is None:
            await self._uow._fail(ConstraintConflictError("Ingestion item run reference is invalid."))
        existing = (await self._uow._execute(select(_INGESTION_ITEMS).where(_INGESTION_ITEMS.c.ingestion_run_source_id == item.ingestion_run_source_id, _INGESTION_ITEMS.c.workspace_id == item.workspace_id, _INGESTION_ITEMS.c.environment_id == item.environment_id))).mappings().one_or_none()
        if existing is not None:
            current = IngestionRunSource.model_validate(dict(existing))
            if current != item:
                await self._uow._fail(ConstraintConflictError("Ingestion item identity conflicts."))
            return current
        await self._uow._execute(insert(_INGESTION_ITEMS).values(**item.model_dump(mode="python")))
        await self.append_transition(IngestionTransition(
            ingestion_transition_id=uuid5(NAMESPACE_URL, f"ingestion-item-root:{item.ingestion_run_source_id}"), workspace_id=item.workspace_id, environment_id=item.environment_id,
            target_kind=IngestionTargetKind.ITEM, ingestion_run_id=item.ingestion_run_id, ingestion_run_source_id=item.ingestion_run_source_id,
            state=IngestionItemState.RECEIVED, attempt=1, actor_or_service="ingestion-ledger", trace_id=self._uow._trace_id_snapshot(), occurred_at=item.created_at,
        ))
        return item

    async def bind_source(self, command: IngestionBindingCommand) -> IngestionMutationResult:
        self._uow._guard_active()
        self._check_scope(command.item.workspace_id, command.item.environment_id)
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
                await self._uow._fail(
                    ConstraintConflictError(
                        "Original receipt verification is unavailable or incomplete."
                    )
                )
        if command.observation.revision.kind.value != "content" and command.binding.original_receipt_id is not None:
            await self._uow._fail(
                ConstraintConflictError(
                    "Contentless ingestion binding cannot include an original receipt."
                )
            )
        claim = await self._uow.idempotency.claim(operation_schema_version=command.digest.operation_schema_version, key=command.idempotency_key, digest=command.digest)
        item_row = (await self._uow._execute(select(_INGESTION_ITEMS).where(_INGESTION_ITEMS.c.ingestion_run_source_id == command.item.ingestion_run_source_id, _INGESTION_ITEMS.c.workspace_id == command.item.workspace_id, _INGESTION_ITEMS.c.environment_id == command.item.environment_id))).mappings().one_or_none()
        if item_row is None or IngestionRunSource.model_validate(dict(item_row)) != command.item:
            await self._uow._fail(ConstraintConflictError("Ingestion binding item is invalid."))
        existing_row = (await self._uow._execute(select(_INGESTION_BINDINGS).where(_INGESTION_BINDINGS.c.ingestion_run_source_id == command.item.ingestion_run_source_id, _INGESTION_BINDINGS.c.workspace_id == command.item.workspace_id, _INGESTION_BINDINGS.c.environment_id == command.item.environment_id))).mappings().one_or_none()
        if isinstance(claim, IdempotencyReplay):
            row = (await self._uow._execute(select(_INGESTION_BINDINGS).where(_INGESTION_BINDINGS.c.binding_id == claim.result.result_id, _INGESTION_BINDINGS.c.workspace_id == command.item.workspace_id, _INGESTION_BINDINGS.c.environment_id == command.item.environment_id))).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Ingestion binding replay is unavailable."))
            return IngestionMutationResult(self._binding_from_row(row), True, claim, claim.result)
        binding = command.binding
        if binding.ingestion_run_source_id != command.item.ingestion_run_source_id or (binding.workspace_id, binding.environment_id) != (command.item.workspace_id, command.item.environment_id):
            await self._uow._fail(ConstraintConflictError("Ingestion binding scope is invalid."))
        if existing_row is not None:
            existing = self._binding_from_row(existing_row)
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
                await self._uow._fail(ConstraintConflictError("Ingestion item is already bound to another source."))
            result = OpaqueResultReference(result_type="ingestion_binding", result_id=existing.binding_id, schema_version=1)
            await self._uow.idempotency.complete(claim, result)
            return IngestionMutationResult(existing, True, claim, result)
        if command.observation.source.source_object_id != binding.source_object_id or command.observation.revision.revision_id != binding.source_revision_id:
            await self._uow._fail(ConstraintConflictError("Ingestion binding observation is invalid."))
        from dataclasses import replace
        observation = replace(command.observation, digest=command.observation.expected_digest(PersistenceOperation("ingestion_binding")))
        observed = await self._uow.sources.record_observation(observation)
        binding = binding.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        await self._uow._execute(insert(_INGESTION_BINDINGS).values(
            binding_id=binding.binding_id, ingestion_run_source_id=binding.ingestion_run_source_id, source_object_id=binding.source_object_id, source_revision_id=binding.source_revision_id,
            workspace_id=binding.workspace_id, environment_id=binding.environment_id, binding_command_identity=binding.binding_command_identity, binding_command_digest=binding.binding_command_digest,
            original_reference=binding.original_reference.model_dump(mode="json") if binding.original_reference else None, original_receipt_kind=binding.original_receipt_kind,
            original_receipt_id=binding.original_receipt_id, original_receipt_digest=binding.original_receipt_digest, idempotency_receipt_id=binding.idempotency_receipt_id, bound_at=binding.bound_at,
        ))
        previous = await self._head(run_id=command.item.ingestion_run_id, item_id=command.item.ingestion_run_source_id)
        if previous is None:
            await self._uow._fail(ConstraintConflictError("Ingestion item transition head is unavailable."))
        await self.append_transition(IngestionTransition(
            ingestion_transition_id=uuid4(), workspace_id=binding.workspace_id, environment_id=binding.environment_id, target_kind=IngestionTargetKind.ITEM,
            ingestion_run_id=command.item.ingestion_run_id, ingestion_run_source_id=binding.ingestion_run_source_id, predecessor_transition_id=previous.ingestion_transition_id,
            state=IngestionItemState.OBSERVED, attempt=1, actor_or_service="ingestion-ledger", trace_id=self._uow._trace_id_snapshot(), occurred_at=binding.bound_at,
        ))
        result = OpaqueResultReference(result_type="ingestion_binding", result_id=binding.binding_id, schema_version=1)
        await self._uow.idempotency.complete(claim, result)
        return IngestionMutationResult(binding, False, claim, result)

    async def append_transition(self, transition: IngestionTransition) -> IngestionTransition:
        self._uow._guard_active()
        self._check_scope(transition.workspace_id, transition.environment_id)
        existing_row = (await self._uow._execute(select(_INGESTION_TRANSITIONS).where(_INGESTION_TRANSITIONS.c.ingestion_transition_id == transition.ingestion_transition_id, _INGESTION_TRANSITIONS.c.workspace_id == transition.workspace_id, _INGESTION_TRANSITIONS.c.environment_id == transition.environment_id))).mappings().one_or_none()
        if existing_row is not None:
            existing = self._transition_from_row(existing_row)
            if existing != transition:
                await self._uow._fail(ConstraintConflictError("Ingestion transition identity conflicts."))
            return existing
        previous = await self._head(run_id=transition.ingestion_run_id, item_id=transition.ingestion_run_source_id)
        try:
            validate_transition(previous, transition)
        except ValueError as error:
            await self._uow._fail(ConstraintConflictError(str(error)))
        safe_metrics_schema, safe_metrics = (None, null())
        if transition.safe_metrics is not None:
            safe_metrics_schema, safe_metrics, _ = self._detail_values(transition.safe_metrics)
        await self._uow._execute(insert(_INGESTION_TRANSITIONS).values(
            ingestion_transition_id=transition.ingestion_transition_id, workspace_id=transition.workspace_id, environment_id=transition.environment_id,
            target_kind=transition.target_kind.value, ingestion_run_id=transition.ingestion_run_id, ingestion_run_source_id=transition.ingestion_run_source_id,
            predecessor_transition_id=transition.predecessor_transition_id, state=transition.state.value, attempt=transition.attempt,
            safe_metrics_schema=safe_metrics_schema, safe_metrics=safe_metrics, actor_or_service=transition.actor_or_service, trace_id=transition.trace_id, occurred_at=transition.occurred_at,
        ))
        return transition

    async def append_warning(self, warning: IngestionWarning) -> IngestionWarning:
        self._uow._guard_active()
        self._check_scope(warning.workspace_id, warning.environment_id)
        schema, detail, digest = self._detail_values(warning.safe_detail)
        await self._uow._execute(insert(_INGESTION_WARNINGS).values(
            ingestion_warning_id=warning.ingestion_warning_id, workspace_id=warning.workspace_id, environment_id=warning.environment_id, ingestion_run_source_id=warning.ingestion_run_source_id,
            attempt=warning.attempt, stage=warning.stage.value, code=warning.code, retryable=False, safe_detail_schema=schema, safe_detail=detail, safe_detail_digest=digest,
            opaque_detail_ref=warning.safe_detail.opaque_detail_ref, occurred_at=warning.occurred_at,
        ))
        return warning

    async def append_failure(self, failure: IngestionFailure) -> IngestionFailure:
        self._uow._guard_active()
        self._check_scope(failure.workspace_id, failure.environment_id)
        schema, detail, digest = self._detail_values(failure.safe_detail)
        await self._uow._execute(insert(_INGESTION_FAILURES).values(
            ingestion_failure_id=failure.ingestion_failure_id, workspace_id=failure.workspace_id, environment_id=failure.environment_id, ingestion_run_source_id=failure.ingestion_run_source_id,
            attempt=failure.attempt, stage=failure.stage.value, code=failure.code, retryable=failure.retryable, safe_detail_schema=schema, safe_detail=detail, safe_detail_digest=digest,
            opaque_detail_ref=failure.safe_detail.opaque_detail_ref, occurred_at=failure.occurred_at,
        ))
        return failure

    async def record_processing_receipt(self, command: ProcessingReceiptCommand) -> IngestionMutationResult:
        self._uow._guard_active()
        receipt = command.receipt
        self._check_scope(receipt.workspace_id, receipt.environment_id)
        claim = await self._uow.idempotency.claim(operation_schema_version=command.digest.operation_schema_version, key=command.idempotency_key, digest=command.digest)
        if isinstance(claim, IdempotencyReplay):
            row = (await self._uow._execute(select(_PROCESSING_RECEIPTS).where(_PROCESSING_RECEIPTS.c.processing_receipt_id == claim.result.result_id, _PROCESSING_RECEIPTS.c.workspace_id == receipt.workspace_id, _PROCESSING_RECEIPTS.c.environment_id == receipt.environment_id))).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Processing receipt replay is unavailable."))
            return IngestionMutationResult(ProcessingReceipt.model_validate(dict(row)), True, claim, claim.result)
        receipt = receipt.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        revision_row = (await self._uow._execute(select(_SOURCE_REVISIONS.c.original_sha256).where(_SOURCE_REVISIONS.c.revision_id == receipt.source_revision_id, _SOURCE_REVISIONS.c.source_object_id == receipt.source_object_id, _SOURCE_REVISIONS.c.workspace_id == receipt.workspace_id, _SOURCE_REVISIONS.c.environment_id == receipt.environment_id))).first()
        if revision_row is None or revision_row.original_sha256 != receipt.original_sha256:
            await self._uow._fail(ConstraintConflictError("Processing receipt original digest is invalid."))
        item_row = (await self._uow._execute(select(_INGESTION_ITEMS.c.ingestion_run_source_id).where(_INGESTION_ITEMS.c.ingestion_run_source_id == receipt.ingestion_run_source_id, _INGESTION_ITEMS.c.workspace_id == receipt.workspace_id, _INGESTION_ITEMS.c.environment_id == receipt.environment_id))).first()
        binding_row = (await self._uow._execute(select(_INGESTION_BINDINGS.c.binding_id).where(_INGESTION_BINDINGS.c.binding_id == receipt.binding_id, _INGESTION_BINDINGS.c.ingestion_run_source_id == receipt.ingestion_run_source_id, _INGESTION_BINDINGS.c.source_object_id == receipt.source_object_id, _INGESTION_BINDINGS.c.source_revision_id == receipt.source_revision_id, _INGESTION_BINDINGS.c.workspace_id == receipt.workspace_id, _INGESTION_BINDINGS.c.environment_id == receipt.environment_id))).first()
        if item_row is None or binding_row is None:
            await self._uow._fail(ConstraintConflictError("Processing receipt references are invalid."))
        await self._uow._execute(insert(_PROCESSING_RECEIPTS).values(
            processing_receipt_id=receipt.processing_receipt_id, workspace_id=receipt.workspace_id, environment_id=receipt.environment_id, ingestion_run_source_id=receipt.ingestion_run_source_id,
            binding_id=receipt.binding_id, source_object_id=receipt.source_object_id, source_revision_id=receipt.source_revision_id, processor_kind=receipt.processor_kind, parser_name=receipt.parser_name,
            parser_version=receipt.parser_version, parser_config_reference=receipt.parser_config_reference, locator_contract_version=receipt.locator_contract_version, original_sha256=receipt.original_sha256,
            output_kind=receipt.output_kind, output_reference=receipt.output_reference, output_digest=receipt.output_digest, coverage=receipt.coverage.value, exclusions=[item.model_dump(mode="json") for item in receipt.exclusions],
            warning_ids=[str(item) for item in receipt.warning_ids], failure_ids=[str(item) for item in receipt.failure_ids], command_identity=receipt.command_identity, command_digest=receipt.command_digest,
            idempotency_receipt_id=receipt.idempotency_receipt_id, completed_at=receipt.completed_at,
        ))
        result = OpaqueResultReference(result_type="processing_receipt", result_id=receipt.processing_receipt_id, schema_version=1)
        await self._uow.idempotency.complete(claim, result)
        return IngestionMutationResult(receipt, False, claim, result)

    async def derive_run_state(self, run_id: UUID) -> IngestionRunStateView | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        run_row = (await self._uow._execute(select(_INGESTION_RUNS).where(_INGESTION_RUNS.c.ingestion_run_id == run_id, _INGESTION_RUNS.c.workspace_id == scope.workspace_id, _INGESTION_RUNS.c.environment_id == scope.environment_id))).mappings().one_or_none()
        if run_row is None:
            return None
        run = IngestionRun.model_validate(dict(run_row))
        item_rows = (
            await self._uow._execute(
                select(_INGESTION_ITEMS)
                .where(
                    _INGESTION_ITEMS.c.ingestion_run_id == run_id,
                    _INGESTION_ITEMS.c.workspace_id == scope.workspace_id,
                    _INGESTION_ITEMS.c.environment_id == scope.environment_id,
                )
                .order_by(_INGESTION_ITEMS.c.input_ordinal)
            )
        ).mappings().all()
        items = tuple(IngestionRunSource.model_validate(dict(row)) for row in item_rows)
        item_states: list[IngestionItemState] = []
        for row in item_rows:
            head = await self._head(run_id=run_id, item_id=row.ingestion_run_source_id)
            if head is not None:
                item_states.append(head.state)  # type: ignore[arg-type]
        run_head = await self._head(run_id=run_id, item_id=None)
        if run_head is None:
            await self._uow._fail(ConstraintConflictError("Ingestion run transition head is unavailable."))
        state = run_head.state
        if state is IngestionRunState.PROCESSING and item_states and all(item_state in {IngestionItemState.PROCESSED, IngestionItemState.PROJECTION_REQUESTED} for item_state in item_states):
            state = IngestionRunState.SUCCEEDED
        elif state is IngestionRunState.PROCESSING and any(item_state in {IngestionItemState.QUARANTINED, IngestionItemState.REJECTED, IngestionItemState.PROCESSING_FAILED} for item_state in item_states) and any(item_state in {IngestionItemState.PROCESSED, IngestionItemState.PROJECTION_REQUESTED} for item_state in item_states):
            state = IngestionRunState.PARTIALLY_SUCCEEDED
        elif state is IngestionRunState.PROCESSING and item_states and all(item_state in {IngestionItemState.QUARANTINED, IngestionItemState.REJECTED, IngestionItemState.PROCESSING_FAILED} for item_state in item_states):
            state = IngestionRunState.FAILED
        return IngestionRunStateView(
            run=run,
            state=state,
            items=items,
            item_states=tuple(item_states),
        )

    async def resolve_item(self, item_id: UUID) -> IngestionRunSource | None:
        self._uow._guard_active()
        scope = self._uow.scope
        row = (await self._uow._execute(select(_INGESTION_ITEMS).where(_INGESTION_ITEMS.c.ingestion_run_source_id == item_id, _INGESTION_ITEMS.c.workspace_id == scope.workspace_id, _INGESTION_ITEMS.c.environment_id == scope.environment_id))).mappings().one_or_none()
        return IngestionRunSource.model_validate(dict(row)) if row is not None else None

    async def resolve_binding(self, item_id: UUID) -> IngestionRunSourceBinding | None:
        self._uow._guard_active()
        scope = self._uow.scope
        row = (
            await self._uow._execute(
                select(_INGESTION_BINDINGS)
                .where(
                    _INGESTION_BINDINGS.c.ingestion_run_source_id == item_id,
                    _INGESTION_BINDINGS.c.workspace_id == scope.workspace_id,
                    _INGESTION_BINDINGS.c.environment_id == scope.environment_id,
                )
                .limit(1)
            )
        ).mappings().one_or_none()
        return self._binding_from_row(row) if row is not None else None

    async def resolve_run_head(self, run_id: UUID) -> IngestionTransition | None:
        self._uow._guard_active()
        return await self._head(run_id=run_id, item_id=None)

    async def resolve_item_head(self, item_id: UUID) -> IngestionTransition | None:
        self._uow._guard_active()
        run_id = await self._run_id_for_item(item_id)
        return None if run_id is None else await self._head(run_id=run_id, item_id=item_id)

    async def _run_id_for_item(self, item_id: UUID) -> UUID | None:
        row = (
            await self._uow._execute(
                select(_INGESTION_ITEMS.c.ingestion_run_id).where(
                    _INGESTION_ITEMS.c.ingestion_run_source_id == item_id,
                    _INGESTION_ITEMS.c.workspace_id == self._uow.scope.workspace_id,
                    _INGESTION_ITEMS.c.environment_id == self._uow.scope.environment_id,
                )
            )
        ).scalar_one_or_none()
        return row

    async def resolve_processing_receipt(self, item_id: UUID) -> ProcessingReceipt | None:
        self._uow._guard_active()
        row = (
            await self._uow._execute(
                select(_PROCESSING_RECEIPTS)
                .where(
                    _PROCESSING_RECEIPTS.c.ingestion_run_source_id == item_id,
                    _PROCESSING_RECEIPTS.c.workspace_id == self._uow.scope.workspace_id,
                    _PROCESSING_RECEIPTS.c.environment_id == self._uow.scope.environment_id,
                )
                .order_by(_PROCESSING_RECEIPTS.c.completed_at.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        return ProcessingReceipt.model_validate(dict(row)) if row is not None else None

    async def resolve_warnings(self, item_id: UUID) -> tuple[IngestionWarning, ...]:
        self._uow._guard_active()
        rows = (
            await self._uow._execute(
                select(_INGESTION_WARNINGS)
                .where(
                    _INGESTION_WARNINGS.c.ingestion_run_source_id == item_id,
                    _INGESTION_WARNINGS.c.workspace_id == self._uow.scope.workspace_id,
                    _INGESTION_WARNINGS.c.environment_id == self._uow.scope.environment_id,
                )
                .order_by(_INGESTION_WARNINGS.c.attempt, _INGESTION_WARNINGS.c.occurred_at)
            )
        ).mappings().all()
        return tuple(self._warning_from_row(row) for row in rows)

    async def resolve_failures(self, item_id: UUID) -> tuple[IngestionFailure, ...]:
        self._uow._guard_active()
        rows = (
            await self._uow._execute(
                select(_INGESTION_FAILURES)
                .where(
                    _INGESTION_FAILURES.c.ingestion_run_source_id == item_id,
                    _INGESTION_FAILURES.c.workspace_id == self._uow.scope.workspace_id,
                    _INGESTION_FAILURES.c.environment_id == self._uow.scope.environment_id,
                )
                .order_by(_INGESTION_FAILURES.c.attempt, _INGESTION_FAILURES.c.occurred_at)
            )
        ).mappings().all()
        return tuple(self._failure_from_row(row) for row in rows)


class _PostgreSQLAdmissionRepository:
    """PostgreSQL append-only admission decision chain."""

    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    def _scope_predicates(self, decision: AdmissionDecision) -> tuple[object, ...]:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != decision.workspace_id
            or scope.environment_id != decision.environment_id
        ):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        return (
            _ADMISSION_DECISIONS.c.workspace_id == decision.workspace_id,
            _ADMISSION_DECISIONS.c.environment_id == decision.environment_id,
        )

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> AdmissionDecision:
        return AdmissionDecision.model_validate(dict(row))

    async def append_decision(self, decision: AdmissionDecision) -> AdmissionDecision:
        self._uow._guard_active()
        scope_predicates = self._scope_predicates(decision)
        await _lock_admission_revision(
            self._uow,
            workspace_id=decision.workspace_id,
            environment_id=decision.environment_id,
            source_object_id=decision.source_object_id,
            revision_id=decision.revision_id,
        )
        revision_row = (await self._uow._execute(select(_SOURCE_REVISIONS).where(
            _SOURCE_REVISIONS.c.revision_id == decision.revision_id,
            _SOURCE_REVISIONS.c.source_object_id == decision.source_object_id,
            _SOURCE_REVISIONS.c.workspace_id == decision.workspace_id,
            _SOURCE_REVISIONS.c.environment_id == decision.environment_id,
        ))).mappings().one_or_none()
        if revision_row is None:
            await self._uow._fail(ConstraintConflictError("Admission decision target is invalid."))
        if decision.disposition.value == "admitted":
            if revision_row.kind != RevisionKind.CONTENT.value:
                await self._uow._fail(ConstraintConflictError("Tombstone admission requires deletion-specific checks."))
            provenance_row = (await self._uow._execute(select(_SOURCE_PROVENANCE.c.provenance_id).where(
                _SOURCE_PROVENANCE.c.revision_id == decision.revision_id,
                _SOURCE_PROVENANCE.c.source_object_id == decision.source_object_id,
                _SOURCE_PROVENANCE.c.workspace_id == decision.workspace_id,
                _SOURCE_PROVENANCE.c.environment_id == decision.environment_id,
            ).limit(1))).first()
            if provenance_row is None:
                await self._uow._fail(ConstraintConflictError("Admission provenance is incomplete."))

        existing = (await self._uow._execute(select(_ADMISSION_DECISIONS).where(
            _ADMISSION_DECISIONS.c.admission_decision_id == decision.decision_id,
            *scope_predicates,
        ))).mappings().one_or_none()
        if existing is not None:
            resolved = self._from_row(existing)
            if resolved != decision:
                await self._uow._fail(ConstraintConflictError("Admission decision identity conflict."))
            return resolved

        chain = (await self._uow._execute(select(_ADMISSION_DECISIONS).where(
            _ADMISSION_DECISIONS.c.source_revision_id == decision.revision_id,
            *scope_predicates,
        ))).mappings().all()
        if decision.predecessor_decision_id is None:
            if decision.decision_version != 1 or any(row.predecessor_decision_id is None for row in chain):
                await self._uow._fail(ConstraintConflictError("Admission decision root already exists."))
        else:
            predecessor = next((row for row in chain if row.admission_decision_id == decision.predecessor_decision_id), None)
            if predecessor is None or decision.decision_version != predecessor.decision_version + 1:
                await self._uow._fail(ConstraintConflictError("Admission decision predecessor is invalid."))
            if any(row.predecessor_decision_id == decision.predecessor_decision_id for row in chain):
                await self._uow._fail(ConstraintConflictError("Admission decision successor already exists."))
        if any(row.decision_version == decision.decision_version for row in chain):
            await self._uow._fail(ConstraintConflictError("Admission decision version already exists."))

        values = decision.model_dump(mode="python")
        values["admission_decision_id"] = values.pop("decision_id")
        values["source_revision_id"] = values.pop("revision_id")
        values["disposition"] = decision.disposition.value
        values["authority_reference"] = decision.authority_reference.model_dump(mode="json")
        await self._uow._execute(postgresql_insert(_ADMISSION_DECISIONS).values(**values))
        return decision

    async def resolve(self, decision_id: UUID) -> AdmissionDecision | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (await self._uow._execute(select(_ADMISSION_DECISIONS).where(
            _ADMISSION_DECISIONS.c.admission_decision_id == decision_id,
            _ADMISSION_DECISIONS.c.workspace_id == scope.workspace_id,
            _ADMISSION_DECISIONS.c.environment_id == scope.environment_id,
        ))).mappings().one_or_none()
        return self._from_row(row) if row is not None else None

    async def resolve_effective(self, revision_id: UUID) -> AdmissionDecision | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (await self._uow._execute(select(_ADMISSION_DECISIONS).where(
            _ADMISSION_DECISIONS.c.source_revision_id == revision_id,
            _ADMISSION_DECISIONS.c.workspace_id == scope.workspace_id,
            _ADMISSION_DECISIONS.c.environment_id == scope.environment_id,
        ).order_by(_ADMISSION_DECISIONS.c.decision_version.desc()).limit(1))).mappings().one_or_none()
        return self._from_row(row) if row is not None else None


class _PostgreSQLAccessPolicyRepository:
    """Atomic policy-history and current-pointer repository."""

    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow
        self._activation_claims: dict[UUID, OwnedIdempotencyClaim] = {}

    def _require_scope(self, policy: AccessPolicyVersion) -> None:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id != policy.workspace_id
            or scope.environment_id != policy.environment_id
        ):
            raise InvalidPersistenceContextError("Persistence context is invalid.")

    @staticmethod
    def _current(row: Mapping[str, Any]) -> CurrentAccessPolicy:
        return CurrentAccessPolicy.model_validate(dict(row))

    @staticmethod
    def _activation(row: Mapping[str, Any]) -> PolicyActivation:
        return PolicyActivation.model_validate(dict(row))

    async def resolve_current(
        self,
        source_object_id: UUID,
        *,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> CurrentAccessPolicy | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (
            (
                await self._uow._execute(
                    select(_CURRENT_ACCESS_POLICIES).where(
                        _CURRENT_ACCESS_POLICIES.c.workspace_id == scope.workspace_id,
                        _CURRENT_ACCESS_POLICIES.c.environment_id
                        == scope.environment_id,
                        _CURRENT_ACCESS_POLICIES.c.source_object_id == source_object_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._current(row) if row is not None else None

    async def resolve_policy(
        self,
        policy_version_id: UUID,
        *,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> AccessPolicyVersion | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (
            (
                await self._uow._execute(
                    select(_ACCESS_POLICY_VERSIONS).where(
                        _ACCESS_POLICY_VERSIONS.c.access_policy_version_id
                        == policy_version_id,
                        _ACCESS_POLICY_VERSIONS.c.workspace_id == scope.workspace_id,
                        _ACCESS_POLICY_VERSIONS.c.environment_id
                        == scope.environment_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        grant_rows = (
            (
                await self._uow._execute(
                    select(_ACCESS_POLICY_GRANTS).where(
                        _ACCESS_POLICY_GRANTS.c.access_policy_version_id
                        == policy_version_id,
                        _ACCESS_POLICY_GRANTS.c.workspace_id == scope.workspace_id,
                        _ACCESS_POLICY_GRANTS.c.environment_id == scope.environment_id,
                        _ACCESS_POLICY_GRANTS.c.source_object_id
                        == row.source_object_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        grants = tuple(
            AccessGrant.model_validate(
                {
                    key: value
                    for key, value in dict(grant_row).items()
                    if key
                    not in {
                        "workspace_id",
                        "environment_id",
                        "source_object_id",
                        "access_policy_version_id",
                    }
                }
            )
            for grant_row in grant_rows
        )
        return AccessPolicyVersion.model_validate({**dict(row), "grants": grants})

    async def resolve_activation_result(
        self, command: AccessPolicyActivationCommand
    ) -> AccessPolicyActivationResult | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        digest = command.expected_digest(self._uow._operation_snapshot())
        claim = await self._uow.idempotency.claim(
            operation_schema_version=1,
            key=command.idempotency_key,
            digest=digest,
        )
        if isinstance(claim, OwnedIdempotencyClaim):
            self._activation_claims[command.activation_id] = claim
            return None
        if (
            claim.result.result_type != "policy_activation"
            or claim.result.schema_version != 1
        ):
            raise ConstraintConflictError("Policy activation replay is unavailable.")
        row = (
            await self._uow._execute(
                select(_POLICY_ACTIVATIONS).where(
                    _POLICY_ACTIVATIONS.c.workspace_id == scope.workspace_id,
                    _POLICY_ACTIVATIONS.c.environment_id == scope.environment_id,
                    _POLICY_ACTIVATIONS.c.policy_activation_id
                    == claim.result.result_id,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            raise ConstraintConflictError("Policy activation replay is unavailable.")
        activation = self._activation(row)
        current = CurrentAccessPolicy(
            workspace_id=activation.workspace_id,
            environment_id=activation.environment_id,
            source_object_id=activation.source_object_id,
            access_policy_version_id=activation.new_policy_version_id,
            policy_activation_id=activation.policy_activation_id,
            policy_generation=activation.policy_generation,
            cas_version=activation.policy_generation,
        )
        return AccessPolicyActivationResult(
            activation=activation,
            current=current,
            outbox_event_id=activation.outbox_event_id,
            replay=True,
        )

    async def _persist_policy(self, policy: AccessPolicyVersion) -> None:
        self._require_scope(policy)
        persisted = await self.resolve_policy(policy.access_policy_version_id)
        if persisted is not None:
            if persisted != policy:
                raise ConstraintConflictError("Policy identity conflicts.")
            return
        values = policy.model_dump(mode="python", exclude={"grants"})
        values["origin_kind"] = policy.origin_kind.value
        await self._uow._execute(insert(_ACCESS_POLICY_VERSIONS).values(**values))
        for grant in policy.grants:
            grant_values = grant.model_dump(mode="python")
            grant_values.update(
                workspace_id=policy.workspace_id,
                environment_id=policy.environment_id,
                source_object_id=policy.source_object_id,
                access_policy_version_id=policy.access_policy_version_id,
                principal_kind=grant.principal_kind.value,
                permission=grant.permission.value,
            )
            await self._uow._execute(
                insert(_ACCESS_POLICY_GRANTS).values(**grant_values)
            )

    async def persist_proposal(
        self, policy: AccessPolicyVersion, proposal: PolicyProposal
    ) -> PolicyProposal:
        self._uow._guard_active()
        await self._persist_policy(policy)
        existing = await self.resolve_proposal(proposal.policy_proposal_id)
        if existing is not None:
            if existing != proposal:
                raise ConstraintConflictError("Proposal identity conflicts.")
            return existing
        values = proposal.model_dump(mode="python")
        values["benefit_classification"] = proposal.benefit_classification.value
        await self._uow._execute(insert(_POLICY_PROPOSALS).values(**values))
        return proposal

    async def resolve_proposal(self, proposal_id: UUID) -> PolicyProposal | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        row = (
            await self._uow._execute(
                select(_POLICY_PROPOSALS).where(
                    _POLICY_PROPOSALS.c.workspace_id == scope.workspace_id,
                    _POLICY_PROPOSALS.c.environment_id == scope.environment_id,
                    _POLICY_PROPOSALS.c.policy_proposal_id == proposal_id,
                )
            )
        ).mappings().one_or_none()
        return PolicyProposal.model_validate(dict(row)) if row is not None else None

    async def persist_approval(self, approval: PolicyApproval) -> PolicyApproval:
        self._uow._guard_active()
        proposal = await self.resolve_proposal(approval.policy_proposal_id)
        if proposal is None:
            raise ConstraintConflictError("Approval proposal is unavailable.")
        scope = self._uow.scope
        assert isinstance(scope, EnvironmentScope)
        existing_row = (
            await self._uow._execute(
                select(_POLICY_APPROVALS).where(
                    _POLICY_APPROVALS.c.workspace_id == scope.workspace_id,
                    _POLICY_APPROVALS.c.environment_id == scope.environment_id,
                    _POLICY_APPROVALS.c.policy_approval_id
                    == approval.policy_approval_id,
                )
            )
        ).mappings().one_or_none()
        if existing_row is not None:
            existing_values = dict(existing_row)
            existing_values.pop("source_object_id")
            existing = PolicyApproval.model_validate(existing_values)
            if existing != approval:
                raise ConstraintConflictError("Approval identity conflicts.")
            return existing
        values = approval.model_dump(mode="python")
        values["source_object_id"] = proposal.source_object_id
        values["decision"] = approval.decision.value
        await self._uow._execute(insert(_POLICY_APPROVALS).values(**values))
        return approval

    async def activate(
        self,
        command: AccessPolicyActivationCommand,
        activation: PolicyActivationDraft,
    ) -> AccessPolicyActivationResult:
        self._uow._guard_active()
        policy = command.policy
        self._require_scope(policy)
        claim = self._activation_claims.get(command.activation_id)
        if claim is None:
            replay = await self.resolve_activation_result(command)
            if replay is not None:
                return replay
            claim = self._activation_claims.get(command.activation_id)
        if claim is None:
            raise ConstraintConflictError("Policy activation claim is unavailable.")
        completed_activation = activation.finalize(claim.receipt_id)
        if command.audit_event is None or command.outbox_intent is None:
            raise ConstraintConflictError(
                "Policy activation requires atomic audit and outbox evidence."
            )
        current_row = (
            await self._uow._execute(
                select(_CURRENT_ACCESS_POLICIES)
                .where(
                    _CURRENT_ACCESS_POLICIES.c.workspace_id == policy.workspace_id,
                    _CURRENT_ACCESS_POLICIES.c.environment_id == policy.environment_id,
                    _CURRENT_ACCESS_POLICIES.c.source_object_id
                    == policy.source_object_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if current_row is None:
            valid = (
                command.expected_policy_version_id is None
                and command.expected_policy_generation == 0
                and command.expected_cas_version == 0
            )
            cas_version = 1
        else:
            current = self._current(current_row)
            valid = (
                current.access_policy_version_id
                == command.expected_policy_version_id
                and current.policy_generation == command.expected_policy_generation
                and current.cas_version == command.expected_cas_version
            )
            cas_version = current.cas_version + 1
        if not valid:
            raise StalePolicyBaselineError("The current policy baseline changed.")

        audit_event_id = await self._uow.audit.append(command.audit_event)
        outbox_event_id = await self._uow.outbox.append(command.outbox_intent)
        if (
            audit_event_id != command.audit_event_id
            or outbox_event_id != command.outbox_event_id
        ):
            raise ConstraintConflictError(
                "Policy activation evidence identity conflicts."
            )

        await self._persist_policy(policy)

        proposal = command.proposal
        if proposal is not None:
            await self.persist_proposal(policy, proposal)
        approval = command.approval
        if approval is not None:
            await self.persist_approval(approval)
        activation_values = completed_activation.model_dump(mode="python")
        await self._uow._execute(
            insert(_POLICY_ACTIVATIONS).values(**activation_values)
        )
        current = CurrentAccessPolicy(
            workspace_id=policy.workspace_id,
            environment_id=policy.environment_id,
            source_object_id=policy.source_object_id,
            access_policy_version_id=policy.access_policy_version_id,
            policy_activation_id=completed_activation.policy_activation_id,
            policy_generation=completed_activation.policy_generation,
            cas_version=cas_version,
        )
        if current_row is None:
            await self._uow._execute(
                insert(_CURRENT_ACCESS_POLICIES).values(
                    **current.model_dump(mode="python")
                )
            )
        else:
            result = await self._uow._execute(
                update(_CURRENT_ACCESS_POLICIES)
                .where(
                    _CURRENT_ACCESS_POLICIES.c.workspace_id == policy.workspace_id,
                    _CURRENT_ACCESS_POLICIES.c.environment_id == policy.environment_id,
                    _CURRENT_ACCESS_POLICIES.c.source_object_id
                    == policy.source_object_id,
                    _CURRENT_ACCESS_POLICIES.c.cas_version
                    == command.expected_cas_version,
                )
                .values(**current.model_dump(mode="python"))
            )
            if result.rowcount != 1:
                raise StalePolicyBaselineError("The current policy baseline changed.")
        activation_result = AccessPolicyActivationResult(
            activation=completed_activation,
            current=current,
            outbox_event_id=command.outbox_event_id,
        )
        await self._uow.idempotency.complete(
            claim,
            OpaqueResultReference(
                result_type="policy_activation",
                result_id=completed_activation.policy_activation_id,
                schema_version=1,
            ),
        )
        return activation_result


class _PostgreSQLIdempotencyRepository:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    async def claim(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> IdempotencyClaimResult:
        self._uow._guard_active()
        operation = self._uow._operation_snapshot()
        if (
            digest.operation != operation
            or digest.operation_schema_version != operation_schema_version
        ):
            await self._uow._fail(
                IdempotencyConflictError(
                    "Idempotency digest does not match the trusted operation."
                )
            )

        scope = self._uow.scope
        environment_id = (
            scope.environment_id if isinstance(scope, EnvironmentScope) else None
        )
        receipt_id = self._uow._receipt_id_factory()
        values = {
            "receipt_id": receipt_id,
            "workspace_id": scope.workspace_id,
            "environment_id": environment_id,
            "operation_name": operation.value,
            "operation_schema_version": operation_schema_version,
            "idempotency_key": key.value,
            "digest_algorithm_version": digest.algorithm_version,
            "command_digest": digest.value,
        }
        uniqueness_columns = [
            _IDEMPOTENCY_RECEIPTS.c.workspace_id,
            _IDEMPOTENCY_RECEIPTS.c.operation_name,
            _IDEMPOTENCY_RECEIPTS.c.operation_schema_version,
            _IDEMPOTENCY_RECEIPTS.c.idempotency_key,
        ]
        if environment_id is None:
            conflict_columns = uniqueness_columns
            conflict_predicate = _IDEMPOTENCY_RECEIPTS.c.environment_id.is_(None)
        else:
            conflict_columns = [
                _IDEMPOTENCY_RECEIPTS.c.workspace_id,
                _IDEMPOTENCY_RECEIPTS.c.environment_id,
                *uniqueness_columns[1:],
            ]
            conflict_predicate = _IDEMPOTENCY_RECEIPTS.c.environment_id.is_not(None)
        inserted = await self._uow._execute(
            postgresql_insert(_IDEMPOTENCY_RECEIPTS)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=conflict_columns,
                index_where=conflict_predicate,
            )
            .returning(_IDEMPOTENCY_RECEIPTS.c.receipt_id)
        )
        owned_receipt_id = inserted.scalar_one_or_none()
        if owned_receipt_id is not None:
            claim = OwnedIdempotencyClaim(
                kind="owned",
                receipt_id=owned_receipt_id,
                operation=operation,
                operation_schema_version=operation_schema_version,
                key=key,
                digest=digest,
            )
            self._uow._owned_claims[owned_receipt_id] = claim
            return claim

        existing = await self._uow._execute(
            select(
                _IDEMPOTENCY_RECEIPTS.c.receipt_id,
                _IDEMPOTENCY_RECEIPTS.c.digest_algorithm_version,
                _IDEMPOTENCY_RECEIPTS.c.command_digest,
                _IDEMPOTENCY_RECEIPTS.c.result_type,
                _IDEMPOTENCY_RECEIPTS.c.result_id,
                _IDEMPOTENCY_RECEIPTS.c.result_schema_version,
            ).where(*self._key_predicates(operation, operation_schema_version, key))
        )
        row = existing.mappings().one_or_none()
        if row is None:
            await self._uow._fail(idempotency_conflict())
        if (
            row.digest_algorithm_version != digest.algorithm_version
            or row.command_digest != digest.value
        ):
            await self._uow._fail(idempotency_conflict())
        if (
            row.result_type is None
            or row.result_id is None
            or row.result_schema_version is None
        ):
            await self._uow._fail(
                IdempotencyConflictError("Idempotency key is already claimed.")
            )
        return IdempotencyReplay(
            kind="replay",
            receipt_id=row.receipt_id,
            operation=operation,
            operation_schema_version=operation_schema_version,
            key=key,
            result=OpaqueResultReference(
                result_type=row.result_type,
                result_id=row.result_id,
                schema_version=row.result_schema_version,
            ),
        )

    async def complete(
        self,
        claim: OwnedIdempotencyClaim,
        result: OpaqueResultReference,
    ) -> None:
        self._uow._guard_active()
        if not isinstance(claim, OwnedIdempotencyClaim):
            await self._uow._fail(
                IdempotencyConflictError(
                    "Only an owned idempotency claim can complete."
                )
            )
        owned = self._uow._owned_claims.get(claim.receipt_id)
        if owned != claim or claim.operation != self._uow._operation_snapshot():
            await self._uow._fail(
                IdempotencyConflictError("Idempotency claim cannot be completed.")
            )
        if (
            claim.digest.operation != claim.operation
            or claim.digest.operation_schema_version != claim.operation_schema_version
        ):
            await self._uow._fail(
                IdempotencyConflictError("Idempotency claim cannot be completed.")
            )
        completed = await self._uow._execute(
            update(_IDEMPOTENCY_RECEIPTS)
            .where(
                _IDEMPOTENCY_RECEIPTS.c.receipt_id == claim.receipt_id,
                _IDEMPOTENCY_RECEIPTS.c.result_id.is_(None),
                *self._key_predicates(
                    claim.operation,
                    claim.operation_schema_version,
                    claim.key,
                ),
                _IDEMPOTENCY_RECEIPTS.c.digest_algorithm_version
                == claim.digest.algorithm_version,
                _IDEMPOTENCY_RECEIPTS.c.command_digest == claim.digest.value,
            )
            .values(
                result_type=result.result_type,
                result_id=result.result_id,
                result_schema_version=result.schema_version,
            )
            .returning(_IDEMPOTENCY_RECEIPTS.c.receipt_id)
        )
        if completed.scalar_one_or_none() is None:
            await self._uow._fail(
                IdempotencyConflictError("Idempotency claim cannot be completed.")
            )
        del self._uow._owned_claims[claim.receipt_id]

    def _key_predicates(
        self,
        operation: PersistenceOperation,
        operation_schema_version: int,
        key: IdempotencyKey,
    ) -> tuple[object, ...]:
        scope = self._uow.scope
        environment_predicate = (
            _IDEMPOTENCY_RECEIPTS.c.environment_id == scope.environment_id
            if isinstance(scope, EnvironmentScope)
            else _IDEMPOTENCY_RECEIPTS.c.environment_id.is_(None)
        )
        return (
            _IDEMPOTENCY_RECEIPTS.c.workspace_id == scope.workspace_id,
            environment_predicate,
            _IDEMPOTENCY_RECEIPTS.c.operation_name == operation.value,
            _IDEMPOTENCY_RECEIPTS.c.operation_schema_version
            == operation_schema_version,
            _IDEMPOTENCY_RECEIPTS.c.idempotency_key == key.value,
        )


class _PostgreSQLOutboxWriter:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    async def append(self, intent: OutboxIntent) -> UUID:
        self._uow._guard_active()
        try:
            intent = validate_outbox_intent_for_context(
                intent,
                registry=self._uow._outbox_events,
                scope=self._uow.scope,
                trace_id=self._uow._trace_id_snapshot(),
            )
        except BaseException as error:
            await self._uow._fail(error)
        aggregate = intent.aggregate
        values = {
            "workspace_id": intent.workspace_id,
            "environment_id": intent.environment_id,
            "event_type": intent.event_type,
            "event_schema_version": intent.schema_version,
            "aggregate_type": aggregate.object_type if aggregate else None,
            "aggregate_id": aggregate.object_id if aggregate else None,
            "aggregate_schema_version": aggregate.schema_version if aggregate else None,
            "producer_deduplication_id": intent.producer_deduplication_id,
            "payload": intent.payload_json(),
            "trace_id": intent.trace_id,
            "correlation_id": intent.correlation_id,
            "causation_id": intent.causation_id,
        }
        event_id = intent.event_id
        if event_id is None:
            generated = await self._uow._execute(select(func.gen_random_uuid()))
            event_id = generated.scalar_one()
        values["event_id"] = event_id
        await self._uow._execute(insert(_OUTBOX_INTENTS).values(**values).inline())
        return event_id


class _PostgreSQLAuditWriter:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    async def append(self, event: AuditEvent) -> UUID:
        self._uow._guard_active()
        try:
            canonical = validate_audit_event_for_context(
                event,
                registry=self._uow._audit_events,
                context=self._uow._context_snapshot_active(),
            )
        except BaseException as error:
            await self._uow._fail(error)

        duplicate = await self._uow._find_audit_by_producer(canonical)
        if duplicate is not None:
            if same_logical_audit_event(duplicate, canonical):
                assert duplicate.audit_event_id is not None
                return duplicate.audit_event_id
            await self._uow._fail(
                AuditConflictError(
                    "Audit producer identity conflicts with an event."
                )
            )

        audit_event_id = canonical.audit_event_id
        if audit_event_id is None:
            await self._uow._run_transaction_probe(
                PostgreSQLTransactionStage.BEFORE_AUDIT_IDENTITY
            )
            generated = await self._uow._execute(select(func.gen_random_uuid()))
            audit_event_id = generated.scalar_one()
        if await self._uow._find_audit_by_id(audit_event_id) is not None:
            await self._uow._fail(
                AuditConflictError("Audit Event identity already exists.")
            )
        self._uow._pending_audit_events[audit_event_id] = (
            canonical.detached_snapshot(
                update={"audit_event_id": audit_event_id}
            )
        )
        return audit_event_id


class _PostgreSQLProjectionSnapshotMixin:
    """5B persistence operations shared by the PostgreSQL projection adapter."""

    async def record_validation(self, command: ProjectionValidationCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            row = (await self._uow._execute(select(_PROJECTION_VALIDATIONS).where(
                _PROJECTION_VALIDATIONS.c.projection_validation_id == claim.result.result_id,
            ))).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Projection validation replay is unavailable."))
            return ProjectionMutationResult(self._validation(row), True, claim, claim.result)
        validation = command.validation
        self._scoped(validation)
        await self._open_run(validation.projection_run_id, validation.projection_candidate_id)
        values = validation.model_dump(mode="python")
        values["decision"] = validation.decision.value
        values["metrics_payload"] = validation.model_dump(mode="json")["metrics_payload"]
        await self._uow._execute(postgresql_insert(_PROJECTION_VALIDATIONS).values(**values))
        reference = OpaqueResultReference("projection_validation", validation.projection_validation_id, 1)
        await self._uow.idempotency.complete(claim, reference)
        return ProjectionMutationResult(validation, False, claim, reference)

    async def list_validations(self, projection_run_id: UUID) -> tuple[ProjectionValidation, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (await self._uow._execute(select(_PROJECTION_VALIDATIONS).where(
            _PROJECTION_VALIDATIONS.c.projection_run_id == projection_run_id,
            _PROJECTION_VALIDATIONS.c.workspace_id == scope.workspace_id,
            _PROJECTION_VALIDATIONS.c.environment_id == scope.environment_id,
        ).order_by(_PROJECTION_VALIDATIONS.c.projection_validation_id))).mappings().all()
        return tuple(self._validation(row) for row in rows)

    async def assemble_snapshot(self, command: ProjectionSnapshotCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            row = (await self._uow._execute(select(_PROJECTION_SNAPSHOTS).where(
                _PROJECTION_SNAPSHOTS.c.projection_snapshot_id == claim.result.result_id,
            ))).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Projection snapshot replay is unavailable."))
            return ProjectionMutationResult(self._snapshot(row), True, claim, claim.result)
        snapshot = command.snapshot
        self._scoped(snapshot)
        run = await self._resolve_run(snapshot.projection_run_id, for_update=True)
        if (
            run is None
            or run.projection_candidate_id != snapshot.projection_candidate_id
            or run.candidate_state is not ProjectionCandidateState.SEALED
            or run.candidate_evidence_digest != snapshot.candidate_evidence_digest
            or run.projection_kind != snapshot.projection_kind
            or run.projection_config_version_id != snapshot.projection_config_version_id
            or run.canonical_boundary_id != snapshot.canonical_boundary_id
            or snapshot.snapshot_state is not ProjectionSnapshotState.ASSEMBLING
        ):
            await self._uow._fail(ConstraintConflictError("Projection snapshot candidate lineage is invalid."))
        config = await self.resolve_config_version(run.projection_config_version_id)
        if config is None:
            await self._uow._fail(ConstraintConflictError("Projection config version is unavailable."))
        if snapshot.predecessor_snapshot_id is not None:
            predecessor = await self.resolve_snapshot(snapshot.predecessor_snapshot_id)
            if predecessor is None or predecessor.projection_kind != snapshot.projection_kind or predecessor.snapshot_state is not ProjectionSnapshotState.FINALIZED:
                await self._uow._fail(ConstraintConflictError("Projection snapshot predecessor is invalid."))
        boundary_rows = (await self._uow._execute(select(_CANONICAL_BOUNDARY_MEMBERS).where(
            _CANONICAL_BOUNDARY_MEMBERS.c.boundary_id == snapshot.canonical_boundary_id,
            _CANONICAL_BOUNDARY_MEMBERS.c.workspace_id == snapshot.workspace_id,
            _CANONICAL_BOUNDARY_MEMBERS.c.environment_id == snapshot.environment_id,
        ))).mappings().all()
        members = tuple(command.members)
        by_source = {row["source_object_id"]: row for row in boundary_rows}
        if len(members) != len(boundary_rows) or {item.source_object_id for item in members} != set(by_source):
            await self._uow._fail(ConstraintConflictError("Projection snapshot membership is incomplete."))
        for member in members:
            self._scoped(member)
            boundary_member = by_source.get(member.source_object_id)
            if boundary_member is None or member.canonical_boundary_member_id != boundary_member["source_object_id"] or member.source_revision_id != boundary_member["source_revision_id"]:
                await self._uow._fail(ConstraintConflictError("Projection snapshot member lineage is invalid."))
            if ((boundary_member["revision_kind"] == "tombstone" and member.disposition is not ProjectionSnapshotMemberDisposition.TOMBSTONED) or (boundary_member["revision_kind"] == "content" and member.disposition is ProjectionSnapshotMemberDisposition.TOMBSTONED)):
                await self._uow._fail(ConstraintConflictError("Projection snapshot disposition does not match the boundary revision."))
            if member.disposition is ProjectionSnapshotMemberDisposition.INCLUDED:
                artifact = (await self._uow._execute(select(
                    _PROJECTION_RECEIPTS.c.command_identity.label("receipt_command_identity"),
                    _PROJECTION_RECEIPTS.c.command_digest.label("receipt_command_digest"),
                    _PROJECTION_PARTITIONS.c.expected_command_identity.label("expected_command_identity"),
                    _PROJECTION_PARTITIONS.c.expected_command_digest.label("expected_command_digest"),
                ).select_from(
                    _PROJECTION_RECEIPTS.join(_PROJECTION_PARTITIONS, _PROJECTION_PARTITIONS.c.projection_partition_id == _PROJECTION_RECEIPTS.c.projection_partition_id)
                ).where(
                    _PROJECTION_RECEIPTS.c.projection_receipt_id == member.projection_receipt_id,
                    _PROJECTION_PARTITIONS.c.projection_partition_id == member.projection_partition_id,
                    _PROJECTION_RECEIPTS.c.workspace_id == member.workspace_id,
                    _PROJECTION_RECEIPTS.c.environment_id == member.environment_id,
                    _PROJECTION_RECEIPTS.c.projection_run_id == run.projection_run_id,
                    _PROJECTION_RECEIPTS.c.projection_candidate_id == run.projection_candidate_id,
                ))).mappings().one_or_none()
                if artifact is None or artifact.receipt_command_identity != artifact.expected_command_identity or artifact.receipt_command_digest != artifact.expected_command_digest:
                    await self._uow._fail(ConstraintConflictError("Projection snapshot artifact evidence is invalid."))
        validations = await self.list_validations(snapshot.projection_run_id)
        partitions = await self.list_partitions(snapshot.projection_run_id)
        receipts = await self.list_receipts(snapshot.projection_run_id)
        references = await self.list_source_references(snapshot.projection_run_id)
        if (
            not validations
            or any(item.decision.value != "passed" for item in validations)
            or not has_complete_physical_validation(
                validations, partitions, receipts, references, run, config
            )
        ):
            await self._uow._fail(ConstraintConflictError("Projection snapshot validation evidence is incomplete."))
        if snapshot.member_count != len(members) or snapshot.manifest_digest != projection_snapshot_manifest_digest(snapshot, members):
            await self._uow._fail(ConstraintConflictError("Projection snapshot manifest is invalid."))
        values = snapshot.model_dump(mode="python")
        values["snapshot_state"] = ProjectionSnapshotState.ASSEMBLING.value
        await self._uow._execute(postgresql_insert(_PROJECTION_SNAPSHOTS).values(**values))
        for member in members:
            await self._uow._execute(postgresql_insert(_PROJECTION_SNAPSHOT_MEMBERS).values(**member.model_dump(mode="python")))
        await self._uow._execute(update(_PROJECTION_SNAPSHOTS).where(
            _PROJECTION_SNAPSHOTS.c.projection_snapshot_id == snapshot.projection_snapshot_id,
        ).values(snapshot_state=ProjectionSnapshotState.FINALIZED.value))
        value = snapshot.model_copy(update={"snapshot_state": ProjectionSnapshotState.FINALIZED})
        reference = OpaqueResultReference("projection_snapshot", value.projection_snapshot_id, 1)
        await self._uow.idempotency.complete(claim, reference)
        return ProjectionMutationResult(value, False, claim, reference)

    async def resolve_snapshot(self, projection_snapshot_id: UUID) -> ProjectionSnapshot | None:
        self._uow._guard_active()
        scope = self._scope()
        row = (await self._uow._execute(select(_PROJECTION_SNAPSHOTS).where(
            _PROJECTION_SNAPSHOTS.c.projection_snapshot_id == projection_snapshot_id,
            _PROJECTION_SNAPSHOTS.c.workspace_id == scope.workspace_id,
            _PROJECTION_SNAPSHOTS.c.environment_id == scope.environment_id,
        ))).mappings().one_or_none()
        return self._snapshot(row) if row is not None else None

    async def list_snapshot_members(self, projection_snapshot_id: UUID) -> tuple[ProjectionSnapshotMember, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (await self._uow._execute(select(_PROJECTION_SNAPSHOT_MEMBERS).where(
            _PROJECTION_SNAPSHOT_MEMBERS.c.projection_snapshot_id == projection_snapshot_id,
            _PROJECTION_SNAPSHOT_MEMBERS.c.workspace_id == scope.workspace_id,
            _PROJECTION_SNAPSHOT_MEMBERS.c.environment_id == scope.environment_id,
        ).order_by(_PROJECTION_SNAPSHOT_MEMBERS.c.canonical_boundary_member_id))).mappings().all()
        return tuple(self._snapshot_member(row) for row in rows)

    async def record_reconciliation(self, command: ProjectionReconciliationCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            row = (await self._uow._execute(select(_PROJECTION_RECONCILIATION_RESULTS).where(
                _PROJECTION_RECONCILIATION_RESULTS.c.reconciliation_result_id == claim.result.result_id,
            ))).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Reconciliation replay is unavailable."))
            return ProjectionMutationResult(self._reconciliation(row), True, claim, claim.result)
        value = command.result
        self._scoped(value)
        snapshot = await self.resolve_snapshot(value.projection_snapshot_id)
        if snapshot is None or snapshot.snapshot_state is not ProjectionSnapshotState.FINALIZED:
            await self._uow._fail(ConstraintConflictError("Reconciliation requires a finalized snapshot."))
        values = value.model_dump(mode="python")
        values["outcome"] = value.outcome.value
        values["findings_payload"] = value.model_dump(mode="json")["findings_payload"]
        await self._uow._execute(postgresql_insert(_PROJECTION_RECONCILIATION_RESULTS).values(**values))
        reference = OpaqueResultReference("projection_reconciliation", value.reconciliation_result_id, 1)
        await self._uow.idempotency.complete(claim, reference)
        return ProjectionMutationResult(value, False, claim, reference)

    async def list_reconciliation_results(self, projection_snapshot_id: UUID) -> tuple[ProjectionReconciliationResult, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (await self._uow._execute(select(_PROJECTION_RECONCILIATION_RESULTS).where(
            _PROJECTION_RECONCILIATION_RESULTS.c.projection_snapshot_id == projection_snapshot_id,
            _PROJECTION_RECONCILIATION_RESULTS.c.workspace_id == scope.workspace_id,
            _PROJECTION_RECONCILIATION_RESULTS.c.environment_id == scope.environment_id,
        ).order_by(_PROJECTION_RECONCILIATION_RESULTS.c.checked_at))).mappings().all()
        return tuple(self._reconciliation(row) for row in rows)

    async def activate(
        self, command: ProjectionActivationCommand
    ) -> ProjectionActivationResult:
        """Publish a finalized snapshot under the counter-first PostgreSQL CAS."""

        self._uow._guard_active()
        if command.digest != command.expected_digest():
            await self._uow._fail(
                IdempotencyConflictError("Projection activation command digest is invalid.")
            )
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            row = (
                await self._uow._execute(
                    select(_PROJECTION_ACTIVATIONS).where(
                        _PROJECTION_ACTIVATIONS.c.projection_activation_id
                        == claim.result.result_id,
                        _PROJECTION_ACTIVATIONS.c.workspace_id == self._scope().workspace_id,
                        _PROJECTION_ACTIVATIONS.c.environment_id == self._scope().environment_id,
                    )
                )
            ).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Projection activation replay is unavailable."))
            activation = self._activation(row)
            drift_count = (
                await self._uow._execute(
                    select(func.count())
                    .select_from(_PROJECTION_DRIFTS)
                    .where(
                        _PROJECTION_DRIFTS.c.projection_activation_id
                        == activation.projection_activation_id,
                        _PROJECTION_DRIFTS.c.workspace_id == activation.workspace_id,
                        _PROJECTION_DRIFTS.c.environment_id == activation.environment_id,
                    )
                )
            ).scalar_one()
            return ProjectionActivationReplay(
                activation=activation,
                drift_count=int(drift_count),
                result_reference=claim.result,
            )

        scope = self._scope()
        await self._uow._execute(
            postgresql_insert(_CANONICAL_GENERATION_COUNTERS)
            .values(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                current_generation=0,
                updated_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing()
        )
        generation_row = (
            await self._uow._execute(
                select(_CANONICAL_GENERATION_COUNTERS)
                .where(
                    _CANONICAL_GENERATION_COUNTERS.c.workspace_id == scope.workspace_id,
                    _CANONICAL_GENERATION_COUNTERS.c.environment_id == scope.environment_id,
                )
                .with_for_update()
            )
        ).mappings().one()
        pointer_row = (
            await self._uow._execute(
                select(_ACTIVE_PROJECTIONS)
                .where(
                    _ACTIVE_PROJECTIONS.c.workspace_id == scope.workspace_id,
                    _ACTIVE_PROJECTIONS.c.environment_id == scope.environment_id,
                    _ACTIVE_PROJECTIONS.c.projection_kind == command.projection_kind,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        pointer = self._pointer(pointer_row) if pointer_row is not None else None

        snapshot_row = (
            await self._uow._execute(
                select(_PROJECTION_SNAPSHOTS)
                .where(
                    _PROJECTION_SNAPSHOTS.c.projection_snapshot_id == command.projection_snapshot_id,
                    _PROJECTION_SNAPSHOTS.c.workspace_id == scope.workspace_id,
                    _PROJECTION_SNAPSHOTS.c.environment_id == scope.environment_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if snapshot_row is None:
            await self._uow._fail(ConstraintConflictError("Projection snapshot is unavailable."))
        snapshot = self._snapshot(snapshot_row)
        if snapshot.snapshot_state is not ProjectionSnapshotState.FINALIZED:
            await self._uow._fail(ConstraintConflictError("Projection snapshot is not finalized."))
        if snapshot.projection_kind != command.projection_kind:
            await self._uow._fail(ConstraintConflictError("Projection activation kind is invalid."))
        boundary_row = (
            await self._uow._execute(
                select(_CANONICAL_BOUNDARIES).where(
                    _CANONICAL_BOUNDARIES.c.boundary_id == snapshot.canonical_boundary_id,
                    _CANONICAL_BOUNDARIES.c.workspace_id == scope.workspace_id,
                    _CANONICAL_BOUNDARIES.c.environment_id == scope.environment_id,
                )
            )
        ).mappings().one_or_none()
        if boundary_row is None:
            await self._uow._fail(ConstraintConflictError("Projection boundary is unavailable."))
        boundary = CanonicalBoundary.model_validate(dict(boundary_row))
        if boundary.scope_version != "r1":
            await self._uow._fail(ConstraintConflictError("Projection boundary scope is unsupported."))
        run_row = (
            await self._uow._execute(
                select(_PROJECTION_RUNS)
                .where(
                    _PROJECTION_RUNS.c.projection_run_id == snapshot.projection_run_id,
                    _PROJECTION_RUNS.c.workspace_id == scope.workspace_id,
                    _PROJECTION_RUNS.c.environment_id == scope.environment_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            run_row is None
            or run_row["projection_candidate_id"] != snapshot.projection_candidate_id
            or run_row["candidate_state"] != ProjectionCandidateState.SEALED.value
            or run_row["candidate_evidence_digest"] != snapshot.candidate_evidence_digest
            or run_row["projection_kind"] != snapshot.projection_kind
            or run_row["projection_config_version_id"] != snapshot.projection_config_version_id
            or run_row["canonical_boundary_id"] != snapshot.canonical_boundary_id
        ):
            await self._uow._fail(ConstraintConflictError("Projection snapshot run lineage is invalid."))
        if command.activation_kind is ProjectionActivationKind.ROLLBACK and run_row["operation"] != "rollback":
            await self._uow._fail(ConstraintConflictError("Rollback requires a rollback projection run."))
        if command.activation_kind is ProjectionActivationKind.PUBLISH and run_row["operation"] == "rollback":
            await self._uow._fail(ConstraintConflictError("Publish cannot use a rollback projection run."))
        if pointer is None:
            if any(
                value is not None
                for value in (
                    command.expected_predecessor_snapshot_id,
                    command.expected_predecessor_activation_generation,
                    command.expected_active_pointer_cas_version,
                )
            ) or snapshot.predecessor_snapshot_id is not None:
                await self._uow._fail(OptimisticConflictError("First publication predecessor is not empty."))
            activation_generation, cas_version = 1, 1
        else:
            if (
                command.expected_predecessor_snapshot_id != pointer.projection_snapshot_id
                or command.expected_predecessor_activation_generation != pointer.activation_generation
                or command.expected_active_pointer_cas_version != pointer.cas_version
                or snapshot.predecessor_snapshot_id != pointer.projection_snapshot_id
            ):
                await self._uow._fail(OptimisticConflictError("Active projection predecessor is stale."))
            active_snapshot = await self.resolve_snapshot(pointer.projection_snapshot_id)
            active_boundary_row = (
                await self._uow._execute(
                    select(_CANONICAL_BOUNDARIES).where(
                        _CANONICAL_BOUNDARIES.c.boundary_id == active_snapshot.canonical_boundary_id,
                        _CANONICAL_BOUNDARIES.c.workspace_id == scope.workspace_id,
                        _CANONICAL_BOUNDARIES.c.environment_id == scope.environment_id,
                    )
                )
            ).mappings().one_or_none()
            if active_boundary_row is None or boundary.accepted_through_generation < active_boundary_row["accepted_through_generation"]:
                await self._uow._fail(OptimisticConflictError("Projection boundary regressed."))
            activation_generation, cas_version = pointer.activation_generation + 1, pointer.cas_version + 1

        activation_id = uuid4()
        context = self._uow._context_snapshot_active()
        audit = self._uow._audit_events.build_event(
            audit_event_id=None,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            event_type="canonical.transition",
            schema_version=1,
            payload=CanonicalTransitionAuditPayload(transition="projection.activated"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=datetime.now(timezone.utc),
            outcome=AuditOutcome.COMMITTED,
            reason="projection_activation_committed",
            target=AuditObjectReference(
                object_type="projection_activation", object_id=activation_id, schema_version=1
            ),
            producer_deduplication_id=f"projection-activated:{activation_id}",
        )
        audit_id = await self._uow.audit.append(audit)
        activation = ProjectionActivation(
            projection_activation_id=activation_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            projection_kind=command.projection_kind,
            projection_snapshot_id=snapshot.projection_snapshot_id,
            predecessor_snapshot_id=pointer.projection_snapshot_id if pointer else None,
            activation_generation=activation_generation,
            activation_kind=command.activation_kind,
            command_identity=command.command_identity,
            command_digest=command.digest.value,
            idempotency_receipt_id=claim.receipt_id,
            audit_event_id=audit_id,
            activated_at=datetime.now(timezone.utc),
        )
        activation_values = activation.model_dump(mode="python")
        activation_values["activation_kind"] = activation.activation_kind.value
        await self._uow._execute(insert(_PROJECTION_ACTIVATIONS).values(**activation_values))
        drift_rows = (
            await self._uow._execute(
                select(_CANONICAL_ACCEPTANCES).where(
                    _CANONICAL_ACCEPTANCES.c.workspace_id == scope.workspace_id,
                    _CANONICAL_ACCEPTANCES.c.environment_id == scope.environment_id,
                    _CANONICAL_ACCEPTANCES.c.accepted_generation > boundary.accepted_through_generation,
                    _CANONICAL_ACCEPTANCES.c.accepted_generation <= generation_row["current_generation"],
                ).order_by(_CANONICAL_ACCEPTANCES.c.accepted_generation)
            )
        ).mappings().all()
        drifts: list[ProjectionDrift] = []
        for row in drift_rows:
            drift = ProjectionDrift(
                projection_drift_id=uuid4(),
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                projection_kind=command.projection_kind,
                projection_snapshot_id=snapshot.projection_snapshot_id,
                projection_activation_id=activation_id,
                activation_generation=activation_generation,
                canonical_acceptance_id=row["canonical_acceptance_id"],
                source_object_id=row["source_object_id"],
                source_revision_id=row["source_revision_id"],
                accepted_generation=row["accepted_generation"],
                detected_by=ProjectionDriftDetectedBy.ACTIVATION,
                detected_at=datetime.now(timezone.utc),
            )
            drifts.append(drift)
            drift_values = drift.model_dump(mode="python")
            drift_values["detected_by"] = drift.detected_by.value
            await self._uow._execute(insert(_PROJECTION_DRIFTS).values(**drift_values))
        pointer_values = ActiveProjection(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            projection_kind=command.projection_kind,
            projection_snapshot_id=snapshot.projection_snapshot_id,
            projection_activation_id=activation_id,
            activation_generation=activation_generation,
            cas_version=cas_version,
        ).model_dump(mode="python")
        if pointer is None:
            await self._uow._execute(insert(_ACTIVE_PROJECTIONS).values(**pointer_values))
        else:
            updated = await self._uow._execute(
                update(_ACTIVE_PROJECTIONS)
                .where(
                    _ACTIVE_PROJECTIONS.c.workspace_id == scope.workspace_id,
                    _ACTIVE_PROJECTIONS.c.environment_id == scope.environment_id,
                    _ACTIVE_PROJECTIONS.c.projection_kind == command.projection_kind,
                    _ACTIVE_PROJECTIONS.c.projection_snapshot_id == pointer.projection_snapshot_id,
                    _ACTIVE_PROJECTIONS.c.activation_generation == pointer.activation_generation,
                    _ACTIVE_PROJECTIONS.c.cas_version == pointer.cas_version,
                )
                .values(**pointer_values)
            )
            if updated.rowcount != 1:
                await self._uow._fail(OptimisticConflictError("Active projection CAS failed."))
        await self._uow.outbox.append(
            self._uow._outbox_events.build_intent(
                event_id=uuid4(),
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                event_type=PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
                schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
                payload=ProjectionActivatedOutboxPayload(
                    projection_activation_id=activation_id,
                    projection_snapshot_id=snapshot.projection_snapshot_id,
                    projection_kind=command.projection_kind,
                    activation_generation=activation_generation,
                    activation_kind=command.activation_kind,
                    canonical_boundary_id=boundary.boundary_id,
                    accepted_through_generation=boundary.accepted_through_generation,
                    drift_count=len(drifts),
                    audit_event_id=audit_id,
                ),
                aggregate=OpaqueObjectReference(
                    object_type="projection_activation",
                    object_id=activation_id,
                    schema_version=1,
                ),
                producer_deduplication_id=f"projection-activated:{activation_id}",
                trace_id=context.trace_id,
            )
        )
        for drift in drifts:
            await self._uow.outbox.append(
                self._uow._outbox_events.build_intent(
                    event_id=uuid4(),
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    event_type=PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
                    schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
                    payload=ProjectionCatchUpRequestedOutboxPayload(
                        projection_drift_id=drift.projection_drift_id,
                        projection_activation_id=activation_id,
                        projection_kind=command.projection_kind,
                        activation_generation=activation_generation,
                        canonical_acceptance_id=drift.canonical_acceptance_id,
                        source_object_id=drift.source_object_id,
                        source_revision_id=drift.source_revision_id,
                        accepted_generation=drift.accepted_generation,
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
        result = ProjectionActivationCommitted(
            activation=activation,
            drift_count=len(drifts),
            result_reference=OpaqueResultReference("projection_activation", activation_id, 1),
        )
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def resolve_active_publication(
        self, projection_kind: str
    ) -> ProjectionPublicationResolution:
        self._uow._guard_active()
        scope = self._scope()
        effective_reconciliation = (
            select(*_label_table_columns(_PROJECTION_RECONCILIATION_RESULTS, "reconciliation"))
            .where(
                _PROJECTION_RECONCILIATION_RESULTS.c.workspace_id == scope.workspace_id,
                _PROJECTION_RECONCILIATION_RESULTS.c.environment_id == scope.environment_id,
                _PROJECTION_RECONCILIATION_RESULTS.c.projection_kind == projection_kind,
                _PROJECTION_RECONCILIATION_RESULTS.c.projection_snapshot_id
                == _PROJECTION_SNAPSHOTS.c.projection_snapshot_id,
                _PROJECTION_RECONCILIATION_RESULTS.c.projection_activation_id
                == _PROJECTION_ACTIVATIONS.c.projection_activation_id,
                _PROJECTION_RECONCILIATION_RESULTS.c.activation_generation
                == _ACTIVE_PROJECTIONS.c.activation_generation,
            )
            .order_by(
                _PROJECTION_RECONCILIATION_RESULTS.c.checked_at.desc(),
                _PROJECTION_RECONCILIATION_RESULTS.c.reconciliation_result_id.desc(),
            )
            .limit(1)
            .lateral("effective_reconciliation")
        )
        has_drift = (
            select(1)
            .where(
                _PROJECTION_DRIFTS.c.workspace_id == scope.workspace_id,
                _PROJECTION_DRIFTS.c.environment_id == scope.environment_id,
                _PROJECTION_DRIFTS.c.projection_activation_id
                == _ACTIVE_PROJECTIONS.c.projection_activation_id,
                _PROJECTION_DRIFTS.c.activation_generation
                == _ACTIVE_PROJECTIONS.c.activation_generation,
            )
            .exists()
        )
        publication_query = (
            select(
                *_label_table_columns(_ACTIVE_PROJECTIONS, "pointer"),
                *_label_table_columns(_PROJECTION_ACTIVATIONS, "activation"),
                *_label_table_columns(_PROJECTION_SNAPSHOTS, "snapshot"),
                *_label_table_columns(_CANONICAL_BOUNDARIES, "boundary"),
                *_label_table_columns(_PROJECTION_CONFIGS, "config"),
                *effective_reconciliation.c,
                has_drift.label("has_drift"),
            )
            .select_from(
                _ACTIVE_PROJECTIONS
                .outerjoin(
                    _PROJECTION_ACTIVATIONS,
                    (
                        _PROJECTION_ACTIVATIONS.c.workspace_id
                        == _ACTIVE_PROJECTIONS.c.workspace_id
                    )
                    & (
                        _PROJECTION_ACTIVATIONS.c.environment_id
                        == _ACTIVE_PROJECTIONS.c.environment_id
                    )
                    & (
                        _PROJECTION_ACTIVATIONS.c.projection_kind
                        == _ACTIVE_PROJECTIONS.c.projection_kind
                    )
                    & (
                        _PROJECTION_ACTIVATIONS.c.projection_activation_id
                        == _ACTIVE_PROJECTIONS.c.projection_activation_id
                    )
                    & (
                        _PROJECTION_ACTIVATIONS.c.projection_snapshot_id
                        == _ACTIVE_PROJECTIONS.c.projection_snapshot_id
                    )
                    & (
                        _PROJECTION_ACTIVATIONS.c.activation_generation
                        == _ACTIVE_PROJECTIONS.c.activation_generation
                    ),
                )
                .outerjoin(
                    _PROJECTION_SNAPSHOTS,
                    (
                        _PROJECTION_SNAPSHOTS.c.workspace_id
                        == _ACTIVE_PROJECTIONS.c.workspace_id
                    )
                    & (
                        _PROJECTION_SNAPSHOTS.c.environment_id
                        == _ACTIVE_PROJECTIONS.c.environment_id
                    )
                    & (
                        _PROJECTION_SNAPSHOTS.c.projection_kind
                        == _ACTIVE_PROJECTIONS.c.projection_kind
                    )
                    & (
                        _PROJECTION_SNAPSHOTS.c.projection_snapshot_id
                        == _ACTIVE_PROJECTIONS.c.projection_snapshot_id
                    ),
                )
                .outerjoin(
                    _CANONICAL_BOUNDARIES,
                    (
                        _CANONICAL_BOUNDARIES.c.workspace_id
                        == _PROJECTION_SNAPSHOTS.c.workspace_id
                    )
                    & (
                        _CANONICAL_BOUNDARIES.c.environment_id
                        == _PROJECTION_SNAPSHOTS.c.environment_id
                    )
                    & (
                        _CANONICAL_BOUNDARIES.c.boundary_id
                        == _PROJECTION_SNAPSHOTS.c.canonical_boundary_id
                    ),
                )
                .outerjoin(
                    _PROJECTION_CONFIGS,
                    (
                        _PROJECTION_CONFIGS.c.workspace_id
                        == _PROJECTION_SNAPSHOTS.c.workspace_id
                    )
                    & (
                        _PROJECTION_CONFIGS.c.environment_id
                        == _PROJECTION_SNAPSHOTS.c.environment_id
                    )
                    & (
                        _PROJECTION_CONFIGS.c.projection_config_version_id
                        == _PROJECTION_SNAPSHOTS.c.projection_config_version_id
                    ),
                )
                .outerjoin(effective_reconciliation, true())
            )
            .where(
                _ACTIVE_PROJECTIONS.c.workspace_id == scope.workspace_id,
                _ACTIVE_PROJECTIONS.c.environment_id == scope.environment_id,
                _ACTIVE_PROJECTIONS.c.projection_kind == projection_kind,
            )
        )
        row = (await self._uow._execute(publication_query)).mappings().one_or_none()
        if row is None:
            return UnpublishedActivePublication(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                projection_kind=projection_kind,
            )
        pointer = self._pointer(
            _unlabel_table_row(row, _ACTIVE_PROJECTIONS, "pointer")
        )
        snapshot_row = _unlabel_table_row(row, _PROJECTION_SNAPSHOTS, "snapshot")
        activation_row = _unlabel_table_row(row, _PROJECTION_ACTIVATIONS, "activation")
        boundary_row = _unlabel_table_row(row, _CANONICAL_BOUNDARIES, "boundary")
        config_row = _unlabel_table_row(row, _PROJECTION_CONFIGS, "config")
        snapshot = (
            self._snapshot(snapshot_row)
            if snapshot_row["projection_snapshot_id"] is not None
            else None
        )
        activation = (
            self._activation(activation_row)
            if activation_row["projection_activation_id"] is not None
            else None
        )
        boundary = (
            CanonicalBoundary.model_validate(dict(boundary_row))
            if boundary_row["boundary_id"] is not None
            else None
        )
        config = (
            self._config(config_row)
            if config_row["projection_config_version_id"] is not None
            else None
        )
        if snapshot is None or activation is None or boundary is None or config is None:
            return UnavailableActivePublication(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                projection_kind=projection_kind,
                reason_code="active_publication_integrity_conflict",
            )
        reconciliation_row = _unlabel_table_row(
            row, _PROJECTION_RECONCILIATION_RESULTS, "reconciliation"
        )
        effective = (
            self._reconciliation(reconciliation_row)
            if reconciliation_row["reconciliation_result_id"] is not None
            else None
        )
        return PublishedActivePublication(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            projection_kind=projection_kind,
            projection_snapshot=snapshot,
            projection_activation=activation,
            activation_generation=pointer.activation_generation,
            active_pointer_cas_version=pointer.cas_version,
            canonical_boundary_id=boundary.boundary_id,
            canonical_boundary_manifest_digest=boundary.manifest_digest,
            accepted_through_generation=boundary.accepted_through_generation,
            canonical_boundary_membership_ref=f"canonical-boundary-members:{boundary.boundary_id}",
            as_of=CanonicalAsOf(
                canonical_boundary_id=boundary.boundary_id,
                accepted_through_generation=boundary.accepted_through_generation,
                boundary_time=boundary.boundary_time,
            ),
            projection_config_version=config,
            freshness=(
                ProjectionFreshness.STALE
                if row["has_drift"]
                else ProjectionFreshness.CURRENT
            ),
            health=ProjectionHealth.HEALTHY
            if effective is not None and effective.outcome.value == "healthy"
            else ProjectionHealth.DEGRADED,
            effective_reconciliation=effective,
        )

    async def list_activations(self, projection_kind: str) -> tuple[ProjectionActivation, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (
            await self._uow._execute(
                select(_PROJECTION_ACTIVATIONS)
                .where(
                    _PROJECTION_ACTIVATIONS.c.workspace_id == scope.workspace_id,
                    _PROJECTION_ACTIVATIONS.c.environment_id == scope.environment_id,
                    _PROJECTION_ACTIVATIONS.c.projection_kind == projection_kind,
                )
                .order_by(_PROJECTION_ACTIVATIONS.c.activation_generation)
            )
        ).mappings().all()
        return tuple(self._activation(row) for row in rows)

    async def list_drifts(self, projection_activation_id: UUID) -> tuple[ProjectionDrift, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (
            await self._uow._execute(
                select(_PROJECTION_DRIFTS)
                .where(
                    _PROJECTION_DRIFTS.c.projection_activation_id == projection_activation_id,
                    _PROJECTION_DRIFTS.c.workspace_id == scope.workspace_id,
                    _PROJECTION_DRIFTS.c.environment_id == scope.environment_id,
                )
                .order_by(_PROJECTION_DRIFTS.c.accepted_generation)
            )
        ).mappings().all()
        return tuple(self._drift(row) for row in rows)


class _PostgreSQLProjectionRepository(_PostgreSQLProjectionSnapshotMixin, ProjectionRepository):
    """PostgreSQL-owned ontology/configuration and candidate evidence ledger."""

    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    def _scope(self) -> EnvironmentScope:
        scope = self._uow.scope
        if not isinstance(scope, EnvironmentScope):
            raise ConstraintConflictError("Projection persistence requires an environment scope.")
        return scope

    def _scoped(self, value: object) -> None:
        scope = self._scope()
        if (
            getattr(value, "workspace_id") != scope.workspace_id
            or getattr(value, "environment_id") != scope.environment_id
        ):
            raise ConstraintConflictError("Projection record is outside the tenant scope.")

    @staticmethod
    def _ontology(row: Mapping[str, Any]) -> OntologyVersion:
        return OntologyVersion.model_validate(dict(row))

    @staticmethod
    def _config(row: Mapping[str, Any]) -> ProjectionConfigVersion:
        return ProjectionConfigVersion.model_validate(dict(row))

    @staticmethod
    def _run(row: Mapping[str, Any]) -> ProjectionRun:
        return ProjectionRun.model_validate(dict(row))

    @staticmethod
    def _transition(row: Mapping[str, Any]) -> ProjectionRunTransition:
        return ProjectionRunTransition.model_validate(dict(row))

    @staticmethod
    def _partition(row: Mapping[str, Any]) -> ProjectionPartition:
        return ProjectionPartition.model_validate(dict(row))

    @staticmethod
    def _receipt(row: Mapping[str, Any]) -> ProjectionReceipt:
        return ProjectionReceipt.model_validate(dict(row))

    @staticmethod
    def _reference(row: Mapping[str, Any]) -> SourceProjectionReference:
        return SourceProjectionReference.model_validate(dict(row))

    @staticmethod
    def _validation(row: Mapping[str, Any]) -> ProjectionValidation:
        return ProjectionValidation.model_validate(dict(row))

    @staticmethod
    def _snapshot(row: Mapping[str, Any]) -> ProjectionSnapshot:
        return ProjectionSnapshot.model_validate(dict(row))

    @staticmethod
    def _snapshot_member(row: Mapping[str, Any]) -> ProjectionSnapshotMember:
        return ProjectionSnapshotMember.model_validate(dict(row))

    @staticmethod
    def _reconciliation(row: Mapping[str, Any]) -> ProjectionReconciliationResult:
        return ProjectionReconciliationResult.model_validate(dict(row))

    @staticmethod
    def _activation(row: Mapping[str, Any]) -> ProjectionActivation:
        return ProjectionActivation.model_validate(dict(row))

    @staticmethod
    def _pointer(row: Mapping[str, Any]) -> ActiveProjection:
        return ActiveProjection.model_validate(dict(row))

    @staticmethod
    def _drift(row: Mapping[str, Any]) -> ProjectionDrift:
        return ProjectionDrift.model_validate(dict(row))

    async def add_ontology_version(self, ontology: OntologyVersion) -> OntologyVersion:
        self._uow._guard_active()
        self._scoped(ontology)
        row = (
            await self._uow._execute(
                select(_ONTOLOGY_VERSIONS).where(
                    _ONTOLOGY_VERSIONS.c.ontology_version_id == ontology.ontology_version_id,
                    _ONTOLOGY_VERSIONS.c.workspace_id == ontology.workspace_id,
                    _ONTOLOGY_VERSIONS.c.environment_id == ontology.environment_id,
                )
            )
        ).mappings().one_or_none()
        if row is not None:
            existing = self._ontology(row)
            if existing != ontology:
                await self._uow._fail(ConstraintConflictError("Ontology version identity conflicts."))
            return existing
        if ontology.predecessor_ontology_version_id is not None:
            predecessor = await self.resolve_ontology_version(ontology.predecessor_ontology_version_id)
            if (
                predecessor is None
                or predecessor.ontology_name != ontology.ontology_name
                or predecessor.version >= ontology.version
            ):
                await self._uow._fail(ConstraintConflictError("Ontology predecessor is invalid."))
        duplicate = (
            await self._uow._execute(
                select(_ONTOLOGY_VERSIONS.c.ontology_version_id).where(
                    _ONTOLOGY_VERSIONS.c.workspace_id == ontology.workspace_id,
                    _ONTOLOGY_VERSIONS.c.environment_id == ontology.environment_id,
                    _ONTOLOGY_VERSIONS.c.ontology_name == ontology.ontology_name,
                    _ONTOLOGY_VERSIONS.c.version == ontology.version,
                )
            )
        ).first()
        if duplicate is not None:
            await self._uow._fail(ConstraintConflictError("Ontology version is already registered."))
        values = ontology.model_dump(mode="python")
        values["definition_payload"] = ontology.model_dump(mode="json")["definition_payload"]
        await self._uow._execute(postgresql_insert(_ONTOLOGY_VERSIONS).values(**values))
        return ontology

    async def resolve_ontology_version(self, ontology_version_id: UUID) -> OntologyVersion | None:
        self._uow._guard_active()
        scope = self._scope()
        row = (
            await self._uow._execute(
                select(_ONTOLOGY_VERSIONS).where(
                    _ONTOLOGY_VERSIONS.c.ontology_version_id == ontology_version_id,
                    _ONTOLOGY_VERSIONS.c.workspace_id == scope.workspace_id,
                    _ONTOLOGY_VERSIONS.c.environment_id == scope.environment_id,
                )
            )
        ).mappings().one_or_none()
        return self._ontology(row) if row is not None else None

    async def add_config_version(self, config: ProjectionConfigVersion) -> ProjectionConfigVersion:
        self._uow._guard_active()
        self._scoped(config)
        ontology = await self.resolve_ontology_version(config.ontology_version_id)
        if ontology is None or not ontology.publication_eligible:
            await self._uow._fail(ConstraintConflictError("Projection config requires a published ontology."))
        row = (
            await self._uow._execute(
                select(_PROJECTION_CONFIGS).where(
                    _PROJECTION_CONFIGS.c.projection_config_version_id
                    == config.projection_config_version_id,
                    _PROJECTION_CONFIGS.c.workspace_id == config.workspace_id,
                    _PROJECTION_CONFIGS.c.environment_id == config.environment_id,
                )
            )
        ).mappings().one_or_none()
        if row is not None:
            existing = self._config(row)
            if existing != config:
                await self._uow._fail(ConstraintConflictError("Projection config identity conflicts."))
            return existing
        if config.predecessor_projection_config_version_id is not None:
            predecessor = await self.resolve_config_version(config.predecessor_projection_config_version_id)
            if (
                predecessor is None
                or predecessor.projection_kind != config.projection_kind
                or predecessor.version >= config.version
            ):
                await self._uow._fail(ConstraintConflictError("Projection config predecessor is invalid."))
        duplicate = (
            await self._uow._execute(
                select(_PROJECTION_CONFIGS.c.projection_config_version_id).where(
                    _PROJECTION_CONFIGS.c.workspace_id == config.workspace_id,
                    _PROJECTION_CONFIGS.c.environment_id == config.environment_id,
                    _PROJECTION_CONFIGS.c.projection_kind == config.projection_kind,
                    _PROJECTION_CONFIGS.c.version == config.version,
                )
            )
        ).first()
        if duplicate is not None:
            await self._uow._fail(ConstraintConflictError("Projection config version is already registered."))
        values = config.model_dump(mode="python")
        values["config_payload"] = config.model_dump(mode="json")["config_payload"]
        await self._uow._execute(postgresql_insert(_PROJECTION_CONFIGS).values(**values))
        return config

    async def resolve_config_version(self, config_id: UUID) -> ProjectionConfigVersion | None:
        self._uow._guard_active()
        scope = self._scope()
        row = (
            await self._uow._execute(
                select(_PROJECTION_CONFIGS).where(
                    _PROJECTION_CONFIGS.c.projection_config_version_id == config_id,
                    _PROJECTION_CONFIGS.c.workspace_id == scope.workspace_id,
                    _PROJECTION_CONFIGS.c.environment_id == scope.environment_id,
                )
            )
        ).mappings().one_or_none()
        return self._config(row) if row is not None else None

    async def _resolve_run(self, run_id: UUID, *, for_update: bool = False) -> ProjectionRun | None:
        scope = self._scope()
        query = select(_PROJECTION_RUNS).where(
            _PROJECTION_RUNS.c.projection_run_id == run_id,
            _PROJECTION_RUNS.c.workspace_id == scope.workspace_id,
            _PROJECTION_RUNS.c.environment_id == scope.environment_id,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._uow._execute(query)).mappings().one_or_none()
        return self._run(row) if row is not None else None

    async def create_run(self, command: ProjectionRunCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            run = await self._resolve_run(claim.result.result_id)
            if run is None:
                await self._uow._fail(ConstraintConflictError("Projection run replay is unavailable."))
            return ProjectionMutationResult(run, True, claim, claim.result)
        run = command.run.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        self._scoped(run)
        config = await self.resolve_config_version(run.projection_config_version_id)
        boundary_row = (
            await self._uow._execute(
                select(_CANONICAL_BOUNDARIES).where(
                    _CANONICAL_BOUNDARIES.c.boundary_id == run.canonical_boundary_id,
                    _CANONICAL_BOUNDARIES.c.workspace_id == run.workspace_id,
                    _CANONICAL_BOUNDARIES.c.environment_id == run.environment_id,
                )
            )
        ).mappings().one_or_none()
        if (
            config is None
            or config.projection_kind != run.projection_kind
            or boundary_row is None
            or boundary_row["projection_kind"] != run.projection_kind
        ):
            await self._uow._fail(ConstraintConflictError("Projection run configuration is invalid."))
        if run.predecessor_snapshot_id is not None:
            predecessor = await self.resolve_snapshot(run.predecessor_snapshot_id)
            if (
                predecessor is None
                or predecessor.projection_kind != run.projection_kind
                or predecessor.snapshot_state is not ProjectionSnapshotState.FINALIZED
            ):
                await self._uow._fail(ConstraintConflictError("Projection run predecessor is invalid."))
        existing_command = (
            await self._uow._execute(
                select(_PROJECTION_RUNS).where(
                    _PROJECTION_RUNS.c.workspace_id == run.workspace_id,
                    _PROJECTION_RUNS.c.environment_id == run.environment_id,
                    _PROJECTION_RUNS.c.command_identity == run.command_identity,
                )
            )
        ).mappings().one_or_none()
        if existing_command is not None:
            if existing_command["command_digest"] != run.command_digest:
                await self._uow._fail(IdempotencyConflictError("Projection command identity conflicts."))
            await self._uow._fail(ConstraintConflictError("Projection command identity is already registered."))
        if await self._resolve_run(run.projection_run_id) is not None:
            await self._uow._fail(ConstraintConflictError("Projection run identity conflicts."))
        values = run.model_dump(mode="python")
        values["operation"] = run.operation.value
        values["candidate_state"] = run.candidate_state.value
        await self._uow._execute(postgresql_insert(_PROJECTION_RUNS).values(**values))
        result = ProjectionMutationResult(
            run,
            False,
            claim,
            OpaqueResultReference("projection_run", run.projection_run_id, 1),
        )
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def resolve_run(self, projection_run_id: UUID) -> ProjectionRun | None:
        self._uow._guard_active()
        return await self._resolve_run(projection_run_id)

    async def _open_run(self, run_id: UUID, candidate_id: UUID, *, for_update: bool = False) -> ProjectionRun:
        run = await self._resolve_run(run_id, for_update=for_update)
        if run is None or run.projection_candidate_id != candidate_id:
            await self._uow._fail(ConstraintConflictError("Projection candidate lineage is invalid."))
        if run.candidate_state is ProjectionCandidateState.SEALED:
            await self._uow._fail(OptimisticConflictError("Projection candidate is sealed."))
        return run

    async def append_transition(self, transition: ProjectionRunTransition) -> ProjectionRunTransition:
        self._uow._guard_active()
        self._scoped(transition)
        await self._open_run(transition.projection_run_id, transition.projection_candidate_id)
        predecessor = None
        if transition.predecessor_transition_id is not None:
            predecessor_row = (
                await self._uow._execute(
                    select(_PROJECTION_TRANSITIONS).where(
                        _PROJECTION_TRANSITIONS.c.projection_run_transition_id
                        == transition.predecessor_transition_id,
                        _PROJECTION_TRANSITIONS.c.workspace_id == transition.workspace_id,
                        _PROJECTION_TRANSITIONS.c.environment_id == transition.environment_id,
                    )
                )
            ).mappings().one_or_none()
            if predecessor_row is None:
                await self._uow._fail(ConstraintConflictError("Projection transition predecessor is invalid."))
            predecessor = self._transition(predecessor_row)
            if (
                predecessor.projection_run_id != transition.projection_run_id
                or predecessor.projection_candidate_id != transition.projection_candidate_id
            ):
                await self._uow._fail(ConstraintConflictError("Projection transition predecessor is invalid."))
        if not transition_allowed(predecessor.state if predecessor else None, transition.state):
            await self._uow._fail(OptimisticConflictError("Projection run transition is not legal."))
        values = transition.model_dump(mode="python")
        values["state"] = transition.state.value
        await self._uow._execute(postgresql_insert(_PROJECTION_TRANSITIONS).values(**values))
        return transition

    async def add_partition(self, partition: ProjectionPartition) -> ProjectionPartition:
        self._uow._guard_active()
        self._scoped(partition)
        await self._open_run(partition.projection_run_id, partition.projection_candidate_id)
        await self._uow._execute(postgresql_insert(_PROJECTION_PARTITIONS).values(**partition.model_dump(mode="python")))
        return partition

    async def list_transitions(self, projection_run_id: UUID) -> tuple[ProjectionRunTransition, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (
            await self._uow._execute(
                select(_PROJECTION_TRANSITIONS)
                .where(
                    _PROJECTION_TRANSITIONS.c.projection_run_id == projection_run_id,
                    _PROJECTION_TRANSITIONS.c.workspace_id == scope.workspace_id,
                    _PROJECTION_TRANSITIONS.c.environment_id == scope.environment_id,
                )
                .order_by(_PROJECTION_TRANSITIONS.c.projection_run_transition_id)
            )
        ).mappings().all()
        return tuple(self._transition(row) for row in rows)

    async def record_receipt(self, command: ProjectionReceiptCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            row = (
                await self._uow._execute(
                    select(_PROJECTION_RECEIPTS).where(
                        _PROJECTION_RECEIPTS.c.projection_receipt_id == claim.result.result_id
                    )
                )
            ).mappings().one_or_none()
            if row is None:
                await self._uow._fail(ConstraintConflictError("Projection receipt replay is unavailable."))
            return ProjectionMutationResult(self._receipt(row), True, claim, claim.result)
        receipt = command.receipt.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
        self._scoped(receipt)
        await self._open_run(receipt.projection_run_id, receipt.projection_candidate_id)
        partition = (
            await self._uow._execute(
                select(_PROJECTION_PARTITIONS).where(
                    _PROJECTION_PARTITIONS.c.projection_partition_id == receipt.projection_partition_id,
                    _PROJECTION_PARTITIONS.c.workspace_id == receipt.workspace_id,
                    _PROJECTION_PARTITIONS.c.environment_id == receipt.environment_id,
                )
            )
        ).mappings().one_or_none()
        if (
            partition is None
            or partition.projection_run_id != receipt.projection_run_id
            or partition.projection_candidate_id != receipt.projection_candidate_id
            or partition.expected_command_identity != receipt.command_identity
            or partition.expected_command_digest != receipt.command_digest
        ):
            await self._uow._fail(ConstraintConflictError("Projection receipt lineage is invalid."))
        existing_command = (
            await self._uow._execute(
                select(_PROJECTION_RECEIPTS).where(
                    _PROJECTION_RECEIPTS.c.workspace_id == receipt.workspace_id,
                    _PROJECTION_RECEIPTS.c.environment_id == receipt.environment_id,
                    _PROJECTION_RECEIPTS.c.command_identity == receipt.command_identity,
                )
            )
        ).mappings().one_or_none()
        if existing_command is not None:
            if existing_command["command_digest"] != receipt.command_digest:
                await self._uow._fail(IdempotencyConflictError("Projection command identity conflicts."))
            await self._uow._fail(ConstraintConflictError("Projection command identity is already registered."))
        values = receipt.model_dump(mode="python")
        json_values = receipt.model_dump(mode="json")
        for field in ("counts", "warnings", "failures", "implementation_metadata"):
            values[field] = json_values[field]
        await self._uow._execute(postgresql_insert(_PROJECTION_RECEIPTS).values(**values))
        result = ProjectionMutationResult(
            receipt,
            False,
            claim,
            OpaqueResultReference("projection_receipt", receipt.projection_receipt_id, 1),
        )
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def add_source_reference(self, reference: SourceProjectionReference) -> SourceProjectionReference:
        self._uow._guard_active()
        self._scoped(reference)
        run = await self._open_run(reference.projection_run_id, reference.projection_candidate_id)
        receipt = (
            await self._uow._execute(
                select(_PROJECTION_RECEIPTS).where(
                    _PROJECTION_RECEIPTS.c.projection_receipt_id == reference.projection_receipt_id,
                    _PROJECTION_RECEIPTS.c.workspace_id == reference.workspace_id,
                    _PROJECTION_RECEIPTS.c.environment_id == reference.environment_id,
                )
            )
        ).mappings().one_or_none()
        if (
            receipt is None
            or receipt.projection_run_id != run.projection_run_id
            or receipt.projection_candidate_id != run.projection_candidate_id
        ):
            await self._uow._fail(ConstraintConflictError("Source projection reference lineage is invalid."))
        partition_row = (
            await self._uow._execute(
                select(_PROJECTION_PARTITIONS).where(
                    _PROJECTION_PARTITIONS.c.projection_partition_id
                    == receipt["projection_partition_id"],
                    _PROJECTION_PARTITIONS.c.workspace_id == reference.workspace_id,
                    _PROJECTION_PARTITIONS.c.environment_id == reference.environment_id,
                )
            )
        ).mappings().one_or_none()
        config = await self.resolve_config_version(reference.projection_config_version_id)
        source_revision = (
            await self._uow._execute(
                select(_SOURCE_REVISIONS.c.source_object_id).where(
                    _SOURCE_REVISIONS.c.revision_id == reference.source_revision_id,
                    _SOURCE_REVISIONS.c.source_object_id == reference.source_object_id,
                    _SOURCE_REVISIONS.c.workspace_id == reference.workspace_id,
                    _SOURCE_REVISIONS.c.environment_id == reference.environment_id,
                )
            )
        ).first()
        if (
            partition_row is None
            or partition_row["security_domain_reference"] != reference.security_domain_reference
            or config is None
            or config.projection_config_version_id != run.projection_config_version_id
            or source_revision is None
        ):
            await self._uow._fail(ConstraintConflictError("Source projection reference lineage is invalid."))
        if reference.processing_receipt_id is not None:
            processing_receipt = (
                await self._uow._execute(
                    select(_PROCESSING_RECEIPTS.c.processing_receipt_id).where(
                        _PROCESSING_RECEIPTS.c.processing_receipt_id == reference.processing_receipt_id,
                        _PROCESSING_RECEIPTS.c.workspace_id == reference.workspace_id,
                        _PROCESSING_RECEIPTS.c.environment_id == reference.environment_id,
                    )
                )
            ).first()
            if processing_receipt is None:
                await self._uow._fail(ConstraintConflictError("Processing receipt lineage is invalid."))
        await self._uow._execute(postgresql_insert(_SOURCE_PROJECTION_REFERENCES).values(**reference.model_dump(mode="python")))
        return reference

    async def list_partitions(self, projection_run_id: UUID) -> tuple[ProjectionPartition, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (await self._uow._execute(select(_PROJECTION_PARTITIONS).where(_PROJECTION_PARTITIONS.c.projection_run_id == projection_run_id, _PROJECTION_PARTITIONS.c.workspace_id == scope.workspace_id, _PROJECTION_PARTITIONS.c.environment_id == scope.environment_id).order_by(_PROJECTION_PARTITIONS.c.projection_partition_id))).mappings().all()
        return tuple(self._partition(row) for row in rows)

    async def list_receipts(self, projection_run_id: UUID) -> tuple[ProjectionReceipt, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (await self._uow._execute(select(_PROJECTION_RECEIPTS).where(_PROJECTION_RECEIPTS.c.projection_run_id == projection_run_id, _PROJECTION_RECEIPTS.c.workspace_id == scope.workspace_id, _PROJECTION_RECEIPTS.c.environment_id == scope.environment_id).order_by(_PROJECTION_RECEIPTS.c.projection_receipt_id))).mappings().all()
        return tuple(self._receipt(row) for row in rows)

    async def list_source_references(self, projection_run_id: UUID) -> tuple[SourceProjectionReference, ...]:
        self._uow._guard_active()
        scope = self._scope()
        rows = (await self._uow._execute(select(_SOURCE_PROJECTION_REFERENCES).where(_SOURCE_PROJECTION_REFERENCES.c.projection_run_id == projection_run_id, _SOURCE_PROJECTION_REFERENCES.c.workspace_id == scope.workspace_id, _SOURCE_PROJECTION_REFERENCES.c.environment_id == scope.environment_id).order_by(_SOURCE_PROJECTION_REFERENCES.c.source_projection_reference_id))).mappings().all()
        return tuple(self._reference(row) for row in rows)

    async def seal_candidate(self, command: ProjectionCandidateSealCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            run = await self._resolve_run(claim.result.result_id)
            if run is None:
                await self._uow._fail(ConstraintConflictError("Projection candidate replay is unavailable."))
            return ProjectionMutationResult(run, True, claim, claim.result)
        run = await self._open_run(command.projection_run_id, command.projection_candidate_id, for_update=True)
        config = await self.resolve_config_version(run.projection_config_version_id)
        if config is None:
            await self._uow._fail(ConstraintConflictError("Projection config version is unavailable."))
        partitions = await self.list_partitions(run.projection_run_id)
        transitions = await self.list_transitions(run.projection_run_id)
        receipts = await self.list_receipts(run.projection_run_id)
        references = await self.list_source_references(run.projection_run_id)
        validations = await self.list_validations(run.projection_run_id)
        if not partitions or not receipts:
            await self._uow._fail(ConstraintConflictError("Projection candidate has incomplete evidence."))
        if (
            any(item.decision.value != "passed" for item in validations)
            or not has_complete_physical_validation(
                validations, partitions, receipts, references, run, config
            )
        ):
            await self._uow._fail(
                ConstraintConflictError(
                    "Projection candidate physical validation evidence is incomplete."
                )
            )
        evidence_digest = projection_evidence_digest(
            partitions=partitions,
            receipts=receipts,
            references=references,
            transitions=transitions,
            validations=validations,
        )
        sealed_at = datetime.now(timezone.utc)
        await self._uow._execute(
            update(_PROJECTION_RUNS)
            .where(
                _PROJECTION_RUNS.c.projection_run_id == run.projection_run_id,
                _PROJECTION_RUNS.c.candidate_state == "open",
            )
            .values(
                candidate_state="sealed",
                candidate_evidence_digest=evidence_digest,
                candidate_sealed_at=sealed_at,
            )
        )
        sealed = ProjectionRun(
            **{
                **run.model_dump(mode="python"),
                "candidate_state": ProjectionCandidateState.SEALED,
                "candidate_evidence_digest": evidence_digest,
                "candidate_sealed_at": sealed_at,
            }
        )
        result = ProjectionMutationResult(
            sealed,
            False,
            claim,
            OpaqueResultReference("projection_run", run.projection_run_id, 1),
        )
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result


class PostgreSQLTenantUnitOfWork:
    """Own one PostgreSQL session and transaction for one trusted operation."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        source_context: TrustedPersistenceContext,
        context_snapshot: TrustedPersistenceContext,
        context_verifier: TrustedContextVerifier,
        receipt_id_factory: Callable[[], UUID],
        outbox_events: OutboxEventRegistry,
        audit_events: AuditEventRegistry,
        transaction_probe: PostgreSQLTransactionProbe,
        boundary_id_factory: Callable[[], UUID],
        boundary_clock: Callable[[], datetime] | None,
    ) -> None:
        self._session_factory = session_factory
        self._source_context = source_context
        self._context_snapshot = context_snapshot
        self._context_verifier = context_verifier
        self._receipt_id_factory = receipt_id_factory
        self._outbox_events = outbox_events
        self._audit_events = audit_events
        self._transaction_probe = transaction_probe
        self._boundary_id_factory = boundary_id_factory
        self._boundary_clock = boundary_clock
        self._scope: PersistenceScope | None = None
        self._operation: PersistenceOperation | None = None
        self._trace_id: UUID | None = None
        self._active_context: TrustedPersistenceContext | None = None
        self._session = None
        self._lifecycle = _Lifecycle.NEW
        self._owner: asyncio.Task[object] | None = None
        self._workspaces = _PostgreSQLWorkspaceRepository(self)
        self._environments = _PostgreSQLEnvironmentRepository(self)
        self._context_profiles = _PostgreSQLContextProfileRepository(self)
        self._context_bundles = _PostgreSQLContextBundleRepository(self)
        self._sources = _PostgreSQLSourceObservationRepository(self)
        self._canonical = _PostgreSQLCanonicalAcceptanceRepository(self)
        self._boundaries = _PostgreSQLCanonicalBoundaryRepository(self)
        self._projections = _PostgreSQLProjectionRepository(self)
        self._ingestion = _PostgreSQLIngestionRepository(self)
        self._admissions = _PostgreSQLAdmissionRepository(self)
        self._access_policies = _PostgreSQLAccessPolicyRepository(self)
        self._idempotency = _PostgreSQLIdempotencyRepository(self)
        self._outbox = _PostgreSQLOutboxWriter(self)
        self._audit = _PostgreSQLAuditWriter(self)
        self._owned_claims: dict[UUID, OwnedIdempotencyClaim] = {}
        self._pending_audit_events: dict[UUID, AuditEvent] = {}
        self._activity_scope: AbstractContextManager[None] | None = None

    @property
    def scope(self) -> PersistenceScope:
        self._guard_active()
        if self._scope is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._scope

    @property
    def workspaces(self) -> _PostgreSQLWorkspaceRepository:
        self._guard_active()
        return self._workspaces

    @property
    def environments(self) -> _PostgreSQLEnvironmentRepository:
        self._guard_active()
        return self._environments

    @property
    def context_profiles(self) -> _PostgreSQLContextProfileRepository:
        self._guard_active()
        return self._context_profiles

    @property
    def context_bundles(self) -> _PostgreSQLContextBundleRepository:
        self._guard_active()
        return self._context_bundles

    @property
    def sources(self) -> _PostgreSQLSourceObservationRepository:
        self._guard_active()
        return self._sources

    @property
    def canonical(self) -> _PostgreSQLCanonicalAcceptanceRepository:
        self._guard_active()
        return self._canonical

    @property
    def boundaries(self) -> _PostgreSQLCanonicalBoundaryRepository:
        self._guard_active()
        return self._boundaries

    @property
    def canonical_boundaries(self) -> _PostgreSQLCanonicalBoundaryRepository:
        self._guard_active()
        return self._boundaries

    @property
    def projections(self) -> _PostgreSQLProjectionRepository:
        self._guard_active()
        return self._projections

    @property
    def projection(self) -> _PostgreSQLProjectionRepository:
        self._guard_active()
        return self._projections

    @property
    def ingestion(self) -> IngestionRepository:
        self._guard_active()
        return self._ingestion

    @property
    def admissions(self) -> _PostgreSQLAdmissionRepository:
        self._guard_active()
        return self._admissions

    @property
    def admission(self) -> _PostgreSQLAdmissionRepository:
        self._guard_active()
        return self._admissions

    @property
    def access_policies(self) -> _PostgreSQLAccessPolicyRepository:
        self._guard_active()
        return self._access_policies

    @property
    def idempotency(self) -> _PostgreSQLIdempotencyRepository:
        self._guard_active()
        return self._idempotency

    @property
    def outbox(self) -> _PostgreSQLOutboxWriter:
        self._guard_active()
        return self._outbox

    @property
    def audit(self) -> _PostgreSQLAuditWriter:
        self._guard_active()
        return self._audit

    def _context_snapshot_active(self) -> TrustedPersistenceContext:
        self._guard_active()
        if self._active_context is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._active_context

    def _operation_snapshot(self) -> PersistenceOperation:
        self._guard_active()
        if self._operation is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._operation

    def _trace_id_snapshot(self) -> UUID:
        self._guard_active()
        if self._trace_id is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._trace_id

    def _audit_scope_predicates(self) -> tuple[object, ...]:
        scope = self.scope
        environment_predicate = (
            _AUDIT_EVENTS.c.environment_id == scope.environment_id
            if isinstance(scope, EnvironmentScope)
            else _AUDIT_EVENTS.c.environment_id.is_(None)
        )
        return (
            _AUDIT_EVENTS.c.workspace_id == scope.workspace_id,
            environment_predicate,
        )

    async def _find_audit_by_id(
        self,
        audit_event_id: UUID,
    ) -> AuditEvent | None:
        pending = self._pending_audit_events.get(audit_event_id)
        if pending is not None:
            return pending
        result = await self._execute(
            select(_AUDIT_EVENTS).where(
                _AUDIT_EVENTS.c.audit_event_id == audit_event_id,
                *self._audit_scope_predicates(),
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        try:
            return _audit_event_from_row(row, registry=self._audit_events)
        except BaseException as error:
            await self._fail(error)

    async def _find_audit_by_producer(
        self,
        event: AuditEvent,
    ) -> AuditEvent | None:
        producer_id = event.producer_deduplication_id
        if producer_id is None:
            return None
        for pending in self._pending_audit_events.values():
            if (
                pending.event_type == event.event_type
                and pending.producer_deduplication_id == producer_id
                and pending.workspace_id == event.workspace_id
                and pending.environment_id == event.environment_id
            ):
                return pending
        result = await self._execute(
            select(_AUDIT_EVENTS).where(
                _AUDIT_EVENTS.c.event_type == event.event_type,
                _AUDIT_EVENTS.c.producer_deduplication_id == producer_id,
                *self._audit_scope_predicates(),
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        try:
            return _audit_event_from_row(row, registry=self._audit_events)
        except BaseException as error:
            await self._fail(error)

    async def _flush_audit_events(self) -> None:
        pending_events = tuple(self._pending_audit_events.values())
        for event in pending_events:
            target = event.target
            inserted = await self._execute(
                postgresql_insert(_AUDIT_EVENTS)
                .values(
                    audit_event_id=event.audit_event_id,
                    workspace_id=event.workspace_id,
                    environment_id=event.environment_id,
                    event_type=event.event_type,
                    schema_version=event.schema_version,
                    origin=event.origin.value,
                    acting_subject_id=event.acting_subject_id,
                    service_principal_id=event.service_principal_id,
                    trace_id=event.trace_id,
                    correlation_id=event.correlation_id,
                    causation_id=event.causation_id,
                    occurred_at=event.occurred_at,
                    target_type=target.object_type if target else None,
                    target_id=target.object_id if target else None,
                    target_schema_version=(target.schema_version if target else None),
                    outcome=event.outcome.value,
                    reason=event.reason,
                    producer_deduplication_id=event.producer_deduplication_id,
                    payload=event.payload_json(),
                )
                .on_conflict_do_nothing()
                .returning(_AUDIT_EVENTS.c.audit_event_id)
            )
            if inserted.scalar_one_or_none() is None:
                await self._fail(
                    AuditConflictError(
                        "Audit Event identity or producer identity already exists."
                    )
                )
        if pending_events:
            await self._run_transaction_probe(
                PostgreSQLTransactionStage.AFTER_AUDIT_FLUSH
            )

    async def __aenter__(self) -> "PostgreSQLTenantUnitOfWork":
        if self._lifecycle is not _Lifecycle.NEW:
            raise UnitOfWorkLifecycleError(
                "Unit of Work cannot be entered more than once."
            )
        self._lifecycle = _Lifecycle.ENTERING
        self._owner = asyncio.current_task()
        try:
            snapshot = self._context_verifier.verify(self._source_context)
            if snapshot != self._context_snapshot:
                raise InvalidPersistenceContextError(
                    "Persistence context is invalid."
                )
            self._session = self._session_factory()
            await self._session.begin()
            await self._bind_context(snapshot)
            self._scope = snapshot.scope
            self._operation = PersistenceOperation(snapshot.operation.value)
            self._trace_id = snapshot.trace_id
            self._active_context = snapshot
            if isinstance(snapshot.scope, EnvironmentScope):
                await self._validate_environment_scope(snapshot.scope)
        except BaseException as error:
            await self._terminate(error)
        self._lifecycle = _Lifecycle.ACTIVE
        self._activity_scope = track_unit_of_work_activity()
        self._activity_scope.__enter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._lifecycle is _Lifecycle.CLOSED:
                return
            self._guard_owner()
            if self._lifecycle is _Lifecycle.ACTIVE:
                await self.rollback()
            await self._close()
        finally:
            self._stop_tracking_activity()

    async def commit(self) -> None:
        self._guard_active()
        if self._owned_claims:
            await self._fail(
                UnitOfWorkLifecycleError(
                    "Owned idempotency claims must be completed before commit."
                )
            )
        await self._flush_audit_events()
        await self._run_transaction_probe(PostgreSQLTransactionStage.BEFORE_COMMIT)
        assert self._session is not None
        try:
            await self._session.commit()
        except BaseException as error:
            await self._terminate(error)
        self._lifecycle = _Lifecycle.COMMITTED
        self._owned_claims.clear()
        self._pending_audit_events.clear()

    async def rollback(self) -> None:
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ROLLED_BACK:
            return
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        assert self._session is not None
        try:
            await self._session.rollback()
        except BaseException as error:
            await self._terminate(error)
        self._lifecycle = _Lifecycle.ROLLED_BACK
        self._owned_claims.clear()
        self._pending_audit_events.clear()

    def _guard_owner(self) -> None:
        if self._owner is None or asyncio.current_task() is not self._owner:
            raise UnitOfWorkLifecycleError(
                "Unit of Work may only be used by its owning task."
            )

    def _guard_active(self) -> None:
        self._guard_owner()
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")

    def _is_active(self) -> bool:
        return self._lifecycle is _Lifecycle.ACTIVE

    async def _bind_context(self, context: TrustedPersistenceContext) -> None:
        scope = context.scope
        environment_id = (
            str(scope.environment_id) if isinstance(scope, EnvironmentScope) else ""
        )
        await self._execute_during_entry(
            _BIND_CONTEXT,
            {
                "workspace_id": str(scope.workspace_id),
                "environment_id": environment_id,
                "acting_subject_id": str(context.acting_subject_id or ""),
                "service_principal_id": str(context.service_principal_id or ""),
                "purpose": context.purpose.value,
                "operation": context.operation.value,
                "trace_id": str(context.trace_id),
            },
        )

    async def _validate_environment_scope(self, scope: EnvironmentScope) -> None:
        result = await self._execute_during_entry(
            select(_ENVIRONMENTS.c.id).where(
                _ENVIRONMENTS.c.id == scope.environment_id,
                _ENVIRONMENTS.c.workspace_id == scope.workspace_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise InvalidPersistenceContextError("Persistence context is invalid.")

    async def _bind_environment(self, environment_id: UUID) -> None:
        await self._execute(
            _BIND_ENVIRONMENT,
            {"environment_id": str(environment_id)},
        )

    async def _restore_environment_binding(self) -> None:
        scope = self.scope
        environment_id = (
            str(scope.environment_id) if isinstance(scope, EnvironmentScope) else ""
        )
        await self._execute(
            _BIND_ENVIRONMENT,
            {"environment_id": environment_id},
        )

    @asynccontextmanager
    async def _temporary_environment_binding(
        self,
        environment_id: UUID,
    ) -> AsyncIterator[None]:
        await self._bind_environment(environment_id)
        try:
            yield
        finally:
            if self._is_active():
                await self._restore_environment_binding()

    @asynccontextmanager
    async def _preserve_environment_binding(self) -> AsyncIterator[None]:
        yield

    async def _execute(self, statement: object, parameters: object | None = None):
        self._guard_active()
        assert self._session is not None
        try:
            return await self._session.execute(statement, parameters)
        except BaseException as error:
            await self._terminate(error)

    async def _execute_during_entry(
        self, statement: object, parameters: object | None = None
    ):
        assert self._session is not None
        return await self._session.execute(statement, parameters)

    async def _run_transaction_probe(
        self,
        stage: PostgreSQLTransactionStage,
    ) -> None:
        self._guard_active()
        try:
            await self._transaction_probe(stage)
        except BaseException as error:
            await self._terminate(error)

    async def _fail(self, error: BaseException) -> NoReturn:
        await self._terminate(error)

    async def _terminate(self, error: BaseException) -> NoReturn:
        self._lifecycle = _Lifecycle.CLOSED
        session = self._session
        self._scope = None
        self._operation = None
        self._trace_id = None
        self._active_context = None
        self._owned_claims.clear()
        self._pending_audit_events.clear()
        if session is not None:
            try:
                await session.rollback()
            except BaseException:
                pass
            try:
                await session.close()
            except BaseException:
                pass
            self._session = None
        self._stop_tracking_activity()
        if isinstance(error, asyncio.CancelledError):
            raise error
        if isinstance(error, PersistenceError):
            raise error
        if isinstance(error, Exception):
            raise translate_persistence_error(error) from None
        raise error

    def _stop_tracking_activity(self) -> None:
        activity_scope = self._activity_scope
        self._activity_scope = None
        if activity_scope is not None:
            activity_scope.__exit__(None, None, None)

    async def _close(self) -> None:
        session = self._session
        self._scope = None
        self._operation = None
        self._trace_id = None
        self._active_context = None
        self._owned_claims.clear()
        self._pending_audit_events.clear()
        self._session = None
        self._lifecycle = _Lifecycle.CLOSED
        if session is not None:
            try:
                await session.close()
            except Exception as error:
                raise translate_persistence_error(error) from None


class PostgreSQLTenantUnitOfWorkFactory:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        audit_events: AuditEventRegistry,
        transaction_probe: PostgreSQLTransactionProbe,
        receipt_id_factory: Callable[[], UUID] = uuid4,
        boundary_id_factory: Callable[[], UUID] = uuid4,
        boundary_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier
        self._receipt_id_factory = receipt_id_factory
        self._outbox_events = outbox_events
        self._audit_events = audit_events
        self._transaction_probe = transaction_probe
        self._boundary_id_factory = boundary_id_factory
        self._boundary_clock = boundary_clock

    def __call__(
        self, context: TrustedPersistenceContext
    ) -> PostgreSQLTenantUnitOfWork:
        if not isinstance(context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        snapshot = self._context_verifier.verify(context)
        return PostgreSQLTenantUnitOfWork(
            session_factory=self._session_factory,
            source_context=context,
            context_snapshot=snapshot,
            context_verifier=self._context_verifier,
            receipt_id_factory=self._receipt_id_factory,
            outbox_events=self._outbox_events,
            audit_events=self._audit_events,
            transaction_probe=self._transaction_probe,
            boundary_id_factory=self._boundary_id_factory,
            boundary_clock=self._boundary_clock,
        )


class PostgreSQLAuditReader:
    """Minimal tenant-scoped read-by-opaque-ID audit seam."""

    def __init__(self, uow_factory: PostgreSQLTenantUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def resolve(
        self,
        context: TrustedPersistenceContext,
        audit_event_id: UUID,
    ) -> AuditEvent | None:
        if not isinstance(audit_event_id, UUID) or audit_event_id.int == 0:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        async with self._uow_factory(context) as uow:
            return await uow._find_audit_by_id(audit_event_id)


class PostgreSQLPersistence:
    """Production persistence adapter assembled from process database resources."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        audit_events: AuditEventRegistry | None = None,
        transaction_probe: PostgreSQLTransactionProbe = _ignore_transaction_stage,
        receipt_id_factory: Callable[[], UUID] = uuid4,
        boundary_id_factory: Callable[[], UUID] = uuid4,
        boundary_clock: Callable[[], datetime] | None = None,
    ) -> None:
        selected_audit_events = (
            audit_events or AuditEventRegistry.with_default_families()
        )
        register_projection_outbox_events(outbox_events)
        selected_audit_events.require_sealed()
        self.tenant_uow_factory = PostgreSQLTenantUnitOfWorkFactory(
            session_factory=session_factory,
            context_verifier=context_verifier,
            receipt_id_factory=receipt_id_factory,
            outbox_events=outbox_events,
            audit_events=selected_audit_events,
            transaction_probe=transaction_probe,
            boundary_id_factory=boundary_id_factory,
            boundary_clock=boundary_clock,
        )
        self.uow_factory = self.tenant_uow_factory
        self.audit_reader = PostgreSQLAuditReader(self.tenant_uow_factory)
        self.backup_recovery_repository = PostgreSQLBackupRecoveryRepository(
            session_factory=session_factory
        )


__all__ = [
    "PostgreSQLPersistence",
    "PostgreSQLAuditReader",
    "PostgreSQLTenantUnitOfWork",
    "PostgreSQLTenantUnitOfWorkFactory",
    "PostgreSQLTransactionProbe",
    "PostgreSQLTransactionStage",
    "translate_persistence_error",
]
