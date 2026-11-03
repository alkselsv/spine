"""Persist governed email attempts, pages and effect receipts with tenant RLS."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261229_97"
down_revision = "20261228_96"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_policy(table_name: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    table = f"{SCHEMA}.{table_name}"
    scope = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{table_name}_tenant_isolation ON {table} FOR ALL TO {runtime} "
        f"USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f"CREATE POLICY pol_{table_name}_migration_maintenance ON {table} FOR ALL TO {migration} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO {runtime}")
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {table} FROM {runtime}")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "email_sync_attempts",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("attempt_key", sa.String(255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("authorization_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_generation", sa.Integer(), nullable=False),
        sa.Column("scope_generation", sa.Integer(), nullable=False),
        sa.Column("committed_checkpoint", sa.Text(), nullable=True),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "attempt_id", name="pk_email_sync_attempts"),
        sa.UniqueConstraint("workspace_id", "environment_id", "attempt_key", name="uq_email_sync_attempts_key"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_email_sync_attempts_environment",
        ),
        sa.CheckConstraint("revision >= 1 AND authorization_generation >= 1 AND configuration_generation >= 1 AND scope_generation >= 1", name="ck_email_sync_attempts_generations"),
        sa.CheckConstraint("jsonb_typeof(record) = 'object'", name="ck_email_sync_attempts_record"),
        schema=SCHEMA,
    )
    op.create_table(
        "email_sync_pages",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("page_id", sa.String(255), nullable=False),
        sa.Column("page_digest", sa.String(64), nullable=False),
        sa.Column("committed", sa.Boolean(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "attempt_id", "page_id", name="pk_email_sync_pages"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "attempt_id"],
            [f"{SCHEMA}.email_sync_attempts.workspace_id", f"{SCHEMA}.email_sync_attempts.environment_id", f"{SCHEMA}.email_sync_attempts.attempt_id"],
            name="fk_email_sync_pages_attempt",
        ),
        sa.CheckConstraint("page_digest ~ '^[0-9a-f]{64}$' AND jsonb_typeof(record) = 'object'", name="ck_email_sync_pages_record"),
        schema=SCHEMA,
    )
    op.create_table(
        "email_sync_effect_receipts",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("attempt_id", uuid, nullable=False),
        sa.Column("page_id", sa.String(255), nullable=False),
        sa.Column("effect_key", sa.String(255), nullable=False),
        sa.Column("receipt_reference", sa.String(255), nullable=False),
        sa.Column("durable", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "attempt_id", "page_id", "effect_key", name="pk_email_sync_effect_receipts"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "attempt_id", "page_id"],
            [f"{SCHEMA}.email_sync_pages.workspace_id", f"{SCHEMA}.email_sync_pages.environment_id", f"{SCHEMA}.email_sync_pages.attempt_id", f"{SCHEMA}.email_sync_pages.page_id"],
            name="fk_email_sync_effect_receipts_page",
        ),
        sa.CheckConstraint("effect_key <> '' AND receipt_reference <> ''", name="ck_email_sync_effect_receipts_reference"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_email_sync_attempts_active",
        "email_sync_attempts",
        ["workspace_id", "environment_id", "updated_at"],
        schema=SCHEMA,
    )
    for name in ("email_sync_attempts", "email_sync_pages", "email_sync_effect_receipts"):
        _tenant_policy(name)

    op.execute(
        f"CREATE FUNCTION {SCHEMA}.guard_email_sync_attempt_update() RETURNS trigger "
        "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine AS $fn$ "
        "BEGIN "
        "IF NEW.workspace_id <> OLD.workspace_id OR NEW.environment_id <> OLD.environment_id "
        "OR NEW.attempt_id <> OLD.attempt_id OR NEW.attempt_key <> OLD.attempt_key "
        "OR NEW.authorization_generation <> OLD.authorization_generation "
        "OR NEW.configuration_generation <> OLD.configuration_generation "
        "OR NEW.scope_generation <> OLD.scope_generation OR NEW.revision <> OLD.revision + 1 THEN "
        "RAISE EXCEPTION 'Email sync attempt revision is stale or its binding changed.' USING ERRCODE = '40001'; "
        "END IF; "
        "IF NEW.committed_checkpoint IS DISTINCT FROM OLD.committed_checkpoint AND ("
        "NOT EXISTS (SELECT 1 FROM spine.email_sync_pages AS successor "
        "WHERE successor.workspace_id = NEW.workspace_id "
        "AND successor.environment_id = NEW.environment_id "
        "AND successor.attempt_id = NEW.attempt_id AND successor.committed "
        "AND successor.record->>'requested_checkpoint' IS NOT DISTINCT FROM OLD.committed_checkpoint "
        "AND successor.record->>'next_checkpoint' IS NOT DISTINCT FROM NEW.committed_checkpoint) "
        "OR EXISTS (SELECT 1 FROM spine.email_sync_pages AS page "
        "WHERE page.workspace_id = NEW.workspace_id AND page.environment_id = NEW.environment_id "
        "AND page.attempt_id = NEW.attempt_id AND page.committed "
        "AND EXISTS (SELECT 1 FROM jsonb_array_elements(page.record->'effects') AS effect "
        "WHERE NOT EXISTS (SELECT 1 FROM spine.email_sync_effect_receipts AS receipt "
        "WHERE receipt.workspace_id = page.workspace_id AND receipt.environment_id = page.environment_id "
        "AND receipt.attempt_id = page.attempt_id AND receipt.page_id = page.page_id "
        "AND receipt.effect_key = effect->>'effect_key' AND receipt.durable) "
        "OR NOT EXISTS (SELECT 1 FROM spine.canonical_acceptances AS acceptance "
        "WHERE acceptance.workspace_id = page.workspace_id "
        "AND acceptance.environment_id = page.environment_id "
        "AND acceptance.canonical_acceptance_id = (effect->>'effect_reference')::uuid)))) THEN "
        "RAISE EXCEPTION 'Email sync checkpoint requires durable effect receipts.' USING ERRCODE = '23514'; "
        "END IF; RETURN NEW; END; $fn$"
    )
    op.execute(
        f"CREATE TRIGGER trg_email_sync_attempt_update BEFORE UPDATE ON {SCHEMA}.email_sync_attempts "
        "FOR EACH ROW EXECUTE FUNCTION spine.guard_email_sync_attempt_update()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER trg_email_sync_attempt_update ON {SCHEMA}.email_sync_attempts")
    op.execute(f"DROP FUNCTION {SCHEMA}.guard_email_sync_attempt_update()")
    op.drop_index("ix_email_sync_attempts_active", table_name="email_sync_attempts", schema=SCHEMA)
    op.drop_table("email_sync_effect_receipts", schema=SCHEMA)
    op.drop_table("email_sync_pages", schema=SCHEMA)
    op.drop_table("email_sync_attempts", schema=SCHEMA)
