from __future__ import annotations

import pytest

from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizationUnavailableError,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.infrastructure.persistence.postgresql_authorization import (
    PostgreSQLAuthorizationDirectory,
    PostgreSQLAuthorizationSnapshotRepository,
)


class _EmptyResolution:
    def mappings(self) -> "_EmptyResolution":
        return self

    def one_or_none(self) -> None:
        return None


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        del args


class _CloseFailureSession:
    def __init__(self) -> None:
        self.execute_count = 0

    async def __aenter__(self) -> "_CloseFailureSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        raise RuntimeError("database cleanup detail")

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *args: object) -> object:
        del args
        self.execute_count += 1
        if self.execute_count == 2:
            return _EmptyResolution()
        return object()


def test_postgresql_authorization_adapter_exposes_no_generic_crud_surface() -> None:
    session_factory = lambda: object()
    directory = PostgreSQLAuthorizationDirectory(session_factory)  # type: ignore[arg-type]
    repository = PostgreSQLAuthorizationSnapshotRepository(session_factory)  # type: ignore[arg-type]

    assert callable(directory.resolve_request_authority)
    assert callable(repository.resolve_current_snapshot)

    forbidden = {"add", "create", "delete", "get", "list", "update"}
    assert all(not hasattr(directory, operation) for operation in forbidden)
    assert all(not hasattr(repository, operation) for operation in forbidden)


@pytest.mark.asyncio
async def test_session_factory_failure_is_disclosure_safe_unavailable() -> None:
    def unavailable_session() -> object:
        raise RuntimeError("database connection detail")

    directory = PostgreSQLAuthorizationDirectory(unavailable_session)  # type: ignore[arg-type]

    with pytest.raises(AuthorizationUnavailableError) as captured:
        await directory.resolve_request_authority(
            AuthenticatedAlias(
                alias=AuthenticationAlias(
                    issuer="https://identity.example.test",
                    subject="subject",
                ),
                authentication_configuration_version="oidc-2026-10-10",
            ),
            RequestedAuthorizationScope(
                workspace_id="20000000-0000-0000-0000-000000000001",
                environment_id="30000000-0000-0000-0000-000000000001",
            ),
            RouteAuthorizationPolicy(
                required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
                required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
                purpose=PersistencePurpose("knowledge_control"),
                operation=PersistenceOperation("list_sources"),
            ),
        )

    assert str(captured.value) == "Authorization is unavailable."


@pytest.mark.asyncio
async def test_session_cleanup_failure_is_disclosure_safe_unavailable() -> None:
    directory = PostgreSQLAuthorizationDirectory(
        lambda: _CloseFailureSession()  # type: ignore[arg-type]
    )

    with pytest.raises(AuthorizationUnavailableError) as captured:
        await directory.resolve_request_authority(
            AuthenticatedAlias(
                alias=AuthenticationAlias(
                    issuer="https://identity.example.test",
                    subject="subject",
                ),
                authentication_configuration_version="oidc-2026-10-10",
            ),
            RequestedAuthorizationScope(
                workspace_id="20000000-0000-0000-0000-000000000001",
                environment_id="30000000-0000-0000-0000-000000000001",
            ),
            RouteAuthorizationPolicy(
                required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
                required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
                purpose=PersistencePurpose("knowledge_control"),
                operation=PersistenceOperation("list_sources"),
            ),
        )

    assert str(captured.value) == "Authorization is unavailable."
