"""Physical verification of a coordinated backup in isolated restore targets.

This module never connects to the live stores. Callers must provide a dedicated
restore database (named with the ``spine_restore_`` prefix) and an absent,
dedicated filesystem target. Restored bytes are kept staged until every check
has passed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from spine.domain.sources.original_reference import ObjectReference
from spine.domain.storage.backup_profile import canonical_included_set_digest
from spine.domain.storage.recovery import StorageRecoveryManifest
from spine.infrastructure.postgres_backup import PostgreSQLBackupConnection, TenantInventoryScope
from spine.infrastructure.backup_artifacts import run_backup_io


class IsolatedRestoreTarget(BaseModel):
    """Explicit deployment binding for a disposable restore destination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    database: str = Field(min_length=1, max_length=128)
    restore_connection: PostgreSQLBackupConnection
    verifier_connection: PostgreSQLBackupConnection
    filesystem_root: Path
    verifier_scopes: tuple[TenantInventoryScope, ...]
    serving_identities: tuple[str, ...]

    def validate_target(self, *, allow_verified_existing: bool = False) -> "IsolatedRestoreTarget":
        if not self.database.startswith("spine_restore_"):
            raise ValueError("isolated restore database must use the spine_restore_ prefix")
        if self.restore_connection.database != self.database:
            raise ValueError("restore connection must target the isolated database")
        if self.verifier_connection.database != self.database:
            raise ValueError("verifier connection must target the isolated database")
        if self.restore_connection.username == self.verifier_connection.username:
            raise ValueError("restore and verifier identities must be separate")
        if self.restore_connection.backup_role == self.verifier_connection.backup_role:
            raise ValueError("restore and verifier roles must be separate")
        if not self.verifier_scopes or len(set(self.verifier_scopes)) != len(self.verifier_scopes):
            raise ValueError("restore verification requires unique trusted tenant scopes")
        if not self.serving_identities or len(set(self.serving_identities)) != len(self.serving_identities):
            raise ValueError("restore target requires the complete serving identity set")
        if any(
            not role.isascii()
            or not role
            or len(role) > 128
            or any(not (char.isalnum() or char in "_.:/-") for char in role)
            for role in self.serving_identities
        ):
            raise ValueError("serving identity reference is invalid")
        if set(self.serving_identities) & {
            self.restore_connection.username,
            self.verifier_connection.username,
            self.verifier_connection.backup_role,
        }:
            raise ValueError("serving identities must be separate from restore verification identities")
        root = self.filesystem_root
        if not root.is_absolute() or root.is_symlink():
            raise ValueError("isolated filesystem restore target must be absolute and not a symlink")
        if any(parent.is_symlink() for parent in (root, *root.parents)):
            raise ValueError("isolated filesystem restore target cannot cross a symlink")
        if not root.parent.is_dir():
            raise ValueError("isolated filesystem restore parent must already exist")
        if root.exists() and not allow_verified_existing:
            raise ValueError("isolated filesystem restore target must be absent")
        if root.exists() and (not root.is_dir() or not allow_verified_existing):
            raise ValueError("isolated filesystem restore target is unavailable")
        return self


class IsolatedRestoreEvidence(BaseModel):
    """Content-free result of one actual isolated physical restore exercise."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    recovery_set_id: str = Field(min_length=1, max_length=256)
    manifest_digest: str = Field(min_length=64, max_length=64)
    outcome: Literal["verified", "rejected", "unavailable", "indeterminate"]
    safe_failure_code: Literal[
        "restore.artifact_unavailable",
        "restore.component_mismatch",
        "restore.tool_unavailable",
        "restore.execution_failed",
        "restore.metadata_mismatch",
        "restore.originals_mismatch",
        "restore.target_unavailable",
    ] | None = None
    postgres_digest: str | None = None
    filesystem_digest: str | None = None
    included_set_digest: str | None = None
    fence_epoch: int = Field(ge=1)
    cutoff_ticket_sequence: int = Field(ge=0)
    verifier_identity: str = Field(min_length=1, max_length=128)
    verified_at: datetime


class IsolatedRestoreFailure(RuntimeError):
    """Disclosure-safe failure with independently classified restore outcome."""

    def __init__(self, evidence: IsolatedRestoreEvidence) -> None:
        super().__init__("isolated restore verification did not pass")
        self.evidence = evidence


class IsolatedPostgreSQLRestorePort(Protocol):
    """Restore and inspect an isolated PostgreSQL target for the coordinator."""

    async def restore_and_read(
        self,
        archive: Path,
        target: IsolatedRestoreTarget,
        *,
        expected_component_version: str,
    ) -> tuple[ObjectReference, ...]: ...


@dataclass(frozen=True, slots=True)
class RestoredOriginal:
    reference: ObjectReference
    manifest_sha256: str


class IsolatedRestoreVerifier:
    """Restore both physical components, reconcile them, then publish evidence."""

    def __init__(
        self,
        *,
        pg_restore_executable: str | os.PathLike[str],
        command_timeout_seconds: float = 900,
        postgres_restore_port: IsolatedPostgreSQLRestorePort | None = None,
    ) -> None:
        executable = Path(pg_restore_executable)
        if not executable.is_absolute() or executable.is_symlink() or not executable.is_file():
            raise ValueError("pg_restore executable must be an absolute regular file")
        if command_timeout_seconds <= 0:
            raise ValueError("restore command timeout must be positive")
        self._pg_restore = executable
        self._timeout = command_timeout_seconds
        self._postgres_restore_port = postgres_restore_port or _DefaultIsolatedPostgreSQLRestorePort(self)

    async def verify_and_restore(
        self,
        *,
        manifest: StorageRecoveryManifest,
        postgres_artifact: str | os.PathLike[str],
        filesystem_artifact: str | os.PathLike[str],
        target: IsolatedRestoreTarget,
    ) -> IsolatedRestoreEvidence:
        """Perform actual pg_restore and filesystem materialization in isolated targets."""

        try:
            target.validate_target(allow_verified_existing=True)
        except ValueError:
            raise self._failure(manifest, target, "unavailable", "restore.target_unavailable") from None
        postgres_evidence = manifest.postgres_component_evidence
        filesystem_evidence = manifest.object_component_evidence
        if (
            manifest.manifest_status.value != "coherent"
            or postgres_evidence is None
            or filesystem_evidence is None
            or not postgres_evidence.verified
            or not filesystem_evidence.verified
            or postgres_evidence.fence_epoch != filesystem_evidence.fence_epoch
            or postgres_evidence.cutoff_ticket_sequence != filesystem_evidence.cutoff_ticket_sequence
            or postgres_evidence.included_set_digest != filesystem_evidence.included_set_digest
            or postgres_evidence.inclusion_policy != filesystem_evidence.inclusion_policy
        ):
            raise self._failure(manifest, target, "rejected", "restore.component_mismatch")

        pg_path = Path(postgres_artifact)
        fs_path = Path(filesystem_artifact)
        try:
            pg_digest = _digest_artifact(pg_path)
            fs_digest = _digest_artifact(fs_path)
            if pg_digest != postgres_evidence.artifact_digest or fs_digest != filesystem_evidence.artifact_digest:
                raise self._failure(manifest, target, "rejected", "restore.artifact_unavailable")
        except IsolatedRestoreFailure:
            raise
        except OSError:
            raise self._failure(manifest, target, "unavailable", "restore.artifact_unavailable") from None

        if target.filesystem_root.exists():
            try:
                recorded = _read_verified_evidence(target.filesystem_root / "restore-evidence.json")
            except (OSError, ValueError):
                raise self._failure(manifest, target, "indeterminate", "restore.target_unavailable") from None
            if (
                recorded.recovery_set_id != manifest.recovery_set_id
                or recorded.manifest_digest != manifest.manifest_digest
                or recorded.postgres_digest != pg_digest
                or recorded.filesystem_digest != fs_digest
                or recorded.outcome != "verified"
            ):
                raise self._failure(manifest, target, "indeterminate", "restore.target_unavailable")
            return recorded

        try:
            staging_root = target.filesystem_root.with_name(
                f".{target.filesystem_root.name}.restore-{manifest.recovery_set_id}"
            )
        except ValueError:
            raise self._failure(manifest, target, "unavailable", "restore.target_unavailable") from None
        try:
            if staging_root.exists() or staging_root.is_symlink():
                raise self._failure(manifest, target, "indeterminate", "restore.target_unavailable")
            staging_root.mkdir(mode=0o700, parents=False)
            originals = await run_backup_io(FilesystemArchiveMaterializer().materialize, fs_path, staging_root)
            canonical = await self._postgres_restore_port.restore_and_read(
                pg_path,
                target,
                expected_component_version=postgres_evidence.tool_version.removeprefix("postgres-"),
            )
            _reconcile_originals(canonical, originals, manifest)
            evidence = self._evidence(
                manifest,
                target,
                outcome="verified",
                postgres_digest=pg_digest,
                filesystem_digest=fs_digest,
            )
            _write_new_file(
                staging_root / "restore-evidence.json",
                evidence.model_dump_json().encode("utf-8"),
            )
            _fsync_directory(staging_root)
            os.replace(staging_root, target.filesystem_root)
            _fsync_directory(target.filesystem_root.parent)
            return evidence
        except IsolatedRestoreFailure:
            _remove_isolated_tree(staging_root)
            raise
        except TimeoutError:
            _remove_isolated_tree(staging_root)
            raise self._failure(manifest, target, "indeterminate", "restore.execution_failed") from None
        except FileNotFoundError:
            _remove_isolated_tree(staging_root)
            raise self._failure(manifest, target, "unavailable", "restore.tool_unavailable") from None
        except Exception:
            _remove_isolated_tree(staging_root)
            raise self._failure(manifest, target, "rejected", "restore.metadata_mismatch") from None

    async def _restore_postgres(
        self,
        archive: Path,
        target: IsolatedRestoreTarget,
        *,
        expected_component_version: str,
    ) -> None:
        connection = target.restore_connection
        actual_version = await self._tool_version(connection)
        if actual_version.split(".", maxsplit=1)[0] != expected_component_version.split(".", maxsplit=1)[0]:
            raise RuntimeError("restore tool is incompatible with the archived component")
        arguments = [
            os.fspath(self._pg_restore),
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--single-transaction",
            "--dbname",
            target.database,
        ]
        arguments.append(os.fspath(archive))
        env = connection.process_environment()
        env["PGUSER"] = connection.username
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise RuntimeError("isolated restore command failed")

    async def _tool_version(self, connection: PostgreSQLBackupConnection) -> str:
        process = await asyncio.create_subprocess_exec(
            os.fspath(self._pg_restore),
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=connection.process_environment(),
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0 or stdout is None:
            raise RuntimeError("restore tool is unavailable")
        match = re.search(rb"PostgreSQL\)\s+(\d+(?:\.\d+){1,2})\b", stdout)
        if match is None:
            raise RuntimeError("restore tool version is unavailable")
        return match.group(1).decode("ascii")

    def _failure(
        self,
        manifest: StorageRecoveryManifest,
        target: IsolatedRestoreTarget,
        outcome: Literal["rejected", "unavailable", "indeterminate"],
        code: str,
    ) -> IsolatedRestoreFailure:
        return IsolatedRestoreFailure(
            self._evidence(manifest, target, outcome=outcome, safe_failure_code=code)
        )

    @staticmethod
    def _evidence(
        manifest: StorageRecoveryManifest,
        target: IsolatedRestoreTarget,
        *,
        outcome: Literal["verified", "rejected", "unavailable", "indeterminate"],
        safe_failure_code: str | None = None,
        postgres_digest: str | None = None,
        filesystem_digest: str | None = None,
    ) -> IsolatedRestoreEvidence:
        postgres_evidence = manifest.postgres_component_evidence
        return IsolatedRestoreEvidence(
            recovery_set_id=manifest.recovery_set_id,
            manifest_digest=manifest.manifest_digest,
            outcome=outcome,
            safe_failure_code=safe_failure_code,
            postgres_digest=postgres_digest,
            filesystem_digest=filesystem_digest,
            included_set_digest=(postgres_evidence.included_set_digest if postgres_evidence else None),
            fence_epoch=manifest.fence_epoch,
            cutoff_ticket_sequence=manifest.cutoff_ticket_sequence,
            verifier_identity=target.verifier_connection.username,
            verified_at=datetime.now(timezone.utc),
        )


class _DefaultIsolatedPostgreSQLRestorePort:
    """Bind the PostgreSQL lifecycle to the production restore implementation."""

    def __init__(self, verifier: IsolatedRestoreVerifier) -> None:
        self._verifier = verifier

    async def restore_and_read(
        self,
        archive: Path,
        target: IsolatedRestoreTarget,
        *,
        expected_component_version: str,
    ) -> tuple[ObjectReference, ...]:
        await _assert_empty_restore_database(target)
        await _seal_restore_database(target)
        await self._verifier._restore_postgres(
            archive,
            target,
            expected_component_version=expected_component_version,
        )
        await _harden_restored_schemas(target)
        await _grant_verifier_read(target)
        return await _read_restored_references(target)


async def _read_restored_references(
    target: IsolatedRestoreTarget,
) -> tuple[ObjectReference, ...]:
    """Read canonical metadata under each trusted tenant scope and verifier role."""

    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    connection_settings = target.verifier_connection
    result: dict[UUID, ObjectReference] = {}
    async with await AsyncConnection.connect(
        **connection_settings.connection_kwargs(), row_factory=dict_row
    ) as connection:
        for scope in target.verifier_scopes:
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT set_config('role', %s, true), "
                        "set_config('spine.workspace_id', %s, true), "
                        "set_config('spine.environment_id', %s, true)",
                        (connection_settings.backup_role, str(scope.workspace_id), str(scope.environment_id)),
                    )
                    await cursor.execute(
                        "SELECT original_reference FROM spine.source_revision_receipt_associations "
                        "WHERE verification_status = 'verified' "
                        "AND storage_status = 'finalized_verified' ORDER BY association_id"
                    )
                    for row in await cursor.fetchall():
                        raw = row["original_reference"]
                        if isinstance(raw, str):
                            raw = json.loads(raw)
                        reference = ObjectReference.model_validate(raw, strict=False)
                        previous = result.get(reference.object_id)
                        if previous is not None and previous != reference:
                            raise ValueError("restored canonical references conflict")
                        result[reference.object_id] = reference
    return tuple(sorted(result.values(), key=lambda item: item.object_id.hex))


async def _assert_empty_restore_database(target: IsolatedRestoreTarget) -> None:
    """Refuse to overlay an existing application database in the restore target."""

    from psycopg import AsyncConnection

    connection_settings = target.restore_connection
    async with await AsyncConnection.connect(**connection_settings.connection_kwargs()) as connection:
        async with connection.cursor() as cursor:
            protected_roles = (
                connection_settings.username,
                target.verifier_connection.username,
                target.verifier_connection.backup_role,
                *target.serving_identities,
            )
            placeholders = ", ".join("%s" for _ in protected_roles)
            await cursor.execute(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
                f"WHERE rolname IN ({placeholders})",
                protected_roles,
            )
            role_rows = await cursor.fetchall()
            roles_by_name = {row[0]: row for row in role_rows}
            if set(roles_by_name) != set(protected_roles) or any(
                row[1] or row[2] for row in role_rows
            ):
                raise ValueError("restore, verifier and serving roles must exist without isolation bypass")
            await _assert_no_unapproved_memberships(cursor, target)
            await cursor.execute(
                "SELECT count(*) FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
                "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'"
            )
            row = await cursor.fetchone()
            if row is None or row[0] != 0:
                raise ValueError("isolated restore database is not empty")


async def _seal_restore_database(target: IsolatedRestoreTarget) -> None:
    """Restrict the fresh target to restore and independent verification roles."""

    from psycopg import AsyncConnection, sql

    connection_settings = target.restore_connection
    async with await AsyncConnection.connect(**connection_settings.connection_kwargs()) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database "
                "WHERE datname = current_database()"
            )
            row = await cursor.fetchone()
            if row is None or row[0] != connection_settings.username:
                raise ValueError("restore identity must own the isolated database")
            await cursor.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(target.database)
                )
            )
            await cursor.execute(
                "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname NOT IN (%s, %s)",
                (
                    connection_settings.username,
                    target.verifier_connection.username,
                ),
            )
            for (role_name,) in await cursor.fetchall():
                await cursor.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                        sql.Identifier(target.database), sql.Identifier(role_name)
                    )
                )
            await cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(target.database),
                    sql.Identifier(target.verifier_connection.username),
                )
            )


async def _harden_restored_schemas(target: IsolatedRestoreTarget) -> None:
    """Remove default/public/serving access from every restored application schema."""

    from psycopg import AsyncConnection, sql

    connection_settings = target.restore_connection
    verifier_roles = {
        target.verifier_connection.username,
        target.verifier_connection.backup_role,
    }
    async with await AsyncConnection.connect(**connection_settings.connection_kwargs()) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT nspname FROM pg_catalog.pg_namespace "
                "WHERE nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'"
            )
            schemas = tuple(row[0] for row in await cursor.fetchall())
            await cursor.execute("SELECT rolname FROM pg_catalog.pg_roles")
            all_roles = tuple(row[0] for row in await cursor.fetchall())
            for schema_name in schemas:
                for role_name in ("PUBLIC", *all_roles):
                    if role_name in {
                        connection_settings.username,
                        *verifier_roles,
                    }:
                        continue
                    role = sql.SQL("PUBLIC") if role_name == "PUBLIC" else sql.Identifier(role_name)
                    schema = sql.Identifier(schema_name)
                    await cursor.execute(
                        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(schema, role)
                    )
                    for object_kind in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                        await cursor.execute(
                            sql.SQL("REVOKE ALL PRIVILEGES ON ALL {} IN SCHEMA {} FROM {}").format(
                                sql.SQL(object_kind), schema, role
                            )
                        )


async def _assert_no_unapproved_memberships(cursor: object, target: IsolatedRestoreTarget) -> None:
    """Serving roles must not inherit restore-owner or verifier privileges."""

    protected_roles = (
        target.restore_connection.username,
        target.verifier_connection.username,
        target.verifier_connection.backup_role,
    )
    await cursor.execute(
        "WITH RECURSIVE inherited_roles (granted_role, member_role) AS ("
        "SELECT roleid, member FROM pg_catalog.pg_auth_members UNION "
        "SELECT inherited_roles.granted_role, membership.member "
        "FROM inherited_roles JOIN pg_catalog.pg_auth_members AS membership "
        "ON membership.roleid = inherited_roles.member_role) "
        "SELECT granted.rolname, member.rolname FROM inherited_roles "
        "JOIN pg_catalog.pg_roles AS granted ON granted.oid = inherited_roles.granted_role "
        "JOIN pg_catalog.pg_roles AS member ON member.oid = inherited_roles.member_role "
        "WHERE granted.rolname = ANY(%s)",
        (list(protected_roles),),
    )
    memberships = await cursor.fetchall()
    for granted_role, member_role in memberships:
        if (
            granted_role == target.verifier_connection.backup_role
            and member_role == target.verifier_connection.username
        ):
            continue
        raise ValueError("unexpected membership could expose isolated restore data")


async def _grant_verifier_read(target: IsolatedRestoreTarget) -> None:
    """Grant the isolated verifier role only the canonical reference read it needs."""

    from psycopg import AsyncConnection, sql

    connection_settings = target.restore_connection
    role_name = target.verifier_connection.backup_role
    async with await AsyncConnection.connect(**connection_settings.connection_kwargs()) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA spine TO {}").format(sql.Identifier(role_name))
            )
            await cursor.execute(
                sql.SQL("GRANT SELECT ON TABLE spine.source_revision_receipt_associations TO {}").format(
                    sql.Identifier(role_name)
                )
            )


def _materialize_archive(archive_path: Path, target_root: Path) -> tuple[RestoredOriginal, ...]:
    """Extract only regular known members, verifying private receipt and payload bytes."""

    manifests: dict[str, tuple[ObjectReference, bytes]] = {}
    payloads: dict[str, tarfile.TarInfo] = {}
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        if any(not member.isfile() or not _safe_member_name(member.name) for member in members):
            raise ValueError("unsupported archive member")
        if len({member.name for member in members}) != len(members):
            raise ValueError("archive contains duplicate member identities")
        for member in members:
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("archive member unavailable")
            if member.name.endswith(".private"):
                raw_bytes = stream.read(1024 * 1024 + 1)
                if len(raw_bytes) > 1024 * 1024:
                    raise ValueError("private receipt exceeds the supported size")
                private = json.loads(raw_bytes)
                from spine.infrastructure.object_storage.contracts import WriteReceipt

                receipt = WriteReceipt.model_validate(private["receipt"], strict=False)
                manifests[member.name] = (receipt.object_reference, raw_bytes)
            else:
                payloads[member.name] = member

        if len(manifests) != len(payloads):
            raise ValueError("archive original set is incomplete")
        originals: list[RestoredOriginal] = []
        discovered: set[UUID] = set()
        for manifest_name, (reference, raw_manifest) in manifests.items():
            if reference.object_id in discovered:
                raise ValueError("archive contains duplicate original identity")
            discovered.add(reference.object_id)
            scope = manifest_name.split("/")[1]
            payload_name = f"objects/{scope}/{reference.object_id.hex}"
            member = payloads.get(payload_name)
            if member is None or member.size != reference.byte_length:
                raise ValueError("archive payload does not match private receipt")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("archive payload unavailable")
            destination = target_root / scope / reference.object_id.hex
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            digest = hashlib.sha256()
            length = 0
            with os.fdopen(descriptor, "wb") as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    length += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            _fsync_directory(destination.parent)
            if digest.hexdigest() != reference.digest_hex or length != reference.byte_length:
                raise ValueError("archive payload digest mismatch")
            private_destination = destination.with_name(destination.name + ".private")
            _write_new_file(private_destination, raw_manifest)
            originals.append(
                RestoredOriginal(reference, hashlib.sha256(raw_manifest).hexdigest())
            )
        return tuple(sorted(originals, key=lambda item: item.reference.object_id.hex))


class FilesystemArchiveMaterializer:
    """Public adapter for restoring verified object archive contents to staging."""

    def materialize(self, archive_path: Path, target_root: Path) -> tuple[RestoredOriginal, ...]:
        return _materialize_archive(archive_path, target_root)


def _reconcile_originals(
    canonical: tuple[ObjectReference, ...],
    originals: tuple[RestoredOriginal, ...],
    manifest: StorageRecoveryManifest,
) -> None:
    canonical_by_id = {reference.object_id: reference for reference in canonical}
    original_by_id = {item.reference.object_id: item for item in originals}
    if canonical_by_id != {key: value.reference for key, value in original_by_id.items()}:
        raise ValueError("restored canonical metadata does not match restored originals")
    if canonical_included_set_digest(canonical) != manifest.postgres_component_evidence.included_set_digest:
        raise ValueError("restored included set differs from recovery evidence")


def _safe_member_name(name: str) -> bool:
    parts = name.split("/")
    return (
        len(parts) == 3
        and parts[0] == "objects"
        and len(parts[1]) == 64
        and all(char in "0123456789abcdef" for char in parts[1])
        and parts[2] not in {"", ".", ".."}
        and not any(part in {".", ".."} for part in parts)
    )


def _digest_artifact(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise OSError("artifact is unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_verified_evidence(path: Path) -> IsolatedRestoreEvidence:
    if path.is_symlink() or not path.is_file():
        raise ValueError("restore evidence is unavailable")
    return IsolatedRestoreEvidence.model_validate_json(path.read_bytes())


def _write_new_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_isolated_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


__all__ = [
    "FilesystemArchiveMaterializer",
    "IsolatedRestoreEvidence",
    "IsolatedRestoreFailure",
    "IsolatedPostgreSQLRestorePort",
    "IsolatedRestoreTarget",
    "IsolatedRestoreVerifier",
    "RestoredOriginal",
]
