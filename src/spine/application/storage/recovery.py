"""Durable coordination protocol for Profile A and Profile B recovery sets."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import ValidationError

from spine.domain.storage.recovery import (
    BackupFenceLease,
    ComponentState,
    FenceOperationKind,
    FenceOperationState,
    FenceOperationTicket,
    FenceState,
    QuiescentEvidence,
    RecoveryComponentEvidence,
    RecoveryFailureCategory,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
    TicketAdmission,
    validate_ticket_transition,
)
from spine.domain.storage.backup_profile import BackupProtectionProfile


class RecoveryCoordinationError(RuntimeError):
    """Base class for safe, disclosure-free coordination failures."""


class FenceAdmissionBlockedError(RecoveryCoordinationError):
    """A fence-controlled operation cannot cross an active recovery fence."""


class FenceOwnershipError(RecoveryCoordinationError):
    """The caller does not hold the current fencing epoch and lease."""


class QuiescenceBlockedError(RecoveryCoordinationError):
    """The durable pre-fence ticket set is not proven quiescent."""


class RecoveryManifestError(RecoveryCoordinationError):
    """Recovery evidence cannot be promoted to the requested manifest state."""


class RecoveryEvidenceProvider(Protocol):
    """Adapter that captures provider-backed, sanitized recovery evidence."""

    async def capture_quiescent_watermark(
        self,
        deployment_scope_id: str,
        fence_epoch: int,
        cutoff_ticket_sequence: int,
    ) -> str: ...

    async def verify_component(
        self,
        deployment_scope_id: str,
        fence_epoch: int,
        component: str,
        boundary: str,
        *,
        cutoff_ticket_sequence: int,
    ) -> RecoveryComponentEvidence: ...


class RecoveryTransaction(Protocol):
    async def get_lease(self, deployment_scope_id: str) -> BackupFenceLease | None: ...

    async def save_lease(self, lease: BackupFenceLease) -> None: ...

    async def next_ticket_sequence(self, deployment_scope_id: str) -> int: ...

    async def add_ticket(self, ticket: FenceOperationTicket) -> None: ...

    async def get_ticket(self, ticket_id: str) -> FenceOperationTicket | None: ...

    async def replace_ticket(self, ticket: FenceOperationTicket) -> None: ...

    async def list_tickets(
        self, deployment_scope_id: str, *, through_sequence: int | None = None
    ) -> tuple[FenceOperationTicket, ...]: ...

    async def get_quiescent_evidence(
        self, deployment_scope_id: str, fence_epoch: int
    ) -> QuiescentEvidence | None: ...

    async def save_quiescent_evidence(
        self, deployment_scope_id: str, evidence: QuiescentEvidence
    ) -> None: ...

    async def add_manifest(self, manifest: StorageRecoveryManifest) -> None: ...

    async def get_manifest(self, recovery_set_id: str) -> StorageRecoveryManifest | None: ...


class RecoveryRepository(Protocol):
    @asynccontextmanager
    async def transaction(self, deployment_scope_id: str) -> AsyncIterator[RecoveryTransaction]: ...


class BackupRecoveryCoordinator:
    """Own the serialized lease/ticket/barrier and immutable manifest protocol."""

    def __init__(
        self,
        *,
        repository: RecoveryRepository,
        ticket_id_factory: Callable[[], str],
        recovery_set_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        evidence_provider: RecoveryEvidenceProvider,
    ) -> None:
        self._repository = repository
        self._ticket_id_factory = ticket_id_factory
        self._recovery_set_id_factory = recovery_set_id_factory
        self._clock = clock
        self._evidence_provider = evidence_provider

    async def lease(self, deployment_scope_id: str) -> BackupFenceLease:
        async with self._repository.transaction(deployment_scope_id) as transaction:
            return await self._require_lease(transaction, deployment_scope_id)

    async def manifest(
        self, deployment_scope_id: str, recovery_set_id: str
    ) -> StorageRecoveryManifest | None:
        async with self._repository.transaction(deployment_scope_id) as transaction:
            manifest = await transaction.get_manifest(recovery_set_id)
            if (
                manifest is not None
                and manifest.deployment_scope_id != deployment_scope_id
            ):
                raise RecoveryCoordinationError("recovery set is outside deployment scope")
            return manifest

    async def admit_operation(
        self,
        deployment_scope_id: str,
        operation_kind: FenceOperationKind,
        target_reference: str,
        *,
        correlation_id: str | None = None,
    ) -> FenceOperationTicket:
        now = self._now()
        async with self._repository.transaction(deployment_scope_id) as transaction:
            lease = await self._require_lease(transaction, deployment_scope_id)
            if lease.ticket_admission is not TicketAdmission.OPEN:
                raise FenceAdmissionBlockedError("operation admission is fenced")
            ticket = FenceOperationTicket(
                ticket_id=self._ticket_id_factory(),
                deployment_scope_id=deployment_scope_id,
                ticket_sequence=await transaction.next_ticket_sequence(deployment_scope_id),
                fence_epoch=lease.active_fence_epoch,
                operation_kind=operation_kind,
                target_reference=target_reference,
                admitted_at=now,
                correlation_id=correlation_id,
            )
            await transaction.add_ticket(ticket)
            return ticket

    async def activate_fence(
        self,
        deployment_scope_id: str,
        *,
        lease_owner: str,
        lease_duration: timedelta,
        takeover_reference: str | None = None,
    ) -> BackupFenceLease:
        now = self._now()
        self._validate_duration(lease_duration)
        async with self._repository.transaction(deployment_scope_id) as transaction:
            current = await self._require_lease(transaction, deployment_scope_id)
            if current.fence_state not in {FenceState.INACTIVE, FenceState.RELEASED}:
                raise FenceOwnershipError("another recovery fence is active")
            cutoff = await transaction.next_ticket_sequence(deployment_scope_id) - 1
            lease = BackupFenceLease(
                deployment_scope_id=deployment_scope_id,
                active_fence_epoch=(current.active_fence_epoch or 0) + 1,
                fence_state=FenceState.DRAINING,
                cutoff_ticket_sequence=cutoff,
                lease_owner=lease_owner,
                lease_expires_at=now + lease_duration,
                lease_renewal=now,
                takeover_reference=takeover_reference,
                ticket_admission=TicketAdmission.CLOSED,
            )
            await transaction.save_lease(lease)
            return lease

    async def renew_fence(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
        lease_duration: timedelta,
    ) -> BackupFenceLease:
        now = self._now()
        self._validate_duration(lease_duration)
        async with self._repository.transaction(deployment_scope_id) as transaction:
            current = await self._require_lease(transaction, deployment_scope_id)
            self._require_owner(current, fence_epoch=fence_epoch, lease_owner=lease_owner)
            lease = current.model_copy(
                update={
                    "lease_expires_at": now + lease_duration,
                    "lease_renewal": now,
                }
            )
            await transaction.save_lease(lease)
            return lease

    async def take_over_fence(
        self,
        deployment_scope_id: str,
        *,
        lease_owner: str,
        lease_duration: timedelta,
        takeover_reference: str,
    ) -> BackupFenceLease:
        now = self._now()
        self._validate_duration(lease_duration)
        async with self._repository.transaction(deployment_scope_id) as transaction:
            current = await self._require_lease(transaction, deployment_scope_id)
            if current.fence_state in {FenceState.INACTIVE, FenceState.RELEASED}:
                raise FenceOwnershipError("there is no expired fence to take over")
            if current.lease_expires_at is None or current.lease_expires_at > now:
                raise FenceOwnershipError("the current recovery lease has not expired")
            assert current.active_fence_epoch is not None
            lease = current.model_copy(
                update={
                    "active_fence_epoch": current.active_fence_epoch + 1,
                    "fence_state": FenceState.DRAINING,
                    "lease_owner": lease_owner,
                    "lease_expires_at": now + lease_duration,
                    "lease_renewal": now,
                    "takeover_reference": takeover_reference,
                    "ticket_admission": TicketAdmission.CLOSED,
                }
            )
            await transaction.save_lease(lease)
            return lease

    async def transition_ticket(
        self,
        deployment_scope_id: str,
        ticket_id: str,
        current_state: FenceOperationState,
        *,
        fence_epoch: int | None,
        lease_owner: str,
        safe_failure_category: RecoveryFailureCategory | None = None,
    ) -> FenceOperationTicket:
        now = self._now()
        async with self._repository.transaction(deployment_scope_id) as transaction:
            ticket = await transaction.get_ticket(ticket_id)
            if ticket is None or ticket.deployment_scope_id != deployment_scope_id:
                raise RecoveryCoordinationError("ticket does not exist")
            lease = await self._require_lease(transaction, deployment_scope_id)
            if lease.fence_state not in {FenceState.INACTIVE, FenceState.RELEASED}:
                self._require_owner(
                    lease, fence_epoch=fence_epoch or 0, lease_owner=lease_owner
                )
                if lease.active_fence_epoch != fence_epoch:
                    raise FenceOwnershipError("stale coordinator or fence epoch")
                if (
                    lease.cutoff_ticket_sequence is not None
                    and ticket.ticket_sequence > lease.cutoff_ticket_sequence
                ):
                    raise FenceOwnershipError("ticket was admitted after the fence cutoff")
            elif lease.fence_state is FenceState.RELEASED:
                raise FenceOwnershipError("ticket is outside the active fence")
            elif ticket.fence_epoch != fence_epoch:
                raise FenceOwnershipError("ticket was admitted under another fence epoch")
            validate_ticket_transition(ticket.state, current_state)
            if current_state is FenceOperationState.TERMINAL_FAILED and safe_failure_category is None:
                raise RecoveryCoordinationError("terminal failure requires a safe category")
            updated = ticket.model_copy(
                update={
                    "state": current_state,
                    "terminal_at": now
                    if current_state
                    in {
                        FenceOperationState.TERMINAL_SUCCEEDED,
                        FenceOperationState.TERMINAL_FAILED,
                        FenceOperationState.RECONCILED_ORPHAN,
                        FenceOperationState.INDETERMINATE,
                        FenceOperationState.CANCELLED_BEFORE_IRREVERSIBLE_BOUNDARY,
                    }
                    else None,
                    "safe_failure_category": safe_failure_category,
                }
            )
            await transaction.replace_ticket(updated)
            return updated

    async def mark_quiescent(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
    ) -> QuiescentEvidence:
        now = self._now()
        async with self._repository.transaction(deployment_scope_id) as transaction:
            lease = await self._require_lease(transaction, deployment_scope_id)
            self._require_owner(lease, fence_epoch=fence_epoch, lease_owner=lease_owner)
            if lease.fence_state not in {FenceState.REQUESTED, FenceState.DRAINING}:
                raise QuiescenceBlockedError("fence is not draining")
            assert lease.cutoff_ticket_sequence is not None
            tickets = await transaction.list_tickets(
                deployment_scope_id,
                through_sequence=lease.cutoff_ticket_sequence,
            )
            expected = lease.cutoff_ticket_sequence
            if len(tickets) != expected:
                raise QuiescenceBlockedError("pre-fence ticket evidence is incomplete")
            terminal_states = {
                FenceOperationState.TERMINAL_SUCCEEDED,
                FenceOperationState.TERMINAL_FAILED,
                FenceOperationState.RECONCILED_ORPHAN,
                FenceOperationState.INDETERMINATE,
                FenceOperationState.CANCELLED_BEFORE_IRREVERSIBLE_BOUNDARY,
            }
            if any(ticket.state not in terminal_states for ticket in tickets):
                raise QuiescenceBlockedError("pre-fence ticket remains executable")
            indeterminate = sum(
                ticket.state is FenceOperationState.INDETERMINATE
                or ticket.state not in terminal_states
                for ticket in tickets
            )
            if indeterminate:
                raise QuiescenceBlockedError("pre-fence operation is indeterminate")
            watermark = await self._evidence_provider.capture_quiescent_watermark(
                deployment_scope_id,
                fence_epoch,
                lease.cutoff_ticket_sequence,
            )
            evidence = QuiescentEvidence.build(
                fence_epoch=fence_epoch,
                cutoff_ticket_sequence=lease.cutoff_ticket_sequence,
                watermark=watermark,
                pre_fence_terminal_ticket_count=len(tickets),
                captured_at=now,
            )
            await transaction.save_lease(
                lease.model_copy(update={"fence_state": FenceState.QUIESCENT})
            )
            await transaction.save_quiescent_evidence(deployment_scope_id, evidence)
            return evidence

    async def begin_snapshot(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
    ) -> BackupFenceLease:
        return await self._advance_fence(
            deployment_scope_id,
            fence_epoch=fence_epoch,
            lease_owner=lease_owner,
            expected={FenceState.QUIESCENT},
            state=FenceState.SNAPSHOTTING,
        )

    async def begin_validation(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
    ) -> BackupFenceLease:
        return await self._advance_fence(
            deployment_scope_id,
            fence_epoch=fence_epoch,
            lease_owner=lease_owner,
            expected={FenceState.SNAPSHOTTING},
            state=FenceState.VALIDATING,
        )

    async def persist_coherent_manifest(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
        postgres_boundary: str,
        object_boundary: str,
        scope: str,
        recovery_set_id: str | None = None,
        postgres_component_evidence: RecoveryComponentEvidence | None = None,
        object_component_evidence: RecoveryComponentEvidence | None = None,
        backup_protection_profile: BackupProtectionProfile | None = None,
    ) -> StorageRecoveryManifest:
        now = self._now()
        async with self._repository.transaction(deployment_scope_id) as transaction:
            lease = await self._require_lease(transaction, deployment_scope_id)
            self._require_owner(lease, fence_epoch=fence_epoch, lease_owner=lease_owner)
            if lease.fence_state is not FenceState.VALIDATING:
                raise RecoveryManifestError("component evidence is not ready for validation")
            quiescent_evidence = await transaction.get_quiescent_evidence(
                deployment_scope_id, fence_epoch
            )
            if quiescent_evidence is None or quiescent_evidence.fence_epoch != fence_epoch:
                raise RecoveryManifestError("durable quiescent evidence is unavailable")
            assert lease.cutoff_ticket_sequence is not None
            if quiescent_evidence.cutoff_ticket_sequence != lease.cutoff_ticket_sequence:
                raise RecoveryManifestError("quiescent cutoff does not match live fence")
            if (postgres_component_evidence is None) != (
                object_component_evidence is None
            ):
                raise RecoveryManifestError("both component receipts are required")
            postgres_evidence = postgres_component_evidence
            object_evidence = object_component_evidence
            if postgres_evidence is None or object_evidence is None:
                postgres_evidence = await self._evidence_provider.verify_component(
                    deployment_scope_id,
                    fence_epoch,
                    "postgres",
                    postgres_boundary,
                    cutoff_ticket_sequence=lease.cutoff_ticket_sequence,
                )
                object_evidence = await self._evidence_provider.verify_component(
                    deployment_scope_id,
                    fence_epoch,
                    "object_volume",
                    object_boundary,
                    cutoff_ticket_sequence=lease.cutoff_ticket_sequence,
                )
            if postgres_evidence.component != "postgres" or object_evidence.component != "object_volume":
                raise RecoveryManifestError("component evidence has the wrong identity")
            if not postgres_evidence.verified or not object_evidence.verified:
                raise RecoveryManifestError("coherent manifest requires verified components")
            tickets = await transaction.list_tickets(
                deployment_scope_id,
                through_sequence=lease.cutoff_ticket_sequence,
            )
            orphaned = sum(
                ticket.state is FenceOperationState.RECONCILED_ORPHAN for ticket in tickets
            )
            indeterminate = sum(
                ticket.state is FenceOperationState.INDETERMINATE for ticket in tickets
            )
            if indeterminate:
                raise RecoveryManifestError("indeterminate operation prevents coherence")
            try:
                manifest = StorageRecoveryManifest.build(
                    recovery_set_id=recovery_set_id or self._recovery_set_id_factory(),
                    deployment_scope_id=deployment_scope_id,
                    profile=RecoveryManifestProfile.COORDINATED,
                    manifest_status=RecoveryManifestStatus.COHERENT,
                    fence_epoch=fence_epoch,
                    cutoff_ticket_sequence=lease.cutoff_ticket_sequence,
                    quiescent_evidence=quiescent_evidence,
                    postgres_component_evidence=postgres_evidence,
                    object_component_evidence=object_evidence,
                    backup_protection_profile=backup_protection_profile,
                    postgres_boundary=postgres_boundary,
                    postgres_recovery_watermark=quiescent_evidence.watermark,
                    object_volume_generation=object_evidence.observed_identity,
                    postgres_component_state=ComponentState.VERIFIED,
                    object_component_state=ComponentState.VERIFIED,
                    postgres_backup_identity=postgres_evidence.observed_identity,
                    postgres_verification_reference=postgres_evidence.verification_reference,
                    object_verification_reference=object_evidence.verification_reference,
                    drained_operation_count=len(tickets) - orphaned,
                    orphaned_operation_count=orphaned,
                    indeterminate_operation_count=indeterminate,
                    scope=scope,
                    created_at=now,
                    completed_at=now,
                )
            except ValidationError as error:
                raise RecoveryManifestError("component recovery evidence is incoherent") from error
            await transaction.add_manifest(manifest)
            await transaction.save_lease(
                lease.model_copy(
                    update={
                        "fence_state": FenceState.RELEASING,
                        "cleanup_release_progress": "manifest_persisted",
                    }
                )
            )
            return manifest

    async def persist_best_effort_manifest(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
        postgres_boundary: str,
        postgres_recovery_watermark: str | None,
        object_volume_generation: str,
        scope: str,
        postgres_component_state: ComponentState,
        object_component_state: ComponentState,
        start_watermark: str,
        end_watermark: str,
        recovery_set_id: str | None = None,
        postgres_component_evidence: RecoveryComponentEvidence | None = None,
        object_component_evidence: RecoveryComponentEvidence | None = None,
        backup_protection_profile: BackupProtectionProfile | None = None,
        failure_category: RecoveryFailureCategory | None = None,
    ) -> StorageRecoveryManifest:
        """Persist an explicit, non-coherent Profile B recovery attempt."""

        now = self._now()
        async with self._repository.transaction(deployment_scope_id) as transaction:
            lease = await self._require_lease(transaction, deployment_scope_id)
            self._require_owner(lease, fence_epoch=fence_epoch, lease_owner=lease_owner)
            assert lease.cutoff_ticket_sequence is not None
            tickets = await transaction.list_tickets(
                deployment_scope_id,
                through_sequence=lease.cutoff_ticket_sequence,
            )
            if len(tickets) != lease.cutoff_ticket_sequence:
                raise RecoveryManifestError("pre-fence ticket evidence is incomplete")
            terminal_states = {
                FenceOperationState.TERMINAL_SUCCEEDED,
                FenceOperationState.TERMINAL_FAILED,
                FenceOperationState.RECONCILED_ORPHAN,
                FenceOperationState.INDETERMINATE,
                FenceOperationState.CANCELLED_BEFORE_IRREVERSIBLE_BOUNDARY,
            }
            quiescent_evidence = await transaction.get_quiescent_evidence(
                deployment_scope_id, fence_epoch
            )
            if quiescent_evidence is not None:
                if quiescent_evidence.cutoff_ticket_sequence != lease.cutoff_ticket_sequence:
                    raise RecoveryManifestError("quiescent cutoff does not match live fence")
                if postgres_recovery_watermark != quiescent_evidence.watermark:
                    raise RecoveryManifestError("watermark is not backed by quiescent evidence")
            else:
                postgres_recovery_watermark = None
            orphaned = sum(
                ticket.state is FenceOperationState.RECONCILED_ORPHAN for ticket in tickets
            )
            indeterminate = sum(
                ticket.state is FenceOperationState.INDETERMINATE
                or ticket.state not in terminal_states
                for ticket in tickets
            )
            manifest = StorageRecoveryManifest.build(
                recovery_set_id=recovery_set_id or self._recovery_set_id_factory(),
                deployment_scope_id=deployment_scope_id,
                profile=RecoveryManifestProfile.BEST_EFFORT,
                manifest_status=RecoveryManifestStatus.INCOMPLETE,
                fence_epoch=fence_epoch,
                cutoff_ticket_sequence=lease.cutoff_ticket_sequence or 0,
                quiescent_evidence=quiescent_evidence,
                postgres_component_evidence=postgres_component_evidence,
                object_component_evidence=object_component_evidence,
                backup_protection_profile=backup_protection_profile,
                postgres_boundary=postgres_boundary,
                postgres_recovery_watermark=postgres_recovery_watermark,
                object_volume_generation=object_volume_generation,
                postgres_component_state=postgres_component_state,
                object_component_state=object_component_state,
                postgres_backup_identity=None,
                drained_operation_count=len(tickets) - orphaned - indeterminate,
                orphaned_operation_count=orphaned,
                indeterminate_operation_count=indeterminate,
                failure_category=failure_category,
                scope=scope,
                start_watermark=start_watermark,
                end_watermark=end_watermark,
                created_at=now,
                completed_at=None,
            )
            await transaction.add_manifest(manifest)
            return manifest

    async def release_fence(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
        degraded: bool = False,
    ) -> BackupFenceLease:
        async with self._repository.transaction(deployment_scope_id) as transaction:
            lease = await self._require_lease(transaction, deployment_scope_id)
            self._require_owner(lease, fence_epoch=fence_epoch, lease_owner=lease_owner)
            if lease.fence_state is not FenceState.RELEASING and not degraded:
                raise FenceOwnershipError("fence is not ready for release")
            if not degraded:
                assert lease.cutoff_ticket_sequence is not None
                tickets = await transaction.list_tickets(
                    deployment_scope_id,
                    through_sequence=lease.cutoff_ticket_sequence,
                )
                terminal_states = {
                    FenceOperationState.TERMINAL_SUCCEEDED,
                    FenceOperationState.TERMINAL_FAILED,
                    FenceOperationState.RECONCILED_ORPHAN,
                    FenceOperationState.INDETERMINATE,
                    FenceOperationState.CANCELLED_BEFORE_IRREVERSIBLE_BOUNDARY,
                }
                if len(tickets) != lease.cutoff_ticket_sequence or any(
                    ticket.state not in terminal_states for ticket in tickets
                ):
                    raise FenceOwnershipError("pre-fence operations are not terminal")
            if degraded:
                updated = lease.model_copy(
                    update={
                        "fence_state": FenceState.RELEASE_DEGRADED,
                        "ticket_admission": TicketAdmission.BLOCKED,
                        "cleanup_release_progress": "release_degraded",
                    }
                )
            else:
                updated = lease.model_copy(
                    update={
                        "fence_state": FenceState.RELEASED,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "lease_renewal": None,
                        "ticket_admission": TicketAdmission.OPEN,
                        "cleanup_release_progress": "released",
                    }
                )
            await transaction.save_lease(updated)
            return updated

    async def fail_attempt(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
        reason: RecoveryFailureCategory,
        release_degraded: bool = False,
    ) -> BackupFenceLease:
        """Record a failed attempt and keep release state separate from evidence."""

        async with self._repository.transaction(deployment_scope_id) as transaction:
            lease = await self._require_lease(transaction, deployment_scope_id)
            self._require_owner(lease, fence_epoch=fence_epoch, lease_owner=lease_owner)
            updated = lease.model_copy(
                update={
                    "fence_state": (
                        FenceState.RELEASE_DEGRADED
                        if release_degraded
                        else FenceState.RELEASING
                    ),
                    "ticket_admission": TicketAdmission.BLOCKED,
                    "cleanup_release_progress": f"failed:{reason.value}",
                }
            )
            await transaction.save_lease(updated)
            return updated

    async def _advance_fence(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
        expected: set[FenceState],
        state: FenceState,
    ) -> BackupFenceLease:
        async with self._repository.transaction(deployment_scope_id) as transaction:
            lease = await self._require_lease(transaction, deployment_scope_id)
            self._require_owner(lease, fence_epoch=fence_epoch, lease_owner=lease_owner)
            if lease.fence_state not in expected:
                raise FenceOwnershipError("fence transition is out of order")
            updated = lease.model_copy(update={"fence_state": state})
            await transaction.save_lease(updated)
            return updated

    async def _require_lease(
        self,
        transaction: RecoveryTransaction,
        deployment_scope_id: str,
    ) -> BackupFenceLease:
        lease = await transaction.get_lease(deployment_scope_id)
        if lease is None:
            lease = BackupFenceLease(deployment_scope_id=deployment_scope_id)
            await transaction.save_lease(lease)
        return lease

    def _require_owner(
        self,
        lease: BackupFenceLease,
        *,
        fence_epoch: int,
        lease_owner: str,
    ) -> None:
        if (
            lease.active_fence_epoch != fence_epoch
            or lease.lease_owner != lease_owner
            or lease.lease_expires_at is None
            or lease.lease_expires_at <= self._now()
        ):
            raise FenceOwnershipError("stale coordinator or fence epoch")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("recovery clock must return timezone-aware datetime")
        return now

    @staticmethod
    def _validate_duration(duration: timedelta) -> None:
        if duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")


__all__ = [
    "BackupRecoveryCoordinator",
    "FenceAdmissionBlockedError",
    "FenceOwnershipError",
    "QuiescenceBlockedError",
    "RecoveryCoordinationError",
    "RecoveryEvidenceProvider",
    "RecoveryManifestError",
    "RecoveryRepository",
    "RecoveryTransaction",
]
