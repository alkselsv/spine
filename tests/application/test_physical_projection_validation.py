from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.physical_projection_validation import (
    PHYSICAL_VALIDATION_GATE_KIND,
    PhysicalProjectionEvidence,
    PhysicalProjectionEvidenceCoordinator,
    PhysicalProjectionEvidenceTranslator,
    PhysicalProjectionReconciliationRequest,
    PhysicalProjectionValidationRejected,
)
from spine.application.persistence.projection import (
    ProjectionCandidateSealCommand,
    ProjectionValidationCommand,
    has_complete_physical_validation,
)
from spine.application.projection_result_mapping import ProjectionResultMappingBridge
from spine.application.testing import DeterministicCanonicalProjectionGateway
from spine.memory.validation import (
    ArtifactKind,
    PhysicalArtifact,
    PhysicalArtifactLocator,
    PhysicalProjectionExpectation,
    PhysicalProjectionSource,
    validate_physical_projection,
)
from spine.domain.projections import (
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionSnapshot,
    ProjectionSnapshotState,
)

from tests.application.test_projection_result_mapping import (
    CANDIDATE,
    CONFIG,
    ENVIRONMENT,
    NOW,
    PARTITION,
    REVISION,
    RUN,
    SOURCE,
    WORKSPACE,
    _adapter_receipt,
    _command,
    _context,
)


def _physical_artifact(command):
    locator = PhysicalArtifactLocator(
        kind="physical_line",
        reference="line:1:0-5",
        digest=sha256(b"line:1:0-5").hexdigest(),
    )
    return PhysicalArtifact(
        artifact_kind=ArtifactKind.CHUNK,
        artifact_id="chunk-1",
        data_id=UUID(int=50),
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        namespace_digest=command.physical_namespace.namespace_digest,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        revision_digest=command.source_revisions[0].revision_digest,
        revision_number=1,
        projection_config_id=CONFIG,
        projection_config_version="1",
        projection_config_digest=command.projection_config.config_digest,
        locators=(locator,),
    )


async def _mapped_result(command):
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )
    return await bridge.map(command, _adapter_receipt(command))


def _evidence(command, mapped) -> PhysicalProjectionEvidence:
    source = PhysicalProjectionSource(
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        revision_digest=command.source_revisions[0].revision_digest,
        revision_number=1,
    )
    expected = _physical_artifact(command)
    expectation = PhysicalProjectionExpectation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        namespace_digest=command.physical_namespace.namespace_digest,
        projection_config_id=CONFIG,
        projection_config_version="1",
        projection_config_digest=command.projection_config.config_digest,
        sources=(source,),
        expected_artifacts=(expected,),
    )
    result = validate_physical_projection(expectation, (expected,))
    return PhysicalProjectionEvidence(
        mapping=mapped,
        expectation=expectation,
        result=result,
        evaluated_at=NOW,
    )


@pytest.mark.asyncio
async def test_physical_evidence_translates_to_canonical_validation_with_lineage() -> None:
    command = _command()
    mapped = await _mapped_result(command)
    evidence = _evidence(command, mapped)

    validation = PhysicalProjectionEvidenceTranslator().to_validation_command(evidence)

    assert validation.validation.gate_kind == PHYSICAL_VALIDATION_GATE_KIND
    assert validation.validation.decision.value == "passed"
    assert validation.validation.projection_run_id == RUN
    assert validation.validation.projection_candidate_id == CANDIDATE
    assert validation.validation.metrics_payload["artifact_evidence"] == {
        "reference": "artifact:shadow:partition-project-1",
        "digest": "9" * 64,
    }
    assert validation.validation.metrics_payload["source_lineage"] == (
        {
            "source_object_id": str(SOURCE),
            "source_revision_id": str(REVISION),
            "revision_digest": "a" * 64,
            "revision_number": 1,
            "status": "current",
        },
    )


@pytest.mark.asyncio
async def test_physical_gate_must_cover_every_candidate_partition() -> None:
    command = _command()
    mapped = await _mapped_result(command)
    evidence = _evidence(command, mapped)
    validation = PhysicalProjectionEvidenceTranslator().to_validation_command(evidence)
    partition = _context(command).partition
    second_partition = partition.model_copy(
        update={"projection_partition_id": UUID(int=70)}
    )

    assert has_complete_physical_validation(
        (validation.validation,),
        (partition,),
        (mapped.handoff.receipt_command.receipt,),
        mapped.handoff.source_references,
        _context(command).run,
        _context(command).projection_config,
    )
    assert not has_complete_physical_validation(
        (validation.validation,),
        (partition, second_partition),
        (mapped.handoff.receipt_command.receipt,),
        mapped.handoff.source_references,
        _context(command).run,
        _context(command).projection_config,
    )
    failed = PhysicalProjectionEvidenceTranslator().to_validation_command(
        evidence.model_copy(
            update={"result": validate_physical_projection(evidence.expectation, ())}
        )
    )
    assert not has_complete_physical_validation(
        (validation.validation, failed.validation),
        (partition,),
        (mapped.handoff.receipt_command.receipt,),
        mapped.handoff.source_references,
        _context(command).run,
        _context(command).projection_config,
    )
    assert not has_complete_physical_validation(
        (validation.validation, validation.validation),
        (partition,),
        (mapped.handoff.receipt_command.receipt,),
        mapped.handoff.source_references,
        _context(command).run,
        _context(command).projection_config,
    )
    incomplete = validation.validation.model_copy(
        update={"metrics_payload": {"projection_lineage": {"partition_id": str(partition.projection_partition_id)}}}
    )
    assert not has_complete_physical_validation(
        (incomplete,),
        (partition,),
        (mapped.handoff.receipt_command.receipt,),
        mapped.handoff.source_references,
        _context(command).run,
        _context(command).projection_config,
    )


@pytest.mark.asyncio
async def test_physical_evidence_rejects_a_fabricated_scope_before_persistence() -> None:
    command = _command()
    mapped = await _mapped_result(command)
    expectation = PhysicalProjectionExpectation(
        workspace_id=UUID(int=99),
        environment_id=ENVIRONMENT,
        namespace_digest=command.physical_namespace.namespace_digest,
        projection_config_id=CONFIG,
        projection_config_version="1",
        projection_config_digest=command.projection_config.config_digest,
        sources=(
            PhysicalProjectionSource(
                source_object_id=SOURCE,
                source_revision_id=REVISION,
                revision_digest="a" * 64,
                revision_number=1,
            ),
        ),
    )

    with pytest.raises(ValueError, match="workspace"):
        PhysicalProjectionEvidence(
            mapping=mapped,
            expectation=expectation,
            result=validate_physical_projection(expectation, ()),
            evaluated_at=NOW,
        )


def _reconciliation_request() -> PhysicalProjectionReconciliationRequest:
    snapshot = ProjectionSnapshot(
        projection_snapshot_id=UUID(int=60),
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        snapshot_state=ProjectionSnapshotState.FINALIZED,
        projection_kind="context",
        projection_config_version_id=CONFIG,
        canonical_boundary_id=UUID(int=9),
        candidate_evidence_digest="a" * 64,
        manifest_digest="b" * 64,
        member_count=1,
        created_at=NOW,
    )
    activation = ProjectionActivation(
        projection_activation_id=UUID(int=61),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="context",
        projection_snapshot_id=snapshot.projection_snapshot_id,
        activation_generation=1,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activation-1",
        command_digest="c" * 64,
        idempotency_receipt_id=UUID(int=62),
        audit_event_id=UUID(int=63),
        activated_at=NOW,
    )
    return PhysicalProjectionReconciliationRequest(
        snapshot=snapshot,
        activation=activation,
        attempt_identity="physical-check-1",
        checked_at=NOW,
        idempotency_receipt_id=UUID(int=64),
    )


@pytest.mark.asyncio
async def test_failed_physical_evidence_becomes_failed_validation_and_degraded_reconciliation() -> None:
    command = _command()
    mapped = await _mapped_result(command)
    evidence = _evidence(command, mapped)
    failed = validate_physical_projection(evidence.expectation, ())
    evidence = evidence.model_copy(update={"result": failed})
    translator = PhysicalProjectionEvidenceTranslator()

    validation = translator.to_validation_command(evidence)
    reconciliation = translator.to_reconciliation_command(
        evidence, _reconciliation_request()
    )

    assert validation.validation.decision.value == "failed"
    assert validation.validation.metrics_payload["diagnostics"][0]["code"] == "missing_artifact"
    assert validation.validation.metrics_payload["repair_candidates"]
    assert reconciliation.result.outcome.value == "degraded"
    assert reconciliation.result.artifact_evidence_digest == "9" * 64
    assert reconciliation.result.findings_payload["validation_id"] == str(
        validation.validation.projection_validation_id
    )


class _Repository:
    def __init__(self) -> None:
        self.validations = {}
        self.seals = 0
        self.sealed = False

    async def record_validation(self, command):
        existing = self.validations.get(command.validation.projection_validation_id)
        if existing is not None:
            return SimpleNamespace(value=existing, replay=True)
        self.validations[command.validation.projection_validation_id] = command.validation
        return SimpleNamespace(value=command.validation, replay=False)

    async def seal_candidate(self, command):
        replay = self.sealed
        if not replay:
            self.seals += 1
            self.sealed = True
        return SimpleNamespace(value="sealed", replay=replay)


class _UnitOfWork:
    def __init__(self, repository: _Repository) -> None:
        self.projections = repository
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_validate_and_seal_records_failed_evidence_without_sealing_and_replays_success() -> None:
    command = _command()
    mapped = await _mapped_result(command)
    evidence = _evidence(command, mapped)
    repository = _Repository()
    unit = _UnitOfWork(repository)
    contexts = []

    coordinator = PhysicalProjectionEvidenceCoordinator(
        uow_factory=lambda context: unit,
        context_factory=lambda command: contexts.append(command) or None,  # type: ignore[return-value]
    )
    seal = ProjectionCandidateSealCommand.create(
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        idempotency_key=IdempotencyKey("seal-physical-1"),
    )

    sealed = await coordinator.validate_and_seal(evidence, seal)
    replay = await coordinator.validate_and_seal(evidence, seal)

    assert sealed.value == "sealed"
    assert replay.replay is True
    assert len(repository.validations) == 1
    assert repository.seals == 1
    assert isinstance(contexts[0], ProjectionValidationCommand)
    assert contexts[1] is seal

    failed_evidence = evidence.model_copy(
        update={"result": validate_physical_projection(evidence.expectation, ())}
    )
    with pytest.raises(PhysicalProjectionValidationRejected):
        await coordinator.validate_and_seal(failed_evidence, seal)
    assert len(repository.validations) == 2
    assert repository.seals == 1
