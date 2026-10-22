"""Explicit bootstrap for the disposable governed-Q&A qualification stack."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import insert, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
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
from spine.infrastructure.db.authorization_seed import PostgreSQLAuthorizationSeedHelper
from spine.infrastructure.db.settings import POSTGRESQL_SEARCH_PATH_OPTIONS
from spine.infrastructure.persistence.postgresql_mappings import (
    access_policy_grants,
    access_policy_versions,
    source_objects,
)
from spine.infrastructure.persistence.postgresql_authorization_mappings import (
    qualification_target_bindings,
)
from spine.infrastructure.persistence.postgresql_qualification_targets import (
    PostgreSQLQualificationTargetBindingResolver,
)


class QualificationBootstrapSettings(BaseSettings):
    """Closed settings for an explicitly disposable development deployment."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_QUALIFICATION_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    deployment_mode: str = Field(pattern="^development$")
    migration_database_url: str = Field(min_length=1)
    identity_id: UUID
    authentication_alias_issuer: str = Field(min_length=1, max_length=2048)
    authentication_alias_subject: str = Field(min_length=1, max_length=512)
    workspace_a_id: UUID
    workspace_b_id: UUID
    environment_a_id: UUID
    environment_b_id: UUID
    authority_expires_at: datetime
    target_reference: str = Field(min_length=1, max_length=255)
    target_scope_reference: str = Field(min_length=1, max_length=255)
    secret_provider_reference: str = Field(min_length=1, max_length=255)
    secret_reference: str = Field(min_length=1, max_length=255)
    database_role_reference: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_distinct_scopes(self) -> "QualificationBootstrapSettings":
        if self.workspace_a_id == self.workspace_b_id:
            raise ValueError("qualification workspaces must be distinct")
        if self.environment_a_id == self.environment_b_id:
            raise ValueError("qualification environments must be distinct")
        AuthenticationAlias(
            issuer=self.authentication_alias_issuer,
            subject=self.authentication_alias_subject,
        )
        return self


class QualificationVerificationSettings(BaseSettings):
    """Runtime-role settings for mutation-free qualification verification."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_QUALIFICATION_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    deployment_mode: str = Field(pattern="^development$")
    runtime_database_url: str = Field(min_length=1)
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
    def validate_distinct_scopes(self) -> "QualificationVerificationSettings":
        if self.workspace_a_id == self.workspace_b_id:
            raise ValueError("qualification workspaces must be distinct")
        if self.environment_a_id == self.environment_b_id:
            raise ValueError("qualification environments must be distinct")
        AuthenticationAlias(
            issuer=self.authentication_alias_issuer,
            subject=self.authentication_alias_subject,
        )
        return self


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
        (
            "approved",
            settings.identity_id,
            settings.authentication_alias_subject,
            settings.workspace_a_id,
            settings.environment_a_id,
            True,
            active,
            settings.authority_expires_at,
        ),
        (
            "insufficient-role",
            _stable_id(settings, "identity:insufficient-role"),
            f"{settings.authentication_alias_subject}-insufficient-role",
            settings.workspace_a_id,
            settings.environment_a_id,
            False,
            None,
            None,
        ),
        (
            "revoked",
            _stable_id(settings, "identity:revoked"),
            f"{settings.authentication_alias_subject}-revoked",
            settings.workspace_a_id,
            settings.environment_a_id,
            True,
            AuthorizationRecordStatus.REVOKED,
            settings.authority_expires_at,
        ),
        (
            "expired",
            _stable_id(settings, "identity:expired"),
            f"{settings.authentication_alias_subject}-expired",
            settings.workspace_a_id,
            settings.environment_a_id,
            True,
            active,
            datetime(2000, 1, 1, tzinfo=timezone.utc),
        ),
        (
            "foreign-scope",
            _stable_id(settings, "identity:foreign-scope"),
            f"{settings.authentication_alias_subject}-foreign-scope",
            settings.workspace_b_id,
            settings.environment_b_id,
            True,
            active,
            settings.authority_expires_at,
        ),
    )
    return AuthorizationDirectoryRecords(
        identities=tuple(
            CanonicalHumanIdentity(
                identity_id=identity_id,
                version=1,
                status=active,
                provenance=provenance(f"identity:{name}"),
            )
            for name, identity_id, *_ in actors
        ),
        alias_bindings=tuple(
            AuthenticationAliasBinding(
                binding_id=_stable_id(settings, f"authentication-alias:{name}"),
                alias=AuthenticationAlias(
                    issuer=settings.authentication_alias_issuer,
                    subject=subject,
                ),
                canonical_human_identity_id=identity_id,
                version=1,
                status=active,
                provenance=provenance(f"authentication-alias:{name}"),
            )
            for name, identity_id, subject, *_ in actors
        ),
        workspace_memberships=tuple(
            WorkspaceMembership(
                membership_id=_stable_id(settings, f"workspace-membership:{name}"),
                workspace_id=workspace_id,
                canonical_human_identity_id=identity_id,
                version=1,
                status=active,
                provenance=provenance(f"workspace-membership:{name}"),
            )
            for name, identity_id, _, workspace_id, *_ in actors
        ),
        environment_memberships=tuple(
            EnvironmentMembership(
                membership_id=_stable_id(settings, f"environment-membership:{name}"),
                workspace_id=workspace_id,
                environment_id=environment_id,
                canonical_human_identity_id=identity_id,
                version=1,
                status=active,
                provenance=provenance(f"environment-membership:{name}"),
            )
            for name, identity_id, _, workspace_id, environment_id, *_ in actors
        ),
        role_bindings=tuple(
            EnvironmentRoleBinding(
                binding_id=_stable_id(settings, f"administrator:{name}"),
                workspace_id=workspace_id,
                environment_id=environment_id,
                canonical_human_identity_id=identity_id,
                role=AuthorizationRole.ADMINISTRATOR,
                version=1,
                status=active,
                provenance=provenance(f"administrator:{name}"),
            )
            for name, identity_id, _, workspace_id, environment_id, has_role, *_ in actors
            if has_role
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
                binding_id=_stable_id(settings, f"qualification-binding:{name}"),
                qualification_id=_stable_id(settings, f"qualification:{name}"),
                workspace_id=workspace_id,
                environment_id=environment_id,
                principal_kind=QualificationPrincipalKind.HUMAN,
                principal_id=identity_id,
                expires_at=expires_at,
                version=1,
                status=binding_status,
                provenance=provenance(f"qualification-binding:{name}"),
            )
            for (
                name,
                identity_id,
                _,
                workspace_id,
                environment_id,
                _,
                binding_status,
                expires_at,
            ) in actors
            if binding_status is not None and expires_at is not None
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
        authority_binding_id=_stable_id(
            settings, "qualification-binding:approved"
        ),
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
        alias_reference="qualification-principal:approved:v1",
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

    return f"issue-508-{suffix}-{workspace_id.hex}"


async def provision(settings: QualificationBootstrapSettings) -> None:
    """Provision the complete disposable authority fixture from an operator command."""

    engine = create_async_engine(
        settings.migration_database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    records = build_authorization_records(settings)
    try:
        async with engine.begin() as connection:
            existing = await connection.scalar(
                text(
                    "SELECT count(*) FROM spine.canonical_human_identities "
                    "WHERE identity_id = :identity_id"
                ),
                {"identity_id": settings.identity_id},
            )
            for workspace_id, suffix in (
                (settings.workspace_a_id, "a"),
                (settings.workspace_b_id, "b"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO spine.workspaces (id, slug, display_name) "
                        "VALUES (:id, :slug, :display_name) ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id": workspace_id,
                        "slug": qualification_workspace_slug(workspace_id, suffix),
                        "display_name": f"Issue 508 Qualification {suffix.upper()}",
                    },
                )
            for workspace_id, environment_id, suffix in (
                (settings.workspace_a_id, settings.environment_a_id, "a"),
                (settings.workspace_b_id, settings.environment_b_id, "b"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO spine.environments "
                        "(id, workspace_id, kind, display_name, created_at) "
                        "VALUES (:id, :workspace_id, 'production', :display_name, :created_at) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id": environment_id,
                        "workspace_id": workspace_id,
                        "display_name": f"Issue 508 Qualification {suffix.upper()}",
                        "created_at": datetime.now(timezone.utc),
                    },
                )
            for profile in build_context_profiles(settings):
                values = profile.model_dump(mode="python")
                values["retrieval_strategy"] = profile.retrieval_strategy.value
                values["completeness_requirement"] = (
                    profile.completeness_requirement.value
                )
                values["partial_answer_fallback"] = (
                    profile.partial_answer_fallback.value
                )
                await connection.execute(
                    text(
                        "INSERT INTO spine.context_profiles ("
                        "profile_id, workspace_id, environment_id, profile_version, "
                        "profile_digest, schema_version, retrieval_strategy, max_results, "
                        "max_tokens, max_relation_hops, freshness_max_age_seconds, "
                        "completeness_requirement, partial_answer_fallback, server_approved, "
                        "revoked, predecessor_profile_id, created_at"
                        ") VALUES ("
                        ":profile_id, :workspace_id, :environment_id, :profile_version, "
                        ":profile_digest, :schema_version, :retrieval_strategy, :max_results, "
                        ":max_tokens, :max_relation_hops, :freshness_max_age_seconds, "
                        ":completeness_requirement, :partial_answer_fallback, :server_approved, "
                        ":revoked, :predecessor_profile_id, :created_at"
                        ") ON CONFLICT (workspace_id, environment_id, profile_id, profile_version) "
                        "DO NOTHING"
                    ),
                    values,
                )
        if existing == 0:
            await PostgreSQLAuthorizationSeedHelper(
                async_sessionmaker(engine, expire_on_commit=False)
            ).append(records)
        await _provision_target_fixture(engine, settings)
        await _verify_existing_authority(engine, settings)
    finally:
        await engine.dispose()


async def bootstrap(settings: QualificationBootstrapSettings) -> None:
    """Compatibility alias for the explicit operator provisioning command."""

    await provision(settings)


async def verify(settings: QualificationVerificationSettings) -> None:
    """Verify pre-provisioned authority through the least-privilege runtime role."""

    engine = create_async_engine(
        settings.runtime_database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        await _verify_existing_authority(engine, settings)
    finally:
        await engine.dispose()


async def _provision_target_fixture(
    engine: AsyncEngine,
    settings: QualificationBootstrapSettings,
) -> None:
    binding = build_target_binding(settings)
    source_object_id = _stable_id(settings, "source-object:qualification-source")
    policy_id = binding.access_policy_references[0]
    async with engine.begin() as connection:
        await connection.execute(
            postgresql_insert(source_objects)
            .values(
                source_object_id=source_object_id,
                workspace_id=settings.workspace_a_id,
                environment_id=settings.environment_a_id,
                source_kind="qualification",
                identity_mode="upload",
                upload_identity="qualification-source-authority-fixture",
                created_at=text("transaction_timestamp()"),
            )
            .on_conflict_do_nothing(index_elements=[source_objects.c.source_object_id])
        )
        await connection.execute(
            postgresql_insert(access_policy_versions)
            .values(
                access_policy_version_id=policy_id,
                workspace_id=settings.workspace_a_id,
                environment_id=settings.environment_a_id,
                source_object_id=source_object_id,
                version=1,
                predecessor_policy_version_id=None,
                document_schema_version="access-policy.v1",
                document_hash="a" * 64,
                origin_kind="approved_source_configuration",
                origin_reference=_stable_id(settings, "access-policy-origin:v1"),
                origin_version=1,
                origin_hash="b" * 64,
                created_by_canonical_human_id=settings.identity_id,
                created_at=text("transaction_timestamp()"),
            )
            .on_conflict_do_nothing(
                index_elements=[access_policy_versions.c.access_policy_version_id]
            )
        )
        await connection.execute(
            postgresql_insert(access_policy_grants)
            .values(
                access_grant_id=_stable_id(settings, "access-grant:approved-human"),
                workspace_id=settings.workspace_a_id,
                environment_id=settings.environment_a_id,
                source_object_id=source_object_id,
                access_policy_version_id=policy_id,
                principal_kind="human",
                principal_id=settings.identity_id,
                permission="read_content",
                purpose=None,
                operation=None,
                valid_from=None,
                valid_until=None,
            )
            .on_conflict_do_nothing(
                index_elements=[access_policy_grants.c.access_grant_id]
            )
        )
        await connection.execute(
            postgresql_insert(qualification_target_bindings)
            .values(
                binding_id=binding.binding_id,
                binding_series_id=binding.binding_series_id,
                binding_version=binding.binding_version,
                predecessor_binding_id=binding.predecessor_binding_id,
                authority_binding_id=binding.authority_binding_id,
                authority_binding_version=binding.authority_binding_version,
                qualification_id=binding.qualification_id,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
                target=binding.target.value,
                target_reference=binding.target_reference,
                target_scope_reference=binding.target_scope_reference,
                principal_kind=binding.principal_kind.value,
                principal_id=binding.principal_id,
                purpose=binding.purpose,
                operation=binding.operation,
                alias_reference=binding.alias_reference,
                secret_provider_reference=binding.secret.provider_reference,
                secret_reference=binding.secret.secret_reference,
                database_role_reference=binding.database_role_reference,
                target_role=binding.target_role.value,
                database_access=binding.database_access.value,
                access_policy_references=list(binding.access_policy_references),
                binding_digest=qualification_target_binding_digest(binding),
            )
            .on_conflict_do_nothing(
                index_elements=[qualification_target_bindings.c.binding_id]
            )
        )


async def _verify_existing_authority(
    engine: AsyncEngine,
    settings: QualificationBootstrapSettings | QualificationVerificationSettings,
) -> None:
    cases = (
        (
            settings.authentication_alias_subject,
            settings.workspace_a_id,
            settings.environment_a_id,
            settings.identity_id,
        ),
        (
            f"{settings.authentication_alias_subject}-foreign-scope",
            settings.workspace_b_id,
            settings.environment_b_id,
            _stable_id(settings, "identity:foreign-scope"),
        ),
        (
            f"{settings.authentication_alias_subject}-insufficient-role",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            f"{settings.authentication_alias_subject}-revoked",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            f"{settings.authentication_alias_subject}-expired",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
        (
            settings.authentication_alias_subject,
            settings.workspace_b_id,
            settings.environment_b_id,
            None,
        ),
        (
            f"{settings.authentication_alias_subject}-foreign-scope",
            settings.workspace_a_id,
            settings.environment_a_id,
            None,
        ),
    )
    verified_cases = 0
    async with engine.connect() as connection:
        for subject, workspace_id, environment_id, expected_identity_id in cases:
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
        raise RuntimeError("existing qualification authority does not match configuration")
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


def _stable_id(
    settings: QualificationBootstrapSettings | QualificationVerificationSettings,
    name: str,
) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:issue-508:{settings.identity_id}:{name}")


def main() -> None:
    asyncio.run(provision(QualificationBootstrapSettings()))


def main_verify() -> None:
    asyncio.run(verify(QualificationVerificationSettings()))


__all__ = [
    "QualificationBootstrapSettings",
    "QualificationVerificationSettings",
    "bootstrap",
    "build_authorization_records",
    "build_context_profiles",
    "build_target_binding",
    "build_target_binding_request",
    "main",
    "main_verify",
    "provision",
    "verify",
]
