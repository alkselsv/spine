from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest

from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationDeniedError,
    AuthorizationRecordState,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizationUnavailableError,
    InMemoryAuthorizationDirectory,
    InMemoryAuthorizationEntry,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)


IDENTITY_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_IDENTITY_ID = UUID("10000000-0000-0000-0000-000000000002")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
OTHER_ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000002")


def authenticated_alias(subject: str = "provider-subject-123") -> AuthenticatedAlias:
    return AuthenticatedAlias(
        alias=AuthenticationAlias(
            issuer="https://identity.example.test",
            subject=subject,
        ),
        authentication_configuration_version="oidc-2026-10-10",
    )


def requested_scope(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
) -> RequestedAuthorizationScope:
    return RequestedAuthorizationScope(
        workspace_id=workspace_id,
        environment_id=environment_id,
    )


def route_policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("list_sources"),
    )


def entry(**overrides: object) -> InMemoryAuthorizationEntry:
    values: dict[str, object] = {
        "alias": authenticated_alias().alias,
        "canonical_human_identity_id": IDENTITY_ID,
        "identity_state": AuthorizationRecordState.ACTIVE,
        "alias_state": AuthorizationRecordState.ACTIVE,
        "workspace_id": WORKSPACE_ID,
        "workspace_membership_state": AuthorizationRecordState.ACTIVE,
        "environment_id": ENVIRONMENT_ID,
        "environment_workspace_id": WORKSPACE_ID,
        "environment_membership_state": AuthorizationRecordState.ACTIVE,
        "roles": frozenset({AuthorizationRole.ADMINISTRATOR}),
        "role_binding_state": AuthorizationRecordState.ACTIVE,
        "snapshot_generation": 7,
        "current_generation": 7,
    }
    values.update(overrides)
    return InMemoryAuthorizationEntry.model_validate(values)


@pytest.mark.asyncio
async def test_in_memory_directory_resolves_current_environment_authority() -> None:
    directory = InMemoryAuthorizationDirectory(entries=(entry(),))

    resolution = await directory.resolve_request_authority(
        authenticated_alias(),
        requested_scope(),
        route_policy(),
    )

    assert resolution.acting_subject_id == IDENTITY_ID
    assert resolution.scope == requested_scope()
    assert resolution.roles == frozenset({AuthorizationRole.ADMINISTRATOR})
    assert resolution.authorization_generation == 7
    assert resolution.authentication_configuration_version == "oidc-2026-10-10"


DENIED_STATES: tuple[
    tuple[str, Mapping[str, object], AuthenticatedAlias, RequestedAuthorizationScope], ...
] = (
    ("unknown alias", {}, authenticated_alias("unknown"), requested_scope()),
    (
        "disabled alias",
        {"alias_state": AuthorizationRecordState.DISABLED},
        authenticated_alias(),
        requested_scope(),
    ),
    (
        "disabled identity",
        {"identity_state": AuthorizationRecordState.DISABLED},
        authenticated_alias(),
        requested_scope(),
    ),
    (
        "missing workspace membership",
        {"workspace_membership_state": AuthorizationRecordState.MISSING},
        authenticated_alias(),
        requested_scope(),
    ),
    (
        "missing environment membership",
        {"environment_membership_state": AuthorizationRecordState.MISSING},
        authenticated_alias(),
        requested_scope(),
    ),
    (
        "missing role",
        {
            "roles": frozenset(),
            "role_binding_state": AuthorizationRecordState.MISSING,
        },
        authenticated_alias(),
        requested_scope(),
    ),
    (
        "stale generation",
        {"snapshot_generation": 6},
        authenticated_alias(),
        requested_scope(),
    ),
    (
        "wrong workspace",
        {},
        authenticated_alias(),
        requested_scope(workspace_id=OTHER_WORKSPACE_ID),
    ),
    (
        "wrong environment",
        {},
        authenticated_alias(),
        requested_scope(environment_id=OTHER_ENVIRONMENT_ID),
    ),
    (
        "environment owned by another workspace",
        {"environment_workspace_id": OTHER_WORKSPACE_ID},
        authenticated_alias(),
        requested_scope(),
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "entry_overrides", "alias", "scope"),
    DENIED_STATES,
    ids=[case[0] for case in DENIED_STATES],
)
async def test_in_memory_directory_denies_without_disclosing_failed_check(
    case: str,
    entry_overrides: Mapping[str, object],
    alias: AuthenticatedAlias,
    scope: RequestedAuthorizationScope,
) -> None:
    del case
    directory = InMemoryAuthorizationDirectory(entries=(entry(**entry_overrides),))

    with pytest.raises(AuthorizationDeniedError) as captured:
        await directory.resolve_request_authority(alias, scope, route_policy())

    assert str(captured.value) == "Authorization is denied."


@pytest.mark.asyncio
async def test_in_memory_directory_reports_unavailable_state_separately() -> None:
    directory = InMemoryAuthorizationDirectory(entries=(entry(),), available=False)

    with pytest.raises(AuthorizationUnavailableError, match="unavailable"):
        await directory.resolve_request_authority(
            authenticated_alias(),
            requested_scope(),
            route_policy(),
        )


@pytest.mark.asyncio
async def test_same_canonical_identity_has_workspace_local_authority() -> None:
    directory = InMemoryAuthorizationDirectory(
        entries=(
            entry(),
            entry(
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
                environment_workspace_id=OTHER_WORKSPACE_ID,
                snapshot_generation=11,
                current_generation=11,
            ),
        )
    )

    second_resolution = await directory.resolve_request_authority(
        authenticated_alias(),
        requested_scope(
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=OTHER_ENVIRONMENT_ID,
        ),
        route_policy(),
    )

    assert second_resolution.acting_subject_id == IDENTITY_ID
    assert second_resolution.authorization_generation == 11


def test_in_memory_directory_rejects_alias_mapped_to_two_identities() -> None:
    with pytest.raises(ValueError, match="one Canonical Human Identity"):
        InMemoryAuthorizationDirectory(
            entries=(entry(), entry(canonical_human_identity_id=OTHER_IDENTITY_ID))
        )


def test_in_memory_directory_rejects_duplicate_scope_entries() -> None:
    stale = entry(snapshot_generation=6)
    incomplete_current = entry(
        identity_state=AuthorizationRecordState.MISSING,
        alias_state=AuthorizationRecordState.MISSING,
        workspace_membership_state=AuthorizationRecordState.MISSING,
        environment_membership_state=AuthorizationRecordState.MISSING,
        roles=frozenset(),
        role_binding_state=AuthorizationRecordState.MISSING,
    )

    with pytest.raises(ValueError, match="scope entries must be unique"):
        InMemoryAuthorizationDirectory(entries=(stale, incomplete_current))
