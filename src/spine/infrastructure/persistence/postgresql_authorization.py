"""PostgreSQL authorization-directory adapter."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import text

from spine.auth.contracts import (
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationResolution,
    AuthorizationRole,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.auth.directory import RepositoryAuthorizationDirectory
from spine.auth.errors import AuthorizationUnavailableError
from spine.auth.records import CurrentAuthorizationSnapshot
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_errors import (
    translate_persistence_error,
)


_BIND_RESOLUTION_SCOPE = text(
    "SELECT "
    "set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true)"
)
_RESOLVE_CURRENT_SNAPSHOT = text(
    "SELECT acting_subject_id, authorization_generation, effective_team_ids "
    "FROM spine.resolve_current_authorization_snapshot("
    ":issuer, :subject, :workspace_id, :environment_id)"
)
_RESOLVE_CURRENT_HUMAN_AUTHORITY = text(
    "SELECT spine.resolve_current_human_authority("
    ":principal_id, :workspace_id, :environment_id)"
)
_RESOLVE_CURRENT_SERVICE_AUTHORITY = text(
    "SELECT spine.resolve_current_service_authority("
    ":principal_id, :workspace_id, :environment_id, :purpose, :operation)"
)


class PostgreSQLAuthorizationSnapshotRepository:
    """Resolve one alias/scope in an Issue #8-style owned read transaction.

    Authorization precedes trusted-context issuance, so this adapter cannot open
    the tenant Unit of Work whose verified acting subject is this lookup's output.
    It follows the same one-session transaction, cleanup, cancellation and safe
    error-translation conventions without exposing a second generic UoW.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve_current_snapshot(
        self,
        alias: AuthenticationAlias,
        requested_scope: RequestedAuthorizationScope,
    ) -> CurrentAuthorizationSnapshot | None:
        try:
            session = self._session_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AuthorizationUnavailableError() from None
        try:
            async with session:
                async with session.begin():
                    parameters = {
                        "issuer": alias.issuer,
                        "subject": alias.subject,
                        "workspace_id": str(requested_scope.workspace_id),
                        "environment_id": str(requested_scope.environment_id),
                    }
                    await session.execute(_BIND_RESOLUTION_SCOPE, parameters)
                    result = await session.execute(
                        _RESOLVE_CURRENT_SNAPSHOT,
                        parameters,
                    )
                    row = result.mappings().one_or_none()
            if row is None:
                return None
            return CurrentAuthorizationSnapshot(
                canonical_human_identity_id=row.acting_subject_id,
                workspace_id=requested_scope.workspace_id,
                environment_id=requested_scope.environment_id,
                roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
                authorization_generation=row.authorization_generation,
                effective_team_ids=frozenset(
                    row.effective_team_ids or ()
                ),
            )
        except asyncio.CancelledError:
            raise
        except AuthorizationUnavailableError:
            raise
        except Exception as error:
            translate_persistence_error(error)
            raise AuthorizationUnavailableError() from None


class PostgreSQLAuthorizationDirectory:
    """Public #69 directory contract backed by the restricted runtime role."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._directory = RepositoryAuthorizationDirectory(
            PostgreSQLAuthorizationSnapshotRepository(session_factory)
        )

    async def resolve_request_authority(
        self,
        authenticated_alias: AuthenticatedAlias,
        requested_scope: RequestedAuthorizationScope,
        route_policy: RouteAuthorizationPolicy,
    ) -> AuthorizationResolution:
        return await self._directory.resolve_request_authority(
            authenticated_alias,
            requested_scope,
            route_policy,
        )


class PostgreSQLQualificationAuthorityResolver:
    """Resolve current human/service authority without exposing binding records."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve_human_authority(
        self,
        *,
        principal_id: UUID,
        requested_scope: RequestedAuthorizationScope,
    ) -> int | None:
        return await self._resolve(
            _RESOLVE_CURRENT_HUMAN_AUTHORITY,
            {
                "principal_id": str(principal_id),
                "workspace_id": str(requested_scope.workspace_id),
                "environment_id": str(requested_scope.environment_id),
            },
            requested_scope,
        )

    async def resolve_service_authority(
        self,
        *,
        principal_id: UUID,
        requested_scope: RequestedAuthorizationScope,
        purpose: str,
        operation: str,
    ) -> int | None:
        return await self._resolve(
            _RESOLVE_CURRENT_SERVICE_AUTHORITY,
            {
                "principal_id": str(principal_id),
                "workspace_id": str(requested_scope.workspace_id),
                "environment_id": str(requested_scope.environment_id),
                "purpose": purpose,
                "operation": operation,
            },
            requested_scope,
        )

    async def _resolve(
        self,
        statement: object,
        parameters: dict[str, str],
        requested_scope: RequestedAuthorizationScope,
    ) -> int | None:
        try:
            session = self._session_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AuthorizationUnavailableError() from None
        try:
            async with session:
                async with session.begin():
                    await session.execute(
                        _BIND_RESOLUTION_SCOPE,
                        {
                            "workspace_id": str(requested_scope.workspace_id),
                            "environment_id": str(requested_scope.environment_id),
                        },
                    )
                    generation = (
                        await session.execute(statement, parameters)
                    ).scalar_one_or_none()
            if generation is None:
                return None
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                raise AuthorizationUnavailableError()
            return generation
        except asyncio.CancelledError:
            raise
        except AuthorizationUnavailableError:
            raise
        except Exception as error:
            translate_persistence_error(error)
            raise AuthorizationUnavailableError() from None


async def check_postgresql_authorization_store_readiness(
    session_factory: SessionFactory,
) -> bool:
    """Exercise the restricted resolver without disclosing whether scope exists."""

    repository = PostgreSQLAuthorizationSnapshotRepository(session_factory)
    try:
        await repository.resolve_current_snapshot(
            AuthenticationAlias(
                issuer="https://readiness.invalid",
                subject="authorization-store-readiness",
            ),
            RequestedAuthorizationScope(
                workspace_id=UUID("00000000-0000-0000-0000-000000000048"),
                environment_id=UUID("00000000-0000-0000-0000-000000000049"),
            ),
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


__all__ = [
    "PostgreSQLAuthorizationDirectory",
    "PostgreSQLAuthorizationSnapshotRepository",
    "PostgreSQLQualificationAuthorityResolver",
    "check_postgresql_authorization_store_readiness",
]
