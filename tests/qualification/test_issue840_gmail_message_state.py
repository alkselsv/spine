"""Highest-seam Gmail state provenance qualification for issue #840."""

from __future__ import annotations

import hashlib

import pytest

from spine.application.gmail_message_state import (
    GmailLabelDisposition,
    GmailMessageEvidenceBinding,
    InMemoryGmailMessageStateRepository,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryResult,
    GmailHistoryState,
)
from spine.connectors.gmail_message_state import GmailMessageStateWriter
from spine.connectors.gmail_observation import (
    GmailCanonicalIngestion,
    gmail_raw_message_observation,
)
from spine.domain.sources.gmail import GmailMessageKey
from tests.qualification.canonical_ingestion_harness import (
    build_canonical_ingestion_harness,
    ingestion_command,
)
from tests.qualification.provider_adapter_harness import NOW
from tests.qualification.test_issue835_gmail_canonical_observation import (
    CONNECTION_ID,
    CONNECTION_VERSION_ID,
    MAILBOX_BINDING_ID,
    MESSAGE_ID,
    RAW_MIME,
    raw_result,
)


@pytest.mark.asyncio
async def test_metadata_only_state_and_relationships_preserve_raw_revision() -> None:
    raw_observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()
    ingested = await harness.orchestrator.ingest_observation(
        ingestion_command(raw_observation, identity="gmail-840-content"),
        raw_observation,
    )
    revision_evidence = ingested.revision.revision_metadata.email_revision
    assert revision_evidence is not None
    binding = GmailMessageEvidenceBinding(
        workspace_id=ingested.source.workspace_id,
        environment_id=ingested.source.environment_id,
        source_object_id=ingested.source.source_object_id,
        source_revision_id=ingested.revision.revision_id,
        message_key=GmailMessageKey(
            connection_id=CONNECTION_ID,
            mailbox_binding_id=MAILBOX_BINDING_ID,
            message_id=MESSAGE_ID,
        ),
    )
    repository = InMemoryGmailMessageStateRepository(
        (binding,), revision_evidence=((binding, revision_evidence),)
    )
    writer = GmailMessageStateWriter(
        repository=repository,
        workspace_id=binding.workspace_id,
        environment_id=binding.environment_id,
        connection_id=CONNECTION_ID,
        mailbox_binding_id=MAILBOX_BINDING_ID,
    )
    canonical_ingestion = GmailCanonicalIngestion(
        orchestrator=harness.orchestrator,
        state_writer=writer,
        clock=lambda: NOW,
    )
    history = GmailHistoryResult(
        requested_start_history_id="987654321",
        admission_scope_digest="a" * 64,
        state=GmailHistoryState.COMPLETE,
        changes=(
            GmailHistoryChange(
                history_id="987654322",
                kind=GmailHistoryChangeKind.LABEL_ADDED,
                message_id=MESSAGE_ID,
                label_id="INBOX",
            ),
        ),
        terminal_history_id="987654322",
        pages_read=1,
        duplicate_count=0,
        malformed_count=0,
    )

    replayed = await canonical_ingestion.ingest_observation(
        ingestion_command(raw_observation, identity="gmail-840-content"),
        raw_observation,
    )
    state = (await writer.record_history(history, received_at=NOW))[0]
    relationships = await repository.list_relationships(binding)

    assert replayed.replay is True
    assert state.disposition is GmailLabelDisposition.APPLIED
    assert state.current.observation.binding.source_revision_id == ingested.revision.revision_id
    assert len(await repository.list_relationships(binding)) == 3
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
async def test_raw_mime_change_still_creates_content_successor_and_projection() -> None:
    first_observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    changed_bytes = RAW_MIME.replace(
        b"Protected synthetic body.", b"Changed protected synthetic body."
    )
    changed_observation = gmail_raw_message_observation(
        raw_result(raw_mime=changed_bytes, history_id="987654323"),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()
    first = await harness.orchestrator.ingest_observation(
        ingestion_command(first_observation, identity="gmail-840-first"),
        first_observation,
    )
    successor_command = ingestion_command(
        changed_observation, identity="gmail-840-successor"
    ).model_copy(update={"expected_current_revision_id": first.revision.revision_id})

    successor = await harness.orchestrator.ingest_observation(
        successor_command, changed_observation
    )

    assert successor.source == first.source
    assert successor.revision.revision_id != first.revision.revision_id
    assert successor.revision.original_sha256 == hashlib.sha256(changed_bytes).hexdigest()
    assert harness.projection_intent_count == 2
