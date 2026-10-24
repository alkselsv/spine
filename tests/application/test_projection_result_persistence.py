from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.projection_result_mapping import (
    AdapterProjectedObject,
    CanonicalProjectionMappingContext,
    ProjectionMappingConflictError,
    ProjectionMappingError,
    ProjectionPreparationStatus,
)
from spine.application.projection_result_persistence import (
    CanonicalProjectionEvidenceRequest,
    build_projection_result_persistence,
)
from spine.domain.projections import (
    OntologyVersion,
    ProjectionCandidateState,
    ProjectionReceipt,
    SourceProjectionReference,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.memory.contracts import ProjectCommand, StableLocator

from tests.application.test_projection_result_mapping import (
    CONFIG,
    NOW,
    RUN,
    _adapter_receipt,
    _command,
    _context,
    _digest,
)


@dataclass(slots=True)
class _CanonicalStore:
    context: CanonicalProjectionMappingContext
    ontology: OntologyVersion
    receipt: ProjectionReceipt | None = None
    references: tuple[SourceProjectionReference, ...] = ()
    reverse_references_on_read: bool = False
    fail_after_reference_count: int | None = None
    commits: int = 0


def _ontology(context: CanonicalProjectionMappingContext) -> OntologyVersion:
    payload = {
        "schema_version": "ontology-definition:r1",
        "entity_types": [{"name": "document"}],
        "relation_types": [],
    }
    return OntologyVersion(
        ontology_version_id=context.projection_config.ontology_version_id,
        workspace_id=context.run.workspace_id,
        environment_id=context.run.environment_id,
        ontology_name="qa",
        version=1,
        definition_payload=payload,
        definition_digest=_digest("ontology-definition:r1", payload),
        publication_eligible=True,
        publication_decision_reference="decision-1",
        publisher_authority_reference="authority-1",
        audit_event_reference="audit-1",
        published_at=NOW,
    )


class _ProjectionRepository:
    def __init__(self, uow: _UnitOfWork) -> None:
        self._uow = uow

    async def resolve_run(self, projection_run_id: UUID):
        return self._uow.store.context.run if projection_run_id == RUN else None

    async def resolve_config_version(self, config_id: UUID):
        return self._uow.store.context.projection_config if config_id == CONFIG else None

    async def resolve_ontology_version(self, ontology_version_id: UUID):
        ontology = self._uow.store.ontology
        return ontology if ontology_version_id == ontology.ontology_version_id else None

    async def list_partitions(self, projection_run_id: UUID):
        return (self._uow.store.context.partition,) if projection_run_id == RUN else ()

    async def list_receipts(self, projection_run_id: UUID):
        receipt = self._uow.pending_receipt
        return (receipt,) if projection_run_id == RUN and receipt is not None else ()

    async def list_source_references(self, projection_run_id: UUID):
        if projection_run_id != RUN:
            return ()
        references = self._uow.pending_references
        return tuple(reversed(references)) if self._uow.store.reverse_references_on_read else references

    async def record_receipt(self, command):
        if self._uow.pending_receipt is not None:
            return SimpleNamespace(value=self._uow.pending_receipt, replay=True)
        self._uow.pending_receipt = command.receipt
        return SimpleNamespace(value=command.receipt, replay=False)

    async def add_source_reference(self, reference):
        next_count = len(self._uow.pending_references) + 1
        if (
            self._uow.store.fail_after_reference_count is not None
            and next_count > self._uow.store.fail_after_reference_count
        ):
            raise ConstraintConflictError("synthetic source reference failure")
        self._uow.pending_references += (reference,)
        return reference


class _BoundaryRepository:
    def __init__(self, store: _CanonicalStore) -> None:
        self._store = store

    async def resolve(self, boundary_id: UUID):
        boundary = self._store.context.canonical_boundary
        return boundary if boundary_id == boundary.boundary_id else None

    async def resolve_members(self, boundary_id: UUID):
        boundary = self._store.context.canonical_boundary
        return self._store.context.boundary_members if boundary_id == boundary.boundary_id else ()


class _UnitOfWork:
    def __init__(self, factory: _UnitOfWorkFactory, store: _CanonicalStore) -> None:
        self._factory = factory
        self.store = store
        self.pending_receipt = self.store.receipt
        self.pending_references = self.store.references
        self.projections = _ProjectionRepository(self)
        self.boundaries = _BoundaryRepository(self.store)

    async def __aenter__(self):
        self._factory.active_count += 1
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self._factory.active_count -= 1

    async def commit(self) -> None:
        self.store.receipt = self.pending_receipt
        self.store.references = self.pending_references
        self.store.commits += 1


class _UnitOfWorkFactory:
    def __init__(self, *stores: _CanonicalStore) -> None:
        self._stores = {
            (store.context.run.workspace_id, store.context.run.environment_id): store
            for store in stores
        }
        self.active_count = 0

    def __call__(self, context: TrustedPersistenceContext) -> _UnitOfWork:
        assert isinstance(context.scope, EnvironmentScope)
        key = (context.scope.workspace_id, context.scope.environment_id)
        return _UnitOfWork(self, self._stores[key])


class _LocatorResolver:
    def __init__(
        self,
        context: CanonicalProjectionMappingContext,
        uow_factory: _UnitOfWorkFactory,
    ) -> None:
        self.context = context
        self.uow_factory = uow_factory
        self.requests: list[CanonicalProjectionEvidenceRequest] = []
        self.uow_states: list[int] = []

    async def resolve(self, request: CanonicalProjectionEvidenceRequest):
        self.requests.append(request)
        self.uow_states.append(self.uow_factory.active_count)
        return self.context.locators


def _trusted_context(command: ProjectCommand) -> TrustedPersistenceContext:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID(int=90),
        secret=b"projection-result-persistence-test-secret",
    )
    return boundary.worker(
        scope=EnvironmentScope(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
        ),
        service_principal_id=UUID(int=91),
        purpose=PersistencePurpose("projection_evidence"),
        operation=PersistenceOperation("projection_receipt"),
        trace_id=UUID(int=92),
    )


def _case(
    context: CanonicalProjectionMappingContext,
    *,
    reference_ids: tuple[UUID, ...] = (UUID(int=30),),
):
    store = _CanonicalStore(context=context, ontology=_ontology(context))
    uow_factory = _UnitOfWorkFactory(store)
    locator_resolver = _LocatorResolver(context, uow_factory)
    bridge = build_projection_result_persistence(
        uow_factory=uow_factory,
        context_factory=_trusted_context,
        locator_resolver=locator_resolver,
        reference_id_source=iter(reference_ids).__next__,
    )
    return store, uow_factory, locator_resolver, bridge


@pytest.mark.asyncio
async def test_valid_handoff_persists_receipt_and_references() -> None:
    command = _command()
    store, _, _, bridge = _case(_context(command))

    result = await bridge.map(command, _adapter_receipt(command))

    assert result.replay is False
    assert store.receipt == result.handoff.receipt_command.receipt
    assert store.references == result.handoff.source_references
    assert store.commits == 1


@pytest.mark.asyncio
async def test_recorded_handoff_replays_after_candidate_is_sealed() -> None:
    command = _command()
    context = _context(command)
    store, _, _, bridge = _case(context)
    await bridge.map(command, _adapter_receipt(command))
    store.context = context.model_copy(
        update={
            "run": context.run.model_copy(
                update={
                    "candidate_state": ProjectionCandidateState.SEALED,
                    "candidate_evidence_digest": "8" * 64,
                    "candidate_sealed_at": NOW,
                }
            )
        }
    )

    replay = await bridge.prepare(command)

    assert replay is ProjectionPreparationStatus.RECORDED


@pytest.mark.asyncio
async def test_map_race_replays_after_candidate_is_sealed() -> None:
    command = _command()
    context = _context(command)
    store, _, _, bridge = _case(
        context,
        reference_ids=(UUID(int=30), UUID(int=30)),
    )
    first = await bridge.map(command, _adapter_receipt(command))
    store.context = context.model_copy(
        update={
            "run": context.run.model_copy(
                update={
                    "candidate_state": ProjectionCandidateState.SEALED,
                    "candidate_evidence_digest": "8" * 64,
                    "candidate_sealed_at": NOW,
                }
            )
        }
    )

    replay = await bridge.map(command, _adapter_receipt(command))

    assert replay.replay is True
    assert replay.handoff == first.handoff


@pytest.mark.asyncio
async def test_recorded_identity_rejects_changed_command_context() -> None:
    command = _command()
    store, _, _, bridge = _case(_context(command))
    await bridge.map(command, _adapter_receipt(command))
    changed_command = command.model_copy(
        update={
            "projection_config": command.projection_config.model_copy(
                update={"version": "2"}
            )
        }
    )

    with pytest.raises(ProjectionMappingError):
        await bridge.prepare(changed_command)

    assert store.commits == 1


@pytest.mark.asyncio
async def test_foreign_workspace_receipt_is_isolated_from_command_scope() -> None:
    command = _command()
    foreign_command = _command(workspace_id=UUID(int=99))
    context = _context(command)
    foreign_context = _context(foreign_command)
    store = _CanonicalStore(context=context, ontology=_ontology(context))
    foreign_store = _CanonicalStore(
        context=foreign_context,
        ontology=_ontology(foreign_context),
    )
    uow_factory = _UnitOfWorkFactory(store, foreign_store)
    bridge = build_projection_result_persistence(
        uow_factory=uow_factory,
        context_factory=_trusted_context,
        locator_resolver=_LocatorResolver(context, uow_factory),
        reference_id_source=iter((UUID(int=30),)).__next__,
    )
    foreign_bridge = build_projection_result_persistence(
        uow_factory=uow_factory,
        context_factory=_trusted_context,
        locator_resolver=_LocatorResolver(foreign_context, uow_factory),
        reference_id_source=iter((UUID(int=31),)).__next__,
    )
    foreign_result = await foreign_bridge.map(
        foreign_command,
        _adapter_receipt(foreign_command),
    )
    foreign_effect = (foreign_store.receipt, foreign_store.references)

    result = await bridge.map(command, _adapter_receipt(command))

    assert result.replay is False
    assert store.receipt == result.handoff.receipt_command.receipt
    assert foreign_effect == (foreign_store.receipt, foreign_store.references)
    assert foreign_store.receipt == foreign_result.handoff.receipt_command.receipt


@pytest.mark.asyncio
async def test_duplicate_delivery_replays_without_second_effect() -> None:
    command = _command()
    store, _, _, bridge = _case(
        _context(command),
        reference_ids=(UUID(int=30), UUID(int=31)),
    )
    first = await bridge.map(command, _adapter_receipt(command))

    replay = await bridge.map(command, _adapter_receipt(command))

    assert replay.replay is True
    assert replay.handoff == first.handoff
    assert store.commits == 1


@pytest.mark.asyncio
async def test_duplicate_command_replays_with_new_adapter_idempotency_key() -> None:
    command = _command()
    store, _, _, bridge = _case(
        _context(command),
        reference_ids=(UUID(int=30), UUID(int=31)),
    )
    first = await bridge.map(command, _adapter_receipt(command))
    redelivery = _adapter_receipt(command).model_copy(
        update={"idempotency_key": "projection-receipt-redelivery"}
    )

    replay = await bridge.map(command, redelivery)

    assert replay.replay is True
    assert (
        replay.handoff.receipt_command.receipt
        == first.handoff.receipt_command.receipt
    )
    assert replay.handoff.source_references == first.handoff.source_references
    assert store.commits == 1


@pytest.mark.asyncio
async def test_retry_after_mapping_ignores_new_adapter_observation_time() -> None:
    command = _command()
    store, _, _, bridge = _case(
        _context(command),
        reference_ids=(UUID(int=30), UUID(int=31)),
    )
    first = await bridge.map(command, _adapter_receipt(command))
    redelivery = _adapter_receipt(command).model_copy(
        update={"completed_at": NOW + timedelta(seconds=30)}
    )

    replay = await bridge.map(command, redelivery)

    assert replay.replay is True
    assert replay.handoff.receipt_command.receipt == first.handoff.receipt_command.receipt
    assert replay.handoff.source_references == first.handoff.source_references
    assert store.commits == 1


@pytest.mark.asyncio
async def test_reference_failure_rolls_back_and_retry_has_one_logical_effect() -> None:
    command = _command()
    context = _context(command)
    second_locator = context.locators[0].model_copy(
        update={
            "segment_id": "segment-2",
            "locator": StableLocator(kind="page", value="3"),
            "locator_digest": "d" * 64,
        }
    )
    context = context.model_copy(update={"locators": context.locators + (second_locator,)})
    receipt = _adapter_receipt(command)
    second_reference = receipt.source_references[0].model_copy(
        update={
            "segment_id": "segment-2",
            "locator": StableLocator(kind="page", value="3"),
            "locator_digest": "d" * 64,
            "projected_object_id": "chunk-2",
        }
    )
    receipt = receipt.model_copy(
        update={
            "emitted_objects": receipt.emitted_objects
            + (AdapterProjectedObject(kind="chunk", object_id="chunk-2"),),
            "source_references": receipt.source_references + (second_reference,),
        }
    )
    store, _, _, bridge = _case(
        context,
        reference_ids=(UUID(int=30), UUID(int=31), UUID(int=32), UUID(int=33)),
    )
    store.fail_after_reference_count = 1

    with pytest.raises(ProjectionMappingError, match="evidence was rejected"):
        await bridge.map(command, receipt)

    assert store.receipt is None
    assert store.references == ()
    store.fail_after_reference_count = None

    recovered = await bridge.map(command, receipt)

    assert recovered.replay is False
    assert len(store.references) == 2
    assert store.commits == 1


@pytest.mark.asyncio
async def test_locator_resolution_runs_outside_unit_of_work() -> None:
    command = _command()
    _, _, resolver, bridge = _case(_context(command))

    await bridge.map(command, _adapter_receipt(command))

    assert resolver.uow_states == [0]


@pytest.mark.asyncio
async def test_evidence_request_retains_canonical_ontology() -> None:
    command = _command()
    store, _, resolver, bridge = _case(_context(command))

    await bridge.map(command, _adapter_receipt(command))

    assert resolver.requests[0].ontology == store.ontology


@pytest.mark.asyncio
async def test_same_identity_with_another_digest_is_rejected() -> None:
    command = _command()
    store, _, _, bridge = _case(
        _context(command),
        reference_ids=(UUID(int=30),),
    )
    await bridge.map(command, _adapter_receipt(command))
    conflict = _command(digest="0" * 64)

    with pytest.raises(ProjectionMappingConflictError, match="digest conflict"):
        await bridge.map(conflict, _adapter_receipt(conflict))

    assert store.commits == 1


@pytest.mark.asyncio
async def test_missing_canonical_locator_evidence_is_rejected() -> None:
    command = _command()
    context = _context(command)
    missing = context.model_copy(update={"locators": ()})
    store, _, _, bridge = _case(missing)

    with pytest.raises(ProjectionMappingError, match="locator evidence is missing"):
        await bridge.map(command, _adapter_receipt(command))

    assert store.receipt is None


@pytest.mark.asyncio
async def test_replay_is_independent_of_reference_read_order() -> None:
    command = _command()
    context = _context(command)
    second_locator = context.locators[0].model_copy(
        update={
            "segment_id": "segment-2",
            "locator": StableLocator(kind="page", value="3"),
            "locator_digest": "d" * 64,
        }
    )
    context = context.model_copy(update={"locators": context.locators + (second_locator,)})
    receipt = _adapter_receipt(command)
    second_reference = receipt.source_references[0].model_copy(
        update={
            "segment_id": "segment-2",
            "locator": StableLocator(kind="page", value="3"),
            "locator_digest": "d" * 64,
            "projected_object_id": "chunk-2",
        }
    )
    receipt = receipt.model_copy(
        update={
            "emitted_objects": receipt.emitted_objects
            + (AdapterProjectedObject(kind="chunk", object_id="chunk-2"),),
            "source_references": receipt.source_references + (second_reference,),
        }
    )
    store, _, _, bridge = _case(
        context,
        reference_ids=(UUID(int=30), UUID(int=31), UUID(int=32), UUID(int=33)),
    )
    first = await bridge.map(command, receipt)
    store.reverse_references_on_read = True

    replay = await bridge.map(command, receipt)

    assert replay.replay is True
    assert replay.handoff == first.handoff


@pytest.mark.asyncio
async def test_cross_scope_ontology_is_rejected() -> None:
    command = _command()
    context = _context(command)
    store, _, _, bridge = _case(context)
    store.ontology = store.ontology.model_copy(update={"workspace_id": UUID(int=99)})

    with pytest.raises(ProjectionMappingError, match="ontology.*ineligible"):
        await bridge.map(command, _adapter_receipt(command))

    assert store.receipt is None
