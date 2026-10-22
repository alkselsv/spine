from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from spine.application.storage.recovery_operator import (
    RecoveryArtifactSet,
    RecoveryOperatorInvocationRejected,
    RecoveryOperatorInvocationSource,
    RecoveryOperatorService,
    ResolvedRecoveryRestoreTarget,
    build_backup_protection_profile,
)
from spine.domain.storage.backup_profile import BackupProtectionProfile
from spine.domain.storage.recovery_operator import (
    RecoveryOperatorOperation,
    RecoveryOperatorProfile,
    RecoveryRestoreTargetFacts,
    RecoverySourceInventoryFacts,
)


def profile_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "operation": RecoveryOperatorOperation.BACKUP,
        "profile_reference": "profile:recovery-v1",
        "deployment_scope_reference": "deployment:production",
        "scope_reference": "scope:workspace-environment",
        "change_reference": "change:2026-532",
        "approval_reference": "approval:storage-owner",
        "recovery_set_id": "recovery-set:one",
        "operator_identity_reference": "identity:operator",
        "coordinator_identity_reference": "identity:coordinator",
        "restore_identity_reference": "identity:restore",
        "verifier_identity_reference": "identity:verifier",
        "source_inventory_reference": "inventory:source-v1",
        "source_volume_reference": "volume:source",
        "protected_destination_reference": "destination:protected",
        "encryption_profile_reference": "encryption:prod",
        "protected_transfer_profile_reference": "transfer:tls",
        "retention_profile_reference": "retention:30d",
        "postgres_tool_profile_reference": "tool:postgres-17",
        "filesystem_semantics_profile_reference": "filesystem:posix-v1",
        "fence_profile_reference": "fence:adr-0022-v1",
        "source_network_profile_reference": "network:source",
        "restore_network_profile_reference": "network:restore",
        "restore_target_inventory_reference": "inventory:restore-v1",
        "serving_identity_inventory_reference": "inventory:serving-v1",
        "evidence_destination_reference": "evidence:protected",
        "evidence_reviewer_reference": "identity:reviewer",
    }
    values.update(overrides)
    return values


def source_facts(**overrides: object) -> RecoverySourceInventoryFacts:
    values: dict[str, object] = {
        "inventory_reference": "inventory:source-v1",
        "deployment_scope_reference": "deployment:production",
        "scope_reference": "scope:workspace-environment",
        "source_volume_reference": "volume:source",
    }
    values.update(overrides)
    return RecoverySourceInventoryFacts.model_validate(values)


def target_facts(**overrides: object) -> RecoveryRestoreTargetFacts:
    values: dict[str, object] = {
        "inventory_reference": "inventory:restore-v1",
        "target_reference": "target:restore",
        "database_target_reference": "database:restore",
        "filesystem_target_reference": "filesystem:restore",
        "serving_identity_inventory_reference": "inventory:serving-v1",
        "restore_identity_reference": "identity:restore",
        "verifier_identity_reference": "identity:verifier",
    }
    values.update(overrides)
    return RecoveryRestoreTargetFacts.model_validate(values)


@dataclass(frozen=True)
class Manifest:
    recovery_set_id: str = "recovery-set:one"
    manifest_digest: str = "a" * 64
    manifest_status: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(value="coherent")
    )


class Approval:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.calls = 0

    async def validate(self, profile: RecoveryOperatorProfile) -> bool:
        self.calls += 1
        return self.approved


class SourceInventory:
    def __init__(self, facts: RecoverySourceInventoryFacts) -> None:
        self.facts = facts

    async def resolve(self, reference: str) -> RecoverySourceInventoryFacts:
        assert reference == self.facts.inventory_reference
        return self.facts


class TargetInventory:
    def __init__(self, resolved: ResolvedRecoveryRestoreTarget) -> None:
        self.resolved = resolved

    async def resolve(self, reference: str) -> ResolvedRecoveryRestoreTarget:
        assert reference == self.resolved.facts.inventory_reference
        return self.resolved


class Recorder:
    def __init__(self) -> None:
        self.records: list[object] = []

    async def ensure_available(self, profile: RecoveryOperatorProfile) -> None:
        del profile

    async def record(self, result: object, *, reference_kind: str) -> str:
        self.records.append(result)
        return "evidence:restore-one" if reference_kind == "restore" else "manifest:one"


class Backup:
    def __init__(self, manifest: Manifest | None = None) -> None:
        self.manifest = manifest or Manifest()
        self.calls: list[dict[str, object]] = []

    async def run(self, **kwargs: object) -> Manifest:
        self.calls.append(kwargs)
        return self.manifest


class MismatchedBackup(Backup):
    protection = BackupProtectionProfile(
        source_volume_id="volume:source",
        destination_id="destination:other",
        encryption_at_rest_profile="encryption:prod",
        protected_transfer_profile="transfer:tls",
        backup_identity="identity:coordinator",
        restore_identity="identity:restore",
    )


class Manifests:
    def __init__(self, manifest: Manifest | None = None) -> None:
        self.manifest = manifest or Manifest()

    async def read(self, reference: str) -> Manifest:
        assert reference == "manifest:one"
        return self.manifest


class Artifacts:
    async def resolve(self, recovery_set_id: str) -> RecoveryArtifactSet:
        assert recovery_set_id == "recovery-set:one"
        return RecoveryArtifactSet(
            postgres_artifact=Path("protected-postgres-artifact"),
            filesystem_artifact=Path("protected-filesystem-artifact"),
        )


class RestoreVerifier:
    def __init__(self, outcome: str = "verified") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def verify_and_restore(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        manifest = kwargs["manifest"]
        return SimpleNamespace(
            outcome=self.outcome,
            manifest_digest=manifest.manifest_digest,
        )


def test_operator_profile_maps_to_existing_backup_protection_contract() -> None:
    profile = RecoveryOperatorProfile.model_validate(profile_values())

    protection = build_backup_protection_profile(profile)

    assert protection.source_volume_id == profile.source_volume_reference
    assert protection.destination_id == profile.protected_destination_reference
    assert protection.backup_identity == profile.coordinator_identity_reference
    assert protection.restore_identity == profile.restore_identity_reference


def service(
    *,
    profile: RecoveryOperatorProfile,
    approval: Approval | None = None,
    source: SourceInventory | None = None,
    target: TargetInventory | None = None,
    backup: Backup | None = None,
    manifests: Manifests | None = None,
    artifacts: Artifacts | None = None,
    verifier: RestoreVerifier | None = None,
    recorder: Recorder | None = None,
) -> tuple[RecoveryOperatorService, dict[str, object]]:
    collaborators: dict[str, object] = {
        "approval": approval or Approval(),
        "source": source or SourceInventory(source_facts()),
        "target": target
        or TargetInventory(
            ResolvedRecoveryRestoreTarget(facts=target_facts(), target=object())
        ),
        "backup": backup or Backup(),
        "manifests": manifests or Manifests(),
        "artifacts": artifacts or Artifacts(),
        "verifier": verifier or RestoreVerifier(),
        "recorder": recorder or Recorder(),
    }
    return (
        RecoveryOperatorService(
            approval=collaborators["approval"],
            source_inventory=collaborators["source"],
            target_inventory=collaborators["target"],
            backup_profile=collaborators["backup"],
            manifest_reader=collaborators["manifests"],
            artifact_resolver=collaborators["artifacts"],
            restore_verifier=collaborators["verifier"],
            evidence=collaborators["recorder"],
        ),
        collaborators,
    )


@pytest.mark.asyncio
async def test_operator_rejects_non_command_before_any_external_call() -> None:
    profile = RecoveryOperatorProfile.model_validate(profile_values())
    operator, collaborators = service(profile=profile)

    with pytest.raises(RecoveryOperatorInvocationRejected, match="explicit operator command"):
        await operator.execute(
            profile,
            invocation_source=RecoveryOperatorInvocationSource.API_STARTUP,
        )

    assert collaborators["approval"].calls == 0
    assert collaborators["backup"].calls == []


@pytest.mark.asyncio
async def test_operator_rejects_source_inventory_mismatch_before_backup() -> None:
    profile = RecoveryOperatorProfile.model_validate(profile_values())
    backup = Backup()
    operator, _ = service(
        profile=profile,
        source=SourceInventory(source_facts(source_volume_reference="volume:other")),
        backup=backup,
    )

    result = await operator.execute(profile)

    assert result.status == "blocked"
    assert result.safe_failure_code == "source.inventory_mismatch"
    assert backup.calls == []


@pytest.mark.asyncio
async def test_operator_runs_coordinated_backup_and_records_only_safe_metadata() -> None:
    profile = RecoveryOperatorProfile.model_validate(profile_values())
    backup = Backup()
    recorder = Recorder()
    operator, _ = service(profile=profile, backup=backup, recorder=recorder)

    result = await operator.execute(profile)

    assert result.status == "verified"
    assert result.manifest_reference == "manifest:one"
    assert backup.calls[0]["lease_owner"] == "identity:coordinator"
    assert "protected-postgres-artifact" not in result.model_dump_json()
    assert len(recorder.records) == 1


@pytest.mark.asyncio
async def test_operator_rejects_mismatched_protection_binding_before_backup() -> None:
    profile = RecoveryOperatorProfile.model_validate(profile_values())
    backup = MismatchedBackup()
    operator, _ = service(profile=profile, backup=backup)

    result = await operator.execute(profile)

    assert result.status == "blocked"
    assert result.safe_failure_code == "backup.binding_mismatch"
    assert backup.calls == []


@pytest.mark.asyncio
async def test_operator_restores_only_after_target_and_manifest_preflight() -> None:
    profile = RecoveryOperatorProfile.model_validate(
        profile_values(
            operation=RecoveryOperatorOperation.RESTORE,
            manifest_reference="manifest:one",
        )
    )
    verifier = RestoreVerifier()
    operator, _ = service(profile=profile, verifier=verifier)

    result = await operator.execute(profile)

    assert result.status == "verified"
    assert result.restore_evidence_reference == "evidence:restore-one"
    assert verifier.calls[0]["target"] is not None


@pytest.mark.asyncio
async def test_operator_does_not_promote_failed_restore() -> None:
    profile = RecoveryOperatorProfile.model_validate(
        profile_values(
            operation=RecoveryOperatorOperation.RESTORE,
            manifest_reference="manifest:one",
        )
    )
    verifier = RestoreVerifier(outcome="rejected")
    operator, _ = service(profile=profile, verifier=verifier)

    result = await operator.execute(profile)

    assert result.status == "rejected"
    assert result.safe_failure_code == "restore.verification_failed"
