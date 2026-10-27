"""Deployment-owned composition for the approved Issue #509 recovery exercise.

This is deliberately a one-shot job.  The public operator profile contains only
opaque references; concrete paths, endpoints and credentials are supplied by
the disposable deployment and are never serialized into its result.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.application.storage.backup_profile import CoordinatedBackupProfile
from spine.application.storage.recovery import BackupRecoveryCoordinator
from spine.application.storage.recovery_operator import (
    RecoveryArtifactSet,
    RecoveryOperatorService,
    ResolvedRecoveryRestoreTarget,
    build_backup_protection_profile,
)
from spine.domain.storage.recovery_operator import (
    RecoveryOperatorProfile,
    RecoveryOperatorResult,
    RecoveryRestoreTargetFacts,
    RecoverySourceInventoryFacts,
)
from spine.infrastructure.backup_artifacts import FilesystemBackupArtifactStore
from spine.infrastructure.isolated_restore import IsolatedRestoreTarget, IsolatedRestoreVerifier
from spine.infrastructure.object_storage.filesystem import (
    FilesystemOriginalObjectStore,
    FilesystemStorageProfile,
)
from spine.infrastructure.original_storage_backup import FilesystemOriginalStorageBackupSource
from spine.infrastructure.persistence.backup_recovery import PostgreSQLBackupRecoveryRepository
from spine.infrastructure.postgres_backup import (
    PostgreSQLBackupConnection,
    PostgreSQLCustomFormatBackupSource,
    PsycopgCanonicalOriginalInventory,
    TenantInventoryScope,
)
from spine.scripts.recovery_operator import load_profile, run_operator_command


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError("qualification binding is incomplete")
    return value


def _absolute_path(name: str, *, must_exist: bool = False) -> Path:
    path = Path(_required(name))
    if not path.is_absolute() or path.is_symlink() or (must_exist and not path.exists()):
        raise RuntimeError("qualification path binding is invalid")
    return path


class _UnusedGrantRegistry:
    """The backup export path never requests application read grants."""


class _Approval:
    def __init__(self, approval_reference: str, expires_at: datetime) -> None:
        self._reference = approval_reference
        self._expires_at = expires_at

    async def validate(self, profile: RecoveryOperatorProfile) -> bool:
        return (
            profile.approval_reference == self._reference
            and datetime.now(timezone.utc) < self._expires_at
        )


class _SourceInventory:
    def __init__(self, facts: RecoverySourceInventoryFacts) -> None:
        self._facts = facts

    async def resolve(self, reference: str) -> RecoverySourceInventoryFacts:
        if reference != self._facts.inventory_reference:
            raise RuntimeError("source inventory is unavailable")
        return self._facts


class _TargetInventory:
    def __init__(self, facts: RecoveryRestoreTargetFacts, target: IsolatedRestoreTarget) -> None:
        self._facts = facts
        self._target = target

    async def resolve(self, reference: str) -> ResolvedRecoveryRestoreTarget:
        if reference != self._facts.inventory_reference:
            raise RuntimeError("restore inventory is unavailable")
        self._target.validate_target()
        return ResolvedRecoveryRestoreTarget(facts=self._facts, target=self._target)


class _PostgreSQLWatermark:
    def __init__(self, connection: PostgreSQLBackupConnection) -> None:
        self._connection = connection

    async def capture_quiescent_watermark(
        self, deployment_scope_id: str, fence_epoch: int, cutoff_ticket_sequence: int
    ) -> str:
        del deployment_scope_id, fence_epoch, cutoff_ticket_sequence
        async with await AsyncConnection.connect(**self._connection.connection_kwargs()) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT pg_catalog.pg_current_wal_lsn()::text")
                row = await cursor.fetchone()
        if row is None or not isinstance(row[0], str):
            raise RuntimeError("watermark is unavailable")
        return f"pg:{row[0]}"

    async def verify_component(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("component receipts must come from physical adapters")


class _ManifestReader:
    def __init__(self, coordinator: BackupRecoveryCoordinator, deployment_scope: str) -> None:
        self._coordinator = coordinator
        self._scope = deployment_scope

    async def read(self, reference: str) -> object:
        recovery_set_id = reference.removeprefix("manifest:")
        manifest = await self._coordinator.manifest(self._scope, recovery_set_id)
        if manifest is None:
            raise RuntimeError("manifest is unavailable")
        return manifest


class _ArtifactResolver:
    def __init__(self, coordinator: BackupRecoveryCoordinator, store: FilesystemBackupArtifactStore, deployment_scope: str) -> None:
        self._coordinator = coordinator
        self._store = store
        self._scope = deployment_scope

    async def resolve(self, recovery_set_id: str) -> RecoveryArtifactSet:
        manifest = await self._coordinator.manifest(self._scope, recovery_set_id)
        if manifest is None or manifest.postgres_component_evidence is None or manifest.object_component_evidence is None:
            raise RuntimeError("recovery artifacts are unavailable")
        postgres = await self._store.verify_file(
            manifest.postgres_component_evidence.artifact_reference,
            expected_sha256=manifest.postgres_component_evidence.artifact_digest,
        )
        filesystem = await self._store.verify_file(
            manifest.object_component_evidence.artifact_reference,
            expected_sha256=manifest.object_component_evidence.artifact_digest,
        )
        return RecoveryArtifactSet(postgres.local_path, filesystem.local_path)


class _Evidence:
    def __init__(self, root: Path) -> None:
        self._root = root

    async def ensure_available(self, profile: RecoveryOperatorProfile) -> None:
        del profile
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._root.is_dir() or self._root.is_symlink():
            raise RuntimeError("evidence destination is unavailable")

    async def record(
        self,
        result: RecoveryOperatorResult,
        *,
        reference_kind: Literal["manifest", "restore"],
    ) -> str:
        payload = result.model_dump_json().encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._root / f"{reference_kind}-{digest}.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise RuntimeError("immutable evidence conflicts") from None
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        return f"{reference_kind}:{digest}"


def _connection(prefix: str) -> PostgreSQLBackupConnection:
    return PostgreSQLBackupConnection(
        database=_required(f"{prefix}_DATABASE"),
        username=_required(f"{prefix}_USERNAME"),
        backup_role=_required(f"{prefix}_ROLE"),
        host=_required(f"{prefix}_HOST"),
        port=int(_required(f"{prefix}_PORT")),
        password_file=os.fspath(_absolute_path(f"{prefix}_PASSFILE", must_exist=True)),
        sslmode="require",
    )


async def _run() -> RecoveryOperatorResult:
    profile = load_profile(_absolute_path("SPINE_RECOVERY_PROFILE", must_exist=True))
    source_connection = _connection("SPINE_SOURCE_BACKUP")
    restore_connection = _connection("SPINE_RESTORE_WRITER")
    verifier_connection = _connection("SPINE_RESTORE_VERIFIER")
    tenant_scope = TenantInventoryScope(
        UUID(_required("SPINE_RECOVERY_WORKSPACE_ID")),
        UUID(_required("SPINE_RECOVERY_ENVIRONMENT_ID")),
    )
    protection = build_backup_protection_profile(profile)
    artifact_store = FilesystemBackupArtifactStore(
        _absolute_path("SPINE_RECOVERY_ARTIFACT_ROOT"), protection=protection
    )
    inventory = PsycopgCanonicalOriginalInventory(
        source_connection, tenant_scopes=(tenant_scope,)
    )
    object_store = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=_absolute_path("SPINE_SOURCE_ORIGINAL_ROOT")),
        grant_registry=_UnusedGrantRegistry(),  # type: ignore[arg-type]
    )
    postgres_source = PostgreSQLCustomFormatBackupSource(
        connection=source_connection,
        inventory=inventory,
        artifact_store=artifact_store,
        pg_dump_executable=_absolute_path("SPINE_PG_DUMP", must_exist=True),
        pg_restore_executable=_absolute_path("SPINE_PG_RESTORE", must_exist=True),
    )
    object_source = FilesystemOriginalStorageBackupSource(
        object_store=object_store,
        inventory=inventory,
        artifact_store=artifact_store,
        volume_generation=profile.source_volume_reference,
    )
    engine = create_async_engine(
        _required("SPINE_COORDINATOR_DATABASE_URL"),
        hide_parameters=True,
        connect_args={"options": "-csearch_path=spine,public", "sslmode": "require"},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    repository = PostgreSQLBackupRecoveryRepository(session_factory=sessions)
    coordinator = BackupRecoveryCoordinator(
        repository=repository,
        ticket_id_factory=lambda: f"ticket:{uuid4().hex}",
        recovery_set_id_factory=lambda: profile.recovery_set_id,
        clock=lambda: datetime.now(timezone.utc),
        evidence_provider=_PostgreSQLWatermark(source_connection),
    )
    backup_profile = CoordinatedBackupProfile(
        coordinator=coordinator,
        postgres_source=postgres_source,
        object_source=object_source,
        protection=protection,
    )
    restore_target = IsolatedRestoreTarget(
        database=restore_connection.database,
        restore_connection=restore_connection,
        verifier_connection=verifier_connection,
        filesystem_root=_absolute_path("SPINE_RESTORE_ORIGINAL_ROOT"),
        verifier_scopes=(tenant_scope,),
        serving_identities=tuple(_required("SPINE_RESTORE_SERVING_IDENTITIES").split(",")),
    )
    source_facts = RecoverySourceInventoryFacts(
        inventory_reference=profile.source_inventory_reference,
        deployment_scope_reference=profile.deployment_scope_reference,
        scope_reference=profile.scope_reference,
        source_volume_reference=profile.source_volume_reference,
    )
    target_facts = RecoveryRestoreTargetFacts(
        inventory_reference=profile.restore_target_inventory_reference,
        target_reference="target:issue509-restore-001",
        database_target_reference="database:issue509-restore-001",
        filesystem_target_reference="filesystem:issue509-restore-001",
        serving_identity_inventory_reference=profile.serving_identity_inventory_reference,
        restore_identity_reference=profile.restore_identity_reference,
        verifier_identity_reference=profile.verifier_identity_reference,
    )
    operator = RecoveryOperatorService(
        approval=_Approval(
            _required("SPINE_RECOVERY_APPROVAL_REFERENCE"),
            datetime.fromisoformat(_required("SPINE_RECOVERY_APPROVAL_EXPIRES_AT").replace("Z", "+00:00")),
        ),
        source_inventory=_SourceInventory(source_facts),
        target_inventory=_TargetInventory(target_facts, restore_target),
        backup_profile=backup_profile,
        manifest_reader=_ManifestReader(coordinator, profile.deployment_scope_reference),
        artifact_resolver=_ArtifactResolver(
            coordinator, artifact_store, profile.deployment_scope_reference
        ),
        restore_verifier=IsolatedRestoreVerifier(
            pg_restore_executable=_absolute_path("SPINE_PG_RESTORE", must_exist=True)
        ),
        evidence=_Evidence(_absolute_path("SPINE_RECOVERY_EVIDENCE_ROOT")),
    )
    try:
        return await run_operator_command(profile, operator)
    finally:
        await engine.dispose()


def main() -> None:
    try:
        result = asyncio.run(_run())
    except Exception:
        print(json.dumps({"schema_version": "spine-recovery-operator-result:v1", "status": "blocked", "safe_failure_code": "composition.failed"}))
        raise SystemExit(2) from None
    print(result.model_dump_json())
    raise SystemExit(0 if result.status == "verified" else 3)


if __name__ == "__main__":
    main()
