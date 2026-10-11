from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    DeletionApproval,
    FilesystemOriginalObjectStore,
    FilesystemStorageProfile,
    ObjectCorrupt,
    ObjectNotFound,
    ObjectUnavailable,
    OriginalUploadCommandInput,
    PreWriteRequestDigest,
    StorageConfigurationError,
    StorageIntegrityIndeterminate,
    StorageError,
    UploadCommandState,
    UploadCommandStateName,
    UploadConflict,
    UploadLimitExceeded,
    WriteReceipt,
)


NOW = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("20000000-0000-4000-8000-000000000001")
OBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
RECEIPT_ID = UUID("40000000-0000-4000-8000-000000000001")
SOURCE_REVISION_ID = UUID("70000000-0000-4000-8000-000000000001")
CONTENT = b"synthetic original\n"


@pytest.fixture
def filesystem_store(tmp_path: Path) -> tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry]:
    registry = AuthorizedReadGrantRegistry()
    store = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(
            root=tmp_path,
            max_chunk_bytes=8,
            max_object_bytes=64,
            read_chunk_bytes=3,
            max_concurrent_operations=2,
            inactivity_timeout=0.2,
            operation_timeout=1.0,
        ),
        grant_registry=registry,
        generation_factory=lambda: UUID("50000000-0000-4000-8000-000000000001"),
        clock=lambda: NOW,
    )
    return store, registry


def command(
    content: bytes = CONTENT,
    *,
    filename: str | None = None,
    media_type: str | None = None,
    expected_sha256: str | None = None,
    expected_length: int | None = None,
) -> OriginalUploadCommandInput:
    return OriginalUploadCommandInput(
        operation_name="original.upload",
        operation_schema_version=1,
        idempotency_key="filesystem-test",
        workspace_id=WORKSPACE_ID,
        expected_sha256=(expected_sha256 or hashlib.sha256(content).hexdigest()),
        expected_byte_length=(len(content) if expected_length is None else expected_length),
        declared_filename=filename,
        declared_media_type=media_type,
        purpose="document.upload",
    )


def state() -> UploadCommandState:
    return UploadCommandState(
        issue8_receipt_id=RECEIPT_ID,
        request_digest=PreWriteRequestDigest(
            algorithm="sha256",
            algorithm_version="spine.command-digest.v1",
            digest_hex="a" * 64,
        ),
        upload_id=UPLOAD_ID,
        object_id=OBJECT_ID,
        state=UploadCommandStateName.STAGING,
    )


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def split_chunks(content: bytes, size: int) -> tuple[bytes, ...]:
    return tuple(content[offset : offset + size] for offset in range(0, len(content), size))


async def finalize(
    store: FilesystemOriginalObjectStore,
    upload_command: OriginalUploadCommandInput = command(),
    upload_state: UploadCommandState = state(),
) -> WriteReceipt:
    active = await store.begin_or_resume_upload(upload_command, upload_state)
    observed = await store.write_chunks(active.upload_id, chunks(*split_chunks(CONTENT, 8)))
    return await store.finalize_upload(active, observed)


async def read_receipt(
    store: FilesystemOriginalObjectStore,
    registry: AuthorizedReadGrantRegistry,
    receipt: WriteReceipt,
    *,
    max_bytes: int = 64,
) -> bytes:
    grant = registry.issue(
        workspace_id=WORKSPACE_ID,
        environment_id=None,
        source_revision_id=SOURCE_REVISION_ID,
        object_reference=receipt.object_reference,
        purpose="read.original",
        authorization_decision_version="policy-v1",
        expires_at=NOW + timedelta(minutes=1),
    )
    lease = registry.consume(
        grant,
        workspace_id=WORKSPACE_ID,
        environment_id=None,
        source_revision_id=SOURCE_REVISION_ID,
        object_reference=receipt.object_reference,
        now=NOW,
    )
    return b"".join(
        [part async for part in store.open_bounded_read(lease, max_bytes=max_bytes)]
    )


def finalized_payload(root: Path) -> Path:
    payloads = list((root / "finalized").rglob("[0-9a-f]*"))
    payloads = [path for path in payloads if path.is_file() and not path.name.endswith(".json")]
    assert len(payloads) == 1
    return payloads[0]


async def test_concurrent_finalizers_publish_one_complete_immutable_object(
    tmp_path: Path,
) -> None:
    command_value = command()
    state_value = state()
    second_state = state().model_copy(
        update={
            "upload_id": UUID("20000000-0000-4000-8000-000000000002"),
            "issue8_receipt_id": UUID("40000000-0000-4000-8000-000000000002"),
        }
    )
    first_registry = AuthorizedReadGrantRegistry()
    second_registry = AuthorizedReadGrantRegistry()
    first = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=tmp_path, max_chunk_bytes=8, max_object_bytes=64),
        grant_registry=first_registry,
        generation_factory=lambda: UUID("50000000-0000-4000-8000-000000000001"),
        clock=lambda: NOW,
    )
    second = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=tmp_path, max_chunk_bytes=8, max_object_bytes=64),
        grant_registry=second_registry,
        generation_factory=lambda: UUID("50000000-0000-4000-8000-000000000002"),
        clock=lambda: NOW,
    )
    publication_barrier = threading.Barrier(2)
    first_publish = first._publish_no_replace
    second_publish = second._publish_no_replace

    def first_racing_publish(
        source: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        publication_barrier.wait(timeout=2.0)
        first_publish(source, destination, **kwargs)

    def second_racing_publish(
        source: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        publication_barrier.wait(timeout=2.0)
        second_publish(source, destination, **kwargs)

    first._publish_no_replace = first_racing_publish
    second._publish_no_replace = second_racing_publish
    await first.begin_or_resume_upload(command_value, state_value)
    await second.begin_or_resume_upload(command_value, second_state)
    first_observed = await first.write_chunks(UPLOAD_ID, chunks(*split_chunks(CONTENT, 8)))
    second_observed = await second.write_chunks(
        second_state.upload_id,
        chunks(*split_chunks(CONTENT, 8)),
    )

    receipts = await asyncio.gather(
        first.finalize_upload(state_value, first_observed),
        second.finalize_upload(second_state, second_observed),
        return_exceptions=True,
    )

    assert sum(isinstance(receipt, WriteReceipt) for receipt in receipts) == 1
    assert sum(isinstance(receipt, StorageError) for receipt in receipts) == 1
    winner = first if isinstance(receipts[0], WriteReceipt) else second
    inventory, cursor = await winner.reconciliation_inventory()
    assert len(inventory) == 1
    assert cursor is None
    assert finalized_payload(tmp_path).read_bytes() == CONTENT


async def test_symlink_and_untrusted_metadata_fail_closed_without_escape(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
    tmp_path: Path,
) -> None:
    store, _ = filesystem_store
    unsafe = command(
        filename="../../outside.txt",
        media_type="text/plain",
    )
    active = await store.begin_or_resume_upload(unsafe, state())
    staged_payload = next((tmp_path / "staged").rglob("payload"))
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"sentinel")
    staged_payload.unlink()
    staged_payload.symlink_to(outside)

    with pytest.raises(StorageIntegrityIndeterminate):
        await store.write_chunks(active.upload_id, chunks(CONTENT))

    assert outside.read_bytes() == b"sentinel"
    with pytest.raises(StorageIntegrityIndeterminate):
        await store.reconciliation_inventory()


async def test_corrupt_and_missing_finalized_bytes_fail_closed(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
) -> None:
    store, registry = filesystem_store
    receipt = await finalize(store)
    payload = finalized_payload(store._root)
    payload.write_bytes(b"corrupt")

    with pytest.raises(ObjectCorrupt):
        await store.verify(receipt.object_reference, max_bytes=64)
    with pytest.raises(ObjectCorrupt):
        await read_receipt(store, registry, receipt)
    with pytest.raises(ObjectCorrupt):
        await read_receipt(store, registry, receipt, max_bytes=3)

    payload.unlink()
    with pytest.raises(ObjectNotFound):
        await store.verify(receipt.object_reference, max_bytes=64)


async def test_finalized_retry_producer_failure_is_adapter_neutral(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
) -> None:
    store, _ = filesystem_store
    receipt = await finalize(store)

    async def broken_stream() -> AsyncIterator[bytes]:
        raise RuntimeError("synthetic producer failure")
        yield b"unreachable"

    with pytest.raises(ObjectUnavailable):
        await store.write_chunks(receipt.upload_id, broken_stream())


async def test_verify_never_reads_beyond_profile_object_limit(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
) -> None:
    store, _ = filesystem_store
    receipt = await finalize(store)
    payload = finalized_payload(store._root)
    payload.write_bytes(CONTENT + (b"x" * 100))

    with pytest.raises(UploadLimitExceeded):
        await store.verify(receipt.object_reference, max_bytes=1024)


async def test_publishing_manifest_is_indeterminate_and_not_readable(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
) -> None:
    store, _ = filesystem_store
    receipt = await finalize(store)
    manifest = next((store._root / "finalized").rglob("*.manifest.json"))
    value = json.loads(manifest.read_text())
    value["status"] = "publishing"
    manifest.write_text(json.dumps(value))

    with pytest.raises(StorageIntegrityIndeterminate):
        await store.verify(receipt.object_reference, max_bytes=64)
    with pytest.raises(StorageIntegrityIndeterminate):
        await store.reconciliation_inventory()


async def test_stale_reference_cannot_prepare_deletion(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
) -> None:
    store, _ = filesystem_store
    receipt = await finalize(store)
    stale = receipt.object_reference.model_copy(
        update={"storage_generation": UUID("50000000-0000-4000-8000-000000000099")}
    )
    approval = DeletionApproval(
        command_id=UUID("80000000-0000-4000-8000-000000000001"),
        approval_reference=UUID("90000000-0000-4000-8000-000000000001"),
    )

    with pytest.raises(UploadConflict):
        await store.prepare_deletion(stale, approval)

    result = await store.verify(receipt.object_reference, max_bytes=64)
    assert result.observed_length == len(CONTENT)


async def test_deleted_object_identity_cannot_be_republished(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
) -> None:
    store, _ = filesystem_store
    receipt = await finalize(store)
    approval = DeletionApproval(
        command_id=UUID("80000000-0000-4000-8000-000000000001"),
        approval_reference=UUID("90000000-0000-4000-8000-000000000001"),
    )
    await store.prepare_deletion(receipt.object_reference, approval)
    await store.delete_controlled(receipt.object_reference, approval)

    replacement_state = state().model_copy(
        update={
            "upload_id": UUID("20000000-0000-4000-8000-000000000002"),
            "issue8_receipt_id": UUID("40000000-0000-4000-8000-000000000002"),
        }
    )
    with pytest.raises(UploadConflict):
        await store.begin_or_resume_upload(
            command().model_copy(update={"idempotency_key": "replacement"}),
            replacement_state,
        )


async def test_tampered_staged_scope_is_rejected_without_escape(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
    tmp_path: Path,
) -> None:
    store, _ = filesystem_store
    active = await store.begin_or_resume_upload(command(), state())
    observed = await store.write_chunks(
        active.upload_id,
        chunks(*split_chunks(CONTENT, 8)),
    )
    manifest = next((tmp_path / "staged").rglob("manifest.json"))
    value = json.loads(manifest.read_text())
    value["scope_key"] = "../escape"
    manifest.write_text(json.dumps(value))

    with pytest.raises(StorageIntegrityIndeterminate):
        await store.finalize_upload(active, observed)

    assert not (tmp_path / "escape").exists()


async def test_publication_failure_after_hardlink_requires_reconciliation(
    tmp_path: Path,
) -> None:
    registry = AuthorizedReadGrantRegistry()
    store = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=tmp_path, max_chunk_bytes=8, max_object_bytes=64),
        grant_registry=registry,
        clock=lambda: NOW,
    )
    active = await store.begin_or_resume_upload(command(), state())
    observed = await store.write_chunks(
        active.upload_id,
        chunks(*split_chunks(CONTENT, 8)),
    )
    original_sync_file = store._sync_file

    def fail_after_file_sync(path: Path, **kwargs: object) -> None:
        original_sync_file(path, **kwargs)
        raise OSError("synthetic publication failure")

    store._sync_file = fail_after_file_sync
    with pytest.raises(ObjectUnavailable):
        await store.finalize_upload(active, observed)

    restarted = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=tmp_path, max_chunk_bytes=8, max_object_bytes=64),
        grant_registry=AuthorizedReadGrantRegistry(),
        clock=lambda: NOW,
    )
    with pytest.raises(StorageIntegrityIndeterminate):
        await restarted.reconciliation_inventory()


async def test_abort_cannot_be_overwritten_by_a_stale_writer(
    tmp_path: Path,
) -> None:
    first = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=tmp_path, max_chunk_bytes=8, max_object_bytes=64),
        grant_registry=AuthorizedReadGrantRegistry(),
        clock=lambda: NOW,
    )
    second = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=tmp_path, max_chunk_bytes=8, max_object_bytes=64),
        grant_registry=AuthorizedReadGrantRegistry(),
        clock=lambda: NOW,
    )
    active = await first.begin_or_resume_upload(command(), state())
    append_finished = asyncio.Event()
    release_record = asyncio.Event()
    original_append = first._append_stream

    async def delayed_append(*args: object, **kwargs: object) -> object:
        observed = await original_append(*args, **kwargs)
        append_finished.set()
        await release_record.wait()
        return observed

    first._append_stream = delayed_append
    writer = asyncio.create_task(
        first.write_chunks(active.upload_id, chunks(*split_chunks(CONTENT, 8)))
    )
    await append_finished.wait()
    await second.abort_upload(active)
    release_record.set()

    with pytest.raises(ObjectUnavailable):
        await writer
    staged_manifest = next((tmp_path / "staged").rglob("manifest.json"))
    assert json.loads(staged_manifest.read_text())["status"] == "aborted"
    assert not staged_manifest.with_name("payload").exists()


async def test_interrupted_upload_is_resumable_after_adapter_restart(
    tmp_path: Path,
) -> None:
    registry = AuthorizedReadGrantRegistry()
    profile = FilesystemStorageProfile(
        root=tmp_path,
        max_chunk_bytes=8,
        max_object_bytes=64,
        inactivity_timeout=0.01,
        operation_timeout=1.0,
    )
    first = FilesystemOriginalObjectStore(profile, grant_registry=registry, clock=lambda: NOW)
    active = await first.begin_or_resume_upload(command(), state())

    async def interrupted() -> AsyncIterator[bytes]:
        yield b"prefix"
        await asyncio.sleep(0.1)
        yield b"never reached"

    with pytest.raises(UploadLimitExceeded):
        await first.write_chunks(active.upload_id, interrupted())

    restarted = FilesystemOriginalObjectStore(profile, grant_registry=registry, clock=lambda: NOW)
    resumed = await restarted.begin_or_resume_upload(
        command().model_copy(update={"idempotency_key": "filesystem-test"}),
        state().transitioned(UploadCommandStateName.INTERRUPTED),
    )
    assert resumed.state is UploadCommandStateName.STAGING


async def test_total_operation_timeout_interrupts_a_slow_stream(tmp_path: Path) -> None:
    registry = AuthorizedReadGrantRegistry()
    store = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(
            root=tmp_path,
            max_chunk_bytes=8,
            max_object_bytes=64,
            inactivity_timeout=1.0,
            operation_timeout=0.2,
        ),
        grant_registry=registry,
        clock=lambda: NOW,
    )
    active = await store.begin_or_resume_upload(command(), state())

    async def slow_stream() -> AsyncIterator[bytes]:
        yield b"prefix"
        await asyncio.sleep(0.3)
        yield b"late"

    with pytest.raises(UploadLimitExceeded):
        await store.write_chunks(active.upload_id, slow_stream())


async def test_filename_and_mime_never_become_physical_storage_identity(
    filesystem_store: tuple[FilesystemOriginalObjectStore, AuthorizedReadGrantRegistry],
) -> None:
    store, _ = filesystem_store
    receipt = await finalize(
        store,
        upload_command=command(
            filename="../../customer-secret.pdf",
            media_type="text/plain",
        ),
    )

    assert receipt.object_reference.object_id == OBJECT_ID
    inventory, _ = await store.reconciliation_inventory()
    assert len(inventory) == 1
    assert "customer-secret.pdf" not in repr(inventory)


async def test_operation_concurrency_limit_applies_backpressure(
    tmp_path: Path,
) -> None:
    registry = AuthorizedReadGrantRegistry()
    store = FilesystemOriginalObjectStore(
        FilesystemStorageProfile(
            root=tmp_path,
            max_chunk_bytes=8,
            max_object_bytes=64,
            max_concurrent_operations=1,
            inactivity_timeout=1.0,
            operation_timeout=2.0,
        ),
        grant_registry=registry,
        clock=lambda: NOW,
    )
    active = await store.begin_or_resume_upload(command(), state())
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_stream() -> AsyncIterator[bytes]:
        started.set()
        yield b"prefix"
        await release.wait()

    writer = asyncio.create_task(store.write_chunks(active.upload_id, blocked_stream()))
    await started.wait()
    inventory = asyncio.create_task(store.reconciliation_inventory())
    await asyncio.sleep(0)
    assert not inventory.done()
    release.set()
    await writer
    assert (await inventory)[0] == ()


async def test_symlinked_storage_root_is_unsupported(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "root-link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(StorageConfigurationError):
        store = FilesystemOriginalObjectStore(
            FilesystemStorageProfile(root=link),
            grant_registry=AuthorizedReadGrantRegistry(),
        )
        await store.begin_or_resume_upload(command(), state())
