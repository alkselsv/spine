"""Explicit fixtures for the disposable governed-Q&A qualification stack."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.auth import (
    AuthenticationAlias,
    AuthorizationRole,
    ExternalSecretReference,
    QualificationBindingDeniedError,
    QualificationDatabaseAccess,
    QualificationTarget,
    QualificationTargetBinding,
    QualificationTargetBindingRequest,
    QualificationTargetRole,
    qualification_target_binding_digest,
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
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
    WorkspaceMembership,
)
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.common import DefinitionModel
from spine.infrastructure.db.settings import POSTGRESQL_SEARCH_PATH_OPTIONS
from spine.infrastructure.persistence.postgresql_qualification_fixtures import (
    PostgreSQLQualificationFixtureProvisioner,
    QualificationEnvironmentFixture,
    QualificationFixturePlan,
    QualificationSourceAccessFixture,
    QualificationWorkspaceFixture,
)
from spine.infrastructure.persistence.postgresql_qualification_targets import (
    PostgreSQLQualificationTargetBindingResolver,
)


class _QualificationScopeSettings(BaseSettings):
    """Shared immutable identity and target scope for Q16 operator commands."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_QUALIFICATION_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    deployment_mode: str = Field(pattern="^development$")
    identity_id: UUID
    authentication_alias_issuer: str = Field(min_length=1, max_length=2048)
    authentication_alias_subject: str = Field(min_length=1, max_length=512)
    workspace_a_id: UUID
    workspace_b_id: UUID
    environment_a_id: UUID
    environment_b_id: UUID
    target_reference: str = Field(min_length=1, max_length=255)
    target_scope_reference: str = Field(min_length=1, max_length=255)
    secret_provider_reference: str = Field(min_length=1, max_length=255)
    secret_reference: str = Field(min_length=1, max_length=255)
    database_role_reference: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_distinct_scopes(self) -> "_QualificationScopeSettings":
        if self.workspace_a_id == self.workspace_b_id:
            raise ValueError("qualification workspaces must be distinct")
        if self.environment_a_id == self.environment_b_id:
            raise ValueError("qualification environments must be distinct")
        AuthenticationAlias(
            issuer=self.authentication_alias_issuer,
            subject=self.authentication_alias_subject,
        )
        return self


class QualificationBootstrapSettings(_QualificationScopeSettings):
    """Closed settings for an explicitly disposable development deployment."""

    migration_database_url: str = Field(min_length=1)
    authority_expires_at: datetime


class QualificationVerificationSettings(_QualificationScopeSettings):
    """Runtime-role settings for mutation-free qualification verification."""

    runtime_database_url: str = Field(min_length=1)


class QualificationVerificationReport(DefinitionModel):
    """Content-free result of least-privilege runtime authority verification."""

    schema_version: Literal["q16-qualification-verification:v1"] = (
        "q16-qualification-verification:v1"
    )
    verified_case_ids: tuple[str, ...]
    target_binding_verified: bool


@dataclass(frozen=True)
class _QualificationActorFixture:
    name: str
    identity_id: UUID
    subject: str
    workspace_id: UUID
    environment_id: UUID
    has_administrator_role: bool
    qualification_status: AuthorizationRecordStatus | None
    qualification_expires_at: datetime | None


def build_authorization_records(
    settings: QualificationBootstrapSettings,
) -> AuthorizationDirectoryRecords:
    """Build the exact authority graph shared by the API and browser journey."""

    def provenance(name: str) -> AuthorizationRecordProvenance:
        return AuthorizationRecordProvenance(
            change_id=_stable_id(settings, f"authority-change:{name}"),
            recorded_by="qualification-fixture-operator",
        )

    active = AuthorizationRecordStatus.ACTIVE
    actors = (
        _QualificationActorFixture(
            name="approved",
            identity_id=settings.identity_id,
            subject=settings.authentication_alias_subject,
            workspace_id=settings.workspace_a_id,
            environment_id=settings.environment_a_id,
            has_administrator_role=True,
            qualification_status=active,
            qualification_expires_at=settings.authority_expires_at,
        ),
        _QualificationActorFixture(
            name="insufficient-role",
            identity_id=_stable_id(settings, "identity:insufficient-role"),
            subject=f"{settings.authentication_alias_subject}-insufficient-role",
            workspace_id=settings.workspace_a_id,
            environment_id=settings.environment_a_id,
            has_administrator_role=False,
            qualification_status=None,
            qualification_expires_at=None,
        ),
        _QualificationActorFixture(
            name="revoked",
            identity_id=_stable_id(settings, "identity:revoked"),
            subject=f"{settings.authentication_alias_subject}-revoked",
            workspace_id=settings.workspace_a_id,
            environment_id=settings.environment_a_id,
            has_administrator_role=True,
            qualification_status=AuthorizationRecordStatus.REVOKED,
            qualification_expires_at=settings.authority_expires_at,
        ),
        _QualificationActorFixture(
            name="expired",
            identity_id=_stable_id(settings, "identity:expired"),
            subject=f"{settings.authentication_alias_subject}-expired",
            workspace_id=settings.workspace_a_id,
            environment_id=settings.environment_a_id,
            has_administrator_role=True,
            qualification_status=active,
            qualification_expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        ),
        _QualificationActorFixture(
            name="foreign-scope",
            identity_id=_stable_id(settings, "identity:foreign-scope"),
            subject=f"{settings.authentication_alias_subject}-foreign-scope",
            workspace_id=settings.workspace_b_id,
            environment_id=settings.environment_b_id,
            has_administrator_role=True,
            qualification_status=active,
            qualification_expires_at=settings.authority_expires_at,
        ),
    )
    return AuthorizationDirectoryRecords(
        identities=tuple(
            CanonicalHumanIdentity(
                identity_id=actor.identity_id,
                version=1,
                status=active,
                provenance=provenance(f"identity:{actor.name}"),
            )
            for actor in actors
        ),
        alias_bindings=tuple(
            AuthenticationAliasBinding(
                binding_id=_stable_id(settings, f"authentication-alias:{actor.name}"),
                alias=AuthenticationAlias(
                    issuer=settings.authentication_alias_issuer,
                    subject=actor.subject,
                ),
                canonical_human_identity_id=actor.identity_id,
                version=1,
                status=active,
                provenance=provenance(f"authentication-alias:{actor.name}"),
            )
            for actor in actors
        ),
        workspace_memberships=tuple(
            WorkspaceMembership(
                membership_id=_stable_id(
                    settings, f"workspace-membership:{actor.name}"
                ),
                workspace_id=actor.workspace_id,
                canonical_human_identity_id=actor.identity_id,
                version=1,
                status=active,
                provenance=provenance(f"workspace-membership:{actor.name}"),
            )
            for actor in actors
        )
        + (
            WorkspaceMembership(
                membership_id=_stable_id(
                    settings, "workspace-membership:approved-workspace-b"
                ),
                workspace_id=settings.workspace_b_id,
                canonical_human_identity_id=settings.identity_id,
                version=1,
                status=active,
                provenance=provenance("workspace-membership:approved-workspace-b"),
            ),
        ),
        environment_memberships=tuple(
            EnvironmentMembership(
                membership_id=_stable_id(
                    settings, f"environment-membership:{actor.name}"
                ),
                workspace_id=actor.workspace_id,
                environment_id=actor.environment_id,
                canonical_human_identity_id=actor.identity_id,
                version=1,
                status=active,
                provenance=provenance(f"environment-membership:{actor.name}"),
            )
            for actor in actors
        )
        + (
            EnvironmentMembership(
                membership_id=_stable_id(
                    settings, "environment-membership:approved-workspace-b"
                ),
                workspace_id=settings.workspace_b_id,
                environment_id=settings.environment_b_id,
                canonical_human_identity_id=settings.identity_id,
                version=1,
                status=active,
                provenance=provenance("environment-membership:approved-workspace-b"),
            ),
        ),
        role_bindings=tuple(
            EnvironmentRoleBinding(
                binding_id=_stable_id(settings, f"administrator:{actor.name}"),
                workspace_id=actor.workspace_id,
                environment_id=actor.environment_id,
                canonical_human_identity_id=actor.identity_id,
                role=AuthorizationRole.ADMINISTRATOR,
                version=1,
                status=active,
                provenance=provenance(f"administrator:{actor.name}"),
            )
            for actor in actors
            if actor.has_administrator_role
        )
        + (
            EnvironmentRoleBinding(
                binding_id=_stable_id(settings, "administrator:approved-workspace-b"),
                workspace_id=settings.workspace_b_id,
                environment_id=settings.environment_b_id,
                canonical_human_identity_id=settings.identity_id,
                role=AuthorizationRole.ADMINISTRATOR,
                version=1,
                status=active,
                provenance=provenance("administrator:approved-workspace-b"),
            ),
        ),
        generations=tuple(
            AuthorizationGeneration(
                workspace_id=workspace_id,
                environment_id=environment_id,
                generation=1,
            )
            for workspace_id, environment_id in (
                (settings.workspace_a_id, settings.environment_a_id),
                (settings.workspace_b_id, settings.environment_b_id),
            )
        ),
        environment_owners=tuple(
            EnvironmentOwnership(
                workspace_id=workspace_id,
                environment_id=environment_id,
            )
            for workspace_id, environment_id in (
                (settings.workspace_a_id, settings.environment_a_id),
                (settings.workspace_b_id, settings.environment_b_id),
            )
        ),
        qualification_bindings=tuple(
            QualificationAuthorityBinding(
                binding_id=_stable_id(
                    settings, f"qualification-binding:{actor.name}"
                ),
                qualification_id=_stable_id(
                    settings, f"qualification:{actor.name}"
                ),
                workspace_id=actor.workspace_id,
                environment_id=actor.environment_id,
                principal_kind=QualificationPrincipalKind.HUMAN,
                principal_id=actor.identity_id,
                expires_at=actor.qualification_expires_at,
                version=1,
                status=actor.qualification_status,
                provenance=provenance(f"qualification-binding:{actor.name}"),
            )
            for actor in actors
            if actor.qualification_status is not None
            and actor.qualification_expires_at is not None
        )
        + (
            QualificationAuthorityBinding(
                binding_id=_stable_id(
                    settings, "qualification-binding:approved-workspace-b"
                ),
                qualification_id=_stable_id(
                    settings, "qualification:approved-workspace-b"
                ),
                workspace_id=settings.workspace_b_id,
                environment_id=settings.environment_b_id,
                principal_kind=QualificationPrincipalKind.HUMAN,
                principal_id=settings.identity_id,
                expires_at=settings.authority_expires_at,
                version=1,
                status=active,
                provenance=provenance(
                    "qualification-binding:approved-workspace-b"
                ),
            ),
        ),
    )


def build_context_profiles(
    settings: QualificationBootstrapSettings,
) -> tuple[ContextProfileVersion, ...]:
    """Build the immutable R1 retrieval profiles pinned by the QA worker."""

    created_at = datetime(2026, 10, 21, tzinfo=timezone.utc)
    return tuple(
        ContextProfileVersion.create(
            profile_id=_stable_id(settings, f"qa-context-profile:{environment_id}"),
            workspace_id=workspace_id,
            environment_id=environment_id,
            profile_version=1,
            retrieval_strategy=RetrievalStrategy.HYBRID,
            max_results=15,
            max_tokens=8_192,
            max_relation_hops=2,
            freshness_max_age_seconds=31_536_000,
            completeness_requirement=CompletenessRequirement.BEST_EFFORT,
            partial_answer_fallback=PartialAnswerFallback.DISALLOWED,
            server_approved=True,
            created_at=created_at,
        )
        for workspace_id, environment_id in (
            (settings.workspace_a_id, settings.environment_a_id),
            (settings.workspace_b_id, settings.environment_b_id),
        )
    )


def build_target_binding(
    settings: QualificationBootstrapSettings | QualificationVerificationSettings,
) -> QualificationTargetBinding:
    """Build the exact immutable SOURCE target binding expected by verification."""

    binding_id = _stable_id(settings, "qualification-target-binding:source")
    return QualificationTargetBinding(
        binding_id=binding_id,
        binding_series_id=binding_id,
        binding_version=1,
        predecessor_binding_id=None,
        authority_binding_id=_stable_id(settings, "qualification-binding:approved"),
        authority_binding_version=1,
        qualification_id=_stable_id(settings, "qualification:approved"),
        workspace_id=settings.workspace_a_id,
        environment_id=settings.environment_a_id,
        target=QualificationTarget.SOURCE,
        target_reference=settings.target_reference,
        target_scope_reference=settings.target_scope_reference,
        principal_id=settings.identity_id,
        principal_kind=QualificationPrincipalKind.HUMAN,
        purpose=None,
        operation=None,
        alias_reference=f"qualification-principal:{settings.identity_id}:approved:v1",
        secret=ExternalSecretReference(
            provider_reference=settings.secret_provider_reference,
            secret_reference=settings.secret_reference,
        ),
        database_role_reference=settings.database_role_reference,
        target_role=QualificationTargetRole.SOURCE_OPERATOR,
        database_access=QualificationDatabaseAccess.WRITE,
        access_policy_references=(
            _stable_id(settings, "access-policy:qualification-source:v1"),
        ),
    )


def build_target_binding_request(
    settings: QualificationBootstrapSettings | QualificationVerificationSettings,
) -> QualificationTargetBindingRequest:
    binding = build_target_binding(settings)
    return QualificationTargetBindingRequest(
        binding_id=binding.binding_id,
        binding_series_id=binding.binding_series_id,
        binding_version=binding.binding_version,
        predecessor_binding_id=binding.predecessor_binding_id,
        authority_binding_id=binding.authority_binding_id,
        authority_binding_version=binding.authority_binding_version,
        qualification_id=binding.qualification_id,
        workspace_id=binding.workspace_id,
        environment_id=binding.environment_id,
        target=binding.target,
        target_reference=binding.target_reference,
        target_scope_reference=binding.target_scope_reference,
        principal_id=binding.principal_id,
        principal_kind=binding.principal_kind,
        purpose=binding.purpose,
        operation=binding.operation,
        alias_reference=binding.alias_reference,
        secret=binding.secret,
        database_role_reference=binding.database_role_reference,
        target_role=binding.target_role,
        required_database_access=binding.database_access,
        access_policy_references=binding.access_policy_references,
    )


def qualification_workspace_slug(workspace_id: UUID, suffix: str) -> str:
    """Keep disposable qualification scopes unique across repeated live runs."""

    return f"q16-{suffix}-{workspace_id.hex}"


def build_fixture_plan(
    settings: QualificationBootstrapSettings,
) -> QualificationFixturePlan:
    """Build one exact immutable plan for the migration-owner adapter."""

    binding = build_target_binding(settings)
    created_at = datetime(2026, 10, 21, tzinfo=timezone.utc)
    return QualificationFixturePlan(
        workspaces=tuple(
            QualificationWorkspaceFixture(
                workspace_id=workspace_id,
                slug=qualification_workspace_slug(workspace_id, suffix),
                display_name=f"Q16 Qualification {suffix.upper()}",
            )
            for workspace_id, suffix in (
                (settings.workspace_a_id, "a"),
                (settings.workspace_b_id, "b"),
            )
        ),
        environments=tuple(
            QualificationEnvironmentFixture(
                environment_id=environment_id,
                workspace_id=workspace_id,
                display_name=f"Q16 Qualification {suffix.upper()}",
            )
            for workspace_id, environment_id, suffix in (
                (settings.workspace_a_id, settings.environment_a_id, "a"),
                (settings.workspace_b_id, settings.environment_b_id, "b"),
            )
        ),
        context_profiles=build_context_profiles(settings),
        authorization_records=build_authorization_records(settings),
        source_access=QualificationSourceAccessFixture(
            source_object_id=_stable_id(settings, "source-object:qualification-source"),
            policy_id=binding.access_policy_references[0],
            grant_id=_stable_id(settings, "access-grant:approved-human"),
            workspace_id=settings.workspace_a_id,
            environment_id=settings.environment_a_id,
            principal_id=settings.identity_id,
            origin_reference=_stable_id(settings, "access-policy-origin:v1"),
            created_at=created_at,
        ),
        target_binding=binding,
    )


async def provision(settings: QualificationBootstrapSettings) -> None:
    """Provision the complete disposable authority fixture from an operator command."""

    engine = create_async_engine(
        settings.migration_database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        await PostgreSQLQualificationFixtureProvisioner(
            async_sessionmaker(engine, expire_on_commit=False)
        ).provision(build_fixture_plan(settings))
        await _verify_existing_authority(engine, settings)
    finally:
        await engine.dispose()


async def advance_authorization_generation(
    settings: QualificationBootstrapSettings,
) -> None:
    """Append the one-shot record used after admission to make it stale."""

    engine = create_async_engine(
        settings.migration_database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    record = EnvironmentRoleBinding(
        binding_id=_stable_id(settings, "administrator:approved:generation-advance"),
        workspace_id=settings.workspace_a_id,
        environment_id=settings.environment_a_id,
        canonical_human_identity_id=settings.identity_id,
        role=AuthorizationRole.ADMINISTRATOR,
        version=2,
        status=AuthorizationRecordStatus.ACTIVE,
        provenance=AuthorizationRecordProvenance(
            change_id=_stable_id(settings, "authority-change:generation-advance"),
            recorded_by="qualification-fixture-operator",
        ),
    )
    try:
        await PostgreSQLQualificationFixtureProvisioner(
            async_sessionmaker(engine, expire_on_commit=False)
        ).advance_generation(record)
    finally:
        await engine.dispose()


async def bootstrap(settings: QualificationBootstrapSettings) -> None:
    """Compatibility alias for the explicit operator provisioning command."""

    await provision(settings)


async def verify(
    settings: QualificationVerificationSettings,
) -> QualificationVerificationReport:
    """Verify pre-provisioned authority through the least-privilege runtime role."""

    engine = create_async_engine(
        settings.runtime_database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        return await _verify_existing_authority(engine, settings)
    finally:
        await engine.dispose()


async def _verify_existing_authority(
    engine: AsyncEngine,
    settings: QualificationBootstrapSettings | QualificationVerificationSettings,
) -> QualificationVerificationReport:
    cases = (
        (
            "approved-scope-a",
            settings.authentication_alias_subject,
            settings.workspace_a_id,
            settings.environment_a_id,
            settings.identity_id,
        ),
        (
            "foreign-approved-scope",
            f"{settings.authentication_alias_subject}-foreign-scope",
            settings.workspace_b_id,
            settings.environment_b_id,
            _stable_id(settings, "identity:foreign-scope"),
        ),
        (
            "insufficient-role",
            f"{settings.authentication_alias_subject}-insufficient-role",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            "revoked-administrator",
            f"{settings.authentication_alias_subject}-revoked",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            "expired-administrator",
            f"{settings.authentication_alias_subject}-expired",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            "approved-scope-b",
            settings.authentication_alias_subject,
            settings.workspace_b_id,
            settings.environment_b_id,
            settings.identity_id,
        ),
        (
            "request-controlled-foreign-scope",
            f"{settings.authentication_alias_subject}-foreign-scope",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            "backup-authority-alias",
            f"{settings.authentication_alias_subject}-backup-authority",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            "runtime-identity-alias",
            f"{settings.authentication_alias_subject}-runtime-identity",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
    )
    verified_cases = 0
    async with engine.connect() as connection:
        for (
            _case_id,
            subject,
            workspace_id,
            environment_id,
            expected_identity_id,
        ) in cases:
            await connection.execute(
                text(
                    "SELECT "
                    "set_config('spine.workspace_id', :workspace_id, true), "
                    "set_config('spine.environment_id', :environment_id, true)"
                ),
                {
                    "workspace_id": str(workspace_id),
                    "environment_id": str(environment_id),
                },
            )
            acting_subject_id = await connection.scalar(
                text(
                    "SELECT acting_subject_id "
                    "FROM spine.resolve_current_authorization_snapshot("
                    ":issuer, :subject, :workspace_id, :environment_id)"
                ),
                {
                    "issuer": settings.authentication_alias_issuer,
                    "subject": subject,
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                },
            )
            verified_cases += acting_subject_id == expected_identity_id
    if verified_cases != len(cases):
        raise RuntimeError(
            "existing qualification authority does not match configuration"
        )
    resolver = PostgreSQLQualificationTargetBindingResolver(
        async_sessionmaker(engine, expire_on_commit=False)
    )
    try:
        resolved = await resolver.resolve(build_target_binding_request(settings))
    except QualificationBindingDeniedError:
        raise RuntimeError(
            "existing qualification target binding does not match configuration"
        ) from None
    if resolved != build_target_binding(settings):
        raise RuntimeError(
            "existing qualification target binding does not match configuration"
        )
    return QualificationVerificationReport(
        verified_case_ids=tuple(case[0] for case in cases),
        target_binding_verified=True,
    )


def _stable_id(
    settings: QualificationBootstrapSettings | QualificationVerificationSettings,
    name: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL, f"spine:q16-qualification:v1:{settings.identity_id}:{name}"
    )


def main() -> None:
    asyncio.run(provision(QualificationBootstrapSettings()))


def main_verify() -> None:
    asyncio.run(verify(QualificationVerificationSettings()))


def main_advance_generation() -> None:
    asyncio.run(advance_authorization_generation(QualificationBootstrapSettings()))


__all__ = [
    "QualificationBootstrapSettings",
    "QualificationVerificationSettings",
    "QualificationVerificationReport",
    "advance_authorization_generation",
    "bootstrap",
    "build_authorization_records",
    "build_context_profiles",
    "build_fixture_plan",
    "build_target_binding",
    "build_target_binding_request",
    "main",
    "main_advance_generation",
    "main_verify",
    "provision",
    "verify",
]
