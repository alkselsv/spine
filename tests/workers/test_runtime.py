from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import pytest

from spine.application.outbox.dispatch import DispatchReport
from spine.application.persistence.projection import PROJECTION_RECEIPT_OPERATION
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.memory.cognee.indexing import CogneeChunkProjectionStore
from spine.workers.runtime import (
    OutboxWorkerSettings,
    _publication_attempt_identity,
    _projection_configuration_id,
    _projection_receipt_context,
)

from tests.application.test_projection_result_mapping import _command


class RecordingWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.failure: Exception | None = None

    async def dispatch_batch(self, *, limit: int, worker_identity: str):
        del limit, worker_identity
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return DispatchReport(
            claimed=1,
            delivered=1,
            retry_scheduled=0,
            quarantined=0,
            outcome_codes=(),
        )


class RecordingEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def outbox_runtime(worker: RecordingWorker, *, shutdown_timeout_seconds: float = 1.0):
    from spine.workers.runtime import ProductionOutboxWorkerRuntime

    settings = OutboxWorkerSettings(
        service_principal_id=UUID(int=500),
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )
    return ProductionOutboxWorkerRuntime(
        worker=worker,
        settings=settings,
        runtime_engine=RecordingEngine(),
        dispatcher_engine=RecordingEngine(),
    )


@pytest.mark.asyncio
async def test_shutdown_stops_claiming_after_current_dispatch_batch_drains() -> None:
    worker = RecordingWorker()
    runtime = outbox_runtime(worker)
    stop = asyncio.Event()

    task = asyncio.create_task(runtime.run_forever(stop))
    await worker.started.wait()
    stop.set()
    worker.release.set()
    await asyncio.wait_for(task, timeout=1)
    await runtime.close()

    assert worker.calls == 1
    assert runtime.runtime_engine.disposed is True
    assert runtime.dispatcher_engine.disposed is True


@pytest.mark.asyncio
async def test_shutdown_bounds_dispatch_drain_and_cancels_in_flight_batch() -> None:
    worker = RecordingWorker()
    runtime = outbox_runtime(worker, shutdown_timeout_seconds=0.01)
    stop = asyncio.Event()

    task = asyncio.create_task(runtime.run_forever(stop))
    await worker.started.wait()
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert worker.calls == 1
    assert task.done()


@pytest.mark.asyncio
async def test_shutdown_propagates_dispatch_failure_after_drain() -> None:
    worker = RecordingWorker()
    worker.failure = RuntimeError("dispatch failed")
    runtime = outbox_runtime(worker)
    stop = asyncio.Event()

    task = asyncio.create_task(runtime.run_forever(stop))
    await worker.started.wait()
    stop.set()
    worker.release.set()

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_shutdown_drain_timeout_emits_only_a_bounded_reason_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = RecordingWorker()
    runtime = outbox_runtime(worker, shutdown_timeout_seconds=0.01)
    stop = asyncio.Event()
    logger = logging.getLogger("spine.worker")
    previous_disabled = logger.disabled
    logger.disabled = True

    try:
        with caplog.at_level(logging.WARNING, logger="spine.worker"):
            task = asyncio.create_task(runtime.run_forever(stop))
            await worker.started.wait()
            stop.set()
            await asyncio.wait_for(task, timeout=1)
    finally:
        logger.disabled = previous_disabled

    assert caplog.records[0].reason_code == "dispatcher.shutdown_drain_timeout"
    assert "payload" not in caplog.text.lower()
    assert "SELECT" not in caplog.text


def test_worker_temporal_queue_matches_compose_qa_queue() -> None:
    settings = OutboxWorkerSettings(
        service_principal_id=UUID(int=500),
    )

    assert settings.temporal_address == "temporal:7233"
    assert settings.temporal_namespace == "default"
    assert settings.temporal_task_queue == "spine-r1-qa"
    assert settings.identity == "outbox-dispatcher"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"handler_timeout_seconds": 60.0, "lease_seconds": 60.0}, "handler timeout"),
        ({"shutdown_timeout_seconds": 61.0, "lease_seconds": 60.0}, "shutdown timeout"),
        (
            {
                "retry_initial_delay_seconds": 31.0,
                "retry_maximum_delay_seconds": 30.0,
            },
            "initial retry delay",
        ),
    ],
)
def test_worker_settings_reject_unsafe_dispatch_bounds(
    overrides: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        OutboxWorkerSettings(service_principal_id=UUID(int=500), **overrides)


def test_process_failure_log_omits_raw_exception_details(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import spine.workers.runtime as runtime_module

    def fail_with_sensitive_detail():
        raise RuntimeError("postgres://secret-user:secret-password/db payload")

    monkeypatch.setattr(
        runtime_module,
        "create_production_outbox_worker_runtime",
        fail_with_sensitive_detail,
    )
    logger = logging.getLogger("spine.worker")
    previous_disabled = logger.disabled
    logger.disabled = True

    try:
        with caplog.at_level(logging.ERROR, logger="spine.worker"):
            with pytest.raises(SystemExit):
                runtime_module.main()
    finally:
        logger.disabled = previous_disabled

    assert caplog.records[-1].reason_code == "dispatcher.process_failure"
    assert "secret-password" not in caplog.text
    assert "payload" not in caplog.text


def test_projection_receipt_context_uses_receipt_operation() -> None:
    command = _command()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID(int=501),
        secret=b"projection-worker-runtime-test-secret",
    )

    context = _projection_receipt_context(boundary, UUID(int=502), command)

    assert context.operation == PROJECTION_RECEIPT_OPERATION
    assert context.scope.workspace_id == command.workspace_id
    assert context.scope.environment_id == command.environment_id
    assert context.trace_id == command.projection_run_id


def test_qa_configuration_identity_does_not_reuse_legacy_context_identity() -> None:
    workspace_id = UUID(int=503)
    environment_id = UUID(int=504)

    assert _projection_configuration_id(
        workspace_id, environment_id, "qa-config"
    ) != _projection_configuration_id(workspace_id, environment_id, "config")


def test_publication_reconciliation_attempt_is_unique_per_snapshot() -> None:
    first = _publication_attempt_identity(UUID(int=505))
    second = _publication_attempt_identity(UUID(int=506))

    assert first != second
    assert first == f"publication:{UUID(int=505).hex}"


def test_chunk_data_identity_is_scoped_to_publication_namespace() -> None:
    source_object_id = UUID(int=507)
    segment_id = UUID(int=508)

    first = CogneeChunkProjectionStore.data_id_for(
        namespace_digest="a" * 64,
        source_object_id=source_object_id,
        segment_id=segment_id,
    )
    successor = CogneeChunkProjectionStore.data_id_for(
        namespace_digest="b" * 64,
        source_object_id=source_object_id,
        segment_id=segment_id,
    )

    assert first != segment_id
    assert successor != first
