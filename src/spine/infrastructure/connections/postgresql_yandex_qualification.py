"""Atomic operator persistence for isolated Yandex qualification authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace

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
    )


__all__ = [
    "PostgreSQLYandexQualificationConnectionRepository",
    "YandexQualificationBootstrapConflictError",
]
