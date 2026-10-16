"""Add immutable Security Domain bindings for safe Knowledge metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261030_35"
down_revision = "20261029_34"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _scope_policy(table: str, role: str) -> None:
    qualified = f'{SCHEMA}."{table}"'
    expression = (
        "workspace_id = (CASE WHEN pg_catalog.current_setting('spine.workspace_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.workspace_id', true) AS pg_catalog.uuid) ELSE NULL END) "
        "AND environment_id = (CASE WHEN pg_catalog.current_setting('spine.environment_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.environment_id', true) AS pg_catalog.uuid) ELSE NULL END)"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {role} '
        f"USING ({expression}) WITH CHECK ({expression})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO '
        f'{_role("migration_role")} USING (true) WITH CHECK (true)'
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {role}")


def upgrade() -> None:
    runtime = _role("runtime_role")
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "source_security_domain_bindings",
        sa.Column("binding_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("source_object_id", uuid_type, nullable=False),
        sa.Column("security_domain_reference", sa.String(256), nullable=False),
        sa.Column("security_domain_version", sa.String(128), nullable=False),
        sa.Column("binding_version", sa.Integer(), nullable=False),
        sa.Column("predecessor_binding_id", uuid_type),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_activation_id", uuid_type, nullable=False),
        sa.PrimaryKeyConstraint("binding_id", name="pk_source_security_domain_bindings"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "binding_id",
            name="uq_source_security_domain_bindings_scope_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "source_object_id",
            "binding_version",
            name="uq_source_security_domain_bindings_source_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id"],
            [
                f"{SCHEMA}.source_objects.workspace_id",
                f"{SCHEMA}.source_objects.environment_id",
                f"{SCHEMA}.source_objects.source_object_id",
            ],
            name="fk_source_security_domain_bindings_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "predecessor_binding_id"],
            [
                f"{SCHEMA}.source_security_domain_bindings.workspace_id",
                f"{SCHEMA}.source_security_domain_bindings.environment_id",
                f"{SCHEMA}.source_security_domain_bindings.source_object_id",
                f"{SCHEMA}.source_security_domain_bindings.binding_id",
            ],
            name="fk_source_security_domain_bindings_predecessor",
        ),
        sa.ForeignKeyConstraint(
            ["policy_activation_id"],
            [f"{SCHEMA}.policy_activations.policy_activation_id"],
            name="fk_source_security_domain_bindings_activation",
        ),
        sa.CheckConstraint(
            "binding_version > 0 AND length(security_domain_reference) > 0",
            name="ck_source_security_domain_bindings_shape",
        ),
        sa.CheckConstraint(
            "security_domain_version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="ck_source_security_domain_bindings_version",
        ),
        sa.CheckConstraint(
            "(binding_version = 1 AND predecessor_binding_id IS NULL) OR "
            "(binding_version > 1 AND predecessor_binding_id IS NOT NULL)",
            name="ck_source_security_domain_bindings_chain",
        ),
        schema=SCHEMA,
    )
    _scope_policy("source_security_domain_bindings", runtime)

    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_source_security_domain_binding_mutation() "
        "RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER "
        f"SET search_path = pg_catalog, {SCHEMA} AS $function$ BEGIN "
        "RAISE EXCEPTION 'Security Domain bindings are immutable.' "
        "USING ERRCODE = '55000', CONSTRAINT = 'ck_source_security_domain_bindings_immutable'; "
        "END; $function$"
    )
    op.execute(
        f"CREATE TRIGGER trg_source_security_domain_bindings_immutable "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.source_security_domain_bindings "
        "FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.reject_source_security_domain_binding_mutation()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_source_security_domain_binding_mutation() FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.reject_source_security_domain_binding_mutation() TO {runtime}"
    )

    op.add_column(
        "access_policy_versions",
        sa.Column("security_domain_binding_id", uuid_type),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_access_policy_versions_security_domain_schema",
        "access_policy_versions",
        "security_domain_binding_id IS NULL OR document_schema_version = 'access-policy.v2'",
        schema=SCHEMA,
    )


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_source_security_domain_binding_mutation() FROM {runtime}"
    )
    op.execute(
        f"DROP TRIGGER trg_source_security_domain_bindings_immutable ON {SCHEMA}.source_security_domain_bindings"
    )
    op.execute(
        f"DROP FUNCTION {SCHEMA}.reject_source_security_domain_binding_mutation()"
    )
    op.drop_constraint(
        "ck_access_policy_versions_security_domain_schema",
        "access_policy_versions",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column(
        "access_policy_versions", "security_domain_binding_id", schema=SCHEMA
    )
    op.drop_table("source_security_domain_bindings", schema=SCHEMA)
