"""In-memory recovery repository used by application contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from spine.application.storage.recovery import (
    RecoveryCoordinationError,
    RecoveryManifestError,
    RecoveryRepository,
    RecoveryTransaction,
)
from spine.domain.storage.recovery import (
    BackupFenceLease,
    FenceOperationTicket,
    QuiescentEvidence,
    RecoveryEvidenceInvalidation,
    StorageRecoveryManifest,
)


class _Transaction:
    def __init__(self, repository: "InMemoryRecoveryRepository") -> None:
        self._repository = repository

    async def get_lease(self, deployment_scope_id: str) -> BackupFenceLease | None:
        return self._repository._leases.get(deployment_scope_id)

    async def save_lease(self, lease: BackupFenceLease) -> None:
        self._repository._leases[lease.deployment_scope_id] = lease

    async def next_ticket_sequence(self, deployment_scope_id: str) -> int:
        sequences = [
            ticket.ticket_sequence
            for ticket in self._repository._tickets.values()
            if ticket.deployment_scope_id == deployment_scope_id
        ]
        return max(sequences, default=0) + 1

    async def add_ticket(self, ticket: FenceOperationTicket) -> None:
        self._repository._tickets[ticket.ticket_id] = ticket

    async def get_ticket(self, ticket_id: str) -> FenceOperationTicket | None:
        return self._repository._tickets.get(ticket_id)

    async def replace_ticket(self, ticket: FenceOperationTicket) -> None:
        if ticket.ticket_id not in self._repository._tickets:
            raise RecoveryCoordinationError("ticket does not exist")
        self._repository._tickets[ticket.ticket_id] = ticket

    async def list_tickets(
        self, deployment_scope_id: str, *, through_sequence: int | None = None
    ) -> tuple[FenceOperationTicket, ...]:
        tickets = (
            ticket
            for ticket in self._repository._tickets.values()
            if ticket.deployment_scope_id == deployment_scope_id
            and (
                through_sequence is None
                or ticket.ticket_sequence <= through_sequence
            )
        )
        return tuple(sorted(tickets, key=lambda item: item.ticket_sequence))

    async def get_quiescent_evidence(
        self, deployment_scope_id: str, fence_epoch: int
    ) -> QuiescentEvidence | None:
        return self._repository._quiescent_evidence.get(
            (deployment_scope_id, fence_epoch)
        )

    async def save_quiescent_evidence(
        self, deployment_scope_id: str, evidence: QuiescentEvidence
    ) -> None:
        self._repository._quiescent_evidence[
            (deployment_scope_id, evidence.fence_epoch)
        ] = evidence

    async def add_manifest(self, manifest: StorageRecoveryManifest) -> None:
        if manifest.recovery_set_id in self._repository._manifests:
            raise RecoveryManifestError("recovery manifest identity already exists")
        self._repository._manifests[manifest.recovery_set_id] = manifest

    async def get_manifest(self, recovery_set_id: str) -> StorageRecoveryManifest | None:
        return self._repository._manifests.get(recovery_set_id)

    async def list_manifests(
        self, deployment_scope_id: str
    ) -> tuple[StorageRecoveryManifest, ...]:
        return tuple(
            sorted(
                (
                    manifest
                    for manifest in self._repository._manifests.values()
                    if manifest.deployment_scope_id == deployment_scope_id
                ),
                key=lambda value: (value.created_at, value.recovery_set_id),
            )
        )

    async def list_invalidations(
        self, deployment_scope_id: str
    ) -> tuple[RecoveryEvidenceInvalidation, ...]:
        return tuple(
            sorted(
                (
                    event
                    for event in self._repository._invalidations.values()
                    if event.deployment_scope_id == deployment_scope_id
                ),
                key=lambda value: value.sequence,
            )
        )

    async def add_invalidation(self, event: RecoveryEvidenceInvalidation) -> None:
        key = (event.deployment_scope_id, event.sequence)
        if key in self._repository._invalidations:
            raise RecoveryCoordinationError("recovery evidence sequence already exists")
        self._repository._invalidations[key] = event


class InMemoryRecoveryRepository(RecoveryRepository):
    """Deterministic repository fake with transaction rollback semantics."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._leases: dict[str, BackupFenceLease] = {}
        self._tickets: dict[str, FenceOperationTicket] = {}
        self._quiescent_evidence: dict[tuple[str, int], QuiescentEvidence] = {}
        self._manifests: dict[str, StorageRecoveryManifest] = {}
        self._invalidations: dict[tuple[str, int], RecoveryEvidenceInvalidation] = {}

    async def seed_manifest(self, manifest: StorageRecoveryManifest) -> None:
        """Seed immutable evidence for an application contract test."""

        async with self._lock:
            if manifest.recovery_set_id in self._manifests:
                raise RecoveryManifestError("recovery manifest identity already exists")
            self._manifests[manifest.recovery_set_id] = manifest

    @asynccontextmanager
    async def transaction(self, deployment_scope_id: str) -> AsyncIterator[RecoveryTransaction]:
        del deployment_scope_id
        async with self._lock:
            leases = self._leases.copy()
            tickets = self._tickets.copy()
            evidence = self._quiescent_evidence.copy()
            manifests = self._manifests.copy()
            invalidations = self._invalidations.copy()
            try:
                yield _Transaction(self)
            except BaseException:
                self._leases = leases
                self._tickets = tickets
                self._quiescent_evidence = evidence
                self._manifests = manifests
                self._invalidations = invalidations
                raise


__all__ = ["InMemoryRecoveryRepository"]
