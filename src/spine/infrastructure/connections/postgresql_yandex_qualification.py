"""Atomic operator persistence for isolated Yandex qualification authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from uuid import UUID

from sqlalchemy import insert, select, text

from spine.application.connections.management import (
    ProviderConnectionConfigurationResult,
)
from spine.application.diagnostics import AuditEvent, AuditEventRegistry
from spine.application.diagnostics.audit import validate_audit_event_for_context
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.yandex_qualification_connection import (
    YandexQualificationConnectionPlan,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionApproval,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.infrastructure.connections.postgresql_provider_management import (
    audit_event_values,
    provider_connection_approvals,
    provider_connection_inspection_from_row,
    provider_connection_version_values,
    provider_connection_versions,
)
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_mappings import audit_events


class YandexQualificationBootstrapConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "Yandex qualification bootstrap conflicts with canonical state"
        )


class PostgreSQLYandexQualificationConnectionRepository:
    """Commit approval, lineage audits and initial connection as one transaction."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier

    async def bootstrap(
        self,
        *,
        plan: YandexQualificationConnectionPlan,
        result: ProviderConnectionConfigurationResult,
        authority_audit: AuditEvent,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult:
        trusted = self._context_verifier.verify(context)
        if (
            not isinstance(trusted.scope, EnvironmentScope)
            or trusted.scope.workspace_id != plan.approval.workspace_id
            or trusted.scope.environment_id != plan.approval.environment_id
            or trusted.acting_subject_id is None
            or trusted.purpose.value != "source_connection_control"
            or trusted.operation.value != "provider_connection_configure"
        ):
            raise YandexQualificationBootstrapConflictError()
        registry = AuditEventRegistry.with_default_families()
        canonical_authority_audit = validate_audit_event_for_context(
            authority_audit, registry=registry, context=trusted
        )
        canonical_connection_audit = validate_audit_event_for_context(
            result.audit, registry=registry, context=trusted
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"
                        ),
                        {"lock": str(plan.request.connection_id)},
                    )
                    approval_rows = (
                        (
                            await session.execute(
                                select(provider_connection_approvals).where(
                                    provider_connection_approvals.c.workspace_id
                                    == plan.approval.workspace_id,
                                    provider_connection_approvals.c.environment_id
                                    == plan.approval.environment_id,
                                    provider_connection_approvals.c.provider_kind
                                    == ProviderKind.YANDEX_DISK.value,
                                    provider_connection_approvals.c.service_principal_id
                                    == plan.approval.service_principal_id,
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    version_rows = (
                        (
                            await session.execute(
                                select(provider_connection_versions).where(
                                    provider_connection_versions.c.connection_id
                                    == plan.request.connection_id
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    audit_ids = (
                        canonical_authority_audit.audit_event_id,
                        canonical_connection_audit.audit_event_id,
                    )
                    audit_rows = (
                        (
                            await session.execute(
                                select(audit_events).where(
                                    audit_events.c.audit_event_id.in_(audit_ids)
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    if approval_rows or version_rows or audit_rows:
                        if not self._is_exact_replay(
                            plan,
                            result,
                            canonical_authority_audit,
                            approval_rows,
                            version_rows,
                            audit_rows,
                        ):
                            raise YandexQualificationBootstrapConflictError()
                        return replace(result, replay=True)
                    await session.execute(
                        insert(provider_connection_approvals).values(
                            **_approval_values(plan.approval, plan.request.occurred_at)
                        )
                    )
                    await session.execute(
                        insert(audit_events),
                        [
                            audit_event_values(canonical_authority_audit),
                            audit_event_values(canonical_connection_audit),
                        ],
                    )
                    await session.execute(
                        insert(provider_connection_versions).values(
                            **provider_connection_version_values(
                                plan.request, result, trusted
                            )
                        )
                    )
                    return result
        except asyncio.CancelledError:
            raise
        except YandexQualificationBootstrapConflictError:
            raise
        except Exception:
            raise YandexQualificationBootstrapConflictError() from None

    @staticmethod
    def _is_exact_replay(
        plan, result, authority_audit, approval_rows, version_rows, audit_rows
    ) -> bool:
        if len(approval_rows) != 1 or len(version_rows) != 1 or len(audit_rows) != 2:
            return False
        if _approval_from_row(approval_rows[0]) != plan.approval:
            return False
        if (
            provider_connection_inspection_from_row(version_rows[0])
            != result.inspection
        ):
            return False
        expected = {
            authority_audit.audit_event_id: audit_event_values(authority_audit),
            result.audit.audit_event_id: audit_event_values(result.audit),
        }
        return all(
            row.audit_event_id in expected
            and all(
                row[key] == value for key, value in expected[row.audit_event_id].items()
            )
            for row in audit_rows
        )


class PostgreSQLYandexQualificationBinaryReadRepository:
    """Provision the exact #789 successor through a runtime-only SQL capability."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier

    async def get_current(
        self, connection_id: UUID, context: TrustedPersistenceContext
    ) -> ProviderConnection:
        trusted = self._trusted_context(context)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                        "set_config('spine.environment_id', :environment_id, true)"
                    ),
                    {
                        "workspace_id": str(trusted.scope.workspace_id),
                        "environment_id": str(trusted.scope.environment_id),
                    },
                )
                row = (
                    await session.execute(
                        select(provider_connection_versions)
                        .where(
                            provider_connection_versions.c.connection_id
                            == connection_id
                        )
                        .order_by(provider_connection_versions.c.version.desc())
                        .limit(1)
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise YandexQualificationBootstrapConflictError()
                return provider_connection_inspection_from_row(row).connection

    async def provision(
        self,
        *,
        plan: YandexQualificationConnectionPlan,
        result: ProviderConnectionConfigurationResult,
        authority_audit: AuditEvent,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult:
        trusted = self._trusted_context(context)
        if (
            trusted.scope.workspace_id != plan.approval.workspace_id
            or trusted.scope.environment_id != plan.approval.environment_id
            or result.inspection.connection.approval_id != plan.approval.approval_id
            or result.inspection.connection.connection_id != plan.request.connection_id
            or result.inspection.connection.valid_until != plan.approval.valid_until
            or result.inspection.connection.approved_manifest_digest
            != plan.approval.approved_manifest_digest
            or result.inspection.connection.capabilities
            != tuple(
                sorted(plan.observation.capabilities, key=lambda item: item.kind.value)
            )
        ):
            raise YandexQualificationBootstrapConflictError()
        registry = AuditEventRegistry.with_default_families()
        canonical_authority_audit = validate_audit_event_for_context(
            authority_audit, registry=registry, context=trusted
        )
        canonical_connection_audit = validate_audit_event_for_context(
            result.audit, registry=registry, context=trusted
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                            "set_config('spine.environment_id', :environment_id, true)"
                        ),
                        {
                            "workspace_id": str(trusted.scope.workspace_id),
                            "environment_id": str(trusted.scope.environment_id),
                        },
                    )
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"
                        ),
                        {"lock": str(plan.request.connection_id)},
                    )
                    version_rows = (
                        await session.execute(
                            select(provider_connection_versions).where(
                                provider_connection_versions.c.idempotency_key
                                == plan.request.idempotency_key
                            )
                        )
                    ).mappings().all()
                    if version_rows:
                        database_now = await session.scalar(
                            text("SELECT clock_timestamp()")
                        )
                        if (
                            database_now is None
                            or plan.approval.valid_until is None
                            or database_now >= plan.approval.valid_until
                        ):
                            raise YandexQualificationBootstrapConflictError()
                        approval_rows = (
                            await session.execute(
                                select(provider_connection_approvals).where(
                                    provider_connection_approvals.c.approval_id
                                    == plan.approval.approval_id
                                )
                            )
                        ).mappings().all()
                        audit_rows = (
                            await session.execute(
                                select(audit_events).where(
                                    audit_events.c.audit_event_id.in_(
                                        (
                                            canonical_authority_audit.audit_event_id,
                                            canonical_connection_audit.audit_event_id,
                                        )
                                    )
                                )
                            )
                        ).mappings().all()
                        if not PostgreSQLYandexQualificationConnectionRepository._is_exact_replay(
                            plan,
                            result,
                            canonical_authority_audit,
                            approval_rows,
                            version_rows,
                            audit_rows,
                        ):
                            raise YandexQualificationBootstrapConflictError()
                        return replace(result, replay=True)

                    inserted = await session.scalar(
                        text(
                            "SELECT spine.provision_yandex_qualification_binary_read_approval("
                            ":approval_id, :workspace_id, :environment_id, :connection_id, "
                            ":predecessor_version_id, :service_principal_id, "
                            ":credential_reference, :grant_reference, "
                            ":resource_scope_reference, :authorization_generation, "
                            ":configuration_generation, :manifest_digest, "
                            ":valid_until, :recorded_at, :connection_version_id, "
                            ":receipt_id, :idempotency_key, :command_digest, "
                            ":authority_audit_event_id, :connection_audit_event_id, "
                            ":acting_subject_id, :trace_id, "
                            "CAST(:authority_payload AS jsonb), "
                            "CAST(:connection_payload AS jsonb))"
                        ),
                        {
                            "approval_id": plan.approval.approval_id,
                            "workspace_id": plan.approval.workspace_id,
                            "environment_id": plan.approval.environment_id,
                            "connection_id": plan.request.connection_id,
                            "predecessor_version_id": (
                                plan.request.expected_predecessor_version_id
                            ),
                            "service_principal_id": plan.approval.service_principal_id,
                            "credential_reference": (
                                plan.approval.credential_reference.value
                            ),
                            "grant_reference": plan.approval.grant_reference.value,
                            "resource_scope_reference": (
                                plan.approval.approved_resource_scope_references[0].value
                            ),
                            "authorization_generation": (
                                plan.approval.authorization_generation
                            ),
                            "configuration_generation": (
                                plan.approval.configuration_generation
                            ),
                            "manifest_digest": plan.approval.approved_manifest_digest,
                            "valid_until": plan.approval.valid_until,
                            "recorded_at": plan.request.occurred_at,
                            "connection_version_id": (
                                result.inspection.connection.connection_version_id
                            ),
                            "receipt_id": result.receipt_id,
                            "idempotency_key": plan.request.idempotency_key,
                            "command_digest": provider_connection_version_values(
                                plan.request, result, trusted
                            )["command_digest"],
                            "authority_audit_event_id": (
                                canonical_authority_audit.audit_event_id
                            ),
                            "connection_audit_event_id": (
                                canonical_connection_audit.audit_event_id
                            ),
                            "acting_subject_id": trusted.acting_subject_id,
                            "trace_id": trusted.trace_id,
                            "authority_payload": json.dumps(
                                audit_event_values(canonical_authority_audit)["payload"],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            "connection_payload": json.dumps(
                                audit_event_values(canonical_connection_audit)["payload"],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    )
                    if inserted not in {True, False}:
                        raise YandexQualificationBootstrapConflictError()
                    return replace(result, replay=not inserted)
        except asyncio.CancelledError:
            raise
        except YandexQualificationBootstrapConflictError:
            raise
        except Exception:
            raise YandexQualificationBootstrapConflictError() from None

    def _trusted_context(
        self, context: TrustedPersistenceContext
    ) -> TrustedPersistenceContext:
        trusted = self._context_verifier.verify(context)
        if (
            not isinstance(trusted.scope, EnvironmentScope)
            or trusted.acting_subject_id is None
            or trusted.purpose.value != "source_connection_control"
            or trusted.operation.value != "provider_connection_configure"
        ):
            raise YandexQualificationBootstrapConflictError()
        return trusted


def _approval_values(
    approval: ProviderConnectionApproval, recorded_at
) -> dict[str, object]:
    return {
        "approval_id": approval.approval_id,
        "workspace_id": approval.workspace_id,
        "environment_id": approval.environment_id,
        "service_principal_id": approval.service_principal_id,
        "provider_kind": approval.provider_kind.value,
        "credential_reference": approval.credential_reference.value,
        "grant_reference": approval.grant_reference.value,
        "approved_resource_scope_references": [
            item.value for item in approval.approved_resource_scope_references
        ],
        "authorization_generation": approval.authorization_generation,
        "configuration_generation": approval.configuration_generation,
        "status": approval.status.value,
        "valid_until": approval.valid_until,
        "approved_manifest_digest": approval.approved_manifest_digest,
        "recorded_at": recorded_at,
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
        valid_until=row.valid_until,
        approved_manifest_digest=row.approved_manifest_digest,
    )


__all__ = [
    "PostgreSQLYandexQualificationConnectionRepository",
    "PostgreSQLYandexQualificationBinaryReadRepository",
    "YandexQualificationBootstrapConflictError",
]
