"""Profiled local-filesystem implementation of the original-object port.

The adapter deliberately supports one deployment shape: a single process (or a
set of processes sharing one local POSIX volume) with staged and finalized
namespaces on the same filesystem.  Physical names are generated identities,
never client metadata.  The adapter does not make claims about power-loss or
filesystem durability beyond the profile documented in
``docs/storage-filesystem-profile.md``.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import stat
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from .contracts import (
    AuthorizedReadGrantRegistry,
    ConsumedOriginalReadLease,
    DeletionApproval,
    DeletionResult,
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    ReconciliationInventoryEntry,
    UploadCommandState,
    UploadCommandStateName,
    WriteReceipt,
)
from .errors import (
    IntegrityMismatch,
    InvalidObjectReference,
    InvalidReadGrant,
    ObjectAlreadyExists,
    ObjectCorrupt,
    ObjectDeletionFailed,
    ObjectNotFound,
    ObjectProtected,
    ObjectUnavailable,
    ReconciliationRequired,
    StorageError,
    StorageConfigurationError,
    StorageIntegrityIndeterminate,
    UploadConflict,
    UploadLimitExceeded,
    UploadNotFound,
)


_MANIFEST_VERSION = 1
_SCOPE_HEX_LENGTH = 64
_OPAQUE_NAME_LENGTH = 32
_JSON_ENCODING = "utf-8"
_DEFAULT_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_MAX_CHUNK_BYTES = 1024 * 1024
_DEFAULT_MAX_OBJECT_BYTES = 256 * 1024 * 1024
_DEFAULT_MAX_CONCURRENT_OPERATIONS = 8
_DEFAULT_INACTIVITY_TIMEOUT = 30.0
_DEFAULT_OPERATION_TIMEOUT = 15 * 60.0
_DEFAULT_MAX_INVENTORY_ENTRIES = 10_000

_IoOperation = Literal["read", "verify"]
_IoObserver = Callable[[_IoOperation, int, int], None]


@dataclass(frozen=True, slots=True)
class FilesystemStorageProfile:
    """Resource limits and root for the supported local-volume profile."""

    root: str | os.PathLike[str]
    max_chunk_bytes: int = _DEFAULT_MAX_CHUNK_BYTES
    max_object_bytes: int = _DEFAULT_MAX_OBJECT_BYTES
    read_chunk_bytes: int = _DEFAULT_READ_CHUNK_BYTES
    max_concurrent_operations: int = _DEFAULT_MAX_CONCURRENT_OPERATIONS
    inactivity_timeout: float = _DEFAULT_INACTIVITY_TIMEOUT
    operation_timeout: float = _DEFAULT_OPERATION_TIMEOUT
    max_inventory_entries: int = _DEFAULT_MAX_INVENTORY_ENTRIES

    def __post_init__(self) -> None:
        if not isinstance(self.root, (str, os.PathLike)) or not os.fspath(self.root):
            raise ValueError("filesystem storage root is required")
        if self.max_chunk_bytes <= 0:
            raise ValueError("max_chunk_bytes must be positive")
        if self.max_object_bytes < 0:
            raise ValueError("max_object_bytes must be non-negative")
        if self.max_chunk_bytes > self.max_object_bytes and self.max_object_bytes:
            raise ValueError("max_chunk_bytes cannot exceed max_object_bytes")
        if self.read_chunk_bytes <= 0:
            raise ValueError("read_chunk_bytes must be positive")
        if self.max_concurrent_operations <= 0:
            raise ValueError("max_concurrent_operations must be positive")
        if self.inactivity_timeout <= 0 or self.operation_timeout <= 0:
            raise ValueError("filesystem timeouts must be positive")
        if self.max_inventory_entries <= 0:
            raise ValueError("max_inventory_entries must be positive")


class FilesystemOriginalObjectStore:
    """Immutable original-object adapter for the profiled local filesystem."""

    def __init__(
        self,
        profile: FilesystemStorageProfile,
        *,
        grant_registry: AuthorizedReadGrantRegistry,
        generation_factory: Callable[[], UUID] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        io_observer: _IoObserver | None = None,
    ) -> None:
        self._profile = profile
        self._grant_registry = grant_registry
        self._generation_factory = generation_factory or uuid4
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._io_observer = io_observer
        self._operation_limit = asyncio.Semaphore(profile.max_concurrent_operations)
        self._upload_locks: dict[UUID, asyncio.Lock] = {}
        self._deletion_approvals: dict[ObjectReference, DeletionApproval] = {}
        self._deleted_references: dict[UUID, ObjectReference] = {}
        self._known_scope_keys: set[str] = set()
        self._root = self._prepare_root(profile.root)
        self._staged_root = self._prepare_directory(self._root / "staged")
        self._finalized_root = self._prepare_directory(self._root / "finalized")
        self._deleted_root = self._prepare_directory(self._root / "deleted")

    async def begin_or_resume_upload(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
    ) -> UploadCommandState:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        self._validate_upload_state(state)
        scope_key = self._scope_key(command.workspace_id, command.environment_id)
        async with self._operation_limit:
            scope_dir = self._ensure_scope_directory(self._staged_root, scope_key)
            final_scope_dir = self._ensure_scope_directory(self._finalized_root, scope_key)
            final_manifest = final_scope_dir / self._manifest_name(state.object_id)
            final_payload = final_scope_dir / state.object_id.hex
            if self._exists(final_manifest):
                final_data = self._read_manifest(final_manifest)
                same_upload = final_data.get("upload_id") == str(state.upload_id)
                if same_upload:
                    return self._state_for_finalized_upload(
                        command,
                        state,
                        final_payload,
                        final_manifest,
                    )

            stage_dir = scope_dir / state.upload_id.hex
            manifest_path = stage_dir / "manifest.json"
            if self._exists(manifest_path):
                manifest = self._read_manifest(manifest_path)
                self._assert_manifest_identity(manifest, command, state)
                status = manifest.get("status")
                if status == "aborted":
                    return state.transitioned(UploadCommandStateName.ABORTED)
                if status == "integrity_conflict":
                    return state.transitioned(UploadCommandStateName.INTEGRITY_CONFLICT)
                if status == "publishing":
                    raise StorageIntegrityIndeterminate()
                if state.state not in {
                    UploadCommandStateName.STAGING,
                    UploadCommandStateName.INTERRUPTED,
                }:
                    raise UploadNotFound()
                if status == "interrupted":
                    self._set_manifest_status(manifest_path, manifest, "staging")
                    return state.transitioned(UploadCommandStateName.STAGING)
                return state

            if state.state is not UploadCommandStateName.STAGING:
                raise UploadNotFound()
            existing_location = self._locate_upload(state.upload_id)
            if existing_location is not None:
                existing_scope = existing_location[3].get("scope_key")
                if existing_scope != scope_key:
                    raise UploadNotFound()
            try:
                self._mkdir_exclusive(stage_dir)
                manifest = self._new_stage_manifest(command, state)
                self._write_json_exclusive(manifest_path, manifest)
                self._create_empty_payload(stage_dir / "payload")
            except FileExistsError:
                if not self._exists(manifest_path):
                    raise StorageIntegrityIndeterminate() from None
                manifest = self._read_manifest(manifest_path)
                self._assert_manifest_identity(manifest, command, state)
            except StorageError:
                raise
            except OSError as error:
                raise self._configuration_or_unavailable(error) from None
            return state

    async def write_chunks(
        self,
        upload_id: UUID,
        chunks: AsyncIterable[bytes],
    ) -> ObservedContentIdentity:
        self._validate_uuid(upload_id)
        lock = self._upload_locks.setdefault(upload_id, asyncio.Lock())
        async with self._operation_limit, lock:
            location = self._locate_upload(upload_id)
            deadline = self._deadline()
            if location is None:
                raise UploadNotFound()
            kind, payload_path, manifest_path, manifest = location
            if kind == "finalized":
                receipt = self._receipt_from_manifest(manifest)
                return await self._observe_input_stream(
                    chunks,
                    deadline=deadline,
                    expected_length=receipt.object_reference.byte_length,
                )
            status = manifest.get("status")
            if status in {"aborted", "integrity_conflict"}:
                raise UploadConflict()
            if status == "interrupted":
                raise UploadConflict()
            if status != "staging":
                raise StorageIntegrityIndeterminate()
            try:
                observed = await self._append_stream(
                    payload_path,
                    chunks,
                    deadline=deadline,
                    manifest=manifest,
                )
                updated_manifest = dict(manifest)
                updated_manifest["status"] = "staging"
                updated_manifest["observed_digest"] = observed.observed_sha256
                updated_manifest["observed_length"] = observed.observed_byte_length
                self._write_json_atomic(manifest_path, updated_manifest)
                return observed
            except StorageError as error:
                self._mark_interrupted(manifest_path, manifest)
                raise error
            except asyncio.CancelledError:
                self._mark_interrupted(manifest_path, manifest)
                raise
            except Exception:
                self._mark_interrupted(manifest_path, manifest)
                raise ObjectUnavailable() from None

    async def abort_upload(self, state: UploadCommandState) -> UploadCommandState:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        self._validate_upload_state(state)
        async with self._operation_limit:
            location = self._locate_upload(state.upload_id)
            if location is None:
                raise UploadNotFound()
            kind, payload_path, manifest_path, manifest = location
            if kind == "finalized":
                raise ObjectProtected()
            self._assert_manifest_identity_from_state(manifest, state)
            status = manifest.get("status")
            if status == "aborted":
                return state.transitioned(UploadCommandStateName.ABORTED)
            if status == "integrity_conflict":
                raise ObjectProtected()
            if status not in {"staging", "interrupted"}:
                raise StorageIntegrityIndeterminate()
            try:
                if self._exists(payload_path):
                    self._unlink_nofollow(payload_path)
                self._set_manifest_status(manifest_path, manifest, "aborted")
            except StorageError:
                raise
            except OSError as error:
                raise ObjectUnavailable() from None
            return state.transitioned(UploadCommandStateName.ABORTED)

    async def finalize_upload(
        self,
        state: UploadCommandState,
        observed_content: ObservedContentIdentity,
    ) -> WriteReceipt:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        self._validate_upload_state(state)
        lock = self._upload_locks.setdefault(state.upload_id, asyncio.Lock())
        async with self._operation_limit, lock:
            scope_key = self._scope_key_from_state_scope(state)
            final_scope_dir = self._ensure_scope_directory(self._finalized_root, scope_key)
            final_payload = final_scope_dir / state.object_id.hex
            final_manifest = final_scope_dir / self._manifest_name(state.object_id)
            if self._exists(final_manifest):
                return self._replay_finalization(
                    state,
                    observed_content,
                    final_payload,
                    final_manifest,
                )

            location = self._locate_upload(state.upload_id)
            if location is None:
                raise UploadNotFound()
            kind, stage_payload, stage_manifest_path, manifest = location
            if kind == "finalized":
                return self._replay_finalization(
                    state,
                    observed_content,
                    stage_payload,
                    stage_manifest_path,
                )
            self._assert_manifest_identity_from_state(manifest, state)
            status = manifest.get("status")
            if status == "aborted":
                raise ObjectProtected()
            if status == "integrity_conflict":
                raise IntegrityMismatch()
            if status != "staging":
                raise UploadConflict()

            deadline = self._deadline()
            actual = self._hash_file(
                stage_payload,
                self._profile.max_object_bytes,
                deadline,
                None,
            )
            recorded_digest = manifest.get("observed_digest")
            recorded_length = manifest.get("observed_length")
            if (
                recorded_digest is not None
                and recorded_length is not None
                and (
                    recorded_digest != actual.observed_sha256
                    or recorded_length != actual.observed_byte_length
                )
            ):
                raise ObjectCorrupt()
            if actual != observed_content:
                raise UploadConflict()
            expected_digest = manifest.get("expected_digest")
            expected_length = manifest.get("expected_length")
            if (expected_digest is not None and expected_digest != actual.observed_sha256) or (
                expected_length is not None and expected_length != actual.observed_byte_length
            ):
                self._set_manifest_status(
                    stage_manifest_path,
                    manifest,
                    "integrity_conflict",
                )
                raise IntegrityMismatch()

            generation = self._new_generation()
            receipt = self._make_receipt(
                state,
                manifest,
                generation,
                actual,
            )
            publishing_manifest = dict(manifest)
            publishing_manifest["status"] = "publishing"
            publishing_manifest["receipt"] = receipt.model_dump(mode="json")
            finalized_manifest = dict(publishing_manifest)
            finalized_manifest["status"] = "finalized"
            self._write_json_atomic(stage_manifest_path, publishing_manifest)
            try:
                self._publish_no_replace(stage_payload, final_payload)
                self._sync_file(final_payload)
                self._write_json_exclusive(final_manifest, finalized_manifest)
                self._sync_directory(final_scope_dir)
            except FileExistsError:
                if not self._exists(final_manifest):
                    raise StorageIntegrityIndeterminate() from None
                return self._replay_finalization(
                    state,
                    observed_content,
                    final_payload,
                    final_manifest,
                )
            except StorageError:
                raise
            except OSError as error:
                raise self._publication_error(error) from None
            return receipt

    def open_bounded_read(
        self,
        lease: ConsumedOriginalReadLease,
        *,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        return self._bounded_read(lease, max_bytes=max_bytes)

    async def _bounded_read(
        self,
        lease: ConsumedOriginalReadLease,
        *,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        if max_bytes <= 0:
            raise UploadLimitExceeded()
        async with self._operation_limit:
            try:
                binding = self._grant_registry.consume_read_lease(
                    lease,
                    now=self._clock(),
                )
            except StorageError:
                raise
            except Exception:
                raise InvalidReadGrant() from None
            reference = binding.object_reference
            self._validate_reference(reference)
            scope_key = self._scope_key(binding.workspace_id, binding.environment_id)
            final_scope_dir = self._finalized_root / scope_key
            if self._exists(final_scope_dir) and final_scope_dir.is_symlink():
                raise StorageIntegrityIndeterminate()
            payload_path = final_scope_dir / reference.object_id.hex
            manifest_path = final_scope_dir / self._manifest_name(reference.object_id)
            if not self._exists(manifest_path):
                if scope_key not in self._known_scope_keys:
                    raise InvalidReadGrant()
                raise ObjectNotFound()
            manifest = self._read_manifest(manifest_path)
            receipt = self._receipt_from_manifest(manifest)
            if receipt.object_reference != reference:
                raise InvalidReadGrant()
            if not self._exists(payload_path):
                raise ObjectNotFound()
            limit = min(max_bytes, self._profile.max_object_bytes)
            deadline = self._deadline()
            try:
                size = self._regular_file_size(payload_path)
                if size != reference.byte_length:
                    raise ObjectCorrupt()
                if limit >= reference.byte_length:
                    actual = self._hash_file(
                        payload_path,
                        limit,
                        deadline,
                        "read",
                    )
                    if actual != receipt.observed_content:
                        raise ObjectCorrupt()
                async for part in self._read_file(
                    payload_path,
                    limit=limit,
                    deadline=deadline,
                ):
                    yield part
            except StorageError:
                raise
            except OSError as error:
                raise self._read_error(error) from None

    async def verify(
        self,
        reference: ObjectReference,
        *,
        max_bytes: int,
    ) -> IntegrityResult:
        self._validate_reference(reference)
        async with self._operation_limit:
            location = self._locate_finalized_reference(reference)
            if location is None:
                raise ObjectNotFound()
            payload_path, manifest_path = location
            manifest = self._read_manifest(manifest_path)
            receipt = self._receipt_from_manifest(manifest)
            if receipt.object_reference != reference:
                raise InvalidObjectReference()
            if max_bytes < reference.byte_length:
                self._observe_io("verify", max_bytes, 0)
                raise UploadLimitExceeded()
            try:
                actual = self._hash_file(
                    payload_path,
                    max_bytes,
                    self._deadline(),
                    "verify",
                )
            except StorageError:
                raise
            except OSError as error:
                raise self._read_error(error) from None
            if actual != receipt.observed_content:
                raise ObjectCorrupt()
            return IntegrityResult(
                status=IntegrityStatus.VERIFIED,
                algorithm="sha256",
                expected_digest=reference.digest_hex,
                observed_digest=actual.observed_sha256,
                expected_length=reference.byte_length,
                observed_length=actual.observed_byte_length,
            )

    async def reconciliation_inventory(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[tuple[ReconciliationInventoryEntry, ...], str | None]:
        if limit <= 0 or limit > self._profile.max_inventory_entries:
            raise UploadLimitExceeded()
        start = self._inventory_cursor(cursor)
        async with self._operation_limit:
            entries: list[ReconciliationInventoryEntry] = []
            self._validate_staged_namespace()
            for manifest_path in self._iter_final_manifests():
                if len(entries) >= self._profile.max_inventory_entries:
                    raise UploadLimitExceeded()
                manifest = self._read_manifest(manifest_path)
                receipt = self._receipt_from_manifest(manifest)
                entries.append(
                    ReconciliationInventoryEntry(
                        object_reference=receipt.object_reference,
                        upload_id=receipt.upload_id,
                        finalized=True,
                        observed_at=self._clock(),
                    )
                )
            entries.sort(key=lambda entry: entry.object_reference.object_id.int)
            page = entries[start : start + limit]
            next_cursor = str(start + limit) if start + limit < len(entries) else None
            return tuple(page), next_cursor

    async def prepare_deletion(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> None:
        self._validate_reference(reference)
        async with self._operation_limit:
            if self._is_deleted_reference(reference):
                self._deletion_approvals[reference] = approval
                return
            location = self._locate_finalized_reference(reference)
            if location is None:
                raise ObjectNotFound()
            self._deletion_approvals[reference] = approval

    async def delete_controlled(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> DeletionResult:
        self._validate_reference(reference)
        async with self._operation_limit:
            if self._deletion_approvals.get(reference) != approval:
                raise ObjectProtected()
            deleted = self._deleted_references.get(reference.object_id)
            if deleted == reference:
                return DeletionResult(status="already_absent")
            location = self._locate_finalized_reference(reference)
            if location is None:
                raise ObjectNotFound()
            payload_path, manifest_path = location
            scope_dir = manifest_path.parent
            try:
                self._unlink_nofollow(payload_path)
                self._unlink_nofollow(manifest_path)
                self._sync_directory(scope_dir)
                self._write_deleted_marker(reference)
            except StorageError:
                raise
            except OSError:
                raise ObjectDeletionFailed() from None
            self._deleted_references[reference.object_id] = reference
            return DeletionResult(status="deleted")

    def _is_deleted_reference(self, reference: ObjectReference) -> bool:
        if self._deleted_references.get(reference.object_id) == reference:
            return True
        marker = self._deleted_root / f"{reference.object_id.hex}.json"
        if not self._exists(marker):
            return False
        try:
            fd = self._open_read(marker)
            try:
                value = json.loads(os.read(fd, 128 * 1024).decode(_JSON_ENCODING))
            finally:
                os.close(fd)
            restored = ObjectReference.model_validate_json(
                json.dumps(value["reference"], separators=(",", ":"))
            )
        except StorageError:
            raise
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        if restored != reference:
            raise StorageIntegrityIndeterminate()
        self._deleted_references[reference.object_id] = restored
        return True

    def _state_for_finalized_upload(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
        payload_path: Path,
        manifest_path: Path,
    ) -> UploadCommandState:
        manifest = self._read_manifest(manifest_path)
        self._assert_manifest_identity(manifest, command, state)
        receipt = self._receipt_from_manifest(manifest)
        self._verify_finalized_file(payload_path, receipt)
        if state.state is UploadCommandStateName.FINALIZED:
            if state.provisional_receipt != receipt:
                raise UploadConflict()
            return state
        return state.transitioned(
            UploadCommandStateName.FINALIZED,
            provisional_receipt=receipt,
        )

    def _replay_finalization(
        self,
        state: UploadCommandState,
        observed_content: ObservedContentIdentity,
        payload_path: Path,
        manifest_path: Path,
    ) -> WriteReceipt:
        manifest = self._read_manifest(manifest_path)
        self._assert_manifest_identity_from_state(manifest, state)
        receipt = self._receipt_from_manifest(manifest)
        if receipt.observed_content != observed_content:
            raise UploadConflict()
        self._verify_finalized_file(payload_path, receipt)
        return receipt

    def _verify_finalized_file(self, payload_path: Path, receipt: WriteReceipt) -> None:
        try:
            actual = self._hash_file(
                payload_path,
                self._profile.max_object_bytes,
                self._deadline(),
                None,
            )
        except StorageError:
            raise
        except OSError as error:
            raise self._read_error(error) from None
        if actual != receipt.observed_content:
            raise ObjectCorrupt()

    async def _observe_input_stream(
        self,
        chunks: AsyncIterable[bytes],
        *,
        deadline: float,
        expected_length: int,
    ) -> ObservedContentIdentity:
        digest = hashlib.sha256()
        length = 0
        iterator = chunks.__aiter__()
        while True:
            remaining = self._remaining_timeout(deadline)
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=min(self._profile.inactivity_timeout, remaining),
                )
            except StopAsyncIteration:
                break
            except (asyncio.TimeoutError, TimeoutError):
                raise UploadLimitExceeded() from None
            self._validate_chunk(chunk, length)
            digest.update(chunk)
            length += len(chunk)
        del expected_length
        return ObservedContentIdentity(
            observed_sha256=digest.hexdigest(),
            observed_byte_length=length,
        )

    async def _append_stream(
        self,
        payload_path: Path,
        chunks: AsyncIterable[bytes],
        *,
        deadline: float,
        manifest: dict[str, Any],
    ) -> ObservedContentIdentity:
        existing = self._hash_file(
            payload_path,
            self._profile.max_object_bytes,
            deadline,
            None,
        )
        recorded_digest = manifest.get("observed_digest")
        recorded_length = manifest.get("observed_length")
        if (
            recorded_digest is not None
            and recorded_length is not None
            and (
                recorded_digest != existing.observed_sha256
                or recorded_length != existing.observed_byte_length
            )
        ):
            raise ObjectCorrupt()
        digest = hashlib.sha256()
        length = 0
        fd = self._open_append(payload_path)
        try:
            digest = hashlib.sha256()
            length = existing.observed_byte_length
            # Re-hash the existing prefix without retaining it in memory.
            self._update_digest_from_path(payload_path, digest, deadline)
            iterator = chunks.__aiter__()
            while True:
                remaining = self._remaining_timeout(deadline)
                try:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=min(self._profile.inactivity_timeout, remaining),
                    )
                except StopAsyncIteration:
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    raise UploadLimitExceeded() from None
                self._validate_chunk(chunk, length)
                self._write_chunk(fd, chunk, deadline)
                digest.update(chunk)
                length += len(chunk)
        finally:
            os.close(fd)
        return ObservedContentIdentity(
            observed_sha256=digest.hexdigest(),
            observed_byte_length=length,
        )

    def _locate_upload(
        self,
        upload_id: UUID,
    ) -> tuple[Literal["staged", "finalized"], Path, Path, dict[str, Any]] | None:
        staged: list[tuple[Path, Path, dict[str, Any]]] = []
        finalized: list[tuple[Path, Path, dict[str, Any]]] = []
        for scope_dir in self._iter_scope_directories(self._staged_root):
            candidate = scope_dir / upload_id.hex
            if self._exists(candidate) and (
                candidate.is_symlink() or not candidate.is_dir()
            ):
                raise StorageIntegrityIndeterminate()
            manifest_path = candidate / "manifest.json"
            if self._exists(manifest_path):
                manifest = self._read_manifest(manifest_path)
                if manifest.get("upload_id") == str(upload_id):
                    staged.append((candidate / "payload", manifest_path, manifest))
        for manifest_path in self._iter_final_manifests():
            manifest = self._read_manifest(manifest_path)
            if manifest.get("upload_id") == str(upload_id):
                finalized.append((manifest_path.parent / self._payload_name_from_manifest(manifest), manifest_path, manifest))
        if len(finalized) > 1 or (not finalized and len(staged) > 1):
            raise StorageIntegrityIndeterminate()
        if finalized:
            payload, manifest_path, manifest = finalized[0]
            return "finalized", payload, manifest_path, manifest
        if staged:
            payload, manifest_path, manifest = staged[0]
            return "staged", payload, manifest_path, manifest
        return None

    def _locate_finalized_reference(
        self,
        reference: ObjectReference,
    ) -> tuple[Path, Path] | None:
        matches: list[tuple[Path, Path]] = []
        for manifest_path in self._iter_final_manifests():
            manifest = self._read_manifest(manifest_path)
            receipt = self._receipt_from_manifest(manifest)
            if receipt.object_reference.object_id == reference.object_id:
                matches.append(
                    (
                        manifest_path.parent / self._payload_name_from_manifest(manifest),
                        manifest_path,
                    )
                )
        if len(matches) > 1:
            raise StorageIntegrityIndeterminate()
        return matches[0] if matches else None

    def _iter_final_manifests(self) -> Iterator[Path]:
        for scope_dir in self._iter_scope_directories(self._finalized_root):
            entries = self._scandir(scope_dir)
            manifest_names = {
                entry.name for entry in entries if entry.name.endswith(".manifest.json")
            }
            for entry in entries:
                if entry.is_symlink():
                    raise StorageIntegrityIndeterminate()
                if not entry.is_file(follow_symlinks=False):
                    raise StorageIntegrityIndeterminate()
                if entry.name.endswith(".manifest.json"):
                    yield Path(entry.path)
                elif (
                    len(entry.name) == _OPAQUE_NAME_LENGTH
                    and all(character in "0123456789abcdef" for character in entry.name)
                    and f"{entry.name}.manifest.json" in manifest_names
                ):
                    continue
                else:
                    raise StorageIntegrityIndeterminate()

    def _validate_staged_namespace(self) -> None:
        for scope_dir in self._iter_scope_directories(self._staged_root):
            for entry in self._scandir(scope_dir):
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    raise StorageIntegrityIndeterminate()
                stage_dir = Path(entry.path)
                for child in self._scandir(stage_dir):
                    if child.is_symlink() or not child.is_file(follow_symlinks=False):
                        raise StorageIntegrityIndeterminate()
                    if child.name not in {"payload", "manifest.json"}:
                        raise StorageIntegrityIndeterminate()

    def _iter_scope_directories(self, root: Path) -> Iterator[Path]:
        for entry in self._scandir(root):
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise StorageIntegrityIndeterminate()
            if len(entry.name) != _SCOPE_HEX_LENGTH or any(
                character not in "0123456789abcdef" for character in entry.name
            ):
                raise StorageIntegrityIndeterminate()
            self._known_scope_keys.add(entry.name)
            yield Path(entry.path)

    def _scandir(self, path: Path) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(path) as entries:
                return list(entries)
        except FileNotFoundError:
            return []
        except OSError as error:
            raise self._configuration_or_unavailable(error) from None

    def _new_stage_manifest(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
    ) -> dict[str, Any]:
        return {
            "version": _MANIFEST_VERSION,
            "status": "staging",
            "scope_key": self._scope_key(command.workspace_id, command.environment_id),
            "upload_id": str(state.upload_id),
            "object_id": str(state.object_id),
            "issue8_receipt_id": str(state.issue8_receipt_id),
            "request_digest": state.request_digest.model_dump(mode="json"),
            "command_fingerprint": self._command_fingerprint(command),
            "expected_digest": command.expected_sha256,
            "expected_length": command.expected_byte_length,
        }

    def _make_receipt(
        self,
        state: UploadCommandState,
        manifest: dict[str, Any],
        generation: UUID,
        observed: ObservedContentIdentity,
    ) -> WriteReceipt:
        now = self._clock()
        if now.tzinfo is None:
            raise StorageIntegrityIndeterminate()
        reference = ObjectReference(
            schema_version=1,
            object_id=state.object_id,
            storage_generation=generation,
            digest_algorithm="sha256",
            digest_hex=observed.observed_sha256,
            byte_length=observed.observed_byte_length,
        )
        return WriteReceipt(
            schema_version=1,
            upload_id=state.upload_id,
            object_reference=reference,
            observed_content=observed,
            verification=IntegrityResult(
                status=IntegrityStatus.VERIFIED,
                algorithm="sha256",
                expected_digest=manifest.get("expected_digest"),
                observed_digest=observed.observed_sha256,
                expected_length=manifest.get("expected_length"),
                observed_length=observed.observed_byte_length,
            ),
            storage_status="finalized_verified",
            finalized_at=now,
        )

    def _assert_manifest_identity(
        self,
        manifest: dict[str, Any],
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
    ) -> None:
        self._assert_manifest_identity_from_state(manifest, state)
        if manifest.get("scope_key") != self._scope_key(command.workspace_id, command.environment_id):
            raise UploadNotFound()
        if manifest.get("command_fingerprint") != self._command_fingerprint(command):
            raise UploadConflict()

    @staticmethod
    def _assert_manifest_identity_from_state(
        manifest: dict[str, Any],
        state: UploadCommandState,
    ) -> None:
        if (
            manifest.get("upload_id") != str(state.upload_id)
            or manifest.get("object_id") != str(state.object_id)
            or manifest.get("issue8_receipt_id") != str(state.issue8_receipt_id)
            or manifest.get("request_digest") != state.request_digest.model_dump(mode="json")
        ):
            raise UploadConflict()

    def _set_manifest_status(
        self,
        path: Path,
        manifest: dict[str, Any],
        status: str,
    ) -> None:
        updated = dict(manifest)
        updated["status"] = status
        self._write_json_atomic(path, updated)

    def _mark_interrupted(self, path: Path, manifest: dict[str, Any]) -> None:
        try:
            self._set_manifest_status(path, manifest, "interrupted")
        except StorageError:
            raise StorageIntegrityIndeterminate() from None
        except OSError:
            raise StorageIntegrityIndeterminate() from None

    def _receipt_from_manifest(self, manifest: dict[str, Any]) -> WriteReceipt:
        try:
            receipt = WriteReceipt.model_validate_json(
                json.dumps(manifest["receipt"], separators=(",", ":"))
            )
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        if manifest.get("status") not in {"finalized", "publishing"}:
            raise StorageIntegrityIndeterminate()
        return receipt

    def _publish_no_replace(self, source: Path, destination: Path) -> None:
        if self._exists(destination):
            raise FileExistsError()
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            raise
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise FileExistsError() from None
            raise

    def _write_deleted_marker(self, reference: ObjectReference) -> None:
        marker = self._deleted_root / f"{reference.object_id.hex}.json"
        self._write_json_atomic(marker, {"version": 1, "reference": reference.model_dump(mode="json")})
        self._sync_directory(self._deleted_root)

    def _update_digest_from_path(self, path: Path, digest: Any, deadline: float) -> None:
        fd = self._open_read(path)
        try:
            while True:
                if self._monotonic_clock() > deadline:
                    raise UploadLimitExceeded()
                chunk = os.read(
                    fd,
                    min(self._profile.read_chunk_bytes, self._profile.max_chunk_bytes),
                )
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(fd)

    def _hash_file(
        self,
        path: Path,
        max_bytes: int,
        deadline: float,
        operation: _IoOperation | None,
    ) -> ObservedContentIdentity:
        if max_bytes < 0:
            raise UploadLimitExceeded()
        fd = self._open_read(path)
        digest = hashlib.sha256()
        length = 0
        try:
            while True:
                if self._monotonic_clock() > deadline:
                    raise UploadLimitExceeded()
                chunk = os.read(fd, min(self._profile.read_chunk_bytes, self._profile.max_chunk_bytes))
                if not chunk:
                    break
                length += len(chunk)
                if length > max_bytes:
                    self._observe_io(operation, max_bytes, length - len(chunk))
                    raise UploadLimitExceeded()
                digest.update(chunk)
        finally:
            os.close(fd)
        self._observe_io(operation, max_bytes, length)
        return ObservedContentIdentity(
            observed_sha256=digest.hexdigest(),
            observed_byte_length=length,
        )

    async def _read_file(
        self,
        path: Path,
        *,
        limit: int,
        deadline: float,
    ) -> AsyncIterator[bytes]:
        fd = self._open_read(path)
        remaining = limit
        try:
            while remaining:
                if self._monotonic_clock() > deadline:
                    raise UploadLimitExceeded()
                chunk = os.read(fd, min(self._profile.read_chunk_bytes, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self._observe_io("read", limit, limit - remaining)
                yield chunk
        finally:
            os.close(fd)

    def _validate_chunk(self, chunk: bytes, current_length: int) -> None:
        if not isinstance(chunk, bytes):
            raise UploadLimitExceeded()
        if len(chunk) > self._profile.max_chunk_bytes:
            raise UploadLimitExceeded()
        if current_length + len(chunk) > self._profile.max_object_bytes:
            raise UploadLimitExceeded()

    def _open_append(self, path: Path) -> int:
        try:
            return os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise self._configuration_or_unavailable(error) from None

    def _write_chunk(self, fd: int, chunk: bytes, deadline: float) -> None:
        view = memoryview(chunk)
        while view:
            if self._monotonic_clock() > deadline:
                raise UploadLimitExceeded()
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)

    def _open_read(self, path: Path) -> int:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise StorageIntegrityIndeterminate()
            return fd
        except StorageError:
            raise
        except OSError as error:
            raise self._read_error(error) from None

    def _regular_file_size(self, path: Path) -> int:
        fd = self._open_read(path)
        try:
            return os.fstat(fd).st_size
        finally:
            os.close(fd)

    def _sync_file(self, path: Path) -> None:
        fd = self._open_read(path)
        try:
            os.fsync(fd)
        except OSError:
            raise StorageIntegrityIndeterminate() from None
        finally:
            os.close(fd)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            raise StorageIntegrityIndeterminate() from None

    def _prepare_root(self, configured_root: str | os.PathLike[str]) -> Path:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise StorageConfigurationError()
        configured = Path(configured_root)
        try:
            if configured.is_symlink():
                raise StorageConfigurationError()
            resolved = configured.absolute().resolve(strict=False)
            resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._assert_directory(resolved)
            os.chmod(resolved, 0o700)
            return resolved
        except StorageError:
            raise
        except OSError:
            raise StorageConfigurationError() from None

    def _prepare_directory(self, path: Path) -> Path:
        try:
            if self._exists(path) and path.is_symlink():
                raise StorageConfigurationError()
            path.mkdir(mode=0o700, exist_ok=True)
            self._assert_directory(path)
            os.chmod(path, 0o700)
            return path
        except StorageError:
            raise
        except OSError:
            raise StorageConfigurationError() from None

    def _ensure_scope_directory(self, root: Path, scope_key: str) -> Path:
        path = root / scope_key
        try:
            if self._exists(path) and path.is_symlink():
                raise StorageIntegrityIndeterminate()
            path.mkdir(mode=0o700, exist_ok=True)
            self._assert_directory(path)
            os.chmod(path, 0o700)
        except StorageError:
            raise
        except OSError:
            raise StorageConfigurationError() from None
        self._known_scope_keys.add(scope_key)
        return path

    @staticmethod
    def _assert_directory(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise StorageConfigurationError()

    @staticmethod
    def _mkdir_exclusive(path: Path) -> None:
        path.mkdir(mode=0o700, exist_ok=False)

    @staticmethod
    def _create_empty_payload(path: Path) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            os.lstat(path)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageIntegrityIndeterminate() from None

    @staticmethod
    def _unlink_nofollow(path: Path) -> None:
        entry = os.lstat(path)
        if stat.S_ISLNK(entry.st_mode):
            raise StorageIntegrityIndeterminate()
        os.unlink(path)

    def _write_json_exclusive(self, path: Path, value: dict[str, Any]) -> None:
        payload = self._json_bytes(value)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            self._write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync_directory(path.parent)

    def _write_json_atomic(self, path: Path, value: dict[str, Any]) -> None:
        if self._exists(path) and path.is_symlink():
            raise StorageIntegrityIndeterminate()
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            self._write_json_exclusive(temporary, value)
        except StorageError:
            raise
        except OSError:
            raise StorageIntegrityIndeterminate() from None
        try:
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        except OSError:
            try:
                self._unlink_nofollow(temporary)
            except OSError:
                pass
            raise StorageIntegrityIndeterminate() from None

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]

    def _read_manifest(self, path: Path) -> dict[str, Any]:
        fd = self._open_read(path)
        try:
            raw = os.read(fd, 128 * 1024)
        finally:
            os.close(fd)
        try:
            value = json.loads(raw.decode(_JSON_ENCODING))
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        if not isinstance(value, dict) or value.get("version") != _MANIFEST_VERSION:
            raise StorageIntegrityIndeterminate()
        return value

    @staticmethod
    def _json_bytes(value: dict[str, Any]) -> bytes:
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode(_JSON_ENCODING)
        except (TypeError, ValueError):
            raise StorageIntegrityIndeterminate() from None

    def _scope_key(self, workspace_id: UUID, environment_id: UUID | None) -> str:
        value = f"{workspace_id}:{environment_id or '-'}".encode("ascii")
        return hashlib.sha256(value).hexdigest()

    def _scope_key_from_state_scope(self, state: UploadCommandState) -> str:
        for scope_key in self._known_scope_keys:
            candidate = self._locate_upload_in_scope(state.upload_id, scope_key)
            if candidate is not None:
                return scope_key
        # Finalization only receives the storage state, not the tenant context.
        # A restart can recover the scope by inspecting the bounded inventory.
        location = self._locate_upload(state.upload_id)
        if location is not None:
            return location[3]["scope_key"]
        raise UploadNotFound()

    def _locate_upload_in_scope(self, upload_id: UUID, scope_key: str) -> Path | None:
        candidate = self._staged_root / scope_key / upload_id.hex / "manifest.json"
        return candidate if self._exists(candidate) else None

    @staticmethod
    def _command_fingerprint(command: OriginalUploadCommandInput) -> str:
        payload = {
            "operation_name": command.operation_name,
            "operation_schema_version": command.operation_schema_version,
            "idempotency_key": command.idempotency_key,
            "workspace_id": str(command.workspace_id),
            "environment_id": str(command.environment_id) if command.environment_id else None,
            "expected_sha256": command.expected_sha256,
            "expected_byte_length": command.expected_byte_length,
            "purpose": command.purpose,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _new_generation(self) -> UUID:
        try:
            generation = self._generation_factory()
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        if not isinstance(generation, UUID) or generation.int == 0:
            raise StorageIntegrityIndeterminate()
        return generation

    def _deadline(self) -> float:
        return self._monotonic_clock() + self._profile.operation_timeout

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = deadline - self._monotonic_clock()
        if remaining <= 0:
            raise UploadLimitExceeded()
        return remaining

    @staticmethod
    def _manifest_name(object_id: UUID) -> str:
        return f"{object_id.hex}.manifest.json"

    @staticmethod
    def _payload_name_from_manifest(manifest: dict[str, Any]) -> str:
        try:
            object_id = UUID(manifest["object_id"])
        except (KeyError, ValueError, TypeError):
            raise StorageIntegrityIndeterminate() from None
        return object_id.hex

    @staticmethod
    def _validate_uuid(value: UUID) -> None:
        if not isinstance(value, UUID) or value.int == 0:
            raise UploadNotFound()

    @staticmethod
    def _validate_upload_state(state: UploadCommandState) -> None:
        if not isinstance(state, UploadCommandState):
            raise UploadNotFound()

    @staticmethod
    def _validate_reference(reference: ObjectReference) -> None:
        if not isinstance(reference, ObjectReference):
            raise InvalidObjectReference()

    def _inventory_cursor(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            value = int(cursor)
        except (TypeError, ValueError):
            raise StorageConfigurationError() from None
        if value < 0 or value > self._profile.max_inventory_entries:
            raise StorageConfigurationError()
        return value

    def _observe_io(
        self,
        operation: _IoOperation | None,
        limit: int,
        processed: int,
    ) -> None:
        if operation is not None and self._io_observer is not None:
            self._io_observer(operation, limit, processed)

    @staticmethod
    def _configuration_or_unavailable(error: OSError) -> StorageError:
        if error.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTSUP", 95), getattr(os, "EINVAL", 22)}:
            return StorageConfigurationError()
        if isinstance(error, (PermissionError, IsADirectoryError, NotADirectoryError)):
            return StorageIntegrityIndeterminate()
        return ObjectUnavailable()

    @staticmethod
    def _publication_error(error: OSError) -> StorageError:
        if isinstance(error, FileExistsError):
            return ObjectAlreadyExists()
        if error.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTSUP", 95), getattr(os, "EINVAL", 22)}:
            return StorageIntegrityIndeterminate()
        return ObjectUnavailable()

    @staticmethod
    def _read_error(error: OSError) -> StorageError:
        if isinstance(error, FileNotFoundError):
            return ObjectNotFound()
        if error.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTSUP", 95), getattr(os, "EINVAL", 22)}:
            return StorageIntegrityIndeterminate()
        return ObjectUnavailable()


__all__ = ["FilesystemOriginalObjectStore", "FilesystemStorageProfile"]
