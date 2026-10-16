"""PostgreSQL adapter for the backup recovery coordination port."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from spine.application.storage.recovery import (
    RecoveryCoordinationError,
    RecoveryTransaction,
)
from spine.domain.storage.recovery import (
    BackupFenceLease,
    FenceOperationTicket,
    QuiescentEvidence,
    ComponentState,
    RecoveryComponentEvidence,
    RecoveryFailureCategory,
    RecoveryEvidenceEventKind,
    RecoveryEvidenceInvalidation,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
)
from spine.domain.storage.backup_profile import BackupProtectionProfile

from .postgresql_mappings import (
    backup_fence_leases,
    fence_operation_tickets,
    recovery_quiescence_evidence,
    recovery_evidence_invalidations,
    storage_recovery_manifests,
)


class _PostgreSQLRecoveryTransaction:
    def __init__(
        self, session: AsyncSession, clock: Callable[[], datetime]
    ) -> None:
        self._session = session
        self._clock = clock

    async def get_lease(self, deployment_scope_id: str) -> BackupFenceLease | None:
        result = await self._session.execute(
            select(backup_fence_leases).where(
                backup_fence_leases.c.deployment_scope_id == deployment_scope_id
            )
        )
        row = result.mappings().one_or_none()
        return _lease_from_row(row) if row is not None else None

    async def save_lease(self, lease: BackupFenceLease) -> None:
        result = await self._session.execute(
            update(backup_fence_leases)
            .where(
                backup_fence_leases.c.deployment_scope_id
                == lease.deployment_scope_id
            )
            .values(**_lease_values(lease, updated_at=self._clock()))
        )
        if result.rowcount != 1:
            raise RecoveryCoordinationError("recovery lease does not exist")

    async def next_ticket_sequence(self, deployment_scope_id: str) -> int:
        result = await self._session.execute(
            select(fence_operation_tickets.c.ticket_sequence)
            .where(
                fence_operation_tickets.c.deployment_scope_id
                == deployment_scope_id
            )
            .order_by(fence_operation_tickets.c.ticket_sequence.desc())
            .limit(1)
        )
        value = result.scalar_one_or_none()
        return (int(value) if value is not None else 0) + 1

    async def add_ticket(self, ticket: FenceOperationTicket) -> None:
        await self._session.execute(
            postgresql_insert(fence_operation_tickets).values(
                **_ticket_values(ticket)
            )
        )

    async def get_ticket(self, ticket_id: str) -> FenceOperationTicket | None:
        result = await self._session.execute(
            select(fence_operation_tickets).where(
                fence_operation_tickets.c.ticket_id == ticket_id
            )
        )
        row = result.mappings().one_or_none()
        return _ticket_from_row(row) if row is not None else None

    async def replace_ticket(self, ticket: FenceOperationTicket) -> None:
        result = await self._session.execute(
            update(fence_operation_tickets)
            .where(fence_operation_tickets.c.ticket_id == ticket.ticket_id)
            .values(**_ticket_values(ticket))
        )
        if result.rowcount != 1:
            raise RecoveryCoordinationError("ticket does not exist")

    async def list_tickets(
        self, deployment_scope_id: str, *, through_sequence: int | None = None
    ) -> tuple[FenceOperationTicket, ...]:
        query = select(fence_operation_tickets).where(
            fence_operation_tickets.c.deployment_scope_id == deployment_scope_id
        )
        if through_sequence is not None:
            query = query.where(
                fence_operation_tickets.c.ticket_sequence <= through_sequence
            )
        result = await self._session.execute(
            query.order_by(fence_operation_tickets.c.ticket_sequence)
        )
        return tuple(_ticket_from_row(row) for row in result.mappings())

    async def get_quiescent_evidence(
        self, deployment_scope_id: str, fence_epoch: int
    ) -> QuiescentEvidence | None:
        result = await self._session.execute(
            select(recovery_quiescence_evidence).where(
                recovery_quiescence_evidence.c.deployment_scope_id
                == deployment_scope_id,
                recovery_quiescence_evidence.c.fence_epoch == fence_epoch,
            )
        )
        row = result.mappings().one_or_none()
        return _quiescent_evidence_from_row(row) if row is not None else None

    async def save_quiescent_evidence(
        self, deployment_scope_id: str, evidence: QuiescentEvidence
    ) -> None:
        await self._session.execute(
            postgresql_insert(recovery_quiescence_evidence)
            .values(**_quiescent_evidence_values(deployment_scope_id, evidence))
            .on_conflict_do_nothing(
                index_elements=[
                    recovery_quiescent_evidence.c.deployment_scope_id,
                    recovery_quiescent_evidence.c.fence_epoch,
                ]
            )
        )

    async def add_manifest(self, manifest: StorageRecoveryManifest) -> None:
        await self._session.execute(
            postgresql_insert(storage_recovery_manifests).values(
                **_manifest_values(manifest)
            )
        )

    async def get_manifest(self, recovery_set_id: str) -> StorageRecoveryManifest | None:
        result = await self._session.execute(
            select(storage_recovery_manifests).where(
                storage_recovery_manifests.c.recovery_set_id == recovery_set_id
            )
        )
        row = result.mappings().one_or_none()
        return _manifest_from_row(row) if row is not None else None

    async def list_manifests(
        self, deployment_scope_id: str
    ) -> tuple[StorageRecoveryManifest, ...]:
        result = await self._session.execute(
            select(storage_recovery_manifests)
            .where(
                storage_recovery_manifests.c.deployment_scope_id
                == deployment_scope_id
            )
            .order_by(
                storage_recovery_manifests.c.created_at,
                storage_recovery_manifests.c.recovery_set_id,
            )
        )
        return tuple(_manifest_from_row(row) for row in result.mappings())

    async def list_invalidations(
        self, deployment_scope_id: str
    ) -> tuple[RecoveryEvidenceInvalidation, ...]:
        result = await self._session.execute(
            select(recovery_evidence_invalidations)
            .where(
                recovery_evidence_invalidations.c.deployment_scope_id
                == deployment_scope_id
            )
            .order_by(recovery_evidence_invalidations.c.sequence)
        )
        return tuple(
            _invalidation_from_row(row) for row in result.mappings()
        )

    async def add_invalidation(self, event: RecoveryEvidenceInvalidation) -> None:
        await self._session.execute(
            postgresql_insert(recovery_evidence_invalidations).values(
                **_invalidation_values(event)
            )
        )

class PostgreSQLBackupRecoveryRepository:
    """Serialize each scope by locking its single live lease row."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    @asynccontextmanager
    async def transaction(self, deployment_scope_id: str) -> AsyncIterator[RecoveryTransaction]:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    select(
                        func.set_config(
                            "spine.deployment_scope_id", deployment_scope_id, True
                        )
                    )
                )
                await session.execute(
                    postgresql_insert(backup_fence_leases)
                    .values(**_initial_lease_values(deployment_scope_id, self._clock()))
                    .on_conflict_do_nothing(
                        index_elements=[backup_fence_leases.c.deployment_scope_id]
                    )
                )
                await session.execute(
                    select(backup_fence_leases)
                    .where(
                        backup_fence_leases.c.deployment_scope_id
                        == deployment_scope_id
                    )
                    .with_for_update()
                )
                yield _PostgreSQLRecoveryTransaction(session, self._clock)


def _initial_lease_values(scope: str, now: datetime) -> dict[str, object]:
    return {
        "deployment_scope_id": scope,
        "fence_state": "inactive",
        "ticket_admission": "open",
        "cleanup_release_progress": "none",
        "created_at": now,
        "updated_at": now,
    }


def _lease_values(lease: BackupFenceLease, *, updated_at: datetime) -> dict[str, object]:
    values = lease.model_dump(mode="python")
    values.pop("schema_version")
    values["fence_state"] = lease.fence_state.value
    values["ticket_admission"] = lease.ticket_admission.value
    values["updated_at"] = updated_at
    return values


def _ticket_values(ticket: FenceOperationTicket) -> dict[str, object]:
    values = ticket.model_dump(mode="python")
    values["schema_version"] = values.pop("schema_version")
    values["operation_kind"] = ticket.operation_kind.value
    values["state"] = ticket.state.value
    values["safe_failure_category"] = (
        ticket.safe_failure_category.value
        if ticket.safe_failure_category is not None
        else None
    )
    return values


def _manifest_values(manifest: StorageRecoveryManifest) -> dict[str, object]:
    values = manifest.model_dump(mode="json")
    values["profile"] = manifest.profile.value
    values["manifest_status"] = manifest.manifest_status.value
    values["postgres_component_state"] = manifest.postgres_component_state.value
    values["object_component_state"] = manifest.object_component_state.value
    return values


def _quiescent_evidence_values(
    deployment_scope_id: str, evidence: QuiescentEvidence
) -> dict[str, object]:
    values = evidence.model_dump(mode="json")
    values["deployment_scope_id"] = deployment_scope_id
    return values


def _invalidation_values(event: RecoveryEvidenceInvalidation) -> dict[str, object]:
    values = event.model_dump(mode="python")
    values["event_kind"] = event.event_kind.value
    return values


def _lease_from_row(row: object) -> BackupFenceLease:
    return BackupFenceLease.model_validate(
        {
            key: row[key]
            for key in (
                "deployment_scope_id",
                "active_fence_epoch",
                "fence_state",
                "cutoff_ticket_sequence",
                "lease_owner",
                "lease_expires_at",
                "lease_renewal",
                "takeover_reference",
                "ticket_admission",
                "cleanup_release_progress",
            )
        }
    )


def _ticket_from_row(row: object) -> FenceOperationTicket:
    return FenceOperationTicket.model_validate(
        {
            key: row[key]
            for key in (
                "ticket_id",
                "deployment_scope_id",
                "ticket_sequence",
                "fence_epoch",
                "operation_kind",
                "target_reference",
                "admitted_at",
                "state",
                "terminal_at",
                "safe_failure_category",
                "correlation_id",
                "schema_version",
            )
        }
    )


def _manifest_from_row(row: object) -> StorageRecoveryManifest:
    values = {
        key: row[key]
        for key in (
            "recovery_set_id",
            "deployment_scope_id",
            "schema_version",
            "profile",
            "manifest_status",
            "backup_profile_version",
            "staged_state_policy",
            "backup_protection_profile",
            "failure_category",
            "fence_epoch",
            "cutoff_ticket_sequence",
            "quiescent_evidence",
            "postgres_component_evidence",
            "object_component_evidence",
            "postgres_boundary",
            "postgres_recovery_watermark",
            "object_volume_generation",
            "postgres_component_state",
            "object_component_state",
            "postgres_backup_identity",
            "postgres_verification_reference",
            "object_verification_reference",
            "drained_operation_count",
            "orphaned_operation_count",
            "indeterminate_operation_count",
            "integrity_algorithm",
            "manifest_digest",
            "scope",
            "start_watermark",
            "end_watermark",
            "created_at",
            "completed_at",
        )
    }
    if values["quiescent_evidence"] is not None:
        values["quiescent_evidence"] = QuiescentEvidence.model_validate(
            values["quiescent_evidence"], strict=False
        )
    if values["schema_version"] == 1:
        values["backup_profile_version"] = "r1-coordinated-backup:v1"
        values["staged_state_policy"] = "exclude-staged-restart-or-reconcile-before-use:v1"
    elif values["backup_protection_profile"] is not None:
        values["backup_protection_profile"] = BackupProtectionProfile.model_validate(
            values["backup_protection_profile"], strict=False
        )
    for name in ("postgres_component_evidence", "object_component_evidence"):
        if values[name] is not None:
            values[name] = RecoveryComponentEvidence.model_validate(
                values[name], strict=False
            )
    values["profile"] = RecoveryManifestProfile(values["profile"])
    values["manifest_status"] = RecoveryManifestStatus(values["manifest_status"])
    if values["failure_category"] is not None:
        values["failure_category"] = RecoveryFailureCategory(
            values["failure_category"]
        )
    values["postgres_component_state"] = ComponentState(
        values["postgres_component_state"]
    )
    values["object_component_state"] = ComponentState(
        values["object_component_state"]
    )
    return StorageRecoveryManifest.model_validate(values)


def _quiescent_evidence_from_row(row: object) -> QuiescentEvidence:
    return QuiescentEvidence.model_validate(
        {
            key: row[key]
            for key in (
                "schema_version",
                "fence_epoch",
                "cutoff_ticket_sequence",
                "watermark",
                "pre_fence_terminal_ticket_count",
                "indeterminate_ticket_count",
                "captured_at",
                "verification_digest",
            )
        }
    )


def _invalidation_from_row(row: object) -> RecoveryEvidenceInvalidation:
    values = {
        key: row[key]
        for key in (
            "deployment_scope_id",
            "sequence",
            "schema_version",
            "recovery_set_id",
            "manifest_digest",
            "event_kind",
            "reason_code",
            "replacement_recovery_set_id",
            "replacement_manifest_digest",
            "replacement_validation_reference",
            "replacement_approval_reference",
            "previous_event_digest",
            "event_digest",
            "occurred_at",
        )
    }
    values["event_kind"] = RecoveryEvidenceEventKind(values["event_kind"])
    return RecoveryEvidenceInvalidation.model_validate(
        values
    )


__all__ = ["PostgreSQLBackupRecoveryRepository"]
