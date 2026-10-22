"""Infrastructure-only mappings for canonical authorization records."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, ForeignKeyConstraint
from sqlalchemy import Integer, String, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PostgreSQLUUID

from spine.infrastructure.persistence.postgresql_mappings import metadata


canonical_human_identities = Table(
    "canonical_human_identities",
    metadata,
    Column("identity_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("created_change_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("created_by", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)

canonical_human_identity_states = Table(
    "canonical_human_identity_states",
    metadata,
    Column("identity_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("status", String(16), nullable=False),
    Column("change_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("recorded_by", Text, nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    ForeignKeyConstraint(
        ["identity_id"],
        ["spine.canonical_human_identities.identity_id"],
    ),
)

authentication_alias_bindings = Table(
    "authentication_alias_bindings",
    metadata,
    Column("binding_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column(
        "canonical_human_identity_id",
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("spine.canonical_human_identities.identity_id"),
        nullable=False,
    ),
    Column("version", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("change_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("recorded_by", Text, nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)

workspace_memberships = Table(
    "workspace_memberships",
    metadata,
    Column("membership_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column(
        "workspace_id",
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("spine.workspaces.id"),
        nullable=False,
    ),
    Column(
        "canonical_human_identity_id",
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("spine.canonical_human_identities.identity_id"),
        nullable=False,
    ),
    Column("version", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("change_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("recorded_by", Text, nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)

environment_memberships = Table(
    "environment_memberships",
    metadata,
    Column("membership_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column(
        "canonical_human_identity_id",
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("spine.canonical_human_identities.identity_id"),
        nullable=False,
    ),
    Column("version", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column(
        "effective_team_ids",
        ARRAY(PostgreSQLUUID(as_uuid=True)),
        nullable=False,
        server_default=text("'{}'::uuid[]"),
    ),
    Column("change_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("recorded_by", Text, nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)

environment_role_bindings = Table(
    "environment_role_bindings",
    metadata,
    Column("binding_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column(
        "canonical_human_identity_id",
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("spine.canonical_human_identities.identity_id"),
        nullable=False,
    ),
    Column("role", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("change_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("recorded_by", Text, nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)

authorization_generations = Table(
    "authorization_generations",
    metadata,
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("generation", BigInteger, nullable=False),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
        ondelete="CASCADE",
    ),
)

qualification_authority_bindings = Table(
    "qualification_authority_bindings",
    metadata,
    Column("binding_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("version", BigInteger, primary_key=True),
    Column("qualification_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("principal_kind", String(16), nullable=False),
    Column("principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("purpose", String(64), nullable=True),
    Column("operation", String(64), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("change_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("recorded_by", Text, nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)

qualification_target_bindings = Table(
    "qualification_target_bindings",
    metadata,
    Column("binding_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("binding_series_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("binding_version", BigInteger, nullable=False),
    Column("predecessor_binding_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("authority_binding_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("authority_binding_version", BigInteger, nullable=False),
    Column("qualification_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("target", String(16), nullable=False),
    Column("target_reference", String(255), nullable=False),
    Column("target_scope_reference", String(255), nullable=False),
    Column("principal_kind", String(16), nullable=False),
    Column("principal_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("purpose", String(64), nullable=True),
    Column("operation", String(64), nullable=True),
    Column("alias_reference", String(255), nullable=False),
    Column("secret_provider_reference", String(255), nullable=False),
    Column("secret_reference", String(255), nullable=False),
    Column("database_role_reference", String(255), nullable=False),
    Column("target_role", String(32), nullable=False),
    Column("database_access", String(16), nullable=False),
    Column(
        "access_policy_references",
        ARRAY(PostgreSQLUUID(as_uuid=True)),
        nullable=False,
    ),
    Column("binding_digest", String(64), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    ForeignKeyConstraint(
        ["authority_binding_id", "authority_binding_version"],
        [
            "spine.qualification_authority_bindings.binding_id",
            "spine.qualification_authority_bindings.version",
        ],
    ),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)


__all__ = [
    "authentication_alias_bindings",
    "authorization_generations",
    "canonical_human_identities",
    "canonical_human_identity_states",
    "environment_memberships",
    "environment_role_bindings",
    "qualification_authority_bindings",
    "qualification_target_bindings",
    "workspace_memberships",
]
