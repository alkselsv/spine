from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.processing_commands import (
    OrchestratedProcessingCommandService,
    ProcessingCommandRejected,
    ReprocessProcessingCommand,
    RetryProcessingCommand,
)
from spine.domain.sources import RevisionKind
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")


def identifier(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"processing-command-test:{value}")


def trusted_context() -> object:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=identifier("issuer"),
        secret=b"processing-command-test-secret-32-bytes",
    )
    return boundary.worker(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        service_principal_id=identifier("service"),
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("document_processing"),
        trace_id=identifier("trace"),
    )


class _Ingestion:
    def __init__(self) -> None:
        self.created_runs = []
        self.created_items = []

    async def create_run(self, command):
        self.created_runs.append(command.run)
        return SimpleNamespace(
            value=command.run.model_copy(
                update={"idempotency_receipt_id": identifier("run-receipt")}
            ),
            replay=False,
        )

    async def create_item(self, item):
        self.created_items.append(item)
        return item


class _Sources:
    async def resolve_source(self, source_object_id):
        return SimpleNamespace(source_object_id=source_object_id)

    async def resolve_revision(self, revision_id):
        return SimpleNamespace(
            revision_id=revision_id,
            source_object_id=identifier("source"),
            kind=RevisionKind.CONTENT,
            original_sha256="a" * 64,
        )


class _Canonical:
    async def resolve_current(self, source_object_id):
        return SimpleNamespace(
            source_object_id=source_object_id,
            current_revision_id=identifier("different-current-revision"),
        )


class _Uow:
    def __init__(self, ingestion, sources, canonical) -> None:
        self.ingestion = ingestion
        self.sources = sources
        self.canonical = canonical

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def rollback(self):
        return None

    async def commit(self):
        return None


def test_reprocess_command_rejects_malformed_profile_reference() -> None:
    with pytest.raises(ValueError):
        ReprocessProcessingCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=identifier("source"),
            source_revision_id=identifier("revision"),
            expected_current_revision_id=identifier("revision"),
            idempotency_key="reprocess-1",
            processing_profile_reference="not a profile",
        )


@pytest.mark.asyncio
async def test_retry_rejects_stale_current_revision_before_creating_a_process_item() -> None:
    ingestion = _Ingestion()
    sources = _Sources()
    canonical = _Canonical()
    context = trusted_context()
    service = OrchestratedProcessingCommandService(
        uow_factory=lambda _: _Uow(ingestion, sources, canonical),
        validation_reader=object(),
        parsed_artifacts=object(),
        parser=lambda *args, **kwargs: None,
        context_factory=lambda operation, trace_id: context,
        outbox_events=OutboxEventRegistry(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProcessingCommandRejected):
        await service.retry(
            context=context,
            command=RetryProcessingCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=identifier("source"),
                source_revision_id=identifier("revision"),
                expected_current_revision_id=identifier("revision"),
                idempotency_key="retry-1",
                parent_failure_id=identifier("failure"),
            ),
        )

    assert len(ingestion.created_runs) == 1
    assert ingestion.created_items == []
