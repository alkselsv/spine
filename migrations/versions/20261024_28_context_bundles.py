"""Persist immutable, attempt-scoped ContextBundles."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261024_28"
down_revision = "20261023_27"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."context_bundles"'
    scope = (
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
        f'CREATE POLICY "pol_context_bundles_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_context_bundles_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def _immutable() -> None:
    qualified = f'{SCHEMA}."context_bundles"'
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_context_bundles_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Context bundle history is immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_context_bundles_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_context_bundles_immutable BEFORE UPDATE OR DELETE ON "
        f"{qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_context_bundles_mutation()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_context_bundles_mutation() FROM PUBLIC")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "context_bundles",
        sa.Column("bundle_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("bundle_version", sa.Integer(), nullable=False),
        sa.Column("bundle_digest", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("profile_id", uuid, nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("trusted_request_context_ref", uuid, nullable=False),
        sa.Column("authorization_context_ref", uuid, nullable=False),
        sa.Column("request_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("publication_pin_id", uuid, nullable=True),
        sa.Column("projection_snapshot_id", uuid, nullable=False),
        sa.Column("projection_activation_id", uuid, nullable=False),
        sa.Column("activation_generation", sa.Integer(), nullable=False),
        sa.Column("canonical_boundary_id", uuid, nullable=False),
        sa.Column("projection_config_version_id", uuid, nullable=False),
        sa.Column("freshness", sa.Text(), nullable=False),
        sa.Column("health", sa.Text(), nullable=False),
        sa.Column("retrieval_strategy", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reconciliation_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_references", postgresql.JSONB(), nullable=False),
        sa.Column("policy_decision_generations", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_boundary", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_as_of", postgresql.JSONB(), nullable=False),
        sa.Column("projection_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("projection_activation", postgresql.JSONB(), nullable=False),
        sa.Column("projection_config_version", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "bundle_id",
            "bundle_version",
            name="pk_context_bundles",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_context_bundles_environment",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "profile_id", "profile_version"],
            [
                f"{SCHEMA}.context_profiles.workspace_id",
                f"{SCHEMA}.context_profiles.environment_id",
                f"{SCHEMA}.context_profiles.profile_id",
                f"{SCHEMA}.context_profiles.profile_version",
            ],
            name="fk_context_bundles_profile",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "run_id",
            "attempt_id",
            "idempotency_key",
            name="uq_context_bundles_attempt",
        ),
        sa.CheckConstraint(
            "bundle_version > 0 AND profile_version > 0 AND activation_generation > 0",
            name=op.f("ck_context_bundles_positive_versions"),
        ),
        sa.CheckConstraint(
            "schema_version = 'context-bundle:v1'",
            name=op.f("ck_context_bundles_schema"),
        ),
        sa.CheckConstraint(
            "bundle_digest ~ '^[0-9a-f]{64}$' AND profile_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_context_bundles_digests"),
        ),
        sa.CheckConstraint(
            "freshness IN ('current', 'stale') AND health IN ('healthy', 'degraded')",
            name=op.f("ck_context_bundles_publication_state"),
        ),
        sa.CheckConstraint(
            "outcome IN ('allowed', 'degraded', 'partial', 'cancelled', 'insufficient', 'abstained')",
            name=op.f("ck_context_bundles_outcome"),
        ),
        sa.CheckConstraint(
            "state IN ('complete', 'partial', 'cancelled')",
            name=op.f("ck_context_bundles_state"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_context_bundles_attempt",
        "context_bundles",
        ["workspace_id", "environment_id", "run_id", "attempt_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_context_bundles_snapshot_generation",
        "context_bundles",
        ["workspace_id", "environment_id", "projection_snapshot_id", "activation_generation"],
        schema=SCHEMA,
    )
    _tenant()
    _immutable()


def downgrade() -> None:
    runtime = _role("runtime_role")
    qualified = f'{SCHEMA}."context_bundles"'
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
    op.drop_index("ix_context_bundles_snapshot_generation", table_name="context_bundles", schema=SCHEMA)
    op.drop_index("ix_context_bundles_attempt", table_name="context_bundles", schema=SCHEMA)
    op.drop_table("context_bundles", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_context_bundles_mutation()")
