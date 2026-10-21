from __future__ import annotations

from uuid import UUID

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


def test_worker_temporal_queue_matches_compose_qa_queue() -> None:
    settings = OutboxWorkerSettings(
        service_principal_id=UUID(int=500),
    )

    assert settings.temporal_address == "temporal:7233"
    assert settings.temporal_namespace == "default"
    assert settings.temporal_task_queue == "spine-r1-qa"


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
