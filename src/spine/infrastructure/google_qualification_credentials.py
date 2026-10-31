"""Private local POSIX store for offline Google Desktop OAuth materials."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from spine.domain.connections.google import OpaqueGoogleReference


@dataclass(frozen=True, slots=True, repr=False)
class _LocalOAuthClientCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True, slots=True, repr=False)
class _LocalOAuthRefreshGrant:
    refresh_token: str


class GoogleLocalCredentialStoreError(RuntimeError):
    """Sanitized failure for local credential storage operations."""

    def __init__(self) -> None:
        super().__init__("Google local credential storage is unavailable.")


class LocalGoogleCredentialStore:
    """Store separate client and refresh-token files below a private config root."""

    __slots__ = ("_root",)

    def __init__(self, root: Path | None = None) -> None:
        self._root = (
            root if root is not None else Path.home() / ".config" / "spine" / "google-qualification"
        )
        if not self._root.is_absolute():
            raise GoogleLocalCredentialStoreError()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def store(
        self,
        *,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
        client_configuration: SecretStr,
        refresh_token: SecretStr,
    ) -> None:
        await asyncio.to_thread(
            self._store,
            credential_reference,
            grant_reference,
            client_configuration.get_secret_value(),
            refresh_token.get_secret_value(),
        )

    async def resolve_client(
        self, credential_reference: OpaqueGoogleReference
    ) -> _LocalOAuthClientCredentials:
        raw = await asyncio.to_thread(self._read, self._path("client", credential_reference))
        try:
            value = json.loads(raw)
            client_id = value["installed"]["client_id"]
            client_secret = value["installed"]["client_secret"]
            if not isinstance(client_id, str) or not isinstance(client_secret, str):
                raise ValueError
            return _LocalOAuthClientCredentials(client_id=client_id, client_secret=client_secret)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise GoogleLocalCredentialStoreError() from None

    async def resolve_grant(
        self, grant_reference: OpaqueGoogleReference
    ) -> _LocalOAuthRefreshGrant:
        raw = await asyncio.to_thread(self._read, self._path("refresh", grant_reference))
        if not raw:
            raise GoogleLocalCredentialStoreError()
        return _LocalOAuthRefreshGrant(refresh_token=raw)

    async def read_refresh_token(self, reference: OpaqueGoogleReference) -> SecretStr:
        raw = await asyncio.to_thread(self._read, self._path("refresh", reference))
        if not raw:
            raise GoogleLocalCredentialStoreError()
        return SecretStr(raw)

    async def delete(
        self,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
    ) -> bool:
        return await asyncio.to_thread(
            self._delete_pair,
            self._path("client", credential_reference),
            self._path("refresh", grant_reference),
        )

    def _store(
        self,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
        client_configuration: str,
        refresh_token: str,
    ) -> None:
        try:
            parsed = json.loads(client_configuration)
            installed = parsed["installed"]
            if not isinstance(installed.get("client_id"), str) or not isinstance(
                installed.get("client_secret"), str
            ):
                raise ValueError
            self._ensure_root()
            client_path = self._path("client", credential_reference)
            grant_path = self._path("refresh", grant_reference)
            created: list[Path] = []
            try:
                self._atomic_write(client_path, client_configuration.encode("utf-8"))
                created.append(client_path)
                self._atomic_write(grant_path, refresh_token.encode("utf-8"))
                created.append(grant_path)
            except GoogleLocalCredentialStoreError:
                for path in created:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
        except GoogleLocalCredentialStoreError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError, KeyError):
            raise GoogleLocalCredentialStoreError() from None

    def _path(self, kind: str, reference: OpaqueGoogleReference) -> Path:
        return self._root / f"{kind}-{reference.value.hex}.private"

    def _ensure_root(self) -> None:
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self._root.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise GoogleLocalCredentialStoreError()
            self._verify_no_git_or_sync_root()
        except GoogleLocalCredentialStoreError:
            raise
        except OSError:
            raise GoogleLocalCredentialStoreError() from None

    def _verify_no_git_or_sync_root(self) -> None:
        # Refuse common synced roots and any root inside a Git worktree.
        resolved = self._root.resolve(strict=True)
        forbidden = ("/Dropbox/", "/Google Drive/", "/OneDrive/", "/iCloud Drive/")
        normalized = f"{resolved}/"
        if any(part in normalized for part in forbidden) or (resolved / ".git").exists():
            raise GoogleLocalCredentialStoreError()
        for parent in resolved.parents:
            if (parent / ".git").exists():
                raise GoogleLocalCredentialStoreError()

    def _atomic_write(self, destination: Path, payload: bytes) -> None:
        temporary = self._root / f".pending-{secrets.token_hex(16)}"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination, follow_symlinks=False)
            temporary.unlink()
            os.chmod(destination, 0o600, follow_symlinks=False)
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise GoogleLocalCredentialStoreError() from None

    def _read(self, path: Path) -> str:
        descriptor: int | None = None
        try:
            self._ensure_root()
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 64 * 1024
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise GoogleLocalCredentialStoreError()
            content = os.read(descriptor, 64 * 1024 + 1)
            if len(content) > 64 * 1024:
                raise GoogleLocalCredentialStoreError()
            return content.decode("utf-8")
        except GoogleLocalCredentialStoreError:
            raise
        except (OSError, UnicodeError):
            raise GoogleLocalCredentialStoreError() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _delete(self, path: Path) -> bool:
        try:
            self._ensure_root()
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                return False
            path.unlink()
            return not path.exists()
        except FileNotFoundError:
            return True
        except (OSError, GoogleLocalCredentialStoreError):
            return False

    def _delete_pair(self, client_path: Path, grant_path: Path) -> bool:
        client_removed = self._delete(client_path)
        if not client_removed:
            return False
        grant_removed = self._delete(grant_path)
        return grant_removed


__all__ = ["GoogleLocalCredentialStoreError", "LocalGoogleCredentialStore"]
