from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

from spine.domain.connections.google import OpaqueGoogleReference
from spine.infrastructure.google_qualification_credentials import (
    GoogleLocalCredentialStoreError,
    LocalGoogleCredentialStore,
)

CLIENT_CONFIGURATION = json.dumps(
    {"installed": {"client_id": "synthetic-client-id", "client_secret": "synthetic-client-secret"}}
)
REFRESH_TOKEN = "synthetic-refresh-token"


def _reference(value: int) -> OpaqueGoogleReference:
    return OpaqueGoogleReference(
        value=UUID(f"00000000-0000-0000-0000-{value:012d}")
    )


@pytest.mark.asyncio
async def test_local_store_keeps_client_and_refresh_material_in_private_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "google-qualification"
    store = LocalGoogleCredentialStore(root)
    client_reference = _reference(1)
    grant_reference = _reference(2)

    await store.store(
        credential_reference=client_reference,
        grant_reference=grant_reference,
        client_configuration=SecretStr(CLIENT_CONFIGURATION),
        refresh_token=SecretStr(REFRESH_TOKEN),
    )
    client_path = root / f"client-{client_reference.value.hex}.private"
    grant_path = root / f"refresh-{grant_reference.value.hex}.private"

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(client_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(grant_path.stat().st_mode) == 0o600
    assert client_path.read_text() == CLIENT_CONFIGURATION
    assert grant_path.read_text() == REFRESH_TOKEN
    assert (await store.resolve_client(client_reference)).client_secret == "synthetic-client-secret"
    assert (await store.resolve_grant(grant_reference)).refresh_token == REFRESH_TOKEN
    assert REFRESH_TOKEN not in repr(store)


@pytest.mark.asyncio
async def test_local_store_rejects_insecure_directory_or_file_modes(tmp_path: Path) -> None:
    root = tmp_path / "google-qualification"
    store = LocalGoogleCredentialStore(root)
    client_reference = _reference(3)
    grant_reference = _reference(4)
    await store.store(
        credential_reference=client_reference,
        grant_reference=grant_reference,
        client_configuration=SecretStr(CLIENT_CONFIGURATION),
        refresh_token=SecretStr(REFRESH_TOKEN),
    )
    os.chmod(root, 0o750)

    with pytest.raises(GoogleLocalCredentialStoreError):
        await store.resolve_grant(grant_reference)

    os.chmod(root, 0o700)
    os.chmod(root / f"refresh-{grant_reference.value.hex}.private", 0o640)
    with pytest.raises(GoogleLocalCredentialStoreError):
        await store.resolve_grant(grant_reference)


@pytest.mark.asyncio
async def test_local_store_delete_reports_verified_cleanup_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "google-qualification"
    store = LocalGoogleCredentialStore(root)
    client_reference = _reference(5)
    grant_reference = _reference(6)
    await store.store(
        credential_reference=client_reference,
        grant_reference=grant_reference,
        client_configuration=SecretStr(CLIENT_CONFIGURATION),
        refresh_token=SecretStr(REFRESH_TOKEN),
    )

    assert await store.delete(client_reference, grant_reference) is True
    assert await store.delete(client_reference, grant_reference) is True
    assert not (root / f"refresh-{grant_reference.value.hex}.private").exists()
    assert not (root / f"client-{client_reference.value.hex}.private").exists()


def test_local_store_rejects_relative_root() -> None:
    with pytest.raises(GoogleLocalCredentialStoreError):
        LocalGoogleCredentialStore(Path("relative"))
