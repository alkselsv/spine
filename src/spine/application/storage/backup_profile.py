"""Execution contract for the approved R1 coordinated backup profile."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Protocol

from spine.domain.storage.recovery import (
    ComponentState,
    RecoveryComponentEvidence,
    RecoveryFailureCategory,
    StorageRecoveryManifest,
)
from spine.domain.storage.backup_profile import (
    BackupProtectionProfile,
    R1_BACKUP_INCLUSION_POLICY,
)
from spine.domain.sources.original_reference import ObjectReference

from .recovery import BackupRecoveryCoordinator


class BackupComponentSource(Protocol):
    """Create and independently verify one durable component artifact."""

    async def create_and_verify(
        self,
        *,
        recovery_set_id: str,
        deployment_scope_id: str,
        fence_epoch: int,
        cutoff_ticket_sequence: int,
        inclusion_policy: str,
        included_set_digest: str | None,
        protection: BackupProtectionProfile,
    ) -> RecoveryComponentEvidence: ...


class CanonicalOriginalReferenceInventory(Protocol):
    """Read canonical finalized original references using the backup identity."""

    async def finalized_references(self) -> tuple[ObjectReference, ...]: ...


class BackupRunFailure(RuntimeError):
    """Sanitized failure returned after recording a non-coherent attempt."""

    def __init__(self, recovery_set_id: str, category: RecoveryFailureCategory) -> None:
        super().__init__("coordinated backup did not produce a coherent recovery set")
        self.recovery_set_id = recovery_set_id
        self.category = category


class BackupComponentFailure(RuntimeError):
    """Sanitized adapter failure with a closed recovery error category."""

    def __init__(self, category: RecoveryFailureCategory) -> None:
        super().__init__("backup component operation failed")
        self.category = category


class CoordinatedBackupProfile:
    """Run PostgreSQL and original-storage artifacts under one ADR 0022 fence."""

    def __init__(
        self,
        *,
        coordinator: BackupRecoveryCoordinator,
        postgres_source: BackupComponentSource,
        object_source: BackupComponentSource,
        protection: BackupProtectionProfile,
        lease_duration: timedelta = timedelta(minutes=15),
        lease_renewal_interval: timedelta | None = None,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        renewal_interval = lease_renewal_interval or lease_duration / 3
        if renewal_interval <= timedelta(0) or renewal_interval >= lease_duration:
            raise ValueError("lease renewal interval must be positive and shorter than the lease")
        self._coordinator = coordinator
        self._postgres_source = postgres_source
        self._object_source = object_source
        self._protection = protection
        self._lease_duration = lease_duration
        self._lease_renewal_interval = renewal_interval

    async def run(
        self,
        *,
        deployment_scope_id: str,
        lease_owner: str,
        scope: str,
        recovery_set_id: str,
    ) -> StorageRecoveryManifest:
        existing = await self._coordinator.manifest(
            deployment_scope_id, recovery_set_id
        )
        if existing is not None:
            if (
                existing.manifest_status.value == "coherent"
                and existing.profile.value == "coordinated"
                and existing.scope == scope
            ):
                return existing
            raise BackupRunFailure(
                recovery_set_id, RecoveryFailureCategory.INDETERMINATE_OPERATION
            )

        postgres_evidence: RecoveryComponentEvidence | None = None
        object_evidence: RecoveryComponentEvidence | None = None
        lease = await self._coordinator.activate_fence(
            deployment_scope_id,
            lease_owner=lease_owner,
            lease_duration=self._lease_duration,
        )
        fence_epoch = lease.active_fence_epoch
        assert fence_epoch is not None
        cutoff = lease.cutoff_ticket_sequence
        assert cutoff is not None
        watermark: str | None = None
        lease_lost = asyncio.Event()
        heartbeat: asyncio.Task[None] | None = asyncio.create_task(
            self._renew_lease(
                deployment_scope_id,
                fence_epoch=fence_epoch,
                lease_owner=lease_owner,
                lease_lost=lease_lost,
            )
        )
        try:
            quiescent = await self._coordinator.mark_quiescent(
                deployment_scope_id,
                fence_epoch=fence_epoch,
                lease_owner=lease_owner,
            )
            watermark = quiescent.watermark
            await self._coordinator.begin_snapshot(
                deployment_scope_id,
                fence_epoch=fence_epoch,
                lease_owner=lease_owner,
            )
            postgres_evidence = await self._postgres_source.create_and_verify(
                recovery_set_id=recovery_set_id,
                deployment_scope_id=deployment_scope_id,
                fence_epoch=fence_epoch,
                cutoff_ticket_sequence=cutoff,
                inclusion_policy=R1_BACKUP_INCLUSION_POLICY,
                included_set_digest=None,
                protection=self._protection,
            )
            self._ensure_lease_healthy(lease_lost)
            object_evidence = await self._object_source.create_and_verify(
                recovery_set_id=recovery_set_id,
                deployment_scope_id=deployment_scope_id,
                fence_epoch=fence_epoch,
                cutoff_ticket_sequence=cutoff,
                inclusion_policy=R1_BACKUP_INCLUSION_POLICY,
                included_set_digest=postgres_evidence.included_set_digest,
                protection=self._protection,
            )
            self._ensure_lease_healthy(lease_lost)
            await self._coordinator.begin_validation(
                deployment_scope_id,
                fence_epoch=fence_epoch,
                lease_owner=lease_owner,
            )
            await self._stop_heartbeat(heartbeat)
            heartbeat = None
            self._ensure_lease_healthy(lease_lost)
            await self._coordinator.renew_fence(
                deployment_scope_id,
                fence_epoch=fence_epoch,
                lease_owner=lease_owner,
                lease_duration=self._lease_duration,
            )
            manifest = await self._coordinator.persist_coherent_manifest(
                deployment_scope_id,
                fence_epoch=fence_epoch,
                lease_owner=lease_owner,
                postgres_boundary=postgres_evidence.artifact_reference,
                object_boundary=object_evidence.artifact_reference,
                scope=scope,
                recovery_set_id=recovery_set_id,
                postgres_component_evidence=postgres_evidence,
                object_component_evidence=object_evidence,
                backup_protection_profile=self._protection,
            )
            await self._coordinator.release_fence(
                deployment_scope_id,
                fence_epoch=fence_epoch,
                lease_owner=lease_owner,
            )
            return manifest
        except Exception as error:
            category = _safe_failure_category(error)
            try:
                await self._coordinator.persist_best_effort_manifest(
                    deployment_scope_id,
                    fence_epoch=fence_epoch,
                    lease_owner=lease_owner,
                    postgres_boundary=f"backup:{recovery_set_id}:postgres",
                    postgres_recovery_watermark=watermark,
                    object_volume_generation=f"backup:{recovery_set_id}:objects",
                    scope=scope,
                    postgres_component_state=(
                        ComponentState.FAILED
                        if postgres_evidence is None or not postgres_evidence.verified
                        else ComponentState.VERIFIED
                    ),
                    object_component_state=(
                        ComponentState.FAILED
                        if object_evidence is None or not object_evidence.verified
                        else ComponentState.VERIFIED
                    ),
                    start_watermark=watermark or "unavailable",
                    end_watermark=watermark or "unavailable",
                    recovery_set_id=recovery_set_id,
                    postgres_component_evidence=postgres_evidence,
                    object_component_evidence=object_evidence,
                    backup_protection_profile=self._protection,
                    failure_category=category,
                )
            except Exception:
                category = RecoveryFailureCategory.INDETERMINATE_OPERATION
            try:
                await self._coordinator.release_fence(
                    deployment_scope_id,
                    fence_epoch=fence_epoch,
                    lease_owner=lease_owner,
                    degraded=True,
                )
            except Exception:
                category = RecoveryFailureCategory.INDETERMINATE_OPERATION
            if isinstance(error, BackupRunFailure):
                raise error from None
            raise BackupRunFailure(recovery_set_id, category) from None
        finally:
            if heartbeat is not None:
                await self._stop_heartbeat(heartbeat)

    async def _renew_lease(
        self,
        deployment_scope_id: str,
        *,
        fence_epoch: int,
        lease_owner: str,
        lease_lost: asyncio.Event,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._lease_renewal_interval.total_seconds())
                await self._coordinator.renew_fence(
                    deployment_scope_id,
                    fence_epoch=fence_epoch,
                    lease_owner=lease_owner,
                    lease_duration=self._lease_duration,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            lease_lost.set()

    @staticmethod
    def _ensure_lease_healthy(lease_lost: asyncio.Event) -> None:
        if lease_lost.is_set():
            raise BackupComponentFailure(RecoveryFailureCategory.STALE_FENCE_EPOCH)

    @staticmethod
    async def _stop_heartbeat(heartbeat: asyncio.Task[None]) -> None:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


def _safe_failure_category(error: Exception) -> RecoveryFailureCategory:
    if isinstance(error, BackupRunFailure):
        return error.category
    if isinstance(error, BackupComponentFailure):
        return error.category
    if isinstance(error, TimeoutError):
        return RecoveryFailureCategory.TIMEOUT
    return RecoveryFailureCategory.COMPONENT_FAILURE
