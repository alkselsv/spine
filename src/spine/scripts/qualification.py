"""Explicit bootstrap for the disposable governed-Q&A qualification stack."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.auth import AuthenticationAlias, AuthorizationRole
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
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.infrastructure.db.authorization_seed import PostgreSQLAuthorizationSeedHelper
from spine.infrastructure.db.settings import POSTGRESQL_SEARCH_PATH_OPTIONS


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


def build_authorization_records(
    settings: QualificationBootstrapSettings,
) -> AuthorizationDirectoryRecords:
    """Build the exact authority graph shared by the API and browser journey."""

    alias = AuthenticationAlias(
        issuer=settings.authentication_alias_issuer,
        subject=settings.authentication_alias_subject,
    )
    def provenance(name: str) -> AuthorizationRecordProvenance:
        return AuthorizationRecordProvenance(
            change_id=_stable_id(settings, f"authority-change:{name}"),
            recorded_by="issue-508-disposable-qualification-bootstrap",
        )
    active = AuthorizationRecordStatus.ACTIVE
    workspace_memberships = tuple(
        WorkspaceMembership(
            membership_id=_stable_id(settings, f"workspace-membership:{workspace_id}"),
            workspace_id=workspace_id,
            canonical_human_identity_id=settings.identity_id,
            version=1,
            status=active,
            provenance=provenance(f"workspace-membership:{workspace_id}"),
        )
        for workspace_id in (settings.workspace_a_id, settings.workspace_b_id)
    )
    scopes = (
        (settings.workspace_a_id, settings.environment_a_id),
        (settings.workspace_b_id, settings.environment_b_id),
    )
    return AuthorizationDirectoryRecords(
        identities=(
            CanonicalHumanIdentity(
                identity_id=settings.identity_id,
                version=1,
                status=active,
                provenance=provenance("identity"),
            ),
        ),
        alias_bindings=(
            AuthenticationAliasBinding(
                binding_id=_stable_id(settings, "authentication-alias"),
                alias=alias,
                canonical_human_identity_id=settings.identity_id,
                version=1,
                status=active,
                provenance=provenance("authentication-alias"),
            ),
        ),
        workspace_memberships=workspace_memberships,
        environment_memberships=tuple(
            EnvironmentMembership(
                membership_id=_stable_id(settings, f"environment-membership:{environment_id}"),
                workspace_id=workspace_id,
                environment_id=environment_id,
                canonical_human_identity_id=settings.identity_id,
                version=1,
                status=active,
                provenance=provenance(f"environment-membership:{environment_id}"),
            )
            for workspace_id, environment_id in scopes
        ),
        role_bindings=tuple(
            EnvironmentRoleBinding(
                binding_id=_stable_id(settings, f"administrator:{environment_id}"),
                workspace_id=workspace_id,
                environment_id=environment_id,
                canonical_human_identity_id=settings.identity_id,
                role=AuthorizationRole.ADMINISTRATOR,
                version=1,
                status=active,
                provenance=provenance(f"administrator:{environment_id}"),
            )
            for workspace_id, environment_id in scopes
        ),
        generations=tuple(
            AuthorizationGeneration(
                workspace_id=workspace_id,
                environment_id=environment_id,
                generation=1,
            )
            for workspace_id, environment_id in scopes
        ),
        environment_owners=tuple(
            EnvironmentOwnership(
                workspace_id=workspace_id,
                environment_id=environment_id,
            )
            for workspace_id, environment_id in scopes
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


def qualification_workspace_slug(workspace_id: UUID, suffix: str) -> str:
    """Keep disposable qualification scopes unique across repeated live runs."""

    return f"issue-508-{suffix}-{workspace_id.hex}"


async def bootstrap(settings: QualificationBootstrapSettings) -> None:
    """Create two disposable scopes and their local-auth authority exactly once."""

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
        else:
            await _verify_existing_authority(engine, settings)
    finally:
        await engine.dispose()


async def _verify_existing_authority(
    engine: AsyncEngine,
    settings: QualificationBootstrapSettings,
) -> None:
    resolved_scopes = 0
    async with engine.connect() as connection:
        for workspace_id, environment_id in (
            (settings.workspace_a_id, settings.environment_a_id),
            (settings.workspace_b_id, settings.environment_b_id),
        ):
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
                    "subject": settings.authentication_alias_subject,
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                },
            )
            resolved_scopes += acting_subject_id == settings.identity_id
    if resolved_scopes != 2:
        raise RuntimeError("existing qualification authority does not match configuration")


def _stable_id(settings: QualificationBootstrapSettings, name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:issue-508:{settings.identity_id}:{name}")


def main() -> None:
    asyncio.run(bootstrap(QualificationBootstrapSettings()))


__all__ = [
    "QualificationBootstrapSettings",
    "bootstrap",
    "build_authorization_records",
    "build_context_profiles",
    "main",
]
