from datetime import datetime, timezone
import asyncio
from uuid import UUID, NAMESPACE_URL, uuid5

import pytest
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    PolicyActivationDraft,
    access_policy_document_hash,
)
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
    CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceOutboxPayload,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.errors import (
    CanonicalAuthorityConflictError,
    CanonicalEligibilityError,
    OptimisticConflictError,
    PersistenceUnavailableError,
)
from spine.application.canonical_acceptance import (
    CanonicalAcceptanceAuditUnavailableError,
    CanonicalAcceptanceCoordinator,
    build_canonical_acceptance_coordinator,
)
from spine.application.persistence.canonical import (
    SourceOrderComparison,
    SourceOrderingPolicyError,
    SourceOrderingPolicyRegistry,
)
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
)
from spine.domain.common import ContextOrigin, EnvironmentKind
from spine.domain.sources import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    AdmissionDecision,
    AdmissionDisposition,
    PolicyOriginKind,
    PrincipalKind,
)
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from spine.infrastructure.persistence.access_policies import (
    InMemoryAccessPolicyRepository,
)
from spine.infrastructure.persistence.canonical_composition import (
    build_production_canonical_acceptance_coordinator,
)
from tests.application.persistence.test_source_observation import (
    NOW,
    provenance,
    receipt_association,
    source,
    synthetic_uuid,
    valid_revision,
)
from tests.application.persistence.test_source_observation import command as observation_command


def uid(value: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"canonical-test-{value}")


def test_canonical_production_composition_requires_failed_audit() -> None:
    with pytest.raises(TypeError):
        build_canonical_acceptance_coordinator(
            uow_factory=lambda context: None,
            context_factory=lambda command: None,
            failed_attempt_audit=None,
        )
    coordinator = build_production_canonical_acceptance_coordinator(
        uow_factory=lambda context: None,
        context_factory=lambda command: None,
    )
    assert isinstance(coordinator, CanonicalAcceptanceCoordinator)
    assert issubclass(
        CanonicalAcceptanceAuditUnavailableError,
        PersistenceUnavailableError,
    )


@pytest.fixture
def canonical_persistence():
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=uid(900), secret=b"canonical-acceptance-test-secret-32-bytes"
    )
    authority = create_initial_workspace_bootstrap_authority()
    events = OutboxEventRegistry()
    events.register(
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload_type=CanonicalAcceptanceOutboxPayload,
    )
    policies = InMemoryAccessPolicyRepository()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=events,
        audit_events=AuditEventRegistry.with_default_families(),
        bootstrap_authority=authority,
        transaction_lock=asyncio.Lock(),
        canonical_policy_repository=policies,
        canonical_clock=lambda: NOW,
    )

    async def initialize() -> None:
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            authority,
            Workspace(id=synthetic_uuid(901), slug="canonical", display_name="Canonical"),
        )

    asyncio.run(initialize())

    def workspace_context() -> object:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=synthetic_uuid(901)),
            service_principal_id=uid(902),
            purpose=PersistencePurpose("canonical_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=uid(903),
        )

    def source_context() -> object:
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=synthetic_uuid(901), environment_id=synthetic_uuid(910)),
            service_principal_id=uid(902),
            purpose=PersistencePurpose("canonical_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=uid(904),
        )

    def canonical_context() -> object:
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=synthetic_uuid(901), environment_id=synthetic_uuid(910)),
            service_principal_id=uid(902),
            purpose=PersistencePurpose("canonical_test"),
            operation=PersistenceOperation("canonical_acceptance"),
            trace_id=uid(904),
        )

    return persistence, policies, workspace_context, source_context, canonical_context


async def activate_policy(
    policies: InMemoryAccessPolicyRepository, source_object
) -> UUID:
    policy = AccessPolicyVersion(
        access_policy_version_id=uid(401),
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        source_object_id=source_object.source_object_id,
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.ELIGIBLE_TEMPLATE,
        origin_reference=uid(402),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=uid(403),
        created_at=NOW,
        grants=(
            AccessGrant(
                access_grant_id=uid(404),
                principal_kind=PrincipalKind.SERVICE,
                principal_id=uid(902),
                permission=AccessPermission.PROCESS_CONTENT,
                purpose="canonical_test",
                operation="canonical_acceptance",
            ),
        ),
    )
    policy = policy.model_copy(
        update={"document_hash": access_policy_document_hash(policy)}
    )
    command = AccessPolicyActivationCommand(
        policy=policy,
        requester_canonical_human_id=uid(403),
        activator_canonical_human_id=uid(403),
        expected_policy_version_id=None,
        expected_policy_generation=0,
        expected_cas_version=0,
        activation_id=uid(405),
        idempotency_key=IdempotencyKey("canonical-policy"),
        audit_event_id=uid(406),
        outbox_event_id=uid(407),
        activated_at=NOW,
    )
    draft = PolicyActivationDraft(
        values={
            "policy_activation_id": command.activation_id,
            "workspace_id": policy.workspace_id,
            "environment_id": policy.environment_id,
            "source_object_id": policy.source_object_id,
            "predecessor_policy_version_id": None,
            "new_policy_version_id": policy.access_policy_version_id,
            "policy_generation": 1,
            "requester_canonical_human_id": command.requester_canonical_human_id,
            "activator_canonical_human_id": command.activator_canonical_human_id,
            "membership_generation": 1,
            "authority_generation": 1,
            "service_authority_generation": 1,
            "audit_event_id": command.audit_event_id,
            "outbox_event_id": command.outbox_event_id,
            "activated_at": NOW,
        }
    )
    await policies.activate(command, draft)
    return policy.access_policy_version_id


async def add_environment(persistence, workspace_context) -> None:
    async with persistence.uow_factory(workspace_context()) as uow:
        await uow.environments.add(
            Environment(
                id=synthetic_uuid(910),
                workspace_id=synthetic_uuid(901),
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="dev",
            )
        )
        await uow.commit()


def acceptance_command(source_object, revision, provenance_value, *, key, generation, expected=None, tombstone=False):
    audit_id = uid(700 + generation)
    outbox_id = uid(800 + generation)
    acceptance_id = uid(600 + generation)
    transition = "source.tombstoned" if tombstone else "source.accepted"
    audit = AuditEventRegistry.with_default_families().build_event(
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        event_type="canonical.transition",
        schema_version=1,
        payload=CanonicalTransitionAuditPayload(transition=transition),
        origin=ContextOrigin.WORKER,
        acting_subject_id=None,
        service_principal_id=uid(902),
        trace_id=uid(904),
        occurred_at=NOW,
        outcome=AuditOutcome.COMMITTED,
        reason="source_tombstoned" if tombstone else "source_accepted",
        target=AuditObjectReference(object_type="source_revision", object_id=revision.revision_id, schema_version=1),
        producer_deduplication_id=f"canonical:{revision.revision_id}",
        audit_event_id=audit_id,
    )
    events = OutboxEventRegistry()
    events.register(
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload_type=CanonicalAcceptanceOutboxPayload,
    )
    outbox = events.build_intent(
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload=CanonicalAcceptanceOutboxPayload(
            canonical_acceptance_id=acceptance_id,
            source_object_id=source_object.source_object_id,
            source_revision_id=revision.revision_id,
            accepted_generation=generation,
            audit_event_id=audit_id,
            transition=transition,
        ),
        trace_id=uid(904),
        aggregate=OpaqueObjectReference(
            object_type="source_object",
            object_id=source_object.source_object_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"canonical:{revision.revision_id}",
        event_id=outbox_id,
    )
    return CanonicalAcceptanceCommand.create(
        source=source_object,
        revision=revision,
        provenance=provenance_value,
        expected_current_revision_id=expected,
        idempotency_key=IdempotencyKey(key),
        canonical_acceptance_id=acceptance_id,
        admission_decision_id=uid(500 + generation) if not tombstone else None,
        access_policy_version_id=uid(400 + generation) if not tombstone else None,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=audit,
        outbox_intent=outbox,
    )


@pytest.mark.asyncio
async def test_canonical_acceptance_advances_head_and_replays_without_second_effect(
    canonical_persistence,
):
    persistence, policies, workspace_context, source_context, canonical_context = (
        canonical_persistence
    )
    await add_environment(persistence, workspace_context)
    source_object = source()
    revision = valid_revision(source_object)
    source_provenance = provenance(source_object, revision)
    async with persistence.uow_factory(source_context()) as uow:
        resolved = await uow.sources.resolve_or_create_source(source_object)
        await uow.sources.record_observation(
            observation_command(
                resolved,
                revision,
                source_provenance,
                "observe",
                receipt_association(resolved, revision, source_provenance),
            )
        )
        await uow.commit()
    policy_id = await activate_policy(policies, source_object)
    async with persistence.uow_factory(canonical_context()) as uow:
        resolved = await uow.sources.resolve_or_create_source(source_object)
        await uow.admissions.append_decision(
            AdmissionDecision(
                decision_id=uid(501),
                source_object_id=resolved.source_object_id,
                revision_id=revision.revision_id,
                workspace_id=resolved.workspace_id,
                environment_id=resolved.environment_id,
                decision_version=1,
                disposition=AdmissionDisposition.ADMITTED,
                validator_profile_version="admission:r1",
                reason_schema_version="reason.v1",
                reason_codes=("clean",),
                authority_reference={
                    "reference_type": "validator",
                    "reference_id": uid(502),
                    "schema_version": 1,
                },
                trace_id=uid(904),
                audit_event_id=uid(503),
                decided_at=NOW,
            )
        )
        command = acceptance_command(
            resolved, revision, source_provenance, key="accept-1", generation=1
        )
        command = CanonicalAcceptanceCommand.create(
            source=command.source,
            revision=command.revision,
            provenance=command.provenance,
            admission_decision_id=command.admission_decision_id,
            access_policy_version_id=policy_id,
            expected_current_revision_id=command.expected_current_revision_id,
            idempotency_key=command.idempotency_key,
            canonical_acceptance_id=command.canonical_acceptance_id,
            audit_event_id=command.audit_event_id,
            outbox_event_id=command.outbox_event_id,
            accepted_at=command.accepted_at,
            audit_event=command.audit_event,
            outbox_intent=command.outbox_intent,
        )
        first = await uow.canonical.accept(command)
        replay = await uow.canonical.accept(command)
        await uow.commit()
    assert first.advanced is True
    assert replay.replay is True
    assert replay.acceptance == first.acceptance
    assert len(persistence.uow_factory._store.state.canonical_acceptances) == 1
    assert len(persistence.uow_factory._store.state.current_source_heads) == 1

    forged_snapshots = (
        (
            resolved.model_copy(update={"source_kind": "forged"}),
            revision,
            source_provenance,
        ),
        (
            resolved,
            revision.model_copy(update={"media_type": "application/forged"}),
            source_provenance,
        ),
        (
            resolved,
            revision,
            source_provenance.model_copy(
                update={"order_scheme": "integer.v1", "order_token": "2"}
            ),
        ),
    )
    for number, (forged_source, forged_revision, forged_provenance) in enumerate(
        forged_snapshots, start=2
    ):
        forged = acceptance_command(
            forged_source,
            forged_revision,
            forged_provenance,
            key=f"forged-authority-{number}",
            generation=number,
            expected=revision.revision_id,
        )
        forged = CanonicalAcceptanceCommand.create(
            source=forged.source,
            revision=forged.revision,
            provenance=forged.provenance,
            admission_decision_id=uid(501),
            access_policy_version_id=policy_id,
            expected_current_revision_id=revision.revision_id,
            idempotency_key=forged.idempotency_key,
            canonical_acceptance_id=forged.canonical_acceptance_id,
            audit_event_id=forged.audit_event_id,
            outbox_event_id=forged.outbox_event_id,
            accepted_at=forged.accepted_at,
            audit_event=forged.audit_event,
            outbox_intent=forged.outbox_intent,
        )
        async with persistence.uow_factory(canonical_context()) as uow:
            with pytest.raises(CanonicalAuthorityConflictError):
                await uow.canonical.accept(forged)


def test_source_ordering_registry_uses_scheme_specific_numeric_order() -> None:
    registry = SourceOrderingPolicyRegistry.r1()

    assert registry.compare("integer.v1", "10", "9") is SourceOrderComparison.HIGHER
    assert registry.compare("integer.v1", "9", "10") is SourceOrderComparison.LOWER
    assert registry.compare("integer.v1", "10", "10") is SourceOrderComparison.EQUAL


@pytest.mark.parametrize(
    ("scheme", "incoming", "current"),
    (("unknown.v1", "1", "1"), ("integer.v1", "01", "1")),
)
def test_source_ordering_registry_rejects_unknown_or_noncanonical_tokens(
    scheme: str, incoming: str, current: str
) -> None:
    with pytest.raises(SourceOrderingPolicyError):
        SourceOrderingPolicyRegistry.r1().compare(scheme, incoming, current)


class RejectingCanonicalRepository:
    def __init__(self, error: Exception, events: list[str]) -> None:
        self._error = error
        self._events = events

    async def accept(self, command: CanonicalAcceptanceCommand):
        del command
        self._events.append("reject")
        raise self._error


class RejectingCanonicalUnitOfWork:
    def __init__(self, error: Exception, events: list[str]) -> None:
        self.canonical = RejectingCanonicalRepository(error, events)
        self._events = events

    async def __aenter__(self):
        self._events.append("begin")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc, traceback
        self._events.append("rollback" if exc_type is not None else "close")

    async def commit(self) -> None:
        self._events.append("commit")


class RecordingFailedAcceptanceAudit:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self._events = events
        self._fail = fail

    async def record_failed_cas(
        self, command: CanonicalAcceptanceCommand, reason_code: str
    ) -> None:
        del command
        self._events.append(f"audit:{reason_code}")
        if self._fail:
            raise RuntimeError("audit unavailable")


def coordinator_command() -> CanonicalAcceptanceCommand:
    source_object = source()
    source_revision = valid_revision(source_object)
    return acceptance_command(
        source_object,
        source_revision,
        provenance(source_object, source_revision),
        key="coordinator-rejection",
        generation=9,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (CanonicalAuthorityConflictError("conflict"), "canonical_authority_conflict"),
        (CanonicalEligibilityError("denied"), "canonical_evidence_ineligible"),
        (OptimisticConflictError("stale"), "canonical_order_or_cas_conflict"),
    ),
)
async def test_failed_acceptance_rolls_back_before_exactly_one_separate_audit(
    error: Exception, reason: str
) -> None:
    events: list[str] = []
    coordinator = CanonicalAcceptanceCoordinator(
        lambda context: RejectingCanonicalUnitOfWork(error, events),
        lambda command: object(),
        RecordingFailedAcceptanceAudit(events),
    )

    with pytest.raises(type(error)):
        await coordinator.accept(coordinator_command())

    assert events == ["begin", "reject", "rollback", f"audit:{reason}"]


@pytest.mark.asyncio
async def test_failed_acceptance_audit_failure_cannot_turn_denial_into_success() -> (
    None
):
    events: list[str] = []
    rejection = CanonicalEligibilityError("denied")
    coordinator = CanonicalAcceptanceCoordinator(
        lambda context: RejectingCanonicalUnitOfWork(rejection, events),
        lambda command: object(),
        RecordingFailedAcceptanceAudit(events, fail=True),
    )

    with pytest.raises(CanonicalAcceptanceAuditUnavailableError) as raised:
        await coordinator.accept(coordinator_command())

    assert raised.value.__cause__ is not None
    assert events == [
        "begin",
        "reject",
        "rollback",
        "audit:canonical_evidence_ineligible",
    ]
