"""Application handoff from physical projection evidence to publication.

The module composes the physical validation seam with the PostgreSQL-owned
projection repository.  It never mutates an active pointer itself; activation,
CAS checks, audit and outbox effects remain inside the repository owned by #89.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Protocol

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.projection import (
    ProjectionActivationCommand,
    ProjectionActivationKind,
    ProjectionActivationResult,
    ProjectionCandidateSealCommand,
    ProjectionMutationResult,
    ProjectionRepository,
)
from spine.application.physical_projection_validation import (
    PhysicalProjectionEvidence,
    PhysicalProjectionEvidenceCoordinator,
    PhysicalProjectionReconciliationRequest,
)
from spine.application.projection_result_mapping import (
    AdapterProjectionReceipt,
    ProjectionResultMappingBridge,
)
from spine.domain.projections import (
    ProjectionSnapshot,
    ProjectionSnapshotState,
    PublishedActivePublication,
)
from spine.memory.contracts import ProjectionCommand, ProjectionRecoveryEvidence
from spine.memory.validation import (
    PhysicalArtifact,
    PhysicalProjectionExpectation,
    validate_physical_projection,
)


class ProjectionPublicationUnitOfWork(Protocol):
    @property
    def projections(self) -> ProjectionRepository: ...

    async def __aenter__(self) -> "ProjectionPublicationUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


def build_successor_rollback_command(
    current: PublishedActivePublication,
    successor: ProjectionSnapshot,
    *,
    command_identity: str,
    idempotency_key: IdempotencyKey,
) -> ProjectionActivationCommand:
    """Build a CAS claim for a new rollback successor.

    The helper only prepares a command.  The #89 repository must still verify
    the successor snapshot, its canonical boundary and all immutable evidence
    after taking its PostgreSQL locks.
    """

    if successor.snapshot_state is not ProjectionSnapshotState.FINALIZED:
        raise ValueError("rollback successor must be finalized")
    if (
        successor.workspace_id != current.workspace_id
        or successor.environment_id != current.environment_id
        or successor.projection_kind != current.projection_kind
    ):
        raise ValueError("rollback successor is outside the active publication scope")
    if successor.projection_snapshot_id == current.projection_snapshot.projection_snapshot_id:
        raise ValueError("rollback requires a successor snapshot")
    if successor.predecessor_snapshot_id != current.projection_snapshot.projection_snapshot_id:
        raise ValueError("rollback successor must point to the active snapshot")

    return ProjectionActivationCommand.create(
        projection_kind=current.projection_kind,
        projection_snapshot_id=successor.projection_snapshot_id,
        activation_kind=ProjectionActivationKind.ROLLBACK,
        expected_predecessor_snapshot_id=current.projection_snapshot.projection_snapshot_id,
        expected_predecessor_activation_generation=current.activation_generation,
        expected_active_pointer_cas_version=current.active_pointer_cas_version,
        command_identity=command_identity,
        idempotency_key=idempotency_key,
    )


class ProjectionPublicationCoordinator:
    """Coordinate physical evidence and PostgreSQL publication handoff."""

    def __init__(
        self,
        *,
        physical_evidence: PhysicalProjectionEvidenceCoordinator,
        uow_factory: Callable[[TrustedPersistenceContext], ProjectionPublicationUnitOfWork],
        context_factory: Callable[[ProjectionActivationCommand], TrustedPersistenceContext],
        mapping_bridge: ProjectionResultMappingBridge | None = None,
    ) -> None:
        self._physical_evidence = physical_evidence
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._mapping_bridge = mapping_bridge

    async def publish(
        self,
        *,
        evidence: PhysicalProjectionEvidence,
        seal_command: ProjectionCandidateSealCommand,
        activation_command: ProjectionActivationCommand,
    ) -> ProjectionActivationResult:
        """Gate publication on physical evidence, then delegate activation to #89."""

        if activation_command.activation_kind is not ProjectionActivationKind.PUBLISH:
            raise ValueError("publish requires a publish activation command")
        await self._physical_evidence.validate_and_seal(evidence, seal_command)
        return await self._activate(activation_command)

    async def reconcile(
        self,
        evidence: PhysicalProjectionEvidence,
        request: PhysicalProjectionReconciliationRequest,
    ) -> ProjectionMutationResult:
        """Persist physical drift/health evidence without changing a snapshot."""

        return await self._physical_evidence.reconcile(evidence, request)

    async def publish_from_adapter_result(
        self,
        *,
        command: ProjectionCommand,
        adapter_receipt: AdapterProjectionReceipt,
        expectation: PhysicalProjectionExpectation,
        actual_artifacts: tuple[PhysicalArtifact, ...],
        evaluated_at: datetime,
        seal_command: ProjectionCandidateSealCommand,
        activation_command: ProjectionActivationCommand,
        recovery: ProjectionRecoveryEvidence | None = None,
    ) -> ProjectionActivationResult:
        """Map and validate a completed adapter result before publication."""

        evidence = await self._evidence_from_adapter_result(
            command=command,
            adapter_receipt=adapter_receipt,
            expectation=expectation,
            actual_artifacts=actual_artifacts,
            evaluated_at=evaluated_at,
            recovery=recovery,
        )
        return await self.publish(
            evidence=evidence,
            seal_command=seal_command,
            activation_command=activation_command,
        )

    async def reconcile_from_adapter_result(
        self,
        *,
        command: ProjectionCommand,
        adapter_receipt: AdapterProjectionReceipt,
        expectation: PhysicalProjectionExpectation,
        actual_artifacts: tuple[PhysicalArtifact, ...],
        request: PhysicalProjectionReconciliationRequest,
        recovery: ProjectionRecoveryEvidence | None = None,
    ) -> ProjectionMutationResult:
        """Map a runtime adapter check into append-only reconciliation evidence."""

        evidence = await self._evidence_from_adapter_result(
            command=command,
            adapter_receipt=adapter_receipt,
            expectation=expectation,
            actual_artifacts=actual_artifacts,
            evaluated_at=request.checked_at,
            recovery=recovery,
        )
        return await self.reconcile(evidence, request)

    async def rollback(
        self,
        *,
        current: PublishedActivePublication,
        successor: ProjectionSnapshot,
        command_identity: str,
        idempotency_key: IdempotencyKey,
    ) -> ProjectionActivationResult:
        """Submit a successor rollback through the normal PostgreSQL CAS path."""

        command = build_successor_rollback_command(
            current,
            successor,
            command_identity=command_identity,
            idempotency_key=idempotency_key,
        )
        return await self._activate(command)

    async def _activate(
        self, command: ProjectionActivationCommand
    ) -> ProjectionActivationResult:
        async with self._uow_factory(self._context_factory(command)) as uow:
            result = await uow.projections.activate(command)
            await uow.commit()
            return result

    async def _evidence_from_adapter_result(
        self,
        *,
        command: ProjectionCommand,
        adapter_receipt: AdapterProjectionReceipt,
        expectation: PhysicalProjectionExpectation,
        actual_artifacts: tuple[PhysicalArtifact, ...],
        evaluated_at: datetime,
        recovery: ProjectionRecoveryEvidence | None,
    ) -> PhysicalProjectionEvidence:
        if self._mapping_bridge is None:
            raise RuntimeError("adapter publication handoff requires a mapping bridge")
        mapping = await self._mapping_bridge.map(command, adapter_receipt)
        return PhysicalProjectionEvidence(
            mapping=mapping,
            expectation=expectation,
            result=validate_physical_projection(expectation, actual_artifacts),
            recovery=recovery,
            evaluated_at=evaluated_at,
        )


__all__ = [
    "ProjectionPublicationCoordinator",
    "ProjectionPublicationUnitOfWork",
    "build_successor_rollback_command",
]
