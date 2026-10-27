from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker

from spine.application.persistence.errors import ConstraintConflictError
from spine.application.storage.recovery_integrity import RecoveryEvidenceCoordinator
from spine.domain.storage.recovery import (
    ComponentState,
    QuiescentEvidence,
    RecoveryEvidenceEventKind,
    RecoveryEvidenceInvalidation,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.db.initial_workspace import (
    PostgreSQLInitialWorkspaceBootstrap,
)
from spine.infrastructure.db.migrations import (
    MigrationPreflightError,
    locate_migration_assets,
    upgrade_database,
)
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import (
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.backup_recovery import (
    PostgreSQLBackupRecoveryRepository,
)
from tests.contracts.persistence.source_canonical_vectors import (
    INVALID_LANGUAGE_TAGS,
    LANGUAGE_VECTORS,
    UNICODE_VECTORS,
)


pytestmark = pytest.mark.postgresql
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
DISPATCHER_PASSWORD = "dispatcher-test-secret"
INITIAL_REVISION = "20261009_01"
RLS_REVISION = "20261009_02"
IDEMPOTENCY_REVISION = "20261010_03"
OUTBOX_REVISION = "20261010_04"
PRE_K0_REVISION = "20261010_05"
K0_REVISION = "20261010_06"
AUTHORIZATION_REVISION = "20261010_07"
AUDIT_REVISION = "20261010_08"
SOURCE_OBSERVATIONS_REVISION = "20261010_12"
DELIVERY_REVISION = "20261011_13"
ATOMIC_DELIVERY_AUDIT_REVISION = "20261012_14"
WORKSPACE_A = UUID("20000000-0000-0000-0000-000000000001")
WORKSPACE_B = UUID("20000000-0000-0000-0000-000000000002")
ENVIRONMENT_A = UUID("30000000-0000-0000-0000-000000000001")
ENVIRONMENT_A_SECOND = UUID("30000000-0000-0000-0000-000000000002")
ENVIRONMENT_B = UUID("30000000-0000-0000-0000-000000000003")

AUTHORIZATION_DIRECTORY_TABLES = {
    "authentication_alias_bindings",
    "authorization_generations",
    "qualification_authority_bindings",
    "qualification_target_bindings",
    "canonical_human_identities",
    "canonical_human_identity_states",
    "environment_memberships",
    "environment_role_bindings",
    "workspace_memberships",
}
AUTHORIZATION_DIRECTORY_CONSTRAINTS = {
    "ck_authentication_alias_bindings_issuer_length",
    "ck_authentication_alias_bindings_recorded_by_length",
    "ck_authentication_alias_bindings_status",
    "ck_authentication_alias_bindings_subject_length",
    "ck_authentication_alias_bindings_version_positive",
    "ck_authorization_generations_generation_positive",
    "ck_qualification_binding_principal_kind",
    "ck_qualification_binding_recorded_by_length",
    "ck_qualification_binding_scope_shape",
    "ck_qualification_binding_status",
    "ck_qualification_binding_version_positive",
    "ck_qualification_target_authority_version",
    "ck_qualification_target_digest",
    "ck_qualification_target_history",
    "ck_qualification_target_kind",
    "ck_qualification_target_policy_references",
    "ck_qualification_target_principal_kind",
    "ck_qualification_target_principal_scope",
    "ck_qualification_target_role_scope",
    "ck_canonical_human_identities_created_by_length",
    "ck_canonical_human_identity_states_recorded_by_length",
    "ck_canonical_human_identity_states_status",
    "ck_canonical_human_identity_states_version_positive",
    "ck_environment_memberships_recorded_by_length",
    "ck_environment_memberships_status",
    "ck_environment_memberships_version_positive",
    "ck_environment_role_bindings_recorded_by_length",
    "ck_environment_role_bindings_role",
    "ck_environment_role_bindings_status",
    "ck_environment_role_bindings_version_positive",
    "ck_workspace_memberships_recorded_by_length",
    "ck_workspace_memberships_status",
    "ck_workspace_memberships_version_positive",
    "fk_alias_bindings_identity",
    "fk_authorization_generations_scope_environments",
    "fk_qualification_binding_environment",
    "fk_qualification_target_authority",
    "fk_qualification_target_environment",
    "fk_qualification_target_predecessor",
    "fk_environment_memberships_identity",
    "fk_environment_memberships_scope_environments",
    "fk_environment_role_bindings_identity",
    "fk_environment_role_bindings_scope_environments",
    "fk_identity_states_identity",
    "fk_workspace_memberships_identity",
    "fk_workspace_memberships_workspace_id_workspaces",
    "pk_authentication_alias_bindings",
    "pk_authorization_generations",
    "pk_qualification_authority_bindings",
    "pk_qualification_target_bindings",
    "pk_canonical_human_identities",
    "pk_canonical_human_identity_states",
    "pk_environment_memberships",
    "pk_environment_role_bindings",
    "pk_workspace_memberships",
    "uq_authentication_alias_bindings_alias_version",
    "uq_authentication_alias_bindings_change_id",
    "uq_canonical_human_identities_created_change_id",
    "uq_qualification_binding_change_id",
    "uq_qualification_target_digest",
    "uq_qualification_target_predecessor",
    "uq_qualification_target_series_version",
    "uq_canonical_human_identity_states_change_id",
    "uq_environment_memberships_change_id",
    "uq_environment_memberships_scope_version",
    "uq_environment_role_bindings_change_id",
    "uq_environment_role_bindings_scope_role_version",
    "uq_workspace_memberships_change_id",
    "uq_workspace_memberships_scope_version",
}
AUTHORIZATION_DIRECTORY_INDEXES = {
    "ix_environment_memberships_current_lookup",
    "ix_environment_role_bindings_current_lookup",
    "ix_workspace_memberships_current_lookup",
    "pk_authentication_alias_bindings",
    "pk_authorization_generations",
    "ix_qualification_authority_current_principal",
    "pk_qualification_authority_bindings",
    "pk_qualification_target_bindings",
    "pk_canonical_human_identities",
    "pk_canonical_human_identity_states",
    "pk_environment_memberships",
    "pk_environment_role_bindings",
    "pk_workspace_memberships",
    "uq_authentication_alias_bindings_alias_version",
    "uq_authentication_alias_bindings_change_id",
    "uq_canonical_human_identities_created_change_id",
    "uq_qualification_binding_change_id",
    "uq_qualification_target_digest",
    "uq_qualification_target_predecessor",
    "uq_qualification_target_series_version",
    "uq_canonical_human_identity_states_change_id",
    "uq_environment_memberships_change_id",
    "uq_environment_memberships_scope_version",
    "uq_environment_role_bindings_change_id",
    "uq_environment_role_bindings_scope_role_version",
    "uq_workspace_memberships_change_id",
    "uq_workspace_memberships_scope_version",
}
AUTHORIZATION_DIRECTORY_COLUMNS = {
    ("canonical_human_identities", column)
    for column in ("identity_id", "created_change_id", "created_by", "created_at")
} | {
    ("canonical_human_identity_states", column)
    for column in (
        "identity_id", "version", "status", "change_id", "recorded_by", "recorded_at"
    )
} | {
    ("authentication_alias_bindings", column)
    for column in (
        "binding_id", "issuer", "subject", "canonical_human_identity_id",
        "version", "status", "change_id", "recorded_by", "recorded_at",
    )
} | {
    ("workspace_memberships", column)
    for column in (
        "membership_id", "workspace_id", "canonical_human_identity_id",
        "version", "status", "change_id", "recorded_by", "recorded_at",
    )
} | {
    ("environment_memberships", column)
    for column in (
        "membership_id", "workspace_id", "environment_id",
        "canonical_human_identity_id", "effective_team_ids", "version", "status", "change_id",
        "recorded_by", "recorded_at",
    )
} | {
    ("environment_role_bindings", column)
    for column in (
        "binding_id", "workspace_id", "environment_id",
        "canonical_human_identity_id", "role", "version", "status",
        "change_id", "recorded_by", "recorded_at",
    )
} | {
    ("authorization_generations", column)
    for column in ("workspace_id", "environment_id", "generation", "updated_at")
} | {
    ("qualification_authority_bindings", column)
    for column in (
        "binding_id", "version", "qualification_id", "workspace_id",
        "environment_id", "principal_kind", "principal_id", "purpose",
        "operation", "expires_at", "status", "change_id", "recorded_by",
        "recorded_at",
    )
} | {
    ("qualification_target_bindings", column)
    for column in (
        "binding_id", "binding_series_id", "binding_version",
        "predecessor_binding_id", "authority_binding_id", "authority_binding_version",
        "qualification_id", "workspace_id", "environment_id", "target",
        "target_reference", "target_scope_reference", "principal_kind",
        "principal_id", "purpose", "operation", "alias_reference",
        "secret_provider_reference", "secret_reference",
        "database_role_reference", "target_role", "database_access",
        "access_policy_references", "binding_digest", "recorded_at",
    )
}

AUDIT_CONSTRAINTS = {
    "ck_audit_events_actor_scope",
    "ck_audit_events_event_type_identifier",
    "ck_audit_events_origin",
    "ck_audit_events_outcome_identifier",
    "ck_audit_events_payload_object",
    "ck_audit_events_producer_identifier",
    "ck_audit_events_reason_identifier",
    "ck_audit_events_schema_version_positive",
    "ck_audit_events_target_complete",
    "fk_audit_events_scope_environments",
    "fk_audit_events_workspace_id_workspaces",
    "pk_audit_events",
}
AUDIT_INDEXES = {
    "ix_audit_events_tenant_appended_at",
    "ix_audit_events_workspace_target",
    "ix_audit_events_workspace_trace_id",
    "pk_audit_events",
    "uq_audit_events_environment_producer",
    "uq_audit_events_workspace_producer",
}
AUDIT_COLUMNS = {
    ("audit_events", column)
    for column in (
        "audit_event_id",
        "workspace_id",
        "environment_id",
        "event_type",
        "schema_version",
        "origin",
        "acting_subject_id",
        "service_principal_id",
        "trace_id",
        "correlation_id",
        "causation_id",
        "occurred_at",
        "appended_at",
        "target_type",
        "target_id",
        "target_schema_version",
        "outcome",
        "reason",
        "producer_deduplication_id",
        "payload",
    )
}

DELIVERY_CONSTRAINTS = {
    "ck_outbox_deliveries_attempt_count_nonnegative",
    "ck_outbox_deliveries_consumer_identifier",
    "ck_outbox_deliveries_error_code_identifier",
    "ck_outbox_deliveries_lease_owner_identifier",
    "ck_outbox_deliveries_state_complete",
    "ck_outbox_deliveries_version_nonnegative",
    "fk_outbox_deliveries_event_id_outbox_intents",
    "pk_outbox_deliveries",
}
DELIVERY_INDEXES = {
    "ix_outbox_deliveries_due",
    "ix_outbox_deliveries_tenant_status",
    "pk_outbox_deliveries",
}
DELIVERY_COLUMNS = {
    ("outbox_deliveries", column)
    for column in (
        "event_id",
        "workspace_id",
        "environment_id",
        "consumer_name",
        "status",
        "attempt_count",
        "next_attempt_at",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "last_error_code",
        "correlation_id",
        "delivered_at",
        "quarantined_at",
        "version",
        "created_at",
        "updated_at",
    )
}

ADMISSION_CONSTRAINTS = {
    "pk_admission_decisions",
    "uq_admission_decisions_scope_revision_id",
    "uq_admission_decisions_revision_version",
    "fk_admission_decisions_revision",
    "fk_admission_decisions_predecessor",
    "ck_admission_decisions_version_positive",
    "ck_admission_decisions_disposition",
    "ck_admission_decisions_profile_identifier",
    "ck_admission_decisions_reason_schema_identifier",
    "ck_admission_decisions_reason_codes",
    "ck_admission_decisions_authority_reference",
    "ck_admission_decisions_references_nonzero",
    "ck_admission_decisions_root_version",
}
ADMISSION_INDEXES = {
    "pk_admission_decisions",
    "uq_admission_decisions_scope_revision_id",
    "uq_admission_decisions_revision_version",
    "uq_admission_decisions_root",
    "uq_admission_decisions_successor",
    "ix_admission_decisions_effective",
}
ADMISSION_COLUMNS = {
    ("admission_decisions", column)
    for column in (
        "admission_decision_id",
        "workspace_id",
        "environment_id",
        "source_object_id",
        "source_revision_id",
        "decision_version",
        "predecessor_decision_id",
        "disposition",
        "validator_profile_version",
        "reason_schema_version",
        "reason_codes",
        "authority_reference",
        "trace_id",
        "audit_event_id",
        "decided_at",
    )
}

CANONICAL_ACCEPTANCE_CONSTRAINTS = {
    "pk_canonical_generation_counters",
    "ck_canonical_generation_counters_generation_nonnegative",
    "pk_canonical_acceptances",
    "uq_canonical_acceptances_generation",
    "uq_canonical_acceptances_revision",
    "uq_canonical_acceptances_scope_id",
    "uq_canonical_acceptances_idempotency_receipt",
    "uq_canonical_acceptances_head_tuple",
    "fk_canonical_acceptances_source",
    "fk_canonical_acceptances_revision",
    "fk_canonical_acceptances_provenance",
    "fk_canonical_acceptances_admission",
    "fk_canonical_acceptances_policy",
    "fk_canonical_acceptances_idempotency",
    "fk_canonical_acceptances_audit",
    "fk_canonical_acceptances_outbox",
    "fk_canonical_acceptances_outbox",
    "ck_canonical_acceptances_generation",
    "ck_canonical_acceptances_ordering",
    "ck_canonical_acceptances_order_pair",
    "ck_canonical_acceptances_ordering_shape",
    "pk_current_source_heads",
    "fk_current_source_heads_revision",
    "fk_current_source_heads_acceptance",
    "fk_current_source_heads_acceptance_tuple",
    "ck_current_source_heads_kind",
    "ck_current_source_heads_versions",
    "ck_current_source_heads_order_pair",
}
CANONICAL_ACCEPTANCE_INDEXES = {
    "pk_canonical_generation_counters",
    "pk_canonical_acceptances",
    "uq_canonical_acceptances_generation",
    "uq_canonical_acceptances_revision",
    "uq_canonical_acceptances_scope_id",
    "uq_canonical_acceptances_idempotency_receipt",
    "uq_canonical_acceptances_head_tuple",
    "ix_canonical_acceptances_source_generation",
    "pk_current_source_heads",
}
CANONICAL_ACCEPTANCE_COLUMNS = {
    ("canonical_generation_counters", column)
    for column in ("workspace_id", "environment_id", "current_generation", "updated_at")
} | {
    ("canonical_acceptances", column)
    for column in (
        "canonical_acceptance_id", "source_object_id", "source_revision_id",
        "workspace_id", "environment_id", "predecessor_revision_id",
        "predecessor_acceptance_id", "accepted_generation", "ordering",
        "ordering_scheme", "ordering_token", "admission_decision_id",
        "access_policy_version_id", "provenance_id", "idempotency_receipt_id",
        "audit_event_id", "outbox_event_id", "accepted_at",
    )
} | {
    ("current_source_heads", column)
    for column in (
        "source_object_id", "workspace_id", "environment_id", "current_revision_id",
        "current_revision_kind", "acceptance_id", "accepted_generation", "cas_version",
        "ordering_scheme", "ordering_token", "changed_at",
    )
}

CANONICAL_BOUNDARY_CONSTRAINTS = {
    "pk_canonical_boundaries",
    "uq_canonical_boundaries_scope_id",
    "uq_canonical_boundaries_idempotency_receipt",
    "fk_canonical_boundaries_environment",
    "fk_canonical_boundaries_idempotency",
    "ck_canonical_boundaries_generation",
    "ck_canonical_boundaries_projection_kind",
    "ck_canonical_boundaries_scope_version",
    "ck_canonical_boundaries_digest",
    "pk_canonical_boundary_members",
    "fk_canonical_boundary_members_boundary",
    "fk_canonical_boundary_members_source",
    "fk_canonical_boundary_members_revision",
    "fk_canonical_boundary_members_acceptance",
    "ck_canonical_boundary_members_kind",
    "ck_canonical_boundary_members_generation",
}
CANONICAL_BOUNDARY_INDEXES = {
    "pk_canonical_boundaries",
    "uq_canonical_boundaries_scope_id",
    "uq_canonical_boundaries_idempotency_receipt",
    "pk_canonical_boundary_members",
    "ix_canonical_boundary_members_scope_source",
}
CANONICAL_BOUNDARY_COLUMNS = {
    ("canonical_boundaries", column)
    for column in (
        "boundary_id", "workspace_id", "environment_id", "projection_kind",
        "scope_version", "accepted_through_generation", "boundary_time",
        "manifest_digest", "idempotency_receipt_id",
    )
} | {
    ("canonical_boundary_members", column)
    for column in (
        "boundary_id", "workspace_id", "environment_id", "source_object_id",
        "source_revision_id", "acceptance_id", "revision_kind",
        "accepted_generation",
    )
}


@dataclass(frozen=True, slots=True)
class MigratedDatabase:
    operator: OperatorDatabaseSettings
    migration: MigrationDatabaseSettings
    runtime_url: SecretStr
    retained_revisions: tuple[str, ...]


def _migration_script_directory() -> ScriptDirectory:
    repository_root = Path(__file__).parents[3]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    return ScriptDirectory.from_config(config)


def _retained_revision_ids() -> tuple[str, ...]:
    scripts = _migration_script_directory()
    heads = tuple(scripts.get_heads())
    assert len(heads) == 1
    return tuple(
        revision.revision
        for revision in reversed(tuple(scripts.iterate_revisions(heads[0], "base")))
    )


# Keep assertions tied to the checked-out migration graph, not a remembered head.
HEAD_REVISION = _retained_revision_ids()[-1]


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@asynccontextmanager
async def _connection(url: SecretStr) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def migrated_database(
    postgresql_provision: TestDatabaseProvision,
) -> MigratedDatabase:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
        dispatcher_role=DISPATCHER_ROLE,
        dispatcher_password=DISPATCHER_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration = MigrationDatabaseSettings(
        url=_role_url(postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD),
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    retained_revisions: list[str] = []
    for revision in _retained_revision_ids()[:-1]:
        await asyncio.to_thread(upgrade_database, migration, revision=revision)
        async with _connection(migration.url) as connection:
            retained_revisions.append(
                await connection.scalar(
                    text("SELECT version_num FROM spine.alembic_version")
                )
            )
    await asyncio.to_thread(upgrade_database, migration)
    return MigratedDatabase(
        operator=operator,
        migration=migration,
        runtime_url=_role_url(
            postgresql_provision,
            RUNTIME_ROLE,
            RUNTIME_PASSWORD,
        ),
        retained_revisions=tuple(retained_revisions),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def tenant_rows(migrated_database: MigratedDatabase) -> AsyncIterator[None]:
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) VALUES "
                "(:workspace_a, 'workspace-a', 'Workspace A'), "
                "(:workspace_b, 'workspace-b', 'Workspace B')"
            ),
            {"workspace_a": WORKSPACE_A, "workspace_b": WORKSPACE_B},
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments "
                "(id, workspace_id, kind, display_name) VALUES "
                "(:environment_a, :workspace_a, 'development', 'A Development'), "
                "(:environment_a_second, :workspace_a, 'staging', 'A Staging'), "
                "(:environment_b, :workspace_b, 'production', 'B Production')"
            ),
            {
                "environment_a": ENVIRONMENT_A,
                "environment_a_second": ENVIRONMENT_A_SECOND,
                "environment_b": ENVIRONMENT_B,
                "workspace_a": WORKSPACE_A,
                "workspace_b": WORKSPACE_B,
            },
        )
        await connection.commit()

    yield
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "DELETE FROM spine.environments WHERE id IN "
                "(:environment_a, :environment_a_second, :environment_b)"
            ),
            {
                "environment_a": ENVIRONMENT_A,
                "environment_a_second": ENVIRONMENT_A_SECOND,
                "environment_b": ENVIRONMENT_B,
            },
        )
        await connection.execute(
            text(
                "DELETE FROM spine.workspaces WHERE id IN "
                "(:workspace_a, :workspace_b)"
            ),
            {"workspace_a": WORKSPACE_A, "workspace_b": WORKSPACE_B},
        )
        await connection.commit()


def _recovery_manifest_for_integration() -> StorageRecoveryManifest:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    evidence = QuiescentEvidence.build(
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        watermark="pg:1",
        pre_fence_terminal_ticket_count=0,
        captured_at=now,
    )
    return StorageRecoveryManifest.build(
        schema_version=1,
        recovery_set_id="recovery-integration-101",
        deployment_scope_id="deployment-integration-101",
        profile=RecoveryManifestProfile.COORDINATED,
        manifest_status=RecoveryManifestStatus.COHERENT,
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        quiescent_evidence=evidence,
        postgres_boundary="snapshot:pg-1",
        postgres_recovery_watermark="pg:1",
        object_volume_generation="volume:1",
        postgres_component_state=ComponentState.VERIFIED,
        object_component_state=ComponentState.VERIFIED,
        postgres_backup_identity="backup:pg-1",
        postgres_verification_reference="verify:pg-1",
        object_verification_reference="verify:volume-1",
        drained_operation_count=0,
        orphaned_operation_count=0,
        indeterminate_operation_count=0,
        scope="deployment-integration-101",
        created_at=now,
        completed_at=now,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_recovery_evidence_append_is_atomic(
    migrated_database: MigratedDatabase,
) -> None:
    engine = create_async_engine(migrated_database.migration.url.get_secret_value())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgreSQLBackupRecoveryRepository(session_factory=session_factory)
    recovery = RecoveryEvidenceCoordinator(repository=repository)
    manifest = _recovery_manifest_for_integration()

    try:
        async with repository.transaction(manifest.deployment_scope_id) as transaction:
            await transaction.add_manifest(manifest)

        first = await recovery.append_invalidation(
            manifest.deployment_scope_id,
            recovery_set_id=manifest.recovery_set_id,
            manifest_digest=manifest.manifest_digest,
            reason_code="component_identity_mismatch",
        )
        assert (
            await recovery.verify(manifest.deployment_scope_id)
        ).trust_state.value == "invalidated"

        second = RecoveryEvidenceInvalidation.build(
            deployment_scope_id=manifest.deployment_scope_id,
            sequence=2,
            recovery_set_id=manifest.recovery_set_id,
            manifest_digest=manifest.manifest_digest,
            event_kind=RecoveryEvidenceEventKind.INVALIDATED,
            reason_code="component_identity_mismatch",
            previous_event_digest=first.event_digest,
            occurred_at=manifest.created_at,
        )
        with pytest.raises(RuntimeError, match="simulated append crash"):
            async with repository.transaction(
                manifest.deployment_scope_id
            ) as transaction:
                await transaction.add_invalidation(second)
                raise RuntimeError("simulated append crash")

        async with repository.transaction(manifest.deployment_scope_id) as transaction:
            assert await transaction.list_invalidations(
                manifest.deployment_scope_id
            ) == (first,)
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "TRUNCATE spine.recovery_evidence_invalidations, "
                    "spine.storage_recovery_manifests, "
                    "spine.recovery_quiescence_evidence, "
                    "spine.fence_operation_tickets, spine.backup_fence_leases"
                )
            )
            await connection.commit()
        await engine.dispose()


@pytest.mark.asyncio(loop_scope="session")
async def test_source_canonical_validation_is_profile_bound(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    """Register the database-owned profile seam for real PostgreSQL execution."""

    async with _connection(migrated_database.runtime_url) as connection:
        assert await connection.scalar(text("SELECT current_user")) == RUNTIME_ROLE
        profile_rows = {
            row.table_name: (row.table_version, row.table_digest)
            for row in (
                await connection.execute(
                    text("SELECT table_name, table_version, table_digest FROM spine.source_canonical_tables")
                )
            )
        }
        assert profile_rows == {
            "unicode": ("15.1.0", "d769555163fd558132c9035e6ab04c6afc75b6140d88810de311c6b4126470b8"),
            "bcp47": ("2025-10-14+2026-09-17", "8473a3a50d7f7f5fa75bad8f376e6e0ec59f78a0ec9d3aab6606ecbe680a8d5c"),
            "bcp47_extensions": ("CLDR-48+IANA-2026-09-17", "3dce486a77d7efe3dcc58dc44de23f7ab505632157cdb5f4e5d7dadd3a1b898a"),
        }
        privileges = await connection.execute(
            text(
                "SELECT has_table_privilege(:runtime, 'spine.source_canonical_unicode', 'SELECT'), "
                "has_table_privilege(:runtime, 'spine.source_canonical_unicode', 'INSERT'), "
                "has_table_privilege(:runtime, 'spine.source_canonical_unicode', 'UPDATE')"
            ),
            {"runtime": RUNTIME_ROLE},
        )
        assert privileges.one() == (True, False, False)
        vectors = (
            ("Cafe\u0301", "Café", "en-US", "en-us"),
            ("\u1100\u1161", "가", "sl-ROZAJ-BISKE", "sl-rozaj-biske"),
            ("A\u030A", "Å", "en-u-ca-gregory", "en-u-ca-gregory"),
        )
        for value, expected_text, language, expected_language in vectors:
            canonical_text, canonical_language = (
                await connection.execute(
                    text(
                        "SELECT spine.source_canonical_nfc(:value), "
                        "spine.source_canonical_language(:language)"
                    ),
                    {"value": value, "language": language},
                )
            ).one()
            assert canonical_text == expected_text
            assert canonical_language == expected_language
        with pytest.raises((IntegrityError, DBAPIError)):
            await connection.execute(
                text("SELECT spine.source_canonical_language('en-zz')")
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_canonicalization_matches_shared_vectors(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    async with _connection(migrated_database.runtime_url) as connection:
        assert await connection.scalar(text("SELECT current_user")) == RUNTIME_ROLE
        for value, expected in UNICODE_VECTORS:
            actual = await connection.scalar(
                text("SELECT spine.source_canonical_nfc(:value)"),
                {"value": value},
            )
            assert actual == expected
        for language, expected in LANGUAGE_VECTORS:
            actual = await connection.scalar(
                text("SELECT spine.source_canonical_language(:language)"),
                {"language": language},
            )
            assert actual == expected
        for language in INVALID_LANGUAGE_TAGS:
            try:
                await connection.execute(
                    text("SELECT spine.source_canonical_language(:language)"),
                    {"language": language},
                )
            except (IntegrityError, DBAPIError):
                pass
            else:
                pytest.fail(f"PostgreSQL accepted invalid language tag: {language}")
            await connection.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_direct_sql_cannot_bypass_source_canonical_validation(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    source_id = UUID("40000000-0000-0000-0000-000000000011")
    revision_id = UUID("40000000-0000-0000-0000-000000000012")
    reference = (
        '{"schema_version":1,"object_id":"40000000-0000-0000-0000-000000000013",'
        '"storage_generation":"40000000-0000-0000-0000-000000000014",'
        '"digest_algorithm":"sha256","digest_hex":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"byte_length":1}'
    )
    insert_source = text(
        "INSERT INTO spine.source_objects "
        "(source_object_id, workspace_id, environment_id, source_kind, identity_mode, upload_identity, created_at) "
        "VALUES (:source_id, :workspace_id, :environment_id, 'document', 'upload', 'sql-canonical-test', now())"
    )
    insert_revision = text(
        "INSERT INTO spine.source_revisions "
        "(revision_id, source_object_id, workspace_id, environment_id, kind, revision_digest, "
        "revision_schema_version, revision_metadata_schema, revision_metadata, revision_metadata_digest, "
        "original_reference, original_sha256, byte_length, media_type, observed_at, "
        "canonicalization_profile, unicode_table_digest, bcp47_table_digest) "
        "VALUES (:revision_id, :source_id, :workspace_id, :environment_id, 'content', :revision_digest, "
        "'source-revision:v1', 'revision-metadata:r1-document-v1', CAST(:metadata AS jsonb), :metadata_digest, "
        "CAST(:reference AS jsonb), :sha, 1, 'text/plain', now(), 'r1-c14n-2026-10', :unicode_digest, :bcp_digest)"
    )
    common = {
        "source_id": source_id,
        "workspace_id": WORKSPACE_A,
        "environment_id": ENVIRONMENT_A,
        "reference": reference,
        "sha": "a" * 64,
        "unicode_digest": "d769555163fd558132c9035e6ab04c6afc75b6140d88810de311c6b4126470b8",
        "bcp_digest": "8473a3a50d7f7f5fa75bad8f376e6e0ec59f78a0ec9d3aab6606ecbe680a8d5c",
    }
    async with _connection(migrated_database.runtime_url) as connection:
        assert await connection.scalar(text("SELECT current_user")) == RUNTIME_ROLE
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(insert_source, common)
        await connection.commit()
        try:
            cases = (
                {"metadata": '{"embedded_title":"Cafe\\u0301"}', "metadata_digest": "c" * 64, "revision_digest": "b" * 64},
                {"metadata": '{"embedded_title":"Café","document_language":"en-us"}', "metadata_digest": "4220b54f52008ad8e811d63115836744d69106765438ca9d7fa670f0caa9b536", "revision_digest": "b" * 64},
            )
            for case in cases:
                with pytest.raises((IntegrityError, DBAPIError)):
                    await connection.execute(insert_revision, {**common, "revision_id": revision_id, **case})
                    await connection.commit()
                await connection.rollback()
        finally:
            await connection.rollback()
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "ALTER TABLE spine.source_objects "
                "DISABLE TRIGGER trg_source_objects_immutable"
            )
        )
        await connection.execute(
            text("DELETE FROM spine.source_objects WHERE source_object_id = :source_id"),
            {"source_id": source_id},
        )
        await connection.execute(
            text(
                "ALTER TABLE spine.source_objects "
                "ENABLE TRIGGER trg_source_objects_immutable"
            )
        )
        await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_source_observation_catalog_and_runtime_security_contract(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        assert await connection.scalar(text("SELECT current_user")) == MIGRATION_ROLE
        table_names = set(
            (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'spine' "
                        "AND tablename IN ('source_objects', 'source_revisions', "
                        "'source_revision_provenance', 'source_canonical_tables', "
                        "'source_canonical_unicode', 'source_canonical_compositions', "
                        "'source_canonical_bcp47')"
                    )
                )
            ).scalars()
        )
        assert table_names == {
            "source_objects",
            "source_revisions",
            "source_revision_provenance",
            "source_canonical_tables",
            "source_canonical_unicode",
            "source_canonical_compositions",
            "source_canonical_bcp47",
        }

        owners = {
            row.relname: row.owner
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname, pg_get_userbyid(relation.relowner) AS owner "
                        "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname IN "
                        "('source_objects', 'source_revisions', 'source_revision_provenance', "
                        "'source_canonical_tables', 'source_canonical_unicode', "
                        "'source_canonical_compositions', 'source_canonical_bcp47')"
                    )
                )
            )
        }
        assert set(owners) == table_names
        assert set(owners.values()) == {MIGRATION_ROLE}

        columns = {
            (row.table_name, row.column_name): row.data_type
            for row in (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = 'spine' AND table_name IN "
                        "('source_objects', 'source_revisions', 'source_revision_provenance')"
                    )
                )
            )
        }
        assert columns == {
            ("source_objects", "source_object_id"): "uuid",
            ("source_objects", "workspace_id"): "uuid",
            ("source_objects", "environment_id"): "uuid",
            ("source_objects", "source_kind"): "text",
            ("source_objects", "identity_mode"): "text",
            ("source_objects", "connection_id"): "uuid",
            ("source_objects", "external_namespace"): "text",
            ("source_objects", "external_generation"): "text",
            ("source_objects", "external_object_id"): "text",
            ("source_objects", "upload_identity"): "text",
            ("source_objects", "created_at"): "timestamp with time zone",
            ("source_revisions", "revision_id"): "uuid",
            ("source_revisions", "source_object_id"): "uuid",
            ("source_revisions", "workspace_id"): "uuid",
            ("source_revisions", "environment_id"): "uuid",
            ("source_revisions", "kind"): "text",
            ("source_revisions", "revision_digest"): "text",
            ("source_revisions", "revision_schema_version"): "text",
            ("source_revisions", "revision_metadata_schema"): "text",
            ("source_revisions", "revision_metadata"): "jsonb",
            ("source_revisions", "revision_metadata_digest"): "text",
            ("source_revisions", "original_reference"): "jsonb",
            ("source_revisions", "original_sha256"): "text",
            ("source_revisions", "byte_length"): "integer",
            ("source_revisions", "media_type"): "text",
            ("source_revisions", "reappearance_after_tombstone_revision_id"): "uuid",
            ("source_revisions", "deletion_reason"): "text",
            ("source_revisions", "deletion_provenance"): "text",
            ("source_revisions", "observed_at"): "timestamp with time zone",
            ("source_revisions", "canonicalization_profile"): "text",
            ("source_revisions", "unicode_table_digest"): "text",
            ("source_revisions", "bcp47_table_digest"): "text",
            ("source_revision_provenance", "provenance_id"): "uuid",
            ("source_revision_provenance", "source_object_id"): "uuid",
            ("source_revision_provenance", "revision_id"): "uuid",
            ("source_revision_provenance", "workspace_id"): "uuid",
            ("source_revision_provenance", "environment_id"): "uuid",
            ("source_revision_provenance", "producer_kind"): "text",
            ("source_revision_provenance", "producer_reference"): "text",
            ("source_revision_provenance", "event_identity"): "text",
            ("source_revision_provenance", "event_digest"): "text",
            ("source_revision_provenance", "connection_id"): "uuid",
            ("source_revision_provenance", "upload_command_reference"): "text",
            ("source_revision_provenance", "origin_locator_kind"): "text",
            ("source_revision_provenance", "origin_locator_value"): "text",
            ("source_revision_provenance", "origin_locator_schema"): "text",
            ("source_revision_provenance", "origin_locator_digest"): "text",
            ("source_revision_provenance", "order_scheme"): "text",
            ("source_revision_provenance", "order_token"): "text",
            ("source_revision_provenance", "observer_service"): "text",
            ("source_revision_provenance", "received_at"): "timestamp with time zone",
            ("source_revision_provenance", "observed_at"): "timestamp with time zone",
        }

        constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT constraint_record.conname FROM pg_constraint AS constraint_record "
                        "JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname IN "
                        "('source_objects', 'source_revisions', 'source_revision_provenance')"
                    )
                )
            ).scalars()
        )
        expected_source_constraints = {
            "pk_source_objects", "uq_source_objects_scope_id", "fk_source_objects_scope",
            "ck_source_objects_identity_mode", "ck_source_objects_identity_complete",
            "ck_source_objects_kind_format", "ck_source_objects_identity_format",
            "pk_source_revisions", "fk_source_revisions_source", "fk_source_revisions_reappearance",
            "uq_source_revisions_digest", "ck_source_revisions_kind", "uq_source_revisions_scope_id",
            "ck_source_revisions_digest", "ck_source_revisions_content_tombstone",
            "ck_source_revisions_schema_profile", "ck_source_revisions_original_digest",
            "ck_source_revisions_original_reference", "ck_source_revisions_media_type",
            "ck_source_revisions_metadata_shape", "ck_source_revisions_deletion_reason",
            "ck_source_revisions_deletion_provenance", "ck_source_revisions_canonical_profile",
            "ck_source_revisions_metadata_digest", "pk_source_revision_provenance",
            "fk_source_revision_provenance_revision", "uq_source_revision_provenance_event",
            "ck_source_revision_provenance_order_pair", "ck_source_revision_provenance_digest",
            "ck_source_revision_provenance_fields",
        }
        missing_source_constraints = {
            expected
            for expected in expected_source_constraints
            if expected not in constraints
            and not any(actual.endswith(expected) for actual in constraints)
            and not (
                expected
                in {
                    "ck_source_canonical_unicode_codepoint",
                    "ck_source_revision_provenance_order_pair",
                    "ck_source_revision_provenance_digest",
                    "ck_source_revision_provenance_fields",
                }
                and any(
                    actual.startswith("ck_source_revision_provenance_")
                    for actual in constraints
                )
            )
        }
        assert not missing_source_constraints, sorted(missing_source_constraints)
        assert len(
            [
                name
                for name in constraints
                if name.startswith("ck_source_revision_provenance_")
            ]
        ) == 3

        indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname = 'spine' "
                        "AND tablename IN ('source_objects', 'source_revisions', 'source_revision_provenance')"
                    )
                )
            ).scalars()
        )
        assert indexes == {
            "pk_source_objects", "uq_source_objects_scope_id", "uq_source_objects_connector_identity",
            "uq_source_objects_upload_identity", "ix_source_objects_connection_lookup",
            "pk_source_revisions", "uq_source_revisions_digest", "uq_source_revisions_scope_id",
            "ix_source_revisions_source_observed", "ix_source_revisions_original_digest",
            "pk_source_revision_provenance", "uq_source_revision_provenance_event",
            "uq_source_revision_provenance_scope_id",
            "ix_source_revision_provenance_revision_order",
        }

        rls = {
            row.relname: (row.relrowsecurity, row.relforcerowsecurity)
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname, relation.relrowsecurity, relation.relforcerowsecurity "
                        "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname IN "
                        "('source_objects', 'source_revisions', 'source_revision_provenance')"
                    )
                )
            )
        }
        assert rls == {
            "source_objects": (True, True),
            "source_revisions": (True, True),
            "source_revision_provenance": (True, True),
        }
        policies = {
            row.policy_name: (row.roles, row.using_expression, row.check_expression)
            for row in (
                await connection.execute(
                    text(
                        "SELECT policy.polname AS policy_name, "
                        "ARRAY(SELECT role.rolname FROM pg_roles AS role WHERE role.oid = ANY(policy.polroles)) AS roles, "
                        "pg_get_expr(policy.polqual, policy.polrelid) AS using_expression, "
                        "pg_get_expr(policy.polwithcheck, policy.polrelid) AS check_expression "
                        "FROM pg_policy AS policy JOIN pg_class AS relation ON relation.oid = policy.polrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname IN "
                        "('source_objects', 'source_revisions', 'source_revision_provenance')"
                    )
                )
            )
        }
        assert set(policies) == {
            "pol_source_objects_tenant_isolation", "pol_source_objects_migration_maintenance",
            "pol_source_revisions_tenant_isolation", "pol_source_revisions_migration_maintenance",
            "pol_source_revision_provenance_tenant_isolation", "pol_source_revision_provenance_migration_maintenance",
        }
        tenant_policies = [
            value for name, value in policies.items() if name.endswith("tenant_isolation")
        ]
        assert all("workspace_id" in using and "environment_id" in using for _, using, _ in tenant_policies)
        assert all("workspace_id" in check and "environment_id" in check for _, _, check in tenant_policies)

        privileges = {
            row.relname: (
                row.select_ok, row.insert_ok, row.update_ok, row.delete_ok,
                row.truncate_ok, row.references_ok, row.trigger_ok,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname, "
                        "has_table_privilege(:role, relation.oid, 'SELECT') AS select_ok, "
                        "has_table_privilege(:role, relation.oid, 'INSERT') AS insert_ok, "
                        "has_table_privilege(:role, relation.oid, 'UPDATE') AS update_ok, "
                        "has_table_privilege(:role, relation.oid, 'DELETE') AS delete_ok, "
                        "has_table_privilege(:role, relation.oid, 'TRUNCATE') AS truncate_ok, "
                        "has_table_privilege(:role, relation.oid, 'REFERENCES') AS references_ok, "
                        "has_table_privilege(:role, relation.oid, 'TRIGGER') AS trigger_ok "
                        "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN ('source_objects', 'source_revisions', "
                        "'source_revision_provenance', 'source_canonical_tables', 'source_canonical_unicode', "
                        "'source_canonical_compositions', 'source_canonical_bcp47')"
                    ),
                    {"role": RUNTIME_ROLE},
                )
            ).mappings()
        }
        assert privileges["source_objects"] == (True, True, False, False, False, False, False)
        assert privileges["source_revisions"] == (True, True, False, False, False, False, False)
        assert privileges["source_revision_provenance"] == (True, True, False, False, False, False, False)
        for table_name in (
            "source_canonical_tables", "source_canonical_unicode",
            "source_canonical_compositions", "source_canonical_bcp47",
        ):
            assert privileges[table_name] == (True, False, False, False, False, False, False)

        triggers = set(
            (
                await connection.execute(
                    text(
                        "SELECT trigger_name FROM information_schema.triggers "
                        "WHERE event_object_schema = 'spine' AND event_object_table IN "
                        "('source_objects', 'source_revisions', 'source_revision_provenance')"
                    )
                )
            ).scalars()
        )
        assert {
            "trg_source_objects_immutable", "trg_source_revisions_immutable",
            "trg_source_revision_provenance_immutable", "trg_source_reappearance",
            "trg_source_revision_canonical_validation",
        } <= triggers

        function_rows = {
            row.proname: row
            for row in (
                await connection.execute(
                    text(
                        "SELECT procedure.proname, pg_get_userbyid(procedure.proowner) AS owner, "
                        "procedure.prosecdef AS security_definer, procedure.provolatile AS volatility, "
                        "procedure.proconfig AS config, has_function_privilege('public', procedure.oid, 'EXECUTE') AS public_execute, "
                        "has_function_privilege(:runtime, procedure.oid, 'EXECUTE') AS runtime_execute "
                        "FROM pg_proc AS procedure JOIN pg_namespace AS namespace "
                        "ON namespace.oid = procedure.pronamespace WHERE namespace.nspname = 'spine' "
                        "AND procedure.proname IN ('source_canonical_nfc', 'source_canonical_trim', "
                        "'source_canonical_language', 'validate_source_revision_canonical', "
                        "'check_source_reappearance', 'reject_source_objects_mutation', "
                        "'reject_source_revisions_mutation', 'reject_source_revision_provenance_mutation', "
                        "'validate_source_receipt_association')"
                    ),
                    {"runtime": RUNTIME_ROLE},
                )
            ).mappings()
        }
        assert set(function_rows) == {
            "source_canonical_nfc", "source_canonical_trim", "source_canonical_language",
            "validate_source_revision_canonical", "check_source_reappearance",
            "reject_source_objects_mutation", "reject_source_revisions_mutation",
            "reject_source_revision_provenance_mutation",
            "validate_source_receipt_association",
        }
        for row in function_rows.values():
            assert row.owner == MIGRATION_ROLE
            assert row.security_definer is (
                row.proname == "validate_source_receipt_association"
            )
            expected_volatility = {
                "source_canonical_nfc": "i",
                "source_canonical_trim": "i",
                "source_canonical_language": "i",
                "validate_source_revision_canonical": "v",
                "check_source_reappearance": "v",
                "reject_source_objects_mutation": "v",
                "reject_source_revisions_mutation": "v",
                "reject_source_revision_provenance_mutation": "v",
                "validate_source_receipt_association": "v",
            }[row.proname]
            assert row.volatility == expected_volatility
            assert "search_path=pg_catalog, spine" in tuple(row.config or ())
            assert row.public_execute is False
            assert row.runtime_execute is (
                row.proname.startswith("source_canonical_")
                and row.proname != "validate_source_receipt_association"
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_role_cannot_mutate_profile_or_immutable_source_rows(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    source_id = UUID("40000000-0000-0000-0000-000000000021")
    async with _connection(migrated_database.runtime_url) as connection:
        assert await connection.scalar(text("SELECT current_user")) == RUNTIME_ROLE
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.source_objects "
                "(source_object_id, workspace_id, environment_id, source_kind, "
                "identity_mode, upload_identity, created_at) VALUES "
                "(:source_id, :workspace_id, :environment_id, 'document', "
                "'upload', 'runtime-security-test', now())"
            ),
            {
                "source_id": source_id,
                "workspace_id": WORKSPACE_A,
                "environment_id": ENVIRONMENT_A,
            },
        )
        await connection.commit()
        for statement in (
            "INSERT INTO spine.source_canonical_unicode "
            "(codepoint, combining_class, is_whitespace) VALUES (1, 0, false)",
            "UPDATE spine.source_objects SET source_kind = 'other' "
            "WHERE source_object_id = :source_id",
            "DELETE FROM spine.source_objects WHERE source_object_id = :source_id",
            "TRUNCATE spine.source_objects",
        ):
            with pytest.raises((ProgrammingError, DBAPIError)):
                await connection.execute(text(statement), {"source_id": source_id})
            await connection.rollback()

    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text("ALTER TABLE spine.source_objects DISABLE TRIGGER trg_source_objects_immutable")
        )
        await connection.execute(
            text("DELETE FROM spine.source_objects WHERE source_object_id = :source_id"),
            {"source_id": source_id},
        )
        await connection.execute(
            text("ALTER TABLE spine.source_objects ENABLE TRIGGER trg_source_objects_immutable")
        )
        await connection.commit()


async def _set_tenant_context(
    connection: AsyncConnection,
    *,
    workspace_id: UUID | str | None,
    environment_id: UUID | str | None = None,
) -> None:
    if workspace_id is not None:
        await connection.execute(
            text("SELECT set_config('spine.workspace_id', :value, true)"),
            {"value": str(workspace_id)},
        )
    if environment_id is not None:
        await connection.execute(
            text("SELECT set_config('spine.environment_id', :value, true)"),
            {"value": str(environment_id)},
        )


async def _assert_final_catalog_after_upgrade(connection: AsyncConnection) -> None:
    policy_tables = {
        "access_policy_versions",
        "access_policy_grants",
        "policy_proposals",
        "policy_approvals",
        "policy_activations",
        "current_access_policies",
        "original_upload_records",
        "qa_agent_artifacts",
        "qa_agent_results",
        "google_connection_versions",
        "google_connection_approvals",
    }
    tables = set(
        (
            await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'spine'")
            )
        ).scalars()
    )
    tables.difference_update(policy_tables)
    constraints = set(
        (
            await connection.execute(
                text(
                    "SELECT conname FROM pg_constraint AS constraint_record "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = constraint_record.connamespace "
                    "WHERE namespace.nspname = 'spine' AND constraint_record.conrelid NOT IN ("
                    "SELECT relation.oid FROM pg_class AS relation JOIN pg_namespace AS ns "
                    "ON ns.oid = relation.relnamespace WHERE ns.nspname = 'spine' AND "
                    "relation.relname IN ('access_policy_versions','access_policy_grants',"
                    "'policy_proposals','policy_approvals','policy_activations',"
                    "'current_access_policies','original_upload_records',"
                    "'qa_agent_artifacts','qa_agent_results',"
                    "'google_connection_versions','google_connection_approvals'))"
                )
            )
        ).scalars()
    )
    indexes = set(
        (
            await connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'spine' AND "
                     "tablename NOT IN ('access_policy_versions','access_policy_grants',"
                     "'policy_proposals','policy_approvals','policy_activations',"
                     "'current_access_policies','original_upload_records',"
                     "'qa_agent_artifacts','qa_agent_results',"
                     "'google_connection_versions','google_connection_approvals')")
            )
        ).scalars()
    )
    owners = set(
        (
            await connection.execute(
                text("SELECT tableowner FROM pg_tables WHERE schemaname = 'spine'")
            )
        ).scalars()
    )
    rls = {
        row.relname: (row.relrowsecurity, row.relforcerowsecurity)
        for row in (
            await connection.execute(
                text(
                    "SELECT relation.relname, relation.relrowsecurity, "
                    "relation.relforcerowsecurity FROM pg_class AS relation "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relkind = 'r'"
                )
            )
        )
        if row.relname != "alembic_version" and row.relname not in policy_tables
    }
    policies = {
        (row.table_name, row.policy_name): (
            tuple(row.roles),
            row.using_expression,
            row.check_expression,
        )
        for row in (
            await connection.execute(
                text(
                    "SELECT relation.relname AS table_name, policy.polname "
                    "AS policy_name, ARRAY(SELECT role.rolname FROM pg_roles AS role "
                    "WHERE role.oid = ANY(policy.polroles)) AS roles, "
                    "pg_get_expr(policy.polqual, policy.polrelid) AS using_expression, "
                    "pg_get_expr(policy.polwithcheck, policy.polrelid) "
                    "AS check_expression FROM pg_policy AS policy "
                    "JOIN pg_class AS relation ON relation.oid = policy.polrelid "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine'"
                )
            )
        )
        if row.table_name not in policy_tables
    }
    privilege_names = ("select_ok", "insert_ok", "update_ok", "delete_ok")
    privileges = {
        row.table_name: tuple(row[name] for name in privilege_names)
        for row in (
            await connection.execute(
                text(
                    "SELECT relation.relname AS table_name, "
                    "has_table_privilege(:role, relation.oid, 'SELECT') AS select_ok, "
                    "has_table_privilege(:role, relation.oid, 'INSERT') AS insert_ok, "
                    "has_table_privilege(:role, relation.oid, 'UPDATE') AS update_ok, "
                    "has_table_privilege(:role, relation.oid, 'DELETE') AS delete_ok "
                    "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relkind = 'r'"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).mappings()
        if row.table_name not in policy_tables
    }
    receipt_update_columns = {
        row.column_name
        for row in (
            await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'spine' "
                    "AND table_name = 'idempotency_receipts' "
                    "AND has_column_privilege(:role, "
                    "'spine.idempotency_receipts', column_name, 'UPDATE')"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).mappings()
    }
    revision = await connection.scalar(
        text("SELECT version_num FROM spine.alembic_version")
    )

    assert revision == HEAD_REVISION
    assert tables == {
        "alembic_version",
        "admission_decisions",
        "audit_events",
        "authentication_alias_bindings",
        "authorization_generations",
        "qualification_authority_bindings",
        "qualification_target_bindings",
        "canonical_human_identities",
        "canonical_human_identity_states",
        "environment_memberships",
        "environment_role_bindings",
        "environments",
        "context_profiles",
        "context_bundles",
        "answer_question_runs",
        "answer_question_attempts",
        "qa_session_versions",
        "qa_message_versions",
        "qa_disclosure_validation_results",
        "qa_answer_disclosure_records",
        "qa_answer_disclosure_deliveries",
        "feedback_records",
        "operational_metric_events",
        "operational_correlation_records",
        "idempotency_receipts",
        "initial_workspace_bootstrap",
        "outbox_intents",
        "outbox_deliveries",
        "workspace_memberships",
        "workspaces",
        "source_canonical_tables",
        "source_canonical_unicode",
        "source_canonical_compositions",
        "source_canonical_bcp47",
        "source_objects",
        "source_revisions",
        "source_revision_provenance",
        "source_revision_receipt_associations",
        "ingestion_runs",
        "ingestion_run_sources",
        "ingestion_run_source_bindings",
        "ingestion_transitions",
        "ingestion_warnings",
        "ingestion_failures",
        "processing_receipts",
        "process_only_operation_lineages",
        "source_security_domain_bindings",
        "backup_fence_leases",
        "fence_operation_tickets",
        "recovery_quiescence_evidence",
        "storage_recovery_manifests",
        "recovery_evidence_invalidations",
        "canonical_generation_counters",
        "canonical_acceptances",
        "current_source_heads",
        "parsed_documents",
        "parsed_document_segments",
        "canonical_boundaries",
        "canonical_boundary_members",
        "ontology_versions",
        "projection_config_versions",
        "projection_runs",
        "projection_run_transitions",
        "projection_partitions",
        "projection_receipts",
        "source_projection_references",
        "projection_validations",
        "projection_snapshots",
        "projection_snapshot_members",
        "projection_reconciliation_results",
        "projection_activations",
        "active_projections",
        "projection_drifts",
        "drive_snapshot_runs",
        "drive_change_runs",
        "google_sheets_sync_runs",
        "google_sheets_identity_dispositions",
        "drive_lifecycle_decisions",
    }
    expected_final_constraints = {
        "alembic_version_pkc",
        "ck_environments_display_name_not_empty",
        "ck_environments_kind",
        "ck_idempotency_receipts_command_digest_sha256",
        "ck_idempotency_receipts_idempotency_key_length",
        "ck_idempotency_receipts_operation_schema_version_positive",
        "ck_idempotency_receipts_result_complete",
        "ck_initial_workspace_bootstrap_singleton",
        "ck_outbox_intents_aggregate_complete",
        "ck_outbox_intents_event_schema_version_positive",
        "ck_outbox_intents_event_type_identifier",
        "ck_outbox_intents_payload_object",
        "ck_outbox_intents_producer_deduplication_id_length",
        "ck_workspaces_display_name_not_empty",
        "ck_workspaces_slug_not_empty",
        "ck_drive_snapshot_runs_state",
        "ck_drive_snapshot_runs_start_token",
        "ck_drive_change_runs_state",
        "ck_drive_change_runs_cursor",
        "ck_google_sheets_sync_runs_state",
        "ck_google_sheets_identity_dispositions_shape",
        "ck_drive_lifecycle_decisions_state",
        "ck_drive_lifecycle_file_id",
        "ck_drive_lifecycle_decisions_committed_cursor",
        "fk_environments_workspace_id_workspaces",
        "fk_idempotency_receipts_scope_environments",
        "fk_idempotency_receipts_workspace_id_workspaces",
        "fk_initial_workspace_bootstrap_workspace_id_workspaces",
        "fk_outbox_intents_scope_environments",
        "fk_outbox_intents_workspace_id_workspaces",
        "fk_drive_snapshot_runs_environment",
        "fk_drive_change_runs_environment",
        "fk_google_sheets_sync_runs_environment",
        "fk_google_sheets_identity_disposition_environment",
        "fk_google_sheets_identity_disposition_run",
        "fk_drive_lifecycle_decisions_environment",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "pk_outbox_intents",
        "pk_workspaces",
        "pk_drive_snapshot_runs",
        "pk_drive_change_runs",
        "pk_google_sheets_sync_runs",
        "pk_google_sheets_identity_dispositions",
        "pk_drive_lifecycle_decisions",
        "uq_environments_workspace_id_id",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_workspaces_slug",
        "uq_drive_snapshot_runs_idempotency",
        "uq_drive_change_runs_idempotency",
        "uq_google_sheets_sync_runs_idempotency",
        "uq_google_sheets_identity_disposition_run_scope",
        "pk_source_canonical_tables",
        "pk_source_canonical_unicode",
        "pk_source_canonical_compositions",
        "pk_source_canonical_bcp47",
        "ck_source_canonical_tables_digest",
        "ck_source_canonical_unicode_codepoint",
        "ck_source_canonical_unicode_ccc",
        "ck_source_canonical_bcp47_category",
        "pk_source_objects",
        "uq_source_objects_scope_id",
        "fk_source_objects_scope",
        "ck_source_objects_identity_mode",
        "ck_source_objects_identity_complete",
        "ck_source_objects_kind_format",
        "ck_source_objects_identity_format",
        "pk_source_revisions",
        "fk_source_revisions_source",
        "fk_source_revisions_reappearance",
        "uq_source_revisions_digest",
        "ck_source_revisions_kind",
        "uq_source_revisions_scope_id",
        "ck_source_revisions_digest",
        "ck_source_revisions_content_tombstone",
        "ck_source_revisions_schema_profile",
        "ck_source_revisions_original_digest",
        "ck_source_revisions_original_reference",
        "ck_source_revisions_media_type",
        "ck_source_revisions_metadata_shape",
        "ck_source_revisions_deletion_reason",
        "ck_source_revisions_deletion_provenance",
        "ck_source_revisions_canonical_profile",
        "ck_source_revisions_metadata_digest",
        "pk_source_revision_provenance",
        "fk_source_revision_provenance_revision",
        "uq_source_revision_provenance_event",
        "ck_source_revision_provenance_order_pair",
        "ck_source_revision_provenance_digest",
        "ck_source_revision_provenance_fields",
        "pk_source_receipt_associations",
        "uq_source_receipt_associations_receipt",
        "uq_source_receipt_associations_provenance",
        "fk_source_receipt_associations_revision",
        "fk_source_receipt_associations_provenance",
        "fk_source_receipt_associations_receipt",
        "ck_source_receipt_associations_verified",
        "ck_source_receipt_associations_reference",
        "pk_ingestion_runs",
        "uq_ingestion_runs_scope_id",
        "uq_ingestion_runs_command",
        "fk_ingestion_runs_environment",
        "fk_ingestion_runs_receipt",
        "ck_ingestion_runs_trigger_kind",
        "ck_ingestion_runs_scope_schema",
        "ck_ingestion_runs_digests",
        "ck_ingestion_runs_command_identity",
        "pk_ingestion_run_sources",
        "uq_ingestion_run_sources_scope_id",
        "uq_ingestion_run_sources_ordinal",
        "uq_ingestion_run_sources_input",
        "uq_ingestion_run_sources_command",
        "fk_ingestion_run_sources_run",
        "ck_ingestion_run_sources_ordinal_positive",
        "ck_ingestion_run_sources_digests",
        "pk_ingestion_run_source_bindings",
        "uq_ingestion_run_source_bindings_scope_id",
        "uq_ingestion_run_source_bindings_item",
        "uq_ingestion_run_source_bindings_command",
        "fk_ingestion_run_source_bindings_item",
        "fk_ingestion_run_source_bindings_revision",
        "fk_ingestion_run_source_bindings_receipt",
        "fk_ingestion_run_source_bindings_original_receipt",
        "ck_ingestion_run_source_bindings_receipt_complete",
        "ck_ingestion_run_source_bindings_digest",
        "pk_ingestion_transitions",
        "uq_ingestion_transitions_scope_id",
        "fk_ingestion_transitions_run",
        "fk_ingestion_transitions_item",
        "fk_ingestion_transitions_predecessor",
        "ck_ingestion_transitions_target_kind",
        "ck_ingestion_transitions_target_state",
        "ck_ingestion_transitions_attempt_positive",
        "ck_ingestion_transitions_safe_metrics",
        "pk_ingestion_warnings",
        "uq_ingestion_warnings_scope_id",
        "fk_ingestion_warnings_item",
        "ck_ingestion_warnings_attempt_positive",
        "ck_ingestion_warnings_stage",
        "ck_ingestion_warnings_code",
        "ck_ingestion_warnings_safe_detail",
        "ck_ingestion_warnings_safe_detail_digest",
        "pk_ingestion_failures",
        "uq_ingestion_failures_scope_id",
        "fk_ingestion_failures_item",
        "ck_ingestion_failures_attempt_positive",
        "ck_ingestion_failures_stage",
        "ck_ingestion_failures_code",
        "ck_ingestion_failures_safe_detail",
        "ck_ingestion_failures_safe_detail_digest",
        "pk_processing_receipts",
        "uq_processing_receipts_scope_id",
        "uq_processing_receipts_command",
        "fk_processing_receipts_item",
        "fk_processing_receipts_binding",
        "fk_processing_receipts_revision",
        "fk_processing_receipts_receipt",
        "ck_processing_receipts_coverage",
        "ck_processing_receipts_locator_version",
        "ck_processing_receipts_output",
        "ck_processing_receipts_complete_exclusions",
        "ck_processing_receipts_complete_failures",
        "ck_processing_receipts_digests",
        "pk_process_only_operation_lineages",
        "uq_process_only_lineages_scope_id",
        "uq_process_only_lineages_run",
        "fk_process_only_lineages_run",
        "fk_process_only_lineages_item",
        "fk_process_only_lineages_revision",
        "fk_process_only_lineages_receipt",
        "ck_process_only_operation_lineages_ck_process_only_line_b9b7",
        "ck_process_only_operation_lineages_ck_process_only_line_8f72",
        "pk_source_security_domain_bindings",
        "uq_source_security_domain_bindings_scope_id",
        "uq_source_security_domain_bindings_source_version",
        "fk_source_security_domain_bindings_source",
        "fk_source_security_domain_bindings_predecessor",
        "fk_source_security_domain_bindings_activation",
        "ck_source_security_domain_bindings_shape",
        "ck_source_security_domain_bindings_version",
        "ck_source_security_domain_bindings_chain",
        "pk_backup_fence_leases",
        "ck_backup_fence_leases_scope",
        "ck_backup_fence_leases_state",
        "ck_backup_fence_leases_admission",
        "pk_fence_operation_tickets",
        "fk_fence_operation_tickets_lease",
        "ck_fence_operation_tickets_sequence",
        "ck_fence_operation_tickets_identifiers",
        "pk_recovery_quiescence_evidence",
        "fk_recovery_quiescence_evidence_lease",
        "ck_recovery_quiescence_evidence_identifiers",
        "ck_recovery_quiescence_evidence_counts",
        "ck_recovery_quiescence_evidence_digest",
        "pk_storage_recovery_manifests",
        "fk_storage_recovery_manifests_lease",
        "ck_storage_recovery_manifests_profile",
        "ck_storage_recovery_manifests_status",
        "ck_storage_recovery_manifests_digest",
        "ck_storage_recovery_manifests_counts",
        "ck_storage_recovery_manifests_profile_status",
        "ck_storage_recovery_manifests_coherent_evidence",
        "pk_recovery_evidence_invalidations",
        "fk_recovery_evidence_invalidations_lease",
        "ck_recovery_evidence_invalidations_identifiers",
        "ck_recovery_evidence_invalidations_sequence_positive",
        "ck_recovery_evidence_invalidations_predecessor",
        "ck_recovery_evidence_invalidations_digests",
        "ck_recovery_evidence_invalidations_event_kind",
        "ck_recovery_evidence_invalidations_replacement",
        "pk_parsed_documents",
        "uq_parsed_documents_scope_id",
        "uq_parsed_documents_digest",
        "uq_parsed_documents_command",
        "fk_parsed_documents_revision",
        "ck_parsed_documents_schema",
        "ck_parsed_documents_coverage",
        "ck_parsed_documents_digests",
        "ck_parsed_documents_exclusions",
        "pk_parsed_document_segments",
        "uq_parsed_segments_digest",
        "uq_parsed_segments_ordinal",
        "fk_parsed_segments_document",
        "ck_parsed_segments_schema",
        "ck_parsed_segments_identity",
        "ck_parsed_segments_metadata",
        "pk_ontology_versions",
        "uq_ontology_versions_scope_id",
        "uq_ontology_versions_name_version",
        "uq_ontology_versions_name_digest",
        "fk_ontology_versions_predecessor",
        "ck_ontology_versions_version",
        "ck_ontology_versions_schema",
        "ck_ontology_versions_digest",
        "pk_projection_config_versions",
        "uq_projection_configs_scope_id",
        "uq_projection_configs_kind_version",
        "uq_projection_configs_kind_digest",
        "fk_projection_configs_ontology",
        "fk_projection_configs_predecessor",
        "ck_projection_configs_version",
        "ck_projection_configs_schema",
        "ck_projection_configs_digest",
        "pk_projection_runs",
        "uq_projection_runs_candidate_target",
        "uq_projection_runs_candidate",
        "uq_projection_runs_command",
        "fk_projection_runs_config",
        "fk_projection_runs_boundary",
        "fk_projection_runs_idempotency",
        "ck_projection_runs_operation",
        "ck_projection_runs_candidate_state",
        "ck_projection_runs_open_or_sealed",
        "pk_projection_run_transitions",
        "uq_projection_transitions_predecessor",
        "fk_projection_transitions_run",
        "ck_projection_transitions_state",
        "ck_projection_transitions_attempt",
        "pk_projection_partitions",
        "uq_projection_partitions_scope_id",
        "uq_projection_partitions_identity",
        "uq_projection_partitions_candidate_domain",
        "fk_projection_partitions_run",
        "pk_projection_receipts",
        "uq_projection_receipts_scope_id",
        "uq_projection_receipts_command",
        "uq_projection_receipts_partition_command",
        "fk_projection_receipts_run",
        "fk_projection_receipts_partition",
        "fk_projection_receipts_idempotency",
        "ck_projection_receipts_command_digest",
        "pk_source_projection_references",
        "uq_source_projection_references_projected_object",
        "fk_source_projection_references_run",
        "fk_source_projection_references_receipt",
        "fk_source_projection_references_source",
        "fk_source_projection_references_revision",
        "fk_source_projection_references_processing_receipt",
        "fk_source_projection_references_config",
        "pk_projection_validations",
        "uq_projection_validations_scope_id",
        "uq_projection_validations_gate",
        "fk_projection_validations_run",
        "ck_projection_validations_input_digest",
        "ck_projection_validations_result_digest",
        "ck_projection_validations_decision",
        "pk_projection_snapshots",
        "uq_projection_snapshots_scope_id",
        "uq_projection_snapshots_candidate",
        "uq_projection_snapshots_kind_manifest",
        "uq_projection_snapshots_kind_evidence",
        "fk_projection_snapshots_run",
        "fk_projection_snapshots_config",
        "fk_projection_snapshots_boundary",
        "fk_projection_snapshots_predecessor",
        "ck_projection_snapshots_state",
        "ck_projection_snapshots_evidence_digest",
        "ck_projection_snapshots_manifest_digest",
        "ck_projection_snapshots_member_count",
        "pk_projection_snapshot_members",
        "uq_projection_snapshot_members_scope_id",
        "uq_projection_snapshot_members_source",
        "uq_projection_snapshot_members_boundary",
        "fk_projection_snapshot_members_snapshot",
        "fk_projection_snapshot_members_boundary_member",
        "fk_projection_snapshot_members_revision",
        "fk_projection_snapshot_members_partition",
        "fk_projection_snapshot_members_receipt",
        "ck_projection_snapshot_members_disposition",
        "ck_projection_snapshot_members_shape",
        "pk_projection_reconciliation_results",
        "uq_projection_reconciliation_results_scope_id",
        "uq_projection_reconciliation_results_attempt",
        "uq_projection_reconciliation_results_target",
        "fk_projection_reconciliation_results_snapshot",
        "fk_projection_reconciliation_results_predecessor",
        "fk_projection_reconciliation_results_idempotency",
        "ck_projection_reconciliation_results_generation",
        "ck_projection_reconciliation_results_outcome",
        "ck_projection_reconciliation_results_attempt_digest",
        "ck_projection_reconciliation_results_findings_digest",
        "ck_projection_reconciliation_results_artifact_digest",
        "fk_projection_runs_predecessor_snapshot",
        "pk_projection_activations",
        "uq_projection_activations_scope_id",
        "uq_projection_activations_generation",
        "uq_projection_activations_snapshot",
        "uq_projection_activations_pointer_target",
        "fk_projection_activations_snapshot",
        "fk_projection_activations_predecessor",
        "fk_projection_activations_idempotency",
        "fk_projection_activations_audit",
        "ck_projection_activations_generation",
        "ck_projection_activations_kind",
        "ck_projection_activations_digest",
        "pk_active_projections",
        "fk_active_projections_activation",
        "ck_active_projections_generation",
        "ck_active_projections_cas",
        "pk_projection_drifts",
        "uq_projection_drifts_scope_id",
        "uq_projection_drifts_activation_acceptance",
        "fk_projection_drifts_activation",
        "fk_projection_drifts_acceptance",
        "ck_projection_drifts_activation_generation",
        "ck_projection_drifts_accepted_generation",
        "ck_projection_drifts_detected_by",
        "pk_context_profiles",
        "uq_context_profiles_identity_digest",
        "fk_context_profiles_environment",
        "ck_context_profiles_version_positive",
        "ck_context_profiles_schema",
        "ck_context_profiles_digest",
        "ck_context_profiles_strategy",
        "ck_context_profiles_bounds",
        "ck_context_profiles_completeness",
        "ck_context_profiles_partial_fallback",
        "ck_context_profiles_server_approved",
            "pk_context_bundles",
            "uq_context_bundles_attempt",
            "uq_context_bundles_exact_identity",
        "fk_context_bundles_environment",
        "fk_context_bundles_profile",
        "ck_context_bundles_positive_versions",
        "ck_context_bundles_schema",
        "ck_context_bundles_digests",
        "ck_context_bundles_publication_state",
        "ck_context_bundles_outcome",
        "ck_context_bundles_state",
        "pk_answer_question_runs",
        "fk_answer_question_runs_environment",
        "uq_answer_question_runs_idempotency",
        "ck_answer_question_runs_digest",
        "ck_answer_question_runs_state",
        "ck_answer_question_runs_outcome",
        "ck_answer_question_runs_terminal",
        "pk_answer_question_attempts",
        "fk_answer_question_attempts_run",
        "fk_answer_question_attempts_context_bundle",
        "uq_answer_question_attempts_number",
        "uq_answer_question_attempts_logical_key",
        "ck_answer_question_attempts_positive",
        "ck_answer_question_attempts_state",
        "ck_answer_question_attempts_outcome",
        "ck_answer_question_attempts_terminal",
        "ck_answer_question_attempts_bundle_ref",
        "ck_answer_question_attempts_logical_key",
        "pk_qa_session_versions",
        "fk_qa_session_versions_environment",
        "ck_qa_session_versions_version_positive",
        "ck_qa_session_versions_status",
        "pk_qa_message_versions",
        "fk_qa_message_versions_environment",
        "ck_qa_message_versions_version_positive",
        "ck_qa_message_versions_ordinal_positive",
        "ck_qa_message_versions_state",
        "ck_qa_message_versions_outcome",
        "pk_qa_disclosure_validation_results",
        "fk_qa_disclosure_validation_results_environment",
        "ck_qa_disclosure_validation_results_checkpoint",
        "ck_qa_disclosure_validation_results_disposition",
        "pk_qa_answer_disclosure_records",
        "fk_qa_answer_disclosure_records_environment",
        "ck_qa_answer_disclosure_records_ck_qa_answer_disclosure_84df",
        "pk_qa_answer_disclosure_deliveries",
        "fk_qa_answer_disclosure_deliveries_record",
        "pk_feedback_records",
        "fk_feedback_records_environment",
        "ck_feedback_records_version",
        "ck_feedback_records_target_kind",
        "ck_feedback_records_outcome",
        "ck_feedback_records_bounded_text",
        "pk_operational_metric_events",
        "uq_operational_metric_events_deduplication",
        "ck_operational_metric_attempt_number",
        "ck_operational_metric_duration",
        "ck_operational_metric_sequence",
        "ck_operational_metric_currency",
        "ck_operational_metric_cost",
        "ck_operational_metric_answer_rate",
        "ck_operational_metric_usefulness",
        "pk_operational_correlation_records",
        "uq_operational_correlation_records_deduplication",
        "ck_operational_correlation_records_ck_operational_corre_855c",
        "ck_operational_correlation_records_ck_operational_corre_57b7",
        "ck_operational_correlation_records_ck_operational_corre_5b37",
        "ck_operational_correlation_records_ck_operational_corre_93e5",
    }
    expected_final_constraints |= (
        AUTHORIZATION_DIRECTORY_CONSTRAINTS
        | AUDIT_CONSTRAINTS
        | DELIVERY_CONSTRAINTS
        | ADMISSION_CONSTRAINTS
    )
    disclosure_constraint_prefix = (
        "ck_qa_disclosure_validation_results_ck_qa_disclosure_"
    )
    disclosure_constraints = {
        actual
        for actual in constraints
        if actual.startswith(disclosure_constraint_prefix)
    }
    assert len(disclosure_constraints) == 2, disclosure_constraints
    missing_final_constraints = {
        expected
        for expected in expected_final_constraints
        if expected not in constraints
        and not any(actual.endswith(expected) for actual in constraints)
        and not (
            expected
            in {
                "ck_qa_disclosure_validation_results_checkpoint",
                "ck_qa_disclosure_validation_results_disposition",
            }
            and disclosure_constraints
        )
        and not (
            expected
            in {
                "ck_source_revision_provenance_order_pair",
                "ck_source_revision_provenance_digest",
                "ck_source_revision_provenance_fields",
            }
            and any(
                actual.startswith("ck_source_revision_provenance_")
                for actual in constraints
            )
        )
        and not (
            expected == "ck_source_canonical_unicode_codepoint"
            and any(
                actual.startswith("ck_source_canonical_unicode_")
                for actual in constraints
            )
        )
        and not (
            expected
            in {
                "ck_projection_validations_input_digest",
                "ck_projection_validations_result_digest",
                "ck_projection_validations_decision",
                "ck_projection_snapshots_state",
                "ck_projection_snapshots_evidence_digest",
                "ck_projection_snapshots_manifest_digest",
                "ck_projection_snapshots_member_count",
                "ck_projection_snapshot_members_disposition",
                "ck_projection_snapshot_members_shape",
                "ck_projection_reconciliation_results_generation",
                "ck_projection_reconciliation_results_outcome",
                "ck_projection_reconciliation_results_attempt_digest",
                "ck_projection_reconciliation_results_findings_digest",
                "ck_projection_reconciliation_results_artifact_digest",
            }
            and any(
                actual.startswith(prefix)
                for prefix in (
                    "ck_projection_validations_",
                    "ck_projection_snapshots_",
                    "ck_projection_snapshot_members_",
                    "ck_projection_reconciliation_results_",
                )
                for actual in constraints
            )
        )
        and not (
            expected
            in {
                "ck_source_security_domain_bindings_shape",
                "ck_source_security_domain_bindings_version",
                "ck_source_security_domain_bindings_chain",
            }
            and any(
                actual.startswith("ck_source_security_domain_bindings_")
                for actual in constraints
            )
        )
    }
    projection_check_constraints = {
        "projection_validations": {
            "ck_projection_validations_input_digest",
            "ck_projection_validations_result_digest",
            "ck_projection_validations_decision",
        },
        "projection_snapshots": {
            "ck_projection_snapshots_state",
            "ck_projection_snapshots_evidence_digest",
            "ck_projection_snapshots_manifest_digest",
            "ck_projection_snapshots_member_count",
        },
        "projection_snapshot_members": {
            "ck_projection_snapshot_members_disposition",
            "ck_projection_snapshot_members_shape",
        },
        "projection_reconciliation_results": {
            "ck_projection_reconciliation_results_generation",
            "ck_projection_reconciliation_results_outcome",
            "ck_projection_reconciliation_results_attempt_digest",
            "ck_projection_reconciliation_results_findings_digest",
            "ck_projection_reconciliation_results_artifact_digest",
        },
        "projection_activations": {
            "ck_projection_activations_generation",
            "ck_projection_activations_kind",
            "ck_projection_activations_digest",
        },
        "active_projections": {
            "ck_active_projections_generation",
            "ck_active_projections_cas",
        },
        "projection_drifts": {
            "ck_projection_drifts_activation_generation",
            "ck_projection_drifts_accepted_generation",
            "ck_projection_drifts_detected_by",
        },
    }
    for table_name, expected_checks in projection_check_constraints.items():
        actual_checks = {
            actual
            for actual in constraints
            if actual.startswith(f"ck_{table_name}_")
        }
        assert len(actual_checks) == len(expected_checks), (table_name, actual_checks)
    source_security_domain_binding_checks = {
        actual
        for actual in constraints
        if actual.startswith("ck_source_security_domain_bindings_")
    }
    assert len(source_security_domain_binding_checks) == 3, (
        source_security_domain_binding_checks
    )
    assert not missing_final_constraints, sorted(missing_final_constraints)
    outbox_fk = (
        (
            await connection.execute(
                text(
                    "SELECT constraint_record.conname, "
                    "constraint_record.condeferrable, "
                    "constraint_record.condeferred, "
                    "pg_get_constraintdef(constraint_record.oid) AS definition "
                    "FROM pg_constraint AS constraint_record "
                    "JOIN pg_class AS source_relation "
                    "ON source_relation.oid = constraint_record.conrelid "
                    "JOIN pg_class AS referenced_relation "
                    "ON referenced_relation.oid = constraint_record.confrelid "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = source_relation.relnamespace "
                    "JOIN pg_attribute AS source_column "
                    "ON source_column.attrelid = source_relation.oid "
                    "AND source_column.attnum = ANY(constraint_record.conkey) "
                    "JOIN pg_attribute AS referenced_column "
                    "ON referenced_column.attrelid = referenced_relation.oid "
                    "AND referenced_column.attnum = ANY(constraint_record.confkey) "
                    "WHERE namespace.nspname = 'spine' "
                    "AND constraint_record.contype = 'f' "
                    "AND source_relation.relname = 'canonical_acceptances' "
                    "AND source_column.attname = 'outbox_event_id' "
                    "AND referenced_relation.relname = 'outbox_intents' "
                    "AND referenced_column.attname = 'event_id'"
                )
            )
        )
        .mappings()
        .one()
    )
    assert outbox_fk.condeferrable is True
    assert outbox_fk.condeferred is True
    assert "FOREIGN KEY (outbox_event_id)" in outbox_fk.definition
    assert "outbox_intents(event_id)" in outbox_fk.definition
    assert indexes == {
        "alembic_version_pkc",
        "ix_environments_workspace_id",
        "ix_google_sheets_identity_disposition_scope",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "pk_outbox_intents",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_idempotency_receipts_environment_key",
        "uq_idempotency_receipts_workspace_key",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_outbox_intents_environment_producer",
        "uq_outbox_intents_workspace_producer",
        "uq_workspaces_slug",
        "pk_drive_snapshot_runs",
        "uq_drive_snapshot_runs_idempotency",
        "pk_drive_change_runs",
        "uq_drive_change_runs_idempotency",
        "pk_google_sheets_sync_runs",
        "pk_google_sheets_identity_dispositions",
        "pk_drive_lifecycle_decisions",
        "uq_google_sheets_sync_runs_idempotency",
        "uq_google_sheets_identity_disposition_run_scope",
        "pk_source_canonical_tables",
        "pk_source_canonical_unicode",
        "pk_source_canonical_compositions",
        "pk_source_canonical_bcp47",
        "pk_source_objects",
        "uq_source_objects_scope_id",
        "uq_source_objects_connector_identity",
        "uq_source_objects_upload_identity",
        "ix_source_objects_connection_lookup",
        "pk_source_revisions",
        "uq_source_revisions_digest",
        "uq_source_revisions_scope_id",
        "ix_source_revisions_source_observed",
        "ix_source_revisions_original_digest",
        "pk_source_revision_provenance",
        "uq_source_revision_provenance_event",
        "uq_source_revision_provenance_scope_id",
        "ix_source_revision_provenance_revision_order",
        "pk_source_receipt_associations",
        "uq_source_receipt_associations_receipt",
        "uq_source_receipt_associations_provenance",
        "ix_source_receipt_associations_revision",
        "ix_source_receipt_associations_object_digest",
        "pk_ingestion_runs",
        "uq_ingestion_runs_scope_id",
        "uq_ingestion_runs_command",
        "ix_ingestion_runs_requested",
        "pk_ingestion_run_sources",
        "uq_ingestion_run_sources_scope_id",
        "uq_ingestion_run_sources_ordinal",
        "uq_ingestion_run_sources_input",
        "uq_ingestion_run_sources_command",
        "ix_ingestion_run_sources_run",
        "pk_ingestion_run_source_bindings",
        "uq_ingestion_run_source_bindings_scope_id",
        "uq_ingestion_run_source_bindings_item",
        "uq_ingestion_run_source_bindings_command",
        "ix_ingestion_run_source_bindings_revision",
        "pk_ingestion_transitions",
        "uq_ingestion_transitions_scope_id",
        "uq_ingestion_transitions_root",
        "uq_ingestion_transitions_predecessor",
        "ix_ingestion_transitions_target",
        "pk_ingestion_warnings",
        "uq_ingestion_warnings_scope_id",
        "uq_ingestion_warnings_dedup",
        "ix_ingestion_warnings_item",
        "pk_ingestion_failures",
        "uq_ingestion_failures_scope_id",
        "uq_ingestion_failures_dedup",
        "ix_ingestion_failures_item",
        "pk_processing_receipts",
        "uq_processing_receipts_scope_id",
        "uq_processing_receipts_command",
        "ix_processing_receipts_revision",
        "ix_processing_receipts_item",
        "pk_source_security_domain_bindings",
        "uq_source_security_domain_bindings_scope_id",
        "uq_source_security_domain_bindings_source_version",
        "pk_backup_fence_leases",
        "pk_fence_operation_tickets",
        "uq_fence_operation_tickets_scope_sequence",
        "ix_fence_operation_tickets_scope_state",
        "pk_recovery_quiescence_evidence",
        "pk_storage_recovery_manifests",
        "ix_storage_recovery_manifests_scope_created",
        "pk_recovery_evidence_invalidations",
        "ix_recovery_evidence_invalidations_scope_digest",
        "pk_parsed_documents",
        "uq_parsed_documents_scope_id",
        "uq_parsed_documents_digest",
        "uq_parsed_documents_command",
        "ix_parsed_documents_revision",
        "pk_parsed_document_segments",
        "uq_parsed_segments_digest",
        "uq_parsed_segments_ordinal",
        "ix_parsed_segments_artifact",
        "pk_ontology_versions",
        "uq_ontology_versions_scope_id",
        "uq_ontology_versions_name_version",
        "uq_ontology_versions_name_digest",
        "pk_projection_config_versions",
        "uq_projection_configs_scope_id",
        "uq_projection_configs_kind_version",
        "uq_projection_configs_kind_digest",
        "pk_projection_runs",
        "uq_projection_runs_candidate_target",
        "uq_projection_runs_candidate",
        "uq_projection_runs_command",
        "ix_projection_runs_scope_state",
        "pk_projection_run_transitions",
        "uq_projection_transitions_predecessor",
        "uq_projection_transitions_root",
        "uq_projection_transitions_successor",
        "pk_projection_partitions",
        "uq_projection_partitions_scope_id",
        "uq_projection_partitions_identity",
        "uq_projection_partitions_candidate_domain",
        "ix_projection_partitions_candidate",
        "pk_projection_receipts",
        "uq_projection_receipts_scope_id",
        "uq_projection_receipts_command",
        "uq_projection_receipts_partition_command",
        "ix_projection_receipts_candidate",
        "pk_source_projection_references",
        "uq_source_projection_references_projected_object",
        "ix_source_projection_references_source",
        "uq_canonical_boundary_members_scope_member",
        "pk_projection_validations",
        "uq_projection_validations_scope_id",
        "uq_projection_validations_gate",
        "pk_projection_snapshots",
        "uq_projection_snapshots_scope_id",
        "uq_projection_snapshots_candidate",
        "uq_projection_snapshots_kind_manifest",
        "uq_projection_snapshots_kind_evidence",
        "pk_projection_snapshot_members",
        "uq_projection_snapshot_members_scope_id",
        "uq_projection_snapshot_members_source",
        "uq_projection_snapshot_members_boundary",
        "pk_projection_reconciliation_results",
        "uq_projection_reconciliation_results_scope_id",
        "uq_projection_reconciliation_results_attempt",
        "uq_projection_reconciliation_results_target",
        "ix_projection_validations_candidate",
        "ix_projection_snapshots_scope_state",
        "ix_projection_snapshot_members_snapshot",
        "ix_projection_reconciliation_results_activation",
        "uq_projection_reconciliation_results_root",
        "uq_projection_reconciliation_results_successor",
        "pk_projection_activations",
        "uq_projection_activations_scope_id",
        "uq_projection_activations_generation",
        "uq_projection_activations_snapshot",
        "uq_projection_activations_pointer_target",
        "pk_active_projections",
        "pk_projection_drifts",
        "uq_projection_drifts_scope_id",
        "uq_projection_drifts_activation_acceptance",
        "pk_context_profiles",
        "uq_context_profiles_identity_digest",
        "ix_context_profiles_lookup",
        "pk_context_bundles",
        "uq_context_bundles_attempt",
        "uq_context_bundles_exact_identity",
        "ix_context_bundles_attempt",
        "ix_context_bundles_snapshot_generation",
        "pk_answer_question_runs",
        "uq_answer_question_runs_idempotency",
        "pk_answer_question_attempts",
        "uq_answer_question_attempts_number",
        "uq_answer_question_attempts_logical_key",
        "ix_answer_question_attempts_run",
        "uq_answer_question_attempts_one_allocated_running",
        "pk_qa_session_versions",
        "pk_qa_message_versions",
        "pk_qa_disclosure_validation_results",
        "pk_qa_answer_disclosure_records",
        "pk_qa_answer_disclosure_deliveries",
        "pk_feedback_records",
        "ix_feedback_records_target",
        "pk_operational_correlation_records",
        "uq_operational_correlation_records_deduplication",
        "pk_operational_metric_events",
        "uq_operational_metric_events_deduplication",
        "ix_operational_metric_events_window",
        "ix_operational_metric_events_operation",
        "ix_operational_correlation_trace",
        "ix_operational_correlation_reference",
        "ix_operational_metric_events_trace_correlation",
        "pk_process_only_operation_lineages",
        "uq_process_only_lineages_scope_id",
        "uq_process_only_lineages_run",
        "ix_qa_session_versions_navigation",
        "ix_qa_message_versions_navigation",
        "ix_projection_activations_kind_generation",
        "ix_active_projections_snapshot_generation",
        "ix_projection_drifts_source",
    } | AUTHORIZATION_DIRECTORY_INDEXES | AUDIT_INDEXES | DELIVERY_INDEXES | ADMISSION_INDEXES | CANONICAL_ACCEPTANCE_INDEXES | CANONICAL_BOUNDARY_INDEXES
    assert owners == {MIGRATION_ROLE}
    expected_rls = {
        "workspaces": (True, True),
        "environments": (True, True),
        "context_profiles": (True, True),
        "context_bundles": (True, True),
        "answer_question_runs": (True, True),
        "answer_question_attempts": (True, True),
        "qa_session_versions": (True, True),
        "qa_message_versions": (True, True),
        "qa_disclosure_validation_results": (True, True),
        "qa_answer_disclosure_records": (True, True),
        "qa_answer_disclosure_deliveries": (True, True),
        "feedback_records": (True, True),
        "operational_metric_events": (True, True),
        "operational_correlation_records": (True, True),
        "idempotency_receipts": (True, True),
        "initial_workspace_bootstrap": (True, True),
        "outbox_intents": (True, True),
        "outbox_deliveries": (True, True),
        "workspace_memberships": (True, True),
        "environment_memberships": (True, True),
        "environment_role_bindings": (True, True),
        "authorization_generations": (True, True),
        "qualification_authority_bindings": (True, True),
        "qualification_target_bindings": (True, True),
        "canonical_human_identities": (False, False),
        "canonical_human_identity_states": (False, False),
        "authentication_alias_bindings": (False, False),
        "audit_events": (True, True),
        "source_canonical_tables": (False, False),
        "source_canonical_unicode": (False, False),
        "source_canonical_compositions": (False, False),
        "source_canonical_bcp47": (False, False),
        "admission_decisions": (True, True),
        "source_objects": (True, True),
        "source_revisions": (True, True),
        "source_revision_provenance": (True, True),
        "source_revision_receipt_associations": (True, True),
        "ingestion_runs": (True, True),
        "ingestion_run_sources": (True, True),
        "ingestion_run_source_bindings": (True, True),
        "ingestion_transitions": (True, True),
        "ingestion_warnings": (True, True),
        "ingestion_failures": (True, True),
        "processing_receipts": (True, True),
        "process_only_operation_lineages": (True, True),
        "source_security_domain_bindings": (True, True),
        "backup_fence_leases": (True, True),
        "fence_operation_tickets": (True, True),
        "recovery_quiescence_evidence": (True, True),
        "storage_recovery_manifests": (True, True),
        "recovery_evidence_invalidations": (True, True),
        "canonical_generation_counters": (True, True),
        "canonical_acceptances": (True, True),
        "current_source_heads": (True, True),
        "parsed_documents": (True, True),
        "parsed_document_segments": (True, True),
        "canonical_boundaries": (True, True),
        "canonical_boundary_members": (True, True),
        "ontology_versions": (True, True),
        "projection_config_versions": (True, True),
        "projection_runs": (True, True),
        "projection_run_transitions": (True, True),
        "projection_partitions": (True, True),
        "projection_receipts": (True, True),
        "source_projection_references": (True, True),
        "projection_validations": (True, True),
        "projection_snapshots": (True, True),
        "projection_snapshot_members": (True, True),
        "projection_reconciliation_results": (True, True),
        "projection_activations": (True, True),
        "active_projections": (True, True),
        "projection_drifts": (True, True),
        "drive_snapshot_runs": (True, True),
        "drive_change_runs": (True, True),
        "google_sheets_sync_runs": (True, True),
        "google_sheets_identity_dispositions": (True, True),
        "drive_lifecycle_decisions": (True, True),
    }
    assert rls == expected_rls
    tenant_rls_tables = {
        table_name
        for table_name, flags in expected_rls.items()
        if flags == (True, True)
    }
    standard_tenant_rls_tables = tenant_rls_tables - {
        "audit_events",
        "outbox_deliveries",
    }
    assert set(policies) == {
        (table_name, f"pol_{table_name}_{policy_kind}")
        for table_name in standard_tenant_rls_tables
        for policy_kind in ("tenant_isolation", "migration_maintenance")
    } | {
        ("audit_events", "pol_audit_events_tenant_read"),
        ("audit_events", "pol_audit_events_tenant_append"),
        ("audit_events", "pol_audit_events_migration_maintenance"),
        ("outbox_intents", "pol_outbox_intents_dispatcher_read"),
        ("outbox_deliveries", "pol_outbox_deliveries_tenant_read"),
        ("outbox_deliveries", "pol_outbox_deliveries_dispatcher"),
        ("outbox_deliveries", "pol_outbox_deliveries_migration_maintenance"),
    }
    for table_name in standard_tenant_rls_tables:
        tenant_policy = policies[(table_name, f"pol_{table_name}_tenant_isolation")]
        assert tenant_policy[0] == (RUNTIME_ROLE,)
        assert tenant_policy[1] is not None
        assert tenant_policy[2] is not None
        assert policies[(table_name, f"pol_{table_name}_migration_maintenance")] == (
            (MIGRATION_ROLE,),
            "true",
            "true",
        )
    audit_read = policies[("audit_events", "pol_audit_events_tenant_read")]
    audit_append = policies[("audit_events", "pol_audit_events_tenant_append")]
    assert audit_read[0] == (RUNTIME_ROLE,)
    assert audit_read[1] is not None and audit_read[2] is None
    assert audit_append[0] == (RUNTIME_ROLE,)
    assert audit_append[1] is None and audit_append[2] is not None
    assert policies[("audit_events", "pol_audit_events_migration_maintenance")] == (
        (MIGRATION_ROLE,),
        "true",
        "true",
    )
    assert policies[("outbox_intents", "pol_outbox_intents_dispatcher_read")][0] == (
        DISPATCHER_ROLE,
    )
    assert policies[("outbox_deliveries", "pol_outbox_deliveries_tenant_read")][0] == (
        RUNTIME_ROLE,
    )
    assert policies[("outbox_deliveries", "pol_outbox_deliveries_dispatcher")] == (
        (DISPATCHER_ROLE,),
        "true",
        "true",
    )
    assert policies[("outbox_deliveries", "pol_outbox_deliveries_migration_maintenance")] == (
        (MIGRATION_ROLE,),
        "true",
        "true",
    )
    assert privileges == {
        "alembic_version": (True, False, False, False),
        "audit_events": (True, True, False, False),
        "workspaces": (True, True, True, True),
        "environments": (True, True, True, True),
        "context_profiles": (True, True, False, False),
        "context_bundles": (True, True, False, False),
        "answer_question_runs": (True, True, True, False),
        "answer_question_attempts": (True, True, True, False),
        "qa_session_versions": (True, True, True, False),
        "qa_message_versions": (True, True, False, False),
        "qa_disclosure_validation_results": (True, True, False, False),
        "qa_answer_disclosure_records": (True, True, False, False),
        "qa_answer_disclosure_deliveries": (True, True, False, False),
        "feedback_records": (True, True, False, False),
        "operational_metric_events": (True, True, False, False),
        "operational_correlation_records": (True, True, False, False),
        "idempotency_receipts": (True, True, False, False),
        "initial_workspace_bootstrap": (False, False, False, False),
        "outbox_intents": (False, True, False, False),
        "outbox_deliveries": (True, False, False, False),
        "authentication_alias_bindings": (False, False, False, False),
        "authorization_generations": (False, False, False, False),
        "qualification_authority_bindings": (False, False, False, False),
        "qualification_target_bindings": (False, False, False, False),
        "canonical_human_identities": (False, False, False, False),
        "canonical_human_identity_states": (False, False, False, False),
        "environment_memberships": (False, False, False, False),
        "environment_role_bindings": (False, False, False, False),
        "workspace_memberships": (False, False, False, False),
        "source_objects": (True, True, False, False),
        "source_revisions": (True, True, False, False),
        "source_revision_provenance": (True, True, False, False),
        "source_revision_receipt_associations": (True, True, False, False),
        "ingestion_runs": (True, True, False, False),
        "ingestion_run_sources": (True, True, False, False),
        "ingestion_run_source_bindings": (True, True, False, False),
        "ingestion_transitions": (True, True, False, False),
        "ingestion_warnings": (True, True, False, False),
        "ingestion_failures": (True, True, False, False),
        "processing_receipts": (True, True, False, False),
        "process_only_operation_lineages": (True, True, False, False),
        "source_security_domain_bindings": (True, True, False, False),
        "backup_fence_leases": (True, True, True, False),
        "fence_operation_tickets": (True, True, True, False),
        "recovery_quiescence_evidence": (True, True, False, False),
        "storage_recovery_manifests": (True, True, False, False),
        "recovery_evidence_invalidations": (True, True, False, False),
        "parsed_documents": (True, True, False, False),
        "parsed_document_segments": (True, True, False, False),
        "admission_decisions": (True, True, False, False),
        "canonical_generation_counters": (True, True, True, False),
        "canonical_acceptances": (True, True, False, False),
        "current_source_heads": (True, True, True, False),
            "canonical_boundaries": (True, True, False, False),
            "canonical_boundary_members": (True, True, False, False),
        "ontology_versions": (True, True, False, False),
        "projection_config_versions": (True, True, False, False),
        "projection_runs": (True, True, True, False),
        "projection_run_transitions": (True, True, False, False),
        "projection_partitions": (True, True, False, False),
        "projection_receipts": (True, True, False, False),
        "source_projection_references": (True, True, False, False),
        "projection_validations": (True, True, False, False),
        "projection_snapshots": (True, True, True, False),
        "projection_snapshot_members": (True, True, False, False),
        "projection_reconciliation_results": (True, True, False, False),
        "projection_activations": (True, True, False, False),
        "active_projections": (True, True, True, False),
        "projection_drifts": (True, True, False, False),
        "drive_snapshot_runs": (True, True, True, False),
        "drive_change_runs": (True, True, True, False),
        "google_sheets_sync_runs": (True, True, True, False),
        "google_sheets_identity_dispositions": (True, True, False, False),
        "drive_lifecycle_decisions": (True, True, True, False),
        "source_canonical_tables": (True, False, False, False),
        "source_canonical_unicode": (True, False, False, False),
        "source_canonical_compositions": (True, False, False, False),
        "source_canonical_bcp47": (True, False, False, False),
    }
    assert receipt_update_columns == {
        "result_type",
        "result_id",
        "result_schema_version",
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_initial_tenancy_revision_upgrades_to_current_head_and_owned_schema(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        heads = (
            await connection.execute(
                text("SELECT version_num FROM spine.alembic_version")
            )
        ).scalars().all()
        owner = await connection.scalar(
            text(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname = 'spine'"
            )
        )
    assert migrated_database.retained_revisions == _retained_revision_ids()[:-1]
    assert heads == [HEAD_REVISION]
    assert owner == MIGRATION_ROLE


@pytest.mark.asyncio(loop_scope="session")
async def test_ingestion_ledger_catalog_has_immutable_and_deferred_integrity_triggers(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT relation.relname AS table_name, trigger.tgname, "
                    "trigger.tgdeferrable, trigger.tginitdeferred, "
                    "pg_get_triggerdef(trigger.oid) AS definition "
                    "FROM pg_trigger AS trigger JOIN pg_class AS relation "
                    "ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND NOT trigger.tgisinternal "
                    "AND relation.relname IN ("
                    "'ingestion_runs', 'ingestion_run_sources', 'ingestion_run_source_bindings', "
                    "'ingestion_transitions', 'ingestion_warnings', "
                    "'ingestion_failures', 'processing_receipts', "
                    "'process_only_operation_lineages')"
                )
            )
        ).mappings().all()
        original_validator = await connection.scalar(
            text(
                "SELECT pg_get_functiondef(namespace.oid) "
                "FROM pg_proc AS namespace "
                "JOIN pg_namespace AS schema_namespace "
                "ON schema_namespace.oid = namespace.pronamespace "
                "WHERE schema_namespace.nspname = 'spine' "
                "AND namespace.proname = 'validate_issue86_binding_original_receipt'"
            )
        )

    by_name = {row.tgname: row for row in rows}
    immutable_tables = (
        "ingestion_runs",
        "ingestion_run_sources",
        "ingestion_run_source_bindings",
        "ingestion_transitions",
        "ingestion_warnings",
        "ingestion_failures",
        "processing_receipts",
        "process_only_operation_lineages",
    )
    assert {
        f"trg_{table}_immutable" for table in immutable_tables
    } <= set(by_name)
    assert {
        "trg_ingestion_runs_receipt_scope",
        "trg_ingestion_run_source_bindings_receipt_scope",
        "trg_processing_receipts_receipt_scope",
    } <= set(by_name)
    assert "trg_ingestion_run_source_bindings_original" in by_name
    assert "trg_ingestion_run_source_bindings_original_receipt_tenant" in by_name
    assert {
        "trg_ingestion_warnings_safe_detail",
        "trg_ingestion_failures_safe_detail",
        "trg_ingestion_transitions_safe_metrics",
    } <= set(by_name)
    for name, row in by_name.items():
        if name.endswith("_receipt_scope"):
            assert row.tgdeferrable is True
            assert row.tginitdeferred is True
            assert "AFTER INSERT OR UPDATE" in row.definition
            assert "FOR EACH ROW" in row.definition
            assert "validate_issue7_receipt_scope" in row.definition
        if name.endswith("_immutable"):
            assert "BEFORE" in row.definition
            assert "UPDATE" in row.definition and "DELETE" in row.definition
    assert "validate_issue7_binding_original" in by_name["trg_ingestion_run_source_bindings_original"].definition
    assert "validate_issue86_safe_metrics" in by_name["trg_ingestion_transitions_safe_metrics"].definition
    assert original_validator is not None
    assert "Verified source receipt association is unavailable." in original_validator
    assert "source_revisions" in original_validator
    assert "revision.revision_id = NEW.source_revision_id" in original_validator
    assert "FOR KEY SHARE" not in original_validator
    assert "idempotency_receipts" in original_validator
    assert "result_type IS NOT NULL" in original_validator
    assert "result_id IS NOT NULL" in original_validator
    assert "result_schema_version IS NOT NULL" in original_validator
    assert "receipt_complete IS DISTINCT FROM true" in original_validator
    assert "Contentless ingestion binding cannot include an original receipt." in original_validator
    assert "source_revision_receipt_associations" in original_validator
    assert "verification_status = 'verified'" in original_validator
    assert "storage_status = 'finalized_verified'" in original_validator


@pytest.mark.asyncio(loop_scope="session")
async def test_initial_schema_has_named_tenancy_constraints_and_postgresql_types(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint AS constraint_record "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = constraint_record.connamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND constraint_record.conname NOT LIKE '%source_canonical%' "
                        "AND constraint_record.conrelid NOT IN ("
                        "SELECT relation.oid FROM pg_class AS relation "
                        "JOIN pg_namespace AS source_namespace "
                        "ON source_namespace.oid = relation.relnamespace "
                        "WHERE source_namespace.nspname = 'spine' AND relation.relname IN "
                        "('google_connection_versions', 'google_connection_approvals', "
                        "'source_objects', 'source_revisions', 'source_revision_provenance', "
                        "'source_revision_receipt_associations', 'ingestion_runs', "
                        "'original_upload_records', 'qa_agent_artifacts', 'qa_agent_results', "
                        "'ingestion_run_sources', 'ingestion_run_source_bindings', "
                        "'ingestion_transitions', 'ingestion_warnings', 'ingestion_failures', "
                            "'processing_receipts', 'process_only_operation_lineages', "
                            "'source_security_domain_bindings', "
                            "'access_policy_versions', "
                            "'access_policy_grants', 'policy_proposals', 'policy_approvals', "
                            "'policy_activations', 'current_access_policies', "
                            "'canonical_generation_counters', 'canonical_acceptances', "
                            "'current_source_heads', 'canonical_boundaries', 'canonical_boundary_members', 'backup_fence_leases', "
                            "'fence_operation_tickets', 'recovery_quiescence_evidence', "
                            "'storage_recovery_manifests', 'recovery_evidence_invalidations', "
                            "'parsed_documents', 'parsed_document_segments', 'ontology_versions', "
                            "'projection_config_versions', 'projection_runs', 'projection_run_transitions', "
                            "'projection_partitions', 'projection_receipts', 'source_projection_references', "
                            "'projection_validations', 'projection_snapshots', 'projection_snapshot_members', "
                            "'projection_reconciliation_results', 'projection_activations', 'active_projections', "
                                "'projection_drifts', 'drive_snapshot_runs', 'drive_change_runs', 'google_sheets_sync_runs', 'google_sheets_identity_dispositions', 'drive_lifecycle_decisions', 'answer_question_runs', "
                                "'answer_question_attempts', 'qa_session_versions', "
                                "'qa_message_versions', 'qa_disclosure_validation_results', 'qa_answer_disclosure_records', 'qa_answer_disclosure_deliveries', 'feedback_records', 'operational_metric_events', 'operational_correlation_records'))"
                    )
                )
            ).scalars()
        )
        columns = {
            (row.table_name, row.column_name): (
                row.data_type,
                row.is_nullable,
                row.column_default,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name, data_type, is_nullable, "
                        "column_default FROM information_schema.columns "
                        "WHERE table_schema = 'spine' "
                        "AND table_name NOT LIKE 'source_canonical%' "
                        "AND table_name NOT IN ('google_connection_versions', 'google_connection_approvals', "
                        "'source_objects', 'source_revisions', 'source_revision_provenance', "
                        "'source_revision_receipt_associations', "
                        "'original_upload_records', 'qa_agent_artifacts', 'qa_agent_results', "
                        "'ingestion_runs', 'ingestion_run_sources', 'ingestion_run_source_bindings', "
                            "'ingestion_transitions', 'ingestion_warnings', 'ingestion_failures', 'processing_receipts', "
                            "'process_only_operation_lineages', "
                            "'source_security_domain_bindings', "
                            "'access_policy_versions', 'access_policy_grants', 'policy_proposals', "
                            "'policy_approvals', 'policy_activations', 'current_access_policies', "
                            "'canonical_generation_counters', 'canonical_acceptances', "
                            "'current_source_heads', 'canonical_boundaries', 'canonical_boundary_members', 'backup_fence_leases', 'fence_operation_tickets', "
                            "'recovery_quiescence_evidence', 'storage_recovery_manifests', "
                            "'recovery_evidence_invalidations', 'parsed_documents', "
                            "'parsed_document_segments', 'ontology_versions', 'projection_config_versions', "
                            "'projection_runs', 'projection_run_transitions', 'projection_partitions', "
                            "'projection_receipts', 'source_projection_references', "
                            "'projection_validations', 'projection_snapshots', 'projection_snapshot_members', "
                            "'projection_reconciliation_results', 'projection_activations', 'active_projections', "
                            "'projection_drifts', 'drive_snapshot_runs', 'drive_change_runs', 'google_sheets_sync_runs', 'google_sheets_identity_dispositions', 'drive_lifecycle_decisions', 'answer_question_runs', "
                                "'answer_question_attempts', 'qa_session_versions', "
                                "'qa_message_versions', 'qa_disclosure_validation_results', 'qa_answer_disclosure_records', 'qa_answer_disclosure_deliveries', 'feedback_records', 'operational_metric_events', 'operational_correlation_records')"
                    )
                )
            ).mappings()
        }
        indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'spine' "
                        "AND indexname NOT LIKE 'pk_source_canonical%' "
                        "AND tablename NOT IN ('google_connection_versions', 'google_connection_approvals', "
                        "'source_objects', 'source_revisions', 'source_revision_provenance', "
                        "'source_revision_receipt_associations', "
                        "'original_upload_records', 'qa_agent_artifacts', 'qa_agent_results', "
                        "'ingestion_runs', 'ingestion_run_sources', 'ingestion_run_source_bindings', "
                            "'ingestion_transitions', 'ingestion_warnings', 'ingestion_failures', 'processing_receipts', "
                            "'process_only_operation_lineages', "
                            "'source_security_domain_bindings', "
                            "'access_policy_versions', 'access_policy_grants', 'policy_proposals', "
                            "'policy_approvals', 'policy_activations', 'current_access_policies', "
                            "'canonical_generation_counters', 'canonical_acceptances', "
                            "'current_source_heads', 'canonical_boundaries', 'canonical_boundary_members', 'backup_fence_leases', 'fence_operation_tickets', "
                            "'recovery_quiescence_evidence', 'storage_recovery_manifests', "
                            "'recovery_evidence_invalidations', 'parsed_documents', "
                            "'parsed_document_segments', 'ontology_versions', 'projection_config_versions', "
                            "'projection_runs', 'projection_run_transitions', 'projection_partitions', "
                            "'projection_receipts', 'source_projection_references', "
                            "'projection_validations', 'projection_snapshots', 'projection_snapshot_members', "
                            "'projection_reconciliation_results', 'projection_activations', 'active_projections', "
                            "'projection_drifts', 'drive_snapshot_runs', 'drive_change_runs', 'google_sheets_sync_runs', 'google_sheets_identity_dispositions', 'drive_lifecycle_decisions', 'answer_question_runs', "
                                "'answer_question_attempts', 'qa_session_versions', "
                            "'qa_message_versions', 'qa_disclosure_validation_results', 'qa_answer_disclosure_records', 'qa_answer_disclosure_deliveries', 'feedback_records', 'operational_metric_events', 'operational_correlation_records')"
                    )
                )
            ).scalars()
        )
        tables = set(
            (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'spine' "
                        "AND tablename NOT LIKE 'source_canonical%' "
                        "AND tablename NOT IN ('google_connection_versions', 'google_connection_approvals', "
                        "'source_objects', 'source_revisions', 'source_revision_provenance', "
                        "'source_revision_receipt_associations', "
                        "'original_upload_records', 'qa_agent_artifacts', 'qa_agent_results', "
                        "'ingestion_runs', 'ingestion_run_sources', 'ingestion_run_source_bindings', "
                                "'ingestion_transitions', 'ingestion_warnings', 'ingestion_failures', 'processing_receipts', "
                                "'process_only_operation_lineages', "
                                "'source_security_domain_bindings', "
                            "'access_policy_versions', 'access_policy_grants', 'policy_proposals', "
                            "'policy_approvals', 'policy_activations', 'current_access_policies', "
                            "'canonical_generation_counters', 'canonical_acceptances', "
                            "'current_source_heads', 'canonical_boundaries', 'canonical_boundary_members', 'backup_fence_leases', 'fence_operation_tickets', "
                            "'recovery_quiescence_evidence', 'storage_recovery_manifests', "
                            "'recovery_evidence_invalidations', 'parsed_documents', "
                            "'parsed_document_segments', 'ontology_versions', 'projection_config_versions', "
                            "'projection_runs', 'projection_run_transitions', 'projection_partitions', "
                            "'projection_receipts', 'source_projection_references', "
                            "'projection_validations', 'projection_snapshots', 'projection_snapshot_members', "
                            "'projection_reconciliation_results', 'projection_activations', 'active_projections', "
                            "'projection_drifts', 'drive_snapshot_runs', 'drive_change_runs', 'google_sheets_sync_runs', 'google_sheets_identity_dispositions', 'drive_lifecycle_decisions', 'answer_question_runs', "
                                "'answer_question_attempts', 'qa_session_versions', "
                                "'qa_message_versions', 'qa_disclosure_validation_results', 'qa_answer_disclosure_records', 'qa_answer_disclosure_deliveries', 'feedback_records', 'operational_metric_events', 'operational_correlation_records')"
                    )
                )
            ).scalars()
        )
        owners = set(
            (
                await connection.execute(
                    text(
                        "SELECT tableowner FROM pg_tables WHERE schemaname = 'spine' "
                        "AND tablename NOT LIKE 'source_canonical%' "
                        "AND tablename NOT IN ('google_connection_versions', 'google_connection_approvals', "
                        "'source_objects', 'source_revisions', 'source_revision_provenance', "
                        "'source_revision_receipt_associations', "
                        "'original_upload_records', 'qa_agent_artifacts', 'qa_agent_results', "
                        "'ingestion_runs', 'ingestion_run_sources', 'ingestion_run_source_bindings', "
                            "'ingestion_transitions', 'ingestion_warnings', 'ingestion_failures', 'processing_receipts', "
                            "'process_only_operation_lineages', "
                            "'access_policy_versions', 'access_policy_grants', 'policy_proposals', "
                            "'policy_approvals', 'policy_activations', 'current_access_policies', "
                            "'canonical_generation_counters', 'canonical_acceptances', "
                            "'current_source_heads', 'canonical_boundaries', 'canonical_boundary_members', 'backup_fence_leases', 'fence_operation_tickets', "
                            "'recovery_quiescence_evidence', 'storage_recovery_manifests', "
                            "'recovery_evidence_invalidations', 'parsed_documents', "
                            "'parsed_document_segments', 'ontology_versions', 'projection_config_versions', "
                            "'projection_runs', 'projection_run_transitions', 'projection_partitions', "
                            "'projection_receipts', 'source_projection_references', "
                            "'projection_validations', 'projection_snapshots', 'projection_snapshot_members', "
                            "'projection_reconciliation_results', 'projection_activations', 'active_projections', "
                            "'projection_drifts', 'drive_snapshot_runs', 'drive_change_runs', 'google_sheets_sync_runs', 'google_sheets_identity_dispositions', 'drive_lifecycle_decisions', 'answer_question_runs', "
                                "'answer_question_attempts', 'qa_session_versions', "
                                "'qa_message_versions', 'qa_disclosure_validation_results', 'qa_answer_disclosure_records', 'qa_answer_disclosure_deliveries', 'feedback_records', 'operational_metric_events', 'operational_correlation_records')"
                    )
                )
            ).scalars()
        )
    assert tables == {
        "alembic_version",
        "admission_decisions",
        "audit_events",
        "authentication_alias_bindings",
            "authorization_generations",
            "qualification_authority_bindings",
            "qualification_target_bindings",
        "canonical_human_identities",
        "canonical_human_identity_states",
        "environment_memberships",
        "environment_role_bindings",
        "environments",
        "context_profiles",
        "context_bundles",
        "idempotency_receipts",
        "initial_workspace_bootstrap",
        "outbox_intents",
        "outbox_deliveries",
        "workspace_memberships",
        "workspaces",
    }
    assert constraints == {
        "alembic_version_pkc",
        "ck_environments_display_name_not_empty",
        "ck_environments_kind",
        "ck_initial_workspace_bootstrap_singleton",
        "ck_workspaces_display_name_not_empty",
        "ck_workspaces_slug_not_empty",
        "fk_environments_workspace_id_workspaces",
        "fk_idempotency_receipts_scope_environments",
        "fk_idempotency_receipts_workspace_id_workspaces",
        "fk_initial_workspace_bootstrap_workspace_id_workspaces",
        "fk_outbox_intents_scope_environments",
        "fk_outbox_intents_workspace_id_workspaces",
        "ck_idempotency_receipts_command_digest_sha256",
        "ck_idempotency_receipts_idempotency_key_length",
        "ck_idempotency_receipts_operation_schema_version_positive",
        "ck_idempotency_receipts_result_complete",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "ck_outbox_intents_aggregate_complete",
        "ck_outbox_intents_event_schema_version_positive",
        "ck_outbox_intents_event_type_identifier",
        "ck_outbox_intents_payload_object",
        "ck_outbox_intents_producer_deduplication_id_length",
        "pk_outbox_intents",
        "pk_workspaces",
        "pk_context_profiles",
        "uq_context_profiles_identity_digest",
        "fk_context_profiles_environment",
        "ck_context_profiles_version_positive",
        "ck_context_profiles_schema",
        "ck_context_profiles_digest",
        "ck_context_profiles_strategy",
        "ck_context_profiles_bounds",
        "ck_context_profiles_completeness",
        "ck_context_profiles_partial_fallback",
        "ck_context_profiles_server_approved",
        "pk_context_bundles",
        "uq_context_bundles_attempt",
        "uq_context_bundles_exact_identity",
        "fk_context_bundles_environment",
        "fk_context_bundles_profile",
        "ck_context_bundles_positive_versions",
        "ck_context_bundles_schema",
        "ck_context_bundles_digests",
        "ck_context_bundles_publication_state",
        "ck_context_bundles_outcome",
        "ck_context_bundles_state",
        "uq_environments_workspace_id_id",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_workspaces_slug",
    } | AUTHORIZATION_DIRECTORY_CONSTRAINTS | AUDIT_CONSTRAINTS | DELIVERY_CONSTRAINTS | ADMISSION_CONSTRAINTS
    assert set(columns) == {
        ("alembic_version", "version_num"),
        ("environments", "created_at"),
        ("environments", "display_name"),
        ("environments", "id"),
        ("environments", "kind"),
        ("environments", "workspace_id"),
        ("initial_workspace_bootstrap", "action_id"),
        ("initial_workspace_bootstrap", "created_at"),
        ("initial_workspace_bootstrap", "executed_by"),
        ("initial_workspace_bootstrap", "singleton"),
        ("initial_workspace_bootstrap", "workspace_id"),
        ("idempotency_receipts", "command_digest"),
        ("idempotency_receipts", "created_at"),
        ("idempotency_receipts", "digest_algorithm_version"),
        ("idempotency_receipts", "environment_id"),
        ("idempotency_receipts", "actor_id"),
        ("idempotency_receipts", "idempotency_key"),
        ("idempotency_receipts", "operation_name"),
        ("idempotency_receipts", "operation_schema_version"),
        ("idempotency_receipts", "receipt_id"),
        ("idempotency_receipts", "result_id"),
        ("idempotency_receipts", "result_schema_version"),
        ("idempotency_receipts", "result_type"),
        ("idempotency_receipts", "workspace_id"),
        ("outbox_intents", "aggregate_id"),
        ("outbox_intents", "aggregate_schema_version"),
        ("outbox_intents", "aggregate_type"),
        ("outbox_intents", "causation_id"),
        ("outbox_intents", "correlation_id"),
        ("outbox_intents", "created_at"),
        ("outbox_intents", "environment_id"),
        ("outbox_intents", "event_id"),
        ("outbox_intents", "event_schema_version"),
        ("outbox_intents", "event_type"),
        ("outbox_intents", "payload"),
        ("outbox_intents", "producer_deduplication_id"),
        ("outbox_intents", "trace_id"),
        ("outbox_intents", "workspace_id"),
        ("workspaces", "created_at"),
        ("workspaces", "display_name"),
        ("workspaces", "id"),
        ("workspaces", "slug"),
        ("context_profiles", "profile_id"),
        ("context_profiles", "workspace_id"),
        ("context_profiles", "environment_id"),
        ("context_profiles", "profile_version"),
        ("context_profiles", "profile_digest"),
        ("context_profiles", "schema_version"),
        ("context_profiles", "retrieval_strategy"),
        ("context_profiles", "max_results"),
        ("context_profiles", "max_tokens"),
        ("context_profiles", "max_relation_hops"),
        ("context_profiles", "freshness_max_age_seconds"),
        ("context_profiles", "completeness_requirement"),
        ("context_profiles", "partial_answer_fallback"),
        ("context_profiles", "server_approved"),
        ("context_profiles", "revoked"),
        ("context_profiles", "predecessor_profile_id"),
        ("context_profiles", "created_at"),
        ("context_bundles", "bundle_id"),
        ("context_bundles", "workspace_id"),
        ("context_bundles", "environment_id"),
        ("context_bundles", "bundle_version"),
        ("context_bundles", "bundle_digest"),
        ("context_bundles", "schema_version"),
        ("context_bundles", "idempotency_key"),
        ("context_bundles", "profile_id"),
        ("context_bundles", "profile_version"),
        ("context_bundles", "profile_digest"),
        ("context_bundles", "trusted_request_context_ref"),
        ("context_bundles", "authorization_context_ref"),
        ("context_bundles", "request_id"),
        ("context_bundles", "run_id"),
        ("context_bundles", "attempt_id"),
        ("context_bundles", "publication_pin_id"),
        ("context_bundles", "projection_snapshot_id"),
        ("context_bundles", "projection_activation_id"),
        ("context_bundles", "activation_generation"),
        ("context_bundles", "canonical_boundary_id"),
        ("context_bundles", "projection_config_version_id"),
        ("context_bundles", "freshness"),
        ("context_bundles", "health"),
        ("context_bundles", "retrieval_strategy"),
        ("context_bundles", "outcome"),
        ("context_bundles", "reason_code"),
        ("context_bundles", "state"),
        ("context_bundles", "reconciliation_evidence"),
        ("context_bundles", "evidence_references"),
        ("context_bundles", "policy_decision_generations"),
        ("context_bundles", "canonical_boundary"),
        ("context_bundles", "canonical_as_of"),
        ("context_bundles", "projection_snapshot"),
        ("context_bundles", "projection_activation"),
        ("context_bundles", "projection_config_version"),
        ("context_bundles", "created_at"),
    } | AUTHORIZATION_DIRECTORY_COLUMNS | AUDIT_COLUMNS | DELIVERY_COLUMNS | ADMISSION_COLUMNS
    assert columns[("workspaces", "id")] == ("uuid", "NO", None)
    assert columns[("environments", "id")] == ("uuid", "NO", None)
    event_id_type, event_id_nullable, event_id_default = columns[
        ("outbox_intents", "event_id")
    ]
    assert event_id_type == "uuid"
    assert event_id_nullable == "NO"
    assert event_id_default is not None and "gen_random_uuid()" in event_id_default
    audit_id_type, audit_id_nullable, audit_id_default = columns[
        ("audit_events", "audit_event_id")
    ]
    assert audit_id_type == "uuid"
    assert audit_id_nullable == "NO"
    assert audit_id_default is not None and "gen_random_uuid()" in audit_id_default
    for table in (
        "workspaces",
        "environments",
        "idempotency_receipts",
        "initial_workspace_bootstrap",
        "outbox_intents",
        "outbox_deliveries",
    ):
        data_type, nullable, default = columns[(table, "created_at")]
        assert data_type == "timestamp with time zone"
        assert nullable == "NO"
        assert default is not None and "CURRENT_TIMESTAMP" in default.upper()
    appended_type, appended_nullable, appended_default = columns[
        ("audit_events", "appended_at")
    ]
    assert appended_type == "timestamp with time zone"
    assert appended_nullable == "NO"
    assert appended_default is not None and "CURRENT_TIMESTAMP" in appended_default.upper()
    assert indexes == {
        "alembic_version_pkc",
        "ix_environments_workspace_id",
        "pk_environments",
        "pk_idempotency_receipts",
        "pk_initial_workspace_bootstrap",
        "pk_outbox_intents",
        "pk_workspaces",
        "uq_environments_workspace_id_id",
        "uq_idempotency_receipts_environment_key",
        "uq_idempotency_receipts_workspace_key",
        "uq_initial_workspace_bootstrap_action_id",
        "uq_initial_workspace_bootstrap_workspace_id",
        "uq_outbox_intents_environment_producer",
        "uq_outbox_intents_workspace_producer",
        "uq_workspaces_slug",
        "pk_context_profiles",
        "uq_context_profiles_identity_digest",
        "ix_context_profiles_lookup",
        "pk_context_bundles",
        "uq_context_bundles_attempt",
        "uq_context_bundles_exact_identity",
        "ix_context_bundles_attempt",
        "ix_context_bundles_snapshot_generation",
    } | AUTHORIZATION_DIRECTORY_INDEXES | AUDIT_INDEXES | DELIVERY_INDEXES | ADMISSION_INDEXES
    assert owners == {MIGRATION_ROLE}


@pytest.mark.asyncio(loop_scope="session")
async def test_environment_workspace_foreign_key_rejects_unknown_owner(
    migrated_database: MigratedDatabase,
) -> None:
    with pytest.raises(IntegrityError):
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.environments "
                    "(id, workspace_id, kind, display_name) "
                    "VALUES (:id, :workspace_id, 'development', 'Invalid')"
                ),
                {
                    "id": UUID("50000000-0000-0000-0000-000000000001"),
                    "workspace_id": UUID("50000000-0000-0000-0000-000000000002"),
                },
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotency_receipt_composite_owner_rejects_mismatched_environment(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    with pytest.raises(IntegrityError):
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.idempotency_receipts "
                    "(receipt_id, workspace_id, environment_id, operation_name, "
                    "operation_schema_version, idempotency_key, "
                    "digest_algorithm_version, command_digest) VALUES "
                    "(:receipt_id, :workspace_id, :environment_id, "
                    "'test.operation', 1, 'mismatched-environment', "
                    "'spine.command-digest.v1', :digest)"
                ),
                {
                    "receipt_id": UUID("51000000-0000-0000-0000-000000000001"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_B,
                    "digest": "a" * 64,
                },
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_intent_composite_owner_rejects_mismatched_environment(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    with pytest.raises(IntegrityError) as error:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.outbox_intents "
                    "(event_id, workspace_id, environment_id, event_type, "
                    "event_schema_version, payload, trace_id) VALUES "
                    "(:event_id, :workspace_id, :environment_id, "
                    "'workspace.created', 1, '{}'::jsonb, :trace_id)"
                ),
                {
                    "event_id": UUID("52000000-0000-0000-0000-000000000001"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_B,
                    "trace_id": UUID("52000000-0000-0000-0000-000000000002"),
                },
            )
    assert (
        error.value.orig.diag.constraint_name
        == "fk_outbox_intents_scope_environments"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_intent_is_immutable_after_insert(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    workspace_id = WORKSPACE_A
    event_id = UUID("52000000-0000-0000-0000-000000000011")
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.outbox_intents "
                "(event_id, workspace_id, event_type, event_schema_version, "
                "payload, trace_id) VALUES "
                "(:event_id, :workspace_id, 'workspace.created', 1, "
                "'{}'::jsonb, :trace_id)"
            ),
            {
                "event_id": event_id,
                "workspace_id": workspace_id,
                "trace_id": UUID("52000000-0000-0000-0000-000000000012"),
            },
        )
        await connection.commit()

    try:
        for statement in (
            "UPDATE spine.outbox_intents SET event_schema_version = 2 "
            "WHERE event_id = :event_id",
            "DELETE FROM spine.outbox_intents WHERE event_id = :event_id",
        ):
            with pytest.raises(IntegrityError) as error:
                async with _connection(migrated_database.migration.url) as connection:
                    await connection.execute(text(statement), {"event_id": event_id})
            assert (
                error.value.orig.diag.constraint_name
                == "ck_outbox_intents_immutable"
            )

    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "ALTER TABLE spine.outbox_intents "
                    "DISABLE TRIGGER trg_outbox_intents_immutable"
                )
            )
            await connection.execute(
                text("DELETE FROM spine.outbox_intents WHERE event_id = :event_id"),
                {"event_id": event_id},
            )
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            await connection.execute(
                text(
                    "ALTER TABLE spine.outbox_intents "
                    "ENABLE TRIGGER trg_outbox_intents_immutable"
                )
            )
            await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_runtime_grant_and_rls_allow_only_scoped_insert(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(text("SELECT event_id FROM spine.outbox_intents"))

    with pytest.raises(DBAPIError):
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=WORKSPACE_A,
                environment_id=ENVIRONMENT_A_SECOND,
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.outbox_intents "
                    "(event_id, workspace_id, environment_id, event_type, "
                    "event_schema_version, payload, trace_id) VALUES "
                    "(:event_id, :workspace_id, :environment_id, "
                    "'workspace.created', 1, '{}'::jsonb, :trace_id)"
                ),
                {
                    "event_id": UUID("52000000-0000-0000-0000-000000000020"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_A,
                    "trace_id": UUID("52000000-0000-0000-0000-000000000021"),
                },
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotency_receipt_result_can_be_completed_exactly_once(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000010")
    first_result_id = UUID("51000000-0000-0000-0000-000000000011")
    replacement_result_id = UUID("51000000-0000-0000-0000-000000000012")
    try:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(
                text(
                    "INSERT INTO spine.idempotency_receipts "
                    "(receipt_id, workspace_id, operation_name, "
                    "operation_schema_version, idempotency_key, "
                    "digest_algorithm_version, command_digest) VALUES "
                    "(:receipt_id, :workspace_id, 'test.operation', 1, "
                    "'write-once-result', 'spine.command-digest.v1', :digest)"
                ),
                {
                    "receipt_id": receipt_id,
                    "workspace_id": WORKSPACE_A,
                    "digest": "b" * 64,
                },
            )
            await connection.commit()

        with pytest.raises(IntegrityError) as error:
            async with _connection(migrated_database.migration.url) as connection:
                await connection.execute(
                    text(
                        "UPDATE spine.idempotency_receipts SET workspace_id = :workspace_id "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": receipt_id, "workspace_id": WORKSPACE_B},
                )
        assert (
            error.value.orig.diag.constraint_name
            == "ck_idempotency_receipt_scope_immutable"
        )

        with pytest.raises(ProgrammingError):
            async with _connection(migrated_database.runtime_url) as connection:
                await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
                await connection.execute(
                    text(
                        "UPDATE spine.idempotency_receipts SET environment_id = :environment_id "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {
                        "receipt_id": receipt_id,
                        "environment_id": ENVIRONMENT_A,
                    },
                )

        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(
                text(
                    "UPDATE spine.idempotency_receipts SET "
                    "result_type = 'proposal', result_id = :result_id, "
                    "result_schema_version = 1 WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id, "result_id": first_result_id},
            )
            await connection.commit()

        with pytest.raises(IntegrityError) as error:
            async with _connection(migrated_database.runtime_url) as connection:
                await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
                await connection.execute(
                    text(
                        "UPDATE spine.idempotency_receipts SET result_id = :result_id "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {
                        "receipt_id": receipt_id,
                        "result_id": replacement_result_id,
                    },
                )
        assert (
            error.value.orig.diag.constraint_name
            == "ck_idempotency_receipts_single_completion"
        )

        async with _connection(migrated_database.migration.url) as connection:
            persisted_result = await connection.scalar(
                text(
                    "SELECT result_id FROM spine.idempotency_receipts "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
        assert persisted_result == first_result_id
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "DELETE FROM spine.idempotency_receipts "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
            await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_idempotency_receipt_rls_isolates_environment_reads_and_writes(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000002")
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.idempotency_receipts "
                "(receipt_id, workspace_id, environment_id, operation_name, "
                "operation_schema_version, idempotency_key, "
                "digest_algorithm_version, command_digest) VALUES "
                "(:receipt_id, :workspace_id, :environment_id, "
                "'test.operation', 1, 'environment-isolation', "
                "'spine.command-digest.v1', :digest)"
            ),
            {
                "receipt_id": receipt_id,
                "workspace_id": WORKSPACE_A,
                "environment_id": ENVIRONMENT_A,
                "digest": "b" * 64,
            },
        )
        await connection.commit()

    try:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=WORKSPACE_A,
                environment_id=ENVIRONMENT_A_SECOND,
            )
            visible = (
                await connection.execute(
                    text("SELECT receipt_id FROM spine.idempotency_receipts")
                )
            ).scalars().all()
            assert receipt_id not in visible
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO spine.idempotency_receipts "
                        "(receipt_id, workspace_id, environment_id, operation_name, "
                        "operation_schema_version, idempotency_key, "
                        "digest_algorithm_version, command_digest) VALUES "
                        "(:receipt_id, :workspace_id, :environment_id, "
                        "'test.operation', 1, 'forbidden-environment', "
                        "'spine.command-digest.v1', :digest)"
                    ),
                    {
                        "receipt_id": UUID(
                            "51000000-0000-0000-0000-000000000003"
                        ),
                        "workspace_id": WORKSPACE_A,
                        "environment_id": ENVIRONMENT_A,
                        "digest": "c" * 64,
                    },
                )
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text(
                    "DELETE FROM spine.idempotency_receipts "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
            await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_role_is_restricted_non_owner_with_rls_guarded_dml(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        role = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                    "rolbypassrls FROM pg_roles WHERE rolname = :role"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).one()
        owner = await connection.scalar(
            text(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname = 'spine'"
            )
        )
    assert tuple(role) == (False, False, False, False, False)
    assert owner != RUNTIME_ROLE

    async with _connection(migrated_database.runtime_url) as connection:
        visible = (
            await connection.execute(text("SELECT id FROM spine.workspaces"))
        ).scalars().all()
    assert visible == []
    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(text("SET ROLE spine_migration"))


@pytest.mark.asyncio(loop_scope="session")
async def test_rls_catalog_declares_forced_read_and_write_checks(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        tables = {
            row.relname: (row.relrowsecurity, row.relforcerowsecurity)
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname, relation.relrowsecurity, "
                        "relation.relforcerowsecurity FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN "
                        "('workspaces', 'environments', 'idempotency_receipts', "
                            "'context_profiles', 'context_bundles', "
                            "'answer_question_runs', 'answer_question_attempts', "
                            "'qa_session_versions', 'qa_message_versions', "
                            "'qa_disclosure_validation_results', "
                        "'qa_answer_disclosure_records', 'qa_answer_disclosure_deliveries', "
                        "'feedback_records', "
                        "'operational_metric_events', "
                        "'operational_correlation_records', "
                        "'initial_workspace_bootstrap', 'outbox_intents', "
                        "'outbox_deliveries', "
                        "'workspace_memberships', 'environment_memberships', "
                        "'environment_role_bindings', 'authorization_generations', "
                        "'qualification_authority_bindings', "
                        "'qualification_target_bindings', "
                            "'google_connection_versions', "
                            "'google_connection_approvals', "
                        "'admission_decisions', "
                        "'source_objects', 'source_revisions', "
                        "'source_revision_provenance', 'source_revision_receipt_associations', "
                        "'ingestion_runs', "
                        "'ingestion_run_sources', 'ingestion_run_source_bindings', "
                        "'ingestion_transitions', 'ingestion_warnings', "
                        "'ingestion_failures', 'processing_receipts', 'source_security_domain_bindings', "
                        "'process_only_operation_lineages', "
                        "'access_policy_versions', 'access_policy_grants', "
                        "'policy_proposals', 'policy_approvals', "
                        "'policy_activations', 'current_access_policies', "
                        "'backup_fence_leases', 'fence_operation_tickets', "
                        "'recovery_quiescence_evidence', 'storage_recovery_manifests', "
                        "'recovery_evidence_invalidations', "
                        "'canonical_generation_counters', 'canonical_acceptances', 'current_source_heads', "
                        "'canonical_boundaries', 'canonical_boundary_members', "
                        "'parsed_documents', 'parsed_document_segments', 'ontology_versions', "
                        "'projection_config_versions', 'projection_runs', 'projection_run_transitions', "
                        "'projection_partitions', 'projection_receipts', 'source_projection_references', "
                        "'projection_validations', 'projection_snapshots', 'projection_snapshot_members', "
                        "'projection_reconciliation_results', 'projection_activations', 'active_projections', "
                        "'projection_drifts', 'drive_snapshot_runs', 'drive_change_runs', 'google_sheets_sync_runs', 'google_sheets_identity_dispositions', 'drive_lifecycle_decisions', 'original_upload_records', "
                        "'qa_agent_artifacts', 'qa_agent_results')"
                    )
                )
            )
        }
        policies = {
            (row.table_name, row.policy_name): (
                row.roles,
                row.using_expression,
                row.check_expression,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, policy.polname "
                        "AS policy_name, ARRAY(SELECT role.rolname FROM pg_roles AS role "
                        "WHERE role.oid = ANY(policy.polroles)) AS roles, "
                        "pg_get_expr(policy.polqual, policy.polrelid) "
                        "AS using_expression, pg_get_expr(policy.polwithcheck, "
                        "policy.polrelid) AS check_expression "
                        "FROM pg_policy AS policy "
                        "JOIN pg_class AS relation ON relation.oid = policy.polrelid "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname <> 'audit_events'"
                    )
                )
            )
        }

    assert tables == {
        "admission_decisions": (True, True),
        "workspaces": (True, True),
        "environments": (True, True),
            "context_profiles": (True, True),
            "context_bundles": (True, True),
            "answer_question_runs": (True, True),
            "answer_question_attempts": (True, True),
            "qa_session_versions": (True, True),
            "qa_message_versions": (True, True),
            "qa_disclosure_validation_results": (True, True),
            "qa_answer_disclosure_records": (True, True),
            "qa_answer_disclosure_deliveries": (True, True),
            "feedback_records": (True, True),
            "operational_metric_events": (True, True),
            "operational_correlation_records": (True, True),
        "idempotency_receipts": (True, True),
        "initial_workspace_bootstrap": (True, True),
        "outbox_intents": (True, True),
        "outbox_deliveries": (True, True),
        "workspace_memberships": (True, True),
        "environment_memberships": (True, True),
        "environment_role_bindings": (True, True),
        "authorization_generations": (True, True),
        "qualification_authority_bindings": (True, True),
        "qualification_target_bindings": (True, True),
        "google_connection_versions": (True, True),
        "google_connection_approvals": (True, True),
        "source_objects": (True, True),
        "source_revisions": (True, True),
        "source_revision_provenance": (True, True),
        "source_revision_receipt_associations": (True, True),
        "ingestion_runs": (True, True),
        "ingestion_run_sources": (True, True),
        "ingestion_run_source_bindings": (True, True),
        "ingestion_transitions": (True, True),
        "ingestion_warnings": (True, True),
        "ingestion_failures": (True, True),
            "processing_receipts": (True, True),
        "process_only_operation_lineages": (True, True),
        "source_security_domain_bindings": (True, True),
            "access_policy_versions": (True, True),
            "access_policy_grants": (True, True),
            "policy_proposals": (True, True),
            "policy_approvals": (True, True),
            "policy_activations": (True, True),
            "current_access_policies": (True, True),
            "backup_fence_leases": (True, True),
            "fence_operation_tickets": (True, True),
            "recovery_quiescence_evidence": (True, True),
            "storage_recovery_manifests": (True, True),
            "recovery_evidence_invalidations": (True, True),
            "canonical_generation_counters": (True, True),
            "canonical_acceptances": (True, True),
            "current_source_heads": (True, True),
            "parsed_documents": (True, True),
            "parsed_document_segments": (True, True),
            "canonical_boundaries": (True, True),
            "canonical_boundary_members": (True, True),
            "ontology_versions": (True, True),
            "projection_config_versions": (True, True),
            "projection_runs": (True, True),
            "projection_run_transitions": (True, True),
            "projection_partitions": (True, True),
            "projection_receipts": (True, True),
            "source_projection_references": (True, True),
            "projection_validations": (True, True),
            "projection_snapshots": (True, True),
            "projection_snapshot_members": (True, True),
            "projection_reconciliation_results": (True, True),
            "projection_activations": (True, True),
            "active_projections": (True, True),
            "projection_drifts": (True, True),
            "drive_snapshot_runs": (True, True),
            "drive_change_runs": (True, True),
            "google_sheets_sync_runs": (True, True),
            "google_sheets_identity_dispositions": (True, True),
            "drive_lifecycle_decisions": (True, True),
            "original_upload_records": (True, True),
            "qa_agent_artifacts": (True, True),
            "qa_agent_results": (True, True),
        }
    standard_tables = set(tables) - {"outbox_deliveries"}
    assert set(policies) == {
        (table_name, f"pol_{table_name}_{policy_kind}")
        for table_name in standard_tables
        for policy_kind in ("tenant_isolation", "migration_maintenance")
    } | {
        ("outbox_intents", "pol_outbox_intents_dispatcher_read"),
        ("outbox_deliveries", "pol_outbox_deliveries_tenant_read"),
        ("outbox_deliveries", "pol_outbox_deliveries_dispatcher"),
        ("outbox_deliveries", "pol_outbox_deliveries_migration_maintenance"),
    }
    for table_name in standard_tables:
        roles, using_expression, check_expression = policies[
            (table_name, f"pol_{table_name}_tenant_isolation")
        ]
        assert roles == [RUNTIME_ROLE]
        assert using_expression is not None
        if table_name == "google_connection_approvals":
            assert check_expression is None
        else:
            assert check_expression is not None
        if table_name in {
            "backup_fence_leases",
            "fence_operation_tickets",
            "recovery_quiescence_evidence",
            "storage_recovery_manifests",
            "recovery_evidence_invalidations",
        }:
            assert "spine.deployment_scope_id" in using_expression
            assert "spine.deployment_scope_id" in check_expression
        else:
            assert "spine.workspace_id" in using_expression
            if check_expression is not None:
                assert "spine.workspace_id" in check_expression
        maintenance = policies[
            (table_name, f"pol_{table_name}_migration_maintenance")
        ]
        assert maintenance == ([MIGRATION_ROLE], "true", "true")
    assert policies[("outbox_intents", "pol_outbox_intents_dispatcher_read")][0] == [
        DISPATCHER_ROLE
    ]
    delivery_read = policies[
        ("outbox_deliveries", "pol_outbox_deliveries_tenant_read")
    ]
    assert delivery_read[0] == [RUNTIME_ROLE]
    assert delivery_read[1] is not None and delivery_read[2] is None
    assert policies[("outbox_deliveries", "pol_outbox_deliveries_dispatcher")] == (
        [DISPATCHER_ROLE],
        "true",
        "true",
    )
    assert policies[
        ("outbox_deliveries", "pol_outbox_deliveries_migration_maintenance")
    ] == ([MIGRATION_ROLE], "true", "true")
    assert "spine.environment_id" in policies[
        ("environments", "pol_environments_tenant_isolation")
    ][1]
    assert "spine.environment_id" in policies[
        ("environments", "pol_environments_tenant_isolation")
    ][2]
    assert "spine.environment_id" in policies[
        ("idempotency_receipts", "pol_idempotency_receipts_tenant_isolation")
    ][1]
    assert "spine.environment_id" in policies[
        ("idempotency_receipts", "pol_idempotency_receipts_tenant_isolation")
    ][2]
    assert "spine.environment_id" in policies[
        ("outbox_intents", "pol_outbox_intents_tenant_isolation")
    ][1]
    assert "spine.environment_id" in policies[
        ("outbox_intents", "pol_outbox_intents_tenant_isolation")
    ][2]


@pytest.mark.asyncio(loop_scope="session")
async def test_access_policy_catalog_enforces_lineage_immutability_and_receipt_scope(
    migrated_database: MigratedDatabase,
) -> None:
    policy_tables = (
        "access_policy_versions",
        "access_policy_grants",
        "policy_proposals",
        "policy_approvals",
        "policy_activations",
        "current_access_policies",
    )
    async with _connection(migrated_database.migration.url) as connection:
        constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT constraint_record.conname FROM pg_constraint AS constraint_record "
                        "JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname = ANY(:tables)"
                    ),
                    {"tables": list(policy_tables)},
                )
            ).scalars()
        )
        triggers = {
            row.tgname: (
                row.tgdeferrable,
                row.tginitdeferred,
                row.definition,
                row.owner,
                row.runtime_trigger,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT trigger.tgname, trigger.tgdeferrable, trigger.tginitdeferred, "
                        "pg_get_triggerdef(trigger.oid) AS definition, "
                        "pg_get_userbyid(relation.relowner) AS owner, "
                        "has_table_privilege(:runtime, relation.oid, 'TRIGGER') "
                        "AS runtime_trigger FROM pg_trigger AS trigger "
                        "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname = ANY(:tables) "
                        "AND NOT trigger.tgisinternal"
                    ),
                    {"tables": list(policy_tables), "runtime": RUNTIME_ROLE},
                )
            ).mappings()
        }
        indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname = 'spine' "
                        "AND tablename = ANY(:tables)"
                    ),
                    {"tables": list(policy_tables)},
                )
            ).scalars()
        )
        functions = {
            row.proname: (
                row.security_definer,
                tuple(row.config or ()),
                row.public_execute,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT procedure.proname, procedure.prosecdef AS security_definer, "
                        "procedure.proconfig AS config, "
                        "has_function_privilege('public', procedure.oid, 'EXECUTE') "
                        "AS public_execute FROM pg_proc AS procedure "
                        "JOIN pg_namespace AS namespace ON namespace.oid = "
                        "procedure.pronamespace WHERE namespace.nspname = 'spine' "
                        "AND procedure.proname IN "
                        "('reject_access_policy_history_mutation', "
                        "'enforce_current_access_policy_cas')"
                    )
                )
            ).mappings()
        }

    expected_constraints = {
        "pk_access_policy_versions",
        "uq_access_policy_versions_scope_source_id",
        "uq_access_policy_versions_source_version",
        "uq_access_policy_versions_source_hash",
        "fk_access_policy_versions_source",
        "fk_access_policy_versions_predecessor",
        "ck_access_policy_versions_positive_versions",
        "ck_access_policy_versions_chain",
        "ck_access_policy_versions_hashes",
        "ck_access_policy_versions_schema",
        "ck_access_policy_versions_origin",
        "ck_access_policy_versions_security_domain_schema",
        "pk_access_policy_grants",
        "fk_access_policy_grants_policy",
        "ck_access_policy_grants_authority_shape",
        "ck_access_policy_grants_validity",
        "ck_access_policy_grants_service_identifiers",
        "pk_policy_proposals",
        "uq_policy_proposals_scope_source_id",
        "uq_policy_proposals_exact",
        "fk_policy_proposals_policy",
        "fk_policy_proposals_predecessor",
        "ck_policy_proposals_generations",
        "ck_policy_proposals_benefit",
        "pk_policy_approvals",
        "uq_policy_approvals_scope_source_id",
        "fk_policy_activations_proposal",
        "fk_policy_activations_approval",
        "fk_policy_activations_idempotency",
        "fk_policy_activations_audit",
        "fk_policy_activations_outbox",
        "uq_policy_approvals_exact",
        "fk_policy_approvals_exact_proposal",
        "uq_policy_approvals_proposal_approver_decision",
        "ck_policy_approvals_decision",
        "ck_policy_approvals_reason",
        "pk_policy_activations",
        "uq_policy_activations_head_tuple",
        "uq_policy_activations_source_policy",
        "uq_policy_activations_source_generation",
        "uq_policy_activations_idempotency_receipt",
        "fk_policy_activations_new_policy",
        "fk_policy_activations_predecessor",
        "ck_policy_activations_generations",
        "ck_policy_activations_evidence_shape",
        "pk_current_access_policies",
        "uq_current_access_policies_scope_source",
        "fk_current_access_policies_source",
        "fk_current_access_policies_activation",
        "ck_current_access_policies_positive_versions",
    }
    missing_constraints = {
        expected
        for expected in expected_constraints
        if expected not in constraints
        and not any(actual.endswith(expected) for actual in constraints)
        and not any(
            actual.startswith(f"ck_{table}_{expected}"[:50])
            for table in policy_tables
            for actual in constraints
            if expected.startswith(f"ck_{table}_")
        )
    }
    assert missing_constraints == set()
    expected_indexes = {
        "pk_access_policy_versions",
        "uq_access_policy_versions_scope_source_id",
        "uq_access_policy_versions_source_version",
        "uq_access_policy_versions_source_hash",
        "ix_access_policy_versions_source",
        "pk_access_policy_grants",
        "uq_access_policy_grants_normalized",
        "ix_access_policy_grants_principal",
        "pk_policy_proposals",
        "uq_policy_proposals_scope_source_id",
        "uq_policy_proposals_exact",
        "ix_policy_proposals_baseline",
        "pk_policy_approvals",
        "uq_policy_approvals_scope_source_id",
        "uq_policy_approvals_exact",
        "uq_policy_approvals_proposal_approver_decision",
        "ix_policy_approvals_proposal",
        "pk_policy_activations",
        "uq_policy_activations_head_tuple",
        "uq_policy_activations_source_policy",
        "uq_policy_activations_source_generation",
        "uq_policy_activations_idempotency_receipt",
        "ix_policy_activations_source",
        "pk_current_access_policies",
        "uq_current_access_policies_scope_source",
        "ix_current_access_policies_policy_generation",
    }
    assert expected_indexes <= indexes
    receipt_trigger = triggers["trg_policy_activations_receipt_scope"]
    assert receipt_trigger[:2] == (True, True)
    assert "CREATE CONSTRAINT TRIGGER" in receipt_trigger[2]
    assert "AFTER INSERT OR UPDATE" in receipt_trigger[2]
    assert "DEFERRABLE INITIALLY DEFERRED" in receipt_trigger[2]
    assert "FOR EACH ROW" in receipt_trigger[2]
    assert "validate_issue7_receipt_scope" in receipt_trigger[2]
    assert receipt_trigger[3:] == (MIGRATION_ROLE, False)
    for table in set(policy_tables) - {"current_access_policies"}:
        assert f"trg_{table}_immutable" in triggers
    assert "trg_current_access_policies_cas" in triggers
    assert set(functions) == {
        "reject_access_policy_history_mutation",
        "enforce_current_access_policy_cas",
    }
    for security_definer, config, public_execute in functions.values():
        assert security_definer is False
        assert config == ("search_path=pg_catalog, spine",)
        assert public_execute is False


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_grants_are_minimal_and_tables_remain_migration_owned(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        privilege_names = (
            "select_ok",
            "insert_ok",
            "update_ok",
            "delete_ok",
            "truncate_ok",
            "references_ok",
            "trigger_ok",
        )
        privileges = {
            row["table_name"]: tuple(row[privilege] for privilege in privilege_names)
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, "
                        "has_table_privilege(:role, relation.oid, 'SELECT') AS select_ok, "
                        "has_table_privilege(:role, relation.oid, 'INSERT') AS insert_ok, "
                        "has_table_privilege(:role, relation.oid, 'UPDATE') AS update_ok, "
                        "has_table_privilege(:role, relation.oid, 'DELETE') AS delete_ok, "
                        "has_table_privilege(:role, relation.oid, 'TRUNCATE') AS truncate_ok, "
                        "has_table_privilege(:role, relation.oid, 'REFERENCES') "
                        "AS references_ok, has_table_privilege(:role, relation.oid, "
                        "'TRIGGER') AS trigger_ok FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN "
                        "('alembic_version', 'workspaces', 'environments', "
                        "'idempotency_receipts', 'initial_workspace_bootstrap', "
                        "'outbox_intents')"
                    ),
                    {"role": RUNTIME_ROLE},
                )
            ).mappings()
        }
        owners = {
            row.relname: row.owner
        for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname, pg_get_userbyid(relation.relowner) "
                        "AS owner FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN "
                        "('alembic_version', 'workspaces', 'environments', "
                        "'idempotency_receipts', 'initial_workspace_bootstrap', "
                        "'outbox_intents')"
                    )
                )
            )
        }
        schema_privileges = (
            await connection.execute(
                text(
                    "SELECT has_schema_privilege(:role, 'spine', 'USAGE'), "
                    "has_schema_privilege(:role, 'spine', 'CREATE')"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).one()
        receipt_update_columns = {
            row.column_name: row.update_ok
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name, has_column_privilege("
                        ":role, 'spine.idempotency_receipts', column_name, 'UPDATE'"
                        ") AS update_ok FROM information_schema.columns "
                        "WHERE table_schema = 'spine' "
                        "AND table_name = 'idempotency_receipts'"
                    ),
                    {"role": RUNTIME_ROLE},
                )
            ).mappings()
        }

    assert privileges["workspaces"] == (True, True, True, True, False, False, False)
    assert privileges["alembic_version"] == (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
    )
    assert privileges["environments"] == (True, True, True, True, False, False, False)
    assert privileges["idempotency_receipts"] == (
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    )
    assert privileges["initial_workspace_bootstrap"] == (
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    )
    assert privileges["outbox_intents"] == (
        False,
        True,
        False,
        False,
        False,
        False,
        False,
    )
    assert owners == {
        "alembic_version": MIGRATION_ROLE,
        "workspaces": MIGRATION_ROLE,
        "environments": MIGRATION_ROLE,
        "idempotency_receipts": MIGRATION_ROLE,
        "initial_workspace_bootstrap": MIGRATION_ROLE,
        "outbox_intents": MIGRATION_ROLE,
    }
    assert {
        column for column, allowed in receipt_update_columns.items() if allowed
    } == {"result_type", "result_id", "result_schema_version"}
    assert tuple(schema_privileges) == (True, False)


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_cannot_mutate_immutable_canonical_evidence(
    migrated_database: MigratedDatabase,
) -> None:
    immutable_tables = (
        "source_objects",
        "source_revisions",
        "source_revision_provenance",
        "source_revision_receipt_associations",
        "admission_decisions",
        "access_policy_versions",
        "access_policy_grants",
        "policy_proposals",
        "policy_approvals",
        "policy_activations",
        "source_security_domain_bindings",
    )
    async with _connection(migrated_database.migration.url) as connection:
        privileges = {
            row.table_name: (
                row.select_ok,
                row.insert_ok,
                row.update_ok,
                row.delete_ok,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, "
                        "has_table_privilege(:role, relation.oid, 'SELECT') AS select_ok, "
                        "has_table_privilege(:role, relation.oid, 'INSERT') AS insert_ok, "
                        "has_table_privilege(:role, relation.oid, 'UPDATE') AS update_ok, "
                        "has_table_privilege(:role, relation.oid, 'DELETE') AS delete_ok "
                        "FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname IN "
                        "(:source_objects, :source_revisions, "
                        ":source_revision_provenance, "
                        ":source_revision_receipt_associations, "
                        ":admission_decisions, :access_policy_versions, "
                        ":access_policy_grants, :policy_proposals, "
                        ":policy_approvals, :policy_activations, "
                        ":source_security_domain_bindings)"
                    ),
                    {
                        "role": RUNTIME_ROLE,
                        **{
                            table_name: table_name
                            for table_name in immutable_tables
                        },
                    },
                )
            ).mappings()
        }
    assert set(privileges) == set(immutable_tables)
    assert set(privileges.values()) == {(True, True, False, False)}


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_receipt_integrity_objects_are_catalogued_and_restricted(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        functions = {
            row.proname: (
                row.owner,
                row.language,
                row.volatility,
                row.security_definer,
                tuple(row.config or ()),
                row.public_execute,
                row.runtime_execute,
                row.definition,
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT procedure.proname, pg_get_userbyid(procedure.proowner) "
                        "AS owner, language.lanname AS language, "
                        "procedure.provolatile AS volatility, procedure.prosecdef "
                        "AS security_definer, procedure.proconfig AS config, "
                        "pg_get_functiondef(procedure.oid) AS definition, "
                        "has_function_privilege('public', procedure.oid, 'EXECUTE') "
                        "AS public_execute, has_function_privilege(:runtime, "
                        "procedure.oid, 'EXECUTE') AS runtime_execute "
                        "FROM pg_proc AS procedure JOIN pg_namespace AS namespace "
                        "ON namespace.oid = procedure.pronamespace "
                        "JOIN pg_language AS language ON language.oid = procedure.prolang "
                        "WHERE namespace.nspname = 'spine' AND procedure.proname IN "
                        "('validate_issue7_receipt_scope', "
                        "'reject_idempotency_receipt_scope_update', "
                        "'validate_idempotency_receipt_tenant', "
                        "'enforce_idempotency_receipt_scope_immutable', "
                        "'enforce_idempotency_receipt_actor_immutable')"
                    ),
                    {"runtime": RUNTIME_ROLE},
                )
            ).mappings()
        }
        trigger = (
            await connection.execute(
                text(
                    "SELECT trigger.tgname, pg_get_userbyid(relation.relowner) AS owner, "
                    "trigger.tgenabled, trigger.tgdeferrable, trigger.tginitdeferred, "
                    "trigger.tgtype, pg_get_triggerdef(trigger.oid) AS definition "
                    "FROM pg_trigger AS trigger JOIN pg_class AS relation "
                    "ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relname = "
                    "'idempotency_receipts' AND NOT trigger.tgisinternal"
                )
            )
        ).mappings().all()

    assert set(functions) == {
        "validate_issue7_receipt_scope",
        "reject_idempotency_receipt_scope_update",
        "enforce_idempotency_receipt_actor_immutable",
    }
    for owner, language, volatility, security_definer, config, public_execute, runtime_execute, definition in functions.values():
        assert owner == MIGRATION_ROLE
        assert language == "plpgsql"
        assert volatility == "v"
        assert security_definer is False
        assert config == ("search_path=pg_catalog, spine",)
        assert public_execute is False
        assert runtime_execute is False
        if "validate_issue7_receipt_scope" in definition:
            assert "FOR KEY SHARE" in definition

    assert len(trigger) == 3
    scope_trigger = next(
        row for row in trigger if row.tgname == "trg_idempotency_receipts_scope_immutable"
    )
    assert scope_trigger.owner == MIGRATION_ROLE
    assert scope_trigger.tgenabled == "O"
    assert scope_trigger.tgdeferrable is False
    assert scope_trigger.tginitdeferred is False
    assert "BEFORE UPDATE OF workspace_id, environment_id" in scope_trigger.definition
    assert "reject_idempotency_receipt_scope_update" in scope_trigger.definition
    assert scope_trigger.tgtype == 19
    actor_trigger = next(
        row for row in trigger if row.tgname == "trg_idempotency_receipts_actor_immutable"
    )
    assert actor_trigger.owner == MIGRATION_ROLE
    assert actor_trigger.tgenabled == "O"
    assert actor_trigger.tgdeferrable is False
    assert actor_trigger.tginitdeferred is False
    assert "BEFORE UPDATE OF actor_id" in actor_trigger.definition
    assert "enforce_idempotency_receipt_actor_immutable" in actor_trigger.definition
    assert actor_trigger.tgtype == 19


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_migration_downgrades_and_reupgrades_cleanly(
    migrated_database: MigratedDatabase,
) -> None:
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = migrated_database.migration
    await asyncio.to_thread(command.downgrade, config, PRE_K0_REVISION)
    async with _connection(migrated_database.migration.url) as connection:
        assert await connection.scalar(
            text("SELECT to_regprocedure('spine.validate_issue7_receipt_scope()')")
        ) is None
        assert await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        ) == PRE_K0_REVISION

    await asyncio.to_thread(upgrade_database, migrated_database.migration)
    async with _connection(migrated_database.migration.url) as connection:
        assert await connection.scalar(
            text("SELECT to_regprocedure('spine.validate_issue7_receipt_scope()')")
        ) is not None
        assert await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        ) == HEAD_REVISION


@pytest_asyncio.fixture
async def k0_child_fixture(
    migrated_database: MigratedDatabase,
) -> AsyncIterator[None]:
    async with _connection(migrated_database.migration.url) as connection:
        await connection.execute(
            text(
                "CREATE TABLE spine.k0_receipt_reference_fixture ("
                "fixture_id uuid PRIMARY KEY, workspace_id uuid NOT NULL, "
                "environment_id uuid, idempotency_receipt_id uuid)"
            )
        )
        await connection.execute(
            text(
                "GRANT INSERT, SELECT ON spine.k0_receipt_reference_fixture "
                f"TO {RUNTIME_ROLE}"
            )
        )
        await connection.execute(
            text(
                "CREATE CONSTRAINT TRIGGER trg_k0_fixture_receipt_tenant "
                "AFTER INSERT OR UPDATE ON spine.k0_receipt_reference_fixture "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION spine.validate_issue7_receipt_scope()"
            )
        )
        trigger = (
            await connection.execute(
                text(
                    "SELECT trigger.tgtype, trigger.tgdeferrable, "
                    "trigger.tginitdeferred, pg_get_triggerdef(trigger.oid) AS definition "
                    "FROM pg_trigger AS trigger JOIN pg_class AS relation "
                    "ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' AND relation.relname = "
                    "'k0_receipt_reference_fixture' AND trigger.tgname = "
                    "'trg_k0_fixture_receipt_tenant'"
                )
            )
        ).one()
        assert trigger.tgtype == 21
        assert trigger.tgdeferrable is True
        assert trigger.tginitdeferred is True
        assert "AFTER INSERT OR UPDATE" in trigger.definition
        assert "FOR EACH ROW" in trigger.definition
        assert "validate_issue7_receipt_scope" in trigger.definition
        await connection.commit()
    try:
        yield
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text("DROP TABLE IF EXISTS spine.k0_receipt_reference_fixture")
            )
            await connection.execute(
                text(
                    "DELETE FROM spine.idempotency_receipts "
                    "WHERE operation_name = 'k0.fixture'"
                )
            )
            await connection.commit()


async def _insert_environment_receipt(
    database: MigratedDatabase,
    *,
    receipt_id: UUID,
    workspace_id: UUID,
    environment_id: UUID | None,
) -> None:
    async with _connection(database.migration.url) as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.idempotency_receipts "
                "(receipt_id, workspace_id, environment_id, operation_name, "
                "operation_schema_version, idempotency_key, digest_algorithm_version, "
                "command_digest) VALUES (:receipt_id, :workspace_id, :environment_id, "
                "'k0.fixture', 1, :idempotency_key, 'spine.command-digest.v1', "
                ":command_digest)"
            ),
            {
                "receipt_id": receipt_id,
                "workspace_id": workspace_id,
                "environment_id": environment_id,
                "idempotency_key": str(receipt_id),
                "command_digest": "a" * 64,
            },
        )
        await connection.commit()


async def _insert_k0_child(
    database: MigratedDatabase,
    *,
    fixture_id: UUID,
    workspace_id: UUID,
    environment_id: UUID | None,
    receipt_id: UUID | None,
    commit: bool = True,
) -> None:
    async with _connection(database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.k0_receipt_reference_fixture "
                "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
            ),
            {
                "fixture_id": fixture_id,
                "workspace_id": workspace_id,
                "environment_id": environment_id,
                "receipt_id": receipt_id,
            },
        )
        if commit:
            await connection.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_deferred_validator_accepts_same_tenant_receipt(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000101")
    await _insert_environment_receipt(
        migrated_database,
        receipt_id=receipt_id,
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
    )
    await _insert_k0_child(
        migrated_database,
        fixture_id=UUID("52000000-0000-0000-0000-000000000101"),
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
        receipt_id=receipt_id,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_deferred_validator_rollback_removes_child_and_retry_succeeds(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000108")
    await _insert_environment_receipt(
        migrated_database,
        receipt_id=receipt_id,
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
    )
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.k0_receipt_reference_fixture "
                "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
            ),
            {
                "fixture_id": UUID("52000000-0000-0000-0000-000000000108"),
                "workspace_id": WORKSPACE_B,
                "environment_id": ENVIRONMENT_A,
                "receipt_id": receipt_id,
            },
        )
        with pytest.raises(DBAPIError):
            await connection.commit()
        await connection.rollback()
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        await connection.execute(
            text(
                "INSERT INTO spine.k0_receipt_reference_fixture "
                "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
            ),
            {
                "fixture_id": UUID("52000000-0000-0000-0000-000000000109"),
                "workspace_id": WORKSPACE_A,
                "environment_id": ENVIRONMENT_A,
                "receipt_id": receipt_id,
            },
        )
        await connection.commit()

    async with _connection(migrated_database.migration.url) as connection:
        count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.k0_receipt_reference_fixture "
                "WHERE idempotency_receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt_id},
        )
    assert count == 1


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("workspace_id", "environment_id", "receipt_id"),
    (
        (WORKSPACE_B, ENVIRONMENT_A, "foreign_workspace"),
        (WORKSPACE_A, ENVIRONMENT_A_SECOND, "foreign_environment"),
        (WORKSPACE_A, None, "null_environment"),
        (WORKSPACE_A, ENVIRONMENT_A, "missing"),
        (WORKSPACE_A, ENVIRONMENT_A, "workspace_scoped"),
        (WORKSPACE_A, ENVIRONMENT_A, "hidden"),
    ),
)
async def test_k0_deferred_validator_rejects_foreign_or_unavailable_receipt(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
    workspace_id: UUID,
    environment_id: UUID | None,
    receipt_id: str,
) -> None:
    receipt_uuid = UUID("51000000-0000-0000-0000-000000000102")
    receipt_ids = {
        "foreign_workspace": UUID("51000000-0000-0000-0000-000000000102"),
        "foreign_environment": UUID("51000000-0000-0000-0000-000000000103"),
        "null_environment": UUID("51000000-0000-0000-0000-000000000104"),
        "hidden": UUID("51000000-0000-0000-0000-000000000105"),
        "workspace_scoped": UUID("51000000-0000-0000-0000-000000000106"),
    }
    if receipt_id in receipt_ids:
        receipt_uuid = receipt_ids[receipt_id]
        await _insert_environment_receipt(
            migrated_database,
            receipt_id=receipt_uuid,
            workspace_id=WORKSPACE_A,
            environment_id=(None if receipt_id == "workspace_scoped" else ENVIRONMENT_A),
        )
    if receipt_id == "hidden":
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=WORKSPACE_B,
                environment_id=ENVIRONMENT_B,
            )
            with pytest.raises(DBAPIError) as error:
                await connection.execute(
                    text(
                        "INSERT INTO spine.k0_receipt_reference_fixture "
                        "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                        "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
                    ),
                    {
                        "fixture_id": UUID("52000000-0000-0000-0000-000000000102"),
                        "workspace_id": WORKSPACE_A,
                        "environment_id": ENVIRONMENT_A,
                        "receipt_id": receipt_uuid,
                    },
                )
                await connection.commit()
            await connection.rollback()
        assert str(receipt_uuid) not in str(error.value)
        return

    selected_receipt = receipt_ids.get(receipt_id)
    with pytest.raises(DBAPIError) as error:
        await _insert_k0_child(
            migrated_database,
            fixture_id=UUID("52000000-0000-0000-0000-000000000102"),
            workspace_id=workspace_id,
            environment_id=environment_id,
            receipt_id=selected_receipt,
        )
    assert str(receipt_uuid) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_k0_deferred_validator_fails_closed_for_malformed_tenant_settings(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    k0_child_fixture: None,
) -> None:
    receipt_id = UUID("51000000-0000-0000-0000-000000000107")
    await _insert_environment_receipt(
        migrated_database,
        receipt_id=receipt_id,
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT_A,
    )
    with pytest.raises(DBAPIError) as error:
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(
                text("SELECT set_config('spine.workspace_id', 'not-a-workspace', true)")
            )
            await connection.execute(
                text("SELECT set_config('spine.environment_id', :value, true)"),
                {"value": str(ENVIRONMENT_A)},
            )
            await connection.execute(
                text(
                    "INSERT INTO spine.k0_receipt_reference_fixture "
                    "(fixture_id, workspace_id, environment_id, idempotency_receipt_id) "
                    "VALUES (:fixture_id, :workspace_id, :environment_id, :receipt_id)"
                ),
                {
                    "fixture_id": UUID("52000000-0000-0000-0000-000000000107"),
                    "workspace_id": WORKSPACE_A,
                    "environment_id": ENVIRONMENT_A,
                    "receipt_id": receipt_id,
                },
            )
            await connection.commit()
    assert str(receipt_id) not in str(error.value)
    assert str(WORKSPACE_A) not in str(error.value)
    assert str(ENVIRONMENT_A) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_rls_hides_and_rejects_another_workspace_rows(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
        visible = (
            await connection.execute(text("SELECT id FROM spine.workspaces ORDER BY id"))
        ).scalars().all()
        updated = (
            await connection.execute(
                text(
                    "UPDATE spine.workspaces SET display_name = 'Hidden update' "
                    "WHERE id = :workspace_id RETURNING id"
                ),
                {"workspace_id": WORKSPACE_B},
            )
        ).scalars().all()
        own_update = (
            await connection.execute(
                text(
                    "UPDATE spine.workspaces SET display_name = 'Visible update' "
                    "WHERE id = :workspace_id RETURNING id"
                ),
                {"workspace_id": WORKSPACE_A},
            )
        ).scalars().all()
        deleted = (
            await connection.execute(
                text(
                    "DELETE FROM spine.workspaces WHERE id = :workspace_id "
                    "RETURNING id"
                ),
                {"workspace_id": WORKSPACE_B},
            )
        ).scalars().all()

    assert visible == [WORKSPACE_A]
    assert updated == []
    assert deleted == []
    assert own_update == [WORKSPACE_A]

    rejected_id = UUID("20000000-0000-0000-0000-000000000099")
    with pytest.raises(DBAPIError) as error:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(connection, workspace_id=WORKSPACE_A)
            await connection.execute(
                text(
                    "INSERT INTO spine.workspaces (id, slug, display_name) "
                    "VALUES (:id, 'rejected-workspace', 'Rejected') RETURNING id"
                ),
                {"id": rejected_id},
            )
    assert str(rejected_id) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_environment_rls_isolates_two_environments_in_one_workspace(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
) -> None:
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=WORKSPACE_A,
            environment_id=ENVIRONMENT_A,
        )
        visible = (
            await connection.execute(text("SELECT id FROM spine.environments"))
        ).scalars().all()
        updated = (
            await connection.execute(
                text(
                    "UPDATE spine.environments SET display_name = 'Hidden update' "
                    "WHERE id = :environment_id RETURNING id"
                ),
                {"environment_id": ENVIRONMENT_A_SECOND},
            )
        ).scalars().all()
        own_delete = (
            await connection.execute(
                text(
                    "DELETE FROM spine.environments WHERE id = :environment_id "
                    "RETURNING id"
                ),
                {"environment_id": ENVIRONMENT_A},
            )
        ).scalars().all()

    assert visible == [ENVIRONMENT_A]
    assert updated == []
    assert own_delete == [ENVIRONMENT_A]

    rejected_environment = UUID("30000000-0000-0000-0000-000000000099")
    rejected_scopes = (
        (ENVIRONMENT_A, WORKSPACE_A),
        (rejected_environment, WORKSPACE_B),
    )
    for environment_setting, row_workspace in rejected_scopes:
        with pytest.raises(DBAPIError) as error:
            async with _connection(migrated_database.runtime_url) as connection:
                await _set_tenant_context(
                    connection,
                    workspace_id=WORKSPACE_A,
                    environment_id=environment_setting,
                )
                await connection.execute(
                    text(
                        "INSERT INTO spine.environments "
                        "(id, workspace_id, kind, display_name) VALUES "
                        "(:id, :workspace_id, 'development', 'Rejected') "
                        "RETURNING id"
                    ),
                    {"id": rejected_environment, "workspace_id": row_workspace},
                )
        assert str(rejected_environment) not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("workspace_setting", "environment_setting", "table_name"),
    [
        (None, None, "workspaces"),
        (WORKSPACE_A, None, "environments"),
        ("not-a-workspace", None, "workspaces"),
        (WORKSPACE_A, "not-an-environment", "environments"),
    ],
)
async def test_missing_or_malformed_settings_fail_closed_without_identifiers(
    migrated_database: MigratedDatabase,
    tenant_rows: None,
    workspace_setting: UUID | str | None,
    environment_setting: UUID | str | None,
    table_name: str,
) -> None:
    async with _connection(migrated_database.runtime_url) as connection:
        await _set_tenant_context(
            connection,
            workspace_id=workspace_setting,
            environment_id=environment_setting,
        )
        visible = (
            await connection.execute(text(f"SELECT id FROM spine.{table_name}"))
        ).scalars().all()
    assert visible == []

    protected_id = (
        "40000000-0000-0000-0000-000000000001"
        if table_name == "workspaces"
        else "40000000-0000-0000-0000-000000000002"
    )
    statement = (
        "INSERT INTO spine.workspaces (id, slug, display_name) "
        "VALUES (:id, 'missing-context', 'Missing Context')"
        if table_name == "workspaces"
        else "INSERT INTO spine.environments "
        "(id, workspace_id, kind, display_name) VALUES "
        "(:id, :workspace_id, 'development', 'Missing Context')"
    )
    with pytest.raises(DBAPIError) as error:
        async with _connection(migrated_database.runtime_url) as connection:
            await _set_tenant_context(
                connection,
                workspace_id=workspace_setting,
                environment_id=environment_setting,
            )
            await connection.execute(
                text(statement),
                {"id": protected_id, "workspace_id": WORKSPACE_A},
            )
    assert protected_id not in str(error.value)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE spine.runtime_ddl_attempt (id integer)",
        "ALTER TABLE spine.workspaces DISABLE ROW LEVEL SECURITY",
        "DROP POLICY pol_workspaces_tenant_isolation ON spine.workspaces",
        "ALTER TABLE spine.idempotency_receipts DISABLE TRIGGER "
        "trg_idempotency_receipts_scope_immutable",
        "ALTER FUNCTION spine.validate_issue7_receipt_scope() "
        "RENAME TO k0_validator_replaced",
        "DROP FUNCTION spine.validate_issue7_receipt_scope()",
        "ALTER ROLE spine_runtime BYPASSRLS",
        "SET ROLE spine_migration",
    ],
)
async def test_runtime_cannot_escalate_database_privileges(
    migrated_database: MigratedDatabase,
    statement: str,
) -> None:
    with pytest.raises(ProgrammingError):
        async with _connection(migrated_database.runtime_url) as connection:
            await connection.execute(text(statement))


@pytest.mark.asyncio(loop_scope="session")
async def test_operator_reconciles_unsafe_existing_runtime_membership(
    migrated_database: MigratedDatabase,
    postgresql_provision: TestDatabaseProvision,
) -> None:
    async with _connection(postgresql_provision.url) as connection:
        await connection.execute(text("GRANT spine_migration TO spine_runtime"))
        await connection.commit()

    await bootstrap_database_roles(migrated_database.operator)
    async with _connection(migrated_database.migration.url) as connection:
        can_assume = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members AS membership "
                "JOIN pg_roles AS parent ON parent.oid = membership.roleid "
                "JOIN pg_roles AS member ON member.oid = membership.member "
                "WHERE parent.rolname = :parent AND member.rolname = :member)"
            ),
            {"parent": MIGRATION_ROLE, "member": RUNTIME_ROLE},
        )
    assert can_assume is False
    await asyncio.to_thread(upgrade_database, migrated_database.migration)


@pytest.mark.asyncio(loop_scope="session")
async def test_real_preflight_rejects_unsafe_role_without_schema_changes(
    migrated_database: MigratedDatabase,
    postgresql_provision: TestDatabaseProvision,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        revision_before = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
    async with _connection(postgresql_provision.url) as connection:
        await connection.execute(text("ALTER ROLE spine_runtime BYPASSRLS"))
        await connection.commit()
    try:
        with pytest.raises(MigrationPreflightError):
            await asyncio.to_thread(upgrade_database, migrated_database.migration)
    finally:
        async with _connection(postgresql_provision.url) as connection:
            await connection.execute(text("ALTER ROLE spine_runtime NOBYPASSRLS"))
            await connection.commit()
    async with _connection(migrated_database.migration.url) as connection:
        revision_after = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
    assert revision_after == revision_before


@pytest.mark.asyncio(loop_scope="session")
async def test_real_preflight_rejects_runtime_write_access_to_revision_table(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        revision_before = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
        await connection.execute(
            text("GRANT UPDATE ON spine.alembic_version TO spine_runtime")
        )
        await connection.commit()
    try:
        with pytest.raises(MigrationPreflightError):
            await asyncio.to_thread(upgrade_database, migrated_database.migration)
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text("REVOKE UPDATE ON spine.alembic_version FROM spine_runtime")
            )
            await connection.commit()
    async with _connection(migrated_database.migration.url) as connection:
        revision_after = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
    assert revision_after == revision_before


@pytest.mark.asyncio(loop_scope="session")
async def test_initial_workspace_is_created_once_by_sealed_audited_action(
    migrated_database: MigratedDatabase,
) -> None:
    authority = create_initial_workspace_bootstrap_authority()
    adapter = PostgreSQLInitialWorkspaceBootstrap(
        settings=migrated_database.migration,
        authority=authority,
    )
    expected = Workspace(
        id=UUID("10000000-0000-0000-0000-000000000001"),
        slug="initial",
        display_name="Initial Workspace",
    )

    await adapter.create_initial_workspace(authority, expected)
    with pytest.raises(ConstraintConflictError, match="sealed"):
        await adapter.create_initial_workspace(authority, expected)

    async with _connection(migrated_database.migration.url) as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT workspace.id, bootstrap.executed_by "
                    "FROM spine.workspaces AS workspace "
                    "JOIN spine.initial_workspace_bootstrap AS bootstrap "
                    "ON bootstrap.workspace_id = workspace.id"
                )
            )
        ).one()
    assert row.id == expected.id
    assert row.executed_by == MIGRATION_ROLE


def _write_failing_revision(tmp_path: Path) -> Config:
    source = Path(__file__).parents[3] / "migrations"
    target = tmp_path / "migrations"
    (target / "versions").mkdir(parents=True)
    shutil.copy(source / "env.py", target / "env.py")
    shutil.copy(source / "script.py.mako", target / "script.py.mako")
    shutil.copy(
        source / "versions" / "20261009_01_initial_tenancy.py",
        target / "versions" / "20261009_01_initial_tenancy.py",
    )
    shutil.copy(
        source / "versions" / "20261009_02_tenant_rls.py",
        target / "versions" / "20261009_02_tenant_rls.py",
    )
    shutil.copy(
        source / "versions" / "20261010_03_idempotency_receipts.py",
        target / "versions" / "20261010_03_idempotency_receipts.py",
    )
    shutil.copy(
        source / "versions" / "20261010_04_outbox_intents.py",
        target / "versions" / "20261010_04_outbox_intents.py",
    )
    shutil.copy(
        source / "versions" / "20261010_05_runtime_readiness.py",
        target / "versions" / "20261010_05_runtime_readiness.py",
    )
    shutil.copy(
        source / "versions" / "20261010_06_receipt_tenant_integrity.py",
        target / "versions" / "20261010_06_receipt_tenant_integrity.py",
    )
    for revision_file in (
        "20261010_07_authorization_directory.py",
        "20261010_08_audit_events.py",
        "20261010_09_source_observations.py",
        "20261010_10_source_observation_integrity.py",
        "20261010_11_source_profile_identity.py",
        "20261010_12_source_canonical_validation.py",
        "20261011_13_outbox_deliveries.py",
        "20261012_14_atomic_outbox_delivery_audit.py",
        "20261012_15_admission_decisions.py",
        "20261013_15_ingestion_operational_ledger.py",
        "20261013_16_ingestion_ledger_hardening.py",
        "20261014_17_source_receipt_associations.py",
        "20261015_18_access_policy_persistence.py",
        "20261016_19_backup_recovery_coordination.py",
        "20261017_20_recovery_evidence_integrity.py",
        "20261018_21_canonical_acceptance.py",
        "20261019_22_parsed_artifacts.py",
        "20261020_23_canonical_boundaries.py",
        "20261021_24_projection_candidate_ledgers.py",
        "20261021_25_projection_snapshot_sealing.py",
        "20261022_26_projection_publication.py",
        "20261023_27_context_profiles.py",
        "20261024_28_context_bundles.py",
        "20261025_29_authorization_team_memberships.py",
        "20261026_30_answer_question_runs.py",
        "20261027_31_qa_sessions.py",
        "20261027_32_disclosure_revalidation.py",
        "20261028_33_answer_disclosure.py",
        "20261029_34_answer_disclosure_delivery.py",
        "20261030_35_safe_policy_metadata.py",
        "20261030_36_feedback_records.py",
        "20261031_37_processing_commands.py",
        "20261101_38_outbox_delivery_correlation_reference.py",
        "20261102_39_recovery_component_artifacts.py",
        "20261103_40_operational_metrics.py",
        "20261104_41_operational_correlation.py",
        "20261105_42_operational_metric_correlation_refs.py",
        "20261106_43_idempotency_actor_scope.py",
        "20261107_44_answer_question_admission_state.py",
        "20261108_45_qa_cancellation_intent.py",
        "20261109_46_qa_message_row_lock_privilege.py",
        "20261110_47_canonical_qa_attempt_allocation.py",
        "20261111_48_ingestion_binding_receipt_guard.py",
        "20261112_49_ingestion_binding_receipt_associations.py",
        "20261113_50_projection_reference_cardinality.py",
        "20261114_51_original_upload_records.py",
        "20261115_52_nullable_source_canonicalization.py",
        "20261116_53_qa_agent_artifacts.py",
        "20261117_54_worker_authorization_generation_read.py",
        "20261118_55_runtime_outbox_correlation_read.py",
        "20261119_56_qualification_authority_expiry.py",
        "20261120_57_qualification_target_bindings.py",
        "20261121_58_ingestion_qualification_failure_codes.py",
        "20261122_59_projection_terminal_transitions.py",
        "20261123_60_google_connection_admission.py",
        "20261124_61_drive_revision_metadata.py",
        "20261125_62_drive_initial_snapshot.py",
        "20261126_63_drive_incremental_changes.py",
        "20261127_64_google_sheets_reconciliation.py",
        "20261128_65_google_sheets_identity_quarantine.py",
        "20261129_66_drive_lifecycle_decisions.py",
        "20261130_67_google_revocation_states.py",
    ):
        shutil.copy(source / "versions" / revision_file, target / "versions" / revision_file)
    (target / "versions" / "20261131_68_injected_failure.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = '20261131_68'\n"
        "down_revision = '20261130_67'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n"
        "    op.create_table('injected_failure', sa.Column('id', sa.Integer()), schema='spine')\n"
        "    raise RuntimeError('injected migration failure')\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    config = Config()
    config.set_main_option("script_location", str(target))
    return config


@pytest.mark.asyncio(loop_scope="session")
async def test_transactional_migration_failure_preserves_prior_schema(
    migrated_database: MigratedDatabase,
    tmp_path: Path,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        workspaces_before = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
    config = _write_failing_revision(tmp_path)
    config.attributes["migration_settings"] = migrated_database.migration
    with pytest.raises(RuntimeError, match="injected migration failure"):
        await asyncio.to_thread(command.upgrade, config, "head")

    async with _connection(migrated_database.migration.url) as connection:
        revision = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )
        workspaces_after = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
        failure_table = await connection.scalar(
            text("SELECT to_regclass('spine.injected_failure')")
        )
    assert revision == HEAD_REVISION
    assert workspaces_after == workspaces_before
    assert failure_table is None


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_revision_is_rejected_without_schema_changes(
    migrated_database: MigratedDatabase,
) -> None:
    async with _connection(migrated_database.migration.url) as connection:
        workspaces_before = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
        await connection.execute(
            text("UPDATE spine.alembic_version SET version_num = 'unknown_revision'")
        )
        await connection.commit()
    try:
        with pytest.raises(CommandError, match="unknown_revision"):
            await asyncio.to_thread(upgrade_database, migrated_database.migration)
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(
                text("UPDATE spine.alembic_version SET version_num = :revision"),
                {"revision": HEAD_REVISION},
            )
            await connection.commit()
    async with _connection(migrated_database.migration.url) as connection:
        workspaces_after = await connection.scalar(
            text("SELECT count(*) FROM spine.workspaces")
        )
    assert workspaces_after == workspaces_before


@pytest.mark.asyncio(loop_scope="session")
async def test_every_retained_revision_upgrades_to_one_final_head(
    migrated_database: MigratedDatabase,
) -> None:
    starting_revisions = (None, *migrated_database.retained_revisions)
    try:
        for starting_revision in starting_revisions:
            async with _connection(migrated_database.migration.url) as connection:
                await connection.execute(text("DROP SCHEMA spine CASCADE"))
                await connection.commit()

            if starting_revision is not None:
                await asyncio.to_thread(
                    upgrade_database,
                    migrated_database.migration,
                    revision=starting_revision,
                )
                async with _connection(migrated_database.migration.url) as connection:
                    assert await connection.scalar(
                        text("SELECT version_num FROM spine.alembic_version")
                    ) == starting_revision

            await asyncio.to_thread(upgrade_database, migrated_database.migration)
            async with _connection(migrated_database.migration.url) as connection:
                await _assert_final_catalog_after_upgrade(connection)
    finally:
        async with _connection(migrated_database.migration.url) as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS spine CASCADE"))
            await connection.commit()
        await asyncio.to_thread(upgrade_database, migrated_database.migration)
