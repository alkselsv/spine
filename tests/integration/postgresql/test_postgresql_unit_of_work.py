from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from spine.api.authorization import get_authorized_persistence_context
from spine.api.qa_runs import install_qa_run_routes
from spine.application.qa_run_history import PersistedQARunHistoryService
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.document_lifecycle import (
    CanonicalDocumentLifecycleCommandService,
    ExistingUploadIntent,
    LifecycleOperationStatus,
    ReplaceDocumentCommand,
    ReplacementLifecycleEvidence,
    ServerDocumentLifecycleComposer,
    TombstoneDocumentCommand,
)
from spine.application.source_lifecycle import SourceLifecycleCoordinator
from spine.application.google_drive_lifecycle import (
    DriveLifecycleDecisionState,
    DriveLifecycleSignal,
    DriveRemovalDecision,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.errors import (
    CanonicalEligibilityError,
    ConstraintConflictError,
    ContextBundleConflictError,
    ContextBundleEvidenceUnavailableError,
    ContextBundleNotFoundError,
    IdempotencyConflictError,
    OptimisticConflictError,
    PersistenceUnavailableError,
    UnexpectedPersistenceError,
)
from spine.application.persistence.ingestion import (
    ExistingRevisionBindingCommand,
    IngestionRunCommand,
)
from spine.application.persistence.parsed_artifacts import ParsedArtifactCommand
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OwnedIdempotencyClaim,
)
from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    QA_ASK_ADMISSION_OPERATION,
    QA_ASK_CANCELLATION_EVENT_TYPE,
)
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.boundary import CanonicalBoundaryCaptureCommand
from spine.application.persistence.projection import ProjectionRunCommand
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.application.persistence.workflow_runs import register_workflow_run_outbox_event
from spine.application.persisted_workflow_runs import PersistedWorkflowRunAdmissionService
from spine.application.workflow_runs import AddContextCommandV1, StartWorkflowCommand, WorkflowCommand
from spine.application.persistence.unit_of_work_activity import (
    unit_of_work_is_active,
)
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    PolicyActivationDraft,
    StalePolicyBaselineError,
    access_policy_document_hash,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
    CommandAuditPayload,
)
from spine.domain.common import ActorKind, ActorRef, ContextOrigin, EnvironmentKind
from spine.domain.workflows import ClientRequestV1
from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleEvidenceReference,
    ContextBundleOutcome,
    ContextBundleState,
)
from spine.domain.context_profiles import ContextProfileRef
from spine.domain.ingestion import (
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    SafeDiagnosticDetail,
)
from spine.domain.projections import (
    OntologyVersion,
    ProjectionConfigVersion,
    ProjectionOperation,
    ProjectionPartition,
    ProjectionRun,
)
from spine.domain.sources import (
    AdmissionDecision,
    AdmissionDisposition,
    IdentityMode,
    RegisterAssociationAcceptance,
    RegisterLinkRole,
    RegisterObservedLinkClaim,
    SourceObject,
)
from spine.domain.sources.canonicalization import content_revision
from spine.domain.sources.google import (
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    GoogleProviderKey,
    google_source_identity,
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
from spine.infrastructure.persistence.postgresql import (
    PostgreSQLPersistence,
    PostgreSQLTransactionStage,
)
from spine.infrastructure.persistence.parsed_artifacts import (
    PostgreSQLParsedArtifactRepository,
)
from spine.infrastructure.persistence.postgresql_operational_metrics import (
    PostgreSQLOperationalMetricsStore,
)
from spine.infrastructure.persistence.postgresql_drive_lifecycle import (
    PostgreSQLDriveLifecycleDecisionRepository,
)
from spine.infrastructure.persistence.postgresql_register_associations import (
    PostgreSQLRegisterAssociationRepository,
)
from spine.application.telemetry import OperationalMetricsQuery
from spine.auth import InMemoryAuthorizationDirectory
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttempt,
    AnswerQuestionAttemptEvent,
    AnswerQuestionAttemptEventKind,
    AnswerQuestionAttemptState,
    AnswerQuestionRunState,
    AnswerQuestionUsage,
)
from spine.domain.metrics import AnswerRateFact, MetricWindow, OperationalMetricEvent, OperationalMeasurement
from spine.domain.capabilities import AnswerDisclosureRecord
from spine.domain.qa_sessions import QAMessage, QAMessageState, QASession, QASessionStatus
from tests.contracts.persistence.adapter import PersistenceAdapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.outbox_events import (
    WorkspaceCreatedPayload,
    create_outbox_event_registry,
)
from tests.contracts.persistence.test_outbox_writer_contract import (
    test_canonical_mutation_and_intent_become_visible_only_after_commit as contract_outbox_commit,
    test_changed_projection_digest_conflicts_on_stable_item_identity as contract_outbox_projection_conflict,
    test_duplicate_producer_identity_has_stable_conflict as contract_outbox_producer_conflict,
    test_environment_intent_requires_matching_environment_scope as contract_outbox_environment_scope,
    test_intent_scope_must_exactly_match_unit_of_work_scope as contract_outbox_scope,
    test_missing_event_identity_is_generated_and_returned as contract_outbox_generated_identity,
    test_projection_request_cannot_cross_environment_scope as contract_outbox_projection_scope,
    test_rollback_discards_canonical_mutation_and_intent as contract_outbox_rollback,
    test_trace_mismatch_is_terminal_and_discards_pending_mutation as contract_outbox_trace_scope,
    workspace as outbox_workspace,
)
from tests.api.test_authorization import (
    IDENTITY_ID as API_IDENTITY_ID,
    authorization_entry,
    authorized_app,
)
from tests.contracts.persistence.test_projection_contract import (
    test_projection_post_validation_fault_retains_seal_and_preserves_active as contract_projection_post_validation_fault,
    test_projection_pre_validation_fault_avoids_physical_read_and_preserves_active as contract_projection_pre_validation_fault,
    test_projection_successor_resolver_accepts_canonical_multi_format_boundary as contract_projection_successor_locator_contract,
    test_projection_successor_rollback_advances_the_canonical_pointer as contract_projection_publication,
    test_projection_successor_public_resolver_retains_tombstone as contract_projection_successor_tombstone,
    test_projection_successor_replay_after_activation_returns_original_plan as contract_projection_successor_replay_after_activation,
    test_projection_sealed_successor_accepts_terminal_complete_transition as contract_projection_sealed_successor_terminal_transition,
    test_projection_recovery_resolver_replays_one_shadow_rebuild_plan as contract_projection_recovery_resolver,
    test_projection_recovery_lifecycle_advances_once_through_complete as contract_projection_recovery_lifecycle,
    test_projection_recovery_result_bridge_persists_and_replays_canonical_evidence as contract_projection_recovery_result_bridge,
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
    test_source_queries_are_bounded_ordered_and_tenant_isolated as contract_source_queries,
)
from tests.contracts.persistence.test_canonical_acceptance_contract import (
    _canonical_outbox,
    _original_receipt,
    _receipt_association,
    _activate_policy as activate_canonical_policy,
    prepare_case as prepare_canonical_case,
    test_canonical_happy_path_and_exact_replay as contract_canonical_happy_replay,
    test_canonical_rejects_forged_authoritative_snapshot as contract_canonical_authority,
    test_canonical_rejects_missing_finalized_receipt_association as contract_canonical_receipt,
    test_canonical_rejects_stale_expected_current_without_success_effect as contract_canonical_cas,
    test_canonical_rejects_superseded_admission as contract_canonical_admission,
    test_canonical_rejects_unknown_ordering_scheme as contract_canonical_unknown_ordering,
    test_canonical_replay_requires_current_policy_authority as contract_canonical_replay_authority,
)
from tests.contracts.persistence.test_canonical_boundary_contract import (
    test_boundary_capture_materializes_exact_current_heads_and_replays as contract_canonical_boundary,
)
from tests.contracts.persistence.test_ingestion_repository_contract import (
    exercise_ingestion_run_item_contract,
    exercise_ingestion_binding_race,
    test_ingestion_binding_requires_a_completed_scoped_original_receipt as contract_ingestion_binding_receipt,
    NOW as INGESTION_CONTRACT_NOW,
)
from spine.domain.sources.original_reference import ObjectReference
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
from tests.contracts.persistence.test_context_profile_repository_contract import (
    persist_scope as persist_context_profile_scope,
    profile as context_profile,
    test_context_profile_repository_create_is_idempotent_for_same_version as contract_context_profile_create_replay,
    test_context_profile_repository_does_not_disclose_cross_workspace_profile as contract_context_profile_isolation,
    test_context_profile_repository_fails_closed_for_digest_mismatch as contract_context_profile_digest_mismatch,
    test_context_profile_repository_fails_closed_for_unknown_version as contract_context_profile_unknown_version,
    test_context_profile_repository_preserves_allowed_partial_fallback as contract_context_profile_partial_fallback,
    test_context_profile_repository_rejects_duplicate_immutable_version as contract_context_profile_duplicate,
    test_context_profile_repository_rejects_revoked_profile as contract_context_profile_revoked,
    test_context_profile_repository_replays_same_immutable_version as contract_context_profile_replay,
    test_context_profile_repository_retries_after_uncommitted_create_failure as contract_context_profile_retry,
)
from tests.contracts.persistence.test_answer_question_run_repository_contract import (
    exercise_answer_question_run_completion_and_cancellation_cas,
    _pin as qa_execution_pin,
    persist_scope as persist_qa_scope,
    qa_context,
    run_command as qa_run_command,
    run_record as qa_run_record,
    test_answer_question_attempt_rejects_unresolvable_context_bundle_reference as contract_qa_context_bundle,
    test_answer_question_attempt_concurrent_completion_has_one_terminal_winner as contract_qa_concurrent_completion,
    test_answer_question_partial_failure_retry_and_cancellation_are_replayable as contract_qa_retry_cancel,
    test_answer_question_attempt_terminal_record_is_immutable_and_replayable as contract_qa_attempt_immutable,
    test_answer_question_attempt_allocation_resumes_and_orders_logical_attempts as contract_qa_attempt_allocation,
    test_answer_question_attempt_allocation_rejects_cancellation_winner as contract_qa_attempt_allocation_cancelled,
    test_answer_question_run_conflicting_command_key_fails_closed as contract_qa_conflicting_key,
    test_answer_question_run_create_and_replay_are_canonical_and_idempotent as contract_qa_create_replay,
    test_answer_question_run_resolution_does_not_cross_workspace_or_environment as contract_qa_isolation,
)
from tests.contracts.persistence.test_ask_admission_contract import (
    exercise_ask_admission_contract,
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
from tests.domain.context_bundle.test_context_bundle_models import context_bundle


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


class CanonicalTransactionProbe:
    def __init__(self) -> None:
        self._target: PostgreSQLTransactionStage | None = None
        self._reached: asyncio.Event | None = None
        self._release: asyncio.Event | None = None
        self._failure: BaseException | None = None

    def arm(
        self,
        target: PostgreSQLTransactionStage,
        *,
        failure: BaseException | None = None,
    ) -> None:
        assert self._target is None
        self._target = target
        self._reached = asyncio.Event()
        self._release = asyncio.Event()
        self._failure = failure

    async def __call__(self, stage: PostgreSQLTransactionStage) -> None:
        if stage is not self._target:
            return
        self._target = None
        failure = self._failure
        self._failure = None
        if failure is not None:
            raise failure
        assert self._reached is not None
        assert self._release is not None
        self._reached.set()
        await self._release.wait()

    async def wait_until_reached(self) -> None:
        assert self._reached is not None
        await asyncio.wait_for(self._reached.wait(), timeout=10)

    def release(self) -> None:
        assert self._release is not None
        self._release.set()

    def disarm(self) -> None:
        release = self._release
        self._target = None
        self._reached = None
        self._release = None
        self._failure = None
        if release is not None:
            release.set()


CANONICAL_TRANSACTION_PROBE = CanonicalTransactionProbe()


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
        transaction_probe=CANONICAL_TRANSACTION_PROBE,
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


@pytest.mark.asyncio(loop_scope="session")
async def test_qa_run_collection_returns_empty_page_from_postgresql(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    workspace_id = UUID("53600000-0000-4000-8000-000000000001")
    environment_id = UUID("53600000-0000-4000-8000-000000000002")
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(536),
        secret=b"issue-536-postgresql-context-secret",
    )
    persistence = PostgreSQLPersistence(
        session_factory=async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        ),
        context_verifier=boundary,
        outbox_events=create_outbox_event_registry(),
    )
    workspace_context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=API_IDENTITY_ID,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("qa_run_history"),
        trace_id=synthetic_uuid(5360),
    )
    async with persistence.uow_factory(workspace_context) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="issue-536-workspace",
                display_name="Issue 536 workspace",
            )
        )
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Issue 536 environment",
            )
        )
        await uow.commit()
    context = boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        acting_subject_id=API_IDENTITY_ID,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("qa_run_history"),
        trace_id=synthetic_uuid(5361),
    )
    application = authorized_app(
        [],
        directory=InMemoryAuthorizationDirectory(
            entries=(
                authorization_entry().model_copy(
                    update={
                        "workspace_id": workspace_id,
                        "environment_id": environment_id,
                        "environment_workspace_id": workspace_id,
                    }
                ),
            )
        ),
    )
    service = PersistedQARunHistoryService(persistence.uow_factory)
    install_qa_run_routes(
        application,
        service=service,
        cursor_secret=b"issue-536-run-history-cursor-secret",
    )
    application.dependency_overrides[get_authorized_persistence_context] = (
        lambda: context
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/qa/runs",
                headers={
                    "Authorization": "Bearer opaque-provider-token",
                    "X-Workspace-ID": str(workspace_id),
                    "X-Environment-ID": str(environment_id),
                },
            )
    finally:
        await engine.dispose()

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_register_link_claims_enforce_tenant_rls_and_immutable_runtime_grants(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.connect() as connection:
            for table_name in (
                "register_link_claims",
                "register_association_acceptances",
            ):
                row = (
                    await connection.execute(
                        text(
                            "SELECT relation.relrowsecurity, relation.relforcerowsecurity, "
                            "has_table_privilege('spine_runtime', relation.oid, 'UPDATE') AS can_update, "
                            "has_table_privilege('spine_runtime', relation.oid, 'DELETE') AS can_delete, "
                            "EXISTS (SELECT 1 FROM pg_catalog.pg_trigger trig "
                            "WHERE trig.tgrelid = relation.oid AND NOT trig.tgisinternal "
                            "AND trig.tgname = :trigger_name) AS has_immutable_trigger "
                            "FROM pg_catalog.pg_class relation "
                            "WHERE relation.oid = CAST(:qualified_table AS regclass)"
                        ),
                        {
                            "trigger_name": f"trg_{table_name}_immutable",
                            "qualified_table": f"spine.{table_name}",
                        },
                    )
                ).mappings().one()
                assert row["relrowsecurity"] is True
                assert row["relforcerowsecurity"] is True
                assert row["can_update"] is False
                assert row["can_delete"] is False
                assert row["has_immutable_trigger"] is True
    finally:
        await migration_engine.dispose()

    runtime_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )
    workspace_a = UUID("f5000000-0000-4000-8000-000000000001")
    environment_a = UUID("f6000000-0000-4000-8000-000000000001")
    workspace_b = UUID("f7000000-0000-4000-8000-000000000001")
    environment_b = UUID("f8000000-0000-4000-8000-000000000001")
    try:
        async with runtime_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                    "set_config('spine.environment_id', :environment_id, true)"
                ),
                {
                    "workspace_id": str(workspace_b),
                    "environment_id": str(environment_b),
                },
            )
            with pytest.raises(DBAPIError, match="row-level security policy"):
                await connection.execute(
                    text(
                        "INSERT INTO spine.register_link_claims "
                        "(claim_id, workspace_id, environment_id, row_source_object_id, "
                        "row_source_revision_id, provenance_id, role, record, observed_at) "
                        "VALUES (:claim_id, :workspace_id, :environment_id, :source_id, "
                        ":revision_id, :provenance_id, 'request', '{}'::jsonb, now())"
                    ),
                    {
                        "claim_id": UUID("f9000000-0000-4000-8000-000000000001"),
                        "workspace_id": workspace_a,
                        "environment_id": environment_a,
                        "source_id": UUID("fa000000-0000-4000-8000-000000000001"),
                        "revision_id": UUID("fb000000-0000-4000-8000-000000000001"),
                        "provenance_id": UUID("fc000000-0000-4000-8000-000000000001"),
                    },
                )
            await transaction.rollback()
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                    "set_config('spine.environment_id', :environment_id, true)"
                ),
                {
                    "workspace_id": str(workspace_b),
                    "environment_id": str(environment_b),
                },
            )
            with pytest.raises(DBAPIError, match="row-level security policy"):
                await connection.execute(
                    text(
                        "INSERT INTO spine.register_association_acceptances "
                        "(acceptance_id, claim_id, workspace_id, environment_id, "
                        "row_source_object_id, row_source_revision_id, target_source_object_id, "
                        "target_source_revision_id, role, record, accepted_at) "
                        "VALUES (:acceptance_id, :claim_id, :workspace_id, :environment_id, "
                        ":row_source_id, :row_revision_id, :target_source_id, "
                        ":target_revision_id, 'request', '{}'::jsonb, now())"
                    ),
                    {
                        "acceptance_id": UUID("fd000000-0000-4000-8000-000000000001"),
                        "claim_id": UUID("fe000000-0000-4000-8000-000000000001"),
                        "workspace_id": workspace_a,
                        "environment_id": environment_a,
                        "row_source_id": UUID("ff000000-0000-4000-8000-000000000001"),
                        "row_revision_id": UUID("a1000000-0000-4000-8000-000000000001"),
                        "target_source_id": UUID("a2000000-0000-4000-8000-000000000001"),
                        "target_revision_id": UUID("a3000000-0000-4000-8000-000000000001"),
                    },
                )
            await transaction.rollback()
    finally:
        await runtime_engine.dispose()


@pytest.mark.asyncio(loop_scope="session")
async def test_register_association_repository_accepts_exact_persisted_google_revisions(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    assert postgresql_adapter.ingestion_context is not None
    workspace_id, environment_id = synthetic_uuid(61600), synthetic_uuid(61601)
    connection_id = synthetic_uuid(61602)
    row_key = GoogleRegisterRowKey(
        connection_id=connection_id,
        spreadsheet_id="register-spreadsheet",
        sheet_id=17,
        immutable_business_id="ROW-616",
    )
    target_key = GoogleDriveFileKey(
        connection_id=connection_id,
        file_id="drive-file-616",
    )

    def google_source(
        source_object_id: UUID, key: GoogleProviderKey, kind: str
    ) -> SourceObject:
        identity = google_source_identity(key)
        return SourceObject(
            source_object_id=source_object_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_kind=kind,
            identity_mode=IdentityMode.CONNECTOR,
            connection_id=connection_id,
            external_namespace=identity.external_namespace,
            external_generation=identity.external_generation,
            external_object_id=identity.external_object_id,
            created_at=INGESTION_CONTRACT_NOW,
        )

    row_case = await prepare_canonical_case(
        postgresql_adapter,
        61610,
        scope=(workspace_id, environment_id),
        source_object=google_source(synthetic_uuid(61603), row_key, "record"),
    )
    target_case = await prepare_canonical_case(
        postgresql_adapter,
        61620,
        scope=(workspace_id, environment_id),
        source_object=google_source(synthetic_uuid(61604), target_key, "document"),
        create_scope=False,
        provenance_event="event-2",
        accepted_generation=2,
    )
    for case in (row_case, target_case):
        async with postgresql_adapter.uow_factory(case.context) as uow:
            result = await uow.canonical.accept(case.command)
            assert result.acceptance is not None
            await uow.commit()

    def association_context(
        operation: PersistenceOperation, _identity: UUID
    ) -> TrustedPersistenceContext:
        return postgresql_adapter.ingestion_context(
            workspace_id, environment_id, operation.value
        )

    repository = PostgreSQLRegisterAssociationRepository(
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=association_context,
    )
    claim = RegisterObservedLinkClaim(
        claim_id=synthetic_uuid(61630),
        workspace_id=workspace_id,
        environment_id=environment_id,
        row_source_object_id=row_case.source.source_object_id,
        row_source_revision_id=row_case.revision.revision_id,
        row_provider_identity=row_key,
        row_revision_digest=row_case.revision.revision_digest,
        row_original_sha256=row_case.revision.original_sha256,
        role=RegisterLinkRole.DRIVE_MATERIAL,
        claimed_external_identifier=target_key.file_id,
        external_identifier_type="google_drive_file_id",
        extraction_profile="register-links:v1",
        evidence_locator="column:final_document_id",
        evidence_digest="a" * 64,
        provenance_id=row_case.provenance.provenance_id,
        observed_at=INGESTION_CONTRACT_NOW,
    )
    accepted_context = association_context(
        PersistenceOperation("register_association_accept"), claim.claim_id
    )
    assert accepted_context.service_principal_id is not None
    acceptance = RegisterAssociationAcceptance(
        acceptance_id=synthetic_uuid(61631),
        claim_id=claim.claim_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        row_source_object_id=row_case.source.source_object_id,
        row_source_revision_id=row_case.revision.revision_id,
        target_source_object_id=target_case.source.source_object_id,
        target_source_revision_id=target_case.revision.revision_id,
        target_provider_identity=target_key,
        role=RegisterLinkRole.DRIVE_MATERIAL,
        accepted_by=accepted_context.service_principal_id,
        accepted_at=INGESTION_CONTRACT_NOW,
    )

    persisted_claim = await repository.record_claim(claim)
    claim_replay = await repository.record_claim(claim)
    handoff = await repository.accept_claim(
        claim_id=claim.claim_id,
        acceptance=acceptance,
    )
    acceptance_replay = await repository.accept_claim(
        claim_id=claim.claim_id,
        acceptance=acceptance,
    )
    resolved = await repository.resolve_handoff(claim.claim_id)

    assert persisted_claim == claim_replay == claim
    assert handoff == acceptance_replay == resolved
    assert handoff.claim.row_source_revision_id == row_case.revision.revision_id
    assert handoff.acceptance.target_source_revision_id == target_case.revision.revision_id


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
        contract_context_profile_replay,
        contract_context_profile_create_replay,
        contract_context_profile_retry,
        contract_context_profile_partial_fallback,
        contract_context_profile_duplicate,
        contract_context_profile_digest_mismatch,
        contract_context_profile_unknown_version,
        contract_context_profile_isolation,
        contract_context_profile_revoked,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_shared_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


async def test_postgresql_unit_of_work_tracks_activity_until_context_exit(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3670)

    assert unit_of_work_is_active() is False
    async with postgresql_adapter.uow_factory(
        postgresql_adapter.workspace_context(workspace_id)
    ) as uow:
        assert unit_of_work_is_active() is True
        await uow.rollback()
        assert unit_of_work_is_active() is True

    assert unit_of_work_is_active() is False


async def test_postgresql_parsed_artifacts_use_transaction_local_tenant_scope(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    from spine.ingestion.parsers import parse_document
    from spine.domain.sources import RevisionMetadata
    from spine.domain.sources.canonicalization import canonical_revision_metadata_digest
    from spine.domain.sources.original_reference import ObjectReference
    from hashlib import sha256

    case = await prepare_canonical_case(postgresql_adapter, 51600)
    content = b"test"
    content_digest = sha256(content).hexdigest()
    revision_metadata = RevisionMetadata(embedded_title="Tenant parsed artifact")
    revision = content_revision(
        revision_id=synthetic_uuid(51601),
        source_object_id=case.source.source_object_id,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        kind=case.revision.kind,
        revision_schema_version=case.revision.revision_schema_version,
        revision_metadata_schema=case.revision.revision_metadata_schema,
        revision_metadata=revision_metadata,
        revision_metadata_digest=canonical_revision_metadata_digest(revision_metadata),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=synthetic_uuid(51602),
            storage_generation=synthetic_uuid(51603),
            digest_algorithm="sha256",
            digest_hex=content_digest,
            byte_length=len(content),
        ),
        original_sha256=content_digest,
        byte_length=len(content),
        media_type="text/plain",
        observed_at=case.revision.observed_at,
    )
    provenance = source_contract_provenance(
        case.source, revision.revision_id, "event-2"
    )
    observation_context = postgresql_adapter.source_context(
        case.source.workspace_id, case.source.environment_id
    )
    async with postgresql_adapter.uow_factory(observation_context) as uow:
        await uow.sources.record_observation(
            SourceObservationCommand.create(
                source=case.source,
                revision=revision,
                provenance=provenance,
                idempotency_key=IdempotencyKey("issue516-parsed-source-revision"),
            )
        )
        await uow.commit()
    document = parse_document(revision, content)
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    parsed_context_boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(51604),
        secret=b"issue-516-parsed-artifact-context-secret-001",
    )

    def parsed_context(workspace_id: UUID, environment_id: UUID) -> TrustedPersistenceContext:
        return parsed_context_boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id, environment_id=environment_id
            ),
            service_principal_id=synthetic_uuid(51605),
            purpose=PersistencePurpose("parsed_artifact_repository"),
            operation=PersistenceOperation("parsed_artifact_read"),
            trace_id=synthetic_uuid(51606),
        )

    repository = PostgreSQLParsedArtifactRepository(
        async_sessionmaker(engine, expire_on_commit=False, autoflush=False),
        context_factory=parsed_context,
        context_verifier=parsed_context_boundary,
    )
    try:
        persisted = await repository.persist(
            ParsedArtifactCommand.create(document, command_identity="issue516:parsed:1")
        )
        resolved = await repository.get(
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            artifact_id=document.artifact_id,
        )
        isolated = await repository.get(
            workspace_id=synthetic_uuid(51610),
            environment_id=synthetic_uuid(51611),
            artifact_id=document.artifact_id,
        )
    finally:
        await engine.dispose()

    assert persisted.replay is False
    assert resolved == document
    assert isolated is None


async def test_postgresql_cognee_receipt_maps_after_physical_read(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    """Read Cognee before opening the canonical PostgreSQL mapping UoW."""
    from hashlib import sha256
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    from spine.application.persistence.boundary import CanonicalBoundaryCaptureCommand
    from spine.application.persistence.context import PersistenceOperation, PersistencePurpose
    from spine.application.persistence.command_digest import digest_command
    from spine.application.persistence.idempotency import IdempotencyKey
    from spine.application.persistence.ingestion import (
        IngestionBindingCommand,
        IngestionRunCommand,
        ProcessingReceiptCommand,
    )
    from spine.application.persistence.parsed_artifacts import ParsedArtifactCommand
    from spine.application.persistence.projection import ProjectionRunCommand
    from spine.application.projection_result_persistence import (
        build_projection_result_persistence,
        ParsedArtifactCanonicalProjectionLocatorResolver,
    )
    from spine.domain.ingestion import (
        IngestionRun,
        IngestionRunSource,
        IngestionRunSourceBinding,
        ProcessingCoverage,
        ProcessingReceipt,
    )
    from spine.domain.artifacts import ParsedDocument
    from spine.domain.projections import (
        OntologyVersion,
        ProjectionConfigVersion as CanonicalProjectionConfigVersion,
        ProjectionRun,
        ProjectionRunTransition,
        ProjectionPartition,
        ProjectionRunState,
    )
    from spine.ingestion.parsers import parse_document
    from tests.domain.artifacts.test_parsed_documents import revision as test_source_revision
    from spine.infrastructure.persistence.cognee_projection_receipt import (
        CogneeProjectionReceiptBuilder,
    )
    from spine.memory.contracts import (
        CanonicalBoundary as MemoryCanonicalBoundary,
        ProjectionCandidate,
        ProjectionConfigVersion,
        ProjectionNamespaceState,
            ProjectionEffect,
            ProjectionEffectAction,
            ProjectionEffectStatus,
            ProjectionOperation as MemoryProjectionOperation,
        ProjectionResult,
        ProjectionSourceRevision,
        ProjectCommand,
    )
    from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter
    from spine.memory.cognee.retrieval.adapter import CogneeDataMetadata

    case = await prepare_canonical_case(postgresql_adapter, 51700)
    assert postgresql_adapter.ingestion_context is not None
    async with postgresql_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    content = b"PostgreSQL Cognee receipt evidence\n"
    revision = case.revision
    template_revision = test_source_revision(
        content,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
    )
    template_document = parse_document(template_revision, content)
    document = ParsedDocument.from_source(
        revision,
        parser_name=template_document.parser_name,
        parser_version=template_document.parser_version,
        parser_config_reference=template_document.parser_config_reference,
        locator_contract_version=template_document.locator_contract_version,
        media_type=template_document.media_type,
        coverage=template_document.coverage,
        segments=template_document.segments,
        exclusions=template_document.exclusions,
        source_byte_length=revision.byte_length,
        source_codepoint_length=template_document.source_codepoint_length,
    )
    runtime_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    context_boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(51704),
        secret=b"issue-516-cognee-composition-context-secret-001",
    )

    def parsed_context(workspace_id: UUID, environment_id: UUID) -> TrustedPersistenceContext:
        return context_boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(51705),
            purpose=PersistencePurpose("parsed_artifact_repository"),
            operation=PersistenceOperation("parsed_artifact_read"),
            trace_id=synthetic_uuid(51706),
        )

    parsed_artifacts = PostgreSQLParsedArtifactRepository(
        async_sessionmaker(runtime_engine, expire_on_commit=False, autoflush=False),
        context_factory=parsed_context,
        context_verifier=context_boundary,
    )
    await parsed_artifacts.persist(
        ParsedArtifactCommand.create(document, command_identity="issue516:parsed:e2e")
    )

    receipt_id = synthetic_uuid(51717)
    ingestion_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "ingestion_run"
    )
    ingestion_run = IngestionRun(
        ingestion_run_id=synthetic_uuid(51721),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        command_identity="issue516-e2e-ingestion",
        command_digest="0" * 64,
        trigger_kind="reprocess",
        requested_scope_schema="ingestion-scope:v1",
        requested_scope_digest=sha256(b"issue516-e2e-scope").hexdigest(),
        requested_at=now,
    )
    ingestion_run_command = IngestionRunCommand.create(
        run=ingestion_run,
        idempotency_key=IdempotencyKey("issue516-e2e-ingestion"),
    )
    ingestion_item = IngestionRunSource(
        ingestion_run_source_id=synthetic_uuid(51718),
        ingestion_run_id=ingestion_run.ingestion_run_id,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        input_ordinal=1,
        input_identity="issue516-e2e-item",
        input_digest=sha256(b"issue516-e2e-item").hexdigest(),
        command_identity="issue516-e2e-item",
        command_digest=sha256(b"issue516-e2e-item-command").hexdigest(),
        created_at=now,
    )
    source_context = postgresql_adapter.source_context(
        case.source.workspace_id, case.source.environment_id
    )
    async with postgresql_adapter.uow_factory(source_context) as uow:
        original_claim = await uow.idempotency.claim(
            operation_schema_version=1,
            key=IdempotencyKey("canonical-original-51700"),
            digest=digest_command(
                operation=source_context.operation,
                operation_schema_version=1,
                payload={"canonical_original": 51700},
            ),
        )
        await uow.rollback()
    association = _receipt_association(
        case.source,
        revision,
        case.provenance,
        receipt_id=original_claim.receipt_id,
        number=51700,
    )
    ingestion_binding = IngestionRunSourceBinding(
        binding_id=synthetic_uuid(51719),
        ingestion_run_source_id=ingestion_item.ingestion_run_source_id,
        source_object_id=case.source.source_object_id,
        source_revision_id=revision.revision_id,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        binding_command_identity="issue516-e2e-binding",
        binding_command_digest=sha256(b"issue516-e2e-binding").hexdigest(),
        original_reference=association.original_reference,
        original_receipt_kind=association.original_receipt_kind,
        original_receipt_id=association.original_receipt_id,
        original_receipt_digest=association.original_reference.digest_hex,
        bound_at=now,
    )
    processing_receipt = ProcessingReceipt(
        processing_receipt_id=receipt_id,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        ingestion_run_source_id=ingestion_item.ingestion_run_source_id,
        binding_id=ingestion_binding.binding_id,
        source_object_id=case.source.source_object_id,
        source_revision_id=revision.revision_id,
        processor_kind="document_parser",
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        parser_config_reference=document.parser_config_reference,
        locator_contract_version=document.locator_contract_version,
        original_sha256=document.original_sha256,
        output_kind="parsed_document",
        output_reference=document.artifact_id,
        output_digest=document.artifact_digest,
        coverage=ProcessingCoverage.COMPLETE,
        command_identity="issue516-e2e-processing",
        command_digest=sha256(b"issue516-e2e-processing").hexdigest(),
        completed_at=now,
    )
    observation = SourceObservationCommand.create(
        source=case.source,
        revision=revision,
        provenance=case.provenance,
        idempotency_key=IdempotencyKey("canonical-observation-51700"),
        receipt_association=association,
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        await uow.ingestion.create_run(ingestion_run_command)
        await uow.ingestion.create_item(ingestion_item)
        await uow.commit()
    binding_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "ingestion_binding"
    )
    async with postgresql_adapter.uow_factory(binding_context) as uow:
        await uow.ingestion.bind_source(
            IngestionBindingCommand.create(
                item=ingestion_item,
                binding=ingestion_binding,
                observation=observation,
                idempotency_key=IdempotencyKey("issue516-e2e-binding"),
            )
        )
        await uow.commit()
    processing_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "processing_receipt"
    )
    async with postgresql_adapter.uow_factory(processing_context) as uow:
        await uow.ingestion.record_processing_receipt(
            ProcessingReceiptCommand.create(
                receipt=processing_receipt,
                idempotency_key=IdempotencyKey("issue516-e2e-processing"),
            )
        )
        await uow.commit()

    boundary_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "canonical_boundary_capture"
    )
    async with postgresql_adapter.uow_factory(boundary_context) as uow:
        captured = await uow.boundaries.capture(
            CanonicalBoundaryCaptureCommand.create(
                projection_kind="context",
                scope_version="r1",
                idempotency_key=IdempotencyKey("issue516-e2e-boundary"),
            )
        )
        await uow.commit()
    boundary = captured.boundary
    projection_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_run"
    )
    result_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_receipt"
    )
    from tests.contracts.persistence.test_projection_contract import _ontology

    ontology = _ontology(case.source.workspace_id, case.source.environment_id)
    from tests.contracts.persistence.test_projection_contract import _digest

    config_payload = {"extractor": "provider-neutral:v1"}
    config = CanonicalProjectionConfigVersion(
        projection_config_version_id=synthetic_uuid(51711),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_kind="context",
        version=1,
        ontology_version_id=ontology.ontology_version_id,
        parser_contract_version=document.parser_version,
        locator_contract_version=document.locator_contract_version,
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="context-projection:v1",
        config_payload=config_payload,
        config_digest=_digest("projection-config:r1", config_payload),
        created_at=now,
    )
    run_id, candidate_id, partition_id = (
        synthetic_uuid(51712), synthetic_uuid(51713), synthetic_uuid(51714)
    )
    command_identity = "issue516-e2e-project"
    command_digest = sha256(b"issue516-e2e-command").hexdigest()
    run = ProjectionRun(
        projection_run_id=run_id,
        projection_candidate_id=candidate_id,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_kind="context",
        operation=ProjectionOperation.BUILD,
        projection_config_version_id=config.projection_config_version_id,
        canonical_boundary_id=boundary.boundary_id,
        command_identity="issue516-e2e-run",
        command_digest=sha256(b"issue516-e2e-run").hexdigest(),
        idempotency_receipt_id=synthetic_uuid(51715),
        started_at=now,
    )
    partition = ProjectionPartition(
        projection_partition_id=partition_id,
        projection_run_id=run_id,
        projection_candidate_id=candidate_id,
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        security_domain_reference="security-domain:e2e",
        security_domain_version="r1",
        partition_identity="default",
        expected_command_identity=command_identity,
        expected_command_digest=command_digest,
        created_at=now,
    )
    async with postgresql_adapter.uow_factory(projection_context) as uow:
        await uow.projections.add_ontology_version(ontology)
        await uow.projections.add_config_version(config)
        await uow.projections.create_run(
            ProjectionRunCommand.create(
                run=run, idempotency_key=IdempotencyKey("issue516-e2e-run")
            )
        )
        await uow.projections.append_transition(
            ProjectionRunTransition(
                projection_run_transition_id=synthetic_uuid(51716),
                projection_run_id=run_id,
                projection_candidate_id=candidate_id,
                workspace_id=case.source.workspace_id,
                environment_id=case.source.environment_id,
                state=ProjectionRunState.REQUESTED,
                attempt=1,
                occurred_at=now,
            )
        )
        await uow.projections.add_partition(partition)
        await uow.commit()

    provider_config = ProjectionConfigVersion(
        config_id=config.projection_config_version_id,
        version=str(config.version),
        config_digest=config.config_digest,
        ontology_version=str(config.ontology_version_id),
        adapter_contract_version=config.adapter_contract_version,
    )
    canonical_boundary = MemoryCanonicalBoundary(
        boundary_id=boundary.boundary_id,
        boundary_digest=boundary.manifest_digest,
        accepted_through_generation=boundary.accepted_through_generation,
    )
    candidate = ProjectionCandidate(
        candidate_id=candidate_id, run_id=run_id, partition_id=partition_id
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            projection_kind="context",
            projection_candidate=candidate,
            projection_config=provider_config,
            canonical_boundary=canonical_boundary,
            state=ProjectionNamespaceState.SHADOW,
        )
    )
    source_revision = ProjectionSourceRevision(
        source_object_id=case.source.source_object_id,
        source_revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        revision_number=1,
        processing_receipt_id=receipt_id,
        locator_contract_version=document.locator_contract_version,
    )
    command = ProjectCommand(
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_run_id=run_id,
        projection_candidate=candidate,
        projection_config=provider_config,
        canonical_boundary=canonical_boundary,
        source_revisions=(source_revision,),
        physical_namespace=namespace,
        command_identity=command_identity,
        command_digest=command_digest,
    )
    class PhysicalReader:
        version = "test-cognee:v1"

        async def list_data_metadata(self, dataset_name: str):
            assert unit_of_work_is_active() is False
            assert dataset_name.startswith("spine_context_")
            segment = document.segments[0]
            metadata = {
                "spine_schema": "chunk-artifact:v1",
                "workspace_id": str(command.workspace_id),
                "environment_id": str(command.environment_id),
                "source_object_id": str(source_revision.source_object_id),
                "source_revision_id": str(source_revision.source_revision_id),
                "revision_digest": source_revision.revision_digest,
                "revision_number": source_revision.revision_number,
                "namespace_digest": command.physical_namespace.namespace_digest,
                "projection_candidate_id": str(candidate_id),
                "projection_run_id": str(run_id),
                "projection_config_id": str(config.projection_config_version_id),
                "projection_config_version": str(config.version),
                "projection_config_digest": config.config_digest,
                "processing_receipt_id": str(receipt_id),
                "segment_id": str(segment.segment_id),
                "segment_digest": segment.segment_digest,
                "locator_contract_version": document.locator_contract_version,
                "locator": segment.locator.model_dump(mode="json"),
            }
            return (CogneeDataMetadata(data_id=str(segment.segment_id), external_metadata=metadata),)

    builder = CogneeProjectionReceiptBuilder(
        physical_reader=PhysicalReader(),
        parsed_artifacts=parsed_artifacts,
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=lambda _command: projection_context,
        clock=lambda: now,
    )
    effect = ProjectionEffect(
        effect_id="issue516-e2e-project:1",
        operation=MemoryProjectionOperation.PROJECT,
        action=ProjectionEffectAction.UPSERT_SOURCE,
        status=ProjectionEffectStatus.APPLIED,
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        command_identity=command.command_identity,
        command_digest=command.command_digest,
        projection_candidate=command.projection_candidate,
        projection_config=command.projection_config,
        canonical_boundary=command.canonical_boundary,
        physical_namespace=command.physical_namespace,
        source_object_id=source_revision.source_object_id,
        source_revision_id=source_revision.source_revision_id,
        revision_digest=source_revision.revision_digest,
        revision_number=source_revision.revision_number,
        processing_receipt_id=receipt_id,
        locator_contract_version=document.locator_contract_version,
    )
    physical_result = ProjectionResult(
        operation=command.operation,
        outcome="succeeded",
        command_identity=command.command_identity,
        command_digest=command.command_digest,
        artifact_reference="observed-cognee-artifact",
        effects=(effect,),
    )
    try:
        adapter_receipt = await builder.build(command, physical_result)

        bridge = build_projection_result_persistence(
            uow_factory=postgresql_adapter.uow_factory,
            context_factory=lambda _command: result_context,
            locator_resolver=ParsedArtifactCanonicalProjectionLocatorResolver(
                uow_factory=postgresql_adapter.uow_factory,
                context_factory=lambda _command: result_context,
                parsed_artifacts=parsed_artifacts,
            ),
            reference_id_source=lambda: synthetic_uuid(51720),
        )
        mapped = await bridge.map(command, adapter_receipt)
        async with postgresql_adapter.uow_factory(result_context) as uow:
            persisted_receipts = await uow.projections.list_receipts(run_id)
            persisted_references = await uow.projections.list_source_references(run_id)
    finally:
        await runtime_engine.dispose()

    assert mapped.handoff.receipt_command.receipt.command_identity == command_identity
    assert any(receipt.command_identity == command_identity for receipt in persisted_receipts)
    assert any(
        reference.projected_object_id == str(document.segments[0].segment_id)
        and reference.locator_reference.startswith("character_range:line:1:")
        for reference in persisted_references
    )


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
        contract_outbox_projection_conflict,
        contract_outbox_projection_scope,
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


async def test_postgresql_projection_publication_satisfies_cas_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_publication(postgresql_adapter)


async def test_postgresql_projection_successor_tombstone_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_successor_tombstone(postgresql_adapter)


async def test_postgresql_projection_successor_replay_after_activation_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_successor_replay_after_activation(postgresql_adapter)


async def test_postgresql_projection_sealed_successor_terminal_transition_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_sealed_successor_terminal_transition(postgresql_adapter)


async def test_postgresql_projection_successor_locator_contract_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_successor_locator_contract(postgresql_adapter)


async def test_postgresql_projection_pre_validation_fault_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_pre_validation_fault(postgresql_adapter)


async def test_postgresql_projection_post_validation_fault_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_post_validation_fault(postgresql_adapter)


async def test_postgresql_projection_recovery_resolver_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_recovery_resolver(postgresql_adapter)


async def test_postgresql_projection_recovery_lifecycle_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_recovery_lifecycle(postgresql_adapter)


async def test_postgresql_projection_recovery_result_bridge_satisfies_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_projection_recovery_result_bridge(postgresql_adapter)


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
        contract_source_queries,
        contract_source_observation_rollback,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_source_observation_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


@pytest.mark.parametrize(
    "contract",
    (
        contract_canonical_happy_replay,
        contract_canonical_authority,
        contract_canonical_receipt,
        contract_canonical_cas,
        contract_canonical_admission,
        contract_canonical_replay_authority,
        contract_canonical_unknown_ordering,
        contract_canonical_boundary,
    ),
    ids=lambda contract: contract.__name__.removeprefix("test_"),
)
async def test_postgresql_adapter_satisfies_canonical_acceptance_contract(
    postgresql_adapter: PersistenceAdapter,
    contract: ContractTest,
) -> None:
    await contract(postgresql_adapter)


async def test_postgresql_document_tombstone_preserves_history_and_canonical_effects(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 34300)
    async with postgresql_adapter.uow_factory(case.context) as uow:
        initial = await uow.canonical.accept(case.command)
        await uow.commit()
    assert initial.acceptance is not None

    command = TombstoneDocumentCommand(
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        expected_current_revision_id=case.revision.revision_id,
        deletion_reason="source_deleted",
        idempotency_key="document-tombstone-34300",
    )

    def canonical_factory(**values: object) -> CanonicalAcceptanceCommand:
        acceptance_id = values["canonical_acceptance_id"]
        revision = values["revision"]
        accepted_at = values["accepted_at"]
        assert isinstance(acceptance_id, UUID)
        audit_event_id = uuid5(NAMESPACE_URL, f"ticket-343:audit:{acceptance_id}")
        outbox_event_id = uuid5(NAMESPACE_URL, f"ticket-343:outbox:{acceptance_id}")
        audit = AuditEventRegistry.with_default_families().build_event(
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            event_type="canonical.transition",
            schema_version=1,
            payload=CanonicalTransitionAuditPayload(transition="source.tombstoned"),
            origin=ContextOrigin.WORKER,
            acting_subject_id=None,
            service_principal_id=case.context.service_principal_id,
            trace_id=case.context.trace_id,
            occurred_at=accepted_at,
            outcome=AuditOutcome.COMMITTED,
            reason="source_tombstoned",
            target=AuditObjectReference(
                object_type="source_revision",
                object_id=revision.revision_id,
                schema_version=1,
            ),
            producer_deduplication_id=f"document-tombstone:{acceptance_id}",
            audit_event_id=audit_event_id,
        )
        outbox = _canonical_outbox(
            postgresql_adapter,
            context=case.context,
            source_object_id=case.source.source_object_id,
            source_revision_id=revision.revision_id,
            acceptance_id=acceptance_id,
            generation=2,
            audit_event_id=audit_event_id,
            event_id=outbox_event_id,
            transition="source.tombstoned",
        )
        return CanonicalAcceptanceCommand.create(
            source=values["source"],
            revision=revision,
            provenance=values["provenance"],
            admission_decision_id=None,
            access_policy_version_id=None,
            expected_current_revision_id=values["expected_current_revision_id"],
            idempotency_key=values["idempotency_key"],
            canonical_acceptance_id=acceptance_id,
            audit_event_id=audit_event_id,
            outbox_event_id=outbox_event_id,
            accepted_at=accepted_at,
            audit_event=audit,
            outbox_intent=outbox,
            operation=case.context.operation,
        )

    composer = ServerDocumentLifecycleComposer(
        uow_factory=postgresql_adapter.uow_factory,
        replacement_evidence=object(),
        canonical_command_factory=canonical_factory,
    )
    service = CanonicalDocumentLifecycleCommandService(
        composer=composer,
        coordinator=SourceLifecycleCoordinator(
            uow_factory=postgresql_adapter.uow_factory,
            context_factory=lambda lifecycle: case.context,
        ),
        processing=object(),
        uow_factory=postgresql_adapter.uow_factory,
        clock=lambda: datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
    )

    receipt = await service.tombstone(context=case.context, command=command)

    assert receipt.status is LifecycleOperationStatus.COMPLETED
    assert receipt.processing_run_id is None
    async with postgresql_adapter.uow_factory(case.context) as uow:
        current = await uow.canonical.resolve_current(case.source.source_object_id)
        revisions = await uow.sources.list_revisions(
            case.source.source_object_id,
            limit=10,
        )
        acceptances = await uow.canonical.list_acceptances(
            case.source.source_object_id
        )
        await uow.commit()
    assert current is not None
    assert current.current_revision_id == receipt.accepted_revision_id
    assert len(revisions) == 2
    assert {revision.revision_id for revision in revisions} == {
        case.revision.revision_id,
        receipt.accepted_revision_id,
    }
    assert len(acceptances) == 2
    assert acceptances[-1].audit_event_id.int != 0
    assert acceptances[-1].outbox_event_id.int != 0


async def test_postgresql_document_replacement_persists_verified_successor_and_handoff(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 34400)
    async with postgresql_adapter.uow_factory(case.context) as uow:
        initial = await uow.canonical.accept(case.command)
        await uow.commit()
    assert initial.acceptance is not None

    command = ReplaceDocumentCommand(
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        source_object_id=case.source.source_object_id,
        expected_current_revision_id=case.revision.revision_id,
        upload_intent=ExistingUploadIntent(
            upload_identity="replacement-34400",
            declared_media_type="text/plain",
            declared_filename="replacement.txt",
            storage_request_reference="finalized:replacement-34400",
        ),
        idempotency_key="document-replacement-34400",
    )
    lifecycle_digest = CanonicalDocumentLifecycleCommandService.command_digest(command)
    replacement_template = source_contract_revision(
        case.source,
        uuid5(
            NAMESPACE_URL,
            f"ticket-343:replacement-revision:{case.source.source_object_id}",
        ),
    )
    replacement_values = replacement_template.model_dump(
        mode="python",
        exclude={"revision_digest"},
    )
    replacement_values.update(
        original_reference=replacement_template.original_reference.model_copy(
            update={"digest_hex": "e" * 64, "byte_length": 11}
        ),
        original_sha256="e" * 64,
        byte_length=11,
    )
    replacement_revision = content_revision(**replacement_values)
    assert replacement_revision.revision_digest != case.revision.revision_digest
    replacement_provenance = source_contract_provenance(
        case.source,
        replacement_revision.revision_id,
        "event-2",
    ).model_copy(
        update={
            "producer_kind": "lifecycle",
            "producer_reference": "document-lifecycle",
            "event_digest": lifecycle_digest.value,
            "observer_service": "document-lifecycle",
        }
    )
    receipt_id = await _original_receipt(
        postgresql_adapter,
        case.context,
        934400,
    )
    association = _receipt_association(
        case.source,
        replacement_revision,
        replacement_provenance,
        receipt_id=receipt_id,
        number=934400,
    )
    async with postgresql_adapter.uow_factory(case.context) as uow:
        await uow.sources.record_observation(
            SourceObservationCommand.create(
                source=case.source,
                revision=replacement_revision,
                provenance=replacement_provenance,
                idempotency_key=IdempotencyKey(
                    "document-replacement-34400:observation"
                ),
                receipt_association=association,
                operation=case.context.operation,
            )
        )
        await uow.commit()

    admission_id = synthetic_uuid(934403)
    async with postgresql_adapter.uow_factory(case.context) as uow:
        await uow.admissions.append_decision(
            AdmissionDecision(
                decision_id=admission_id,
                source_object_id=case.source.source_object_id,
                revision_id=replacement_revision.revision_id,
                workspace_id=case.source.workspace_id,
                environment_id=case.source.environment_id,
                decision_version=1,
                disposition=AdmissionDisposition.ADMITTED,
                validator_profile_version="admission:r1",
                reason_schema_version="reason.v1",
                reason_codes=("clean",),
                authority_reference={
                    "reference_type": "validator",
                    "reference_id": synthetic_uuid(934404),
                    "schema_version": 1,
                },
                trace_id=case.context.trace_id,
                audit_event_id=synthetic_uuid(934405),
                decided_at=datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
            )
        )
        await uow.commit()

    class EvidenceProvider:
        async def prepare(self, **values: object) -> ReplacementLifecycleEvidence:
            assert values["command_digest"] == lifecycle_digest
            return ReplacementLifecycleEvidence(
                source=case.source,
                revision=replacement_revision,
                provenance=replacement_provenance,
                receipt_association=association,
                admission_decision_id=admission_id,
                access_policy_version_id=case.policy_id,
            )

    def canonical_factory(**values: object) -> CanonicalAcceptanceCommand:
        acceptance_id = values["canonical_acceptance_id"]
        accepted_at = values["accepted_at"]
        assert isinstance(acceptance_id, UUID)
        audit_event_id = uuid5(NAMESPACE_URL, f"ticket-343:audit:{acceptance_id}")
        outbox_event_id = uuid5(NAMESPACE_URL, f"ticket-343:outbox:{acceptance_id}")
        audit = AuditEventRegistry.with_default_families().build_event(
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            event_type="canonical.transition",
            schema_version=1,
            payload=CanonicalTransitionAuditPayload(transition="source.accepted"),
            origin=ContextOrigin.WORKER,
            acting_subject_id=None,
            service_principal_id=case.context.service_principal_id,
            trace_id=case.context.trace_id,
            occurred_at=accepted_at,
            outcome=AuditOutcome.COMMITTED,
            reason="source_accepted",
            target=AuditObjectReference(
                object_type="source_revision",
                object_id=replacement_revision.revision_id,
                schema_version=1,
            ),
            producer_deduplication_id=f"document-replacement:{acceptance_id}",
            audit_event_id=audit_event_id,
        )
        outbox = _canonical_outbox(
            postgresql_adapter,
            context=case.context,
            source_object_id=case.source.source_object_id,
            source_revision_id=replacement_revision.revision_id,
            acceptance_id=acceptance_id,
            generation=2,
            audit_event_id=audit_event_id,
            event_id=outbox_event_id,
            transition="source.accepted",
        )
        return CanonicalAcceptanceCommand.create(
            source=case.source,
            revision=replacement_revision,
            provenance=replacement_provenance,
            admission_decision_id=admission_id,
            access_policy_version_id=case.policy_id,
            expected_current_revision_id=case.revision.revision_id,
            idempotency_key=values["idempotency_key"],
            canonical_acceptance_id=acceptance_id,
            audit_event_id=audit_event_id,
            outbox_event_id=outbox_event_id,
            accepted_at=accepted_at,
            audit_event=audit,
            outbox_intent=outbox,
            operation=case.context.operation,
        )

    processing_intents: list[object] = []

    async def process_replacement(**values: object) -> object:
        processing_intents.append(values["intent"])
        return SimpleNamespace(run_id=synthetic_uuid(934406))

    service = CanonicalDocumentLifecycleCommandService(
        composer=ServerDocumentLifecycleComposer(
            uow_factory=postgresql_adapter.uow_factory,
            replacement_evidence=EvidenceProvider(),
            canonical_command_factory=canonical_factory,
        ),
        coordinator=SourceLifecycleCoordinator(
            uow_factory=postgresql_adapter.uow_factory,
            context_factory=lambda lifecycle: case.context,
        ),
        processing=SimpleNamespace(replacement=process_replacement),
        uow_factory=postgresql_adapter.uow_factory,
        clock=lambda: datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
    )

    receipt = await service.replace(context=case.context, command=command)

    assert receipt.status is LifecycleOperationStatus.PROCESSING_DISPATCHED
    assert len(processing_intents) == 1
    assert processing_intents[0].source_revision_id == replacement_revision.revision_id
    async with postgresql_adapter.uow_factory(case.context) as uow:
        current = await uow.canonical.resolve_current(case.source.source_object_id)
        revisions = await uow.sources.list_revisions(
            case.source.source_object_id,
            limit=10,
        )
        associations = await uow.sources.resolve_receipt_associations(
            replacement_revision.revision_id
        )
        await uow.commit()
    assert current is not None
    assert current.current_revision_id == replacement_revision.revision_id
    assert len(revisions) == 2
    assert associations == (association,)


async def test_postgresql_boundary_capture_fences_acceptance_at_counter_lock(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 26000)
    assert postgresql_adapter.ingestion_context is not None
    boundary_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "canonical_boundary_capture",
    )
    boundary_command = CanonicalBoundaryCaptureCommand.create(
        projection_kind="qa",
        scope_version="r1",
        idempotency_key=IdempotencyKey("boundary-race-26000"),
    )

    async def capture() -> object:
        async with postgresql_adapter.uow_factory(boundary_context) as uow:
            result = await uow.boundaries.capture(boundary_command)
            await uow.commit()
            return result

    async def accept() -> None:
        async with postgresql_adapter.uow_factory(case.context) as uow:
            await uow.canonical.accept(case.command)
            await uow.commit()

    capture_task: asyncio.Task[object] | None = None
    acceptance_task: asyncio.Task[None] | None = None
    try:
        CANONICAL_TRANSACTION_PROBE.arm(
            PostgreSQLTransactionStage.AFTER_CANONICAL_BOUNDARY_COUNTER_LOCKED
        )
        capture_task = asyncio.create_task(capture())
        await CANONICAL_TRANSACTION_PROBE.wait_until_reached()
        acceptance_task = asyncio.create_task(accept())
        await asyncio.sleep(0)
        CANONICAL_TRANSACTION_PROBE.release()
        captured = await capture_task
        await acceptance_task
    finally:
        CANONICAL_TRANSACTION_PROBE.disarm()
        for task in (capture_task, acceptance_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (capture_task, acceptance_task) if task is not None),
            return_exceptions=True,
        )

    assert captured.boundary.accepted_through_generation == 0
    assert captured.members == ()

    later_command = CanonicalBoundaryCaptureCommand.create(
        projection_kind="qa",
        scope_version="r1",
        idempotency_key=IdempotencyKey("boundary-race-26001"),
    )
    async with postgresql_adapter.uow_factory(boundary_context) as uow:
        later = await uow.boundaries.capture(later_command)
        await uow.rollback()

    assert later.boundary.accepted_through_generation == 1
    assert len(later.members) == 1
    assert later.members[0].source_revision_id == case.revision.revision_id


async def test_postgresql_canonical_acceptance_requires_corresponding_outbox_intent(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 19250)
    async with postgresql_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()

    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            constraint_name = await connection.scalar(
                text(
                    "SELECT constraint_record.conname "
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
        assert isinstance(constraint_name, str)
        with pytest.raises(IntegrityError) as error:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE spine.outbox_intents "
                        "DISABLE TRIGGER trg_outbox_intents_immutable"
                    )
                )
                await connection.execute(
                    text(
                        "DELETE FROM spine.outbox_intents "
                        "WHERE event_id = :event_id"
                    ),
                    {"event_id": case.command.outbox_event_id},
                )
                await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert error.value.orig.diag.constraint_name == constraint_name
    finally:
        await engine.dispose()


async def test_postgresql_policy_change_wins_before_acceptance_and_stale_evidence_fails_closed(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 19300)
    assert postgresql_adapter.ingestion_context is not None
    policy_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "access_policy_activation",
    )
    policy_task: asyncio.Task[object] | None = None
    acceptance_task: asyncio.Task[object] | None = None
    try:
        CANONICAL_TRANSACTION_PROBE.arm(PostgreSQLTransactionStage.BEFORE_COMMIT)
        policy_task = asyncio.create_task(
            activate_canonical_policy(
                postgresql_adapter,
                context=policy_context,
                source_object=case.source,
                number=19320,
                expected_policy_id=case.policy_id,
                expected_generation=1,
                expected_cas=1,
            )
        )
        await CANONICAL_TRANSACTION_PROBE.wait_until_reached()

        async def accept() -> None:
            async with postgresql_adapter.uow_factory(case.context) as uow:
                await uow.canonical.accept(case.command)
                await uow.commit()

        acceptance_task = asyncio.create_task(accept())
        await asyncio.sleep(0)
        CANONICAL_TRANSACTION_PROBE.release()
        await policy_task
        with pytest.raises(CanonicalEligibilityError):
            await acceptance_task
    finally:
        CANONICAL_TRANSACTION_PROBE.disarm()
        for task in (policy_task, acceptance_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (policy_task, acceptance_task) if task is not None),
            return_exceptions=True,
        )


async def test_postgresql_acceptance_locks_authoritative_policy_until_commit(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 19400)
    assert postgresql_adapter.ingestion_context is not None

    async def accept() -> object:
        async with postgresql_adapter.uow_factory(case.context) as uow:
            result = await uow.canonical.accept(case.command)
            await uow.commit()
            return result

    acceptance_task: asyncio.Task[object] | None = None
    policy_task: asyncio.Task[object] | None = None
    try:
        CANONICAL_TRANSACTION_PROBE.arm(
            PostgreSQLTransactionStage.AFTER_CANONICAL_EVIDENCE_LOCKED
        )
        acceptance_task = asyncio.create_task(accept())
        await CANONICAL_TRANSACTION_PROBE.wait_until_reached()
        policy_context = postgresql_adapter.ingestion_context(
            case.source.workspace_id,
            case.source.environment_id,
            "access_policy_activation",
        )
        policy_task = asyncio.create_task(
            activate_canonical_policy(
                postgresql_adapter,
                context=policy_context,
                source_object=case.source,
                number=19420,
                expected_policy_id=case.policy_id,
                expected_generation=1,
                expected_cas=1,
            )
        )
        await asyncio.sleep(0)
        CANONICAL_TRANSACTION_PROBE.release()
        result = await acceptance_task
        await policy_task
        assert result.advanced is True
    finally:
        CANONICAL_TRANSACTION_PROBE.disarm()
        for task in (acceptance_task, policy_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (acceptance_task, policy_task) if task is not None),
            return_exceptions=True,
        )

    async with postgresql_adapter.uow_factory(case.context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await uow.canonical.accept(case.command)


async def test_postgresql_admission_change_wins_before_acceptance_and_fails_closed(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 19500)

    async def deny_admission() -> None:
        async with postgresql_adapter.uow_factory(case.context) as uow:
            await uow.admissions.append_decision(
                AdmissionDecision(
                    decision_id=synthetic_uuid(19520),
                    source_object_id=case.source.source_object_id,
                    revision_id=case.revision.revision_id,
                    workspace_id=case.source.workspace_id,
                    environment_id=case.source.environment_id,
                    decision_version=2,
                    predecessor_decision_id=case.admission_id,
                    disposition=AdmissionDisposition.REJECTED,
                    validator_profile_version="admission:r1",
                    reason_schema_version="reason.v1",
                    reason_codes=("revoked",),
                    authority_reference={
                        "reference_type": "validator",
                        "reference_id": synthetic_uuid(19521),
                        "schema_version": 1,
                    },
                    trace_id=case.context.trace_id,
                    audit_event_id=synthetic_uuid(19522),
                    decided_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
                )
            )
            await uow.commit()

    admission_task: asyncio.Task[object] | None = None
    acceptance_task: asyncio.Task[object] | None = None
    try:
        CANONICAL_TRANSACTION_PROBE.arm(PostgreSQLTransactionStage.BEFORE_COMMIT)
        admission_task = asyncio.create_task(deny_admission())
        await CANONICAL_TRANSACTION_PROBE.wait_until_reached()

        async def accept() -> None:
            async with postgresql_adapter.uow_factory(case.context) as uow:
                await uow.canonical.accept(case.command)
                await uow.commit()

        acceptance_task = asyncio.create_task(accept())
        await asyncio.sleep(0)
        CANONICAL_TRANSACTION_PROBE.release()
        await admission_task
        with pytest.raises(CanonicalEligibilityError):
            await acceptance_task
    finally:
        CANONICAL_TRANSACTION_PROBE.disarm()
        for task in (admission_task, acceptance_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (admission_task, acceptance_task) if task is not None),
            return_exceptions=True,
        )


async def test_postgresql_adapter_satisfies_ingestion_repository_contract(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    await exercise_ingestion_run_item_contract(postgresql_adapter)
    await contract_ingestion_binding_receipt(postgresql_adapter)

    incomplete_receipt_id = synthetic_uuid(1831)
    workspace_id = synthetic_uuid(1811)
    environment_id = synthetic_uuid(1812)
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.idempotency_receipts ("
                    "receipt_id, workspace_id, environment_id, operation_name, "
                    "operation_schema_version, idempotency_key, "
                    "digest_algorithm_version, command_digest) VALUES ("
                    ":receipt_id, :workspace_id, :environment_id, 'original.upload', "
                    "1, 'incomplete-original-upload', 'spine.command-digest.v1', :digest)"
                ),
                {
                    "receipt_id": incomplete_receipt_id,
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                    "digest": "3" * 64,
                },
            )
    finally:
        await engine.dispose()

    denied_item = IngestionRunSource(
        ingestion_run_source_id=synthetic_uuid(1826),
        ingestion_run_id=synthetic_uuid(1813),
        workspace_id=workspace_id,
        environment_id=environment_id,
        input_ordinal=2,
        input_identity="contract-binding-denied-input",
        input_digest="1" * 64,
        command_identity="contract-binding-denied-item",
        command_digest="2" * 64,
        created_at=INGESTION_CONTRACT_NOW,
    )
    incomplete_binding = IngestionRunSourceBinding(
        binding_id=synthetic_uuid(1832),
        ingestion_run_source_id=denied_item.ingestion_run_source_id,
        source_object_id=synthetic_uuid(1815),
        source_revision_id=synthetic_uuid(1816),
        workspace_id=workspace_id,
        environment_id=environment_id,
        binding_command_identity="incomplete-receipt-binding",
        binding_command_digest="4" * 64,
        original_reference=ObjectReference(
            schema_version=1,
            object_id=synthetic_uuid(1817),
            storage_generation=synthetic_uuid(1818),
            digest_algorithm="sha256",
            digest_hex="e" * 64,
            byte_length=4,
        ),
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=incomplete_receipt_id,
        original_receipt_digest="e" * 64,
        bound_at=INGESTION_CONTRACT_NOW,
    )
    incomplete_command = ExistingRevisionBindingCommand.create(
        item=denied_item,
        binding=incomplete_binding,
        idempotency_key=IdempotencyKey("contract-binding-incomplete-receipt"),
    )
    assert postgresql_adapter.ingestion_context is not None
    with pytest.raises(ConstraintConflictError):
        async with postgresql_adapter.uow_factory(
            postgresql_adapter.ingestion_context(
                workspace_id, environment_id, "ingestion_binding"
            )
        ) as uow:
            await uow.ingestion.bind_existing_revision(incomplete_command)
            await uow.commit()

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

    conflicting = proposal.model_copy(
        update={"benefit_classification": PolicyBenefitClassification.SELF_BENEFITING}
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        with pytest.raises(ConstraintConflictError):
            await uow.access_policies.persist_proposal(policy, conflicting)


async def test_access_policy_activation_has_one_concurrent_cas_winner_and_stable_replay(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    assert postgresql_adapter.source_context is not None
    assert postgresql_adapter.ingestion_context is not None
    workspace_id, environment_id = synthetic_uuid(3900), synthetic_uuid(3901)
    await prepare_source_scope(postgresql_adapter, workspace_id, environment_id)
    source_object = source_contract_source(workspace_id, environment_id)
    source_revision = source_contract_revision(source_object, synthetic_uuid(3902))
    observation = SourceObservationCommand.create(
        source=source_object,
        revision=source_revision,
        provenance=source_contract_provenance(
            source_object, source_revision.revision_id, "event-1"
        ),
        idempotency_key=IdempotencyKey("policy-activation-source"),
    )
    source_context = postgresql_adapter.source_context(workspace_id, environment_id)
    async with postgresql_adapter.uow_factory(source_context) as uow:
        await uow.sources.record_observation(observation)
        await uow.commit()

    context = postgresql_adapter.ingestion_context(
        workspace_id, environment_id, "access_policy_activation"
    )
    audit_events = AuditEventRegistry.with_default_families()

    def evidence(
        *, audit_id: UUID, outbox_id: UUID, activation_id: UUID
    ) -> tuple[object, object]:
        audit = audit_events.build_event(
            audit_event_id=audit_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="access_policy.activate"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
            outcome=AuditOutcome.ACCEPTED,
            reason="policy_activated",
        )
        outbox = postgresql_adapter.outbox_events.build_intent(
            event_id=outbox_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            event_type="workspace.created",
            schema_version=1,
            payload=WorkspaceCreatedPayload(
                workspace=OpaqueObjectReference(
                    object_type="policy_activation",
                    object_id=activation_id,
                    schema_version=1,
                ),
                lifecycle_state="activated",
            ),
            trace_id=context.trace_id,
        )
        return audit, outbox

    first_policy = AccessPolicyVersion(
        access_policy_version_id=synthetic_uuid(3903),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object.source_object_id,
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.ELIGIBLE_TEMPLATE,
        origin_reference=synthetic_uuid(3904),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=synthetic_uuid(3905),
        created_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )
    first_policy = first_policy.model_copy(
        update={"document_hash": access_policy_document_hash(first_policy)}
    )
    first_activation_id = synthetic_uuid(3906)
    first_audit_id, first_outbox_id = synthetic_uuid(3907), synthetic_uuid(3908)
    first_audit, first_outbox = evidence(
        audit_id=first_audit_id,
        outbox_id=first_outbox_id,
        activation_id=first_activation_id,
    )
    first_command = AccessPolicyActivationCommand(
        policy=first_policy,
        requester_canonical_human_id=synthetic_uuid(3905),
        activator_canonical_human_id=synthetic_uuid(3905),
        expected_policy_version_id=None,
        expected_policy_generation=0,
        expected_cas_version=0,
        activation_id=first_activation_id,
        idempotency_key=IdempotencyKey("initial-policy-activation"),
        audit_event_id=first_audit_id,
        outbox_event_id=first_outbox_id,
        activated_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        audit_event=first_audit,
        outbox_intent=first_outbox,
    )
    first_draft = PolicyActivationDraft(
        values={
            "policy_activation_id": first_activation_id,
            "workspace_id": workspace_id,
            "environment_id": environment_id,
            "source_object_id": source_object.source_object_id,
            "predecessor_policy_version_id": None,
            "new_policy_version_id": first_policy.access_policy_version_id,
            "policy_generation": 1,
            "policy_proposal_id": None,
            "policy_proposal_version": None,
            "policy_proposal_hash": None,
            "policy_approval_id": None,
            "policy_approval_decision": None,
            "initial_eligibility_decision_id": synthetic_uuid(3909),
            "requester_canonical_human_id": synthetic_uuid(3905),
            "activator_canonical_human_id": synthetic_uuid(3905),
            "membership_generation": 1,
            "authority_generation": 1,
            "service_authority_generation": 1,
            "audit_event_id": first_audit_id,
            "outbox_event_id": first_outbox_id,
            "activated_at": datetime(2026, 10, 12, tzinfo=timezone.utc),
        }
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        assert await uow.access_policies.resolve_activation_result(first_command) is None
        rolled_back = await uow.access_policies.activate(first_command, first_draft)
        await uow.rollback()

    async with postgresql_adapter.uow_factory(context) as uow:
        assert await uow.access_policies.resolve_activation_result(first_command) is None
        first = await uow.access_policies.activate(first_command, first_draft)
        await uow.commit()

    assert first.activation.policy_activation_id == rolled_back.activation.policy_activation_id

    second_policy = AccessPolicyVersion(
        access_policy_version_id=synthetic_uuid(3910),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object.source_object_id,
        version=2,
        predecessor_policy_version_id=first_policy.access_policy_version_id,
        document_schema_version="access-policy.v1",
        document_hash="c" * 64,
        origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
        origin_reference=synthetic_uuid(3911),
        origin_version=1,
        origin_hash="d" * 64,
        created_by_canonical_human_id=synthetic_uuid(3905),
        created_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )
    second_policy = second_policy.model_copy(
        update={"document_hash": access_policy_document_hash(second_policy)}
    )
    proposal = PolicyProposal(
        policy_proposal_id=synthetic_uuid(3912),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object.source_object_id,
        proposal_version=1,
        proposed_policy_version_id=second_policy.access_policy_version_id,
        proposed_policy_hash=second_policy.document_hash,
        requester_canonical_human_id=synthetic_uuid(3905),
        expected_policy_version_id=first_policy.access_policy_version_id,
        expected_policy_generation=1,
        benefit_classification=PolicyBenefitClassification.REVOCATION,
        benefit_schema_version="policy-benefit.v1",
        membership_generation=1,
        authority_generation=1,
        service_authority_generation=1,
        submitted_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
    )
    second_activation_id = synthetic_uuid(3913)
    second_audit_id, second_outbox_id = synthetic_uuid(3914), synthetic_uuid(3915)
    second_audit, second_outbox = evidence(
        audit_id=second_audit_id,
        outbox_id=second_outbox_id,
        activation_id=second_activation_id,
    )
    second_command = AccessPolicyActivationCommand(
        policy=second_policy,
        requester_canonical_human_id=synthetic_uuid(3905),
        activator_canonical_human_id=synthetic_uuid(3905),
        expected_policy_version_id=first_policy.access_policy_version_id,
        expected_policy_generation=1,
        expected_cas_version=1,
        activation_id=second_activation_id,
        idempotency_key=IdempotencyKey("successor-policy-activation"),
        audit_event_id=second_audit_id,
        outbox_event_id=second_outbox_id,
        activated_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        audit_event=second_audit,
        outbox_intent=second_outbox,
        proposal=proposal,
    )
    second_draft = PolicyActivationDraft(
        values={
            **first_draft.values,
            "policy_activation_id": second_activation_id,
            "predecessor_policy_version_id": first_policy.access_policy_version_id,
            "new_policy_version_id": second_policy.access_policy_version_id,
            "policy_generation": 2,
            "policy_proposal_id": proposal.policy_proposal_id,
            "policy_proposal_version": proposal.proposal_version,
            "policy_proposal_hash": proposal.proposed_policy_hash,
            "initial_eligibility_decision_id": None,
            "audit_event_id": second_audit_id,
            "outbox_event_id": second_outbox_id,
        }
    )
    third_policy = second_policy.model_copy(
        update={
            "access_policy_version_id": synthetic_uuid(3917),
            "origin_reference": synthetic_uuid(3918),
            "document_hash": "e" * 64,
        }
    )
    third_policy = third_policy.model_copy(
        update={"document_hash": access_policy_document_hash(third_policy)}
    )
    third_proposal = proposal.model_copy(
        update={
            "policy_proposal_id": synthetic_uuid(3919),
            "proposed_policy_version_id": third_policy.access_policy_version_id,
            "proposed_policy_hash": third_policy.document_hash,
        }
    )
    third_activation_id = synthetic_uuid(3920)
    third_audit_id, third_outbox_id = synthetic_uuid(3921), synthetic_uuid(3922)
    third_audit, third_outbox = evidence(
        audit_id=third_audit_id,
        outbox_id=third_outbox_id,
        activation_id=third_activation_id,
    )
    third_command = replace(
        second_command,
        policy=third_policy,
        proposal=third_proposal,
        activation_id=third_activation_id,
        idempotency_key=IdempotencyKey("concurrent-successor-policy-activation"),
        audit_event_id=third_audit_id,
        outbox_event_id=third_outbox_id,
        audit_event=third_audit,
        outbox_intent=third_outbox,
    )
    third_draft = PolicyActivationDraft(
        values={
            **second_draft.values,
            "policy_activation_id": third_activation_id,
            "new_policy_version_id": third_policy.access_policy_version_id,
            "policy_proposal_id": third_proposal.policy_proposal_id,
            "policy_proposal_hash": third_proposal.proposed_policy_hash,
            "audit_event_id": third_audit_id,
            "outbox_event_id": third_outbox_id,
        }
    )

    async def activate(
        command: AccessPolicyActivationCommand,
        draft: PolicyActivationDraft,
    ) -> object:
        async with postgresql_adapter.uow_factory(context) as uow:
            assert await uow.access_policies.resolve_activation_result(command) is None
            result = await uow.access_policies.activate(command, draft)
            await uow.commit()
            return result

    outcomes = await asyncio.gather(
        activate(second_command, second_draft),
        activate(third_command, third_draft),
        return_exceptions=True,
    )

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(
        isinstance(outcome, StalePolicyBaselineError) for outcome in outcomes
    ) == 1

    async with postgresql_adapter.uow_factory(context) as uow:
        replay = await uow.access_policies.resolve_activation_result(first_command)
        await uow.rollback()

    assert replay is not None and replay.replay is True
    assert replay.activation == first.activation
    assert replay.current == first.current

    conflicting_replay = replace(first_command, outbox_event_id=synthetic_uuid(3916))
    async with postgresql_adapter.uow_factory(context) as uow:
        with pytest.raises(IdempotencyConflictError):
            await uow.access_policies.resolve_activation_result(conflicting_replay)


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


async def test_workflow_outbox_failure_rolls_back_work_item_run_and_receipt(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    workspace_id = synthetic_uuid(63800)
    environment_id = synthetic_uuid(63801)
    actor_id = synthetic_uuid(63802)
    command_id = synthetic_uuid(63803)
    trace_id = synthetic_uuid(63806)
    version_id = synthetic_uuid(63807)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(63808),
        secret=b"issue-638-workflow-rollback-context-secret",
    )
    context = boundary.interactive(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        acting_subject_id=actor_id,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow.run.start"),
        trace_id=trace_id,
    )
    events = create_outbox_event_registry()
    register_workflow_run_outbox_event(events)
    seed_persistence = PostgreSQLPersistence(
        session_factory=session_factory,
        context_verifier=boundary,
        outbox_events=events,
    )
    setup_context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=actor_id,
        purpose=PersistencePurpose("contract_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=trace_id,
    )
    try:
        async with seed_persistence.uow_factory(setup_context) as uow:
            await uow.workspaces.add(
                Workspace(
                    id=workspace_id,
                    slug="issue-638-workflow-rollback",
                    display_name="Issue 638 rollback workspace",
                )
            )
            await uow.environments.add(
                Environment(
                    id=environment_id,
                    workspace_id=workspace_id,
                    kind=EnvironmentKind.PRODUCTION,
                    display_name="Issue 638 environment",
                )
            )
            await uow.commit()

        failing_persistence = PostgreSQLPersistence(
            session_factory=lambda: FailureInjectingSession(
                session_factory(), "during_flush"
            ),  # type: ignore[arg-type]
            context_verifier=boundary,
            outbox_events=events,
        )
        service = PersistedWorkflowRunAdmissionService(failing_persistence.uow_factory)
        command = StartWorkflowCommand(
            command_id=command_id,
            idempotency_key="issue-638-workflow-rollback",
            workspace_id=workspace_id,
            environment_id=environment_id,
            actor=ActorRef(kind=ActorKind.HUMAN, id=actor_id),
            environment=EnvironmentKind.PRODUCTION,
            workflow_key="commercial.requirements",
            workflow_version_id=version_id,
            workflow_version="1.0.0",
            work_item_key="issue-638-workflow-rollback",
            client_request=ClientRequestV1(message="Prepare a proposal"),
            trace_id=trace_id,
            correlation_id=trace_id,
            causation_id=command_id,
        )

        with pytest.raises(PersistenceUnavailableError):
            await service.start(context=context, command=command)

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
                            "(SELECT count(*) FROM spine.work_items "
                            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
                            "AND work_item_key = :work_item_key), "
                            "(SELECT count(*) FROM spine.workflow_runs "
                            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
                            "AND command_id = :command_id), "
                            "(SELECT count(*) FROM spine.idempotency_receipts "
                            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
                            "AND idempotency_key = :idempotency_key), "
                            "(SELECT count(*) FROM spine.outbox_intents "
                            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
                            "AND event_type = 'workflow.run-started' "
                            "AND payload->>'command_id' = :command_id_text)"
                        ),
                        {
                            "workspace_id": workspace_id,
                            "environment_id": environment_id,
                            "work_item_key": command.work_item_key,
                            "command_id": command_id,
                            "command_id_text": str(command_id),
                            "idempotency_key": command.idempotency_key,
                        },
                    )
                ).one()
            assert tuple(counts) == (0, 0, 0, 0)
        finally:
            await migration_engine.dispose()
    finally:
        await engine.dispose()


async def test_workflow_add_context_replays_and_restores_events_from_postgresql(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(63900)
    environment_id = synthetic_uuid(63901)
    actor_id = synthetic_uuid(959)
    version_id = synthetic_uuid(63904)
    start_id = synthetic_uuid(63905)
    continuation_id = synthetic_uuid(63906)
    setup_context = postgresql_adapter.workspace_context(workspace_id)
    async with postgresql_adapter.uow_factory(setup_context) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="issue-639-workflow-command",
                display_name="Issue 639 workflow command",
            )
        )
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Issue 639 environment",
            )
        )
        await uow.commit()
    service = PersistedWorkflowRunAdmissionService(postgresql_adapter.uow_factory)
    assert postgresql_adapter.ingestion_context is not None
    start_context = postgresql_adapter.ingestion_context(
        workspace_id, environment_id, "workflow.run.start"
    )
    trace_id = start_context.trace_id
    start = StartWorkflowCommand(
        command_id=start_id,
        idempotency_key="issue-639-start",
        workspace_id=workspace_id,
        environment_id=environment_id,
        actor=ActorRef(kind=ActorKind.SERVICE, id=actor_id),
        environment=EnvironmentKind.PRODUCTION,
        workflow_key="commercial.requirements",
        workflow_version_id=version_id,
        workflow_version="1.0.0",
        work_item_key="issue-639-work-item",
        client_request=ClientRequestV1(message="Prepare proposal requirements"),
        trace_id=trace_id,
        correlation_id=start_id,
        causation_id=start_id,
    )
    started = await service.start(context=start_context, command=start)
    command_context = postgresql_adapter.ingestion_context(
        workspace_id, environment_id, "workflow.run.command"
    )
    continuation = WorkflowCommand(
        command_id=continuation_id,
        idempotency_key="issue-639-context",
        run_id=started.run_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        actor=ActorRef(kind=ActorKind.SERVICE, id=actor_id),
        command_type="add_context",
        command_schema_version=1,
        expected_run_version=1,
        payload=AddContextCommandV1(message="Delivery deadline is December 20."),
        trace_id=trace_id,
        correlation_id=start_id,
        causation_id=start_id,
    )

    accepted = await service.dispatch(context=command_context, command=continuation)
    replayed = await service.dispatch(context=command_context, command=continuation)
    events = await service.list_events(
        context=command_context,
        run_id=started.run_id,
        after_sequence=None,
        limit=20,
    )

    assert accepted is not None and accepted.run_version == 2
    assert replayed is not None and replayed.admission == "replayed"
    assert events is not None
    assert [item.event_type for item in events.items] == [
        "workflow.run-started",
        "workflow.context-added",
    ]
    assert all("deadline" not in item.summary.lower() for item in events.items)
    with pytest.raises(OptimisticConflictError):
        await service.dispatch(
            context=command_context,
            command=continuation.model_copy(
                update={
                    "command_id": synthetic_uuid(63907),
                    "idempotency_key": "issue-639-stale",
                }
            ),
        )


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


async def test_environment_uow_rejects_expired_service_qualification(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    from spine.application.persistence.errors import InvalidPersistenceContextError

    workspace_id = synthetic_uuid(54100)
    environment_id = synthetic_uuid(54101)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    workspace_context = postgresql_adapter.workspace_context(workspace_id)
    async with postgresql_adapter.uow_factory(workspace_context) as uow:
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Expired qualification",
            )
        )
        await uow.commit()

    migration_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.qualification_authority_bindings ("
                    "binding_id, version, qualification_id, workspace_id, environment_id, "
                    "principal_kind, principal_id, purpose, operation, expires_at, status, "
                    "change_id, recorded_by) VALUES ("
                    ":binding_id, 1, :qualification_id, :workspace_id, :environment_id, "
                    "'service', :principal_id, 'contract_test', 'environment_repository', "
                    "transaction_timestamp(), 'active', :change_id, 'uow-expiry-test')"
                ),
                {
                    "binding_id": synthetic_uuid(54102),
                    "qualification_id": synthetic_uuid(54103),
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                    "principal_id": synthetic_uuid(953),
                    "change_id": synthetic_uuid(54104),
                },
            )
    finally:
        await migration_engine.dispose()

    context = postgresql_adapter.environment_context(workspace_id, environment_id)
    with pytest.raises(InvalidPersistenceContextError):
        async with postgresql_adapter.uow_factory(context):
            pass

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


def _scoped_context_bundle(
    workspace_id: UUID,
    environment_id: UUID,
    profile_ref: ContextProfileRef,
    **overrides: object,
) -> ContextBundle:
    base = context_bundle(profile_ref=profile_ref)
    values = base.model_dump(mode="python", exclude={"bundle_digest"})
    values.update(
        {
            "workspace_id": workspace_id,
            "environment_id": environment_id,
            "projection_snapshot": base.projection_snapshot.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                }
            ),
            "projection_activation": base.projection_activation.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                }
            ),
            "canonical_boundary": base.canonical_boundary.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                }
            ),
            "projection_config_version": base.projection_config_version.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                }
            ),
        }
    )
    if "evidence_references" not in overrides:
        values["evidence_references"] = tuple(
            evidence.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "environment_id": environment_id,
                    "projection_snapshot_id": base.projection_snapshot.projection_snapshot_id,
                    "activation_generation": base.activation_generation,
                }
            )
            for evidence in base.evidence_references
        )
    values.update(overrides)
    return ContextBundle.create(**values)


@pytest.mark.asyncio
async def test_postgresql_context_bundle_replays_isolates_and_safe_reads(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(9901)
    environment_id = synthetic_uuid(9902)
    await persist_context_profile_scope(
        postgresql_adapter, workspace_id, environment_id
    )
    stored_profile = context_profile(
        workspace_id,
        environment_id,
        profile_id=UUID(int=3),
    )
    context = postgresql_adapter.environment_context(workspace_id, environment_id)

    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.context_profiles.create(stored_profile)
        bundle = _scoped_context_bundle(
            workspace_id,
            environment_id,
            ContextProfileRef(
                profile_id=stored_profile.profile_id,
                profile_version=stored_profile.profile_version,
                profile_digest=stored_profile.profile_digest,
            ),
        )
        assert await uow.context_bundles.create(bundle) == bundle
        assert await uow.context_bundles.create(bundle) == bundle
        await uow.commit()

    with pytest.raises(ContextBundleConflictError):
        async with postgresql_adapter.uow_factory(context) as uow:
            await uow.context_bundles.create(
                _scoped_context_bundle(
                    workspace_id,
                    environment_id,
                    bundle_id=UUID(int=99),
                    profile_ref=bundle.profile_ref,
                    evidence_references=(),
                    policy_decision_generations=(),
                    outcome=ContextBundleOutcome.INSUFFICIENT,
                    reason_code="insufficient_evidence",
                )
            )

    async with postgresql_adapter.uow_factory(context) as uow:
        assert (
            await uow.context_bundles.resolve_attempt(
                run_id=bundle.run_id,
                attempt_id=bundle.attempt_id,
                idempotency_key=bundle.idempotency_key,
            )
        ) == bundle

    cancelled = _scoped_context_bundle(
        workspace_id,
        environment_id,
        bundle_id=UUID(int=98),
        profile_ref=bundle.profile_ref,
        run_id=UUID(int=97),
        attempt_id=UUID(int=96),
        idempotency_key="cancelled-attempt",
        evidence_references=(),
        policy_decision_generations=(),
        outcome=ContextBundleOutcome.CANCELLED,
        state=ContextBundleState.CANCELLED,
        reason_code="cancelled",
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.context_bundles.create(cancelled)
        await uow.commit()
    async with postgresql_adapter.uow_factory(context) as uow:
        assert (
            await uow.context_bundles.resolve_attempt(
                run_id=cancelled.run_id,
                attempt_id=cancelled.attempt_id,
                idempotency_key=cancelled.idempotency_key,
            )
        ) == cancelled

    degraded = _scoped_context_bundle(
        workspace_id,
        environment_id,
        bundle_id=UUID(int=95),
        profile_ref=bundle.profile_ref,
        run_id=UUID(int=94),
        attempt_id=UUID(int=93),
        idempotency_key="degraded-attempt",
        outcome=ContextBundleOutcome.DEGRADED,
        reason_code="stale_evidence",
        evidence_references=(
            ContextBundleEvidenceReference(
                evidence_id="stale-evidence",
                workspace_id=workspace_id,
                environment_id=environment_id,
                projection_snapshot_id=bundle.projection_snapshot.projection_snapshot_id,
                activation_generation=bundle.activation_generation,
                source_object_id=UUID(int=20),
                source_revision_id=UUID(int=21),
                locator={"kind": "page", "value": "1"},
                evidence_digest="7" * 64,
                status="stale",
            ),
        ),
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.context_bundles.create(degraded)
        await uow.commit()
    with pytest.raises(ContextBundleEvidenceUnavailableError):
        async with postgresql_adapter.uow_factory(context) as uow:
            await uow.context_bundles.resolve_attempt(
                run_id=degraded.run_id,
                attempt_id=degraded.attempt_id,
                idempotency_key=degraded.idempotency_key,
            )

    foreign_context = postgresql_adapter.environment_context(
        synthetic_uuid(9903), synthetic_uuid(9904)
    )
    await persist_context_profile_scope(
        postgresql_adapter, synthetic_uuid(9903), synthetic_uuid(9904)
    )
    with pytest.raises(ContextBundleNotFoundError):
        async with postgresql_adapter.uow_factory(foreign_context) as uow:
            await uow.context_bundles.resolve_attempt(
                run_id=bundle.run_id,
                attempt_id=bundle.attempt_id,
                idempotency_key=bundle.idempotency_key,
            )


@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_postgresql_projection_runs_resolve_by_canonical_source_revision(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 48000)
    async with postgresql_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()

    boundary_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "canonical_boundary_capture"
    )
    async with postgresql_adapter.uow_factory(boundary_context) as uow:
        captured = await uow.boundaries.capture(
            CanonicalBoundaryCaptureCommand.create(
                projection_kind="context",
                scope_version="r1",
                idempotency_key=IdempotencyKey("projection-boundary-48000"),
            )
        )
        await uow.commit()

    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    ontology_payload = {
        "schema_version": "ontology-definition:r1",
        "entity_types": [{"name": "document"}],
        "relation_types": [],
    }
    ontology_digest = hashlib.sha256(
        json.dumps(
            {"schema": "ontology-definition:r1", "payload": ontology_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    ontology = OntologyVersion(
        ontology_version_id=synthetic_uuid(48040),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        ontology_name="qualification",
        version=1,
        definition_payload=ontology_payload,
        definition_digest=ontology_digest,
        publication_eligible=True,
        publication_decision_reference="decision-48000",
        publisher_authority_reference="authority-48000",
        audit_event_reference="audit-48000",
        published_at=now,
    )
    config_payload = {"adapter": "context-projection:v1"}
    config_digest = hashlib.sha256(
        json.dumps(
            {"schema": "projection-config:r1", "payload": config_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    config = ProjectionConfigVersion(
        projection_config_version_id=synthetic_uuid(48041),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_kind="context",
        version=1,
        ontology_version_id=ontology.ontology_version_id,
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="context-projection:v1",
        config_payload=config_payload,
        config_digest=config_digest,
        created_at=now,
    )
    run = ProjectionRun(
        projection_run_id=synthetic_uuid(48042),
        projection_candidate_id=synthetic_uuid(48043),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_kind="context",
        operation=ProjectionOperation.BUILD,
        projection_config_version_id=config.projection_config_version_id,
        canonical_boundary_id=captured.boundary.boundary_id,
        command_identity="projection-run-48000",
        command_digest="4" * 64,
        idempotency_receipt_id=synthetic_uuid(48044),
        started_at=now,
    )
    projection_context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_run"
    )
    async with postgresql_adapter.uow_factory(projection_context) as uow:
        await uow.projections.add_ontology_version(ontology)
        await uow.projections.add_config_version(config)
        await uow.projections.create_run(
            ProjectionRunCommand.create(
                run=run,
                idempotency_key=IdempotencyKey("projection-run-48000"),
            )
        )
        await uow.projections.add_partition(
            ProjectionPartition(
                projection_partition_id=synthetic_uuid(48045),
                projection_run_id=run.projection_run_id,
                projection_candidate_id=run.projection_candidate_id,
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                security_domain_reference="security-domain:qualification",
                security_domain_version="r1",
                partition_identity="default",
                expected_command_identity="partition-command-48000",
                expected_command_digest="5" * 64,
                created_at=now,
            )
        )
        await uow.commit()

    async with postgresql_adapter.uow_factory(projection_context) as uow:
        runs = await uow.projections.list_runs_for_source_revision(
            case.source.source_object_id, case.revision.revision_id
        )
        assert len(runs) == 1
        assert runs[0].projection_run_id == run.projection_run_id
        assert runs[0].projection_candidate_id == run.projection_candidate_id
        assert runs[0].canonical_boundary_id == run.canonical_boundary_id
        assert runs[0].command_identity == run.command_identity
        assert runs[0].command_digest == run.command_digest
        partitions = await uow.projections.list_partitions(run.projection_run_id)
        assert len(partitions) == 1
        await uow.rollback()


async def test_postgresql_answer_question_run_create_and_replay_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_create_replay(postgresql_adapter)


async def test_postgresql_answer_question_run_conflicting_key_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_conflicting_key(postgresql_adapter)


async def test_postgresql_answer_question_attempt_immutability_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_attempt_immutable(postgresql_adapter)


async def test_postgresql_answer_question_attempt_allocation_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_attempt_allocation(postgresql_adapter)


async def test_postgresql_answer_question_attempt_allocation_rejects_cancellation(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_attempt_allocation_cancelled(postgresql_adapter)


async def test_postgresql_answer_question_context_bundle_reference_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_context_bundle(postgresql_adapter)


async def test_postgresql_answer_question_concurrent_completion_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_concurrent_completion(postgresql_adapter)


async def test_postgresql_answer_question_run_isolation_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_isolation(postgresql_adapter)


async def test_postgresql_ask_admission_replays_persisted_run_message_and_rejects_conflicts(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await exercise_ask_admission_contract(postgresql_adapter)


async def test_postgresql_ask_admission_replays_after_application_process_recreation(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    assert postgresql_adapter.ingestion_context is not None
    workspace_id = synthetic_uuid(43200)
    environment_id = synthetic_uuid(43201)
    session_id = synthetic_uuid(43202)
    subject_id = synthetic_uuid(959)
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)

    async with postgresql_adapter.uow_factory(
        postgresql_adapter.workspace_context(workspace_id)
    ) as uow:
        await uow.workspaces.add(
            Workspace(id=workspace_id, slug="ask-recovery", display_name="Ask recovery")
        )
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()

    session_context = postgresql_adapter.ingestion_context(
        workspace_id, environment_id, "qa_sessions"
    )
    async with postgresql_adapter.uow_factory(session_context) as uow:
        await uow.qa_sessions.create_session(
            QASession(
                session_id=session_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                subject_id=subject_id,
                created_at=now,
                updated_at=now,
                version=1,
                status="active",
            ),
            command_id=synthetic_uuid(43203),
            idempotency_key=IdempotencyKey("issue-430-process-restart-session"),
            digest=digest_command(
                operation=PersistenceOperation("qa_sessions"),
                operation_schema_version=1,
                payload={"session_id": session_id, "subject_id": subject_id},
            ),
        )
        await uow.commit()

    ask_context = postgresql_adapter.ingestion_context(
        workspace_id, environment_id, QA_ASK_ADMISSION_OPERATION.value
    )
    command = AskAdmissionCommand.create(
        workspace_id=workspace_id,
        environment_id=environment_id,
        session_id=session_id,
        subject=ActorRef(kind=ActorKind.SERVICE, id=subject_id),
        environment=EnvironmentKind.PRODUCTION,
        question="Restart-safe question",
        idempotency_key=IdempotencyKey("issue-430-process-restart-admission"),
        authorization_generation=1,
    )
    async with postgresql_adapter.uow_factory(ask_context) as uow:
        first = await uow.ask_admissions.admit(command)
        await uow.commit()

    async with postgresql_adapter.uow_factory(ask_context) as uow:
        admitted_run = await uow.answer_question_runs.resolve_run(first.run_id)
        assert admitted_run is not None
        cancellation_intent = await uow.answer_question_runs.request_cancellation(
            run_id=first.run_id,
            requested_at=admitted_run.created_at + timedelta(seconds=1),
        )
        await uow.commit()

    # A subprocess reconstructs trusted context and persistence from durable
    # configuration after the admission and cancellation commits.
    replay_script = r'''
import asyncio, json, sys
from uuid import UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from spine.application.persistence.ask_admission import AskAdmissionCommand, QA_ASK_ADMISSION_OPERATION
from spine.application.persistence.context import EnvironmentScope, PersistenceOperation, PersistencePurpose
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from tests.contracts.persistence.outbox_events import create_outbox_event_registry
from tests.contracts.persistence.ids import synthetic_uuid

async def main():
    url, workspace_id, environment_id, session_id, subject_id = sys.argv[1:]
    workspace_id, environment_id, session_id, subject_id = map(UUID, (workspace_id, environment_id, session_id, subject_id))
    engine = create_async_engine(url, pool_pre_ping=True, connect_args={"options": "-csearch_path=spine,public"})
    boundary = TrustedContextBoundary.for_testing(issuer_id=synthetic_uuid(950), secret=b"issue-45-postgresql-contract-secret")
    persistence = PostgreSQLPersistence(session_factory=async_sessionmaker(engine, expire_on_commit=False, autoflush=False), context_verifier=boundary, outbox_events=create_outbox_event_registry())
    context = boundary.worker(scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id), service_principal_id=synthetic_uuid(959), purpose=PersistencePurpose("contract_test"), operation=PersistenceOperation(QA_ASK_ADMISSION_OPERATION.value), trace_id=synthetic_uuid(960))
    command = AskAdmissionCommand.create(workspace_id=workspace_id, environment_id=environment_id, session_id=session_id, subject=ActorRef(kind=ActorKind.SERVICE, id=subject_id), environment=EnvironmentKind.PRODUCTION, question="Restart-safe question", idempotency_key=IdempotencyKey("issue-430-process-restart-admission"), authorization_generation=1)
    async with persistence.uow_factory(context) as uow:
        replay = await uow.ask_admissions.admit(command)
        run = await uow.answer_question_runs.resolve_run(replay.run_id)
        await uow.commit()
    print(json.dumps({"command_id": str(replay.command_id), "message_id": str(replay.message_id), "run_id": str(replay.run_id), "replay": replay.replay, "cancellation_requested_at": run.cancellation_requested_at.isoformat() if run and run.cancellation_requested_at else None}))
    await engine.dispose()

asyncio.run(main())
'''
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            replay_script,
            postgresql_contract_database.runtime_url.get_secret_value(),
            str(workspace_id),
            str(environment_id),
            str(session_id),
            str(subject_id),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0, child.stderr
    child_replay = json.loads(child.stdout)

    # This second adapter reconstruction independently checks the message and
    # outbox rows without relying on the subprocess's in-memory objects.
    restarted_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    restarted_boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(950),
        secret=b"issue-45-postgresql-contract-secret",
    )
    restarted_persistence = PostgreSQLPersistence(
        session_factory=async_sessionmaker(
            restarted_engine,
            expire_on_commit=False,
            autoflush=False,
        ),
        context_verifier=restarted_boundary,
        outbox_events=create_outbox_event_registry(),
    )
    try:
        async with restarted_persistence.uow_factory(ask_context) as uow:
            replay = await uow.ask_admissions.admit(command)
            run = await uow.answer_question_runs.resolve_run(replay.run_id)
            message = await uow.qa_sessions.resolve_message(
                workspace_id=workspace_id,
                environment_id=environment_id,
                session_id=session_id,
                message_id=replay.message_id,
            )
            await uow.commit()
    finally:
        await restarted_engine.dispose()

    inspection_engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with inspection_engine.connect() as connection:
            persisted_payload = await connection.scalar(
                text(
                    "SELECT payload::text FROM spine.outbox_intents "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": first.dispatch_event_id},
            )
            cancellation_event_result = await connection.execute(
                text(
                    "SELECT event_id, workspace_id, environment_id, event_type, "
                    "event_schema_version, aggregate_type, aggregate_id, "
                    "aggregate_schema_version, producer_deduplication_id, payload, "
                    "trace_id, correlation_id, causation_id "
                    "FROM spine.outbox_intents WHERE event_type = :event_type "
                    "AND payload ->> 'run_id' = :run_id"
                ),
                {
                    "event_type": QA_ASK_CANCELLATION_EVENT_TYPE,
                    "run_id": str(first.run_id),
                },
            )
            cancellation_event = cancellation_event_result.mappings().one()
    finally:
        await inspection_engine.dispose()

    assert child_replay["replay"] is True
    assert tuple(UUID(child_replay[name]) for name in ("command_id", "message_id", "run_id")) == (
        first.command_id,
        first.message_id,
        first.run_id,
    )
    assert replay.dispatch_event_id == first.dispatch_event_id
    assert run.state.value == "admitted"
    assert message is not None
    assert message.state is QAMessageState.PENDING
    assert message.question == "Restart-safe question"
    assert cancellation_intent.cancellation_requested_at == admitted_run.created_at + timedelta(seconds=1)
    assert run.cancellation_requested_at == cancellation_intent.cancellation_requested_at
    assert child_replay["cancellation_requested_at"] == cancellation_intent.cancellation_requested_at.isoformat()
    assert persisted_payload is not None
    assert "Restart-safe question" not in persisted_payload
    assert cancellation_event["payload"] == {
        "run_id": str(first.run_id),
        "schema_version": 1,
    }

    # Recover the exact committed PostgreSQL outbox event after application
    # process recreation and deliver it only after replacing the Temporal worker.
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from spine.application.authorized_ask import AuthorizedAskResult
    from spine.application.diagnostics import DiagnosticContext
    from spine.application.persistence.ask_admission import (
        AskCancellationDispatchPayload,
        register_ask_admission_outbox_event,
    )
    from spine.application.persistence.outbox import OpaqueObjectReference
    from spine.application.workflows.ask_admission_dispatch import (
        ASK_ADMISSION_WORKFLOW_TYPE,
        AskAdmissionWorkflowInput,
        AskCancellationDispatchConsumer,
    )
    from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
    from spine.workflows.temporal.definitions import (
        AskAdmissionWorkflowDefinition,
        build_ask_admission_activity_bindings,
    )
    from spine.workflows.temporal.client import TemporalSdkWorkflowClient
    from tests.api.test_authorized_ask import safe_output

    outbox_registry = create_outbox_event_registry()
    register_ask_admission_outbox_event(outbox_registry)
    persisted_intent = outbox_registry.build_intent(
        event_id=cancellation_event["event_id"],
        workspace_id=cancellation_event["workspace_id"],
        environment_id=cancellation_event["environment_id"],
        event_type=cancellation_event["event_type"],
        schema_version=cancellation_event["event_schema_version"],
        payload=AskCancellationDispatchPayload.model_validate(
            cancellation_event["payload"]
        ),
        aggregate=OpaqueObjectReference(
            object_type=cancellation_event["aggregate_type"],
            object_id=cancellation_event["aggregate_id"],
            schema_version=cancellation_event["aggregate_schema_version"],
        ),
        producer_deduplication_id=cancellation_event[
            "producer_deduplication_id"
        ],
        trace_id=cancellation_event["trace_id"],
        correlation_id=cancellation_event["correlation_id"],
        causation_id=cancellation_event["causation_id"],
    )
    workflow_input = AskAdmissionWorkflowInput(
        command_id=first.command_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        session_id=session_id,
        message_id=first.message_id,
        run_id=first.run_id,
        trace_id=cancellation_event["trace_id"],
        authorization_generation=1,
        correlation_id=cancellation_event["correlation_id"],
        causation_id=first.dispatch_event_id,
    )

    class RecoveredCancellationActivity:
        def __init__(self) -> None:
            self.execution_started = asyncio.Event()
            self.cancel_calls = 0

        async def execute(
            self, value: AskAdmissionWorkflowInput
        ) -> AuthorizedAskResult:
            assert value.run_id == first.run_id
            self.execution_started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled Temporal activity unexpectedly resumed")

        async def cancel(
            self, value: AskAdmissionWorkflowInput
        ) -> AuthorizedAskResult:
            self.cancel_calls += 1
            return AuthorizedAskResult(
                command_id=value.command_id,
                session_id=value.session_id,
                message_id=value.message_id,
                output=safe_output(AnswerQuestionOutcome.CANCELLED),
                accepted_answer=None,
            )

    recovered_activity = RecoveredCancellationActivity()
    execute_activity, cancel_activity = build_ask_admission_activity_bindings(
        recovered_activity
    )
    task_queue = "issue-430-persisted-cancellation-recovery"
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as temporal:
        handle = await temporal.client.start_workflow(
            ASK_ADMISSION_WORKFLOW_TYPE,
            workflow_input.model_dump(mode="json"),
            id=str(first.run_id),
            task_queue=task_queue,
            result_type=dict,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        async with Worker(
            temporal.client,
            task_queue=task_queue,
            identity="issue-430-before-persisted-cancel-recovery",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            await asyncio.wait_for(recovered_activity.execution_started.wait(), timeout=10)

        async with Worker(
            temporal.client,
            task_queue=task_queue,
            identity="issue-430-after-persisted-cancel-recovery",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            consumer = AskCancellationDispatchConsumer(
                TemporalSdkWorkflowClient(temporal.client, task_queue=task_queue)
            )
            await consumer.consume(
                persisted_intent,
                DiagnosticContext(
                    trace_id=persisted_intent.trace_id,
                    correlation_id=persisted_intent.correlation_id,
                    causation_id=persisted_intent.event_id,
                ),
            )
            terminal = await asyncio.wait_for(handle.result(), timeout=15)

    assert terminal["outcome"] == AnswerQuestionOutcome.CANCELLED.value
    assert recovered_activity.cancel_calls == 1


async def test_postgresql_terminal_persistence_retries_after_commit_failure(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    workspace_id = synthetic_uuid(43400)
    environment_id = synthetic_uuid(43401)
    run = qa_run_record(
        workspace_id,
        environment_id,
        run_id=synthetic_uuid(43402),
        created_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
    )
    command = qa_run_command(run)
    context = qa_context(postgresql_adapter, workspace_id, environment_id)
    await persist_qa_scope(postgresql_adapter, workspace_id, environment_id)
    started_at = datetime(2026, 10, 17, tzinfo=timezone.utc)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(43403),
        run=run,
        attempt_number=1,
        execution_pin=qa_execution_pin(workspace_id, environment_id),
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=started_at,
    )
    completed_attempt = attempt.complete(
        state=AnswerQuestionAttemptState.SUCCEEDED,
        outcome=AnswerQuestionOutcome.ABSTAINED,
        usage=AnswerQuestionUsage(),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(43404),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.COMPLETED,
            occurred_at=started_at + timedelta(minutes=1),
        ),
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(command)
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.answer_question_runs.complete_attempt(completed_attempt)
        await uow.commit()
    async with postgresql_adapter.uow_factory(context) as uow:
        persisted_run = await uow.answer_question_runs.resolve_run(run.run_id)
        persisted_attempts = await uow.answer_question_runs.list_attempts(run.run_id)
    assert persisted_run is not None
    persisted_attempt = persisted_attempts[0]
    terminal_run = persisted_run.complete(
        outcome=AnswerQuestionOutcome.ABSTAINED,
        final_attempt_id=persisted_attempt.attempt_id,
        completed_at=persisted_attempt.completed_at,
    )

    failed_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    failed_session_factory = async_sessionmaker(
        failed_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    failed_persistence = PostgreSQLPersistence(
        session_factory=lambda: FailureInjectingSession(
            failed_session_factory(), "during_commit"
        ),  # type: ignore[arg-type]
        context_verifier=TrustedContextBoundary.for_testing(
            issuer_id=synthetic_uuid(950),
            secret=b"issue-45-postgresql-contract-secret",
        ),
        outbox_events=create_outbox_event_registry(),
    )
    try:
        with pytest.raises(UnexpectedPersistenceError):
            async with failed_persistence.uow_factory(context) as uow:
                await uow.answer_question_runs.complete_run(terminal_run)
                await uow.commit()
    finally:
        await failed_engine.dispose()

    async with postgresql_adapter.uow_factory(context) as uow:
        after_failed_commit = await uow.answer_question_runs.resolve_run(run.run_id)
    assert after_failed_commit.state.value == "running"

    restarted_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    restarted_persistence = PostgreSQLPersistence(
        session_factory=async_sessionmaker(
            restarted_engine,
            expire_on_commit=False,
            autoflush=False,
        ),
        context_verifier=TrustedContextBoundary.for_testing(
            issuer_id=synthetic_uuid(950),
            secret=b"issue-45-postgresql-contract-secret",
        ),
        outbox_events=create_outbox_event_registry(),
    )
    try:
        async with restarted_persistence.uow_factory(context) as uow:
            first_persist = await uow.answer_question_runs.complete_run(terminal_run)
            await uow.commit()
        async with restarted_persistence.uow_factory(context) as uow:
            replay = await uow.answer_question_runs.complete_run(terminal_run)
            persisted = await uow.answer_question_runs.resolve_run(run.run_id)
    finally:
        await restarted_engine.dispose()

    assert first_persist == terminal_run
    assert replay == terminal_run
    assert persisted == terminal_run


async def test_postgresql_terminal_retry_keeps_run_message_and_accepted_disclosure_consistent(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    from spine.domain.agents import AgentResult, AgentRunStatus
    from spine.domain.capabilities import AgentResultRef
    from spine.domain.context_bundles import ContextBundleRef
    from tests.api.test_authorized_ask import (
        ATTEMPT_ID,
        ENVIRONMENT_ID as ASK_ENVIRONMENT_ID,
        MESSAGE_ID,
        RUN_ID,
        SESSION_ID,
        WORKSPACE_ID as ASK_WORKSPACE_ID,
        answered_result,
    )

    accepted = answered_result().accepted_answer
    assert accepted is not None
    workspace_id = ASK_WORKSPACE_ID
    environment_id = ASK_ENVIRONMENT_ID
    context = qa_context(postgresql_adapter, workspace_id, environment_id)
    assert postgresql_adapter.ingestion_context is not None
    session_context = postgresql_adapter.ingestion_context(
        workspace_id, environment_id, "qa_sessions"
    )
    await persist_qa_scope(postgresql_adapter, workspace_id, environment_id)
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    profile = context_profile(
        workspace_id,
        environment_id,
        profile_id=synthetic_uuid(43423),
    )
    profile_ref = ContextProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
    )
    bundle = _scoped_context_bundle(
        workspace_id,
        environment_id,
        profile_ref,
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        idempotency_key="issue-430-terminal-retry-context",
        request_id=synthetic_uuid(43424),
    )
    run = qa_run_record(
        workspace_id,
        environment_id,
        run_id=RUN_ID,
        created_at=now,
    )
    attempt = AnswerQuestionAttempt.start(
        attempt_id=ATTEMPT_ID,
        run=run,
        attempt_number=1,
        execution_pin=qa_execution_pin(workspace_id, environment_id),
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=now,
    )
    completed_attempt = attempt.complete(
        state=AnswerQuestionAttemptState.SUCCEEDED,
        outcome=AnswerQuestionOutcome.ANSWERED,
        context_bundle_ref=ContextBundleRef(
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.bundle_version,
            bundle_digest=bundle.bundle_digest,
        ),
        output_artifact_ids=(synthetic_uuid(43421),),
        agent_result_ref=AgentResultRef(result_id=synthetic_uuid(43422)),
        agent_result=AgentResult(
            status=AgentRunStatus.SUCCEEDED,
            output_artifact_ids=(synthetic_uuid(43421),),
        ),
        usage=AnswerQuestionUsage(),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(43414),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.COMPLETED,
            occurred_at=now + timedelta(minutes=1),
        ),
    )
    terminal_run = run.complete(
        outcome=AnswerQuestionOutcome.ANSWERED,
        final_attempt_id=attempt.attempt_id,
        output_artifact_ids=completed_attempt.output_artifact_ids,
        completed_at=completed_attempt.completed_at,
    )
    session = QASession(
        session_id=SESSION_ID,
        workspace_id=workspace_id,
        environment_id=environment_id,
        subject_id=synthetic_uuid(43415),
        created_at=now,
        updated_at=now,
        version=1,
        status=QASessionStatus.ACTIVE,
    )
    message = QAMessage(
        message_id=MESSAGE_ID,
        session_id=SESSION_ID,
        workspace_id=workspace_id,
        environment_id=environment_id,
        version=1,
        ordinal=1,
        trace_id=run.diagnostic_context.trace_id,
        run_id=RUN_ID,
        question="Which policy applies?",
        state=QAMessageState.RUNNING,
        created_at=now,
        updated_at=now,
    )
    disclosure = AnswerDisclosureRecord(
        request_id=synthetic_uuid(43416),
        request_digest="a" * 64,
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        outcome=AnswerQuestionOutcome.ANSWERED,
        validation=accepted.validation,
        accepted_answer=accepted,
        created_at=now + timedelta(minutes=1),
    )
    session_digest = digest_command(
        operation=PersistenceOperation("qa_sessions"),
        operation_schema_version=1,
        payload={"session_id": SESSION_ID, "subject_id": session.subject_id},
    )
    message_digest = digest_command(
        operation=PersistenceOperation("qa_sessions"),
        operation_schema_version=1,
        payload={"session_id": SESSION_ID, "message_id": MESSAGE_ID, "run_id": RUN_ID},
    )
    completion_digest = digest_command(
        operation=PersistenceOperation("qa_sessions"),
        operation_schema_version=1,
        payload={
            "session_id": SESSION_ID,
            "message_id": MESSAGE_ID,
            "answer": accepted.answer.text,
            "outcome": AnswerQuestionOutcome.ANSWERED.value,
        },
    )

    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.context_profiles.create(profile)
        await uow.context_bundles.create(bundle)
        await uow.answer_question_runs.create_run(qa_run_command(run))
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.answer_question_runs.complete_attempt(completed_attempt)
        await uow.commit()
    async with postgresql_adapter.uow_factory(context) as uow:
        persisted_running_run = await uow.answer_question_runs.resolve_run(RUN_ID)
        persisted_attempts = await uow.answer_question_runs.list_attempts(RUN_ID)
    assert persisted_running_run is not None
    persisted_attempt = persisted_attempts[0]
    terminal_run = persisted_running_run.complete(
        outcome=AnswerQuestionOutcome.ANSWERED,
        final_attempt_id=persisted_attempt.attempt_id,
        output_artifact_ids=persisted_attempt.output_artifact_ids,
        completed_at=persisted_attempt.completed_at,
    )
    async with postgresql_adapter.uow_factory(session_context) as uow:
        await uow.qa_sessions.create_session(
            session,
            command_id=synthetic_uuid(43417),
            idempotency_key=IdempotencyKey("issue-430-terminal-session"),
            digest=session_digest,
        )
        await uow.qa_sessions.create_pending_message(
            message,
            command_id=synthetic_uuid(43418),
            idempotency_key=IdempotencyKey("issue-430-terminal-message"),
            digest=message_digest,
        )
        await uow.commit()

    failed_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    failed_session_factory = async_sessionmaker(
        failed_engine, expire_on_commit=False, autoflush=False
    )
    failed_boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(950),
        secret=b"issue-45-postgresql-contract-secret",
    )
    failed_context = failed_boundary.worker(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        service_principal_id=synthetic_uuid(959),
        purpose=PersistencePurpose("contract_test"),
        operation=PersistenceOperation("qa_sessions"),
        trace_id=synthetic_uuid(960),
    )
    failed_persistence = PostgreSQLPersistence(
        session_factory=lambda: FailureInjectingSession(
            failed_session_factory(), "during_commit"
        ),  # type: ignore[arg-type]
        context_verifier=failed_boundary,
        outbox_events=create_outbox_event_registry(),
    )
    try:
        with pytest.raises(UnexpectedPersistenceError):
            async with failed_persistence.uow_factory(failed_context) as uow:
                await uow.answer_question_runs.complete_run(terminal_run)
                await uow.answer_disclosures.save(disclosure)
                await uow.qa_sessions.complete_message(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    session_id=SESSION_ID,
                    message_id=MESSAGE_ID,
                    answer=accepted.answer.text,
                    outcome=AnswerQuestionOutcome.ANSWERED,
                    cancelled=False,
                    completed_at=completed_attempt.completed_at,
                    command_id=synthetic_uuid(43419),
                    idempotency_key=IdempotencyKey("issue-430-terminal-message-result"),
                    disclosure_dependencies=None,
                    digest=completion_digest,
                )
                await uow.commit()
    finally:
        await failed_engine.dispose()

    async with postgresql_adapter.uow_factory(session_context) as uow:
        after_failure_run = await uow.answer_question_runs.resolve_run(RUN_ID)
        after_failure_message = await uow.qa_sessions.resolve_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
        )
        after_failure_disclosure = await uow.answer_disclosures.resolve(
            workspace_id=workspace_id,
            environment_id=environment_id,
            request_id=disclosure.request_id,
        )
    receipt_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        async with receipt_engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text("SELECT set_config('spine.workspace_id', :value, true)"),
                    {"value": str(workspace_id)},
                )
                await connection.execute(
                    text("SELECT set_config('spine.environment_id', :value, true)"),
                    {"value": str(environment_id)},
                )
                failed_receipt = await connection.execute(
                    text(
                        "SELECT receipt_id FROM spine.idempotency_receipts "
                        "WHERE workspace_id = :workspace_id "
                        "AND environment_id = :environment_id "
                        "AND operation_name = 'qa_sessions' "
                        "AND idempotency_key = 'issue-430-terminal-message-result'"
                    ),
                    {
                        "workspace_id": workspace_id,
                        "environment_id": environment_id,
                    },
                )
                assert failed_receipt.first() is None
    finally:
        await receipt_engine.dispose()
    assert after_failure_run is not None and after_failure_run.state.value == "running"
    assert after_failure_message is not None and after_failure_message.state is QAMessageState.RUNNING
    assert after_failure_disclosure is None

    async with postgresql_adapter.uow_factory(session_context) as uow:
        await uow.answer_question_runs.complete_run(terminal_run)
        await uow.answer_disclosures.save(disclosure)
        await uow.qa_sessions.complete_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
            answer=accepted.answer.text,
            outcome=AnswerQuestionOutcome.ANSWERED,
            cancelled=False,
            completed_at=completed_attempt.completed_at,
            command_id=synthetic_uuid(43419),
            idempotency_key=IdempotencyKey("issue-430-terminal-message-result"),
            disclosure_dependencies=None,
            digest=completion_digest,
        )
        await uow.commit()
    async with postgresql_adapter.uow_factory(session_context) as uow:
        replay_result = await uow.qa_sessions.complete_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
            answer=accepted.answer.text,
            outcome=AnswerQuestionOutcome.ANSWERED,
            cancelled=False,
            completed_at=completed_attempt.completed_at,
            command_id=synthetic_uuid(43419),
            idempotency_key=IdempotencyKey("issue-430-terminal-message-result"),
            disclosure_dependencies=None,
            digest=completion_digest,
        )
        replay_receipt = await uow.idempotency.claim(
            operation_schema_version=1,
            key=IdempotencyKey("issue-430-terminal-message-result"),
            digest=completion_digest,
        )
        await uow.commit()
    async with postgresql_adapter.uow_factory(session_context) as uow:
        persisted_run = await uow.answer_question_runs.resolve_run(RUN_ID)
        persisted_message = await uow.qa_sessions.resolve_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
        )
        persisted_disclosure = await uow.answer_disclosures.resolve_for_attempt(
            workspace_id=workspace_id,
            environment_id=environment_id,
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
        )

    assert persisted_run == terminal_run
    assert persisted_message is not None
    assert persisted_message.state is QAMessageState.TERMINAL
    assert persisted_message.run_id == persisted_run.run_id
    assert persisted_message.terminal_outcome is persisted_run.outcome
    assert persisted_message.answer == accepted.answer.text
    assert persisted_disclosure == disclosure
    assert persisted_disclosure.accepted_answer == accepted
    assert replay_result.value == persisted_message
    assert replay_result.replay is True
    assert isinstance(replay_receipt, IdempotencyReplay)
    assert replay_receipt.result.result_id == MESSAGE_ID

    receipt_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        async with receipt_engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text("SELECT set_config('spine.workspace_id', :value, true)"),
                    {"value": str(workspace_id)},
                )
                await connection.execute(
                    text("SELECT set_config('spine.environment_id', :value, true)"),
                    {"value": str(environment_id)},
                )
                persisted_receipts = await connection.execute(
                    text(
                        "SELECT receipt_id, result_id FROM spine.idempotency_receipts "
                        "WHERE workspace_id = :workspace_id "
                        "AND environment_id = :environment_id "
                        "AND operation_name = 'qa_sessions' "
                        "AND idempotency_key = 'issue-430-terminal-message-result'"
                    ),
                    {
                        "workspace_id": workspace_id,
                        "environment_id": environment_id,
                    },
                )
                receipt_rows = persisted_receipts.all()
    finally:
        await receipt_engine.dispose()
    assert len(receipt_rows) == 1
    assert receipt_rows[0].result_id == MESSAGE_ID

    immutable_engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        async with immutable_engine.connect() as connection:
            with pytest.raises(DBAPIError, match="append-only"):
                async with connection.begin():
                    await connection.execute(
                        text("SELECT set_config('spine.workspace_id', :value, true)"),
                        {"value": str(workspace_id)},
                    )
                    await connection.execute(
                        text("SELECT set_config('spine.environment_id', :value, true)"),
                        {"value": str(environment_id)},
                    )
                    await connection.execute(
                        text(
                            "UPDATE spine.qa_message_versions SET version = version "
                            "WHERE workspace_id = :workspace_id "
                            "AND environment_id = :environment_id AND message_id = :message_id"
                        ),
                        {
                            "workspace_id": workspace_id,
                            "environment_id": environment_id,
                            "message_id": MESSAGE_ID,
                        },
                    )
    finally:
        await immutable_engine.dispose()


async def test_postgresql_answer_question_retry_cancellation_contract(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    await contract_qa_retry_cancel(postgresql_adapter)


@pytest.mark.parametrize("first_winner", ("completion", "cancellation"))
async def test_postgresql_ask_completion_cancellation_cas_keeps_first_terminal_winner(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    first_winner: str,
) -> None:
    inspector = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
    )

    async def wait_until_second_writer_blocked() -> None:
        try:
            async with inspector.connect() as connection:
                for _ in range(200):
                    blocked_writers = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity AS activity "
                            "WHERE activity.query ILIKE '%answer_question_runs%' "
                            "AND cardinality(pg_blocking_pids(activity.pid)) > 0"
                        )
                    )
                    if blocked_writers:
                        return
                    await asyncio.sleep(0.025)
                activity = await connection.execute(
                    text(
                        "SELECT pid, usename, state, wait_event_type, wait_event, query "
                        "FROM pg_stat_activity WHERE datname = current_database()"
                    )
                )
                pytest.fail(
                    "second PostgreSQL terminal writer never blocked on the run row: "
                    f"{activity.fetchall()}"
                )
        finally:
            await inspector.dispose()

    await exercise_answer_question_run_completion_and_cancellation_cas(
        postgresql_adapter,
        first_winner,
        wait_until_second_writer_blocked,
    )


async def test_temporal_retry_after_terminal_commit_failure_persists_one_postgresql_winner(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    from temporalio.api.enums.v1 import EventType
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from spine.application.authorized_ask import AuthorizedAskResult
    from spine.application.workflows.ask_admission_dispatch import (
        ASK_ADMISSION_WORKFLOW_TYPE,
        AskAdmissionWorkflowInput,
    )
    from spine.workflows.temporal.definitions import (
        AskAdmissionWorkflowDefinition,
        build_ask_admission_activity_bindings,
    )
    from tests.api.test_authorized_ask import (
        COMMAND_ID,
        MESSAGE_ID,
        RUN_ID,
        SESSION_ID,
        TRACE_ID,
        safe_output,
    )

    workspace_id = synthetic_uuid(43430)
    environment_id = synthetic_uuid(43431)
    await persist_qa_scope(postgresql_adapter, workspace_id, environment_id)
    context = qa_context(postgresql_adapter, workspace_id, environment_id)
    started_at = datetime(2026, 10, 17, tzinfo=timezone.utc)
    run = qa_run_record(
        workspace_id,
        environment_id,
        run_id=RUN_ID,
        created_at=started_at,
    )
    attempt = AnswerQuestionAttempt.start(
        attempt_id=synthetic_uuid(43432),
        run=run,
        attempt_number=1,
        execution_pin=qa_execution_pin(workspace_id, environment_id),
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=started_at,
    )
    completed_attempt = attempt.complete(
        state=AnswerQuestionAttemptState.SUCCEEDED,
        outcome=AnswerQuestionOutcome.ABSTAINED,
        usage=AnswerQuestionUsage(),
        terminal_event=AnswerQuestionAttemptEvent(
            event_id=synthetic_uuid(43433),
            workspace_id=workspace_id,
            environment_id=environment_id,
            environment=EnvironmentKind.PRODUCTION,
            kind=AnswerQuestionAttemptEventKind.COMPLETED,
            occurred_at=started_at + timedelta(minutes=1),
        ),
    )
    terminal_run = run.complete(
        outcome=AnswerQuestionOutcome.ABSTAINED,
        final_attempt_id=attempt.attempt_id,
        completed_at=completed_attempt.completed_at,
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(qa_run_command(run))
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.answer_question_runs.complete_attempt(completed_attempt)
        await uow.commit()
    async with postgresql_adapter.uow_factory(context) as uow:
        persisted_run = await uow.answer_question_runs.resolve_run(RUN_ID)
        persisted_attempts = await uow.answer_question_runs.list_attempts(RUN_ID)
    assert persisted_run is not None
    persisted_attempt = persisted_attempts[0]
    terminal_run = persisted_run.complete(
        outcome=AnswerQuestionOutcome.ABSTAINED,
        final_attempt_id=persisted_attempt.attempt_id,
        output_artifact_ids=persisted_attempt.output_artifact_ids,
        completed_at=persisted_attempt.completed_at,
    )

    class PostgreSQLTerminalActivity:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.first_commit_failed = asyncio.Event()
            self.retried = asyncio.Event()
            self.last_error: str | None = None

        async def execute(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
            assert workflow_input.run_id == RUN_ID
            self.calls += 1
            self.started.set()
            engine = create_async_engine(
                postgresql_contract_database.runtime_url.get_secret_value(),
                pool_pre_ping=True,
                hide_parameters=True,
                connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
            )
            session_factory = async_sessionmaker(
                engine, expire_on_commit=False, autoflush=False
            )
            boundary = TrustedContextBoundary.for_testing(
                issuer_id=synthetic_uuid(950),
                secret=b"issue-45-postgresql-contract-secret",
            )
            persistence = PostgreSQLPersistence(
                session_factory=(
                    lambda: FailureInjectingSession(
                        session_factory(), "during_commit"
                    )
                    if self.calls == 1
                    else session_factory()
                ),  # type: ignore[arg-type]
                context_verifier=boundary,
                outbox_events=create_outbox_event_registry(),
            )
            try:
                async with persistence.uow_factory(context) as uow:
                    await uow.answer_question_runs.complete_run(terminal_run)
                    await uow.commit()
            except Exception as error:
                self.last_error = type(error).__name__
                self.first_commit_failed.set()
                raise
            finally:
                await engine.dispose()
            self.retried.set()
            return AuthorizedAskResult(
                command_id=COMMAND_ID,
                session_id=SESSION_ID,
                message_id=MESSAGE_ID,
                output=safe_output(AnswerQuestionOutcome.ABSTAINED),
                accepted_answer=None,
            )

        async def cancel(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
            del workflow_input
            raise AssertionError("cancellation activity is not expected")

    activity = PostgreSQLTerminalActivity()
    execute_activity, cancel_activity = build_ask_admission_activity_bindings(activity)
    task_queue = "issue-430-postgresql-terminal-retry"
    workflow_input = AskAdmissionWorkflowInput(
        command_id=COMMAND_ID,
        workspace_id=workspace_id,
        environment_id=environment_id,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        trace_id=TRACE_ID,
        authorization_generation=1,
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        handle = await environment.client.start_workflow(
            ASK_ADMISSION_WORKFLOW_TYPE,
            workflow_input.model_dump(mode="json"),
            id=str(RUN_ID),
            task_queue=task_queue,
            result_type=dict,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        async with Worker(
            environment.client,
            task_queue=task_queue,
            identity="issue-430-before-terminal-recovery",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[],
        ):
            for _ in range(100):
                history = await handle.fetch_history()
                if any(
                    event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                    for event in history.events
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Temporal did not persist its terminal activity before worker restart")

        async with Worker(
            environment.client,
            task_queue=task_queue,
            identity="issue-430-after-terminal-recovery",
            max_cached_workflows=0,
            workflows=[AskAdmissionWorkflowDefinition],
            activities=[execute_activity, cancel_activity],
        ):
            await asyncio.wait_for(activity.started.wait(), timeout=10)
            await asyncio.wait_for(activity.first_commit_failed.wait(), timeout=10)
            result = await asyncio.wait_for(handle.result(), timeout=30)

    assert activity.calls >= 2
    assert activity.retried.is_set()
    assert activity.last_error == "UnexpectedPersistenceError"
    assert result["outcome"] == AnswerQuestionOutcome.ABSTAINED.value
    async with postgresql_adapter.uow_factory(context) as uow:
        persisted = await uow.answer_question_runs.resolve_run(RUN_ID)
    assert persisted == terminal_run


async def test_postgresql_operational_metrics_are_idempotent_retained_and_tenant_scoped(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(37001)
    environment_id = synthetic_uuid(37002)
    other_workspace_id = synthetic_uuid(37003)
    other_environment_id = synthetic_uuid(37004)
    await persist_idempotency_workspace(postgresql_adapter, workspace_id)
    await persist_idempotency_environment(postgresql_adapter, workspace_id, environment_id)
    await persist_idempotency_workspace(postgresql_adapter, other_workspace_id)
    await persist_idempotency_environment(postgresql_adapter, other_workspace_id, other_environment_id)

    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    current = OperationalMetricEvent(
        event_id=synthetic_uuid(37005),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_identity="test.metrics:v1",
        measurement=OperationalMeasurement.RETRIEVAL_LATENCY,
        event_at=now - timedelta(minutes=1),
        deduplication_key="metrics-current",
        logical_operation_id=synthetic_uuid(37006),
        terminal=True,
        duration_ms=17,
    )
    expired = current.model_copy(
        update={
            "event_id": synthetic_uuid(37007),
            "event_at": now - timedelta(days=91),
            "deduplication_key": "metrics-expired",
            "logical_operation_id": synthetic_uuid(37008),
        }
    )
    async with postgresql_adapter.uow_factory(
        postgresql_adapter.environment_context(workspace_id, environment_id)
    ) as uow:
        store = PostgreSQLOperationalMetricsStore(uow)
        await store.append(current)
        await store.append(current)
        await store.append(expired)
        await uow.commit()

    metrics_query = OperationalMetricsQuery(
        workspace_id=workspace_id,
        environment_id=environment_id,
        window=MetricWindow(
            start=now - timedelta(hours=1),
            end=now + timedelta(hours=1),
        ),
        measurements=(OperationalMeasurement.RETRIEVAL_LATENCY,),
    )
    async with postgresql_adapter.uow_factory(
        postgresql_adapter.environment_context(workspace_id, environment_id)
    ) as uow:
        store = PostgreSQLOperationalMetricsStore(uow)
        answer_fact = AnswerRateFact(
            run_id=synthetic_uuid(37009),
            workspace_id=workspace_id,
            environment_id=environment_id,
            event_at=now - timedelta(minutes=2),
            outcome=AnswerQuestionOutcome.ANSWERED,
            accepted=True,
            execution_started=True,
            accepted_evidence=True,
            user_initiated=True,
        )
        await store.append_answer_rate_fact(answer_fact)
        assert await store.answer_rate_facts(
            metrics_query.model_copy(update={"measurements": (OperationalMeasurement.ANSWER_RATE,)}),
            now=now,
            retention=timedelta(days=90),
        ) == (answer_fact,)
        events = await store.query_events(
            metrics_query,
            now=now,
            retention=timedelta(days=90),
        )
        assert events == (current,)

    other_query = metrics_query.model_copy(
        update={
            "workspace_id": other_workspace_id,
            "environment_id": other_environment_id,
        }
    )
    async with postgresql_adapter.uow_factory(
        postgresql_adapter.environment_context(other_workspace_id, other_environment_id)
    ) as uow:
        assert await PostgreSQLOperationalMetricsStore(uow).query_events(
            other_query,
            now=now,
            retention=timedelta(days=90),
        ) == ()


async def _seed_postgresql_terminalization_case(
    adapter: PersistenceAdapter,
    *,
    base: int,
    workspace_id: UUID | None = None,
    environment_id: UUID | None = None,
):
    from spine.application.answer_question_attempts import (
        PersistedAnswerQuestionAttemptService,
    )
    from spine.application.persistence.answer_question_runs import (
        CanonicalQATerminalizationReceipt,
    )
    from spine.application.qa_sessions import PersistedQASessionService
    from spine.domain.capabilities import (
        AnswerAuthorizationOutcome,
        AnswerQuestionOutput,
        AnswerQuestionSafeReason,
        AnswerQuestionSafeReasonCode,
        AnswerQuestionScope,
        RunAttemptCorrelation,
    )
    from spine.domain.capabilities.answer_disclosure import (
        AnswerDisclosureRecordV2,
        AnswerExecutionCheckpoint,
    )

    workspace_id = workspace_id or synthetic_uuid(base)
    environment_id = environment_id or synthetic_uuid(base + 1)
    run_id = synthetic_uuid(base + 2)
    attempt_id = synthetic_uuid(base + 3)
    session_id = synthetic_uuid(base + 4)
    message_id = synthetic_uuid(base + 5)
    subject_id = synthetic_uuid(base + 6)
    logical_key = f"issue-496-terminalization-{base}"
    started_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
    await persist_qa_scope(adapter, workspace_id, environment_id)
    context = qa_context(adapter, workspace_id, environment_id)
    run = qa_run_record(
        workspace_id,
        environment_id,
        run_id=run_id,
        idempotency_key=logical_key,
        created_at=started_at,
    )
    pin = qa_execution_pin(workspace_id, environment_id)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=attempt_id,
        run=run,
        attempt_number=1,
        logical_execution_key=logical_key,
        execution_pin=pin,
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=started_at,
    )
    output = AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=run_id,
            attempt_id=attempt_id,
            trace_id=run.diagnostic_context.trace_id,
        ),
        scope=AnswerQuestionScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        outcome=AnswerQuestionOutcome.ABSTAINED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE
        ),
    )
    disclosure = AnswerDisclosureRecordV2(
        request_id=synthetic_uuid(base + 7),
        request_digest="a" * 64,
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=run_id,
        attempt_id=attempt_id,
        outcome=AnswerQuestionOutcome.ABSTAINED,
        safe_output=output,
        terminal_output=output,
        execution_checkpoint=AnswerExecutionCheckpoint(
            execution_pin_digest=pin.pin_digest,
        ),
        created_at=started_at,
    )
    session = QASession(
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        subject_id=subject_id,
        created_at=started_at,
        updated_at=started_at,
        version=1,
        status=QASessionStatus.ACTIVE,
    )
    message = QAMessage(
        message_id=message_id,
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        version=1,
        ordinal=1,
        trace_id=run.diagnostic_context.trace_id,
        run_id=run_id,
        question="synthetic private question",
        state=QAMessageState.RUNNING,
        created_at=started_at,
        updated_at=started_at,
    )
    async with adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(qa_run_command(run))
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.qa_sessions.create_session(
            session,
            command_id=synthetic_uuid(base + 8),
            idempotency_key=IdempotencyKey(f"issue-496-session-{base}"),
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=1,
                payload={"session_id": session_id, "subject_id": subject_id},
            ),
        )
        await uow.qa_sessions.create_pending_message(
            message,
            command_id=synthetic_uuid(base + 9),
            idempotency_key=IdempotencyKey(f"issue-496-message-{base}"),
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=1,
                payload={"session_id": session_id, "message_id": message_id, "run_id": run_id},
            ),
        )
        await uow.answer_disclosures.save(disclosure)
        await uow.commit()

    terminalizer = PersistedAnswerQuestionAttemptService(
        uow_factory=adapter.uow_factory,
        execution=object(),
        clock=lambda: started_at + timedelta(minutes=1),
        event_id_factory=lambda: synthetic_uuid(base + 10),
    )
    sessions = PersistedQASessionService(
        repository=None,
        disclosure_eligibility=None,
        clock=lambda: started_at + timedelta(minutes=1),
        command_id_factory=lambda: synthetic_uuid(base + 11),
    )
    receipt = CanonicalQATerminalizationReceipt(
        run_id=run_id,
        attempt_id=attempt_id,
        attempt_number=1,
        execution_pin=pin,
        output=output,
        context_bundle_ref=None,
        agent_result_ref=None,
        agent_result=None,
        output_artifact_ids=(),
        candidate_input_artifact_id=None,
        candidate_output_artifacts=(),
        disclosure_record=disclosure,
        authorization=AnswerAuthorizationOutcome.ALLOWED,
    )
    return SimpleNamespace(
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=run_id,
        attempt_id=attempt_id,
        session_id=session_id,
        message_id=message_id,
        logical_key=logical_key,
        context=context,
        run=run,
        attempt=attempt,
        output=output,
        disclosure=disclosure,
        receipt=receipt,
        terminalizer=terminalizer,
        sessions=sessions,
    )


async def _assert_postgresql_runtime_role_is_rls_restricted(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    text(
                        "SELECT current_user, role.rolsuper, role.rolbypassrls "
                        "FROM pg_roles AS role WHERE role.rolname = current_user"
                    )
                )
            ).one()
            relations = (
                await connection.execute(
                    text(
                        "SELECT relation.relname, relation.relrowsecurity, "
                        "relation.relforcerowsecurity FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname = ANY(:tables)"
                    ),
                    {
                        "tables": [
                            "answer_question_runs",
                            "answer_question_attempts",
                            "qa_session_versions",
                            "qa_message_versions",
                            "qa_answer_disclosure_records",
                        ]
                    },
                )
            ).all()
    finally:
        await engine.dispose()

    assert identity == (RUNTIME_ROLE, False, False)
    assert {name: (enabled, forced) for name, enabled, forced in relations} == {
        "answer_question_runs": (True, True),
        "answer_question_attempts": (True, True),
        "qa_session_versions": (True, True),
        "qa_message_versions": (True, True),
        "qa_answer_disclosure_records": (True, True),
    }


async def _assert_postgresql_runtime_role_cannot_read_terminalization_case(
    postgresql_contract_database: PostgreSQLContractDatabase,
    case,
    *,
    workspace_id: UUID,
    environment_id: UUID,
) -> None:
    """Probe RLS directly, without application scope predicates."""
    engine = create_async_engine(
        postgresql_contract_database.runtime_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        async with engine.begin() as connection:
            identity = await connection.scalar(text("SELECT current_user"))
            await connection.execute(
                text(
                    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                    "set_config('spine.environment_id', :environment_id, true)"
                ),
                {
                    "workspace_id": str(workspace_id),
                    "environment_id": str(environment_id),
                },
            )
            visible_rows = await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM spine.answer_question_runs WHERE run_id = :run_id), "
                    "(SELECT count(*) FROM spine.answer_question_attempts WHERE attempt_id = :attempt_id), "
                    "(SELECT count(*) FROM spine.qa_session_versions WHERE session_id = :session_id), "
                    "(SELECT count(*) FROM spine.qa_message_versions WHERE message_id = :message_id), "
                    "(SELECT count(*) FROM spine.qa_answer_disclosure_records WHERE request_id = :request_id)"
                ),
                {
                    "run_id": case.run_id,
                    "attempt_id": case.attempt_id,
                    "session_id": case.session_id,
                    "message_id": case.message_id,
                    "request_id": case.disclosure.request_id,
                },
            )
            counts = visible_rows.one()
    finally:
        await engine.dispose()

    assert identity == RUNTIME_ROLE
    assert counts == (0, 0, 0, 0, 0)


async def _assert_postgresql_terminalization_case_running(adapter: PersistenceAdapter, case) -> None:
    async with adapter.uow_factory(case.context) as uow:
        run = await uow.answer_question_runs.resolve_run(case.run_id)
        attempt = await uow.answer_question_runs.resolve_attempt(case.attempt_id)
        message = await uow.qa_sessions.resolve_message(
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            session_id=case.session_id,
            message_id=case.message_id,
        )
        disclosure = await uow.answer_disclosures.resolve_for_attempt(
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            run_id=case.run_id,
            attempt_id=case.attempt_id,
        )
    assert run.state is AnswerQuestionRunState.RUNNING
    assert run.final_attempt_id is None
    assert attempt.state is AnswerQuestionAttemptState.RUNNING
    assert message is not None and message.state is QAMessageState.RUNNING
    assert message.terminal_outcome is None
    assert disclosure == case.disclosure


class _PersistedRuntimeArtifactsFixture:
    """Runtime artifact-port fixture with explicit immutable persisted records."""

    def __init__(self, *, result_id: UUID, result: AgentResult, artifacts: tuple[Artifact, ...]) -> None:
        self._result_id = result_id
        self._result = result
        self._artifacts = {artifact.id: artifact for artifact in artifacts}

    async def load_agent_result(
        self,
        result_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> AgentResult | None:
        if result_id != self._result_id:
            return None
        if any(
            artifact.workspace_id != workspace_id
            or artifact.environment_id != environment_id
            or artifact.work_item_id != work_item_id
            for artifact in self._artifacts.values()
        ):
            return None
        return self._result

    async def load(
        self,
        artifact_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> Artifact | None:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None or (
            artifact.workspace_id,
            artifact.environment_id,
            artifact.work_item_id,
        ) != (workspace_id, environment_id, work_item_id):
            return None
        return artifact

    async def verify_lineage(
        self,
        artifact: Artifact,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> None:
        if (
            self._artifacts.get(artifact.id) != artifact
            or artifact.workspace_id != workspace_id
            or artifact.environment_id != environment_id
            or artifact.work_item_id != work_item_id
            or any(parent_id not in self._artifacts for parent_id in artifact.upstream_artifact_ids)
        ):
            raise ValueError("persisted synthetic runtime artifact lineage is invalid")


@pytest.mark.asyncio
async def test_postgresql_terminalizer_commits_answered_persisted_lineage(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    from spine.application.diagnostics import DiagnosticContext
    from spine.application.answer_question_attempts import PersistedAnswerQuestionAttemptService
    from spine.application.persistence.answer_question_runs import CanonicalQATerminalizationReceipt
    from spine.application.qa_sessions import PersistedQASessionService
    from spine.domain.agents import AgentResult
    from spine.domain.artifacts import Artifact
    from spine.domain.capabilities import AnswerAuthorizationOutcome
    from spine.domain.capabilities.answer_question_runs import AgentResultRef
    from spine.domain.context_bundles import ContextBundleRef
    from spine.domain.context_profiles import ContextProfileRef
    from tests.application.test_answer_question_terminalization import _answered_execution
    from tests.application.persistence.test_ask_admission import (
        ENVIRONMENT_ID,
        TRACE_ID,
        WORKSPACE_ID,
    )
    from types import SimpleNamespace

    base = 50200
    workspace_id = WORKSPACE_ID
    environment_id = ENVIRONMENT_ID
    run_id = synthetic_uuid(base + 2)
    attempt_id = synthetic_uuid(base + 3)
    session_id = synthetic_uuid(base + 4)
    message_id = synthetic_uuid(base + 5)
    subject_id = synthetic_uuid(base + 6)
    logical_key = f"issue-496-terminalization-{base}"
    started_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
    await persist_qa_scope(postgresql_adapter, workspace_id, environment_id)
    context = qa_context(postgresql_adapter, workspace_id, environment_id)
    profile = context_profile(
        workspace_id,
        environment_id,
        profile_id=synthetic_uuid(base + 10),
    )
    profile_ref = ContextProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
    )
    bundle = _scoped_context_bundle(
        workspace_id,
        environment_id,
        profile_ref,
        run_id=run_id,
        attempt_id=attempt_id,
        idempotency_key=f"issue-496-context-{base}",
        request_id=synthetic_uuid(base + 11),
    )
    run = qa_run_record(
        workspace_id,
        environment_id,
        run_id=run_id,
        idempotency_key=logical_key,
        diagnostic_context=DiagnosticContext(
            trace_id=TRACE_ID,
            correlation_id=synthetic_uuid(base + 13),
            causation_id=synthetic_uuid(base + 14),
        ),
        created_at=started_at,
    )
    pin = qa_execution_pin(workspace_id, environment_id)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=attempt_id,
        run=run,
        attempt_number=1,
        logical_execution_key=logical_key,
        execution_pin=pin,
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=started_at,
    )

    (
        bundle,
        dependencies,
        accepted_answer,
        output,
        disclosure,
        input_artifact,
        output_artifact,
        agent_result,
    ) = _answered_execution(SimpleNamespace(run_id=run_id), attempt, pin, bundle=bundle)
    candidate_input_id = input_artifact.id
    candidate_output_id = output_artifact.id
    agent_result_id = UUID(int=930)
    artifacts = _PersistedRuntimeArtifactsFixture(
        result_id=agent_result_id,
        result=agent_result,
        artifacts=(input_artifact, output_artifact),
    )

    session = QASession(
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        subject_id=subject_id,
        created_at=started_at,
        updated_at=started_at,
        version=1,
        status=QASessionStatus.ACTIVE,
    )
    message = QAMessage(
        message_id=message_id,
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        version=1,
        ordinal=1,
        trace_id=run.diagnostic_context.trace_id,
        run_id=run_id,
        question="synthetic answered question",
        state=QAMessageState.RUNNING,
        created_at=started_at,
        updated_at=started_at,
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.context_profiles.create(profile)
        await uow.context_bundles.create(bundle)
        await uow.answer_question_runs.create_run(qa_run_command(run))
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.qa_sessions.create_session(
            session,
            command_id=synthetic_uuid(base + 21),
            idempotency_key=IdempotencyKey(f"issue-496-session-{base}"),
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=1,
                payload={"session_id": session_id, "subject_id": subject_id},
            ),
        )
        await uow.qa_sessions.create_pending_message(
            message,
            command_id=synthetic_uuid(base + 22),
            idempotency_key=IdempotencyKey(f"issue-496-message-{base}"),
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=1,
                payload={"session_id": session_id, "message_id": message_id, "run_id": run_id},
            ),
        )
        await uow.answer_disclosures.save(disclosure)
        await uow.commit()

    receipt = CanonicalQATerminalizationReceipt(
        run_id=run_id,
        attempt_id=attempt_id,
        attempt_number=attempt.attempt_number,
        execution_pin=pin,
        output=output,
        context_bundle_ref=ContextBundleRef(
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.bundle_version,
            bundle_digest=bundle.bundle_digest,
        ),
        agent_result_ref=AgentResultRef(result_id=agent_result_id),
        agent_result=agent_result,
        output_artifact_ids=agent_result.output_artifact_ids,
        candidate_input_artifact_id=candidate_input_id,
        candidate_output_artifacts=(output_artifact,),
        disclosure_record=disclosure,
        authorization=AnswerAuthorizationOutcome.ALLOWED,
        disclosure_dependencies=dependencies,
    )
    terminalizer = PersistedAnswerQuestionAttemptService(
        uow_factory=postgresql_adapter.uow_factory,
        execution=object(),
        clock=lambda: started_at + timedelta(minutes=1),
        event_id_factory=lambda: synthetic_uuid(base + 23),
        artifacts=artifacts,
    )
    sessions = PersistedQASessionService(
        repository=None,
        disclosure_eligibility=None,
        clock=lambda: started_at + timedelta(minutes=1),
        command_id_factory=lambda: synthetic_uuid(base + 24),
    )
    result = await terminalizer.terminalize(
        context=context,
        session_id=session_id,
        message_id=message_id,
        idempotency_key=logical_key,
        sessions=sessions,
        receipt=receipt,
    )

    async with postgresql_adapter.uow_factory(context) as uow:
        persisted_run = await uow.answer_question_runs.resolve_run(run_id)
        persisted_attempt = await uow.answer_question_runs.resolve_attempt(attempt_id)
        persisted_message = await uow.qa_sessions.resolve_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            message_id=message_id,
        )
        persisted_bundle = await uow.context_bundles.resolve(
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.bundle_version,
            bundle_digest=bundle.bundle_digest,
        )
    assert result.output == output
    assert persisted_run.state is AnswerQuestionRunState.COMPLETED
    assert persisted_run.final_attempt_id == attempt_id
    assert persisted_run.output_artifact_ids == (candidate_output_id,)
    assert persisted_attempt.state is AnswerQuestionAttemptState.SUCCEEDED
    assert persisted_attempt.context_bundle_ref == bundle.reference
    assert persisted_attempt.agent_result_ref == AgentResultRef(result_id=agent_result_id)
    assert persisted_attempt.output_artifact_ids == (candidate_output_id,)
    assert persisted_message is not None
    assert persisted_message.terminal_outcome is AnswerQuestionOutcome.ANSWERED
    assert persisted_message.answer == accepted_answer.answer.text
    assert persisted_bundle == bundle
    assert output_artifact.upstream_artifact_ids == (candidate_input_id,)


@pytest.mark.parametrize("scope_mismatch", ("workspace", "environment"))
async def test_postgresql_terminalizer_enforces_runtime_rls_for_cross_scope(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
    scope_mismatch: str,
) -> None:
    from spine.application.persistence.errors import AnswerQuestionRunNotFoundError

    case = await _seed_postgresql_terminalization_case(
        postgresql_adapter,
        base=49800 if scope_mismatch == "workspace" else 49900,
    )
    await _assert_postgresql_runtime_role_is_rls_restricted(postgresql_contract_database)

    if scope_mismatch == "workspace":
        other_workspace_id = synthetic_uuid(49820)
        other_environment_id = synthetic_uuid(49821)
        await persist_qa_scope(postgresql_adapter, other_workspace_id, other_environment_id)
    else:
        other_workspace_id = case.workspace_id
        other_environment_id = synthetic_uuid(49920)
        await persist_idempotency_environment(
            postgresql_adapter, other_workspace_id, other_environment_id
        )
    assert postgresql_adapter.ingestion_context is not None
    unauthorized_scope = postgresql_adapter.ingestion_context(
        other_workspace_id,
        other_environment_id,
        "answer_question_run",
    )

    with pytest.raises(AnswerQuestionRunNotFoundError) as error:
        await case.terminalizer.terminalize(
            context=unauthorized_scope,
            session_id=case.session_id,
            message_id=case.message_id,
            idempotency_key=case.logical_key,
            sessions=case.sessions,
            receipt=case.receipt,
        )
    assert "synthetic private question" not in str(error.value)
    assert str(case.workspace_id) not in str(error.value)
    await _assert_postgresql_runtime_role_cannot_read_terminalization_case(
        postgresql_contract_database,
        case,
        workspace_id=other_workspace_id,
        environment_id=other_environment_id,
    )
    await _assert_postgresql_terminalization_case_running(postgresql_adapter, case)

    result = await case.terminalizer.terminalize(
        context=case.context,
        session_id=case.session_id,
        message_id=case.message_id,
        idempotency_key=case.logical_key,
        sessions=case.sessions,
        receipt=case.receipt,
    )
    assert result.output == case.output
    async with postgresql_adapter.uow_factory(case.context) as uow:
        run = await uow.answer_question_runs.resolve_run(case.run_id)
        attempt = await uow.answer_question_runs.resolve_attempt(case.attempt_id)
        message = await uow.qa_sessions.resolve_message(
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            session_id=case.session_id,
            message_id=case.message_id,
        )
    assert run.final_attempt_id == case.attempt_id
    assert run.outcome is AnswerQuestionOutcome.ABSTAINED
    assert attempt.outcome is AnswerQuestionOutcome.ABSTAINED
    assert message is not None and message.terminal_outcome is AnswerQuestionOutcome.ABSTAINED


async def test_postgresql_terminalizer_rejects_unverified_trusted_context(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    from spine.application.persistence.context import (
        PersistenceOperation,
        PersistencePurpose,
    )
    from spine.application.persistence.errors import InvalidPersistenceContextError

    case = await _seed_postgresql_terminalization_case(postgresql_adapter, base=50000)
    forged_issuer = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(50020),
        secret=b"different-trusted-context-boundary-secret",
    )
    unverified_context = forged_issuer.worker(
        scope=EnvironmentScope(
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
        ),
        service_principal_id=synthetic_uuid(50021),
        purpose=PersistencePurpose("contract_test"),
        operation=PersistenceOperation("answer_question_run"),
        trace_id=synthetic_uuid(50022),
    )
    with pytest.raises(InvalidPersistenceContextError):
        await case.terminalizer.terminalize(
            context=unverified_context,
            session_id=case.session_id,
            message_id=case.message_id,
            idempotency_key=case.logical_key,
            sessions=case.sessions,
            receipt=case.receipt,
        )
    await _assert_postgresql_terminalization_case_running(postgresql_adapter, case)

    result = await case.terminalizer.terminalize(
        context=case.context,
        session_id=case.session_id,
        message_id=case.message_id,
        idempotency_key=case.logical_key,
        sessions=case.sessions,
        receipt=case.receipt,
    )
    assert result.output == case.output


async def test_postgresql_terminalizer_rejects_receipt_attempt_mismatch(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    from dataclasses import replace
    from spine.application.persistence.errors import AnswerQuestionRunConflictError

    case = await _seed_postgresql_terminalization_case(postgresql_adapter, base=50100)
    mismatched_receipt = replace(case.receipt, attempt_number=case.receipt.attempt_number + 1)
    with pytest.raises(AnswerQuestionRunConflictError):
        await case.terminalizer.terminalize(
            context=case.context,
            session_id=case.session_id,
            message_id=case.message_id,
            idempotency_key=case.logical_key,
            sessions=case.sessions,
            receipt=mismatched_receipt,
        )
    await _assert_postgresql_terminalization_case_running(postgresql_adapter, case)
    result = await case.terminalizer.terminalize(
        context=case.context,
        session_id=case.session_id,
        message_id=case.message_id,
        idempotency_key=case.logical_key,
        sessions=case.sessions,
        receipt=case.receipt,
    )
    assert result.output == case.output


@pytest.mark.asyncio
async def test_postgresql_terminalizer_rejects_receipt_scope_mismatch(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    from dataclasses import replace
    from spine.application.persistence.errors import AnswerQuestionRunConflictError
    from spine.domain.capabilities import AnswerQuestionScope

    case = await _seed_postgresql_terminalization_case(postgresql_adapter, base=50300)
    other_environment_id = synthetic_uuid(50320)
    await persist_idempotency_environment(
        postgresql_adapter, case.workspace_id, other_environment_id
    )
    other_pin = qa_execution_pin(case.workspace_id, other_environment_id)
    other_output = case.output.model_copy(
        update={
            "scope": AnswerQuestionScope(
                workspace_id=case.workspace_id,
                environment_id=other_environment_id,
            )
        }
    )
    other_checkpoint = case.disclosure.execution_checkpoint.model_copy(
        update={"execution_pin_digest": other_pin.pin_digest}
    )
    other_disclosure = case.disclosure.model_copy(
        update={
            "environment_id": other_environment_id,
            "safe_output": other_output,
            "terminal_output": other_output,
            "execution_checkpoint": other_checkpoint,
        }
    )
    mismatched_receipt = replace(
        case.receipt,
        execution_pin=other_pin,
        output=other_output,
        disclosure_record=other_disclosure,
    )
    with pytest.raises(AnswerQuestionRunConflictError):
        await case.terminalizer.terminalize(
            context=case.context,
            session_id=case.session_id,
            message_id=case.message_id,
            idempotency_key=case.logical_key,
            sessions=case.sessions,
            receipt=mismatched_receipt,
        )
    await _assert_postgresql_terminalization_case_running(postgresql_adapter, case)
    result = await case.terminalizer.terminalize(
        context=case.context,
        session_id=case.session_id,
        message_id=case.message_id,
        idempotency_key=case.logical_key,
        sessions=case.sessions,
        receipt=case.receipt,
    )
    assert result.output == case.output


@pytest.mark.asyncio
async def test_postgresql_application_terminalizer_commits_attempt_run_and_message_together(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    from spine.application.answer_question_attempts import (
        PersistedAnswerQuestionAttemptService,
    )
    from spine.application.qa_sessions import PersistedQASessionService
    from spine.domain.capabilities import (
        AnswerQuestionOutput,
        AnswerQuestionSafeReason,
        AnswerQuestionSafeReasonCode,
        AnswerQuestionScope,
        RunAttemptCorrelation,
    )
    from spine.domain.capabilities.answer_disclosure import (
        AnswerDisclosureRecordV2,
        AnswerExecutionCheckpoint,
    )

    workspace_id = synthetic_uuid(49700)
    environment_id = synthetic_uuid(49701)
    run_id = synthetic_uuid(49702)
    attempt_id = synthetic_uuid(49703)
    session_id = synthetic_uuid(49704)
    message_id = synthetic_uuid(49705)
    subject_id = synthetic_uuid(49706)
    logical_key = "issue-496-postgresql-terminalization"
    started_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
    await persist_qa_scope(postgresql_adapter, workspace_id, environment_id)
    context = qa_context(postgresql_adapter, workspace_id, environment_id)
    run = qa_run_record(
        workspace_id,
        environment_id,
        run_id=run_id,
        idempotency_key=logical_key,
        created_at=started_at,
    )
    pin = qa_execution_pin(workspace_id, environment_id)
    attempt = AnswerQuestionAttempt.start(
        attempt_id=attempt_id,
        run=run,
        attempt_number=1,
        logical_execution_key=logical_key,
        execution_pin=pin,
        input_artifact_ids=run.input_artifact_ids,
        diagnostic_context=run.diagnostic_context,
        started_at=started_at,
    )
    output = AnswerQuestionOutput(
        correlation=RunAttemptCorrelation(
            run_id=run_id,
            attempt_id=attempt_id,
            trace_id=run.diagnostic_context.trace_id,
        ),
        scope=AnswerQuestionScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        outcome=AnswerQuestionOutcome.ABSTAINED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE
        ),
    )
    disclosure = AnswerDisclosureRecordV2(
        request_id=synthetic_uuid(49707),
        request_digest="a" * 64,
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=run_id,
        attempt_id=attempt_id,
        outcome=AnswerQuestionOutcome.ABSTAINED,
        safe_output=output,
        terminal_output=output,
        execution_checkpoint=AnswerExecutionCheckpoint(
            execution_pin_digest=pin.pin_digest,
        ),
        created_at=started_at,
    )
    session = QASession(
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        subject_id=subject_id,
        created_at=started_at,
        updated_at=started_at,
        version=1,
        status=QASessionStatus.ACTIVE,
    )
    message = QAMessage(
        message_id=message_id,
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        version=1,
        ordinal=1,
        trace_id=run.diagnostic_context.trace_id,
        run_id=run_id,
        question="synthetic question",
        state=QAMessageState.RUNNING,
        created_at=started_at,
        updated_at=started_at,
    )
    async with postgresql_adapter.uow_factory(context) as uow:
        await uow.answer_question_runs.create_run(qa_run_command(run))
        await uow.answer_question_runs.start_attempt(attempt)
        await uow.qa_sessions.create_session(
            session,
            command_id=synthetic_uuid(49708),
            idempotency_key=IdempotencyKey("issue-496-postgresql-session"),
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=1,
                payload={"session_id": session_id, "subject_id": subject_id},
            ),
        )
        await uow.qa_sessions.create_pending_message(
            message,
            command_id=synthetic_uuid(49709),
            idempotency_key=IdempotencyKey("issue-496-postgresql-message"),
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=1,
                payload={"session_id": session_id, "message_id": message_id, "run_id": run_id},
            ),
        )
        await uow.answer_disclosures.save(disclosure)
        await uow.commit()

    sessions = PersistedQASessionService(
        repository=None,  # The terminalizer supplies its caller-owned repository.
        disclosure_eligibility=None,
        clock=lambda: started_at + timedelta(minutes=1),
        command_id_factory=lambda: synthetic_uuid(49710),
    )
    terminalizer = PersistedAnswerQuestionAttemptService(
        uow_factory=postgresql_adapter.uow_factory,
        execution=object(),  # Allocation is out of scope for this terminalization test.
        clock=lambda: started_at + timedelta(minutes=1),
        event_id_factory=lambda: synthetic_uuid(49711),
    )
    CANONICAL_TRANSACTION_PROBE.arm(
        PostgreSQLTransactionStage.BEFORE_COMMIT,
        failure=RuntimeError("synthetic terminal transaction failure"),
    )
    try:
        with pytest.raises(UnexpectedPersistenceError):
            await terminalizer.terminalize(
                context=context,
                session_id=session_id,
                message_id=message_id,
                idempotency_key=logical_key,
                sessions=sessions,
                receipt=None,
            )
    finally:
        CANONICAL_TRANSACTION_PROBE.disarm()

    async with postgresql_adapter.uow_factory(context) as uow:
        rolled_back_run = await uow.answer_question_runs.resolve_run(run_id)
        rolled_back_attempt = await uow.answer_question_runs.resolve_attempt(attempt_id)
        rolled_back_message = await uow.qa_sessions.resolve_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            message_id=message_id,
        )
    assert rolled_back_run.state is AnswerQuestionRunState.RUNNING
    assert rolled_back_run.final_attempt_id is None
    assert rolled_back_attempt.state is AnswerQuestionAttemptState.RUNNING
    assert rolled_back_attempt.completed_at is None
    assert rolled_back_message.state is QAMessageState.RUNNING
    assert rolled_back_message.terminal_outcome is None

    result = await terminalizer.terminalize(
        context=context,
        session_id=session_id,
        message_id=message_id,
        idempotency_key=logical_key,
        sessions=sessions,
        receipt=None,
    )

    assert result.output == output
    async with postgresql_adapter.uow_factory(context) as uow:
        persisted_run = await uow.answer_question_runs.resolve_run(run_id)
        persisted_attempt = await uow.answer_question_runs.resolve_attempt(attempt_id)
        persisted_message = await uow.qa_sessions.resolve_message(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            message_id=message_id,
        )
    assert persisted_run.final_attempt_id == attempt_id
    assert persisted_run.outcome is AnswerQuestionOutcome.ABSTAINED
    assert persisted_attempt.outcome is AnswerQuestionOutcome.ABSTAINED
    assert persisted_message.terminal_outcome is AnswerQuestionOutcome.ABSTAINED


@pytest.mark.asyncio(loop_scope="session")
async def test_drive_lifecycle_decision_repository_persists_tenant_scoped_transition(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_canonical_case(postgresql_adapter, 61000)
    assert postgresql_adapter.ingestion_context is not None
    repository = PostgreSQLDriveLifecycleDecisionRepository(
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=lambda operation, identity: postgresql_adapter.ingestion_context(
            case.source.workspace_id,
            case.source.environment_id,
            operation.value,
        ),
    )
    decision = DriveRemovalDecision(
        decision_id=synthetic_uuid(61010),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        reconciliation_id=synthetic_uuid(61011),
        snapshot_run_id=synthetic_uuid(61012),
        change_sync_run_id=synthetic_uuid(61013),
        committed_cursor="drive-cursor-610",
        service_principal_id=synthetic_uuid(61014),
        access_policy_version_id=synthetic_uuid(61015),
        authorization_generation=1,
        configuration_generation=1,
        connection_id=synthetic_uuid(61016),
        resource_scope_reference=synthetic_uuid(61017),
        policy_version_id=synthetic_uuid(61018),
        policy_version="drive-removal-policy:v1",
        file_id="drive-file-610",
        signal=DriveLifecycleSignal.CONFIRMED_ABSENT,
        source_object_id=case.source.source_object_id,
        expected_current_revision_id=case.revision.revision_id,
        policy_authorized=True,
        state=DriveLifecycleDecisionState.PENDING,
        decided_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )

    assert await repository.record(decision) == decision
    assert await repository.resolve(
        decision.decision_id,
        workspace_id=decision.workspace_id,
        environment_id=decision.environment_id,
    ) == decision
    assert await repository.resolve(
        decision.decision_id,
        workspace_id=synthetic_uuid(61020),
        environment_id=synthetic_uuid(61021),
    ) is None

    tombstoned = decision.model_copy(
        update={
            "state": DriveLifecycleDecisionState.TOMBSTONED,
            "tombstone_revision_id": synthetic_uuid(61022),
        }
    )
    assert await repository.record(tombstoned) == tombstoned
    assert await repository.resolve(
        decision.decision_id,
        workspace_id=decision.workspace_id,
        environment_id=decision.environment_id,
    ) == tombstoned


@pytest.mark.asyncio(loop_scope="session")
async def test_requirements_artifacts_replay_successor_and_isolate_workspaces(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    from spine.application.requirements_artifacts import (
        PersistedRequirementsArtifactService,
        RequirementsExtractionClarificationRevalidator,
    )
    from spine.application.persistence.errors import IdempotencyConflictError
    from spine.application.persistence.errors import InvalidPersistenceContextError
    from spine.domain.workflows import Requirement, RequirementState

    workspace_id = synthetic_uuid(64000)
    environment_id = synthetic_uuid(64001)
    actor_id = synthetic_uuid(959)
    version_id = synthetic_uuid(64004)
    start_id = synthetic_uuid(64005)
    async with postgresql_adapter.uow_factory(postgresql_adapter.workspace_context(workspace_id)) as uow:
        await uow.workspaces.add(Workspace(
            id=workspace_id, slug="issue-640-requirements", display_name="Issue 640",
        ))
        await uow.environments.add(Environment(
            id=environment_id, workspace_id=workspace_id,
            kind=EnvironmentKind.PRODUCTION, display_name="Issue 640 environment",
        ))
        await uow.commit()

    start_context = postgresql_adapter.ingestion_context(workspace_id, environment_id, "workflow.run.start")
    register_workflow_run_outbox_event(postgresql_adapter.outbox_events)
    run_service = PersistedWorkflowRunAdmissionService(postgresql_adapter.uow_factory)
    receipt = await run_service.start(context=start_context, command=StartWorkflowCommand(
        command_id=start_id, idempotency_key="issue-640-start", workspace_id=workspace_id,
        environment_id=environment_id, actor=ActorRef(kind=ActorKind.SERVICE, id=actor_id),
        environment=EnvironmentKind.PRODUCTION, workflow_key="commercial.requirements",
        workflow_version_id=version_id, workflow_version="1.0.0", work_item_key="issue-640-work-item",
        client_request=ClientRequestV1(message="Deliver by Friday"), trace_id=start_context.trace_id,
        correlation_id=start_context.trace_id, causation_id=start_id,
    ))
    artifacts = PersistedRequirementsArtifactService(
        postgresql_adapter.uow_factory,
        clarification_revalidator=RequirementsExtractionClarificationRevalidator(),
    )
    first = await artifacts.produce_first_candidate(context=start_context, run_id=receipt.run_id)
    replay = await artifacts.produce_first_candidate(context=start_context, run_id=receipt.run_id)
    assert first is not None and first == replay
    with pytest.raises(IdempotencyConflictError):
        async with postgresql_adapter.uow_factory(start_context) as uow:
            await uow.requirements_artifacts.persist_candidate(
                first.model_copy(update={"production_digest": "b" * 64})
            )
    with pytest.raises(InvalidPersistenceContextError):
        async with postgresql_adapter.uow_factory(start_context) as uow:
            await uow.requirements_artifacts.persist_candidate(
                first.model_copy(update={"work_item_id": synthetic_uuid(64099),
                                         "artifact_version": 2,
                                         "predecessor_version": 1,
                                         "production_identity": "invalid-work-item"})
            )

    successor = await artifacts.persist_successor(
        context=start_context, artifact_id=first.artifact_id,
        production_identity="issue-640-revision-2",
        requirements=(Requirement(requirement_id=synthetic_uuid(64006), category="functional",
                                  statement="Deliver by Friday at noon"),),
        state=RequirementState.PARTIAL,
    )
    restored = await artifacts.get(context=start_context, artifact_id=first.artifact_id)
    versions = await artifacts.versions(context=start_context, artifact_id=first.artifact_id)
    assert restored == successor and successor.artifact_version == 2
    assert successor.predecessor_version == 1
    assert versions == (first, successor)

    other_workspace = synthetic_uuid(64007)
    other_environment = synthetic_uuid(64008)
    async with postgresql_adapter.uow_factory(postgresql_adapter.workspace_context(other_workspace)) as uow:
        await uow.workspaces.add(Workspace(
            id=other_workspace, slug="issue-640-other", display_name="Other workspace",
        ))
        await uow.environments.add(Environment(
            id=other_environment, workspace_id=other_workspace,
            kind=EnvironmentKind.PRODUCTION, display_name="Other environment",
        ))
        await uow.commit()
    other_context = postgresql_adapter.ingestion_context(other_workspace, other_environment, "workflow.run.read")
    assert await artifacts.get(context=other_context, artifact_id=first.artifact_id) is None
