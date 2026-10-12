from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    ConstraintConflictError,
    PersistenceUnavailableError,
    UnexpectedPersistenceError,
)
from spine.application.persistence.ingestion import IngestionRunCommand
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OwnedIdempotencyClaim,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.access_policies import access_policy_document_hash
from spine.domain.common import EnvironmentKind
from spine.domain.ingestion import (
    IngestionRun,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    SafeDiagnosticDetail,
)
from spine.domain.sources.access_policy import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    PolicyBenefitClassification,
    PolicyOriginKind,
    PolicyProposal,
    PrincipalKind,
)
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from tests.contracts.persistence.adapter import PersistenceAdapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.outbox_events import (
    WorkspaceCreatedPayload,
    create_outbox_event_registry,
)
from tests.contracts.persistence.test_outbox_writer_contract import (
    test_canonical_mutation_and_intent_become_visible_only_after_commit as contract_outbox_commit,
    test_duplicate_producer_identity_has_stable_conflict as contract_outbox_producer_conflict,
    test_environment_intent_requires_matching_environment_scope as contract_outbox_environment_scope,
    test_intent_scope_must_exactly_match_unit_of_work_scope as contract_outbox_scope,
    test_missing_event_identity_is_generated_and_returned as contract_outbox_generated_identity,
    test_rollback_discards_canonical_mutation_and_intent as contract_outbox_rollback,
    test_trace_mismatch_is_terminal_and_discards_pending_mutation as contract_outbox_trace_scope,
    workspace as outbox_workspace,
)
from tests.contracts.persistence.test_source_observation_contract import (
    prepare as prepare_source_scope,
    provenance as source_contract_provenance,
    revision as source_contract_revision,
    source as source_contract_source,
    test_changed_revision_metadata_creates_new_immutable_revision as contract_source_metadata_change,
    test_source_identity_rejects_mixed_connector_and_upload_identity as contract_source_identity_validation,
    test_same_observation_key_with_changed_digest_conflicts as contract_source_observation_digest_conflict,
    test_observation_rollback_removes_source_revision_and_provenance as contract_source_observation_rollback,
    test_source_identity_modes_remain_distinct as contract_source_identity_modes,
    test_source_observation_replay_is_stable as contract_source_observation_replay,
    test_source_observation_reuses_revision_for_new_provenance as contract_source_observation_revision_reuse,
    test_content_reappearance_requires_tombstone_predecessor as contract_source_content_reappearance_rejected,
    test_tombstone_reappearance_requires_tombstone_predecessor as contract_source_reappearance,
)
from tests.contracts.persistence.test_ingestion_repository_contract import (
    exercise_ingestion_run_item_contract,
    exercise_ingestion_binding_race,
    test_ingestion_binding_requires_a_scoped_original_receipt as contract_ingestion_binding_receipt,
)
from tests.contracts.persistence.test_idempotency_repository_contract import (
    OPERATION_SCHEMA_VERSION,
    command_digest as idempotency_command_digest,
    persist_environment as persist_idempotency_environment,
    persist_workspace as persist_idempotency_workspace,
    result_ref as idempotency_result_ref,
    test_claim_complete_and_replay_return_same_result_reference as contract_idempotency_replay,
    test_completing_replay_unknown_or_already_completed_claim_fails as contract_idempotency_completion_conflict,
    test_differently_scoped_claim_cannot_complete as contract_idempotency_scope,
    test_incomplete_owned_claim_prevents_commit_and_rolls_back as contract_idempotency_incomplete,
    test_independent_workspaces_environments_and_keys_do_not_collide as contract_idempotency_independent_scopes,
    test_rollback_discards_claim_and_completed_receipt as contract_idempotency_rollback,
    test_same_key_with_different_digest_raises_stable_conflict as contract_idempotency_digest_conflict,
)
from tests.contracts.persistence.test_environment_repository_contract import (
    test_duplicate_environment_identity_raises_typed_conflict as contract_duplicate_environment,
    test_environment_cannot_be_added_for_another_workspace as contract_environment_scope,
    test_environment_commit_makes_it_visible_within_owning_workspace as contract_environment_commit,
    test_foreign_environment_is_not_disclosed as contract_foreign_environment,
    test_mismatched_environment_scope_fails_before_repository_access as contract_mismatched_environment,
)
from tests.contracts.persistence.test_unit_of_work_contract import (
    test_concurrent_duplicate_commit_has_one_logical_effect as contract_concurrent_duplicate,
    test_duplicate_workspace_identity_raises_typed_conflict as contract_duplicate_workspace,
    test_explicit_commit_makes_workspace_visible as contract_explicit_commit,
    test_explicit_rollback_is_idempotent_before_close as contract_explicit_rollback,
    test_exit_without_successful_commit_discards_workspace as contract_implicit_rollback,
    test_independent_uows_do_not_observe_uncommitted_mutations as contract_independent_uows,
    test_missing_workspace_is_normal_result_and_uow_remains_usable as contract_missing_workspace,
    test_repository_failure_is_terminal_and_discards_pending_mutations as contract_repository_failure,
    test_cancelled_commit_is_terminal_and_discards_pending_mutation as contract_cancelled_commit,
    test_cancelled_entry_is_terminal as contract_cancelled_entry,
    test_uow_concurrent_entry_fails_deterministically as contract_concurrent_entry,
    test_uow_concurrent_use_fails_deterministically as contract_concurrent_use,
    test_uow_post_close_access_fails_deterministically as contract_post_close,
    test_uow_reentry_fails_deterministically as contract_reentry,
    test_worker_and_interactive_contexts_have_same_workspace_isolation as contract_worker_parity,
)


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.asyncio(loop_scope="session"),
]

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
DISPATCHER_PASSWORD = "dispatcher-test-secret"


@dataclass(frozen=True, slots=True)
class PostgreSQLContractDatabase:
    migration_url: SecretStr
    runtime_url: SecretStr


@dataclass(frozen=True, slots=True)
class OneConnectionHarness:
    adapter: PersistenceAdapter
    engine: AsyncEngine
    migration_url: SecretStr
    workspace_statement_started: asyncio.Event


class EnvironmentCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: OpaqueObjectReference
    lifecycle_state: str


class TransactionGate:
    def __init__(self) -> None:
        self._blocking = False
        self._release = asyncio.Event()
        self._release.set()

    async def wait(self) -> None:
        if self._blocking:
            await self._release.wait()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        self._blocking = True
        self._release.clear()
        try:
            yield
        finally:
            self._blocking = False
            self._release.set()


class TransactionControlledSession:
    """Delegate real SQL while allowing contract tests to pause lifecycle awaits."""

    def __init__(self, session: AsyncSession, gate: TransactionGate) -> None:
        self._session = session
        self._gate = gate

    async def begin(self) -> object:
        await self._gate.wait()
        return await self._session.begin()

    async def execute(self, statement: object, parameters: object | None = None) -> object:
        return await self._session.execute(statement, parameters)

    async def commit(self) -> None:
        await self._gate.wait()
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def close(self) -> None:
        await self._session.close()


class FailureInjectingSession:
    """Run real PostgreSQL statements, then fail at one transaction boundary."""

    def __init__(self, session: AsyncSession, stage: str) -> None:
        self._session = session
        self._stage = stage

    async def begin(self) -> object:
        return await self._session.begin()

    async def execute(self, statement: object, parameters: object | None = None) -> object:
        result = await self._session.execute(statement, parameters)
        table = getattr(statement, "table", None)
        if self._stage == "during_flush" and getattr(table, "name", None) == "outbox_intents":
            raise OperationalError(
                "INSERT INTO spine.outbox_intents",
                {},
                RuntimeError("synthetic flush failure"),
            )
        return result

    async def commit(self) -> None:
        if self._stage == "during_commit":
            await self._session.execute(
                text(
                    "CREATE TEMPORARY TABLE commit_failure_parent "
                    "(id integer PRIMARY KEY) ON COMMIT DROP"
                )
            )
            await self._session.execute(
                text(
                    "CREATE TEMPORARY TABLE commit_failure_child "
                    "(parent_id integer, CONSTRAINT fk_commit_failure_probe "
                    "FOREIGN KEY (parent_id) REFERENCES commit_failure_parent(id) "
                    "DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
                )
            )
            await self._session.execute(
                text("INSERT INTO commit_failure_child (parent_id) VALUES (1)")
            )
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def close(self) -> None:
        await self._session.close()


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def postgresql_contract_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[PostgreSQLContractDatabase]:
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
    migration_url = _role_url(
        postgresql_provision,
        MIGRATION_ROLE,
        MIGRATION_PASSWORD,
    )
    migration = MigrationDatabaseSettings(
        url=migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    await asyncio.to_thread(upgrade_database, migration)
    database = PostgreSQLContractDatabase(
        migration_url=migration_url,
        runtime_url=_role_url(
            postgresql_provision,
            RUNTIME_ROLE,
            RUNTIME_PASSWORD,
        ),
    )
    yield database

    engine = create_async_engine(migration_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE spine.initial_workspace_bootstrap, "
                    "spine.environments, spine.workspaces CASCADE"
                )
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def postgresql_adapter(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> AsyncIterator[PersistenceAdapter]:
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_size=5,
        max_overflow=0,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    transaction_gate = TransactionGate()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(950),
        secret=b"issue-45-postgresql-contract-secret",
    )
    outbox_events = create_outbox_event_registry()
    persistence = PostgreSQLPersistence(
        session_factory=lambda: TransactionControlledSession(
            session_factory(), transaction_gate
        ),  # type: ignore[arg-type]
        context_verifier=boundary,
        outbox_events=outbox_events,
    )

    def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(951),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(952),
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
            service_principal_id=synthetic_uuid(953),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(954),
        )

    def source_context(workspace_id: UUID, environment_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(957),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=synthetic_uuid(958),
        )

    def ingestion_context(
        workspace_id: UUID, environment_id: UUID, operation: str
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id, environment_id=environment_id
            ),
            service_principal_id=synthetic_uuid(959),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation(operation),
            trace_id=synthetic_uuid(960),
        )

    def worker_workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(955),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(956),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        async with transaction_gate.hold():
            yield

    try:
        yield PersistenceAdapter(
            uow_factory=persistence.uow_factory,
            workspace_context=workspace_context,
            worker_workspace_context=worker_workspace_context,
            environment_context=environment_context,
            hold_transactions=hold_transactions,
            outbox_events=outbox_events,
            source_context=source_context,
            ingestion_context=ingestion_context,
        )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def one_connection_harness(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> AsyncIterator[OneConnectionHarness]:
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(960),
        secret=b"issue-45-one-connection-secret-001",
    )
    outbox_events = create_outbox_event_registry()
    persistence = PostgreSQLPersistence(
        session_factory=async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        ),
        context_verifier=boundary,
        outbox_events=outbox_events,
    )
    workspace_statement_started = asyncio.Event()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def observe_workspace_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "FROM spine.workspaces" in statement:
            workspace_statement_started.set()

    def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(961),
            purpose=PersistencePurpose("pool_leakage_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(962),
        )

    def worker_workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(965),
            purpose=PersistencePurpose("pool_leakage_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(966),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        yield

    adapter = PersistenceAdapter(
        uow_factory=persistence.uow_factory,
        workspace_context=workspace_context,
        worker_workspace_context=worker_workspace_context,
        environment_context=lambda workspace_id, environment_id: boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=synthetic_uuid(963),
            purpose=PersistencePurpose("pool_leakage_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(964),
        ),
        hold_transactions=hold_transactions,
        outbox_events=outbox_events,
    )
    try:
        yield OneConnectionHarness(
            adapter=adapter,
            engine=engine,
            migration_url=postgresql_contract_database.migration_url,
            workspace_statement_started=workspace_statement_started,
        )
    finally:
        await engine.dispose()


ContractTest = Callable[[PersistenceAdapter], Awaitable[None]]


@pytest.mark.parametrize(
    "contract",
    (
        contract_explicit_commit,
        contract_missing_workspace,
        contract_worker_parity,
        contract_independent_uows,
        contract_concurrent_duplicate,
        contract_explicit_rollback,
        contract_repository_failure,
        contract_duplicate_workspace,
        contract_reentry,
        contract_concurrent_entry,
        contract_concurrent_use,
        contract_post_close,
        contract_cancelled_entry,
        contract_cancelled_commit,
        contract_environment_commit,
        contract_environment_scope,
        contract_foreign_environment,
        contract_duplicate_environment,
        contract_mismatched_environment,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_shared_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


@pytest.mark.parametrize(
    "contract",
    (
        contract_idempotency_replay,
        contract_idempotency_digest_conflict,
        contract_idempotency_independent_scopes,
        contract_idempotency_incomplete,
        contract_idempotency_rollback,
        contract_idempotency_completion_conflict,
        contract_idempotency_scope,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_idempotency_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


@pytest.mark.parametrize(
    "contract",
    (
        contract_outbox_commit,
        contract_outbox_rollback,
        contract_outbox_scope,
        contract_outbox_environment_scope,
        contract_outbox_producer_conflict,
        contract_outbox_generated_identity,
        contract_outbox_trace_scope,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_outbox_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


@pytest.mark.parametrize(
    "contract",
    (
        contract_source_observation_replay,
        contract_source_observation_revision_reuse,
        contract_source_observation_digest_conflict,
        contract_source_identity_modes,
        contract_source_identity_validation,
        contract_source_metadata_change,
        contract_source_reappearance,
        contract_source_content_reappearance_rejected,
        contract_source_observation_rollback,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_source_observation_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


async def test_postgresql_adapter_satisfies_ingestion_repository_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await exercise_ingestion_run_item_contract(postgresql_adapter)
    await contract_ingestion_binding_receipt(postgresql_adapter)
    await exercise_ingestion_binding_race(postgresql_adapter)


async def test_access_policy_repository_round_trips_proposal_and_complete_policy(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    assert postgresql_adapter.source_context is not None
    workspace_id, environment_id = synthetic_uuid(3800), synthetic_uuid(3801)
    await prepare_source_scope(postgresql_adapter, workspace_id, environment_id)
    source_object = source_contract_source(workspace_id, environment_id)
    source_revision = source_contract_revision(source_object, synthetic_uuid(3802))
    command = SourceObservationCommand.create(
        source=source_object,
        revision=source_revision,
        provenance=source_contract_provenance(
            source_object, source_revision.revision_id, "event-1"
        ),
        idempotency_key=IdempotencyKey("policy-repository-source"),
    )
    context = postgresql_adapter.source_context(workspace_id, environment_id)
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.sources.record_observation(command)
        await uow.commit()

    policy = AccessPolicyVersion(
        access_policy_version_id=synthetic_uuid(3803),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object.source_object_id,
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
        origin_reference=synthetic_uuid(3804),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=synthetic_uuid(3805),
        created_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        grants=(
            AccessGrant(
                access_grant_id=synthetic_uuid(3806),
                principal_kind=PrincipalKind.HUMAN,
                principal_id=synthetic_uuid(3807),
                permission=AccessPermission.READ_CONTENT,
            ),
        ),
    )
    policy = policy.model_copy(
        update={"document_hash": access_policy_document_hash(policy)}
    )
    proposal = PolicyProposal(
        policy_proposal_id=synthetic_uuid(3808),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object.source_object_id,
        proposal_version=1,
        proposed_policy_version_id=policy.access_policy_version_id,
        proposed_policy_hash=policy.document_hash,
        requester_canonical_human_id=synthetic_uuid(3805),
        expected_policy_version_id=None,
        expected_policy_generation=0,
        benefit_classification=PolicyBenefitClassification.OTHER_ONLY,
        benefit_schema_version="policy-benefit.v1",
        membership_generation=1,
        authority_generation=1,
        service_authority_generation=1,
        submitted_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )

    async with postgresql_adapter.uow_factory(context) as uow:
        persisted = await uow.access_policies.persist_proposal(policy, proposal)
        await uow.commit()
    async with postgresql_adapter.uow_factory(context) as uow:
        resolved_policy = await uow.access_policies.resolve_policy(
            policy.access_policy_version_id
        )
        resolved_proposal = await uow.access_policies.resolve_proposal(
            proposal.policy_proposal_id
        )
        await uow.rollback()

    assert persisted == proposal
    assert resolved_policy == policy
    assert resolved_proposal == proposal


@pytest.mark.parametrize(
    ("case_id", "safe_metrics", "message"),
    (
        (0, '{"schema_version":"ingestion-safe-detail:v1","secret":"x"}', "Safe metrics field is not allowlisted"),
        (1, '{"schema_version":"other"}', "Safe metrics schema is invalid"),
        (2, '[]', "Safe metrics schema is invalid"),
        (3, '{"schema_version":"ingestion-safe-detail:v1","attempt":0}', "Safe metrics attempt is invalid"),
        (4, '{"schema_version":"ingestion-safe-detail:v1","attempt":1.5}', "Safe metrics attempt is invalid"),
        (5, '{"schema_version":"ingestion-safe-detail:v1","attempt":true}', "Safe metrics attempt is invalid"),
        (6, '{"schema_version":"ingestion-safe-detail:v1","item_count":10000000000}', "Safe metrics counter is invalid"),
        (7, '{"schema_version":"ingestion-safe-detail:v1","byte_count":-1}', "Safe metrics counter is invalid"),
        (8, '{"schema_version":"ingestion-safe-detail:v1","byte_count":1.5}', "Safe metrics counter is invalid"),
        (9, '{"schema_version":"ingestion-safe-detail:v1","duration_ms":null}', "Safe metrics counter is invalid"),
        (10, '{"schema_version":"ingestion-safe-detail:v1","retryable":"yes"}', "Safe metrics boolean is invalid"),
        (11, '{"schema_version":"ingestion-safe-detail:v1","retryable":null}', "Safe metrics boolean is invalid"),
        (12, '{"schema_version":"ingestion-safe-detail:v1","opaque_detail_ref":"00000000-0000-0000-0000-000000000000"}', "Safe metrics reference is invalid"),
        (13, '{"schema_version":"ingestion-safe-detail:v1","opaque_detail_ref":"not-a-uuid"}', "Safe metrics reference is invalid"),
    ),
)
async def test_postgresql_rejects_invalid_ingestion_safe_metrics(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    case_id: int,
    safe_metrics: str,
    message: str,
) -> None:
    base_id = 3600 + case_id * 10
    workspace_id = synthetic_uuid(base_id)
    environment_id = synthetic_uuid(base_id + 1)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    await persist_idempotency_environment(postgresql_adapter, workspace_id, environment_id)
    assert postgresql_adapter.ingestion_context is not None
    context = postgresql_adapter.ingestion_context(
        workspace_id, environment_id, "ingestion_run"
    )
    run = IngestionRun(
        ingestion_run_id=synthetic_uuid(base_id + 2),
        workspace_id=workspace_id,
        environment_id=environment_id,
        command_identity="safe-metrics-test-run",
        command_digest="a" * 64,
        trigger_kind="upload",
        requested_scope_schema="ingestion-scope:v1",
        requested_scope_digest="b" * 64,
        requested_at=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
    )
    command = IngestionRunCommand.create(
        run=run, idempotency_key=IdempotencyKey("safe-metrics-test-run")
    )
    root_id = uuid5(
        NAMESPACE_URL, f"ingestion-run-root:{command.run.ingestion_run_id}"
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.ingestion.create_run(command)
        await uow.ingestion.append_transition(
            IngestionTransition(
                ingestion_transition_id=synthetic_uuid(base_id + 5),
                workspace_id=workspace_id,
                environment_id=environment_id,
                target_kind=IngestionTargetKind.RUN,
                ingestion_run_id=command.run.ingestion_run_id,
                predecessor_transition_id=root_id,
                state=IngestionRunState.RECEIVING,
                attempt=1,
                safe_metrics=SafeDiagnosticDetail(
                    schema_version="ingestion-safe-detail:v1", attempt=1
                ),
                actor_or_service="safe-metrics-test",
                trace_id=synthetic_uuid(base_id + 6),
                occurred_at=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
            )
        )
        await uow.commit()

    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    transition_id = synthetic_uuid(base_id + 3)
    try:
        async with engine.begin() as connection:
            with pytest.raises(IntegrityError, match=message):
                await connection.execute(
                    text(
                        "INSERT INTO spine.ingestion_transitions ("
                        "ingestion_transition_id, workspace_id, environment_id, "
                        "target_kind, ingestion_run_id, ingestion_run_source_id, "
                        "predecessor_transition_id, state, attempt, safe_metrics_schema, "
                        "safe_metrics, actor_or_service, trace_id, occurred_at) "
                        "VALUES (:transition_id, :workspace_id, :environment_id, 'run', "
                        ":run_id, NULL, :predecessor_id, 'receiving', 1, "
                        "'ingestion-safe-detail:v1', CAST(:safe_metrics AS jsonb), "
                        "'safe-metrics-test', :trace_id, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "transition_id": transition_id,
                        "workspace_id": workspace_id,
                        "environment_id": environment_id,
                        "run_id": command.run.ingestion_run_id,
                        "predecessor_id": synthetic_uuid(base_id + 5),
                        "safe_metrics": safe_metrics,
                        "trace_id": synthetic_uuid(base_id + 4),
                    },
                )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("stage", "offset"),
    (
        ("after_mutation", 0),
        ("after_append", 10),
        ("during_flush", 20),
        ("before_commit", 30),
        ("during_commit", 40),
    ),
)
async def test_real_postgresql_failure_rolls_back_mutation_and_outbox_intent(
    postgresql_contract_database: PostgreSQLContractDatabase,
    stage: str,
    offset: int,
) -> None:
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2250 + offset),
        secret=f"issue-47-{stage}-failure-secret".encode(),
    )
    workspace_id = synthetic_uuid(2260 + offset)
    event_id = synthetic_uuid(2261 + offset)
    trace_id = synthetic_uuid(2262 + offset)
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(2263 + offset),
        purpose=PersistencePurpose("outbox_failure_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=trace_id,
    )
    outbox_events = create_outbox_event_registry()
    persistence = PostgreSQLPersistence(
        session_factory=lambda: FailureInjectingSession(session_factory(), stage),  # type: ignore[arg-type]
        context_verifier=boundary,
        outbox_events=outbox_events,
    )
    expected_intent = outbox_events.build_intent(
        event_id=event_id,
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
        producer_deduplication_id=f"failure:{stage}",
        trace_id=trace_id,
    )
    expected_error = {
        "during_flush": PersistenceUnavailableError,
        "during_commit": UnexpectedPersistenceError,
    }.get(stage, RuntimeError)

    try:
        with pytest.raises(expected_error):
            async with persistence.uow_factory(context) as uow:
                await uow.workspaces.add(outbox_workspace(workspace_id))
                if stage == "after_mutation":
                    raise RuntimeError("synthetic failure after mutation")
                await uow.outbox.append(expected_intent)
                if stage == "after_append":
                    raise RuntimeError("synthetic failure after append")
                if stage == "before_commit":
                    await asyncio.sleep(0)
                    raise RuntimeError("synthetic failure before commit")
                await uow.commit()

        migration_engine = create_async_engine(
            postgresql_contract_database.migration_url.get_secret_value(),
            hide_parameters=True,
        )
        try:
            async with migration_engine.connect() as connection:
                persisted = (
                    await connection.execute(
                        text(
                            "SELECT "
                            "EXISTS(SELECT 1 FROM spine.workspaces WHERE id = :workspace_id), "
                            "EXISTS(SELECT 1 FROM spine.outbox_intents WHERE event_id = :event_id)"
                        ),
                        {"workspace_id": workspace_id, "event_id": event_id},
                    )
                ).one()
            assert tuple(persisted) == (False, False)
        finally:
            await migration_engine.dispose()
    finally:
        await engine.dispose()


async def test_concurrent_duplicate_claims_have_one_owner_and_stable_replay(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1730)
    environment_id = synthetic_uuid(1731)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    context = postgresql_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("concurrent-duplicate-key")
    digest = idempotency_command_digest(context.operation)
    expected = idempotency_result_ref(environment_id)
    environment = Environment(
        id=environment_id,
        workspace_id=workspace_id,
        kind=EnvironmentKind.PRODUCTION,
        display_name="Concurrent production",
    )
    start = asyncio.Event()

    async def submit() -> object:
        await start.wait()
        async with postgresql_adapter.uow_factory(context) as uow:
            outcome = await uow.idempotency.claim(
                operation_schema_version=OPERATION_SCHEMA_VERSION,
                key=key,
                digest=digest,
            )
            if isinstance(outcome, OwnedIdempotencyClaim):
                await uow.environments.add(environment)
                await uow.idempotency.complete(outcome, expected)
            await uow.commit()
            return outcome

    first = asyncio.create_task(submit())
    second = asyncio.create_task(submit())
    start.set()
    outcomes = await asyncio.gather(first, second)

    assert sum(isinstance(value, OwnedIdempotencyClaim) for value in outcomes) == 1
    replays = [value for value in outcomes if isinstance(value, IdempotencyReplay)]
    assert len(replays) == 1
    assert replays[0].result == expected

    async with postgresql_adapter.uow_factory(context) as uow:
        assert await uow.environments.resolve(environment_id) == environment
        replay = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(replay, IdempotencyReplay)
        assert replay.result == expected
        await uow.commit()


async def test_receipt_identity_collision_keeps_primary_key_conflict_meaning(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    workspace_id = synthetic_uuid(1732)
    receipt_id = synthetic_uuid(1733)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.idempotency_receipts "
                    "(receipt_id, workspace_id, operation_name, "
                    "operation_schema_version, idempotency_key, "
                    "digest_algorithm_version, command_digest, result_type, "
                    "result_id, result_schema_version) VALUES "
                    "(:receipt_id, :workspace_id, 'workspace_repository', 1, "
                    "'occupied-receipt-id', 'spine.command-digest.v1', :digest, "
                    "'proposal', :result_id, 1)"
                ),
                {
                    "receipt_id": receipt_id,
                    "workspace_id": workspace_id,
                    "digest": "c" * 64,
                    "result_id": synthetic_uuid(1734),
                },
            )
    finally:
        await migration_engine.dispose()

    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(1735),
        secret=b"issue-46-receipt-identity-collision",
    )
    persistence = PostgreSQLPersistence(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        context_verifier=boundary,
        outbox_events=create_outbox_event_registry(),
        receipt_id_factory=lambda: receipt_id,
    )
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(1736),
        purpose=PersistencePurpose("receipt_collision_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=synthetic_uuid(1737),
    )
    try:
        with pytest.raises(
            ConstraintConflictError,
            match="Idempotency receipt identity already exists",
        ):
            async with persistence.uow_factory(context) as uow:
                await uow.idempotency.claim(
                    operation_schema_version=OPERATION_SCHEMA_VERSION,
                    key=IdempotencyKey("different-key-same-receipt-id"),
                    digest=idempotency_command_digest(context.operation),
                )
    finally:
        await engine.dispose()


async def test_waiting_duplicate_becomes_owner_after_first_owner_rolls_back(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1740)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    context = postgresql_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("owner-rollback-key")
    digest = idempotency_command_digest(context.operation)
    expected = idempotency_result_ref(synthetic_uuid(1741))
    owner_claimed = asyncio.Event()
    allow_rollback = asyncio.Event()

    async def fail_owner() -> None:
        async with postgresql_adapter.uow_factory(context) as uow:
            claim = await uow.idempotency.claim(
                operation_schema_version=OPERATION_SCHEMA_VERSION,
                key=key,
                digest=digest,
            )
            assert isinstance(claim, OwnedIdempotencyClaim)
            owner_claimed.set()
            await allow_rollback.wait()
            raise RuntimeError("synthetic owner failure")

    async def retry_after_owner() -> object:
        await owner_claimed.wait()
        async with postgresql_adapter.uow_factory(context) as uow:
            claim = await uow.idempotency.claim(
                operation_schema_version=OPERATION_SCHEMA_VERSION,
                key=key,
                digest=digest,
            )
            assert isinstance(claim, OwnedIdempotencyClaim)
            await uow.idempotency.complete(claim, expected)
            await uow.commit()
            return claim

    failed_owner = asyncio.create_task(fail_owner())
    successor = asyncio.create_task(retry_after_owner())
    await owner_claimed.wait()
    allow_rollback.set()
    failed, succeeded = await asyncio.gather(
        failed_owner,
        successor,
        return_exceptions=True,
    )

    assert isinstance(failed, RuntimeError)
    assert isinstance(succeeded, OwnedIdempotencyClaim)


async def test_receipt_result_and_canonical_mutation_replay_as_one_effect(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1750)
    environment_id = synthetic_uuid(1751)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    context = postgresql_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("canonical-mutation-key")
    digest = idempotency_command_digest(context.operation)
    expected = idempotency_result_ref(environment_id)
    environment = Environment(
        id=environment_id,
        workspace_id=workspace_id,
        kind=EnvironmentKind.PRODUCTION,
        display_name="Production",
    )

    async def execute() -> str:
        async with postgresql_adapter.uow_factory(context) as uow:
            outcome = await uow.idempotency.claim(
                operation_schema_version=OPERATION_SCHEMA_VERSION,
                key=key,
                digest=digest,
            )
            if isinstance(outcome, IdempotencyReplay):
                assert outcome.result == expected
                await uow.commit()
                return "replayed"
            assert isinstance(outcome, OwnedIdempotencyClaim)
            await uow.environments.add(environment)
            await uow.idempotency.complete(outcome, expected)
            await uow.commit()
            return "owned"

    assert await execute() == "owned"
    assert await execute() == "replayed"
    async with postgresql_adapter.uow_factory(context) as uow:
        assert await uow.environments.resolve(environment_id) == environment


async def test_receipt_and_canonical_mutation_roll_back_together(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1760)
    environment_id = synthetic_uuid(1761)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    context = postgresql_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("atomic-rollback-key")
    digest = idempotency_command_digest(context.operation)
    expected = idempotency_result_ref(environment_id)
    environment = Environment(
        id=environment_id,
        workspace_id=workspace_id,
        kind=EnvironmentKind.STAGING,
        display_name="Staging",
    )

    async with postgresql_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.environments.add(environment)
        await uow.idempotency.complete(claim, expected)
        await uow.rollback()

    async with postgresql_adapter.uow_factory(context) as uow:
        assert await uow.environments.resolve(environment_id) is None
        retry = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(retry, OwnedIdempotencyClaim)
        await uow.rollback()


@pytest.mark.parametrize(
    ("stage", "offset"),
    (
        ("after_receipt", 0),
        ("after_mutation", 10),
        ("after_outbox", 20),
        ("during_flush", 30),
        ("before_commit", 40),
        ("during_commit", 50),
    ),
)
async def test_combined_command_failure_leaves_no_receipt_mutation_or_outbox(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    stage: str,
    offset: int,
) -> None:
    workspace_id = synthetic_uuid(2400 + offset)
    environment_id = synthetic_uuid(2401 + offset)
    event_id = synthetic_uuid(2402 + offset)
    trace_id = synthetic_uuid(2403 + offset)
    key = IdempotencyKey(f"combined-failure-{stage}")
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)

    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2404 + offset),
        secret=f"issue-48-{stage}-secret-for-testing-only".encode(),
    )
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(2405 + offset),
        purpose=PersistencePurpose("combined_command_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=trace_id,
    )
    outbox_events = create_outbox_event_registry()
    outbox_events.register(
        event_type="environment.created",
        schema_version=1,
        payload_type=EnvironmentCreatedPayload,
    )
    persistence = PostgreSQLPersistence(
        session_factory=lambda: FailureInjectingSession(session_factory(), stage),  # type: ignore[arg-type]
        context_verifier=boundary,
        outbox_events=outbox_events,
    )
    environment = Environment(
        id=environment_id,
        workspace_id=workspace_id,
        kind=EnvironmentKind.STAGING,
        display_name=f"Failure {stage}",
    )
    expected_result = idempotency_result_ref(environment_id)
    intent = outbox_events.build_intent(
        event_id=event_id,
        workspace_id=workspace_id,
        event_type="environment.created",
        schema_version=1,
        payload=EnvironmentCreatedPayload(
            environment=OpaqueObjectReference(
                object_type="environment",
                object_id=environment_id,
                schema_version=1,
            ),
            lifecycle_state="active",
        ),
        producer_deduplication_id=f"environment:{environment_id}",
        trace_id=trace_id,
    )
    expected_error = {
        "during_flush": PersistenceUnavailableError,
        "during_commit": UnexpectedPersistenceError,
    }.get(stage, RuntimeError)

    try:
        with pytest.raises(expected_error):
            async with persistence.uow_factory(context) as uow:
                claim = await uow.idempotency.claim(
                    operation_schema_version=OPERATION_SCHEMA_VERSION,
                    key=key,
                    digest=idempotency_command_digest(context.operation),
                )
                assert isinstance(claim, OwnedIdempotencyClaim)
                if stage == "after_receipt":
                    raise RuntimeError("synthetic failure after receipt")
                await uow.environments.add(environment)
                if stage == "after_mutation":
                    raise RuntimeError("synthetic failure after mutation")
                await uow.outbox.append(intent)
                if stage == "after_outbox":
                    raise RuntimeError("synthetic failure after outbox")
                await uow.idempotency.complete(claim, expected_result)
                if stage == "before_commit":
                    raise RuntimeError("synthetic failure before commit")
                await uow.commit()

        migration_engine = create_async_engine(
            postgresql_contract_database.migration_url.get_secret_value(),
            hide_parameters=True,
        )
        try:
            async with migration_engine.connect() as connection:
                persisted = (
                    await connection.execute(
                        text(
                            "SELECT "
                            "EXISTS(SELECT 1 FROM spine.idempotency_receipts "
                            "WHERE workspace_id = :workspace_id "
                            "AND idempotency_key = :key), "
                            "EXISTS(SELECT 1 FROM spine.environments "
                            "WHERE id = :environment_id), "
                            "EXISTS(SELECT 1 FROM spine.outbox_intents "
                            "WHERE event_id = :event_id)"
                        ),
                        {
                            "workspace_id": workspace_id,
                            "key": key.value,
                            "environment_id": environment_id,
                            "event_id": event_id,
                        },
                    )
                ).one()
            assert tuple(persisted) == (False, False, False)
        finally:
            await migration_engine.dispose()
    finally:
        await engine.dispose()


async def test_lost_commit_response_replays_one_combined_effect(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    class LostResponseError(RuntimeError):
        pass

    workspace_id = synthetic_uuid(2470)
    environment_id = synthetic_uuid(2471)
    event_id = synthetic_uuid(2472)
    trace_id = synthetic_uuid(2473)
    key = IdempotencyKey("combined-lost-response")
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)

    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2474),
        secret=b"issue-48-lost-response-secret-for-testing-only",
    )
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(2475),
        purpose=PersistencePurpose("combined_command_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=trace_id,
    )
    outbox_events = create_outbox_event_registry()
    outbox_events.register(
        event_type="environment.created",
        schema_version=1,
        payload_type=EnvironmentCreatedPayload,
    )
    persistence = PostgreSQLPersistence(
        session_factory=session_factory,
        context_verifier=boundary,
        outbox_events=outbox_events,
    )
    environment = Environment(
        id=environment_id,
        workspace_id=workspace_id,
        kind=EnvironmentKind.PRODUCTION,
        display_name="Committed once",
    )
    result = idempotency_result_ref(environment_id)
    digest = idempotency_command_digest(context.operation)
    intent = outbox_events.build_intent(
        event_id=event_id,
        workspace_id=workspace_id,
        event_type="environment.created",
        schema_version=1,
        payload=EnvironmentCreatedPayload(
            environment=OpaqueObjectReference(
                object_type="environment",
                object_id=environment_id,
                schema_version=1,
            ),
            lifecycle_state="active",
        ),
        producer_deduplication_id=f"environment:{environment_id}",
        trace_id=trace_id,
    )

    try:
        with pytest.raises(LostResponseError):
            async with persistence.uow_factory(context) as uow:
                claim = await uow.idempotency.claim(
                    operation_schema_version=OPERATION_SCHEMA_VERSION,
                    key=key,
                    digest=digest,
                )
                assert isinstance(claim, OwnedIdempotencyClaim)
                await uow.environments.add(environment)
                await uow.outbox.append(intent)
                await uow.idempotency.complete(claim, result)
                await uow.commit()
                raise LostResponseError("response lost after commit")

        async with persistence.uow_factory(context) as uow:
            replay = await uow.idempotency.claim(
                operation_schema_version=OPERATION_SCHEMA_VERSION,
                key=key,
                digest=digest,
            )
            assert isinstance(replay, IdempotencyReplay)
            assert replay.result == result
            await uow.commit()

        migration_engine = create_async_engine(
            postgresql_contract_database.migration_url.get_secret_value(),
            hide_parameters=True,
        )
        try:
            async with migration_engine.connect() as connection:
                counts = (
                    await connection.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM spine.idempotency_receipts "
                            "WHERE workspace_id = :workspace_id "
                            "AND idempotency_key = :key), "
                            "(SELECT count(*) FROM spine.environments "
                            "WHERE id = :environment_id), "
                            "(SELECT count(*) FROM spine.outbox_intents "
                            "WHERE event_id = :event_id)"
                        ),
                        {
                            "workspace_id": workspace_id,
                            "key": key.value,
                            "environment_id": environment_id,
                            "event_id": event_id,
                        },
                    )
                ).one()
            assert tuple(counts) == (1, 1, 1)
        finally:
            await migration_engine.dispose()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("raise_error", (False, True), ids=("normal", "exception"))
async def test_postgresql_adapter_implicitly_rolls_back(
    postgresql_adapter: PersistenceAdapter,
    raise_error: bool,
) -> None:
    await contract_implicit_rollback(postgresql_adapter, raise_error)


async def test_named_check_constraint_maps_to_stable_conflict(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(720)
    invalid = Workspace(id=workspace_id, slug="", display_name="Invalid")

    with pytest.raises(
        ConstraintConflictError,
        match="Workspace data violates persistence constraints",
    ):
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.workspace_context(workspace_id)
        ) as uow:
            await uow.workspaces.add(invalid)


async def test_named_foreign_key_maps_to_stable_conflict(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(721)
    invalid = Environment(
        id=synthetic_uuid(722),
        workspace_id=workspace_id,
        kind=EnvironmentKind.DEVELOPMENT,
        display_name="Missing owner",
    )

    with pytest.raises(
        ConstraintConflictError,
        match="Owning Workspace does not exist",
    ):
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.workspace_context(workspace_id)
        ) as uow:
            await uow.environments.add(invalid)


async def test_named_unique_constraint_maps_to_stable_conflict(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    first_id = synthetic_uuid(723)
    second_id = synthetic_uuid(724)
    first = Workspace(id=first_id, slug="shared-slug", display_name="First")
    second = Workspace(id=second_id, slug="shared-slug", display_name="Second")

    async with postgresql_adapter.uow_factory(
        postgresql_adapter.workspace_context(first_id)
    ) as uow:
        await uow.workspaces.add(first)
        await uow.commit()

    with pytest.raises(
        ConstraintConflictError,
        match="Workspace identity already exists",
    ):
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.workspace_context(second_id)
        ) as uow:
            await uow.workspaces.add(second)


async def _assert_followup_scope_is_isolated(
    harness: OneConnectionHarness,
    protected_workspace_id: UUID,
) -> None:
    async with harness.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT current_setting('spine.workspace_id', true), "
                    "current_setting('spine.environment_id', true)"
                )
            )
        ).one()
        visible_without_context = (
            await connection.execute(text("SELECT id FROM spine.workspaces"))
        ).scalars().all()
    assert tuple(row) in {(None, None), ("", ""), (None, ""), ("", None)}
    assert visible_without_context == []

    other_workspace_id = synthetic_uuid(protected_workspace_id.int % 1000 + 800)
    other_context = harness.adapter.workspace_context(other_workspace_id)
    async with harness.adapter.uow_factory(other_context) as uow:
        assert await uow.workspaces.resolve(protected_workspace_id) is None


async def test_single_connection_clears_tenant_settings_after_every_terminal_path(
    one_connection_harness: OneConnectionHarness,
) -> None:
    adapter = one_connection_harness.adapter
    committed_id = synthetic_uuid(701)
    rolled_back_id = synthetic_uuid(702)
    failed_id = synthetic_uuid(703)

    async with adapter.uow_factory(adapter.workspace_context(committed_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=committed_id,
                slug="pool-committed",
                display_name="Pool Committed",
            )
        )
        await uow.commit()
    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        committed_id,
    )

    async with adapter.uow_factory(adapter.workspace_context(rolled_back_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=rolled_back_id,
                slug="pool-rolled-back",
                display_name="Pool Rolled Back",
            )
        )
        await uow.rollback()
    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        rolled_back_id,
    )

    async with adapter.uow_factory(adapter.workspace_context(failed_id)) as uow:
        candidate = Workspace(
            id=failed_id,
            slug="pool-failed",
            display_name="Pool Failed",
        )
        await uow.workspaces.add(candidate)
        with pytest.raises(ConstraintConflictError):
            await uow.workspaces.add(candidate.model_copy(deep=True))
    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        failed_id,
    )


async def test_cancelled_statement_releases_single_connection_without_scope_leakage(
    one_connection_harness: OneConnectionHarness,
) -> None:
    adapter = one_connection_harness.adapter
    workspace_id = synthetic_uuid(704)
    entered = asyncio.Event()
    execute_query = asyncio.Event()

    async def blocked_reader() -> None:
        async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
            entered.set()
            await execute_query.wait()
            await uow.workspaces.resolve(workspace_id)

    task = asyncio.create_task(blocked_reader())
    await entered.wait()
    migration_engine = create_async_engine(
        one_connection_harness.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("LOCK TABLE spine.workspaces IN ACCESS EXCLUSIVE MODE")
            )
            one_connection_harness.workspace_statement_started.clear()
            execute_query.set()
            await asyncio.wait_for(
                one_connection_harness.workspace_statement_started.wait(),
                timeout=2,
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await transaction.rollback()
    finally:
        await migration_engine.dispose()

    await _assert_followup_scope_is_isolated(
        one_connection_harness,
        workspace_id,
    )
    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        assert await uow.workspaces.resolve(workspace_id) is None
