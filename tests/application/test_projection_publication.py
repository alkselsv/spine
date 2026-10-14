from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.idempotency import IdempotencyKey, OpaqueResultReference
from spine.application.persistence.projection import (
    ProjectionActivationCommand,
    ProjectionActivationKind,
    ProjectionActivationRejected,
    ProjectionActivationReplay,
    ProjectionCandidateSealCommand,
)
from spine.application.projection_publication import (
    ProjectionPublicationCoordinator,
    build_successor_rollback_command,
)
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionConfigVersion,
    ProjectionHealth,
    ProjectionSnapshot,
    ProjectionSnapshotState,
    ProjectionFreshness,
    ProjectionActivation,
    PublishedActivePublication,
)
from tests.application.test_physical_projection_validation import (
    _command as _physical_command,
    _evidence as _physical_evidence,
    _mapped_result as _mapped_physical_result,
    _physical_artifact,
)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
CONFIG = UUID(int=3)
BOUNDARY = UUID(int=4)
ACTIVE_SNAPSHOT = UUID(int=5)
ACTIVE_ACTIVATION = UUID(int=6)
SUCCESSOR_SNAPSHOT = UUID(int=7)


def _config() -> ProjectionConfigVersion:
    config_digest = sha256(
        json.dumps(
            {"schema": "projection-config:r1", "payload": {}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ProjectionConfigVersion(
        projection_config_version_id=CONFIG,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=8),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload={},
        config_digest=config_digest,
        created_at=NOW,
    )


def _snapshot(snapshot_id: UUID, *, predecessor: UUID | None = None) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        projection_snapshot_id=snapshot_id,
        projection_run_id=UUID(int=9),
        projection_candidate_id=UUID(int=10),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        snapshot_state=ProjectionSnapshotState.FINALIZED,
        projection_kind="qa",
        projection_config_version_id=CONFIG,
        canonical_boundary_id=BOUNDARY,
        predecessor_snapshot_id=predecessor,
        candidate_evidence_digest="b" * 64,
        manifest_digest="c" * 64,
        member_count=0,
        created_at=NOW,
    )


def _publication() -> PublishedActivePublication:
    snapshot = _snapshot(ACTIVE_SNAPSHOT)
    activation = ProjectionActivation(
        projection_activation_id=ACTIVE_ACTIVATION,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=ACTIVE_SNAPSHOT,
        activation_generation=4,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activation-1",
        command_digest="d" * 64,
        idempotency_receipt_id=UUID(int=11),
        audit_event_id=UUID(int=12),
        activated_at=NOW,
    )
    return PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=snapshot,
        projection_activation=activation,
        activation_generation=4,
        active_pointer_cas_version=9,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_manifest_digest="e" * 64,
        accepted_through_generation=12,
        canonical_boundary_membership_ref="members",
        as_of=CanonicalAsOf(
            canonical_boundary_id=BOUNDARY,
            accepted_through_generation=12,
            boundary_time=NOW,
        ),
        projection_config_version=_config(),
        freshness=ProjectionFreshness.CURRENT,
        health=ProjectionHealth.HEALTHY,
    )


def test_rollback_command_targets_only_a_validated_successor_snapshot() -> None:
    current = _publication()
    successor = _snapshot(SUCCESSOR_SNAPSHOT, predecessor=ACTIVE_SNAPSHOT)

    command = build_successor_rollback_command(
        current,
        successor,
        command_identity="rollback-1",
        idempotency_key=IdempotencyKey("rollback-1"),
    )

    assert command.activation_kind is ProjectionActivationKind.ROLLBACK
    assert command.projection_snapshot_id == SUCCESSOR_SNAPSHOT
    assert command.expected_predecessor_snapshot_id == ACTIVE_SNAPSHOT
    assert command.expected_predecessor_activation_generation == 4
    assert command.expected_active_pointer_cas_version == 9


def test_rollback_command_rejects_historical_snapshot_without_successor_lineage() -> None:
    current = _publication()

    with pytest.raises(ValueError, match="successor"):
        build_successor_rollback_command(
            current,
            _snapshot(UUID(int=99)),
            command_identity="rollback-1",
            idempotency_key=IdempotencyKey("rollback-1"),
        )


@pytest.mark.asyncio
async def test_publish_runs_physical_gate_before_postgresql_activation() -> None:
    events: list[str] = []

    class EvidenceCoordinator:
        async def validate_and_seal(self, evidence, seal_command):
            events.append("seal")
            return SimpleNamespace(value="sealed", replay=False)

    class Repository:
        async def activate(self, command):
            events.append("activate")
            return "activated"

    class UnitOfWork:
        projections = Repository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def commit(self):
            events.append("commit")

    command = ProjectionActivationCommand.create(
        projection_kind="qa",
        projection_snapshot_id=SUCCESSOR_SNAPSHOT,
        activation_kind=ProjectionActivationKind.PUBLISH,
        expected_predecessor_snapshot_id=None,
        expected_predecessor_activation_generation=None,
        expected_active_pointer_cas_version=None,
        command_identity="activation-1",
        idempotency_key=IdempotencyKey("activation-1"),
    )
    coordinator = ProjectionPublicationCoordinator(
        physical_evidence=EvidenceCoordinator(),
        uow_factory=lambda context: UnitOfWork(),
        context_factory=lambda command: object(),
    )

    result = await coordinator.publish(
        evidence=object(),
        seal_command=ProjectionCandidateSealCommand.create(
            projection_run_id=UUID(int=9),
            projection_candidate_id=UUID(int=10),
            idempotency_key=IdempotencyKey("seal-1"),
        ),
        activation_command=command,
    )

    assert result == "activated"
    assert events == ["seal", "activate", "commit"]


@pytest.mark.asyncio
async def test_failed_physical_gate_does_not_attempt_activation() -> None:
    events: list[str] = []

    class EvidenceCoordinator:
        async def validate_and_seal(self, evidence, seal_command):
            events.append("seal")
            raise ValueError("physical gate rejected")

    class Repository:
        async def activate(self, command):
            events.append("activate")
            return "activated"

    class UnitOfWork:
        projections = Repository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def commit(self):
            events.append("commit")

    command = ProjectionActivationCommand.create(
        projection_kind="qa",
        projection_snapshot_id=SUCCESSOR_SNAPSHOT,
        activation_kind=ProjectionActivationKind.PUBLISH,
        expected_predecessor_snapshot_id=None,
        expected_predecessor_activation_generation=None,
        expected_active_pointer_cas_version=None,
        command_identity="activation-2",
        idempotency_key=IdempotencyKey("activation-2"),
    )
    coordinator = ProjectionPublicationCoordinator(
        physical_evidence=EvidenceCoordinator(),
        uow_factory=lambda context: UnitOfWork(),
        context_factory=lambda command: object(),
    )

    with pytest.raises(ValueError, match="physical gate"):
        await coordinator.publish(
            evidence=object(),
            seal_command=ProjectionCandidateSealCommand.create(
                projection_run_id=UUID(int=9),
                projection_candidate_id=UUID(int=10),
                idempotency_key=IdempotencyKey("seal-2"),
            ),
            activation_command=command,
        )

    assert events == ["seal"]


@pytest.mark.asyncio
async def test_rollback_delegates_successor_cas_claim_to_publication_repository() -> None:
    commands = []

    class Repository:
        async def activate(self, command):
            commands.append(command)
            return "rollback-accepted"

    class UnitOfWork:
        projections = Repository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def commit(self):
            return None

    coordinator = ProjectionPublicationCoordinator(
        physical_evidence=SimpleNamespace(),
        uow_factory=lambda context: UnitOfWork(),
        context_factory=lambda command: object(),
    )

    result = await coordinator.rollback(
        current=_publication(),
        successor=_snapshot(SUCCESSOR_SNAPSHOT, predecessor=ACTIVE_SNAPSHOT),
        command_identity="rollback-1",
        idempotency_key=IdempotencyKey("rollback-1"),
    )

    assert result == "rollback-accepted"
    assert len(commands) == 1
    assert commands[0].activation_kind is ProjectionActivationKind.ROLLBACK
    assert commands[0].expected_active_pointer_cas_version == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["replay", "cas_conflict"])
async def test_publish_returns_repository_replay_or_cas_rejection(
    outcome: str,
) -> None:
    current = _publication()
    expected = (
        ProjectionActivationReplay(
            activation=current.projection_activation,
            drift_count=0,
            result_reference=OpaqueResultReference(
                "projection_activation", ACTIVE_ACTIVATION, 1
            ),
        )
        if outcome == "replay"
        else ProjectionActivationRejected(
            code="expected_predecessor_conflict", retryable=True
        )
    )

    class EvidenceCoordinator:
        async def validate_and_seal(self, evidence, seal_command):
            return SimpleNamespace(value="sealed", replay=False)

    class Repository:
        async def activate(self, command):
            return expected

    class UnitOfWork:
        projections = Repository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def commit(self):
            return None

    command = ProjectionActivationCommand.create(
        projection_kind="qa",
        projection_snapshot_id=SUCCESSOR_SNAPSHOT,
        activation_kind=ProjectionActivationKind.PUBLISH,
        expected_predecessor_snapshot_id=None,
        expected_predecessor_activation_generation=None,
        expected_active_pointer_cas_version=None,
        command_identity="activation-replay",
        idempotency_key=IdempotencyKey("activation-replay"),
    )
    coordinator = ProjectionPublicationCoordinator(
        physical_evidence=EvidenceCoordinator(),
        uow_factory=lambda context: UnitOfWork(),
        context_factory=lambda command: object(),
    )

    result = await coordinator.publish(
        evidence=object(),
        seal_command=ProjectionCandidateSealCommand.create(
            projection_run_id=UUID(int=9),
            projection_candidate_id=UUID(int=10),
            idempotency_key=IdempotencyKey("seal-replay"),
        ),
        activation_command=command,
    )

    assert result == expected


@pytest.mark.asyncio
async def test_publish_from_adapter_result_maps_and_validates_before_activation() -> None:
    physical_command = _physical_command()
    mapped = await _mapped_physical_result(physical_command)
    expected_evidence = _physical_evidence(physical_command, mapped)
    observed = []

    class MappingBridge:
        async def map(self, command, adapter_receipt):
            return mapped

    class EvidenceCoordinator:
        async def validate_and_seal(self, evidence, seal_command):
            observed.append(evidence)
            return SimpleNamespace(value="sealed", replay=False)

    class Repository:
        async def activate(self, command):
            return "published"

    class UnitOfWork:
        projections = Repository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def commit(self):
            return None

    activation = ProjectionActivationCommand.create(
        projection_kind="qa",
        projection_snapshot_id=SUCCESSOR_SNAPSHOT,
        activation_kind=ProjectionActivationKind.PUBLISH,
        expected_predecessor_snapshot_id=None,
        expected_predecessor_activation_generation=None,
        expected_active_pointer_cas_version=None,
        command_identity="activation-adapter",
        idempotency_key=IdempotencyKey("activation-adapter"),
    )
    coordinator = ProjectionPublicationCoordinator(
        physical_evidence=EvidenceCoordinator(),
        uow_factory=lambda context: UnitOfWork(),
        context_factory=lambda command: object(),
        mapping_bridge=MappingBridge(),
    )

    result = await coordinator.publish_from_adapter_result(
        command=physical_command,
        adapter_receipt=mapped.handoff.adapter_receipt,
        expectation=expected_evidence.expectation,
        actual_artifacts=(_physical_artifact(physical_command),),
        evaluated_at=NOW,
        seal_command=ProjectionCandidateSealCommand.create(
            projection_run_id=UUID(int=9),
            projection_candidate_id=UUID(int=10),
            idempotency_key=IdempotencyKey("seal-adapter"),
        ),
        activation_command=activation,
    )

    assert result == "published"
    assert observed[0].result.passed is True
