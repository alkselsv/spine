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
