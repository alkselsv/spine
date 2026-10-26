"""PostgreSQL adapter for governed Google connection authority."""

from __future__ import annotations

import asyncio

from sqlalchemy import BigInteger, Column, DateTime, ForeignKeyConstraint, String, Table
from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PostgreSQLUUID

from spine.application.connections.google import (
    GoogleConnectionAuditSnapshot,
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionDeniedError,
    GoogleConnectionMutationResult,
    GoogleReadCommand,
    google_connection_audit_snapshot,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_mappings import metadata


google_connection_versions = Table(
    "google_connection_versions",
    metadata,
    Column("connection_version_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("connection_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("predecessor_version_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("service_principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("credential_reference", String(224), nullable=False),
    Column("grant_reference", String(224), nullable=False),
    Column("approved_resource_scope_references", ARRAY(String(224)), nullable=False),
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

google_connection_audit_events = Table(
    "google_connection_audit_events",
    metadata,
    Column("audit_event_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("connection_version_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("connection_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("service_principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("authorization_generation", BigInteger, nullable=False),
    Column("configuration_generation", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("resource_scope_references", ARRAY(String(224)), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
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
            if not _context_allows_registration(trusted, connection):
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
                        return await self._result_for_row(
                            session, replay_row, replay=True
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
                    if not _valid_successor(current, connection):
                        raise GoogleConnectionConflictError()
                    await session.execute(
                        insert(google_connection_versions).values(
                            **_connection_values(command)
                        )
                    )
                    audit = google_connection_audit_snapshot(command)
                    await session.execute(
                        insert(google_connection_audit_events).values(
                            **_audit_values(audit)
                        )
                    )
            return GoogleConnectionMutationResult(connection=connection, audit=audit)
        except asyncio.CancelledError:
            raise
        except GoogleConnectionConflictError:
            raise
        except Exception:
            raise GoogleConnectionConflictError() from None

    async def resolve_for_read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnection:
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
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(_BIND_SCOPE, _scope_parameters(trusted.scope))
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
            return _connection_from_row(row)
        except asyncio.CancelledError:
            raise
        except GoogleConnectionDeniedError:
            raise
        except Exception:
            raise GoogleConnectionDeniedError() from None

    async def _result_for_row(self, session, row, *, replay: bool):
        audit_row = (
            await session.execute(
                select(google_connection_audit_events).where(
                    google_connection_audit_events.c.audit_event_id
                    == row.audit_event_id
                )
            )
        ).mappings().one()
        audit = GoogleConnectionAuditSnapshot(
            audit_event_id=audit_row.audit_event_id,
            connection_id=audit_row.connection_id,
            connection_version_id=audit_row.connection_version_id,
            workspace_id=audit_row.workspace_id,
            environment_id=audit_row.environment_id,
            service_principal_id=audit_row.service_principal_id,
            authorization_generation=audit_row.authorization_generation,
            configuration_generation=audit_row.configuration_generation,
            status=audit_row.status,
            resource_scope_references=tuple(audit_row.resource_scope_references),
        )
        return GoogleConnectionMutationResult(
            connection=_connection_from_row(row), audit=audit, replay=replay
        )


def _scope_parameters(scope: EnvironmentScope) -> dict[str, str]:
    return {
        "workspace_id": str(scope.workspace_id),
        "environment_id": str(scope.environment_id),
    }


def _context_allows_registration(
    context: TrustedPersistenceContext, connection: GoogleConnection
) -> bool:
    return (
        isinstance(context.scope, EnvironmentScope)
        and context.purpose.value == "google_connector"
        and context.operation.value == "google_connection_register"
        and context.scope.workspace_id == connection.workspace_id
        and context.scope.environment_id == connection.environment_id
        and (
            context.origin is ContextOrigin.INTERACTIVE
            or context.service_principal_id == connection.service_principal_id
        )
    )


def _valid_successor(
    current: GoogleConnection | None, candidate: GoogleConnection
) -> bool:
    if current is None:
        return candidate.version == 1 and candidate.predecessor_version_id is None
    return (
        candidate.version == current.version + 1
        and candidate.predecessor_version_id == current.connection_version_id
        and candidate.authorization_generation >= current.authorization_generation
        and candidate.configuration_generation >= current.configuration_generation
        and (
            candidate.authorization_generation > current.authorization_generation
            or candidate.configuration_generation > current.configuration_generation
        )
    )


def _connection_values(command: GoogleConnectionCommand) -> dict[str, object]:
    connection = command.connection
    return {
        "connection_version_id": connection.connection_version_id,
        "connection_id": connection.connection_id,
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


def _audit_values(audit: GoogleConnectionAuditSnapshot) -> dict[str, object]:
    return {
        "audit_event_id": audit.audit_event_id,
        "connection_version_id": audit.connection_version_id,
        "connection_id": audit.connection_id,
        "workspace_id": audit.workspace_id,
        "environment_id": audit.environment_id,
        "service_principal_id": audit.service_principal_id,
        "authorization_generation": audit.authorization_generation,
        "configuration_generation": audit.configuration_generation,
        "status": audit.status,
        "resource_scope_references": list(audit.resource_scope_references),
    }


def _connection_from_row(row) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=row.connection_version_id,
        connection_id=row.connection_id,
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


__all__ = [
    "PostgreSQLGoogleConnectionRepository",
    "google_connection_audit_events",
    "google_connection_versions",
]
