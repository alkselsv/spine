"""Adapter-independent authorization-directory decision logic."""

from __future__ import annotations

from pydantic import ValidationError

from spine.auth.contracts import (
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationResolution,
    AuthorizationScopeRequirement,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
    _snapshot_route_authorization_policy,
)
from spine.auth.errors import AuthorizationDeniedError
from spine.auth.repositories import AuthorizationSnapshotRepository


class RepositoryAuthorizationDirectory:
    """Map one purpose-specific current snapshot to the public #69 contract."""

    def __init__(self, repository: AuthorizationSnapshotRepository) -> None:
        self._repository = repository

    async def resolve_request_authority(
        self,
        authenticated_alias: AuthenticatedAlias,
        requested_scope: RequestedAuthorizationScope,
        route_policy: RouteAuthorizationPolicy,
    ) -> AuthorizationResolution:
        try:
            alias_snapshot = _snapshot_authenticated_alias(authenticated_alias)
            scope_snapshot = _snapshot_requested_scope(requested_scope)
            policy_snapshot = _snapshot_route_authorization_policy(route_policy)
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise AuthorizationDeniedError() from None

        if (
            policy_snapshot.required_scope
            is not AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT
        ):
            raise AuthorizationDeniedError()

        snapshot = await self._repository.resolve_current_snapshot(
            alias_snapshot.alias,
            scope_snapshot,
        )
        if snapshot is None:
            raise AuthorizationDeniedError()
        if (
            snapshot.workspace_id != scope_snapshot.workspace_id
            or snapshot.environment_id != scope_snapshot.environment_id
            or not policy_snapshot.required_roles.issubset(snapshot.roles)
        ):
            raise AuthorizationDeniedError()

        return AuthorizationResolution(
            acting_subject_id=snapshot.canonical_human_identity_id,
            scope=scope_snapshot,
            roles=policy_snapshot.required_roles,
            authorization_generation=snapshot.authorization_generation,
            authentication_configuration_version=(
                alias_snapshot.authentication_configuration_version
            ),
        )


def _snapshot_authenticated_alias(value: AuthenticatedAlias) -> AuthenticatedAlias:
    if type(value) is not AuthenticatedAlias or type(value.alias) is not AuthenticationAlias:
        raise TypeError("authenticated alias must use the canonical contract")
    return AuthenticatedAlias(
        alias=AuthenticationAlias(
            issuer=value.alias.issuer,
            subject=value.alias.subject,
        ),
        authentication_configuration_version=value.authentication_configuration_version,
    )


def _snapshot_requested_scope(
    value: RequestedAuthorizationScope,
) -> RequestedAuthorizationScope:
    if type(value) is not RequestedAuthorizationScope:
        raise TypeError("requested scope must use the canonical contract")
    return RequestedAuthorizationScope(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
    )


__all__ = ["RepositoryAuthorizationDirectory"]
