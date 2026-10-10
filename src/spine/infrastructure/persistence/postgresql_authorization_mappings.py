"""Infrastructure-only mappings for canonical authorization records."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, ForeignKeyConstraint
from sqlalchemy import Integer, String, Table, Text, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID

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


__all__ = [
    "authentication_alias_bindings",
    "authorization_generations",
    "canonical_human_identities",
    "canonical_human_identity_states",
    "environment_memberships",
    "environment_role_bindings",
    "workspace_memberships",
]
