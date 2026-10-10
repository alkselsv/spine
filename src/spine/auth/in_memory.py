"""Deterministic in-memory authorization-directory adapter."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from uuid import UUID

from pydantic import Field, ValidationError, field_validator

from spine.auth.contracts import (
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationResolution,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
    _snapshot_route_authorization_policy,
)
from spine.auth.errors import AuthorizationDeniedError, AuthorizationUnavailableError
from spine.domain.common import DefinitionModel


class AuthorizationRecordState(str, Enum):
    """Current state of one logical in-memory authorization record."""

    ACTIVE = "active"
    DISABLED = "disabled"
    MISSING = "missing"


class InMemoryAuthorizationEntry(DefinitionModel):
    """One deterministic resolver fixture, not a canonical persistence record."""

    alias: AuthenticationAlias
    canonical_human_identity_id: UUID
    identity_state: AuthorizationRecordState
    alias_state: AuthorizationRecordState
    workspace_id: UUID
    workspace_membership_state: AuthorizationRecordState
    environment_id: UUID
    environment_workspace_id: UUID
    environment_membership_state: AuthorizationRecordState
    roles: frozenset[AuthorizationRole]
    role_binding_state: AuthorizationRecordState
    snapshot_generation: int = Field(ge=1)
    current_generation: int = Field(ge=1)

    @field_validator(
        "canonical_human_identity_id",
        "workspace_id",
        "environment_id",
        "environment_workspace_id",
    )
    @classmethod
    def _reject_nil_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("authorization record identifiers must be non-nil UUIDs")
        return value


class InMemoryAuthorizationDirectory:
    """Resolve an immutable seed snapshot through the production directory port."""

    def __init__(
        self,
        *,
        entries: Iterable[InMemoryAuthorizationEntry],
        available: bool = True,
    ) -> None:
        snapshots = tuple(_snapshot_entry(entry) for entry in entries)
        _validate_alias_identity_mapping(snapshots)
        _validate_unique_scope_entries(snapshots)
        self._entries = snapshots
        self._available = available

    async def resolve_request_authority(
        self,
        authenticated_alias: AuthenticatedAlias,
        requested_scope: RequestedAuthorizationScope,
        route_policy: RouteAuthorizationPolicy,
    ) -> AuthorizationResolution:
        if not self._available:
            raise AuthorizationUnavailableError()

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

        matches = [entry for entry in self._entries if entry.alias == alias_snapshot.alias]
        scoped = [
            entry
            for entry in matches
            if entry.workspace_id == scope_snapshot.workspace_id
            and entry.environment_id == scope_snapshot.environment_id
        ]
        if len(scoped) != 1:
            raise AuthorizationDeniedError()

        entry = scoped[0]
        active_states = (
            entry.identity_state,
            entry.alias_state,
            entry.workspace_membership_state,
            entry.environment_membership_state,
            entry.role_binding_state,
        )
        if any(state is not AuthorizationRecordState.ACTIVE for state in active_states):
            raise AuthorizationDeniedError()
        if entry.environment_workspace_id != scope_snapshot.workspace_id:
            raise AuthorizationDeniedError()
        if entry.snapshot_generation != entry.current_generation:
            raise AuthorizationDeniedError()
        if not policy_snapshot.required_roles.issubset(entry.roles):
            raise AuthorizationDeniedError()

        return AuthorizationResolution(
            acting_subject_id=entry.canonical_human_identity_id,
            scope=scope_snapshot,
            roles=policy_snapshot.required_roles,
            authorization_generation=entry.current_generation,
            authentication_configuration_version=(
                alias_snapshot.authentication_configuration_version
            ),
        )


def _snapshot_entry(entry: InMemoryAuthorizationEntry) -> InMemoryAuthorizationEntry:
    if type(entry) is not InMemoryAuthorizationEntry:
        raise TypeError("authorization entries must use the canonical contract")
    return InMemoryAuthorizationEntry(
        alias=AuthenticationAlias(
            issuer=entry.alias.issuer,
            subject=entry.alias.subject,
        ),
        canonical_human_identity_id=entry.canonical_human_identity_id,
        identity_state=entry.identity_state,
        alias_state=entry.alias_state,
        workspace_id=entry.workspace_id,
        workspace_membership_state=entry.workspace_membership_state,
        environment_id=entry.environment_id,
        environment_workspace_id=entry.environment_workspace_id,
        environment_membership_state=entry.environment_membership_state,
        roles=frozenset(entry.roles),
        role_binding_state=entry.role_binding_state,
        snapshot_generation=entry.snapshot_generation,
        current_generation=entry.current_generation,
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


def _validate_alias_identity_mapping(
    entries: tuple[InMemoryAuthorizationEntry, ...],
) -> None:
    identities_by_alias: dict[AuthenticationAlias, UUID] = {}
    for entry in entries:
        known_identity = identities_by_alias.setdefault(
            entry.alias, entry.canonical_human_identity_id
        )
        if known_identity != entry.canonical_human_identity_id:
            raise ValueError(
                "One Authentication Alias must map to one Canonical Human Identity."
            )


def _validate_unique_scope_entries(
    entries: tuple[InMemoryAuthorizationEntry, ...],
) -> None:
    keys: set[tuple[AuthenticationAlias, UUID, UUID]] = set()
    for entry in entries:
        key = (entry.alias, entry.workspace_id, entry.environment_id)
        if key in keys:
            raise ValueError("Authorization directory scope entries must be unique.")
        keys.add(key)


__all__ = [
    "AuthorizationRecordState",
    "InMemoryAuthorizationDirectory",
    "InMemoryAuthorizationEntry",
]
