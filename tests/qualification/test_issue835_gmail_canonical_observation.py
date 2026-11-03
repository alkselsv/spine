"""Highest-seam Gmail RAW observation tracer for issue #835."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from spine.connectors.gmail import GmailRawMessageResult, GmailRawMessageStatus
from spine.connectors.gmail_observation import (
    GmailCanonicalObservationReader,
    GmailObservationFailureCode,
    GmailObservationMappingError,
    gmail_raw_message_observation,
)
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.connections.provider import ProviderCapabilityKind
from spine.domain.sources import GMAIL_EXTERNAL_GENERATION, GMAIL_EXTERNAL_NAMESPACE
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


CONNECTION_ID = stable_id("issue835-connection")
CONNECTION_VERSION_ID = stable_id("issue835-connection-version")
MAILBOX_BINDING_ID = stable_id("issue835-mailbox-binding")
LABEL_REFERENCE = stable_id("issue835-label")
SCOPE_REFERENCE = stable_id("issue835-scope")
MESSAGE_ID = "18f4a7d9c2b6e031"
RAW_MIME = (
    b"Date: Fri, 30 Oct 2026 11:58:00 +0000\r\n"
    b"Message-ID: <shared@example.test>\r\n"
    b"References: <parent@example.test>\r\n"
    b"In-Reply-To: <parent@example.test>\r\n"
    b"Subject: Protected synthetic subject\r\n"
    b"From: sender@example.test\r\n"
    b"To: recipient@example.test\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Protected synthetic body.\r\n"
)


def mailbox_scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        connection_version_id=CONNECTION_VERSION_ID,
        provider_kind="google",
        resource_scope_reference=SCOPE_REFERENCE,
        mailbox_subject_reference=stable_id("issue835-mailbox-subject"),
        mailbox_binding_id=MAILBOX_BINDING_ID,
        authorization_generation=3,
        configuration_generation=5,
        scope_generation=7,
        allowed_label_references=(LABEL_REFERENCE,),
        allowed_operations=(EmailProviderOperation.READ_RAW_MESSAGE,),
    )


def raw_result(
    *,
    message_id: str = MESSAGE_ID,
    raw_mime: bytes = RAW_MIME,
    history_id: str = "987654321",
) -> GmailRawMessageResult:
    return GmailRawMessageResult(
        status=GmailRawMessageStatus.ACCEPTED,
        mailbox_scope=mailbox_scope(),
        requested_message_id=message_id,
        authorized_label_reference=LABEL_REFERENCE,
        message_id=message_id,
        raw_mime=raw_mime,
        sha256=hashlib.sha256(raw_mime).hexdigest(),
        byte_length=len(raw_mime),
        observed_label_references=(LABEL_REFERENCE,),
        internal_date_ms=1793361540000,
        history_id=history_id,
        thread_id="thread-835",
    )


class StaticAdmission:
    def __init__(self, result: GmailRawMessageResult) -> None:
        self.result = result

    async def read(self, command, context):
        del command, context
        return self.result


def observation_reader(admission) -> GmailCanonicalObservationReader:
    return GmailCanonicalObservationReader(
        admission=admission,
        scope=mailbox_scope(),
        authorized_label_reference=LABEL_REFERENCE,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_authorized_gmail_raw_message_reaches_canonical_outbox_with_evidence() -> None:
    observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()
    command = ingestion_command(observation, identity="gmail-835-success")
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=observation_reader(StaticAdmission(raw_result())),
    )

    result = await orchestrator.ingest(command)
    observation = result.observation

    assert result.source.external_namespace == GMAIL_EXTERNAL_NAMESPACE
    assert result.source.external_generation == GMAIL_EXTERNAL_GENERATION
    assert result.source.external_object_id == observation.external_identity.external_object_id
    assert result.revision.original_sha256 == hashlib.sha256(RAW_MIME).hexdigest()
    assert result.revision.byte_length == len(RAW_MIME)
    assert result.revision.media_type == "message/rfc822"
    assert result.provenance.event_identity == observation.event_identity
    assert result.provenance.event_digest == observation.event_digest
    assert result.provenance.source_contract_id == "external-observation"
    assert result.provenance.source_contract_version == "v2"
    assert result.provenance.received_at == NOW
    assert result.provenance.observed_at == datetime(
        2026, 10, 30, 11, 59, tzinfo=timezone.utc
    )
    evidence = result.revision.revision_metadata.email_revision
    assert evidence is not None
    assert result.revision.revision_metadata_schema == "revision-metadata:r1-email-v1"
    assert evidence.provider_kind == "google"
    assert evidence.provider_message_id == MESSAGE_ID
    assert observation.provider_version_before == "987654321"
    assert observation.provider_version_after == "987654321"
    assert observation.representation.email_evidence.provider_received_at == NOW
    assert evidence.internal_date == datetime(
        2026, 10, 30, 11, 59, tzinfo=timezone.utc
    )
    assert evidence.header_date == datetime(
        2026, 10, 30, 11, 58, tzinfo=timezone.utc
    )
    assert evidence.provider_thread_reference == "thread-835"
    assert evidence.rfc_message_id_claims == ("<shared@example.test>",)
    assert evidence.references_claims == ("<parent@example.test>",)
    assert evidence.in_reply_to_claims == ("<parent@example.test>",)
    assert harness.projection_intent_count == 1
    assert observation.required_capabilities() == frozenset(
        {
            ProviderCapabilityKind.RAW_MESSAGE_READ,
            ProviderCapabilityKind.STABLE_IDENTITY,
        }
    )


def test_gmail_event_identity_is_stable_across_retry_receipt_times() -> None:
    first = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    retry = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW.replace(minute=NOW.minute + 1),
    )

    assert retry.event_identity == first.event_identity
    assert retry.event_digest == first.event_digest


@pytest.mark.asyncio
async def test_reobserved_same_gmail_bytes_keep_one_canonical_revision() -> None:
    first_observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    retry_observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW.replace(minute=NOW.minute + 1),
    )
    harness = await build_canonical_ingestion_harness()
    first = await harness.orchestrator.ingest_observation(
        ingestion_command(first_observation, identity="gmail-835-observed-first"),
        first_observation,
    )
    retry_command = ingestion_command(
        retry_observation, identity="gmail-835-observed-retry"
    ).model_copy(update={"expected_current_revision_id": first.revision.revision_id})

    retry = await harness.orchestrator.ingest_observation(
        retry_command, retry_observation
    )

    assert retry.revision == first.revision
    assert retry.source == first.source
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
async def test_gmail_history_advance_without_content_change_keeps_revision() -> None:
    first_observation = gmail_raw_message_observation(
        raw_result(history_id="987654321"),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    state_only_observation = gmail_raw_message_observation(
        raw_result(history_id="987654322"),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW.replace(minute=NOW.minute + 1),
    )
    harness = await build_canonical_ingestion_harness()
    first = await harness.orchestrator.ingest_observation(
        ingestion_command(first_observation, identity="gmail-835-history-first"),
        first_observation,
    )
    state_command = ingestion_command(
        state_only_observation, identity="gmail-835-history-state-only"
    ).model_copy(update={"expected_current_revision_id": first.revision.revision_id})

    state_only = await harness.orchestrator.ingest_observation(
        state_command, state_only_observation
    )

    assert state_only.revision == first.revision
    assert state_only.provenance.event_identity != first.provenance.event_identity
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
async def test_gmail_observation_replay_keeps_stable_revision_receipts_and_one_outbox() -> None:
    observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()
    command = ingestion_command(observation, identity="gmail-835-replay")

    first, replay = await harness.ingest_twice(command, observation)

    assert replay.replay is True
    assert replay.source == first.source
    assert replay.revision == first.revision
    assert replay.binding.original_receipt_id == first.binding.original_receipt_id
    assert replay.processing_receipt == first.processing_receipt
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
async def test_gmail_observation_retry_after_partial_upload_has_one_logical_effect() -> None:
    observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()

    class FailOnceUploader:
        def __init__(self) -> None:
            self.calls = 0

        async def upload(self, context, command, chunks):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary original-storage failure")
            return await harness.uploader.upload(context, command, chunks)

    uploader = FailOnceUploader()
    orchestrator = replace(harness.orchestrator, uploader=uploader)
    command = ingestion_command(observation, identity="gmail-835-partial-retry")

    with pytest.raises(RuntimeError, match="temporary original-storage failure"):
        await orchestrator.ingest_observation(command, observation)
    result = await orchestrator.ingest_observation(command, observation)

    assert result.replay is True
    assert uploader.calls == 2
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
async def test_changed_gmail_raw_bytes_append_a_new_revision_without_overwrite() -> None:
    first_observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    changed_bytes = RAW_MIME.replace(
        b"Protected synthetic body.", b"Changed protected synthetic body."
    )
    changed_observation = gmail_raw_message_observation(
        raw_result(raw_mime=changed_bytes),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()
    first = await harness.orchestrator.ingest_observation(
        ingestion_command(first_observation, identity="gmail-835-first"),
        first_observation,
    )
    successor_command = ingestion_command(
        changed_observation, identity="gmail-835-successor"
    ).model_copy(update={"expected_current_revision_id": first.revision.revision_id})

    successor = await harness.orchestrator.ingest_observation(
        successor_command, changed_observation
    )

    assert successor.source == first.source
    assert successor.revision.revision_id != first.revision.revision_id
    assert successor.revision.original_sha256 == hashlib.sha256(changed_bytes).hexdigest()
    assert first.revision.original_sha256 == hashlib.sha256(RAW_MIME).hexdigest()
    assert harness.uploader.objects[first.revision.original_reference.object_id] == RAW_MIME
    assert (
        harness.uploader.objects[successor.revision.original_reference.object_id]
        == changed_bytes
    )
    assert harness.projection_intent_count == 2


@pytest.mark.asyncio
async def test_duplicate_rfc_message_id_does_not_merge_gmail_provider_messages() -> None:
    first_observation = gmail_raw_message_observation(
        raw_result(message_id="gmail-provider-message-one"),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    second_observation = gmail_raw_message_observation(
        raw_result(message_id="gmail-provider-message-two"),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()

    first = await harness.orchestrator.ingest_observation(
        ingestion_command(first_observation, identity="gmail-835-duplicate-one"),
        first_observation,
    )
    second = await harness.orchestrator.ingest_observation(
        ingestion_command(second_observation, identity="gmail-835-duplicate-two"),
        second_observation,
    )

    assert first.source.source_object_id != second.source.source_object_id
    assert first.source.external_object_id != second.source.external_object_id
    assert (
        first.revision.revision_metadata.email_revision.rfc_message_id_claims
        == second.revision.revision_metadata.email_revision.rfc_message_id_claims
        == ("<shared@example.test>",)
    )
    assert harness.projection_intent_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_update",
    (
        {"workspace_id": stable_id("issue835-other-workspace")},
        {"resource_scope_reference": stable_id("issue835-other-scope")},
        {"authorization_generation": 4},
        {"configuration_generation": 6},
    ),
)
async def test_wrong_gmail_tenant_scope_or_generation_is_rejected_before_upload(
    command_update: dict[str, object],
) -> None:
    observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()
    command = ingestion_command(
        observation, identity="gmail-835-scope-denied"
    ).model_copy(update=command_update)

    orchestrator = replace(
        harness.orchestrator,
        observation_reader=observation_reader(StaticAdmission(raw_result())),
    )
    with pytest.raises(GmailObservationMappingError) as error:
        await orchestrator.ingest(command)

    rendered = str(error.value)
    assert error.value.code is GmailObservationFailureCode.STALE_AUTHORITY
    assert "Protected" not in rendered
    assert "sender@example.test" not in rendered
    assert MESSAGE_ID not in rendered
    assert harness.uploader.objects == {}
    assert harness.events.built_intents == []


@pytest.mark.asyncio
async def test_protected_read_failure_is_rejected_at_highest_seam_without_disclosure() -> None:
    class ProtectedFailureAdmission:
        async def read(self, command, context):
            del command, context
            raise RuntimeError(
                "token=protected-token subject=Protected filename=secret.eml "
                "sender@example.test Protected synthetic body"
            )

    observation = gmail_raw_message_observation(
        raw_result(),
        connection_version_id=CONNECTION_VERSION_ID,
        received_at=NOW,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=observation_reader(ProtectedFailureAdmission()),
    )

    with pytest.raises(GmailObservationMappingError) as error:
        await orchestrator.ingest(
            ingestion_command(observation, identity="gmail-835-protected-failure")
        )

    rendered = str(error.value)
    assert error.value.code is GmailObservationFailureCode.NOT_ELIGIBLE
    for protected in (
        "protected-token",
        "Protected",
        "secret.eml",
        "sender@example.test",
        "synthetic body",
    ):
        assert protected not in rendered
    assert harness.uploader.objects == {}
    assert harness.events.built_intents == []


def test_stale_connection_version_is_disclosure_safe() -> None:
    with pytest.raises(GmailObservationMappingError) as stale_error:
        gmail_raw_message_observation(
            raw_result(),
            connection_version_id=stable_id("issue835-stale-connection-version"),
            received_at=NOW,
        )

    assert stale_error.value.code is GmailObservationFailureCode.STALE_AUTHORITY
    rendered = str(stale_error.value)
    assert "Protected" not in rendered
    assert "sender@example.test" not in rendered
    assert MESSAGE_ID not in rendered


def test_failed_raw_result_is_disclosure_safe() -> None:
    failed = GmailRawMessageResult(
        status=GmailRawMessageStatus.PROVIDER_ERROR,
        mailbox_scope=mailbox_scope(),
        requested_message_id=MESSAGE_ID,
        authorized_label_reference=LABEL_REFERENCE,
    )
    with pytest.raises(GmailObservationMappingError) as provider_error:
        gmail_raw_message_observation(
            failed,
            connection_version_id=CONNECTION_VERSION_ID,
            received_at=NOW,
        )

    assert provider_error.value.code is GmailObservationFailureCode.NOT_ELIGIBLE
    rendered = str(provider_error.value)
    assert "Protected" not in rendered
    assert "sender@example.test" not in rendered
    assert MESSAGE_ID not in rendered


def test_malformed_protected_header_is_rejected_without_disclosure() -> None:
    protected_claim = b"<" + b"protected-address@invalid" * 300 + b">"
    raw_mime = b"Message-ID: " + protected_claim + b"\r\n\r\nProtected body"

    with pytest.raises(GmailObservationMappingError) as error:
        gmail_raw_message_observation(
            raw_result(raw_mime=raw_mime),
            connection_version_id=CONNECTION_VERSION_ID,
            received_at=NOW,
        )

    assert error.value.code is GmailObservationFailureCode.MALFORMED_EVIDENCE
    assert "protected-address" not in str(error.value)
