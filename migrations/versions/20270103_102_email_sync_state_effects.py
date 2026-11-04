"""Persist content-free email state effects before checkpoint publication."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20270103_102"
down_revision = "20270102_101"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _install_guard(*, state_aware: bool) -> None:
    durable_target = (
        "((effect->>'kind' = 'state' AND EXISTS (SELECT 1 FROM spine.email_sync_state_observations AS observation "
        "WHERE observation.workspace_id = page.workspace_id AND observation.environment_id = page.environment_id "
        "AND observation.observation_id = (effect->>'effect_reference')::uuid)) OR "
        "(effect->>'kind' <> 'state' AND EXISTS (SELECT 1 FROM spine.canonical_acceptances AS acceptance "
        "WHERE acceptance.workspace_id = page.workspace_id AND acceptance.environment_id = page.environment_id "
        "AND acceptance.canonical_acceptance_id = (effect->>'effect_reference')::uuid)))"
        if state_aware
        else "EXISTS (SELECT 1 FROM spine.canonical_acceptances AS acceptance WHERE acceptance.workspace_id = page.workspace_id AND acceptance.environment_id = page.environment_id AND acceptance.canonical_acceptance_id = (effect->>'effect_reference')::uuid)"
    )
    op.execute(
        f"CREATE OR REPLACE FUNCTION {SCHEMA}.guard_email_sync_attempt_update() RETURNS trigger "
        "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine AS $fn$ "
        "BEGIN IF NEW.workspace_id <> OLD.workspace_id OR NEW.environment_id <> OLD.environment_id "
        "OR NEW.attempt_id <> OLD.attempt_id OR NEW.attempt_key <> OLD.attempt_key "
        "OR NEW.authorization_generation <> OLD.authorization_generation "
        "OR NEW.configuration_generation <> OLD.configuration_generation "
        "OR NEW.scope_generation <> OLD.scope_generation OR NEW.revision <> OLD.revision + 1 THEN "
        "RAISE EXCEPTION 'Email sync attempt revision is stale or its binding changed.' USING ERRCODE = '40001'; END IF; "
        "IF NEW.committed_checkpoint IS DISTINCT FROM OLD.committed_checkpoint AND ("
        "NOT EXISTS (SELECT 1 FROM spine.email_sync_pages AS successor WHERE successor.workspace_id = NEW.workspace_id "
        "AND successor.environment_id = NEW.environment_id AND successor.attempt_id = NEW.attempt_id AND successor.committed "
        "AND successor.record->>'requested_checkpoint' IS NOT DISTINCT FROM OLD.committed_checkpoint "
        "AND successor.record->>'next_checkpoint' IS NOT DISTINCT FROM NEW.committed_checkpoint) OR "
        "EXISTS (SELECT 1 FROM spine.email_sync_pages AS page WHERE page.workspace_id = NEW.workspace_id "
        "AND page.environment_id = NEW.environment_id AND page.attempt_id = NEW.attempt_id AND page.committed "
        "AND EXISTS (SELECT 1 FROM jsonb_array_elements(page.record->'effects') AS effect WHERE "
        "NOT EXISTS (SELECT 1 FROM spine.email_sync_effect_receipts AS receipt WHERE receipt.workspace_id = page.workspace_id "
        "AND receipt.environment_id = page.environment_id AND receipt.attempt_id = page.attempt_id "
        "AND receipt.page_id = page.page_id AND receipt.effect_key = effect->>'effect_key' AND receipt.durable) "
        f"OR NOT {durable_target}))) THEN RAISE EXCEPTION 'Email sync checkpoint requires durable effect receipts.' "
        "USING ERRCODE = '23514'; END IF; RETURN NEW; END; $fn$"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "email_sync_state_observations",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("observation_id", uuid, nullable=False),
        sa.Column("connection_id", uuid, nullable=False),
        sa.Column("mailbox_binding_id", uuid, nullable=False),
        sa.Column("state_kind", sa.String(64), nullable=False),
        sa.Column("provider_event_digest", sa.String(64), nullable=False),
        sa.Column("outbox_event_id", uuid, nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "observation_id",
            name="pk_email_sync_state_observations",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "outbox_event_id",
            name="uq_email_sync_state_observations_outbox",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_email_sync_state_observations_environment",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_event_id"],
            [f"{SCHEMA}.outbox_intents.event_id"],
            name="fk_email_sync_state_observations_outbox",
        ),
        sa.CheckConstraint(
            "state_kind ~ '^[a-z][a-z0-9_]*$' AND provider_event_digest ~ '^[0-9a-f]{64}$' AND jsonb_typeof(record) = 'object'",
            name="ck_email_sync_state_observations_record",
        ),
        schema=SCHEMA,
    )
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    scope = "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    op.execute(
        f"ALTER TABLE {SCHEMA}.email_sync_state_observations ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.email_sync_state_observations FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY pol_email_sync_state_observations_tenant_isolation ON {SCHEMA}.email_sync_state_observations FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f"CREATE POLICY pol_email_sync_state_observations_migration_maintenance ON {SCHEMA}.email_sync_state_observations FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON TABLE {SCHEMA}.email_sync_state_observations TO {runtime}"
    )
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.email_sync_state_observations FROM {runtime}"
    )
    _install_guard(state_aware=True)


def downgrade() -> None:
    _install_guard(state_aware=False)
    op.drop_table("email_sync_state_observations", schema=SCHEMA)
