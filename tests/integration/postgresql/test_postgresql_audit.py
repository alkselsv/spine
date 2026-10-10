from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.diagnostics.audit import (
    AuditEventRegistry,
    AuditOutcome,
    CommandAuditPayload,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import PersistenceUnavailableError
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import (
    PostgreSQLPersistence,
    PostgreSQLTransactionStage,
)
from spine.domain.workspaces import Workspace
from tests.contracts.persistence.adapter import AuditPersistenceAdapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.outbox_events import (
    WorkspaceCreatedPayload,
    create_outbox_event_registry,
)
from tests.contracts.persistence.test_audit_writer_contract import (
    test_audit_event_becomes_visible_only_after_explicit_commit as contract_audit_commit,
    test_audit_failure_rolls_back_mutation_and_outbox_intent as contract_audit_failure,
    test_required_audit_commits_before_releasing_effect as contract_required_commit,
    test_audit_context_substitution_is_rejected_before_append as contract_context_substitution,
    test_mutated_content_bearing_payload_is_rejected_with_safe_error as contract_safe_payload,
    test_duplicate_producer_identity_returns_original_audit_event as contract_replay,
    test_conflicting_producer_identity_raises_stable_audit_conflict as contract_producer_conflict,
    test_audit_reader_does_not_reveal_event_to_another_workspace as contract_isolation,
    test_required_audit_failure_never_releases_effect as contract_required_failure,
    test_concurrent_duplicate_commit_keeps_one_logical_audit_event as contract_concurrent_replay,
    test_distinct_retry_attempt_identities_create_distinct_audit_events as contract_retry_attempts,
    test_exit_without_commit_discards_audit_event as contract_implicit_rollback,
    test_denied_required_audit_never_releases_protected_effect as contract_denied,
    test_denied_required_audit_failure_still_never_releases_effect as contract_denied_failure,
    test_rejected_required_audit_never_releases_protected_effect as contract_rejected,
    test_allowed_required_audit_releases_without_leaking_target_metadata as contract_allowed_safe,
    test_historical_audit_event_cannot_authorize_a_new_unit_of_work as contract_not_authority,
)


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.asyncio(loop_scope="session"),
]

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
MIGRATION_PASSWORD = "migration-audit-secret"
RUNTIME_PASSWORD = "runtime-audit-secret"


@dataclass(frozen=True, slots=True)
class AuditDatabase:
    migration_engine: AsyncEngine
    runtime_engine: AsyncEngine


class AuditFailureSwitch:
    def __init__(self) -> None:
        self.fail_next_generated_identity = False

    def fail_next(self) -> None:
        self.fail_next_generated_identity = True


class AuditFailureProbe:
    """Inject one sanitized failure at the public audit transaction boundary."""

    def __init__(self, failure: AuditFailureSwitch) -> None:
        self._failure = failure

    async def __call__(self, stage: PostgreSQLTransactionStage) -> None:
        if (
            self._failure.fail_next_generated_identity
            and stage is PostgreSQLTransactionStage.BEFORE_AUDIT_IDENTITY
        ):
            self._failure.fail_next_generated_identity = False
            raise OperationalError(
                "AUDIT_IDENTITY_BOUNDARY",
                {},
                RuntimeError("synthetic audit persistence failure"),
            )


class AtomicFailureProbe:
    def __init__(self, stage: str) -> None:
        self._stage = stage

    async def __call__(self, stage: PostgreSQLTransactionStage) -> None:
        if (
            self._stage == "after_flush"
            and stage is PostgreSQLTransactionStage.AFTER_AUDIT_FLUSH
        ) or (
            self._stage == "pre_commit"
            and stage is PostgreSQLTransactionStage.BEFORE_COMMIT
        ):
            raise OperationalError(
                f"AUDIT_TRANSACTION_BOUNDARY:{self._stage}",
                {},
                RuntimeError("synthetic audit transaction failure"),
            )


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def audit_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[AuditDatabase]:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration_url = _role_url(
        postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD
    )
    await asyncio.to_thread(
        upgrade_database,
        MigrationDatabaseSettings(
            url=migration_url,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
        ),
    )
    migration_engine = create_async_engine(
        migration_url.get_secret_value(), hide_parameters=True
    )
    runtime_engine = create_async_engine(
        _role_url(
            postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD
        ).get_secret_value(),
        pool_size=5,
        max_overflow=0,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        yield AuditDatabase(
            migration_engine=migration_engine,
            runtime_engine=runtime_engine,
        )
    finally:
        await runtime_engine.dispose()
        async with migration_engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE spine.workspaces CASCADE")
            )
        await migration_engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def postgresql_audit_adapter(
    audit_database: AuditDatabase,
) -> AuditPersistenceAdapter:
    session_factory = async_sessionmaker(
        audit_database.runtime_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2600),
        secret=b"issue-64-postgresql-audit-contract",
    )
    outbox_events = create_outbox_event_registry()
    audit_events = AuditEventRegistry.with_default_families()
    audit_failure = AuditFailureSwitch()
    persistence = PostgreSQLPersistence(
        session_factory=session_factory,
        context_verifier=boundary,
        outbox_events=outbox_events,
        audit_events=audit_events,
        transaction_probe=AuditFailureProbe(audit_failure),
    )

    def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(2601),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(2602),
        )

    def environment_context(
        workspace_id: UUID,
        environment_id: UUID,
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=synthetic_uuid(2603),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(2604),
        )

    def worker_workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(2605),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(2606),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        yield

    return AuditPersistenceAdapter(
        uow_factory=persistence.uow_factory,
        workspace_context=workspace_context,
        worker_workspace_context=worker_workspace_context,
        environment_context=environment_context,
        hold_transactions=hold_transactions,
        outbox_events=outbox_events,
        audit_events=audit_events,
        read_audit_event=persistence.audit_reader.resolve,
        fail_next_audit_append=audit_failure.fail_next,
    )


async def test_postgresql_audit_event_becomes_visible_only_after_explicit_commit(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_audit_commit(postgresql_audit_adapter)


async def test_postgresql_audit_failure_rolls_back_mutation_and_outbox_intent(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_audit_failure(postgresql_audit_adapter)


async def test_postgresql_required_audit_commits_before_releasing_effect(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_required_commit(postgresql_audit_adapter)


@pytest.mark.parametrize(
    "substitution",
    (
        {"workspace_id": synthetic_uuid(2231)},
        {"environment_id": synthetic_uuid(2232)},
        {"acting_subject_id": synthetic_uuid(2233)},
        {"service_principal_id": synthetic_uuid(2234)},
        {"trace_id": synthetic_uuid(2235)},
        {
            "origin": "worker",
            "acting_subject_id": None,
            "service_principal_id": synthetic_uuid(2236),
        },
    ),
)
async def test_postgresql_audit_context_substitution_is_rejected_before_append(
    postgresql_audit_adapter: AuditPersistenceAdapter,
    substitution: dict[str, object],
) -> None:
    await contract_context_substitution(postgresql_audit_adapter, substitution)


async def test_postgresql_mutated_payload_is_rejected_with_safe_error(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_safe_payload(postgresql_audit_adapter)


async def test_postgresql_duplicate_producer_identity_returns_original_event(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_replay(postgresql_audit_adapter)


async def test_postgresql_conflicting_producer_identity_is_stable(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_producer_conflict(postgresql_audit_adapter)


async def test_postgresql_audit_reader_isolates_workspaces(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_isolation(postgresql_audit_adapter)


async def test_postgresql_required_audit_failure_never_releases_effect(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_required_failure(postgresql_audit_adapter)


async def test_postgresql_concurrent_duplicate_keeps_one_logical_event(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_concurrent_replay(postgresql_audit_adapter)


async def test_postgresql_distinct_retry_attempts_create_distinct_events(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_retry_attempts(postgresql_audit_adapter)


async def test_postgresql_exit_without_commit_discards_audit_event(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_implicit_rollback(postgresql_audit_adapter)


async def test_postgresql_denied_required_audit_never_releases_effect(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_denied(postgresql_audit_adapter)


async def test_postgresql_denied_audit_failure_never_releases_effect(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_denied_failure(postgresql_audit_adapter)


async def test_postgresql_rejected_audit_never_releases_effect(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_rejected(postgresql_audit_adapter)


async def test_postgresql_allowed_audit_does_not_leak_target_metadata(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_allowed_safe(postgresql_audit_adapter)


async def test_postgresql_historical_audit_cannot_authorize_new_work(
    postgresql_audit_adapter: AuditPersistenceAdapter,
) -> None:
    await contract_not_authority(postgresql_audit_adapter)


async def _persist_workspace_audit(
    adapter: AuditPersistenceAdapter,
    *,
    workspace_id: UUID,
    audit_event_id: UUID,
    slug: str,
) -> TrustedPersistenceContext:
    context = adapter.workspace_context(workspace_id)
    event = adapter.audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.inspect"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.COMPLETED,
        reason="inspection_completed",
    )
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                display_name="Audit Security Contract",
            )
        )
        await uow.audit.append(event)
        await uow.commit()
    return context


async def _bind_workspace(connection: object, workspace_id: UUID) -> None:
    await connection.execute(
        text(
            "SELECT set_config('spine.workspace_id', :workspace_id, true), "
            "set_config('spine.environment_id', '', true)"
        ),
        {"workspace_id": str(workspace_id)},
    )


async def test_audit_catalog_has_named_append_only_tenant_contract(
    audit_database: AuditDatabase,
) -> None:
    async with audit_database.migration_engine.connect() as connection:
        table = (
            await connection.execute(
                text(
                    "SELECT relation.relrowsecurity, relation.relforcerowsecurity, "
                    "obj_description(relation.oid, 'pg_class') AS comment "
                    "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' "
                    "AND relation.relname = 'audit_events'"
                )
            )
        ).mappings().one()
        constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'spine.audit_events'::regclass"
                    )
                )
            ).scalars()
        )
        indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'spine' AND tablename = 'audit_events'"
                    )
                )
            ).scalars()
        )
        policies = {
            row.polname: row.polcmd
            for row in (
                await connection.execute(
                    text(
                        "SELECT policy.polname, policy.polcmd FROM pg_policy AS policy "
                        "WHERE policy.polrelid = 'spine.audit_events'::regclass"
                    )
                )
            ).mappings()
        }
        privileges = (
            await connection.execute(
                text(
                    "SELECT "
                    "has_table_privilege(:role, 'spine.audit_events', 'SELECT'), "
                    "has_table_privilege(:role, 'spine.audit_events', 'INSERT'), "
                    "has_table_privilege(:role, 'spine.audit_events', 'UPDATE'), "
                    "has_table_privilege(:role, 'spine.audit_events', 'DELETE')"
                ),
                {"role": RUNTIME_ROLE},
            )
        ).one()
        triggers = set(
            (
                await connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgrelid = 'spine.audit_events'::regclass "
                        "AND NOT tgisinternal"
                    )
                )
            ).scalars()
        )
        public_can_execute = await connection.scalar(
            text(
                "SELECT has_function_privilege("
                "'public', 'spine.reject_audit_event_mutation()', 'EXECUTE')"
            )
        )
        revision = await connection.scalar(
            text("SELECT version_num FROM spine.alembic_version")
        )

    assert (table.relrowsecurity, table.relforcerowsecurity) == (True, True)
    assert table.comment == (
        "classification=tenant_audit; runtime_table_access=append_scoped_read"
    )
    assert constraints == {
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
    assert indexes == {
        "ix_audit_events_tenant_appended_at",
        "ix_audit_events_workspace_target",
        "ix_audit_events_workspace_trace_id",
        "pk_audit_events",
        "uq_audit_events_environment_producer",
        "uq_audit_events_workspace_producer",
    }
    assert policies == {
        "pol_audit_events_migration_maintenance": "*",
        "pol_audit_events_tenant_append": "a",
        "pol_audit_events_tenant_read": "r",
    }
    assert tuple(privileges) == (True, True, False, False)
    assert triggers == {"trg_audit_events_immutable"}
    assert public_can_execute is False
    assert revision == "20261010_08"


async def test_runtime_cannot_update_or_delete_audit_events(
    postgresql_audit_adapter: AuditPersistenceAdapter,
    audit_database: AuditDatabase,
) -> None:
    workspace_id = synthetic_uuid(2700)
    audit_event_id = synthetic_uuid(2701)
    await _persist_workspace_audit(
        postgresql_audit_adapter,
        workspace_id=workspace_id,
        audit_event_id=audit_event_id,
        slug="audit-runtime-immutable",
    )

    for statement in (
        "UPDATE spine.audit_events SET reason = 'changed' "
        "WHERE audit_event_id = :audit_event_id",
        "DELETE FROM spine.audit_events WHERE audit_event_id = :audit_event_id",
    ):
        with pytest.raises(ProgrammingError):
            async with audit_database.runtime_engine.begin() as connection:
                await _bind_workspace(connection, workspace_id)
                await connection.execute(
                    text(statement),
                    {"audit_event_id": audit_event_id},
                )

    for statement in (
        "UPDATE spine.audit_events SET reason = 'changed' "
        "WHERE audit_event_id = :audit_event_id",
        "DELETE FROM spine.audit_events WHERE audit_event_id = :audit_event_id",
    ):
        with pytest.raises(IntegrityError) as captured:
            async with audit_database.migration_engine.begin() as connection:
                await connection.execute(
                    text(statement),
                    {"audit_event_id": audit_event_id},
                )
        assert captured.value.orig.diag.constraint_name == "ck_audit_events_immutable"


async def test_runtime_rls_denies_cross_workspace_audit_read_and_insert(
    postgresql_audit_adapter: AuditPersistenceAdapter,
    audit_database: AuditDatabase,
) -> None:
    workspace_a = synthetic_uuid(2710)
    workspace_b = synthetic_uuid(2711)
    event_a = synthetic_uuid(2712)
    event_b = synthetic_uuid(2713)
    context_a = await _persist_workspace_audit(
        postgresql_audit_adapter,
        workspace_id=workspace_a,
        audit_event_id=event_a,
        slug="audit-rls-a",
    )
    await _persist_workspace_audit(
        postgresql_audit_adapter,
        workspace_id=workspace_b,
        audit_event_id=event_b,
        slug="audit-rls-b",
    )

    async with audit_database.runtime_engine.begin() as connection:
        await _bind_workspace(connection, workspace_a)
        visible = set(
            (
                await connection.execute(
                    text(
                        "SELECT audit_event_id FROM spine.audit_events "
                        "WHERE audit_event_id IN (:event_a, :event_b)"
                    ),
                    {"event_a": event_a, "event_b": event_b},
                )
            ).scalars()
        )
    assert visible == {event_a}

    with pytest.raises(ProgrammingError) as captured:
        async with audit_database.runtime_engine.begin() as connection:
            await _bind_workspace(connection, workspace_a)
            await connection.execute(
                text(
                    "INSERT INTO spine.audit_events "
                    "(audit_event_id, workspace_id, event_type, schema_version, "
                    "origin, acting_subject_id, trace_id, occurred_at, outcome, "
                    "reason, payload) VALUES "
                    "(:audit_event_id, :workspace_id, 'command.outcome', 1, "
                    "'interactive', :acting_subject_id, :trace_id, CURRENT_TIMESTAMP, "
                    "'completed', 'inspection_completed', "
                    "'{\"command_type\":\"workspace.inspect\"}'::jsonb)"
                ),
                {
                    "audit_event_id": synthetic_uuid(2714),
                    "workspace_id": workspace_b,
                    "acting_subject_id": context_a.acting_subject_id,
                    "trace_id": context_a.trace_id,
                },
            )
    assert str(workspace_b) not in str(captured.value)


@pytest.mark.parametrize(
    "stage",
    (
        "after_mutation",
        "after_audit_append",
        "after_outbox_append",
        "after_flush",
        "pre_commit",
    ),
)
async def test_mutation_audit_and_outbox_roll_back_together_at_every_boundary(
    audit_database: AuditDatabase,
    stage: str,
) -> None:
    offset = {
        "after_mutation": 0,
        "after_audit_append": 10,
        "after_outbox_append": 20,
        "after_flush": 30,
        "pre_commit": 40,
    }[stage]
    workspace_id = synthetic_uuid(2750 + offset)
    audit_event_id = synthetic_uuid(2751 + offset)
    outbox_event_id = synthetic_uuid(2752 + offset)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2753 + offset),
        secret=f"issue-64-atomic-{stage}-contract".encode(),
    )
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(2754 + offset),
        purpose=PersistencePurpose("audit_atomicity_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=synthetic_uuid(2755 + offset),
    )
    outbox_events = create_outbox_event_registry()
    audit_events = AuditEventRegistry.with_default_families()
    base_sessions = async_sessionmaker(
        audit_database.runtime_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    persistence = PostgreSQLPersistence(
        session_factory=base_sessions,
        context_verifier=boundary,
        outbox_events=outbox_events,
        audit_events=audit_events,
        transaction_probe=AtomicFailureProbe(stage),
    )
    workspace = Workspace(
        id=workspace_id,
        slug=f"audit-atomic-{stage}",
        display_name="Audit Atomicity",
    )
    audit_event = audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.create"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.COMPLETED,
        reason="workspace_created",
    )
    outbox_intent = outbox_events.build_intent(
        event_id=outbox_event_id,
        workspace_id=workspace_id,
        event_type="workspace.created",
        schema_version=1,
        payload=WorkspaceCreatedPayload(
            workspace=OpaqueObjectReference(
                object_type="workspace",
                object_id=workspace_id,
                schema_version=1,
            ),
            lifecycle_state="active",
        ),
        trace_id=context.trace_id,
    )
    expected_error = (
        PersistenceUnavailableError
        if stage in {"after_flush", "pre_commit"}
        else RuntimeError
    )

    with pytest.raises(expected_error):
        async with persistence.uow_factory(context) as uow:
            await uow.workspaces.add(workspace)
            if stage == "after_mutation":
                raise RuntimeError("synthetic failure after mutation")
            await uow.audit.append(audit_event)
            if stage == "after_audit_append":
                raise RuntimeError("synthetic failure after audit append")
            await uow.outbox.append(outbox_intent)
            if stage == "after_outbox_append":
                raise RuntimeError("synthetic failure after outbox append")
            await uow.commit()

    async with audit_database.migration_engine.connect() as connection:
        persisted = (
            await connection.execute(
                text(
                    "SELECT "
                    "EXISTS(SELECT 1 FROM spine.workspaces WHERE id = :workspace_id), "
                    "EXISTS(SELECT 1 FROM spine.audit_events "
                    "WHERE audit_event_id = :audit_event_id), "
                    "EXISTS(SELECT 1 FROM spine.outbox_intents "
                    "WHERE event_id = :outbox_event_id)"
                ),
                {
                    "workspace_id": workspace_id,
                    "audit_event_id": audit_event_id,
                    "outbox_event_id": outbox_event_id,
                },
            )
        ).one()
    assert tuple(persisted) == (False, False, False)


async def test_committed_response_loss_replays_original_audit_identity(
    postgresql_audit_adapter: AuditPersistenceAdapter,
    audit_database: AuditDatabase,
) -> None:
    class LostResponseError(RuntimeError):
        pass

    workspace_id = synthetic_uuid(2820)
    original_event_id = synthetic_uuid(2821)
    retry_event_id = synthetic_uuid(2822)
    context = postgresql_audit_adapter.workspace_context(workspace_id)
    async with postgresql_audit_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="audit-lost-response",
                display_name="Audit Lost Response",
            )
        )
        await setup.commit()

    def event(audit_event_id: UUID):
        return postgresql_audit_adapter.audit_events.build_event(
            audit_event_id=audit_event_id,
            workspace_id=workspace_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="workspace.inspect"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.COMPLETED,
            reason="inspection_completed",
            producer_deduplication_id="workspace.inspect:lost-response-2820",
        )

    with pytest.raises(LostResponseError):
        async with postgresql_audit_adapter.uow_factory(context) as first:
            assert await first.audit.append(event(original_event_id)) == original_event_id
            await first.commit()
            raise LostResponseError("response lost after commit")

    async with postgresql_audit_adapter.uow_factory(context) as retry:
        assert await retry.audit.append(event(retry_event_id)) == original_event_id
        await retry.commit()

    async with audit_database.migration_engine.connect() as connection:
        count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.audit_events "
                "WHERE workspace_id = :workspace_id "
                "AND producer_deduplication_id = "
                "'workspace.inspect:lost-response-2820'"
            ),
            {"workspace_id": workspace_id},
        )
    assert count == 1
