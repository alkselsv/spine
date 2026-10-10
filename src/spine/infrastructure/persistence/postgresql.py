"""PostgreSQL implementation of the application persistence seam."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from enum import Enum, auto
from types import TracebackType
from typing import NoReturn
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.application.diagnostics.audit import (
    AuditEventRegistry,
    same_logical_audit_event,
    validate_audit_event_for_context,
)
from spine.application.persistence.command_digest import CommandDigest
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
    ConstraintConflictError,
    IdempotencyConflictError,
    InvalidPersistenceContextError,
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
from spine.domain.sources import SourceObject, SourceRevision, SourceRevisionProvenance
from spine.domain.sources.canonicalization import assert_revision_digest
from spine.domain.sources.errors import RevisionDigestMismatchError as DomainRevisionDigestMismatchError
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_errors import (
    translate_persistence_error,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events as _AUDIT_EVENTS,
    environments as _ENVIRONMENTS,
    idempotency_receipts as _IDEMPOTENCY_RECEIPTS,
    outbox_intents as _OUTBOX_INTENTS,
    source_objects as _SOURCE_OBJECTS,
    source_revisions as _SOURCE_REVISIONS,
    source_revision_provenance as _SOURCE_PROVENANCE,
    workspaces as _WORKSPACES,
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
            return SourceObservationResult(
                source=SourceObject.model_validate(dict(source_row)),
                revision=SourceRevision.model_validate(dict(rev_row)),
                provenance=SourceRevisionProvenance.model_validate(dict(event_row)),
                replay=replay,
                claim=claim,
                result_reference=result_reference,
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
            values["revision_metadata"] = revision.revision_metadata.model_dump(mode="json") if revision.revision_metadata is not None else None
            values["original_reference"] = revision.original_reference.model_dump(mode="json") if revision.original_reference is not None else None
            await self._uow._execute(postgresql_insert(_SOURCE_REVISIONS).values(**values).on_conflict_do_nothing(constraint="uq_source_revisions_digest"))
            rev_row = (await self._uow._execute(rev_query)).mappings().one()
        await self._uow._execute(
            postgresql_insert(_SOURCE_PROVENANCE).values(**provenance.model_dump(mode="python")).on_conflict_do_nothing(
                index_elements=[_SOURCE_PROVENANCE.c.workspace_id, _SOURCE_PROVENANCE.c.environment_id, _SOURCE_PROVENANCE.c.producer_kind, _SOURCE_PROVENANCE.c.producer_reference, _SOURCE_PROVENANCE.c.event_identity]
            )
        )
        event_row = (await self._uow._execute(event_query)).mappings().one()
        if event_row.event_digest != provenance.event_digest:
            await self._uow._fail(ObservationIntegrityConflictError("Source observation conflicts."))
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
        return SourceRevision.model_validate(dict(row)) if row is not None else None


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


__all__ = [
    "PostgreSQLPersistence",
    "PostgreSQLAuditReader",
    "PostgreSQLTenantUnitOfWork",
    "PostgreSQLTenantUnitOfWorkFactory",
    "PostgreSQLTransactionProbe",
    "PostgreSQLTransactionStage",
    "translate_persistence_error",
]
