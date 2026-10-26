"""PostgreSQL adapter for governed Google connection authority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
)
from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PostgreSQLUUID
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionDeniedError,
    GoogleConnectionMutationResult,
    GoogleReadCommand,
    build_google_connection_audit_event,
    connection_matches_approval,
    context_allows_google_connection_registration,
    is_valid_google_connection_successor,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.domain.audit import AuditEvent
from spine.domain.common import ContextOrigin
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionApproval,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_mappings import audit_events


_google_connection_metadata = MetaData(schema="spine")


google_connection_versions = Table(
    "google_connection_versions",
    _google_connection_metadata,
    Column("connection_version_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("connection_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("approval_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("predecessor_version_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("service_principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("credential_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("grant_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column(
        "approved_resource_scope_references",
        ARRAY(PostgreSQLUUID(as_uuid=True)),
        nullable=False,
    ),
    Column("authorization_generation", BigInteger, nullable=False),
    Column("configuration_generation", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("command_digest", String(64), nullable=False),
    Column("audit_event_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)

google_connection_approvals = Table(
    "google_connection_approvals",
    _google_connection_metadata,
    Column("approval_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("service_principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("credential_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("grant_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column(
        "approved_resource_scope_references",
        ARRAY(PostgreSQLUUID(as_uuid=True)),
        nullable=False,
    ),
    Column("authorization_generation", BigInteger, nullable=False),
    Column("configuration_generation", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)

_BIND_SCOPE = text(
    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true)"
)


class PostgreSQLGoogleConnectionRepository:
    """Append-only PostgreSQL connection repository protected by tenant RLS."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier

    async def register(
        self,
        command: GoogleConnectionCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnectionMutationResult:
        try:
            trusted = self._context_verifier.verify(context)
            connection = command.connection
            if not context_allows_google_connection_registration(trusted, connection):
                raise GoogleConnectionConflictError()
            parameters = _scope_parameters(trusted.scope)
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(_BIND_SCOPE, parameters)
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:connection_lock, 0))"
                        ),
                        {"connection_lock": str(connection.connection_id)},
                    )
                    approval_row = (
                        await session.execute(
                            select(google_connection_approvals).where(
                                google_connection_approvals.c.approval_id
                                == connection.approval_id,
                                google_connection_approvals.c.workspace_id
                                == connection.workspace_id,
                                google_connection_approvals.c.environment_id
                                == connection.environment_id,
                            )
                        )
                    ).mappings().one_or_none()
                    approval = (
                        _approval_from_row(approval_row)
                        if approval_row is not None
                        else None
                    )
                    if not connection_matches_approval(connection, approval):
                        raise GoogleConnectionConflictError()
                    replay_row = (
                        await session.execute(
                            select(google_connection_versions).where(
                                google_connection_versions.c.workspace_id
                                == connection.workspace_id,
                                google_connection_versions.c.environment_id
                                == connection.environment_id,
                                google_connection_versions.c.idempotency_key
                                == command.idempotency_key,
                            )
                        )
                    ).mappings().one_or_none()
                    if replay_row is not None:
                        if replay_row.command_digest != command.digest():
                            raise GoogleConnectionConflictError()
                        return GoogleConnectionMutationResult(
                            connection=_connection_from_row(replay_row),
                            audit=build_google_connection_audit_event(command, trusted),
                            replay=True,
                        )
                    current_row = (
                        await session.execute(
                            select(google_connection_versions)
                            .where(
                                google_connection_versions.c.workspace_id
                                == connection.workspace_id,
                                google_connection_versions.c.environment_id
                                == connection.environment_id,
                                google_connection_versions.c.connection_id
                                == connection.connection_id,
                            )
                            .order_by(google_connection_versions.c.version.desc())
                            .limit(1)
                        )
                    ).mappings().one_or_none()
                    current = (
                        _connection_from_row(current_row)
                        if current_row is not None
                        else None
                    )
                    if not is_valid_google_connection_successor(current, connection):
                        raise GoogleConnectionConflictError()
                    audit = build_google_connection_audit_event(command, trusted)
                    await session.execute(
                        postgresql_insert(audit_events).values(
                            **_audit_event_values(audit)
                        )
                    )
                    await session.execute(
                        insert(google_connection_versions).values(
                            **_connection_values(command)
                        )
                    )
            return GoogleConnectionMutationResult(connection=connection, audit=audit)
        except asyncio.CancelledError:
            raise
        except GoogleConnectionConflictError:
            raise
        except Exception:
            raise GoogleConnectionConflictError() from None

    @asynccontextmanager
    async def admit_read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> AsyncIterator[GoogleConnection]:
        try:
            trusted = self._context_verifier.verify(context)
            if (
                trusted.origin is not ContextOrigin.WORKER
                or not isinstance(trusted.scope, EnvironmentScope)
                or trusted.service_principal_id is None
                or trusted.purpose.value != "google_connector"
                or trusted.operation.value != "google_connection_read"
            ):
                raise GoogleConnectionDeniedError()
        except asyncio.CancelledError:
            raise
        except GoogleConnectionDeniedError:
            raise
        except Exception:
            raise GoogleConnectionDeniedError() from None
        async with self._session_factory() as session:
            async with session.begin():
                try:
                    await session.execute(_BIND_SCOPE, _scope_parameters(trusted.scope))
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock_shared("
                            "hashtextextended(:connection_lock, 0))"
                        ),
                        {"connection_lock": str(command.connection_id)},
                    )
                    row = (
                        await session.execute(
                            select(google_connection_versions)
                            .where(
                                google_connection_versions.c.connection_id
                                == command.connection_id,
                                google_connection_versions.c.workspace_id
                                == trusted.scope.workspace_id,
                                google_connection_versions.c.environment_id
                                == trusted.scope.environment_id,
                                google_connection_versions.c.service_principal_id
                                == trusted.service_principal_id,
                                google_connection_versions.c.authorization_generation
                                == command.authorization_generation,
                                google_connection_versions.c.configuration_generation
                                == command.configuration_generation,
                                google_connection_versions.c.status
                                == GoogleConnectionStatus.ACTIVE.value,
                                google_connection_versions.c.approved_resource_scope_references.any(
                                    command.resource_scope_reference.value
                                ),
                            )
                            .order_by(google_connection_versions.c.version.desc())
                            .limit(1)
                        )
                    ).mappings().one_or_none()
                    if row is None:
                        raise GoogleConnectionDeniedError()
                    latest_version = await session.scalar(
                        select(google_connection_versions.c.version)
                        .where(
                            google_connection_versions.c.connection_id
                            == command.connection_id,
                            google_connection_versions.c.workspace_id
                            == trusted.scope.workspace_id,
                            google_connection_versions.c.environment_id
                            == trusted.scope.environment_id,
                        )
                        .order_by(google_connection_versions.c.version.desc())
                        .limit(1)
                    )
                    if latest_version != row.version:
                        raise GoogleConnectionDeniedError()
                    connection = _connection_from_row(row)
                except asyncio.CancelledError:
                    raise
                except GoogleConnectionDeniedError:
                    raise
                except Exception:
                    raise GoogleConnectionDeniedError() from None
                yield connection

def _scope_parameters(scope: EnvironmentScope) -> dict[str, str]:
    return {
        "workspace_id": str(scope.workspace_id),
        "environment_id": str(scope.environment_id),
    }

def _connection_values(command: GoogleConnectionCommand) -> dict[str, object]:
    connection = command.connection
    return {
        "connection_version_id": connection.connection_version_id,
        "connection_id": connection.connection_id,
        "approval_id": connection.approval_id,
        "version": connection.version,
        "predecessor_version_id": connection.predecessor_version_id,
        "workspace_id": connection.workspace_id,
        "environment_id": connection.environment_id,
        "service_principal_id": connection.service_principal_id,
        "credential_reference": connection.credential_reference.value,
        "grant_reference": connection.grant_reference.value,
        "approved_resource_scope_references": [
            item.value for item in connection.approved_resource_scope_references
        ],
        "authorization_generation": connection.authorization_generation,
        "configuration_generation": connection.configuration_generation,
        "status": connection.status.value,
        "idempotency_key": command.idempotency_key,
        "command_digest": command.digest(),
        "audit_event_id": command.audit_event_id,
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


def _connection_from_row(row) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=row.connection_version_id,
        connection_id=row.connection_id,
        approval_id=row.approval_id,
        version=row.version,
        predecessor_version_id=row.predecessor_version_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        service_principal_id=row.service_principal_id,
        credential_reference=OpaqueGoogleReference(value=row.credential_reference),
        grant_reference=OpaqueGoogleReference(value=row.grant_reference),
        approved_resource_scope_references=tuple(
            OpaqueGoogleReference(value=value)
            for value in row.approved_resource_scope_references
        ),
        authorization_generation=row.authorization_generation,
        configuration_generation=row.configuration_generation,
        status=GoogleConnectionStatus(row.status),
    )


def _approval_from_row(row) -> GoogleConnectionApproval:
    return GoogleConnectionApproval(
        approval_id=row.approval_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        service_principal_id=row.service_principal_id,
        credential_reference=OpaqueGoogleReference(value=row.credential_reference),
        grant_reference=OpaqueGoogleReference(value=row.grant_reference),
        approved_resource_scope_references=tuple(
            OpaqueGoogleReference(value=value)
            for value in row.approved_resource_scope_references
        ),
        authorization_generation=row.authorization_generation,
        configuration_generation=row.configuration_generation,
        status=GoogleConnectionStatus(row.status),
    )


__all__ = [
    "PostgreSQLGoogleConnectionRepository",
    "google_connection_approvals",
    "google_connection_versions",
]
