"""Synthetic email observation tracer for issue #827."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable
from dataclasses import replace
from typing import Any

import pytest

from spine.application.connector_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    ConnectorIngestionResult,
)
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
)
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import IdempotencyConflictError
from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    LifecycleObservationRepresentation,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderStructuredObjectKey,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderKind,
)
from tests.qualification.canonical_ingestion_harness import (
    build_canonical_ingestion_harness,
    ingestion_command,
)
from tests.qualification.provider_adapter_harness import (
    ENVIRONMENT_ID,
    NOW,
    WORKSPACE_ID,
    stable_id,
)


RAW_MIME = (
    b"From: synthetic-sender@example.test\r\n"
    b"To: synthetic-recipient@example.test\r\n"
    b"Subject: Synthetic qualification message\r\n"
    b"Message-ID: <issue-827@example.test>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Synthetic body for canonical-ingestion qualification.\r\n"
)


def email_observation(
    raw_mime: bytes = RAW_MIME,
    *,
    message_id: str = "synthetic-message-827",
) -> NormalizedExternalObservationV2:
    connection_id = stable_id("issue827-connection")
    return NormalizedExternalObservationV2(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=connection_id,
        connection_version_id=stable_id("issue827-connection-version"),
        provider_kind=ProviderKind.YANDEX_MAIL,
        resource_scope_reference=OpaqueProviderReference(
            value=stable_id("issue827-mailbox-scope")
        ),
        authorization_generation=1,
        configuration_generation=1,
        source_kind="email_message",
        external_identity=ProviderExternalIdentity(
            external_namespace="synthetic_mail_message",
            external_generation="synthetic_mailbox_epoch:v1",
            external_object_id=message_id,
            provider_key=ProviderStructuredObjectKey(
                connection_id=connection_id,
                object_type="email_message",
                provider_object_id=message_id,
            ),
            assurance=ExternalIdentityAssurance.SCOPE_STABLE,
            assurance_evidence_reference="synthetic_email_observation:v1",
        ),
        representation=BinaryObservationRepresentation(
            original_bytes=raw_mime,
            media_type="message/rfc822",
            extraction_profile="raw_email_mime:v1",
            locator_contract_version="email_message:v1",
        ),
        coverage=ConnectorCoverage.COMPLETE,
        producer_reference="synthetic_mail_observer",
        event_identity="synthetic-mailbox:event-827",
        event_digest=hashlib.sha256(raw_mime).hexdigest(),
        observer_service="issue827_qualification",
        received_at=NOW,
        observed_at=NOW,
    )


@pytest.mark.asyncio
async def test_synthetic_email_observation_reaches_canonical_acceptance_and_outbox() -> None:
    observation = email_observation()
    harness = await build_canonical_ingestion_harness()
    command = ingestion_command(observation, identity="email-827")

    first, replay = await harness.ingest_twice(command, observation)

    assert isinstance(first, ConnectorIngestionResult)
    assert first.canonical_acceptance is not None
    assert first.canonical_acceptance.acceptance is not None
    assert first.revision.original_sha256 == hashlib.sha256(RAW_MIME).hexdigest()
    assert first.revision.byte_length == len(RAW_MIME)
    assert first.revision.original_reference is not None
    assert first.revision.original_reference.digest_hex == hashlib.sha256(RAW_MIME).hexdigest()
    assert harness.uploader.objects[first.revision.original_reference.object_id] == RAW_MIME
    assert first.source.external_namespace == "synthetic_mail_message"
    assert first.source.external_object_id == "synthetic-message-827"
    assert first.provenance.event_identity == observation.event_identity
    assert first.provenance.event_digest == observation.event_digest
    assert first.provenance.received_at == NOW
    assert replay.replay is True
    assert replay.source == first.source
    assert replay.revision == first.revision
    assert replay.processing_receipt == first.processing_receipt
    read_context = harness.context_factory(
        PersistenceOperation("source_observation"), stable_id("email-827-lineage-read")
    )
    async with harness.persistence.uow_factory(read_context) as uow:
        stored_provenance = await uow.sources.list_provenance(first.revision.revision_id)
        receipt_associations = await uow.sources.resolve_receipt_associations(
            first.revision.revision_id
        )
        await uow.rollback()
    assert stored_provenance == (first.provenance,)
    assert len(receipt_associations) == 1
    assert receipt_associations[0].original_sha256 == hashlib.sha256(RAW_MIME).hexdigest()
    assert receipt_associations[0].byte_length == len(RAW_MIME)
    assert receipt_associations[0].verification_status == "verified"
    assert harness.projection_intent_count == 1
    assert sum(
        getattr(intent, "event_type", None) == PROJECTION_REQUEST_EVENT_TYPE
        for intent in harness.events.built_intents
    ) == 1
    assert any(
        getattr(intent, "event_type", None) == CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE
        for intent in harness.events.built_intents
    )


@pytest.mark.asyncio
async def test_changed_raw_email_bytes_conflict_without_disclosing_message_content() -> None:
    observation = email_observation()
    harness = await build_canonical_ingestion_harness()
    command = ingestion_command(observation, identity="email-827-conflict")
    await harness.orchestrator.ingest_observation(command, observation)
    changed = email_observation(RAW_MIME.replace(b"Synthetic body", b"Changed body"))

    with pytest.raises(IdempotencyConflictError) as error:
        await harness.orchestrator.ingest_observation(command, changed)

    assert b"Synthetic body" not in str(error.value).encode()
    assert b"Changed body" not in str(error.value).encode()


@pytest.mark.asyncio
async def test_changed_message_identity_conflicts_without_disclosing_identity() -> None:
    observation = email_observation()
    harness = await build_canonical_ingestion_harness()
    command = ingestion_command(observation, identity="email-827-identity-conflict")
    await harness.orchestrator.ingest_observation(command, observation)
    changed_identity = email_observation(message_id="synthetic-message-other")

    with pytest.raises(IdempotencyConflictError) as error:
        await harness.orchestrator.ingest_observation(command, changed_identity)

    assert "synthetic-message" not in str(error.value)


@pytest.mark.asyncio
async def test_unfinalized_email_upload_never_reaches_canonical_acceptance() -> None:
    class UnfinalizedUploader:
        def __init__(self) -> None:
            self.staged: list[bytes] = []

        async def upload(
            self,
            context: Any,
            command: Any,
            chunks: AsyncIterable[bytes],
        ) -> Any:
            del context, command
            self.staged.append(b"".join([chunk async for chunk in chunks]))
            raise RuntimeError("original upload was not finalized")

    observation = email_observation()
    harness = await build_canonical_ingestion_harness()
    uploader = UnfinalizedUploader()
    harness = replace(
        harness, orchestrator=replace(harness.orchestrator, uploader=uploader)
    )

    with pytest.raises(RuntimeError, match="not finalized") as error:
        await harness.orchestrator.ingest_observation(
            ingestion_command(observation, identity="email-827-staged"), observation
        )

    assert uploader.staged == [RAW_MIME]
    assert RAW_MIME.decode() not in str(error.value)
    assert harness.events.built_intents == []


@pytest.mark.asyncio
async def test_email_observation_outside_tenant_is_denied_before_upload() -> None:
    observation = email_observation()
    harness = await build_canonical_ingestion_harness()
    command = ingestion_command(observation, identity="email-827-denied").model_copy(
        update={"workspace_id": stable_id("other-workspace")}
    )

    with pytest.raises(ValueError, match="outside the trusted read scope") as error:
        await harness.orchestrator.ingest_observation(command, observation)

    assert "synthetic" not in str(error.value).lower()
    assert harness.uploader.objects == {}
    assert harness.events.built_intents == []


@pytest.mark.asyncio
async def test_lifecycle_only_email_observation_never_enters_content_ingestion() -> None:
    content = email_observation()
    lifecycle = content.model_copy(
        update={
            "representation": LifecycleObservationRepresentation(
                state="temporarily_unavailable",
                evidence_reference="synthetic_mailbox_probe",
                evidence_digest="a" * 64,
            )
        }
    )
    harness = await build_canonical_ingestion_harness()

    with pytest.raises(ValueError, match="Lifecycle-only observations"):
        await harness.orchestrator.ingest_observation(
            ingestion_command(lifecycle, identity="email-827-lifecycle"), lifecycle
        )

    assert harness.events.built_intents == []
