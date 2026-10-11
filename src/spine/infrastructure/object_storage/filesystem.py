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
import base64
import binascii
import errno
import hashlib
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4
from weakref import WeakValueDictionary, finalize

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
    PreWriteRequestDigest,
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
class _ScannedEntry:
    name: str
    is_symlink: bool
    is_file: bool
    is_dir: bool


@dataclass(frozen=True, slots=True)
class _ReadPlan:
    payload_path: Path
    receipt: WriteReceipt
    limit: int
    deadline: float
    payload_identity: tuple[int, int, int]


class _BlockingOperationAbandoned(StorageIntegrityIndeterminate):
    """The worker outlived its caller and still owns any passed resources."""


class _OwnedFd:
    """Close an fd at most once, even when worker cleanup races caller cleanup."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._lock = threading.Lock()
        self._closed = False

    def close(self, _value: Any = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.close(self.fd)
            except OSError:
                raise StorageIntegrityIndeterminate() from None


class _ReadGrantBinding(Protocol):
    workspace_id: UUID
    environment_id: UUID | None
    object_reference: ObjectReference


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
        self._upload_locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()
        self._deletion_approvals: OrderedDict[ObjectReference, DeletionApproval] = OrderedDict()
        self._state_lock = threading.RLock()
        self._indeterminate = threading.Event()
        self._ready_lock = asyncio.Lock()
        self._initialized = False
        self._root = Path(profile.root).absolute()
        self._root_fd: int | None = None
        self._root_fd_finalizer = None
        self._staged_root = self._root / "staged"
        self._finalized_root = self._root / "finalized"
        self._deleted_root = self._root / "deleted"

    async def begin_or_resume_upload(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
    ) -> UploadCommandState:
        await self._ensure_ready()
        async with self._operation_limit:
            self._ensure_operational()
            deadline = self._deadline()
            return await self._run_blocking(
                self._begin_or_resume_upload_sync,
                command,
                state,
                deadline,
                deadline=deadline,
            )

    def _begin_or_resume_upload_sync(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
        operation_deadline: float,
    ) -> UploadCommandState:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        self._validate_upload_state(state)
        scope_key = self._scope_key(command.workspace_id, command.environment_id)
        scope_dir = self._ensure_scope_directory(self._staged_root, scope_key)
        final_scope_dir = self._ensure_scope_directory(self._finalized_root, scope_key)
        if self._deleted_object_status(state.object_id) is not None:
            raise UploadConflict()
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
                    deadline=operation_deadline,
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
                if (
                    manifest.get("observed_digest") is None
                    or manifest.get("observed_length") is None
                ):
                    return state.transitioned(
                        UploadCommandStateName.RECONCILIATION_REQUIRED
                    )
                self._set_manifest_status(manifest_path, manifest, "staging")
                return state.transitioned(UploadCommandStateName.STAGING)
            return state

        if state.state is not UploadCommandStateName.STAGING:
            raise UploadNotFound()
        existing_location = self._locate_upload(state.upload_id)
        if existing_location is not None:
            existing_scope = self._actual_scope_for_location(existing_location)
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
            if error.errno == getattr(os, "ELOOP", 40):
                raise StorageIntegrityIndeterminate() from None
            raise self._configuration_or_unavailable(error) from None
        return state

    async def write_chunks(
        self,
        upload_id: UUID,
        chunks: AsyncIterable[bytes],
    ) -> ObservedContentIdentity:
        await self._ensure_ready()
        self._validate_uuid(upload_id)
        lock = self._upload_locks.setdefault(upload_id, asyncio.Lock())
        async with self._operation_limit, lock:
            self._ensure_operational()
            deadline = self._deadline()
            location = await self._run_blocking(
                self._locate_upload,
                upload_id,
                deadline=deadline,
            )
            if location is None:
                raise UploadNotFound()
            kind, payload_path, manifest_path, manifest = location
            if kind == "finalized":
                receipt = await self._run_blocking(
                    self._receipt_from_manifest, manifest, manifest_path
                )
                try:
                    return await self._observe_input_stream(
                        chunks,
                        deadline=deadline,
                        expected_length=receipt.object_reference.byte_length,
                    )
                except StorageError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise ObjectUnavailable() from None
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
                    manifest_path=manifest_path,
                )
                await self._run_blocking(
                    self._record_observed_content,
                    payload_path,
                    manifest_path,
                    observed,
                    deadline,
                    deadline=deadline,
                )
                return observed
            except ObjectCorrupt as error:
                await self._mark_integrity_conflict_after_failure(
                    manifest_path, manifest, deadline
                )
                raise error
            except StorageError as error:
                await self._mark_interrupted_after_failure(
                    manifest_path, manifest, deadline
                )
                raise error
            except asyncio.CancelledError:
                await self._mark_interrupted_after_failure(
                    manifest_path, manifest, deadline
                )
                raise
            except Exception:
                await self._mark_interrupted_after_failure(
                    manifest_path, manifest, deadline
                )
                raise ObjectUnavailable() from None

    async def abort_upload(self, state: UploadCommandState) -> UploadCommandState:
        await self._ensure_ready()
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        self._validate_upload_state(state)
        lock = self._upload_locks.setdefault(state.upload_id, asyncio.Lock())
        async with self._operation_limit, lock:
            self._ensure_operational()
            deadline = self._deadline()
            return await self._run_blocking(
                self._abort_upload_sync,
                state,
                deadline,
                deadline=deadline,
            )

    def _abort_upload_sync(
        self, state: UploadCommandState, deadline: float
    ) -> UploadCommandState:
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        self._validate_upload_state(state)
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
        payload_fd: int | None = None
        try:
            if self._exists(payload_path):
                payload_fd = self._open_append(payload_path)
                self._lock_file(payload_fd, deadline)
                manifest = self._read_manifest(manifest_path)
                self._assert_manifest_identity_from_state(manifest, state)
                status = manifest.get("status")
                if status == "aborted":
                    return state.transitioned(UploadCommandStateName.ABORTED)
                if status == "integrity_conflict":
                    raise ObjectProtected()
                if status not in {"staging", "interrupted"}:
                    raise StorageIntegrityIndeterminate()
                self._unlink_nofollow(payload_path)
            self._set_manifest_status(manifest_path, manifest, "aborted")
        except StorageError:
            raise
        except OSError:
            raise ObjectUnavailable() from None
        finally:
            if payload_fd is not None:
                self._close_fd(payload_fd)
        return state.transitioned(UploadCommandStateName.ABORTED)

    async def finalize_upload(
        self,
        state: UploadCommandState,
        observed_content: ObservedContentIdentity,
    ) -> WriteReceipt:
        await self._ensure_ready()
        if state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        self._validate_upload_state(state)
        lock = self._upload_locks.setdefault(state.upload_id, asyncio.Lock())
        async with self._operation_limit, lock:
            self._ensure_operational()
            deadline = self._deadline()
            return await self._run_blocking(
                self._finalize_upload_sync,
                state,
                observed_content,
                deadline,
                deadline=deadline,
            )

    def _finalize_upload_sync(
        self,
        state: UploadCommandState,
        observed_content: ObservedContentIdentity,
        deadline: float,
    ) -> WriteReceipt:
        scope_key = self._scope_key_from_state_scope(state)
        if self._deleted_object_status(state.object_id) is not None:
            raise UploadConflict()
        final_scope_dir = self._ensure_scope_directory(self._finalized_root, scope_key)
        final_payload = final_scope_dir / state.object_id.hex
        final_manifest = final_scope_dir / self._manifest_name(state.object_id)
        if self._exists(final_manifest):
            try:
                return self._replay_finalization(
                    state,
                    observed_content,
                    final_payload,
                    final_manifest,
                    deadline=deadline,
                )
            except UploadConflict:
                self._mark_publication_conflict(
                    self._staged_root / scope_key / state.upload_id.hex / "manifest.json",
                    deadline=deadline,
                )
                raise

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
                deadline=deadline,
            )

        self._assert_manifest_identity_from_state(manifest, state)
        status = manifest.get("status")
        if status == "aborted":
            raise ObjectProtected()
        if status == "integrity_conflict":
            raise IntegrityMismatch()
        if status == "publishing":
            raise StorageIntegrityIndeterminate()
        if status != "staging":
            raise UploadConflict()

        stage_fd = self._open_append(stage_payload)
        try:
            self._lock_file(stage_fd, deadline)
            if self._exists(final_manifest):
                try:
                    return self._replay_finalization(
                        state,
                        observed_content,
                        final_payload,
                        final_manifest,
                        deadline=deadline,
                    )
                except UploadConflict:
                    self._mark_publication_conflict(
                        stage_manifest_path, deadline=deadline, locked=True
                    )
                    raise
            manifest = self._read_manifest(stage_manifest_path)
            self._assert_manifest_identity_from_state(manifest, state)
            status = manifest.get("status")
            if status == "aborted":
                raise ObjectProtected()
            if status == "integrity_conflict":
                raise IntegrityMismatch()
            if status == "publishing":
                raise StorageIntegrityIndeterminate()
            if status != "staging":
                raise UploadConflict()

            actual = self._hash_file(
                stage_payload,
                self._profile.max_object_bytes,
                deadline,
                None,
            )
            stage_identity = self._file_identity_from_fd(stage_fd)
            if self._file_identity(stage_payload) != stage_identity:
                raise StorageIntegrityIndeterminate()
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
            receipt = self._make_receipt(state, manifest, generation, actual)
            publishing_manifest = dict(manifest)
            publishing_manifest["status"] = "publishing"
            publishing_manifest["receipt"] = receipt.model_dump(mode="json")
            finalized_manifest = dict(publishing_manifest)
            finalized_manifest["status"] = "finalized"
            self._write_json_atomic(stage_manifest_path, publishing_manifest)
            publication_fd: int | None = None
            try:
                publication_fd = self._publish_no_replace(
                    stage_payload,
                    final_payload,
                    expected_source_identity=stage_identity,
                    deadline=deadline,
                )
                self._sync_file(final_payload, expected_identity=stage_identity)
                self._write_json_exclusive(final_manifest, finalized_manifest)
                self._sync_directory(final_scope_dir)
            except FileExistsError:
                while not self._exists(final_manifest):
                    if self._deleted_object_status(state.object_id) is not None:
                        raise UploadConflict()
                    if self._monotonic_clock() >= deadline:
                        raise StorageIntegrityIndeterminate()
                    time.sleep(min(0.01, max(0.0, deadline - self._monotonic_clock())))
                if not self._exists(final_manifest):
                    raise StorageIntegrityIndeterminate() from None
                try:
                    return self._replay_finalization(
                        state,
                        observed_content,
                        final_payload,
                        final_manifest,
                        deadline=deadline,
                    )
                except UploadConflict:
                    self._mark_publication_conflict(
                        stage_manifest_path, deadline=deadline, locked=True
                    )
                    raise
            except StorageError:
                raise
            except OSError as error:
                raise self._publication_error(error) from None
            finally:
                if publication_fd is not None:
                    self._close_fd(publication_fd)
            return receipt
        finally:
            self._close_fd(stage_fd)

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
        await self._ensure_ready()
        if max_bytes <= 0:
            raise UploadLimitExceeded()
        async with self._operation_limit:
            self._ensure_operational()
            try:
                binding = self._grant_registry.consume_read_lease(
                    lease,
                    now=self._clock(),
                )
            except StorageError:
                raise
            except Exception:
                raise InvalidReadGrant() from None
            try:
                read_deadline = self._deadline()
                plan = await self._run_blocking(
                    self._prepare_read_sync,
                    binding,
                    max_bytes,
                    read_deadline,
                    deadline=read_deadline,
                )
                actual = await self._run_blocking(
                    self._hash_file,
                    plan.payload_path,
                    self._profile.max_object_bytes,
                    plan.deadline,
                    "read",
                    deadline=plan.deadline,
                )
                if actual != plan.receipt.observed_content:
                    raise ObjectCorrupt()
            except StorageError:
                raise
            except OSError as error:
                raise self._read_error(error) from None
        try:
            async for part in self._read_file(
                plan.payload_path,
                limit=plan.limit,
                deadline=plan.deadline,
                expected_identity=plan.payload_identity,
            ):
                yield part
        except StorageError:
            raise
        except OSError as error:
            raise self._read_error(error) from None
        except (TypeError, NotImplementedError, ValueError):
            raise StorageConfigurationError() from None

    def _prepare_read_sync(
        self,
        binding: _ReadGrantBinding,
        max_bytes: int,
        deadline: float,
    ) -> _ReadPlan:
        reference = binding.object_reference
        self._validate_reference(reference)
        scope_key = self._scope_key(binding.workspace_id, binding.environment_id)
        final_scope_dir = self._finalized_root / scope_key
        if not self._exists(final_scope_dir):
            raise InvalidReadGrant()
        if self._is_symlink(final_scope_dir):
            raise StorageIntegrityIndeterminate()
        payload_path = final_scope_dir / reference.object_id.hex
        manifest_path = final_scope_dir / self._manifest_name(reference.object_id)
        if not self._exists(manifest_path):
            raise ObjectNotFound()
        manifest = self._read_manifest(manifest_path)
        self._assert_manifest_scope(manifest, final_scope_dir)
        receipt = self._receipt_from_manifest(manifest, manifest_path)
        if self._is_deleted_reference(reference):
            raise ObjectNotFound()
        if self._is_deletion_pending(reference):
            raise StorageIntegrityIndeterminate()
        if receipt.object_reference != reference:
            raise InvalidReadGrant()
        if not self._exists(payload_path):
            raise ObjectNotFound()
        limit = min(max_bytes, self._profile.max_object_bytes)
        size = self._regular_file_size(payload_path)
        if size != reference.byte_length:
            raise ObjectCorrupt()
        return _ReadPlan(
            payload_path=payload_path,
            receipt=receipt,
            limit=limit,
            deadline=deadline,
            payload_identity=self._file_identity(payload_path),
        )

    async def verify(
        self,
        reference: ObjectReference,
        *,
        max_bytes: int,
    ) -> IntegrityResult:
        await self._ensure_ready()
        self._validate_reference(reference)
        async with self._operation_limit:
            self._ensure_operational()
            deadline = self._deadline()
            return await self._run_blocking(
                self._verify_sync,
                reference,
                max_bytes,
                deadline,
                deadline=deadline,
            )

    def _verify_sync(
        self,
        reference: ObjectReference,
        max_bytes: int,
        deadline: float,
    ) -> IntegrityResult:
        location = self._locate_finalized_reference(reference)
        if location is None:
            raise ObjectNotFound()
        payload_path, manifest_path = location
        manifest = self._read_manifest(manifest_path)
        receipt = self._receipt_from_manifest(manifest, manifest_path)
        if receipt.object_reference != reference:
            raise InvalidObjectReference()
        if self._is_deletion_pending(reference):
            raise StorageIntegrityIndeterminate()
        if max_bytes < reference.byte_length:
            self._observe_io("verify", max_bytes, 0)
            raise UploadLimitExceeded()
        try:
            actual = self._hash_file(
                payload_path,
                min(max_bytes, self._profile.max_object_bytes),
                deadline,
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
        await self._ensure_ready()
        if limit <= 0 or limit > self._profile.max_inventory_entries:
            raise UploadLimitExceeded()
        async with self._operation_limit:
            self._ensure_operational()
            deadline = self._deadline()
            return await self._run_blocking(
                self._reconciliation_inventory_sync,
                cursor,
                limit,
                deadline,
                deadline=deadline,
            )

    def _reconciliation_inventory_sync(
        self,
        cursor: str | None,
        limit: int,
        deadline: float,
    ) -> tuple[tuple[ReconciliationInventoryEntry, ...], str | None]:
        start_key = self._inventory_cursor(cursor)
        page: list[ReconciliationInventoryEntry] = []
        seen_object_ids: set[UUID] = set()
        self._validate_staged_namespace(deadline=deadline)
        scanned = 0
        has_more = False
        last_key: str | None = None
        for manifest_path in self._iter_final_manifests():
            manifest_key = f"{manifest_path.parent.name}/{manifest_path.name}"
            if start_key is not None and manifest_key <= start_key:
                continue
            if scanned >= self._profile.max_inventory_entries:
                raise UploadLimitExceeded()
            manifest = self._read_manifest(manifest_path)
            self._assert_manifest_scope(manifest, manifest_path.parent)
            receipt = self._receipt_from_manifest(manifest, manifest_path)
            if self._is_deleted_reference(receipt.object_reference):
                continue
            if self._is_deletion_pending(receipt.object_reference):
                raise StorageIntegrityIndeterminate()
            object_id = receipt.object_reference.object_id
            if object_id in seen_object_ids:
                raise StorageIntegrityIndeterminate()
            seen_object_ids.add(object_id)
            if manifest_path.name != self._manifest_name(receipt.object_reference.object_id):
                raise StorageIntegrityIndeterminate()
            payload_path = manifest_path.parent / receipt.object_reference.object_id.hex
            if not self._exists(payload_path) or self._is_symlink(payload_path):
                raise StorageIntegrityIndeterminate()
            self._verify_finalized_file(payload_path, receipt, deadline=deadline)
            if len(page) < limit:
                page.append(
                    ReconciliationInventoryEntry(
                        object_reference=receipt.object_reference,
                        upload_id=receipt.upload_id,
                        finalized=True,
                        observed_at=self._clock(),
                    )
                )
                last_key = manifest_key
            else:
                has_more = True
            scanned += 1
        next_cursor = self._encode_inventory_cursor(last_key) if has_more else None
        return tuple(page), next_cursor

    async def prepare_deletion(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> None:
        await self._ensure_ready()
        self._validate_reference(reference)
        async with self._operation_limit:
            self._ensure_operational()
            await self._run_blocking(
                self._prepare_deletion_sync,
                reference,
                approval,
                deadline=self._deadline(),
            )

    def _prepare_deletion_sync(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> None:
        if self._is_deleted_reference(reference):
            self._remember_deletion_approval(reference, approval)
            return
        location = self._locate_finalized_reference(reference)
        if location is None:
            raise ObjectNotFound()
        self._assert_exact_finalized_reference(location, reference)
        self._remember_deletion_approval(reference, approval)

    async def delete_controlled(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> DeletionResult:
        await self._ensure_ready()
        self._validate_reference(reference)
        async with self._operation_limit:
            self._ensure_operational()
            deadline = self._deadline()
            return await self._run_blocking(
                self._delete_controlled_sync,
                reference,
                approval,
                deadline,
                deadline=deadline,
            )

    def _delete_controlled_sync(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
        deadline: float,
    ) -> DeletionResult:
        with self._state_lock:
            if self._deletion_approvals.get(reference) != approval:
                raise ObjectProtected()
        marker_status = self._deletion_marker_status(reference)
        if marker_status == "deleted":
            return DeletionResult(status="already_absent")
        location = self._locate_finalized_reference(reference)
        if location is None:
            marker_status = self._deletion_marker_status(reference)
            if marker_status == "pending":
                scope_key = self._deletion_marker_scope(reference)
                if scope_key is None:
                    raise StorageIntegrityIndeterminate()
                self._recover_pending_deletion(reference, scope_key, deadline)
                self._remove_staged_publication_evidence(
                    reference,
                    scope_key,
                    deadline=deadline,
                )
                self._write_deleted_marker(
                    reference,
                    status="deleted",
                    scope_key=scope_key,
                )
                return DeletionResult(status="already_absent")
            raise ObjectNotFound()
        self._assert_exact_finalized_reference(location, reference)
        payload_path, manifest_path = location
        scope_dir = manifest_path.parent
        if marker_status == "pending" and not self._exists(payload_path):
            self._recover_pending_deletion(reference, scope_dir.name, deadline)
            self._remove_staged_publication_evidence(
                reference,
                scope_dir.name,
                deadline=deadline,
            )
            self._write_deleted_marker(
                reference,
                status="deleted",
                scope_key=scope_dir.name,
            )
            return DeletionResult(status="already_absent")
        payload_fd: int | None = None
        scope_fd: int | None = None
        try:
            scope_fd = self._open_directory(scope_dir)
            self._lock_file(scope_fd, deadline)
            marker_status = self._deletion_marker_status(reference)
            if marker_status == "deleted":
                return DeletionResult(status="already_absent")
            if marker_status == "pending" and not self._exists(payload_path):
                self._recover_pending_deletion_locked(
                    reference,
                    scope_dir.name,
                    deadline,
                )
                self._remove_staged_publication_evidence(
                    reference,
                    scope_dir.name,
                    deadline=deadline,
                )
                self._write_deleted_marker(
                    reference,
                    status="deleted",
                    scope_key=scope_dir.name,
                )
                return DeletionResult(status="already_absent")
            self._write_deleted_marker(
                reference,
                status="pending",
                scope_key=scope_dir.name,
            )
            payload_fd = self._open_append(payload_path)
            self._lock_file(payload_fd, deadline)
            manifest = self._read_manifest(manifest_path)
            receipt = self._receipt_from_manifest(manifest, manifest_path)
            if receipt.object_reference != reference:
                raise StorageIntegrityIndeterminate()
            identity = self._file_identity_from_fd(payload_fd)
            if self._file_identity(payload_path) != identity:
                raise StorageIntegrityIndeterminate()
            manifest_identity = self._file_identity(manifest_path)
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            self._unlink_nofollow(payload_path, expected_identity=identity)
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            self._unlink_nofollow(manifest_path, expected_identity=manifest_identity)
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            self._sync_directory(scope_dir)
            self._remove_staged_publication_evidence(
                reference, scope_dir.name, deadline=deadline
            )
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            self._write_deleted_marker(
                reference,
                status="deleted",
                scope_key=scope_dir.name,
            )
        except StorageError:
            raise
        except OSError:
            raise ObjectDeletionFailed() from None
        finally:
            if payload_fd is not None:
                self._close_fd(payload_fd)
            if scope_fd is not None:
                self._close_fd(scope_fd)
        return DeletionResult(status="deleted")

    def _remove_staged_publication_evidence(
        self,
        reference: ObjectReference,
        scope_key: str,
        *,
        deadline: float,
    ) -> None:
        staged_scope = self._staged_root / scope_key
        if not self._exists(staged_scope):
            return
        if self._is_symlink(staged_scope) or not self._is_directory(staged_scope):
            raise StorageIntegrityIndeterminate()
        scope_fd = self._open_directory(staged_scope)
        try:
            self._lock_file(scope_fd, deadline)
            self._remove_staged_publication_evidence_locked(
                reference,
                scope_key,
                deadline=deadline,
            )
        finally:
            self._close_fd(scope_fd)

    def _remove_staged_publication_evidence_locked(
        self,
        reference: ObjectReference,
        scope_key: str,
        *,
        deadline: float,
    ) -> None:
        staged_scope = self._staged_root / scope_key
        if not self._exists(staged_scope):
            return
        if self._is_symlink(staged_scope) or not self._is_directory(staged_scope):
            raise StorageIntegrityIndeterminate()
        for entry in self._scandir(staged_scope):
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            if entry.is_symlink or not entry.is_dir:
                raise StorageIntegrityIndeterminate()
            stage_dir = staged_scope / entry.name
            manifest_path = stage_dir / "manifest.json"
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            if not self._exists(manifest_path):
                continue
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            manifest = self._read_manifest(manifest_path)
            self._assert_manifest_scope(manifest, staged_scope)
            if (
                manifest.get("status") != "publishing"
                or manifest.get("object_id") != str(reference.object_id)
            ):
                continue
            try:
                staged_receipt = WriteReceipt.model_validate_json(
                    json.dumps(manifest["receipt"], separators=(",", ":"))
                )
                manifest_upload_id = UUID(manifest["upload_id"])
            except (Exception, ValueError):
                raise StorageIntegrityIndeterminate() from None
            if (
                staged_receipt.object_reference != reference
                or staged_receipt.upload_id != manifest_upload_id
            ):
                continue
            payload_path = stage_dir / "payload"
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            if self._exists(payload_path):
                if self._monotonic_clock() >= deadline:
                    raise StorageIntegrityIndeterminate()
                payload_identity = self._file_identity(payload_path)
                self._unlink_nofollow(
                    payload_path,
                    expected_identity=payload_identity,
                )
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            manifest_identity = self._file_identity(manifest_path)
            self._unlink_nofollow(manifest_path, expected_identity=manifest_identity)
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            self._rmdir_nofollow(stage_dir)
            if self._monotonic_clock() >= deadline:
                raise StorageIntegrityIndeterminate()
            self._sync_directory(staged_scope)

    def _recover_pending_deletion(
        self,
        reference: ObjectReference,
        scope_key: str,
        deadline: float,
    ) -> None:
        if not self._is_scope_key(scope_key):
            raise StorageIntegrityIndeterminate()
        scope_dir = self._finalized_root / scope_key
        scope_fd = self._open_directory(scope_dir) if self._exists(scope_dir) else None
        try:
            if scope_fd is not None:
                self._lock_file(scope_fd, deadline)
            self._recover_pending_deletion_locked(reference, scope_key, deadline)
        finally:
            if scope_fd is not None:
                self._close_fd(scope_fd)

    def _recover_pending_deletion_locked(
        self,
        reference: ObjectReference,
        scope_key: str,
        deadline: float,
    ) -> None:
        if not self._is_scope_key(scope_key):
            raise StorageIntegrityIndeterminate()
        scope_dir = self._finalized_root / scope_key
        payload_path = scope_dir / reference.object_id.hex
        manifest_path = scope_dir / self._manifest_name(reference.object_id)
        manifest_exists = self._exists(manifest_path)
        payload_exists = self._exists(payload_path)
        if not manifest_exists and not payload_exists:
            return
        if not manifest_exists:
            raise StorageIntegrityIndeterminate()
        manifest = self._read_manifest(manifest_path)
        self._assert_manifest_scope(manifest, scope_dir)
        receipt = self._receipt_from_manifest(manifest, manifest_path)
        if receipt.object_reference != reference:
            raise StorageIntegrityIndeterminate()
        if payload_exists:
            payload_identity = self._file_identity(payload_path)
            self._unlink_nofollow(payload_path, expected_identity=payload_identity)
        if self._monotonic_clock() >= deadline:
            raise StorageIntegrityIndeterminate()
        manifest_identity = self._file_identity(manifest_path)
        self._unlink_nofollow(manifest_path, expected_identity=manifest_identity)
        if self._monotonic_clock() >= deadline:
            raise StorageIntegrityIndeterminate()
        self._sync_directory(scope_dir)

    def _read_deletion_marker(self, reference: ObjectReference) -> dict[str, Any] | None:
        marker = self._deleted_root / f"{reference.object_id.hex}.json"
        if not self._exists(marker):
            return None
        try:
            value = self._read_manifest(marker)
            restored = ObjectReference.model_validate_json(
                json.dumps(value["reference"], separators=(",", ":"))
            )
        except StorageError:
            raise
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        if restored != reference:
            raise StorageIntegrityIndeterminate()
        status = value.get("status", "deleted")
        if status not in {"pending", "deleted"}:
            raise StorageIntegrityIndeterminate()
        scope_key = value.get("scope_key")
        if scope_key is not None and not self._is_scope_key(scope_key):
            raise StorageIntegrityIndeterminate()
        return value

    def _deleted_object_status(self, object_id: UUID) -> str | None:
        marker = self._deleted_root / f"{object_id.hex}.json"
        if not self._exists(marker):
            return None
        try:
            value = self._read_manifest(marker)
            reference = ObjectReference.model_validate_json(
                json.dumps(value["reference"], separators=(",", ":"))
            )
        except StorageError:
            raise
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        if reference.object_id != object_id:
            raise StorageIntegrityIndeterminate()
        status = value.get("status", "deleted")
        if status not in {"pending", "deleted"}:
            raise StorageIntegrityIndeterminate()
        return status

    def _deletion_marker_status(self, reference: ObjectReference) -> str | None:
        value = self._read_deletion_marker(reference)
        if value is None:
            return None
        status = value.get("status", "deleted")
        return status

    def _deletion_marker_scope(self, reference: ObjectReference) -> str | None:
        value = self._read_deletion_marker(reference)
        if value is None:
            return None
        scope_key = value.get("scope_key")
        return scope_key if isinstance(scope_key, str) else None

    def _is_deleted_reference(self, reference: ObjectReference) -> bool:
        return self._deletion_marker_status(reference) == "deleted"

    def _is_deletion_pending(self, reference: ObjectReference) -> bool:
        return self._deletion_marker_status(reference) == "pending"

    def _remember_deletion_approval(
        self,
        reference: ObjectReference,
        approval: DeletionApproval,
    ) -> None:
        with self._state_lock:
            self._deletion_approvals[reference] = approval
            self._deletion_approvals.move_to_end(reference)
            while len(self._deletion_approvals) > self._profile.max_inventory_entries:
                self._deletion_approvals.popitem(last=False)

    def _assert_exact_finalized_reference(
        self,
        location: tuple[Path, Path],
        reference: ObjectReference,
    ) -> None:
        _, manifest_path = location
        manifest = self._read_manifest(manifest_path)
        receipt = self._receipt_from_manifest(manifest, manifest_path)
        if receipt.object_reference != reference:
            raise UploadConflict()

    def _state_for_finalized_upload(
        self,
        command: OriginalUploadCommandInput,
        state: UploadCommandState,
        payload_path: Path,
        manifest_path: Path,
        *,
        deadline: float,
    ) -> UploadCommandState:
        manifest = self._read_manifest(manifest_path)
        self._assert_manifest_identity(manifest, command, state)
        receipt = self._receipt_from_manifest(manifest, manifest_path)
        self._verify_finalized_file(payload_path, receipt, deadline=deadline)
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
        deadline: float | None = None,
    ) -> WriteReceipt:
        manifest = self._read_manifest(manifest_path)
        self._assert_manifest_scope(manifest, manifest_path.parent)
        self._assert_manifest_identity_from_state(manifest, state)
        receipt = self._receipt_from_manifest(manifest, manifest_path)
        if (
            state.state is UploadCommandStateName.FINALIZED
            and state.provisional_receipt != receipt
        ):
            raise UploadConflict()
        if receipt.observed_content != observed_content:
            raise UploadConflict()
        self._verify_finalized_file(payload_path, receipt, deadline=deadline)
        return receipt

    def _verify_finalized_file(
        self,
        payload_path: Path,
        receipt: WriteReceipt,
        *,
        deadline: float | None = None,
    ) -> None:
        try:
            actual = self._hash_file(
                payload_path,
                self._profile.max_object_bytes,
                deadline if deadline is not None else self._setup_deadline(),
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
                chunk = await self._next_stream_chunk(
                    iterator,
                    timeout=min(self._profile.inactivity_timeout, remaining),
                )
            except StopAsyncIteration:
                break
            except (asyncio.TimeoutError, TimeoutError):
                raise UploadLimitExceeded() from None
            self._validate_chunk(chunk, length)
            if length + len(chunk) > expected_length:
                raise UploadConflict()
            digest.update(chunk)
            length += len(chunk)
        return ObservedContentIdentity(
            observed_sha256=digest.hexdigest(),
            observed_byte_length=length,
        )

    async def _next_stream_chunk(
        self,
        iterator: AsyncIterator[bytes],
        *,
        timeout: float,
    ) -> bytes:
        task = asyncio.create_task(iterator.__anext__())
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=min(0.1, timeout),
                    )
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    self._indeterminate.set()
                    task.add_done_callback(self._discard_stream_task)
                else:
                    self._discard_stream_task(task)
            raise

    @staticmethod
    def _discard_stream_task(task: asyncio.Task[bytes]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    async def _append_stream(
        self,
        payload_path: Path,
        chunks: AsyncIterable[bytes],
        *,
        deadline: float,
        manifest: dict[str, Any],
        manifest_path: Path,
    ) -> ObservedContentIdentity:
        fd = await self._run_blocking(
            self._open_append,
            payload_path,
            on_cancel=os.close,
            deadline=deadline,
        )
        owned_fd = _OwnedFd(fd)
        fd_abandoned = False
        try:
            setup_deadline = deadline
            try:
                await self._run_blocking(
                    self._lock_file,
                    fd,
                    setup_deadline,
                    on_cancel=owned_fd.close,
                )
            except (asyncio.CancelledError, _BlockingOperationAbandoned):
                fd_abandoned = True
                raise
            current_manifest = await self._run_blocking(
                self._read_manifest,
                manifest_path,
                deadline=setup_deadline,
            )
            if current_manifest.get("status") != "staging":
                raise UploadConflict()
            manifest = current_manifest
            existing = await self._run_blocking(
                self._hash_file,
                payload_path,
                self._profile.max_object_bytes,
                setup_deadline,
                None,
                deadline=setup_deadline,
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
            length = existing.observed_byte_length
            # Re-hash the existing prefix without retaining it in memory.
            await self._run_blocking(
                self._update_digest_from_path,
                payload_path,
                digest,
                setup_deadline,
                deadline=setup_deadline,
            )
            stream_deadline = deadline
            iterator = chunks.__aiter__()
            while True:
                remaining = self._remaining_timeout(stream_deadline)
                try:
                    chunk = await self._next_stream_chunk(
                        iterator,
                        timeout=min(self._profile.inactivity_timeout, remaining),
                    )
                except StopAsyncIteration:
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    raise UploadLimitExceeded() from None
                self._validate_chunk(chunk, length)
                write_fd = await self._run_blocking(
                    os.dup,
                    fd,
                    on_cancel=os.close,
                    deadline=stream_deadline,
                )
                owned_write_fd = _OwnedFd(write_fd)
                write_abandoned = False

                def release_abandoned_write(_: Any) -> None:
                    try:
                        owned_write_fd.close()
                    finally:
                        owned_fd.close()

                try:
                    await self._run_blocking(
                        self._write_chunk,
                        write_fd,
                        chunk,
                        stream_deadline,
                        on_cancel=release_abandoned_write,
                        deadline=stream_deadline,
                    )
                except (asyncio.CancelledError, _BlockingOperationAbandoned):
                    write_abandoned = True
                    fd_abandoned = True
                    raise
                finally:
                    if not write_abandoned:
                        await self._run_blocking(
                            owned_write_fd.close,
                            check_operational=False,
                        )
                digest.update(chunk)
                length += len(chunk)
        finally:
            if not fd_abandoned:
                await self._run_blocking(owned_fd.close, check_operational=False)
        return ObservedContentIdentity(
            observed_sha256=digest.hexdigest(),
            observed_byte_length=length,
        )

    def _locate_upload(
        self,
        upload_id: UUID,
    ) -> tuple[Literal["staged", "finalized"], Path, Path, dict[str, Any]] | None:
        staged: tuple[Path, Path, dict[str, Any]] | None = None
        finalized: tuple[Path, Path, dict[str, Any]] | None = None
        for scope_dir in self._iter_scope_directories(self._staged_root):
            candidate = scope_dir / upload_id.hex
            if self._exists(candidate) and (
                self._is_symlink(candidate) or not self._is_directory(candidate)
            ):
                raise StorageIntegrityIndeterminate()
            manifest_path = candidate / "manifest.json"
            if self._exists(manifest_path):
                manifest = self._read_manifest(manifest_path)
                self._assert_manifest_scope(manifest, scope_dir)
                if manifest.get("upload_id") == str(upload_id):
                    if staged is not None:
                        raise StorageIntegrityIndeterminate()
                    staged = (candidate / "payload", manifest_path, manifest)
        for manifest_path in self._iter_final_manifests():
            manifest = self._read_manifest(manifest_path)
            self._assert_manifest_scope(manifest, manifest_path.parent)
            if manifest.get("upload_id") == str(upload_id):
                if finalized is not None:
                    raise StorageIntegrityIndeterminate()
                finalized = (
                    manifest_path.parent
                    / self._payload_name_from_manifest(manifest),
                    manifest_path,
                    manifest,
                )
        if finalized:
            payload, manifest_path, manifest = finalized
            return "finalized", payload, manifest_path, manifest
        if staged:
            payload, manifest_path, manifest = staged
            return "staged", payload, manifest_path, manifest
        return None

    def _locate_finalized_reference(
        self,
        reference: ObjectReference,
    ) -> tuple[Path, Path] | None:
        if self._is_deleted_reference(reference):
            return None
        match: tuple[Path, Path] | None = None
        for manifest_path in self._iter_final_manifests():
            manifest = self._read_manifest(manifest_path)
            self._assert_manifest_scope(manifest, manifest_path.parent)
            receipt = self._receipt_from_manifest(manifest, manifest_path)
            if receipt.object_reference.object_id == reference.object_id:
                if match is not None:
                    raise StorageIntegrityIndeterminate()
                match = (
                    manifest_path.parent / self._payload_name_from_manifest(manifest),
                    manifest_path,
                )
        return match

    def _iter_final_manifests(self) -> Iterator[Path]:
        for scope_dir in self._iter_scope_directories(self._finalized_root):
            entries = self._scandir(scope_dir)
            manifest_names = {
                entry.name for entry in entries if entry.name.endswith(".manifest.json")
            }
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.is_symlink:
                    raise StorageIntegrityIndeterminate()
                if not entry.is_file:
                    raise StorageIntegrityIndeterminate()
                if entry.name.endswith(".manifest.json"):
                    prefix = entry.name[: -len(".manifest.json")]
                    if len(prefix) != _OPAQUE_NAME_LENGTH or any(
                        character not in "0123456789abcdef" for character in prefix
                    ):
                        raise StorageIntegrityIndeterminate()
                    yield scope_dir / entry.name
                elif (
                    len(entry.name) == _OPAQUE_NAME_LENGTH
                    and all(character in "0123456789abcdef" for character in entry.name)
                    and f"{entry.name}.manifest.json" in manifest_names
                ):
                    continue
                else:
                    raise StorageIntegrityIndeterminate()

    def _validate_staged_namespace(self, *, deadline: float) -> None:
        for scope_dir in self._iter_scope_directories(self._staged_root):
            for entry in self._scandir(scope_dir):
                if entry.is_symlink or not entry.is_dir:
                    raise StorageIntegrityIndeterminate()
                if len(entry.name) != _OPAQUE_NAME_LENGTH or any(
                    character not in "0123456789abcdef" for character in entry.name
                ):
                    raise StorageIntegrityIndeterminate()
                stage_dir = scope_dir / entry.name
                children = self._scandir(stage_dir)
                names = {child.name for child in children}
                if "manifest.json" not in names:
                    raise StorageIntegrityIndeterminate()
                manifest = self._read_manifest(stage_dir / "manifest.json")
                if manifest.get("scope_key") != scope_dir.name:
                    raise StorageIntegrityIndeterminate()
                self._validate_staged_manifest(manifest, entry.name)
                status = manifest.get("status")
                if status == "publishing":
                    try:
                        publishing_receipt = WriteReceipt.model_validate_json(
                            json.dumps(manifest["receipt"], separators=(",", ":"))
                        )
                    except Exception:
                        raise StorageIntegrityIndeterminate() from None
                    if (
                        publishing_receipt.upload_id != UUID(manifest["upload_id"])
                        or publishing_receipt.object_reference.object_id
                        != UUID(manifest["object_id"])
                        or (
                            manifest.get("expected_digest") is not None
                            and publishing_receipt.object_reference.digest_hex
                            != manifest.get("expected_digest")
                        )
                        or (
                            manifest.get("expected_length") is not None
                            and publishing_receipt.object_reference.byte_length
                            != manifest.get("expected_length")
                        )
                    ):
                        raise StorageIntegrityIndeterminate()
                    finalized_manifest_path = (
                        self._finalized_root
                        / scope_dir.name
                        / self._manifest_name(publishing_receipt.object_reference.object_id)
                    )
                    if not self._exists(finalized_manifest_path):
                        raise StorageIntegrityIndeterminate()
                    finalized_manifest = self._read_manifest(finalized_manifest_path)
                    finalized_receipt = self._receipt_from_manifest(
                        finalized_manifest, finalized_manifest_path
                    )
                    if finalized_receipt != publishing_receipt:
                        self._set_manifest_status(
                            stage_dir / "manifest.json",
                            manifest,
                            "integrity_conflict",
                        )
                        status = "integrity_conflict"
                if status not in {
                    "staging",
                    "interrupted",
                    "publishing",
                    "aborted",
                    "integrity_conflict",
                }:
                    raise StorageIntegrityIndeterminate()
                payload = stage_dir / "payload"
                if status in {"staging", "interrupted"} and not self._exists(payload):
                    raise StorageIntegrityIndeterminate()
                if status in {"staging", "interrupted"} and manifest.get(
                    "observed_digest"
                ) is not None:
                    actual = self._hash_file(
                        payload,
                        self._profile.max_object_bytes,
                        deadline,
                        None,
                    )
                    if (
                        actual.observed_sha256 != manifest.get("observed_digest")
                        or actual.observed_byte_length != manifest.get("observed_length")
                    ):
                        raise StorageIntegrityIndeterminate()
                for child in children:
                    if child.is_symlink or not child.is_file:
                        raise StorageIntegrityIndeterminate()
                    if child.name not in {"payload", "manifest.json"}:
                        raise StorageIntegrityIndeterminate()

    def _validate_staged_manifest(
        self,
        manifest: dict[str, Any],
        stage_name: str,
    ) -> None:
        try:
            upload_id = UUID(manifest["upload_id"])
            object_id = UUID(manifest["object_id"])
            issue8_receipt_id = UUID(manifest["issue8_receipt_id"])
            PreWriteRequestDigest.model_validate_json(
                json.dumps(manifest["request_digest"], separators=(",", ":"))
            )
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        command_fingerprint = manifest.get("command_fingerprint")
        if (
            upload_id.hex != stage_name
            or object_id.int == 0
            or issue8_receipt_id.int == 0
            or not isinstance(command_fingerprint, str)
            or len(command_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in command_fingerprint)
        ):
            raise StorageIntegrityIndeterminate()
        expected_digest = manifest.get("expected_digest")
        expected_length = manifest.get("expected_length")
        if expected_digest is None and expected_length is None:
            return
        if expected_digest is not None and (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_digest)
        ):
            raise StorageIntegrityIndeterminate()
        if expected_length is not None and (
            not isinstance(expected_length, int) or expected_length < 0
        ):
            raise StorageIntegrityIndeterminate()
        observed_digest = manifest.get("observed_digest")
        observed_length = manifest.get("observed_length")
        if observed_digest is None and observed_length is None:
            return
        if (
            not isinstance(observed_digest, str)
            or len(observed_digest) != 64
            or any(character not in "0123456789abcdef" for character in observed_digest)
            or not isinstance(observed_length, int)
            or observed_length < 0
            or observed_length > self._profile.max_object_bytes
        ):
            raise StorageIntegrityIndeterminate()

    def _iter_scope_directories(self, root: Path) -> Iterator[Path]:
        for entry in sorted(self._scandir(root), key=lambda item: item.name):
            if entry.is_symlink or not entry.is_dir:
                raise StorageIntegrityIndeterminate()
            if len(entry.name) != _SCOPE_HEX_LENGTH or any(
                character not in "0123456789abcdef" for character in entry.name
            ):
                raise StorageIntegrityIndeterminate()
            yield root / entry.name

    def _scandir(self, path: Path) -> list[_ScannedEntry]:
        try:
            fd = self._open_directory(path)
            try:
                with os.scandir(fd) as entries:
                    scanned: list[_ScannedEntry] = []
                    for entry in entries:
                        if len(scanned) >= self._profile.max_inventory_entries:
                            raise UploadLimitExceeded()
                        scanned.append(
                            _ScannedEntry(
                                name=entry.name,
                                is_symlink=entry.is_symlink(),
                                is_file=entry.is_file(follow_symlinks=False),
                                is_dir=entry.is_dir(follow_symlinks=False),
                            )
                        )
                    return scanned
            finally:
                os.close(fd)
        except FileNotFoundError:
            raise StorageIntegrityIndeterminate() from None
        except StorageError:
            raise
        except (TypeError, NotImplementedError, ValueError):
            raise StorageConfigurationError() from None
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

    def _mark_publication_conflict(
        self,
        path: Path,
        *,
        deadline: float,
        locked: bool = False,
    ) -> None:
        if not self._exists(path):
            return
        payload_fd: int | None = None
        try:
            if not locked:
                payload_path = path.parent / "payload"
                if self._exists(payload_path):
                    payload_fd = self._open_append(payload_path)
                    self._lock_file(payload_fd, deadline)
            current_manifest = self._read_manifest(path)
            if current_manifest.get("status") in {"staging", "publishing"}:
                self._set_manifest_status(path, current_manifest, "integrity_conflict")
        except BaseException:
            raise
        finally:
            if payload_fd is not None:
                self._close_fd(payload_fd)

    def _record_observed_content(
        self,
        payload_path: Path,
        manifest_path: Path,
        observed: ObservedContentIdentity,
        deadline: float,
    ) -> None:
        fd = self._open_append(payload_path)
        try:
            self._lock_file(fd, deadline)
            manifest = self._read_manifest(manifest_path)
            if manifest.get("status") != "staging":
                raise UploadConflict()
            actual = self._hash_file(
                payload_path,
                self._profile.max_object_bytes,
                deadline,
                None,
            )
            if actual != observed:
                raise ObjectCorrupt()
            updated = dict(manifest)
            updated["observed_digest"] = actual.observed_sha256
            updated["observed_length"] = actual.observed_byte_length
            self._write_json_atomic(manifest_path, updated)
        finally:
            self._close_fd(fd)

    async def _mark_interrupted_after_failure(
        self,
        path: Path,
        manifest: dict[str, Any],
        deadline: float,
    ) -> None:
        if self._monotonic_clock() >= deadline:
            self._indeterminate.set()
            return
        try:
            await self._run_blocking(
                self._mark_interrupted,
                path,
                manifest,
                deadline,
                deadline=deadline,
                check_operational=False,
            )
        except StorageError:
            self._indeterminate.set()

    async def _mark_integrity_conflict_after_failure(
        self,
        path: Path,
        manifest: dict[str, Any],
        deadline: float,
    ) -> None:
        if self._monotonic_clock() >= deadline:
            self._indeterminate.set()
            return
        try:
            await self._run_blocking(
                self._mark_interrupted,
                path,
                manifest,
                deadline,
                "integrity_conflict",
                deadline=deadline,
                check_operational=False,
            )
        except StorageError:
            self._indeterminate.set()

    def _mark_interrupted(
        self,
        path: Path,
        manifest: dict[str, Any],
        deadline: float,
        status: str = "interrupted",
    ) -> None:
        del manifest
        payload_path = path.parent / "payload"
        payload_fd: int | None = None
        try:
            if not self._exists(payload_path):
                current = self._read_manifest(path)
                if current.get("status") == "staging":
                    raise StorageIntegrityIndeterminate()
                return
            payload_fd = self._open_append(payload_path)
            self._lock_file(payload_fd, deadline)
            current = self._read_manifest(path)
            if current.get("status") != "staging":
                return
            observed = self._hash_file(
                payload_path,
                self._profile.max_object_bytes,
                deadline,
                None,
            )
            updated = dict(current)
            updated["observed_digest"] = observed.observed_sha256
            updated["observed_length"] = observed.observed_byte_length
            updated["status"] = status
            self._write_json_atomic(path, updated)
        except StorageError:
            raise StorageIntegrityIndeterminate() from None
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageIntegrityIndeterminate() from None
        finally:
            if payload_fd is not None:
                self._close_fd(payload_fd)

    def _receipt_from_manifest(
        self,
        manifest: dict[str, Any],
        manifest_path: Path | None = None,
    ) -> WriteReceipt:
        try:
            receipt = WriteReceipt.model_validate_json(
                json.dumps(manifest["receipt"], separators=(",", ":"))
            )
        except Exception:
            raise StorageIntegrityIndeterminate() from None
        if manifest.get("status") != "finalized":
            raise StorageIntegrityIndeterminate()
        if (
            manifest.get("upload_id") != str(receipt.upload_id)
            or manifest.get("object_id") != str(receipt.object_reference.object_id)
            or (
                manifest_path is not None
                and manifest_path.name
                != self._manifest_name(receipt.object_reference.object_id)
            )
        ):
            raise StorageIntegrityIndeterminate()
        return receipt

    def _publish_no_replace(
        self,
        source: Path,
        destination: Path,
        *,
        expected_source_identity: tuple[int, int, int] | None = None,
        deadline: float | None = None,
    ) -> int:
        if self._exists(destination):
            raise FileExistsError()
        publication_fd = self._open_directory(destination.parent)
        try:
            self._lock_publication_scope(
                publication_fd,
                destination,
                deadline if deadline is not None else self._setup_deadline(),
            )
            if destination.parent.parent == self._finalized_root:
                try:
                    object_id = UUID(destination.name)
                except (TypeError, ValueError):
                    object_id = None
                if object_id is not None:
                    deleted_status = self._deleted_object_status(object_id)
                    if deleted_status == "deleted":
                        raise UploadConflict()
                    if deleted_status == "pending":
                        raise StorageIntegrityIndeterminate()
            source_parent_fd, source_name = self._open_secure_parent(source)
            try:
                destination_parent_fd, destination_name = self._open_secure_parent(
                    destination
                )
                try:
                    os.link(
                        source_name,
                        destination_name,
                        src_dir_fd=source_parent_fd,
                        dst_dir_fd=destination_parent_fd,
                        follow_symlinks=False,
                    )
                    if expected_source_identity is not None and (
                        self._file_identity(destination) != expected_source_identity
                    ):
                        raise StorageIntegrityIndeterminate()
                except FileExistsError:
                    raise
                except (TypeError, NotImplementedError, ValueError):
                    raise StorageConfigurationError() from None
                except OSError as error:
                    if error.errno == errno.EEXIST:
                        raise FileExistsError() from None
                    raise
                finally:
                    os.close(destination_parent_fd)
            finally:
                os.close(source_parent_fd)
        except BaseException:
            self._close_fd(publication_fd)
            raise
        return publication_fd

    def _write_deleted_marker(
        self,
        reference: ObjectReference,
        *,
        status: str = "deleted",
        scope_key: str | None = None,
    ) -> None:
        marker = self._deleted_root / f"{reference.object_id.hex}.json"
        value: dict[str, Any] = {
            "version": 1,
            "status": status,
            "reference": reference.model_dump(mode="json"),
        }
        if scope_key is not None:
            if not self._is_scope_key(scope_key):
                raise StorageIntegrityIndeterminate()
            value["scope_key"] = scope_key
        self._write_json_atomic(marker, value)
        self._sync_directory(self._deleted_root)

    def _update_digest_from_path(self, path: Path, digest: Any, deadline: float) -> None:
        fd = self._open_read(path)
        try:
            while True:
                if self._monotonic_clock() > deadline:
                    raise UploadLimitExceeded()
                try:
                    chunk = os.read(
                        fd,
                        min(self._profile.read_chunk_bytes, self._profile.max_chunk_bytes),
                    )
                except (TypeError, NotImplementedError, ValueError):
                    raise StorageConfigurationError() from None
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            self._close_fd(fd)

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
            try:
                initial_stat = os.fstat(fd)
                if initial_stat.st_size > max_bytes:
                    raise UploadLimitExceeded()
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
            except OSError as error:
                raise self._read_error(error) from None
            while True:
                if self._monotonic_clock() > deadline:
                    raise UploadLimitExceeded()
                try:
                    chunk = os.read(
                        fd,
                        min(
                            self._profile.read_chunk_bytes,
                            self._profile.max_chunk_bytes,
                            max_bytes - length,
                        ),
                    )
                except (TypeError, NotImplementedError, ValueError):
                    raise StorageConfigurationError() from None
                except OSError as error:
                    raise self._read_error(error) from None
                if not chunk:
                    break
                length += len(chunk)
                if length > max_bytes:
                    self._observe_io(operation, max_bytes, length - len(chunk))
                    raise UploadLimitExceeded()
                digest.update(chunk)
            final_stat = os.fstat(fd)
            if (
                final_stat.st_dev != initial_stat.st_dev
                or final_stat.st_ino != initial_stat.st_ino
                or final_stat.st_size != initial_stat.st_size
                or final_stat.st_size != length
            ):
                raise ObjectCorrupt()
        except (TypeError, NotImplementedError, ValueError):
            raise StorageConfigurationError() from None
        except OSError as error:
            raise self._read_error(error) from None
        finally:
            self._close_fd(fd)
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
        expected_identity: tuple[int, int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        remaining = limit
        offset = 0
        while remaining:
            if self._monotonic_clock() > deadline:
                raise UploadLimitExceeded()
            async with self._operation_limit:
                self._ensure_operational()
                fd = await self._run_blocking(
                    self._open_read,
                    path,
                    on_cancel=os.close,
                    deadline=deadline,
                )
                owned_fd = _OwnedFd(fd)
                fd_abandoned = False
                try:
                    if expected_identity is not None:
                        try:
                            identity = await self._run_blocking(
                                self._file_identity_from_fd,
                                fd,
                                on_cancel=owned_fd.close,
                                deadline=deadline,
                            )
                        except (asyncio.CancelledError, _BlockingOperationAbandoned):
                            fd_abandoned = True
                            raise
                        if identity != expected_identity:
                            raise ObjectCorrupt()
                    try:
                        chunk = await self._run_blocking(
                            os.pread,
                            fd,
                            min(
                                self._profile.read_chunk_bytes,
                                self._profile.max_chunk_bytes,
                                remaining,
                            ),
                            offset,
                            on_cancel=owned_fd.close,
                            deadline=deadline,
                        )
                    except (asyncio.CancelledError, _BlockingOperationAbandoned):
                        fd_abandoned = True
                        raise
                finally:
                    if not fd_abandoned:
                        await self._run_blocking(
                            owned_fd.close,
                            check_operational=False,
                        )
            if not chunk:
                break
            remaining -= len(chunk)
            offset += len(chunk)
            self._observe_io("read", limit, limit - remaining)
            yield chunk

    def _validate_chunk(self, chunk: bytes, current_length: int) -> None:
        if not isinstance(chunk, bytes):
            raise UploadLimitExceeded()
        if len(chunk) > self._profile.max_chunk_bytes:
            raise UploadLimitExceeded()
        if current_length + len(chunk) > self._profile.max_object_bytes:
            raise UploadLimitExceeded()

    def _open_secure_parent(self, path: Path) -> tuple[int, str]:
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            raise StorageIntegrityIndeterminate() from None
        if not relative.parts:
            raise StorageIntegrityIndeterminate()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            if self._root_fd is None:
                fd = os.open(self._root, flags)
            else:
                fd = os.dup(self._root_fd)
        except (TypeError, NotImplementedError, ValueError):
            raise StorageConfigurationError() from None
        try:
            for component in relative.parts[:-1]:
                try:
                    child_fd = os.open(component, flags, dir_fd=fd)
                except (TypeError, NotImplementedError, ValueError):
                    raise StorageConfigurationError() from None
                os.close(fd)
                fd = child_fd
            return fd, relative.parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def _open_relative(self, path: Path, flags: int, mode: int = 0o600) -> int:
        parent_fd, name = self._open_secure_parent(path)
        try:
            try:
                return os.open(name, flags, mode, dir_fd=parent_fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
        finally:
            os.close(parent_fd)

    def _open_directory(self, path: Path) -> int:
        try:
            if path == self._root:
                return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except (TypeError, NotImplementedError, ValueError):
            raise StorageConfigurationError() from None
        return self._open_relative(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _lstat(self, path: Path) -> os.stat_result:
        parent_fd, name = self._open_secure_parent(path)
        try:
            try:
                return os.lstat(name, dir_fd=parent_fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
        finally:
            os.close(parent_fd)

    def _is_symlink(self, path: Path) -> bool:
        return stat.S_ISLNK(self._lstat(path).st_mode)

    def _is_directory(self, path: Path) -> bool:
        return stat.S_ISDIR(self._lstat(path).st_mode)

    def _open_append(self, path: Path) -> int:
        try:
            return self._open_relative(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
            )
        except OSError as error:
            if error.errno == getattr(os, "ELOOP", 40):
                raise StorageIntegrityIndeterminate() from None
            raise self._configuration_or_unavailable(error) from None

    def _lock_file(self, fd: int, deadline: float) -> None:
        try:
            import fcntl
        except ImportError:
            raise StorageConfigurationError() from None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if self._monotonic_clock() >= deadline:
                    raise UploadLimitExceeded()
                time.sleep(min(0.01, max(0.0, deadline - self._monotonic_clock())))
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise StorageIntegrityIndeterminate() from None
                if self._monotonic_clock() >= deadline:
                    raise UploadLimitExceeded()
                time.sleep(min(0.01, max(0.0, deadline - self._monotonic_clock())))

    def _lock_publication_scope(self, fd: int, destination: Path, deadline: float) -> None:
        try:
            import fcntl
        except ImportError:
            raise StorageConfigurationError() from None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if self._exists(destination):
                    raise FileExistsError()
                if self._monotonic_clock() >= deadline:
                    raise UploadLimitExceeded()
                time.sleep(min(0.01, max(0.0, deadline - self._monotonic_clock())))
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise StorageIntegrityIndeterminate() from None
                if self._exists(destination):
                    raise FileExistsError()
                if self._monotonic_clock() >= deadline:
                    raise UploadLimitExceeded()
                time.sleep(min(0.01, max(0.0, deadline - self._monotonic_clock())))
    def _write_chunk(self, fd: int, chunk: bytes, deadline: float) -> None:
        view = memoryview(chunk)
        while view:
            if self._monotonic_clock() > deadline:
                raise UploadLimitExceeded()
            try:
                written = os.write(fd, view)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
            view = view[written:]
        try:
            os.fsync(fd)
        except (TypeError, NotImplementedError, ValueError):
            raise StorageConfigurationError() from None

    def _open_read(self, path: Path) -> int:
        fd: int | None = None
        try:
            fd = self._open_relative(path, os.O_RDONLY | os.O_NOFOLLOW)
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
            if not regular:
                raise StorageIntegrityIndeterminate()
            return fd
        except StorageError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    self._indeterminate.set()
            raise
        except OSError as error:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    self._indeterminate.set()
            raise self._read_error(error) from None
        except (TypeError, NotImplementedError, ValueError):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    self._indeterminate.set()
            raise StorageConfigurationError() from None

    def _regular_file_size(self, path: Path) -> int:
        fd = self._open_read(path)
        try:
            try:
                return os.fstat(fd).st_size
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
        finally:
            os.close(fd)

    def _file_identity(self, path: Path) -> tuple[int, int, int]:
        fd = self._open_read(path)
        try:
            return self._file_identity_from_fd(fd)
        finally:
            self._close_fd(fd)

    @staticmethod
    def _file_identity_from_fd(fd: int) -> tuple[int, int, int]:
        try:
            stat_result = os.fstat(fd)
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageConfigurationError() from None
        return stat_result.st_dev, stat_result.st_ino, stat_result.st_size

    def _sync_file(
        self,
        path: Path,
        *,
        expected_identity: tuple[int, int, int] | None = None,
    ) -> None:
        fd = self._open_read(path)
        try:
            if expected_identity is not None and self._file_identity_from_fd(fd) != expected_identity:
                raise StorageIntegrityIndeterminate()
            os.fsync(fd)
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageIntegrityIndeterminate() from None
        finally:
            os.close(fd)

    def _sync_directory(self, path: Path) -> None:
        try:
            fd = self._open_directory(path)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageIntegrityIndeterminate() from None

    def _prepare_root(self, configured_root: str | os.PathLike[str]) -> Path:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise StorageConfigurationError()
        try:
            root = Path(os.path.abspath(os.fspath(configured_root)))
            if "\x00" in os.fspath(root) or not root.is_absolute():
                raise StorageConfigurationError()
            if any(component in {"", ".", ".."} for component in root.parts[1:]):
                raise StorageConfigurationError()
            return root
        except StorageError:
            raise
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageConfigurationError() from None
        except RuntimeError:
            raise StorageConfigurationError() from None

    def _prepare_directory(self, path: Path) -> Path:
        created = False
        try:
            if self._exists(path) and self._is_symlink(path):
                raise StorageConfigurationError()
            parent_fd, name = self._open_secure_parent(path)
            try:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    created = True
                except (TypeError, NotImplementedError, ValueError):
                    raise StorageConfigurationError() from None
            except FileExistsError:
                pass
            finally:
                os.close(parent_fd)
            fd = self._open_directory(path)
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
            if created and path.parent == self._root:
                self._sync_directory(self._root)
            return path
        except StorageError:
            raise
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageConfigurationError() from None

    def _ensure_scope_directory(self, root: Path, scope_key: str) -> Path:
        path = root / scope_key
        created = False
        try:
            if not self._is_scope_key(scope_key):
                raise StorageIntegrityIndeterminate()
            if self._exists(path) and self._is_symlink(path):
                raise StorageIntegrityIndeterminate()
            parent_fd, name = self._open_secure_parent(path)
            try:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    created = True
                except (TypeError, NotImplementedError, ValueError):
                    raise StorageConfigurationError() from None
            except FileExistsError:
                pass
            finally:
                os.close(parent_fd)
            fd = self._open_directory(path)
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
            if created:
                self._sync_directory(root)
        except StorageError:
            raise
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageConfigurationError() from None
        return path

    @staticmethod
    def _assert_directory(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise StorageConfigurationError()

    def _mkdir_exclusive(self, path: Path) -> None:
        parent_fd, name = self._open_secure_parent(path)
        try:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
            created = True
        except FileExistsError:
            raise
        finally:
            os.close(parent_fd)
        if created:
            self._sync_directory(path.parent)

    def _create_empty_payload(self, path: Path) -> None:
        try:
            fd = self._open_relative(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            )
        except (TypeError, NotImplementedError, ValueError):
            raise StorageConfigurationError() from None
        try:
            try:
                os.fsync(fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
        finally:
            os.close(fd)

    def _exists(self, path: Path) -> bool:
        try:
            self._lstat(path)
            return True
        except FileNotFoundError:
            return False
        except (TypeError, NotImplementedError, ValueError, OSError):
            raise StorageIntegrityIndeterminate() from None

    def _unlink_nofollow(
        self,
        path: Path,
        *,
        expected_identity: tuple[int, int, int] | None = None,
    ) -> None:
        parent_fd, name = self._open_secure_parent(path)
        try:
            try:
                entry = os.lstat(name, dir_fd=parent_fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
            if stat.S_ISLNK(entry.st_mode):
                raise StorageIntegrityIndeterminate()
            if expected_identity is not None and (
                entry.st_dev,
                entry.st_ino,
                entry.st_size,
            ) != expected_identity:
                raise StorageIntegrityIndeterminate()
            try:
                os.unlink(name, dir_fd=parent_fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
        finally:
            os.close(parent_fd)

    def _rmdir_nofollow(self, path: Path) -> None:
        parent_fd, name = self._open_secure_parent(path)
        try:
            try:
                entry = os.lstat(name, dir_fd=parent_fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise StorageIntegrityIndeterminate()
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
        finally:
            os.close(parent_fd)

    def _write_json_exclusive(self, path: Path, value: dict[str, Any]) -> None:
        payload = self._json_bytes(value)
        fd = self._open_relative(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        )
        try:
            self._write_all(fd, payload)
            try:
                os.fsync(fd)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
        finally:
            os.close(fd)
        self._sync_directory(path.parent)

    def _write_json_atomic(self, path: Path, value: dict[str, Any]) -> None:
        if self._exists(path) and self._is_symlink(path):
            raise StorageIntegrityIndeterminate()
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            self._write_json_exclusive(temporary, value)
        except StorageError:
            raise
        except (TypeError, NotImplementedError, OSError):
            raise StorageIntegrityIndeterminate() from None
        try:
            parent_fd, name = self._open_secure_parent(path)
            try:
                os.replace(
                    temporary.name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            finally:
                os.close(parent_fd)
            self._sync_directory(path.parent)
        except (TypeError, NotImplementedError, ValueError, OSError):
            try:
                self._unlink_nofollow(temporary)
            except (StorageError, TypeError, NotImplementedError, OSError):
                pass
            raise StorageIntegrityIndeterminate() from None

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            try:
                written = os.write(fd, view)
            except (TypeError, NotImplementedError, ValueError):
                raise StorageConfigurationError() from None
            view = view[written:]

    def _read_manifest(self, path: Path) -> dict[str, Any]:
        fd = self._open_read(path)
        try:
            try:
                raw = os.read(fd, 128 * 1024)
            except (TypeError, NotImplementedError, ValueError, OSError):
                raise StorageIntegrityIndeterminate() from None
        finally:
            self._close_fd(fd)
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

    def _assert_manifest_scope(self, manifest: dict[str, Any], scope_dir: Path) -> None:
        if not self._is_scope_key(scope_dir.name) or manifest.get("scope_key") != scope_dir.name:
            raise StorageIntegrityIndeterminate()

    def _scope_key_from_state_scope(self, state: UploadCommandState) -> str:
        location = self._locate_upload(state.upload_id)
        if location is None:
            raise UploadNotFound()
        return self._actual_scope_for_location(location)

    def _actual_scope_for_location(
        self,
        location: tuple[Literal["staged", "finalized"], Path, Path, dict[str, Any]],
    ) -> str:
        kind, _, manifest_path, manifest = location
        scope_key = (
            manifest_path.parent.parent.name
            if kind == "staged"
            else manifest_path.parent.name
        )
        if not self._is_scope_key(scope_key) or manifest.get("scope_key") != scope_key:
            raise StorageIntegrityIndeterminate()
        return scope_key

    @staticmethod
    def _is_scope_key(value: object) -> bool:
        return isinstance(value, str) and len(value) == _SCOPE_HEX_LENGTH and all(
            character in "0123456789abcdef" for character in value
        )

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

    async def _ensure_ready(self) -> None:
        self._ensure_operational()
        if self._initialized:
            return
        async with self._ready_lock:
            self._ensure_operational()
            if self._initialized:
                return
            root, staged, finalized, deleted, root_fd = await self._run_blocking(
                self._initialize_layout,
                on_cancel=self._close_layout_fd,
                deadline=self._setup_deadline(),
            )
            self._root = root
            self._staged_root = staged
            self._finalized_root = finalized
            self._deleted_root = deleted
            self._root_fd = root_fd
            self._root_fd_finalizer = finalize(self, os.close, root_fd)
            self._initialized = True

    def _initialize_layout(self) -> tuple[Path, Path, Path, Path, int]:
        root = self._prepare_root(self._profile.root)
        self._root = root
        root_fd = self._open_or_create_root(root)
        self._root_fd = root_fd
        try:
            staged = self._prepare_directory(root / "staged")
            finalized = self._prepare_directory(root / "finalized")
            deleted = self._prepare_directory(root / "deleted")
        except BaseException:
            self._close_fd(root_fd)
            self._root_fd = None
            raise
        return root, staged, finalized, deleted, root_fd

    def _open_or_create_root(self, root: Path) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd: int | None = None
        root_fd: int | None = None
        try:
            parent_fd = os.open(root.anchor, flags)
            parts = root.parts[1:]
            if not parts:
                raise StorageConfigurationError()
            for component in parts[:-1]:
                try:
                    child_fd = os.open(component, flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                        child_fd = os.open(component, flags, dir_fd=parent_fd)
                    except FileExistsError:
                        child_fd = os.open(component, flags, dir_fd=parent_fd)
                try:
                    os.close(parent_fd)
                except OSError:
                    os.close(child_fd)
                    raise StorageConfigurationError() from None
                parent_fd = child_fd
            name = parts[-1]
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            root_fd = os.open(name, flags, dir_fd=parent_fd)
            os.fchmod(root_fd, 0o700)
            try:
                os.close(parent_fd)
            except OSError:
                os.close(root_fd)
                root_fd = None
                raise StorageConfigurationError() from None
            parent_fd = None
            return root_fd
        except StorageError:
            raise
        except (TypeError, NotImplementedError, ValueError, OSError):
            if root_fd is not None:
                try:
                    os.close(root_fd)
                except OSError:
                    pass
            raise StorageConfigurationError() from None
        finally:
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass

    def _deadline(self) -> float:
        return self._monotonic_clock() + self._profile.operation_timeout

    def _setup_deadline(self) -> float:
        return self._monotonic_clock() + max(self._profile.operation_timeout, 1.0)

    @staticmethod
    def _close_fd(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            raise StorageIntegrityIndeterminate() from None

    def _close_layout_fd(self, value: Any) -> None:
        try:
            self._close_fd(value[4])
        except (IndexError, KeyError, TypeError):
            self._indeterminate.set()

    async def _run_blocking(
        self,
        function: Callable[..., Any],
        *args: Any,
        on_cancel: Callable[[Any], None] | None = None,
        deadline: float | None = None,
        check_operational: bool = True,
    ) -> Any:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spine-storage")

        def invoke() -> Any:
            if check_operational:
                self._ensure_operational()
            return function(*args)

        result = executor.submit(invoke)

        def abandon() -> None:
            self._indeterminate.set()

            def drain() -> None:
                try:
                    value = result.result()
                except BaseException:
                    return
                if on_cancel is not None:
                    try:
                        on_cancel(value)
                    except BaseException:
                        self._indeterminate.set()

            threading.Thread(target=drain, daemon=True).start()

        try:
            while not result.done():
                if deadline is not None and self._monotonic_clock() >= deadline:
                    abandon()
                    raise _BlockingOperationAbandoned()
                await asyncio.sleep(0.001)
            try:
                value = result.result()
            except StorageIntegrityIndeterminate:
                self._indeterminate.set()
                raise
            if deadline is not None and self._monotonic_clock() >= deadline:
                self._indeterminate.set()
                if on_cancel is not None:
                    try:
                        on_cancel(value)
                    except BaseException:
                        self._indeterminate.set()
                raise _BlockingOperationAbandoned()
            if check_operational:
                try:
                    self._ensure_operational()
                except BaseException:
                    if on_cancel is not None:
                        try:
                            on_cancel(value)
                        except BaseException:
                            self._indeterminate.set()
                    raise
            return value
        except asyncio.CancelledError:
            if result.done():
                try:
                    value = result.result()
                    if on_cancel is not None:
                        try:
                            on_cancel(value)
                        except BaseException:
                            self._indeterminate.set()
                except BaseException:
                    self._indeterminate.set()
            else:
                abandon()
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _ensure_operational(self) -> None:
        if self._indeterminate.is_set():
            raise StorageIntegrityIndeterminate()

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

    def _inventory_cursor(self, cursor: str | None) -> str | None:
        if cursor is None:
            return None
        try:
            encoded = cursor.encode("ascii")
            decoded = base64.urlsafe_b64decode(encoded).decode(_JSON_ENCODING)
            version, raw_value = decoded.split(":", 1)
            if version != "v1":
                raise ValueError()
            scope_key, manifest_name = raw_value.split("/", 1)
            if (
                not self._is_scope_key(scope_key)
                or len(manifest_name) != _OPAQUE_NAME_LENGTH + len(".manifest.json")
                or not manifest_name.endswith(".manifest.json")
                or any(
                    character not in "0123456789abcdef"
                    for character in manifest_name[:_OPAQUE_NAME_LENGTH]
                )
            ):
                raise ValueError()
            return raw_value
        except (TypeError, ValueError, UnicodeError, binascii.Error):
            raise StorageConfigurationError() from None

    @staticmethod
    def _encode_inventory_cursor(value: str) -> str:
        return base64.urlsafe_b64encode(f"v1:{value}".encode(_JSON_ENCODING)).decode("ascii")

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
        if error.errno in {
            getattr(os, "ELOOP", 40),
            getattr(os, "ENOTSUP", 95),
            getattr(os, "EINVAL", 22),
            getattr(os, "EXDEV", 18),
        }:
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
