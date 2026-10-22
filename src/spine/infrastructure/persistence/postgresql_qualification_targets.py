"""Restricted PostgreSQL adapter for qualification target bindings."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from spine.auth.qualification_targets import (
    ExternalSecretReference,
    QualificationBindingDeniedError,
    QualificationDatabaseAccess,
    QualificationTarget,
    QualificationTargetBinding,
    QualificationTargetBindingRequest,
    QualificationTargetRole,
    qualification_target_binding_digest,
)
from spine.auth.records import QualificationPrincipalKind
from spine.infrastructure.db.engine import SessionFactory


_BIND_RESOLUTION_SCOPE = text(
    "SELECT "
    "set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true)"
)
_RESOLVE_TARGET_BINDING = text(
    "SELECT * FROM spine.resolve_current_qualification_target_binding("
    ":binding_id, :binding_digest, :workspace_id, :environment_id)"
)


class PostgreSQLQualificationTargetBindingResolver:
    """Resolve one exact binding without exposing target inventory enumeration."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve(
        self, request: QualificationTargetBindingRequest
    ) -> QualificationTargetBinding:
        parameters = {
            "binding_id": str(request.binding_id),
            "binding_digest": qualification_target_binding_digest(request),
            "workspace_id": str(request.workspace_id),
            "environment_id": str(request.environment_id),
        }
        try:
            session = self._session_factory()
            async with session:
                async with session.begin():
                    await session.execute(_BIND_RESOLUTION_SCOPE, parameters)
                    row = (
                        await session.execute(_RESOLVE_TARGET_BINDING, parameters)
                    ).mappings().one_or_none()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QualificationBindingDeniedError() from None
        if row is None:
            raise QualificationBindingDeniedError()
        try:
            binding = QualificationTargetBinding(
                binding_id=row.binding_id,
                binding_series_id=row.binding_series_id,
                binding_version=row.binding_version,
                predecessor_binding_id=row.predecessor_binding_id,
                authority_binding_id=row.authority_binding_id,
                authority_binding_version=row.authority_binding_version,
                qualification_id=row.qualification_id,
                workspace_id=row.workspace_id,
                environment_id=row.environment_id,
                target=QualificationTarget(row.target),
                target_reference=row.target_reference,
                target_scope_reference=row.target_scope_reference,
                principal_kind=QualificationPrincipalKind(row.principal_kind),
                principal_id=row.principal_id,
                purpose=row.purpose,
                operation=row.operation,
                alias_reference=row.alias_reference,
                secret=ExternalSecretReference(
                    provider_reference=row.secret_provider_reference,
                    secret_reference=row.secret_reference,
                ),
                database_role_reference=row.database_role_reference,
                target_role=QualificationTargetRole(row.target_role),
                database_access=QualificationDatabaseAccess(row.database_access),
                access_policy_references=tuple(row.access_policy_references),
            )
            if qualification_target_binding_digest(binding) != parameters["binding_digest"]:
                raise QualificationBindingDeniedError()
            return binding
        except Exception:
            raise QualificationBindingDeniedError() from None


__all__ = ["PostgreSQLQualificationTargetBindingResolver"]
