from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from spine.application.storage.backup_profile import (
    BackupComponentSource,
    BackupProtectionProfile,
    BackupRunFailure,
    CoordinatedBackupProfile,
)
from spine.application.storage.recovery import BackupRecoveryCoordinator
from spine.domain.storage.recovery import (
    RecoveryComponentEvidence,
    RecoveryFailureCategory,
    RecoveryManifestStatus,
)
from spine.infrastructure.persistence.backup_recovery_in_memory import (
    InMemoryRecoveryRepository,
)


class Clock:
    def __call__(self) -> datetime:
        return datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class EvidenceProvider:
    async def capture_quiescent_watermark(
        self, deployment_scope_id: str, fence_epoch: int, cutoff_ticket_sequence: int
    ) -> str:
        del deployment_scope_id, fence_epoch, cutoff_ticket_sequence
        return "pg:watermark-1"

    async def verify_component(
        self,
        deployment_scope_id: str,
        fence_epoch: int,
        component: str,
        boundary: str,
        *,
        cutoff_ticket_sequence: int,
    ) -> RecoveryComponentEvidence:
        raise AssertionError("the profile supplies receipts from independent verifiers")


class ComponentSource:
    def __init__(
        self,
        component: str,
        *,
        included_set_digest: str = "1" * 64,
        delay_seconds: float = 0,
    ) -> None:
        self.component = component
        self.included_set_digest = included_set_digest
        self.delay_seconds = delay_seconds
        self.calls: list[dict[str, object]] = []

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
    ) -> RecoveryComponentEvidence:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.calls.append(
            {
                "recovery_set_id": recovery_set_id,
                "deployment_scope_id": deployment_scope_id,
                "fence_epoch": fence_epoch,
                "cutoff_ticket_sequence": cutoff_ticket_sequence,
                "inclusion_policy": inclusion_policy,
                "included_set_digest": included_set_digest,
                "destination_id": protection.destination_id,
            }
        )
        identity = "backup:pg-1" if self.component == "postgres" else "volume:1"
        expected_digest = self.included_set_digest
        return RecoveryComponentEvidence(
            component=self.component,
            expected_identity=identity,
            observed_identity=identity,
            artifact_reference=f"artifact:{recovery_set_id}:{self.component}",
            artifact_digest="2" * 64,
            tool_profile=(
                "pg_dump-custom-compatible-restore:v1"
                if self.component == "postgres"
                else "linux-posix-finalized-originals:v1"
            ),
            tool_version="tool-17.0",
            fence_epoch=fence_epoch,
            cutoff_ticket_sequence=cutoff_ticket_sequence,
            included_set_digest=expected_digest,
            inclusion_policy=inclusion_policy,
            durable=True,
            verified=True,
            verification_reference=f"verify:{self.component}-1",
        )


def profile(
    postgres: BackupComponentSource | None = None,
    objects: BackupComponentSource | None = None,
    *,
    lease_renewal_interval: timedelta | None = None,
) -> tuple[CoordinatedBackupProfile, BackupComponentSource, BackupComponentSource]:
    postgres_source = postgres or ComponentSource("postgres")
    object_source = objects or ComponentSource("object_volume")
    coordinator = BackupRecoveryCoordinator(
        repository=InMemoryRecoveryRepository(),
        ticket_id_factory=lambda: "ticket-1",
        recovery_set_id_factory=lambda: "generated-set",
        clock=Clock(),
        evidence_provider=EvidenceProvider(),
    )
    return (
        CoordinatedBackupProfile(
            coordinator=coordinator,
            postgres_source=postgres_source,
            object_source=object_source,
            protection=BackupProtectionProfile(
        source_volume_id="volume:1",
                destination_id="destination:approved-volume",
                encryption_at_rest_profile="encryption:approved",
                protected_transfer_profile="transfer:approved",
                backup_identity="identity:backup",
                restore_identity="identity:restore",
            ),
            lease_renewal_interval=lease_renewal_interval,
        ),
        postgres_source,
        object_source,
    )


@pytest.mark.asyncio
async def test_coordinated_profile_records_matching_verified_components() -> None:
    backup, postgres, objects = profile()

    manifest = await backup.run(
        deployment_scope_id="deployment-1",
        lease_owner="backup-worker-1",
        scope="deployment-1",
        recovery_set_id="recovery-1",
    )

    assert manifest.manifest_status is RecoveryManifestStatus.COHERENT
    assert manifest.schema_version == 2
    assert manifest.fence_epoch == manifest.postgres_component_evidence.fence_epoch
    assert manifest.postgres_component_evidence.included_set_digest == (
        manifest.object_component_evidence.included_set_digest
    )
    assert postgres.calls[0]["included_set_digest"] is None
    assert objects.calls[0]["included_set_digest"] == "1" * 64
    assert postgres.calls[0]["destination_id"] == "destination:approved-volume"


@pytest.mark.asyncio
async def test_coordinated_profile_replays_same_immutable_recovery_set() -> None:
    backup, postgres, objects = profile()
    first = await backup.run(
        deployment_scope_id="deployment-1",
        lease_owner="backup-worker-1",
        scope="deployment-1",
        recovery_set_id="recovery-1",
    )

    replay = await backup.run(
        deployment_scope_id="deployment-1",
        lease_owner="backup-worker-1",
        scope="deployment-1",
        recovery_set_id="recovery-1",
    )

    assert replay == first
    assert len(postgres.calls) == len(objects.calls) == 1


@pytest.mark.asyncio
async def test_coordinated_profile_persists_mixed_component_failure_as_incomplete() -> None:
    backup, _, _ = profile(objects=ComponentSource("object_volume", included_set_digest="3" * 64))

    with pytest.raises(BackupRunFailure) as failure:
        await backup.run(
            deployment_scope_id="deployment-1",
            lease_owner="backup-worker-1",
            scope="deployment-1",
            recovery_set_id="recovery-mixed",
        )

    assert failure.value.category is RecoveryFailureCategory.COMPONENT_FAILURE


@pytest.mark.asyncio
async def test_coordinated_profile_renews_lease_during_component_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup, _, _ = profile(
        postgres=ComponentSource("postgres", delay_seconds=0.02),
        lease_renewal_interval=timedelta(milliseconds=2),
    )
    renewals: list[int] = []
    original = backup._coordinator.renew_fence

    async def record_renewal(deployment_scope_id: str, **kwargs: object):
        renewals.append(int(kwargs["fence_epoch"]))
        return await original(deployment_scope_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup._coordinator, "renew_fence", record_renewal)

    manifest = await backup.run(
        deployment_scope_id="deployment-1",
        lease_owner="backup-worker-1",
        scope="deployment-1",
        recovery_set_id="recovery-renewed",
    )

    assert manifest.manifest_status is RecoveryManifestStatus.COHERENT
    assert renewals


@pytest.mark.asyncio
async def test_coordinated_profile_rejects_lost_lease_before_coherent_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup, _, _ = profile(
        postgres=ComponentSource("postgres", delay_seconds=0.02),
        lease_renewal_interval=timedelta(milliseconds=2),
    )

    async def lose_lease(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("lease owner changed")

    monkeypatch.setattr(backup._coordinator, "renew_fence", lose_lease)

    with pytest.raises(BackupRunFailure) as failure:
        await backup.run(
            deployment_scope_id="deployment-1",
            lease_owner="backup-worker-1",
            scope="deployment-1",
            recovery_set_id="recovery-lease-lost",
        )

    assert failure.value.category is RecoveryFailureCategory.STALE_FENCE_EPOCH


@pytest.mark.parametrize(
    "field,value",
    [
        ("retention_days", 29),
        ("cadence", "hourly"),
        ("encryption_at_rest_profile", ""),
    ],
)
def test_backup_protection_profile_rejects_unapproved_configuration(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "source_volume_id": "volume:1",
        "destination_id": "destination:approved-volume",
        "encryption_at_rest_profile": "encryption:approved",
        "protected_transfer_profile": "transfer:approved",
        "backup_identity": "identity:backup",
        "restore_identity": "identity:restore",
    }
    values[field] = value

    with pytest.raises(ValueError):
        BackupProtectionProfile.model_validate(values)
