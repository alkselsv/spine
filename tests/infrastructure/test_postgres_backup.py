from __future__ import annotations

import hashlib
import sys
from types import ModuleType
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.storage.backup_profile import (
    BackupComponentFailure,
    BackupProtectionProfile,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.domain.storage.backup_profile import (
    R1_BACKUP_INCLUSION_POLICY,
    canonical_included_set_digest,
)
from spine.infrastructure.backup_artifacts import FilesystemBackupArtifactStore
from spine.infrastructure.postgres_backup import (
    PostgreSQLBackupConnection,
    PostgreSQLCustomFormatBackupSource,
    PsycopgCanonicalOriginalInventory,
    TenantInventoryScope,
)


def protection() -> BackupProtectionProfile:
    return BackupProtectionProfile(
        source_volume_id="volume:originals",
        destination_id="volume:backups",
        encryption_at_rest_profile="encryption:deployment-bound",
        protected_transfer_profile="transfer:deployment-bound",
        backup_identity="identity:backup",
        restore_identity="identity:restore",
    )


class Inventory:
    def __init__(self, references: tuple[ObjectReference, ...]) -> None:
        self.references = references

    async def finalized_references(self) -> tuple[ObjectReference, ...]:
        return self.references


def tools(tmp_path: Path, *, fail_dump: bool = False) -> tuple[Path, Path]:
    dump = tmp_path / "pg_dump"
    restore = tmp_path / "pg_restore"
    dump.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "if '--version' in sys.argv:\n"
        " print('pg_dump (PostgreSQL) 17.2.1'); raise SystemExit(0)\n"
        + ("print('secret connection details', file=sys.stderr); raise SystemExit(1)\n" if fail_dump else "")
        + "assert '--format=custom' in sys.argv\n"
        "assert '--no-password' in sys.argv\n"
        "target = pathlib.Path(sys.argv[sys.argv.index('--file') + 1])\n"
        "target.write_bytes(b'PGDMP\x01synthetic archive')\n",
        encoding="utf-8",
    )
    restore.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "if '--version' in sys.argv:\n"
        " print('pg_restore (PostgreSQL) 17.2.0'); raise SystemExit(0)\n"
        "archive = pathlib.Path(sys.argv[-1]).read_bytes()\n"
        "assert archive.startswith(b'PGDMP')\n"
        "listing = pathlib.Path(sys.argv[sys.argv.index('--file') + 1])\n"
        "listing.write_text('TABLE DATA source_revision_receipt_associations\\n')\n",
        encoding="utf-8",
    )
    dump.chmod(0o700)
    restore.chmod(0o700)
    return dump, restore


def reference() -> ObjectReference:
    return ObjectReference(
        schema_version=1,
        object_id=UUID("30000000-0000-4000-8000-000000000001"),
        storage_generation=UUID("50000000-0000-4000-8000-000000000001"),
        digest_algorithm="sha256",
        digest_hex="a" * 64,
        byte_length=12,
    )


@pytest.mark.asyncio
async def test_postgres_source_creates_and_independently_lists_custom_archive(
    tmp_path: Path,
) -> None:
    dump, restore = tools(tmp_path)
    protection_profile = protection()
    source = PostgreSQLCustomFormatBackupSource(
        connection=PostgreSQLBackupConnection(
            database="spine",
            username="backup_service",
            backup_role="spine_backup",
            password_file="/run/secrets/pgpass",
        ),
        inventory=Inventory((reference(),)),
        artifact_store=FilesystemBackupArtifactStore(
            tmp_path / "destination", protection=protection_profile
        ),
        pg_dump_executable=dump,
        pg_restore_executable=restore,
    )

    evidence = await source.create_and_verify(
        recovery_set_id="recovery-1",
        deployment_scope_id="deployment-1",
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        inclusion_policy=R1_BACKUP_INCLUSION_POLICY,
        included_set_digest=None,
        protection=protection_profile,
    )

    assert evidence.verified and evidence.durable
    assert evidence.tool_profile == "pg_dump-custom-compatible-restore:v1"
    assert evidence.tool_version == "postgres-17.2.1"
    assert evidence.included_set_digest == canonical_included_set_digest((reference(),))
    assert evidence.artifact_digest == hashlib.sha256(
        b"PGDMP\x01synthetic archive"
    ).hexdigest()
    assert "secret connection details" not in str(evidence.model_dump())


@pytest.mark.asyncio
async def test_postgres_source_redacts_tool_failure_output(tmp_path: Path) -> None:
    dump, restore = tools(tmp_path, fail_dump=True)
    protection_profile = protection()
    source = PostgreSQLCustomFormatBackupSource(
        connection=PostgreSQLBackupConnection(
            database="spine",
            username="backup_service",
            backup_role="spine_backup",
        ),
        inventory=Inventory(()),
        artifact_store=FilesystemBackupArtifactStore(
            tmp_path / "destination", protection=protection_profile
        ),
        pg_dump_executable=dump,
        pg_restore_executable=restore,
    )

    with pytest.raises(BackupComponentFailure) as failure:
        await source.create_and_verify(
            recovery_set_id="recovery-failed",
            deployment_scope_id="deployment-1",
            fence_epoch=1,
            cutoff_ticket_sequence=0,
            inclusion_policy=R1_BACKUP_INCLUSION_POLICY,
            included_set_digest=None,
            protection=protection_profile,
        )

    assert "secret connection details" not in str(failure.value)


def test_postgres_connection_rejects_unprotected_transport_and_embedded_identity() -> None:
    with pytest.raises(ValueError, match="protected transport"):
        PostgreSQLBackupConnection(
            database="spine",
            username="backup_service",
            backup_role="spine_backup",
            sslmode="disable",
        )

    with pytest.raises(ValueError, match="identity reference"):
        PostgreSQLBackupConnection(
            database="postgres://user:secret@db/spine",
            username="backup_service",
            backup_role="spine_backup",
        )


@pytest.mark.asyncio
async def test_canonical_inventory_sets_rls_context_for_each_trusted_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[tuple[str, object]] = []

    class Cursor:
        async def __aenter__(self) -> "Cursor":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, query: str, params: object = None) -> None:
            executed.append((query, params))

        async def fetchall(self) -> list[dict[str, object]]:
            return [{"original_reference": reference().model_dump(mode="json")}]

    class Transaction:
        async def __aenter__(self) -> "Transaction":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Connection:
        def __init__(self) -> None:
            self.transactions = 0

        async def __aenter__(self) -> "Connection":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def transaction(self) -> Transaction:
            self.transactions += 1
            return Transaction()

        def cursor(self) -> Cursor:
            return Cursor()

    connection = Connection()

    class AsyncConnectionFactory:
        @staticmethod
        async def connect(**_kwargs: object) -> Connection:
            return connection

    psycopg_module = ModuleType("psycopg")
    psycopg_module.AsyncConnection = AsyncConnectionFactory  # type: ignore[attr-defined]
    psycopg_rows_module = ModuleType("psycopg.rows")
    psycopg_rows_module.dict_row = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", psycopg_rows_module)

    scopes = (
        TenantInventoryScope(
            UUID("10000000-0000-4000-8000-000000000001"),
            UUID("20000000-0000-4000-8000-000000000001"),
        ),
        TenantInventoryScope(
            UUID("10000000-0000-4000-8000-000000000002"),
            UUID("20000000-0000-4000-8000-000000000002"),
        ),
    )
    inventory = PsycopgCanonicalOriginalInventory(
        PostgreSQLBackupConnection(
            database="spine_test_backup",
            username="backup_service",
            backup_role="spine_backup",
        ),
        tenant_scopes=scopes,
    )

    references = await inventory.finalized_references()

    scope_contexts = [
        params for query, params in executed if "set_config" in query
    ]
    assert scope_contexts == [
        (
            "spine_backup",
            str(scopes[0].workspace_id),
            str(scopes[0].environment_id),
        ),
        (
            "spine_backup",
            str(scopes[1].workspace_id),
            str(scopes[1].environment_id),
        ),
    ]
    assert connection.transactions == 2
    assert references == (reference(),)


def test_canonical_inventory_rejects_empty_or_duplicate_scope_sets() -> None:
    connection = PostgreSQLBackupConnection(
        database="spine_test_backup",
        username="backup_service",
        backup_role="spine_backup",
    )
    with pytest.raises(ValueError, match="unique tenant scopes"):
        PsycopgCanonicalOriginalInventory(connection, tenant_scopes=())
