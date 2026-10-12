"""PostgreSQL implementation of the application persistence seam."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
import hashlib
import json
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from enum import Enum, auto
from types import TracebackType
from typing import Any, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, insert, null, select, text, update
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
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
    idempotency_conflict,
)
from spine.application.persistence.outbox import (
    OutboxEventRegistry,
    OutboxIntent,
    validate_outbox_intent_for_context,
)
from spine.domain.audit import AuditEvent, AuditObjectReference, AuditOutcome
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
    BEFORE_COMMIT = auto()


PostgreSQLTransactionProbe = Callable[
    [PostgreSQLTransactionStage], Awaitable[None]
]


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
            await _lock_admission_revision(
                self._uow,
                workspace_id=acceptance.workspace_id,
                environment_id=acceptance.environment_id,
                source_object_id=acceptance.source_object_id,
                revision_id=acceptance.source_revision_id,
            )
            current = await self.resolve_current(command.source.source_object_id)
            if (
                current is None
                or current.acceptance_id != acceptance.canonical_acceptance_id
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
                current.current_revision_kind is RevisionKind.TOMBSTONE
                and acceptance.admission_decision_id is None
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
            return CanonicalAcceptanceResult(
                source=self._source(source_row),
                revision=self._uow.sources._revision_from_row(revision_row),
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

        await _lock_admission_revision(
            self._uow,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            source_object_id=source.source_object_id,
            revision_id=revision.revision_id,
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
        if command.audit_event is None or command.outbox_intent is None:
            await self._uow._fail(
                ConstraintConflictError(
                    "Canonical acceptance audit and outbox evidence is required."
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
            await self._uow._fail(
                ConstraintConflictError(
                    "Original receipt verification is unavailable; content binding is fail-closed."
                )
            )
        if command.binding.original_receipt_id is not None:
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
        item_rows = (await self._uow._execute(select(_INGESTION_ITEMS).where(_INGESTION_ITEMS.c.ingestion_run_id == run_id, _INGESTION_ITEMS.c.workspace_id == scope.workspace_id, _INGESTION_ITEMS.c.environment_id == scope.environment_id))).mappings().all()
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
        return IngestionRunStateView(run=run, state=state, item_states=tuple(item_states))

    async def resolve_item(self, item_id: UUID) -> IngestionRunSource | None:
        self._uow._guard_active()
        scope = self._uow.scope
        row = (await self._uow._execute(select(_INGESTION_ITEMS).where(_INGESTION_ITEMS.c.ingestion_run_source_id == item_id, _INGESTION_ITEMS.c.workspace_id == scope.workspace_id, _INGESTION_ITEMS.c.environment_id == scope.environment_id))).mappings().one_or_none()
        return IngestionRunSource.model_validate(dict(row)) if row is not None else None


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
        self, source_object_id: UUID
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
        self, policy_version_id: UUID
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
    ) -> None:
        self._session_factory = session_factory
        self._source_context = source_context
        self._context_snapshot = context_snapshot
        self._context_verifier = context_verifier
        self._receipt_id_factory = receipt_id_factory
        self._outbox_events = outbox_events
        self._audit_events = audit_events
        self._transaction_probe = transaction_probe
        self._scope: PersistenceScope | None = None
        self._operation: PersistenceOperation | None = None
        self._trace_id: UUID | None = None
        self._active_context: TrustedPersistenceContext | None = None
        self._session = None
        self._lifecycle = _Lifecycle.NEW
        self._owner: asyncio.Task[object] | None = None
        self._workspaces = _PostgreSQLWorkspaceRepository(self)
        self._environments = _PostgreSQLEnvironmentRepository(self)
        self._sources = _PostgreSQLSourceObservationRepository(self)
        self._canonical = _PostgreSQLCanonicalAcceptanceRepository(self)
        self._ingestion = _PostgreSQLIngestionRepository(self)
        self._admissions = _PostgreSQLAdmissionRepository(self)
        self._access_policies = _PostgreSQLAccessPolicyRepository(self)
        self._idempotency = _PostgreSQLIdempotencyRepository(self)
        self._outbox = _PostgreSQLOutboxWriter(self)
        self._audit = _PostgreSQLAuditWriter(self)
        self._owned_claims: dict[UUID, OwnedIdempotencyClaim] = {}
        self._pending_audit_events: dict[UUID, AuditEvent] = {}

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
    def sources(self) -> _PostgreSQLSourceObservationRepository:
        self._guard_active()
        return self._sources

    @property
    def canonical(self) -> _PostgreSQLCanonicalAcceptanceRepository:
        self._guard_active()
        return self._canonical

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
        await self._close()

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
        if isinstance(error, asyncio.CancelledError):
            raise error
        if isinstance(error, PersistenceError):
            raise error
        if isinstance(error, Exception):
            raise translate_persistence_error(error) from None
        raise error

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
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier
        self._receipt_id_factory = receipt_id_factory
        self._outbox_events = outbox_events
        self._audit_events = audit_events
        self._transaction_probe = transaction_probe

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
    ) -> None:
        selected_audit_events = (
            audit_events or AuditEventRegistry.with_default_families()
        )
        selected_audit_events.require_sealed()
        self.tenant_uow_factory = PostgreSQLTenantUnitOfWorkFactory(
            session_factory=session_factory,
            context_verifier=context_verifier,
            receipt_id_factory=receipt_id_factory,
            outbox_events=outbox_events,
            audit_events=selected_audit_events,
            transaction_probe=transaction_probe,
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
