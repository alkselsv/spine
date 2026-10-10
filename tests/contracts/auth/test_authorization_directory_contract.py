from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest

from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationDeniedError,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.auth.records import (
    AuthenticationAliasBinding,
    AuthorizationDirectoryRecords,
    AuthorizationGeneration,
    AuthorizationRecordProvenance,
    AuthorizationRecordStatus,
    CanonicalHumanIdentity,
    EnvironmentMembership,
    EnvironmentOwnership,
    EnvironmentRoleBinding,
    WorkspaceMembership,
)

from .adapter import AuthorizationDirectoryFactory


IDENTITY_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_IDENTITY_ID = UUID("10000000-0000-0000-0000-000000000002")
ALIAS_BINDING_ID = UUID("11000000-0000-0000-0000-000000000001")
WORKSPACE_MEMBERSHIP_ID = UUID("12000000-0000-0000-0000-000000000001")
ENVIRONMENT_MEMBERSHIP_ID = UUID("13000000-0000-0000-0000-000000000001")
ROLE_BINDING_ID = UUID("14000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
OTHER_ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000002")


def provenance(sequence: int) -> AuthorizationRecordProvenance:
    return AuthorizationRecordProvenance(
        change_id=UUID(f"90000000-0000-0000-0000-{sequence:012d}"),
        recorded_by="synthetic-test-bootstrap",
    )


def alias(subject: str = "provider-subject-123") -> AuthenticationAlias:
    return AuthenticationAlias(
        issuer="https://identity.example.test",
        subject=subject,
    )


def authenticated_alias(subject: str = "provider-subject-123") -> AuthenticatedAlias:
    return AuthenticatedAlias(
        alias=alias(subject),
        authentication_configuration_version="oidc-2026-10-10",
    )


def scope(
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


def records(**overrides: object) -> AuthorizationDirectoryRecords:
    values: dict[str, object] = {
        "identities": (
            CanonicalHumanIdentity(
                identity_id=IDENTITY_ID,
                version=1,
                status=AuthorizationRecordStatus.ACTIVE,
                provenance=provenance(1),
            ),
        ),
        "alias_bindings": (
            AuthenticationAliasBinding(
                binding_id=ALIAS_BINDING_ID,
                alias=alias(),
                canonical_human_identity_id=IDENTITY_ID,
                version=1,
                status=AuthorizationRecordStatus.ACTIVE,
                provenance=provenance(2),
            ),
        ),
        "workspace_memberships": (
            WorkspaceMembership(
                membership_id=WORKSPACE_MEMBERSHIP_ID,
                workspace_id=WORKSPACE_ID,
                canonical_human_identity_id=IDENTITY_ID,
                version=1,
                status=AuthorizationRecordStatus.ACTIVE,
                provenance=provenance(3),
            ),
        ),
        "environment_memberships": (
            EnvironmentMembership(
                membership_id=ENVIRONMENT_MEMBERSHIP_ID,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                canonical_human_identity_id=IDENTITY_ID,
                version=1,
                status=AuthorizationRecordStatus.ACTIVE,
                provenance=provenance(4),
            ),
        ),
        "role_bindings": (
            EnvironmentRoleBinding(
                binding_id=ROLE_BINDING_ID,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                canonical_human_identity_id=IDENTITY_ID,
                role=AuthorizationRole.ADMINISTRATOR,
                version=1,
                status=AuthorizationRecordStatus.ACTIVE,
                provenance=provenance(5),
            ),
        ),
        "generations": (
            AuthorizationGeneration(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                generation=7,
            ),
        ),
        "environment_owners": (
            EnvironmentOwnership(
                environment_id=ENVIRONMENT_ID,
                workspace_id=WORKSPACE_ID,
            ),
        ),
    }
    values.update(overrides)
    return AuthorizationDirectoryRecords.model_validate(values)


@pytest.mark.asyncio
async def test_current_records_resolve_exact_authorization_contract(
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    directory = await authorization_directory_factory(records())

    resolution = await directory.resolve_request_authority(
        authenticated_alias(), scope(), route_policy()
    )

    assert resolution.acting_subject_id == IDENTITY_ID
    assert resolution.scope == scope()
    assert resolution.roles == frozenset({AuthorizationRole.ADMINISTRATOR})
    assert resolution.authorization_generation == 7
    assert resolution.authentication_configuration_version == "oidc-2026-10-10"


DENIED_RECORD_OVERRIDES: tuple[tuple[str, Mapping[str, object]], ...] = (
    (
        "disabled identity",
        {
            "identities": (
                CanonicalHumanIdentity(
                    identity_id=IDENTITY_ID,
                    version=1,
                    status=AuthorizationRecordStatus.DISABLED,
                    provenance=provenance(10),
                ),
            )
        },
    ),
    (
        "revoked alias",
        {
            "alias_bindings": (
                AuthenticationAliasBinding(
                    binding_id=ALIAS_BINDING_ID,
                    alias=alias(),
                    canonical_human_identity_id=IDENTITY_ID,
                    version=1,
                    status=AuthorizationRecordStatus.REVOKED,
                    provenance=provenance(11),
                ),
            )
        },
    ),
    (
        "disabled workspace membership",
        {
            "workspace_memberships": (
                WorkspaceMembership(
                    membership_id=WORKSPACE_MEMBERSHIP_ID,
                    workspace_id=WORKSPACE_ID,
                    canonical_human_identity_id=IDENTITY_ID,
                    version=1,
                    status=AuthorizationRecordStatus.DISABLED,
                    provenance=provenance(12),
                ),
            )
        },
    ),
    (
        "revoked environment membership",
        {
            "environment_memberships": (
                EnvironmentMembership(
                    membership_id=ENVIRONMENT_MEMBERSHIP_ID,
                    workspace_id=WORKSPACE_ID,
                    environment_id=ENVIRONMENT_ID,
                    canonical_human_identity_id=IDENTITY_ID,
                    version=1,
                    status=AuthorizationRecordStatus.REVOKED,
                    provenance=provenance(13),
                ),
            )
        },
    ),
    (
        "disabled role binding",
        {
            "role_bindings": (
                EnvironmentRoleBinding(
                    binding_id=ROLE_BINDING_ID,
                    workspace_id=WORKSPACE_ID,
                    environment_id=ENVIRONMENT_ID,
                    canonical_human_identity_id=IDENTITY_ID,
                    role=AuthorizationRole.ADMINISTRATOR,
                    version=1,
                    status=AuthorizationRecordStatus.DISABLED,
                    provenance=provenance(14),
                ),
            )
        },
    ),
    ("missing workspace membership", {"workspace_memberships": ()}),
    ("missing environment membership", {"environment_memberships": ()}),
    ("missing role binding", {"role_bindings": ()}),
    (
        "wrong environment owner",
        {
            "environment_owners": (
                EnvironmentOwnership(
                    environment_id=ENVIRONMENT_ID,
                    workspace_id=OTHER_WORKSPACE_ID,
                ),
            )
        },
    ),
    ("missing generation", {"generations": ()}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "record_overrides"),
    DENIED_RECORD_OVERRIDES,
    ids=[case for case, _ in DENIED_RECORD_OVERRIDES],
)
async def test_inactive_or_incomplete_records_deny_without_disclosure(
    authorization_directory_factory: AuthorizationDirectoryFactory,
    case: str,
    record_overrides: Mapping[str, object],
) -> None:
    del case
    directory = await authorization_directory_factory(records(**record_overrides))

    with pytest.raises(AuthorizationDeniedError) as captured:
        await directory.resolve_request_authority(
            authenticated_alias(), scope(), route_policy()
        )

    assert str(captured.value) == "Authorization is denied."


@pytest.mark.asyncio
async def test_latest_history_version_is_the_only_authority(
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    active = records().workspace_memberships[0]
    revoked = active.model_copy(
        update={
            "membership_id": UUID("12000000-0000-0000-0000-000000000002"),
            "version": 2,
            "status": AuthorizationRecordStatus.REVOKED,
            "provenance": provenance(20),
        }
    )
    directory = await authorization_directory_factory(
        records(workspace_memberships=(active, revoked))
    )

    with pytest.raises(AuthorizationDeniedError):
        await directory.resolve_request_authority(
            authenticated_alias(), scope(), route_policy()
        )


@pytest.mark.asyncio
async def test_same_identity_has_strictly_workspace_local_authority(
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    base = records()
    directory = await authorization_directory_factory(
        AuthorizationDirectoryRecords(
            identities=base.identities,
            alias_bindings=base.alias_bindings,
            workspace_memberships=base.workspace_memberships
            + (
                WorkspaceMembership(
                    membership_id=UUID("12000000-0000-0000-0000-000000000002"),
                    workspace_id=OTHER_WORKSPACE_ID,
                    canonical_human_identity_id=IDENTITY_ID,
                    version=1,
                    status=AuthorizationRecordStatus.ACTIVE,
                    provenance=provenance(30),
                ),
            ),
            environment_memberships=base.environment_memberships
            + (
                EnvironmentMembership(
                    membership_id=UUID("13000000-0000-0000-0000-000000000002"),
                    workspace_id=OTHER_WORKSPACE_ID,
                    environment_id=OTHER_ENVIRONMENT_ID,
                    canonical_human_identity_id=IDENTITY_ID,
                    version=1,
                    status=AuthorizationRecordStatus.ACTIVE,
                    provenance=provenance(31),
                ),
            ),
            role_bindings=base.role_bindings
            + (
                EnvironmentRoleBinding(
                    binding_id=UUID("14000000-0000-0000-0000-000000000002"),
                    workspace_id=OTHER_WORKSPACE_ID,
                    environment_id=OTHER_ENVIRONMENT_ID,
                    canonical_human_identity_id=IDENTITY_ID,
                    role=AuthorizationRole.ADMINISTRATOR,
                    version=1,
                    status=AuthorizationRecordStatus.ACTIVE,
                    provenance=provenance(32),
                ),
            ),
            generations=base.generations
            + (
                AuthorizationGeneration(
                    workspace_id=OTHER_WORKSPACE_ID,
                    environment_id=OTHER_ENVIRONMENT_ID,
                    generation=11,
                ),
            ),
            environment_owners=(
                EnvironmentOwnership(
                    environment_id=ENVIRONMENT_ID,
                    workspace_id=WORKSPACE_ID,
                ),
                EnvironmentOwnership(
                    environment_id=OTHER_ENVIRONMENT_ID,
                    workspace_id=OTHER_WORKSPACE_ID,
                ),
            ),
        )
    )

    second = await directory.resolve_request_authority(
        authenticated_alias(),
        scope(OTHER_WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
        route_policy(),
    )

    assert second.acting_subject_id == IDENTITY_ID
    assert second.authorization_generation == 11

    with pytest.raises(AuthorizationDeniedError):
        await directory.resolve_request_authority(
            authenticated_alias(),
            scope(WORKSPACE_ID, OTHER_ENVIRONMENT_ID),
            route_policy(),
        )
