"""PostgreSQL authority for provider-neutral connection management."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PostgreSQLUUID
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError

from spine.application.connections.management import (
    ProviderConnectionConfigurationAdmission,
    ProviderConnectionConfigurationRequest,
    ProviderConnectionConfigurationResult,
    ProviderConnectionInspection,
    ProviderConnectionManager,
    ProviderConnectionNotFoundError,
    ProviderRuntimeObservation,
    build_provider_connection_audit,
    provider_connection_command_digest,
    require_provider_connection_successor,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    OptimisticConflictError,
)
from spine.domain.common import ContextOrigin
from spine.domain.audit import AuditEvent
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderConnection,
    ProviderConnectionApproval,
    ProviderConnectionHealth,
    ProviderConnectionStatus,
    ProviderDiagnosticCode,
    ProviderKind,
)
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_mappings import audit_events


_metadata = MetaData(schema="spine")


provider_connection_approvals = Table(
    "provider_connection_approvals",
    _metadata,
    Column("approval_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("service_principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("provider_kind", String(32), nullable=False),
    Column("credential_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("grant_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column(
        "approved_resource_scope_references",
        ARRAY(PostgreSQLUUID(as_uuid=True)),
        nullable=False,
    ),
    Column("authorization_generation", BigInteger, nullable=False),
    Column("configuration_generation", BigInteger, nullable=False),
    Column("status", String(32), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)


provider_connection_versions = Table(
    "provider_connection_versions",
    _metadata,
    Column("connection_version_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("connection_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("approval_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("predecessor_version_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("service_principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("provider_kind", String(32), nullable=False),
    Column("credential_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("grant_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column(
        "approved_resource_scope_references",
        ARRAY(PostgreSQLUUID(as_uuid=True)),
        nullable=False,
    ),
    Column("authorization_generation", BigInteger, nullable=False),
    Column("configuration_generation", BigInteger, nullable=False),
    Column("status", String(32), nullable=False),
    Column("capabilities", JSONB, nullable=False),
    Column("health", String(32), nullable=False),
    Column("diagnostic_codes", ARRAY(String(64)), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("cursor_lag_seconds", BigInteger, nullable=True),
    Column("last_successful_reconciliation_at", DateTime(timezone=True), nullable=True),
    Column("receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("command_digest", String(64), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["approval_id"], ["spine.provider_connection_approvals.approval_id"]
    ),
    ForeignKeyConstraint(
        ["predecessor_version_id"],
        ["spine.provider_connection_versions.connection_version_id"],
    ),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)


_BIND_SCOPE = text(
    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true)"
)


class PostgreSQLProviderConnectionManagementRepository:
    """Append-only connection versions protected by tenant RLS and CAS."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier

    async def authorize_configuration(
        self,
        request: ProviderConnectionConfigurationRequest,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationAdmission:
        trusted = self._configuration_context(context)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(trusted.scope))
                replay = await _find_command(session, request, trusted)
                approval = await _find_approval(session, request.approval_id)
                if approval is None:
                    raise ProviderConnectionNotFoundError()
                if replay is not None:
                    return ProviderConnectionConfigurationAdmission(
                        approval=approval,
                        replay=replay,
                    )
                current = await _find_current(session, request.connection_id)
                require_provider_connection_successor(current, request, approval)
                return ProviderConnectionConfigurationAdmission(approval=approval)

    async def register(
        self,
        request: ProviderConnectionConfigurationRequest,
        approval: ProviderConnectionApproval,
        observation: ProviderRuntimeObservation,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult:
        trusted = self._configuration_context(context)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(_BIND_SCOPE, _scope_parameters(trusted.scope))
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:connection_lock, 0))"
                        ),
                        {"connection_lock": str(request.connection_id)},
                    )
                    replay = await _find_command(session, request, trusted)
                    if replay is not None:
                        return ProviderConnectionConfigurationResult(
                            receipt_id=replay.receipt_id,
                            inspection=replay.inspection,
                            audit=replay.audit,
                            replay=True,
                        )
                    canonical_approval = await _find_approval(
                        session, request.approval_id
                    )
                    if canonical_approval is None:
                        raise ProviderConnectionNotFoundError()
                    if canonical_approval != approval:
                        raise OptimisticConflictError()
                    current = await _find_current(session, request.connection_id)
                    require_provider_connection_successor(
                        current, request, canonical_approval
                    )
                    result = ProviderConnectionManager.build_result(
                        request=request,
                        approval=approval,
                        observation=observation,
                        context=trusted,
                        replay=False,
                    )
                    await session.execute(
                        postgresql_insert(audit_events).values(
                            **_audit_event_values(result.audit)
                        )
                    )
                    await session.execute(
                        insert(provider_connection_versions).values(
                            **_version_values(request, result, trusted)
                        )
                    )
                    return result
        except asyncio.CancelledError:
            raise
        except (
            IdempotencyConflictError,
            OptimisticConflictError,
            ProviderConnectionNotFoundError,
        ):
            raise
        except IntegrityError:
            raise OptimisticConflictError() from None

    async def list_current(
        self,
        context: TrustedPersistenceContext,
        *,
        after_connection_id: UUID | None = None,
        limit: int = 101,
    ) -> tuple[ProviderConnectionInspection, ...]:
        trusted = self._read_context(context)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(trusted.scope))
                ranked = select(
                    *provider_connection_versions.c,
                    func.row_number()
                    .over(
                        partition_by=provider_connection_versions.c.connection_id,
                        order_by=provider_connection_versions.c.version.desc(),
                    )
                    .label("current_rank"),
                ).subquery()
                statement = select(ranked).where(ranked.c.current_rank == 1)
                if after_connection_id is not None:
                    statement = statement.where(
                        ranked.c.connection_id > after_connection_id
                    )
                rows = (
                    await session.execute(
                        statement.order_by(ranked.c.connection_id).limit(limit)
                    )
                ).mappings().all()
                return tuple(_inspection_from_row(row) for row in rows)

    async def get_current(
        self,
        connection_id: UUID,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionInspection | None:
        trusted = self._read_context(context)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(trusted.scope))
                return await _find_current(session, connection_id)

    def _configuration_context(
        self, context: TrustedPersistenceContext
    ) -> TrustedPersistenceContext:
        trusted = self._context_verifier.verify(context)
        if not _allows_context(trusted, {"provider_connection_configure"}):
            raise ProviderConnectionNotFoundError()
        return trusted

    def _read_context(
        self, context: TrustedPersistenceContext
    ) -> TrustedPersistenceContext:
        trusted = self._context_verifier.verify(context)
        if not _allows_context(
            trusted,
            {
                "provider_connection_configure",
                "provider_connection_inspect",
                "provider_connection_list",
            },
        ):
            raise ProviderConnectionNotFoundError()
        return trusted


def _allows_context(
    context: TrustedPersistenceContext, operations: set[str]
) -> bool:
    return (
        context.origin is ContextOrigin.INTERACTIVE
        and context.acting_subject_id is not None
        and isinstance(context.scope, EnvironmentScope)
        and context.purpose.value == "source_connection_control"
        and context.operation.value in operations
    )


def _scope_parameters(scope: EnvironmentScope) -> dict[str, str]:
    return {
        "workspace_id": str(scope.workspace_id),
        "environment_id": str(scope.environment_id),
    }


async def _find_approval(session, approval_id: UUID) -> ProviderConnectionApproval | None:
    row = (
        await session.execute(
            select(provider_connection_approvals).where(
                provider_connection_approvals.c.approval_id == approval_id,
                provider_connection_approvals.c.status
                == ProviderConnectionStatus.ACTIVE.value,
            )
        )
    ).mappings().one_or_none()
    return _approval_from_row(row) if row is not None else None


async def _find_command(
    session,
    request: ProviderConnectionConfigurationRequest,
    context: TrustedPersistenceContext,
) -> ProviderConnectionConfigurationResult | None:
    row = (
        await session.execute(
            select(provider_connection_versions).where(
                provider_connection_versions.c.idempotency_key
                == request.idempotency_key
            )
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    if row.command_digest != provider_connection_command_digest(
        request,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
    ):
        raise IdempotencyConflictError()
    return ProviderConnectionConfigurationResult(
        receipt_id=row.receipt_id,
        inspection=_inspection_from_row(row),
        audit=build_provider_connection_audit(
            request=replace(request, occurred_at=row.recorded_at),
            connection=_inspection_from_row(row).connection,
            context=context,
            receipt_id=row.receipt_id,
        ),
        replay=True,
    )


async def _find_current(session, connection_id: UUID) -> ProviderConnectionInspection | None:
    row = (
        await session.execute(
            select(provider_connection_versions)
            .where(provider_connection_versions.c.connection_id == connection_id)
            .order_by(provider_connection_versions.c.version.desc())
            .limit(1)
        )
    ).mappings().one_or_none()
    return _inspection_from_row(row) if row is not None else None


def _version_values(
    request: ProviderConnectionConfigurationRequest,
    result: ProviderConnectionConfigurationResult,
    context: TrustedPersistenceContext,
) -> dict[str, object]:
    item = result.inspection
    connection = item.connection
    return {
        "connection_version_id": connection.connection_version_id,
        "connection_id": connection.connection_id,
        "approval_id": connection.approval_id,
        "version": connection.version,
        "predecessor_version_id": connection.predecessor_version_id,
        "workspace_id": connection.workspace_id,
        "environment_id": connection.environment_id,
        "service_principal_id": connection.service_principal_id,
        "provider_kind": connection.provider_kind.value,
        "credential_reference": connection.credential_reference.value,
        "grant_reference": connection.grant_reference.value,
        "approved_resource_scope_references": [
            item.value for item in connection.approved_resource_scope_references
        ],
        "authorization_generation": connection.authorization_generation,
        "configuration_generation": connection.configuration_generation,
        "status": connection.status.value,
        "capabilities": [
            capability.model_dump(mode="json") for capability in connection.capabilities
        ],
        "health": item.health.value,
        "diagnostic_codes": [code.value for code in item.diagnostic_codes],
        "observed_at": item.observed_at,
        "cursor_lag_seconds": item.cursor_lag_seconds,
        "last_successful_reconciliation_at": item.last_successful_reconciliation_at,
        "receipt_id": result.receipt_id,
        "idempotency_key": request.idempotency_key,
        "command_digest": provider_connection_command_digest(
            request,
            workspace_id=context.scope.workspace_id,
            environment_id=context.scope.environment_id,
        ),
        "recorded_at": request.occurred_at,
    }


def _audit_event_values(audit: AuditEvent) -> dict[str, object]:
    target = audit.target
    return {
        "audit_event_id": audit.audit_event_id,
        "workspace_id": audit.workspace_id,
        "environment_id": audit.environment_id,
        "event_type": audit.event_type,
        "schema_version": audit.schema_version,
        "origin": audit.origin.value,
        "acting_subject_id": audit.acting_subject_id,
        "service_principal_id": audit.service_principal_id,
        "trace_id": audit.trace_id,
        "correlation_id": audit.correlation_id,
        "causation_id": audit.causation_id,
        "occurred_at": audit.occurred_at,
        "appended_at": audit.occurred_at,
        "target_type": target.object_type if target else None,
        "target_id": target.object_id if target else None,
        "target_schema_version": target.schema_version if target else None,
        "outcome": audit.outcome.value,
        "reason": audit.reason,
        "producer_deduplication_id": audit.producer_deduplication_id,
        "payload": audit.payload_json(),
    }


def _approval_from_row(row) -> ProviderConnectionApproval:
    return ProviderConnectionApproval(
        approval_id=row.approval_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        service_principal_id=row.service_principal_id,
        provider_kind=ProviderKind(row.provider_kind),
        credential_reference=OpaqueProviderReference(value=row.credential_reference),
        grant_reference=OpaqueProviderReference(value=row.grant_reference),
        approved_resource_scope_references=tuple(
            OpaqueProviderReference(value=value)
            for value in row.approved_resource_scope_references
        ),
        authorization_generation=row.authorization_generation,
        configuration_generation=row.configuration_generation,
        status=ProviderConnectionStatus(row.status),
    )


def _inspection_from_row(row) -> ProviderConnectionInspection:
    connection = ProviderConnection(
        connection_version_id=row.connection_version_id,
        connection_id=row.connection_id,
        approval_id=row.approval_id,
        version=row.version,
        predecessor_version_id=row.predecessor_version_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        service_principal_id=row.service_principal_id,
        provider_kind=ProviderKind(row.provider_kind),
        credential_reference=OpaqueProviderReference(value=row.credential_reference),
        grant_reference=OpaqueProviderReference(value=row.grant_reference),
        approved_resource_scope_references=tuple(
            OpaqueProviderReference(value=value)
            for value in row.approved_resource_scope_references
        ),
        authorization_generation=row.authorization_generation,
        configuration_generation=row.configuration_generation,
        status=ProviderConnectionStatus(row.status),
        capabilities=tuple(
            ProviderCapability.model_validate(value) for value in row.capabilities
        ),
    )
    return ProviderConnectionInspection(
        connection=connection,
        health=ProviderConnectionHealth(row.health),
        diagnostic_codes=tuple(
            ProviderDiagnosticCode(value) for value in row.diagnostic_codes
        ),
        observed_at=row.observed_at,
        cursor_lag_seconds=row.cursor_lag_seconds,
        last_successful_reconciliation_at=row.last_successful_reconciliation_at,
    )


__all__ = [
    "PostgreSQLProviderConnectionManagementRepository",
    "provider_connection_approvals",
    "provider_connection_versions",
]
