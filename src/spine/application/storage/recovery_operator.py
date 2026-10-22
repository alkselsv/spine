"""One-shot application binding for coordinated backup and isolated restore."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from spine.domain.storage.backup_profile import BackupProtectionProfile
from spine.domain.storage.recovery import StorageRecoveryManifest
from spine.domain.storage.recovery_operator import (
    RecoveryOperatorOperation,
    RecoveryOperatorProfile,
    RecoveryOperatorResult,
    RecoveryRestoreTargetFacts,
    RecoverySourceInventoryFacts,
)

from .backup_profile import CoordinatedBackupProfile


class RecoveryOperatorError(RuntimeError):
    """Disclosure-safe recovery operator error."""


class RecoveryOperatorInvocationRejected(RecoveryOperatorError):
    """The service was not called from the explicit operator command boundary."""


class RecoveryOperatorInvocationSource(str, Enum):
    OPERATOR_COMMAND = "operator_command"
    API_STARTUP = "api_startup"
    WORKER_STARTUP = "worker_startup"
    SCHEDULER = "scheduler"
    API_REQUEST = "api_request"


class RecoveryOperatorApprovalPort(Protocol):
    async def validate(self, profile: RecoveryOperatorProfile) -> bool: ...


class RecoverySourceInventoryPort(Protocol):
    async def resolve(self, reference: str) -> RecoverySourceInventoryFacts: ...


@dataclass(frozen=True, slots=True)
class ResolvedRecoveryRestoreTarget:
    facts: RecoveryRestoreTargetFacts
    target: object


class RecoveryRestoreTargetPort(Protocol):
    async def resolve(self, reference: str) -> ResolvedRecoveryRestoreTarget: ...


@dataclass(frozen=True, slots=True)
class RecoveryArtifactSet:
    """Resolved protected artifacts; paths never cross into operator results."""

    postgres_artifact: Path
    filesystem_artifact: Path


class RecoveryArtifactPort(Protocol):
    async def resolve(self, recovery_set_id: str) -> RecoveryArtifactSet: ...


class RecoveryManifestPort(Protocol):
    async def read(self, reference: str) -> StorageRecoveryManifest: ...


class RecoveryRestoreVerifierPort(Protocol):
    async def verify_and_restore(
        self,
        *,
        manifest: StorageRecoveryManifest,
        postgres_artifact: Path,
        filesystem_artifact: Path,
        target: object,
    ) -> object: ...


class RecoveryEvidencePort(Protocol):
    async def ensure_available(self, profile: RecoveryOperatorProfile) -> None: ...

    async def record(
        self,
        result: RecoveryOperatorResult,
        *,
        reference_kind: Literal["manifest", "restore"],
    ) -> str: ...


class RecoveryOperatorService:
    """Validate deployment bindings, then call only existing recovery contracts."""

    def __init__(
        self,
        *,
        approval: RecoveryOperatorApprovalPort,
        source_inventory: RecoverySourceInventoryPort,
        target_inventory: RecoveryRestoreTargetPort,
        backup_profile: CoordinatedBackupProfile,
        manifest_reader: RecoveryManifestPort,
        artifact_resolver: RecoveryArtifactPort,
        restore_verifier: RecoveryRestoreVerifierPort,
        evidence: RecoveryEvidencePort,
    ) -> None:
        self._approval = approval
        self._source_inventory = source_inventory
        self._target_inventory = target_inventory
        self._backup_profile = backup_profile
        self._manifest_reader = manifest_reader
        self._artifact_resolver = artifact_resolver
        self._restore_verifier = restore_verifier
        self._evidence = evidence

    async def execute(
        self,
        profile: RecoveryOperatorProfile,
        *,
        invocation_source: RecoveryOperatorInvocationSource = (
            RecoveryOperatorInvocationSource.OPERATOR_COMMAND
        ),
    ) -> RecoveryOperatorResult:
        if type(profile) is not RecoveryOperatorProfile:
            raise RecoveryOperatorError("recovery operator profile contract is invalid")
        if invocation_source is not RecoveryOperatorInvocationSource.OPERATOR_COMMAND:
            raise RecoveryOperatorInvocationRejected(
                "recovery operations require an explicit operator command"
            )

        try:
            await self._evidence.ensure_available(profile)
        except Exception:
            return self._failure(profile, "blocked", "evidence.unavailable")

        try:
            approved = await self._approval.validate(profile)
        except Exception:
            approved = False
        if not approved:
            return self._failure(profile, "blocked", "approval.invalid")

        try:
            source = await self._source_inventory.resolve(profile.source_inventory_reference)
        except Exception:
            return self._failure(profile, "blocked", "source.inventory_unavailable")
        if not isinstance(source, RecoverySourceInventoryFacts):
            return self._failure(profile, "blocked", "source.inventory_invalid")
        source_failure = self._validate_source(profile, source)
        if source_failure is not None:
            return self._failure(profile, "blocked", source_failure)

        target: ResolvedRecoveryRestoreTarget | None = None
        if self._includes_restore(profile.operation):
            try:
                assert profile.restore_target_inventory_reference is not None
                target = await self._target_inventory.resolve(
                    profile.restore_target_inventory_reference
                )
            except Exception:
                return self._failure(profile, "blocked", "restore.target_unavailable")
            if not isinstance(target, ResolvedRecoveryRestoreTarget):
                return self._failure(profile, "blocked", "restore.target_invalid")
            target_failure = self._validate_target(profile, target)
            if target_failure is not None:
                return self._failure(profile, "blocked", target_failure)

        manifest: StorageRecoveryManifest | object | None = None
        manifest_reference: str | None = None
        if self._includes_backup(profile.operation):
            backup_binding_failure = self._validate_backup_binding(profile)
            if backup_binding_failure is not None:
                return self._failure(profile, "blocked", backup_binding_failure)
            manifest, backup_failure = await self._run_backup(profile)
            if backup_failure is not None:
                return self._failure(profile, "indeterminate", backup_failure)
            assert manifest is not None
            if not self._is_coherent_manifest(manifest):
                return self._failure(profile, "indeterminate", "manifest.untrusted")
            if not self._manifest_matches_profile(profile, manifest):
                return self._failure(profile, "indeterminate", "manifest.identity_mismatch")
            backup_result = self._manifest_result(profile, manifest)
            manifest_reference = await self._record(
                backup_result, reference_kind="manifest"
            )
            if manifest_reference is None:
                return self._failure(profile, "indeterminate", "evidence.record_failed")
        else:
            assert profile.manifest_reference is not None
            try:
                manifest = await self._manifest_reader.read(profile.manifest_reference)
            except Exception:
                return self._failure(profile, "blocked", "manifest.unavailable")
            if not self._is_coherent_manifest(manifest):
                return self._failure(profile, "blocked", "manifest.untrusted")
            if not self._manifest_matches_profile(profile, manifest):
                return self._failure(profile, "blocked", "manifest.identity_mismatch")
            manifest_reference = profile.manifest_reference

        if not self._includes_restore(profile.operation):
            return self._manifest_result(
                profile,
                manifest,
                manifest_reference=manifest_reference,
                replay=self._manifest_was_replayed(manifest),
            )

        assert target is not None
        assert manifest is not None
        if not self._is_coherent_manifest(manifest):
            return self._failure(profile, "blocked", "manifest.untrusted")
        try:
            artifacts = await self._artifact_resolver.resolve(manifest.recovery_set_id)
            evidence = await self._restore_verifier.verify_and_restore(
                manifest=manifest,
                postgres_artifact=artifacts.postgres_artifact,
                filesystem_artifact=artifacts.filesystem_artifact,
                target=target.target,
            )
        except Exception as error:
            outcome = getattr(getattr(error, "evidence", None), "outcome", None)
            if outcome in {"rejected", "unavailable", "indeterminate"}:
                return self._failure(
                    profile,
                    outcome,
                    "restore.verification_failed",
                    manifest_reference=manifest_reference,
                    manifest_digest=getattr(manifest, "manifest_digest", None),
                )
            return self._failure(
                profile,
                "indeterminate",
                "restore.execution_failed",
                manifest_reference=manifest_reference,
                manifest_digest=getattr(manifest, "manifest_digest", None),
            )

        outcome = getattr(evidence, "outcome", None)
        if outcome != "verified":
            status = outcome if outcome in {"rejected", "indeterminate"} else "blocked"
            return self._failure(
                profile,
                status,
                "restore.verification_failed",
                manifest_reference=manifest_reference,
                manifest_digest=getattr(manifest, "manifest_digest", None),
            )
        restore_result = RecoveryOperatorResult(
            operation=profile.operation,
            status="verified",
            recovery_set_id=manifest.recovery_set_id,
            manifest_reference=manifest_reference,
            manifest_digest=manifest.manifest_digest,
            replay=bool(getattr(target.facts, "target_reference", None))
            and bool(getattr(target, "replay", False)),
        )
        restore_reference = await self._record(restore_result, reference_kind="restore")
        if restore_reference is None:
            return self._failure(
                profile,
                "indeterminate",
                "evidence.record_failed",
                manifest_reference=manifest_reference,
                manifest_digest=manifest.manifest_digest,
            )
        return restore_result.model_copy(update={"restore_evidence_reference": restore_reference})

    async def _run_backup(
        self, profile: RecoveryOperatorProfile
    ) -> tuple[object | None, str | None]:
        try:
            manifest = await self._backup_profile.run(
                deployment_scope_id=profile.deployment_scope_reference,
                lease_owner=profile.coordinator_identity_reference,
                scope=profile.scope_reference,
                recovery_set_id=profile.recovery_set_id,
            )
        except Exception as error:
            category = getattr(getattr(error, "category", None), "value", None)
            return None, f"backup.{category or 'failed'}"
        return manifest, None

    async def _record(
        self,
        result: RecoveryOperatorResult,
        *,
        reference_kind: Literal["manifest", "restore"],
    ) -> str | None:
        try:
            reference = await self._evidence.record(result, reference_kind=reference_kind)
            if not isinstance(reference, str) or not reference:
                return None
            return reference
        except Exception:
            return None

    @staticmethod
    def _validate_source(
        profile: RecoveryOperatorProfile, facts: RecoverySourceInventoryFacts
    ) -> str | None:
        if facts.inventory_reference != profile.source_inventory_reference:
            return "source.inventory_mismatch"
        if facts.deployment_scope_reference != profile.deployment_scope_reference:
            return "source.scope_mismatch"
        if facts.scope_reference != profile.scope_reference:
            return "source.scope_mismatch"
        if facts.source_volume_reference != profile.source_volume_reference:
            return "source.inventory_mismatch"
        return None

    def _validate_backup_binding(self, profile: RecoveryOperatorProfile) -> str | None:
        configured = getattr(self._backup_profile, "protection", None)
        if configured is None:
            return None
        expected = build_backup_protection_profile(profile)
        fields = (
            "source_volume_id",
            "destination_id",
            "encryption_at_rest_profile",
            "protected_transfer_profile",
            "backup_identity",
            "restore_identity",
            "retention_days",
            "cadence",
        )
        if any(getattr(configured, field) != getattr(expected, field) for field in fields):
            return "backup.binding_mismatch"
        return None

    @staticmethod
    def _validate_target(
        profile: RecoveryOperatorProfile, target: ResolvedRecoveryRestoreTarget
    ) -> str | None:
        facts = target.facts
        if facts.inventory_reference != profile.restore_target_inventory_reference:
            return "restore.target_mismatch"
        if facts.serving_identity_inventory_reference != profile.serving_identity_inventory_reference:
            return "restore.serving_inventory_mismatch"
        if facts.target_reference == profile.source_volume_reference:
            return "restore.target_mismatch"
        if facts.restore_identity_reference != profile.restore_identity_reference:
            return "restore.identity_mismatch"
        if facts.verifier_identity_reference != profile.verifier_identity_reference:
            return "restore.identity_mismatch"
        return None

    @staticmethod
    def _is_coherent_manifest(manifest: object) -> bool:
        return getattr(getattr(manifest, "manifest_status", None), "value", None) == "coherent"

    @staticmethod
    def _manifest_matches_profile(profile: RecoveryOperatorProfile, manifest: object) -> bool:
        return getattr(manifest, "recovery_set_id", None) == profile.recovery_set_id

    @staticmethod
    def _manifest_was_replayed(manifest: object) -> bool:
        return getattr(manifest, "manifest_status", None) is not None and bool(
            getattr(manifest, "replayed", False)
        )

    @staticmethod
    def _manifest_result(
        profile: RecoveryOperatorProfile,
        manifest: object,
        *,
        manifest_reference: str | None = None,
        replay: bool = False,
    ) -> RecoveryOperatorResult:
        return RecoveryOperatorResult(
            operation=profile.operation,
            status="verified",
            recovery_set_id=manifest.recovery_set_id,
            manifest_reference=manifest_reference,
            manifest_digest=manifest.manifest_digest,
            replay=replay,
        )

    @staticmethod
    def _includes_backup(operation: RecoveryOperatorOperation) -> bool:
        return operation in {
            RecoveryOperatorOperation.BACKUP,
            RecoveryOperatorOperation.BACKUP_AND_RESTORE,
        }

    @staticmethod
    def _includes_restore(operation: RecoveryOperatorOperation) -> bool:
        return operation in {
            RecoveryOperatorOperation.RESTORE,
            RecoveryOperatorOperation.BACKUP_AND_RESTORE,
        }

    @staticmethod
    def _failure(
        profile: RecoveryOperatorProfile,
        status: Literal["verified", "rejected", "blocked", "indeterminate"],
        code: str,
        *,
        manifest_reference: str | None = None,
        manifest_digest: str | None = None,
    ) -> RecoveryOperatorResult:
        return RecoveryOperatorResult(
            operation=profile.operation,
            status=status,
            recovery_set_id=profile.recovery_set_id,
            manifest_reference=manifest_reference,
            manifest_digest=manifest_digest,
            safe_failure_code=code,
        )


def build_backup_protection_profile(
    profile: RecoveryOperatorProfile,
) -> BackupProtectionProfile:
    """Map approved operator references to the existing backup contract."""

    return BackupProtectionProfile(
        source_volume_id=profile.source_volume_reference,
        destination_id=profile.protected_destination_reference,
        encryption_at_rest_profile=profile.encryption_profile_reference,
        protected_transfer_profile=profile.protected_transfer_profile_reference,
        backup_identity=profile.coordinator_identity_reference,
        restore_identity=profile.restore_identity_reference,
        retention_days=30,
        cadence="daily",
    )


__all__ = [
    "RecoveryArtifactPort",
    "RecoveryArtifactSet",
    "RecoveryEvidencePort",
    "RecoveryManifestPort",
    "RecoveryOperatorApprovalPort",
    "RecoveryOperatorError",
    "RecoveryOperatorInvocationRejected",
    "RecoveryOperatorInvocationSource",
    "RecoveryOperatorService",
    "RecoveryRestoreTargetPort",
    "RecoveryRestoreVerifierPort",
    "RecoverySourceInventoryPort",
    "ResolvedRecoveryRestoreTarget",
    "build_backup_protection_profile",
]
