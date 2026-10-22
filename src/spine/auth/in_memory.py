"""Deterministic in-memory authorization-directory adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator

from spine.auth.contracts import (
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationResolution,
    AuthorizationRole,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.auth.directory import RepositoryAuthorizationDirectory
from spine.auth.errors import AuthorizationDeniedError, AuthorizationUnavailableError
from spine.auth.records import (
    AuthenticationAliasBinding,
    AuthorizationDirectoryRecords,
    AuthorizationGeneration,
    AuthorizationRecordProvenance,
    AuthorizationRecordStatus,
    CanonicalHumanIdentity,
    CurrentAuthorizationSnapshot,
    EnvironmentMembership,
    EnvironmentOwnership,
    EnvironmentRoleBinding,
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
    WorkspaceMembership,
)
from spine.domain.common import DefinitionModel


class AuthorizationRecordState(str, Enum):
    """Compatibility fixture state retained from ticket #69."""

    ACTIVE = "active"
    DISABLED = "disabled"
    MISSING = "missing"


class InMemoryAuthorizationEntry(DefinitionModel):
    """Compatibility aggregate for existing #69 callers and tests."""

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


_RecordT = TypeVar("_RecordT")


class InMemoryAuthorizationSnapshotRepository:
    """Resolve current logical records without exposing enumeration operations."""

    def __init__(
        self,
        records: AuthorizationDirectoryRecords,
        *,
        available: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(records) is not AuthorizationDirectoryRecords:
            raise TypeError("authorization records must use the canonical contract")
        self._records = AuthorizationDirectoryRecords.model_validate(
            records.model_dump(mode="python")
        )
        self._available = available
        self._clock = clock
        _validate_record_versions(self._records)

    async def resolve_current_snapshot(
        self,
        alias: AuthenticationAlias,
        requested_scope: RequestedAuthorizationScope,
    ) -> CurrentAuthorizationSnapshot | None:
        if not self._available:
            raise AuthorizationUnavailableError()

        alias_binding = _latest(
            (
                record
                for record in self._records.alias_bindings
                if record.alias == alias
            ),
            lambda record: record.version,
        )
        if (
            alias_binding is None
            or alias_binding.status is not AuthorizationRecordStatus.ACTIVE
        ):
            return None
        identity_id = alias_binding.canonical_human_identity_id

        identity = _latest(
            (
                record
                for record in self._records.identities
                if record.identity_id == identity_id
            ),
            lambda record: record.version,
        )
        workspace_membership = _latest(
            (
                record
                for record in self._records.workspace_memberships
                if record.workspace_id == requested_scope.workspace_id
                and record.canonical_human_identity_id == identity_id
            ),
            lambda record: record.version,
        )
        environment_membership = _latest(
            (
                record
                for record in self._records.environment_memberships
                if record.workspace_id == requested_scope.workspace_id
                and record.environment_id == requested_scope.environment_id
                and record.canonical_human_identity_id == identity_id
            ),
            lambda record: record.version,
        )
        role_binding = _latest(
            (
                record
                for record in self._records.role_bindings
                if record.workspace_id == requested_scope.workspace_id
                and record.environment_id == requested_scope.environment_id
                and record.canonical_human_identity_id == identity_id
                and record.role is AuthorizationRole.ADMINISTRATOR
            ),
            lambda record: record.version,
        )
        generation = next(
            (
                record
                for record in self._records.generations
                if record.workspace_id == requested_scope.workspace_id
                and record.environment_id == requested_scope.environment_id
            ),
            None,
        )
        owner = next(
            (
                record
                for record in self._records.environment_owners
                if record.environment_id == requested_scope.environment_id
            ),
            None,
        )
        required = (identity, workspace_membership, environment_membership, role_binding)
        if any(
            record is None or record.status is not AuthorizationRecordStatus.ACTIVE
            for record in required
        ):
            return None
        if (
            generation is None
            or owner is None
            or owner.workspace_id != requested_scope.workspace_id
            or not _qualification_authority_is_current(
                self._records.qualification_bindings,
                principal_kind=QualificationPrincipalKind.HUMAN,
                principal_id=identity_id,
                requested_scope=requested_scope,
                purpose=None,
                operation=None,
                now=self._clock(),
            )
        ):
            return None

        return CurrentAuthorizationSnapshot(
            canonical_human_identity_id=identity_id,
            workspace_id=requested_scope.workspace_id,
            environment_id=requested_scope.environment_id,
            roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            authorization_generation=generation.generation,
            effective_team_ids=environment_membership.effective_team_ids,
        )

    async def resolve_current_service_authority(
        self,
        *,
        service_principal_id: UUID,
        requested_scope: RequestedAuthorizationScope,
        purpose: str,
        operation: str,
        expected_generation: int | None = None,
    ) -> int | None:
        if not self._available:
            raise AuthorizationUnavailableError()
        generation = next(
            (
                record
                for record in self._records.generations
                if record.workspace_id == requested_scope.workspace_id
                and record.environment_id == requested_scope.environment_id
            ),
            None,
        )
        owner = next(
            (
                record
                for record in self._records.environment_owners
                if record.environment_id == requested_scope.environment_id
            ),
            None,
        )
        if (
            generation is None
            or owner is None
            or owner.workspace_id != requested_scope.workspace_id
            or (
                expected_generation is not None
                and expected_generation != generation.generation
            )
            or not _qualification_authority_is_current(
                self._records.qualification_bindings,
                principal_kind=QualificationPrincipalKind.SERVICE,
                principal_id=service_principal_id,
                requested_scope=requested_scope,
                purpose=purpose,
                operation=operation,
                now=self._clock(),
            )
        ):
            return None
        return generation.generation


class InMemoryAuthorizationDirectory:
    """Resolve either canonical records or legacy aggregate fixtures."""

    def __init__(
        self,
        *,
        records: AuthorizationDirectoryRecords | None = None,
        entries: Iterable[InMemoryAuthorizationEntry] | None = None,
        available: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if (records is None) == (entries is None):
            raise TypeError("provide exactly one authorization record source")
        if records is None:
            records = _records_from_entries(tuple(entries or ()))
        self._directory = RepositoryAuthorizationDirectory(
            InMemoryAuthorizationSnapshotRepository(
                records, available=available, clock=clock
            )
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


class InMemoryServiceAuthorizationDirectory:
    """Resolve bounded service authority without creating a principal registry."""

    def __init__(
        self,
        records: AuthorizationDirectoryRecords,
        *,
        available: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = InMemoryAuthorizationSnapshotRepository(
            records, available=available, clock=clock
        )

    async def resolve_service_authority(
        self,
        *,
        service_principal_id: UUID,
        requested_scope: RequestedAuthorizationScope,
        purpose: str,
        operation: str,
        expected_generation: int | None = None,
    ) -> int:
        generation = await self._repository.resolve_current_service_authority(
            service_principal_id=service_principal_id,
            requested_scope=requested_scope,
            purpose=purpose,
            operation=operation,
            expected_generation=expected_generation,
        )
        if generation is None:
            raise AuthorizationDeniedError()
        return generation


def _latest(
    records: Iterable[_RecordT],
    version: Callable[[_RecordT], int],
) -> _RecordT | None:
    return max(records, key=version, default=None)


def _validate_record_versions(records: AuthorizationDirectoryRecords) -> None:
    collections_and_keys: tuple[tuple[Iterable[object], Callable[[object], object]], ...] = (
        (records.identities, lambda item: (item.identity_id, item.version)),  # type: ignore[attr-defined]
        (records.alias_bindings, lambda item: (item.alias, item.version)),  # type: ignore[attr-defined]
        (
            records.workspace_memberships,
            lambda item: (  # type: ignore[attr-defined]
                item.workspace_id,
                item.canonical_human_identity_id,
                item.version,
            ),
        ),
        (
            records.environment_memberships,
            lambda item: (  # type: ignore[attr-defined]
                item.workspace_id,
                item.environment_id,
                item.canonical_human_identity_id,
                item.version,
            ),
        ),
        (
            records.role_bindings,
            lambda item: (  # type: ignore[attr-defined]
                item.workspace_id,
                item.environment_id,
                item.canonical_human_identity_id,
                item.role,
                item.version,
            ),
        ),
        (
            records.generations,
            lambda item: (item.workspace_id, item.environment_id),  # type: ignore[attr-defined]
        ),
        (records.environment_owners, lambda item: item.environment_id),  # type: ignore[attr-defined]
        (
            records.qualification_bindings,
            lambda item: (item.binding_id, item.version),  # type: ignore[attr-defined]
        ),
    )
    for collection, key in collections_and_keys:
        seen: set[object] = set()
        for item in collection:
            item_key = key(item)
            if item_key in seen:
                raise ValueError("authorization record versions must be unique")
            seen.add(item_key)
    _validate_qualification_binding_history(records.qualification_bindings)


def _validate_qualification_binding_history(
    bindings: tuple[QualificationAuthorityBinding, ...],
) -> None:
    immutable_fields = (
        "qualification_id",
        "workspace_id",
        "environment_id",
        "principal_kind",
        "principal_id",
        "purpose",
        "operation",
        "expires_at",
    )
    first_by_binding: dict[UUID, QualificationAuthorityBinding] = {}
    latest_by_binding: dict[UUID, QualificationAuthorityBinding] = {}
    principal_bindings: dict[tuple[object, ...], UUID] = {}
    for binding in sorted(bindings, key=lambda item: (item.binding_id.int, item.version)):
        first = first_by_binding.setdefault(binding.binding_id, binding)
        previous = latest_by_binding.get(binding.binding_id)
        if (
            previous is not None
            and previous.status is AuthorizationRecordStatus.REVOKED
        ):
            raise ValueError("revoked qualification authority cannot be reactivated")
        if any(getattr(binding, field) != getattr(first, field) for field in immutable_fields):
            raise ValueError("qualification binding scope and deadline are immutable")
        key = (
            binding.workspace_id,
            binding.environment_id,
            binding.principal_kind,
            binding.principal_id,
            binding.purpose,
            binding.operation,
        )
        previous_id = principal_bindings.setdefault(key, binding.binding_id)
        if previous_id != binding.binding_id:
            raise ValueError("qualification principal scope has multiple bindings")
        latest_by_binding[binding.binding_id] = binding


def _qualification_authority_is_current(
    bindings: tuple[QualificationAuthorityBinding, ...],
    *,
    principal_kind: QualificationPrincipalKind,
    principal_id: UUID,
    requested_scope: RequestedAuthorizationScope,
    purpose: str | None,
    operation: str | None,
    now: datetime,
) -> bool:
    principal_bindings = tuple(
        binding
        for binding in bindings
        if binding.principal_kind is principal_kind
        and binding.principal_id == principal_id
        and binding.workspace_id == requested_scope.workspace_id
        and binding.environment_id == requested_scope.environment_id
    )
    if not principal_bindings:
        return True
    candidates = tuple(
        binding
        for binding in principal_bindings
        if binding.purpose == purpose and binding.operation == operation
    )
    if not candidates:
        return False
    current = max(candidates, key=lambda item: item.version)
    return bool(
        current.status is AuthorizationRecordStatus.ACTIVE
        and current.purpose == purpose
        and current.operation == operation
        and now < current.expires_at
    )


def _fixture_uuid(kind: str, *parts: object) -> UUID:
    value = ":".join(("spine-auth-fixture", kind, *(str(part) for part in parts)))
    return uuid5(NAMESPACE_URL, value)


def _provenance(kind: str, *parts: object) -> AuthorizationRecordProvenance:
    return AuthorizationRecordProvenance(
        change_id=_fixture_uuid("change", kind, *parts),
        recorded_by="in-memory-compatibility-fixture",
    )


def _status(state: AuthorizationRecordState) -> AuthorizationRecordStatus:
    if state is AuthorizationRecordState.ACTIVE:
        return AuthorizationRecordStatus.ACTIVE
    return AuthorizationRecordStatus.DISABLED


def _records_from_entries(
    entries: tuple[InMemoryAuthorizationEntry, ...],
) -> AuthorizationDirectoryRecords:
    _validate_alias_identity_mapping(entries)
    _validate_unique_scope_entries(entries)
    identities: dict[UUID, CanonicalHumanIdentity] = {}
    aliases: dict[AuthenticationAlias, AuthenticationAliasBinding] = {}
    workspaces: dict[tuple[UUID, UUID], WorkspaceMembership] = {}
    environments: dict[tuple[UUID, UUID, UUID], EnvironmentMembership] = {}
    roles: dict[tuple[UUID, UUID, UUID], EnvironmentRoleBinding] = {}
    generations: dict[tuple[UUID, UUID], AuthorizationGeneration] = {}
    owners: dict[UUID, EnvironmentOwnership] = {}

    for entry in entries:
        if entry.identity_state is not AuthorizationRecordState.MISSING:
            identities.setdefault(
                entry.canonical_human_identity_id,
                CanonicalHumanIdentity(
                    identity_id=entry.canonical_human_identity_id,
                    version=1,
                    status=_status(entry.identity_state),
                    provenance=_provenance(
                        "identity", entry.canonical_human_identity_id
                    ),
                ),
            )
        if entry.alias_state is not AuthorizationRecordState.MISSING:
            aliases.setdefault(
                entry.alias,
                AuthenticationAliasBinding(
                    binding_id=_fixture_uuid(
                        "alias", entry.alias.issuer, entry.alias.subject
                    ),
                    alias=entry.alias,
                    canonical_human_identity_id=entry.canonical_human_identity_id,
                    version=1,
                    status=_status(entry.alias_state),
                    provenance=_provenance(
                        "alias", entry.alias.issuer, entry.alias.subject
                    ),
                ),
            )
        workspace_key = (entry.workspace_id, entry.canonical_human_identity_id)
        if entry.workspace_membership_state is not AuthorizationRecordState.MISSING:
            workspaces[workspace_key] = WorkspaceMembership(
                membership_id=_fixture_uuid("workspace-membership", *workspace_key),
                workspace_id=entry.workspace_id,
                canonical_human_identity_id=entry.canonical_human_identity_id,
                version=1,
                status=_status(entry.workspace_membership_state),
                provenance=_provenance("workspace-membership", *workspace_key),
            )
        environment_key = (
            entry.workspace_id,
            entry.environment_id,
            entry.canonical_human_identity_id,
        )
        if entry.environment_membership_state is not AuthorizationRecordState.MISSING:
            environments[environment_key] = EnvironmentMembership(
                membership_id=_fixture_uuid(
                    "environment-membership", *environment_key
                ),
                workspace_id=entry.workspace_id,
                environment_id=entry.environment_id,
                canonical_human_identity_id=entry.canonical_human_identity_id,
                version=1,
                status=_status(entry.environment_membership_state),
                provenance=_provenance("environment-membership", *environment_key),
            )
        if (
            entry.role_binding_state is not AuthorizationRecordState.MISSING
            and AuthorizationRole.ADMINISTRATOR in entry.roles
        ):
            roles[environment_key] = EnvironmentRoleBinding(
                binding_id=_fixture_uuid("role-binding", *environment_key),
                workspace_id=entry.workspace_id,
                environment_id=entry.environment_id,
                canonical_human_identity_id=entry.canonical_human_identity_id,
                role=AuthorizationRole.ADMINISTRATOR,
                version=1,
                status=_status(entry.role_binding_state),
                provenance=_provenance("role-binding", *environment_key),
            )
        if entry.snapshot_generation == entry.current_generation:
            generations[(entry.workspace_id, entry.environment_id)] = (
                AuthorizationGeneration(
                    workspace_id=entry.workspace_id,
                    environment_id=entry.environment_id,
                    generation=entry.current_generation,
                )
            )
        owners[entry.environment_id] = EnvironmentOwnership(
            workspace_id=entry.environment_workspace_id,
            environment_id=entry.environment_id,
        )

    return AuthorizationDirectoryRecords(
        identities=tuple(identities.values()),
        alias_bindings=tuple(aliases.values()),
        workspace_memberships=tuple(workspaces.values()),
        environment_memberships=tuple(environments.values()),
        role_bindings=tuple(roles.values()),
        generations=tuple(generations.values()),
        environment_owners=tuple(owners.values()),
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
    "InMemoryAuthorizationSnapshotRepository",
]
