"""PostgreSQL custom-format backup source and canonical-original inventory."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from spine.application.storage.backup_profile import (
    BackupComponentFailure,
    BackupProtectionProfile,
    CanonicalOriginalReferenceInventory,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.domain.storage.backup_profile import canonical_included_set_digest
from spine.domain.storage.recovery import (
    RecoveryComponentEvidence,
    RecoveryFailureCategory,
)

from .backup_artifacts import FilesystemBackupArtifactStore, run_backup_io


_PG_VERSION = re.compile(r"PostgreSQL\)\s+(\d+(?:\.\d+){1,2})\b")


@dataclass(frozen=True, slots=True)
class PostgreSQLBackupConnection:
    """Non-secret libpq settings; credentials are supplied by passfile/service."""

    database: str
    username: str
    backup_role: str
    host: str | None = None
    port: int | None = None
    password_file: str | None = None
    service: str | None = None
    service_file: str | None = None
    sslmode: str = "verify-full"

    def __post_init__(self) -> None:
        if not self.database or not self.username or not self.backup_role:
            raise ValueError("PostgreSQL backup identity settings are required")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("PostgreSQL port is invalid")
        if self.sslmode not in {"verify-full", "verify-ca", "require"}:
            raise ValueError("PostgreSQL backup requires protected transport")
        if self.password_file is not None and not Path(self.password_file).is_absolute():
            raise ValueError("PostgreSQL password file path must be absolute")
        if self.service_file is not None and not Path(self.service_file).is_absolute():
            raise ValueError("PostgreSQL service file path must be absolute")
        for value in (self.database, self.username, self.backup_role):
            if not _is_safe_identifier(value):
                raise ValueError("PostgreSQL backup identity reference is invalid")

    def connection_kwargs(self) -> dict[str, object]:
        values: dict[str, object] = {
            "dbname": self.database,
            "user": self.username,
            "sslmode": self.sslmode,
        }
        for attribute, key in (
            ("host", "host"),
            ("port", "port"),
            ("password_file", "passfile"),
            ("service", "service"),
            ("service_file", "servicefile"),
        ):
            value = getattr(self, attribute)
            if value is not None:
                values[key] = value
        return values

    def process_environment(self) -> dict[str, str]:
        values = {
            "PGDATABASE": self.database,
            "PGUSER": self.username,
            "PGSSLMODE": self.sslmode,
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", ""),
        }
        for attribute, key in (
            ("host", "PGHOST"),
            ("port", "PGPORT"),
            ("password_file", "PGPASSFILE"),
            ("service", "PGSERVICE"),
            ("service_file", "PGSERVICEFILE"),
        ):
            value = getattr(self, attribute)
            if value is not None:
                values[key] = str(value)
        return values


@dataclass(frozen=True, slots=True)
class TenantInventoryScope:
    """Trusted workspace/environment scope allowed to read canonical metadata."""

    workspace_id: UUID
    environment_id: UUID


class PsycopgCanonicalOriginalInventory(CanonicalOriginalReferenceInventory):
    """Read verified receipt references under the configured backup identity."""

    def __init__(
        self,
        connection: PostgreSQLBackupConnection,
        *,
        tenant_scopes: tuple[TenantInventoryScope, ...],
    ) -> None:
        if not tenant_scopes or len(set(tenant_scopes)) != len(tenant_scopes):
            raise ValueError("canonical backup inventory requires unique tenant scopes")
        self._connection = connection
        self._tenant_scopes = tenant_scopes

    async def finalized_references(self) -> tuple[ObjectReference, ...]:
        try:
            from psycopg import AsyncConnection
            from psycopg.rows import dict_row

            async with await AsyncConnection.connect(
                **self._connection.connection_kwargs(), row_factory=dict_row
            ) as connection:
                rows = []
                for scope in self._tenant_scopes:
                    async with connection.transaction():
                        async with connection.cursor() as cursor:
                            await cursor.execute(
                                "SELECT set_config('role', %s, true), "
                                "set_config('spine.workspace_id', %s, true), "
                                "set_config('spine.environment_id', %s, true)",
                                (
                                    self._connection.backup_role,
                                    str(scope.workspace_id),
                                    str(scope.environment_id),
                                ),
                            )
                            await cursor.execute(
                                "SELECT original_reference "
                                "FROM spine.source_revision_receipt_associations "
                                "WHERE verification_status = 'verified' "
                                "AND storage_status = 'finalized_verified' "
                                "ORDER BY association_id"
                            )
                            rows.extend(await cursor.fetchall())
            unique: dict[object, ObjectReference] = {}
            for row in rows:
                raw = row["original_reference"]
                if isinstance(raw, str):
                    raw = json.loads(raw)
                reference = ObjectReference.model_validate(raw, strict=False)
                current = unique.get(reference.object_id)
                if current is not None and current != reference:
                    raise BackupComponentFailure(
                        RecoveryFailureCategory.COMPONENT_IDENTITY_MISMATCH
                    )
                unique[reference.object_id] = reference
            return tuple(sorted(unique.values(), key=lambda item: item.object_id.hex))
        except BackupComponentFailure:
            raise
        except Exception:
            raise BackupComponentFailure(
                RecoveryFailureCategory.COMPONENT_FAILURE
            ) from None


class PostgreSQLCustomFormatBackupSource:
    """Create a pg_dump custom archive and verify it through compatible pg_restore."""

    def __init__(
        self,
        *,
        connection: PostgreSQLBackupConnection,
        inventory: CanonicalOriginalReferenceInventory,
        artifact_store: FilesystemBackupArtifactStore,
        pg_dump_executable: str | os.PathLike[str],
        pg_restore_executable: str | os.PathLike[str],
        command_timeout_seconds: float = 900,
    ) -> None:
        self._connection = connection
        self._inventory = inventory
        self._artifact_store = artifact_store
        self._pg_dump = _absolute_executable(pg_dump_executable)
        self._pg_restore = _absolute_executable(pg_restore_executable)
        if command_timeout_seconds <= 0:
            raise ValueError("PostgreSQL backup command timeout must be positive")
        self._timeout = command_timeout_seconds

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
        del deployment_scope_id
        references = await self._inventory.finalized_references()
        actual_set_digest = canonical_included_set_digest(references)
        if included_set_digest is not None and included_set_digest != actual_set_digest:
            raise BackupComponentFailure(
                RecoveryFailureCategory.COMPONENT_IDENTITY_MISMATCH
            )
        dump_version, restore_version = await asyncio.gather(
            self._tool_version(self._pg_dump),
            self._tool_version(self._pg_restore),
        )
        dump_major = dump_version.split(".", maxsplit=1)[0]
        restore_major = restore_version.split(".", maxsplit=1)[0]
        if dump_major != restore_major:
            raise BackupComponentFailure(
                RecoveryFailureCategory.COMPONENT_IDENTITY_MISMATCH
            )

        with tempfile.TemporaryDirectory(prefix="spine-pg-backup-") as temp_dir:
            archive_path = Path(temp_dir) / "component.dump"
            await self._run_tool(
                self._pg_dump,
                (
                    "--format=custom",
                    "--no-password",
                    "--role",
                    self._connection.backup_role,
                    "--file",
                    os.fspath(archive_path),
                ),
                capture_stdout=False,
            )
            dump_digest, dump_length = await run_backup_io(
                _digest_regular_file, archive_path
            )
            if dump_length == 0:
                raise BackupComponentFailure(RecoveryFailureCategory.INTEGRITY_FAILURE)
            await self._verify_archive(archive_path, temp_dir)
            try:
                stored = await self._artifact_store.persist_file(
                    recovery_set_id=recovery_set_id,
                    component="postgres",
                    source_path=archive_path,
                    expected_sha256=dump_digest,
                    protection=protection,
                )
                persisted = await self._artifact_store.verify_file(
                    stored.artifact_reference,
                    expected_sha256=dump_digest,
                )
                await self._verify_archive(persisted.local_path, temp_dir)
            except Exception:
                raise BackupComponentFailure(
                    RecoveryFailureCategory.COMPONENT_FAILURE
                ) from None

        identity = f"postgres-backup:{stored.sha256}"
        return RecoveryComponentEvidence(
            component="postgres",
            expected_identity=identity,
            observed_identity=identity,
            artifact_reference=stored.artifact_reference,
            artifact_digest=stored.sha256,
            tool_profile="pg_dump-custom-compatible-restore:v1",
            tool_version=f"postgres-{dump_version}",
            fence_epoch=fence_epoch,
            cutoff_ticket_sequence=cutoff_ticket_sequence,
            included_set_digest=actual_set_digest,
            inclusion_policy=inclusion_policy,
            durable=True,
            verified=True,
            verification_reference=f"pg_restore-list:{stored.sha256}",
        )

    async def _tool_version(self, executable: Path) -> str:
        output = await self._run_tool(executable, ("--version",), capture_stdout=True)
        match = _PG_VERSION.search(output)
        if match is None:
            raise BackupComponentFailure(RecoveryFailureCategory.COMPONENT_FAILURE)
        return match.group(1)

    async def _verify_archive(self, archive_path: Path, temp_dir: str) -> None:
        listing = Path(temp_dir) / f"{archive_path.name}.toc"
        try:
            await self._run_tool(
                self._pg_restore,
                ("--list", "--file", os.fspath(listing), os.fspath(archive_path)),
                capture_stdout=False,
            )
        except BackupComponentFailure as error:
            if error.category is RecoveryFailureCategory.TIMEOUT:
                raise
            raise BackupComponentFailure(
                RecoveryFailureCategory.INTEGRITY_FAILURE
            ) from None
        try:
            listing_content = listing.read_text(encoding="utf-8")
            if "source_revision_receipt_associations" not in listing_content:
                raise BackupComponentFailure(RecoveryFailureCategory.INTEGRITY_FAILURE)
        except OSError:
            raise BackupComponentFailure(RecoveryFailureCategory.INTEGRITY_FAILURE) from None

    async def _run_tool(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        *,
        capture_stdout: bool,
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                os.fspath(executable),
                *arguments,
                env=self._connection.process_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except TimeoutError:
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            raise BackupComponentFailure(RecoveryFailureCategory.TIMEOUT) from None
        except (OSError, ValueError):
            raise BackupComponentFailure(RecoveryFailureCategory.COMPONENT_FAILURE) from None
        if process.returncode != 0:
            raise BackupComponentFailure(RecoveryFailureCategory.COMPONENT_FAILURE)
        return stdout.decode("utf-8", errors="replace") if stdout is not None else ""


def _absolute_executable(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("PostgreSQL tool executable must be an absolute regular file")
    return path


def _digest_regular_file(path: Path) -> tuple[str, int]:
    import hashlib
    import stat

    digest = hashlib.sha256()
    total = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupComponentFailure(RecoveryFailureCategory.INTEGRITY_FAILURE)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
    except BackupComponentFailure:
        raise
    except OSError:
        raise BackupComponentFailure(RecoveryFailureCategory.INTEGRITY_FAILURE) from None
    finally:
        os.close(descriptor)
    return digest.hexdigest(), total


def _is_safe_identifier(value: str) -> bool:
    return bool(value) and len(value) <= 128 and all(
        char.isascii() and (char.isalnum() or char in "_.:/-") for char in value
    )


__all__ = [
    "PostgreSQLBackupConnection",
    "PostgreSQLCustomFormatBackupSource",
    "PsycopgCanonicalOriginalInventory",
    "TenantInventoryScope",
]
