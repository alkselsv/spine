"""Highest-seam qualification for issue #848."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from spine.application.connector_ingestion import ConnectorIngestionCommand
from spine.application.connections.email_read import MailboxReadDeniedError
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import IdempotencyConflictError
from spine.connectors.yandex_mail_observation import (
    YandexMailCanonicalObservationError,
    YandexMailCanonicalObservationReader,
    yandex_mail_raw_message_observation,
)
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchResult,
)
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.ingestion import IngestionItemState
from tests.qualification.canonical_ingestion_harness import (
    POLICY_ID,
    build_canonical_ingestion_harness,
)
from tests.qualification.provider_adapter_harness import (
    ENVIRONMENT_ID,
    WORKSPACE_ID,
    stable_id,
)


RAW_MIME = (
    b"From: synthetic-sender@example.test\r\n"
    b"To: synthetic-recipient@example.test\r\n"
    b"Subject: Synthetic IMAP qualification\r\n"
    b"Message-ID: <shared-copy@example.test>\r\n"
    b"\r\n"
    b"Synthetic issue 848 body.\r\n"
)
INTERNAL_DATE = datetime(2026, 10, 31, 9, 30, tzinfo=timezone.utc)
FOLDER_ID = stable_id("issue848-folder")


def _scope(**updates: object) -> MailboxReadScope:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "connection_id": stable_id("issue848-connection"),
        "connection_version_id": stable_id("issue848-connection-version"),
        "provider_kind": "yandex_mail",
        "resource_scope_reference": stable_id("issue848-mailbox-scope"),
        "mailbox_subject_reference": stable_id("issue848-mailbox-subject"),
        "mailbox_binding_id": stable_id("issue848-mailbox-binding"),
        "authorization_generation": 3,
        "configuration_generation": 4,
        "scope_generation": 5,
        "allowed_folder_references": (FOLDER_ID,),
        "allowed_operations": (EmailProviderOperation.READ_RAW_MESSAGE,),
    }
    values.update(updates)
    return MailboxReadScope(**values)


def _fetch_result(
    scope: MailboxReadScope,
    *,
    uid_validity: int = 101,
    uid: int = 42,
    raw_bytes: bytes = RAW_MIME,
) -> YandexMailRawMessageFetchResult:
    return YandexMailRawMessageFetchResult(
        code=YandexMailRawMessageFetchCode.COMPLETE,
        complete=True,
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=FOLDER_ID,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=uid_validity,
        uid=uid,
        raw_bytes=raw_bytes,
        content_length=len(raw_bytes),
        content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        declared_octets=len(raw_bytes),
        envelope=b'("Sat, 31 Oct 2026 09:30:00 +0000" "Synthetic")',
        internal_date=INTERNAL_DATE,
        fetch_data_item="BODY.PEEK[]",
        seen_preserved=True,
        mutation_evidence_complete=True,
    )


def _command(
    scope: MailboxReadScope,
    *,
    identity: str = "message",
    uid: int = 42,
) -> ConnectorIngestionCommand:
    return ConnectorIngestionCommand(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        command_identity=f"qualification.issue848.{identity}",
        idempotency_key=f"issue848:{identity}",
        connection_id=scope.connection_id,
        resource_scope_reference=scope.resource_scope_reference,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        resource_object_reference=str(uid),
        access_policy_version_id=POLICY_ID,
    )


class _AdmittedRawMessage:
    def __init__(self, result: YandexMailRawMessageFetchResult) -> None:
        self.result = result
        self.calls = 0

    async def read(self, command: object, context: object) -> YandexMailRawMessageFetchResult:
        del command, context
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_qualified_imap_message_reaches_canonical_acceptance_and_replays() -> None:
    scope = _scope()
    admission = _AdmittedRawMessage(_fetch_result(scope))
    reader = YandexMailCanonicalObservationReader(
        admission=admission,
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
        max_message_bytes=4096,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=reader,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
    )

    first = await orchestrator.ingest(_command(scope))
    restarted_admission = _AdmittedRawMessage(_fetch_result(scope))
    orchestrator = replace(
        orchestrator,
        observation_reader=YandexMailCanonicalObservationReader(
            admission=restarted_admission,
            scope=scope,
            folder_reference=FOLDER_ID,
            uid_validity=101,
            uid=42,
            max_message_bytes=4096,
        ),
    )
    replay = await orchestrator.ingest(_command(scope))

    assert first.source.external_namespace == "imap"
    assert first.source.external_generation == "imap-uidvalidity-v1:101"
    provider_key = first.observation.external_identity.provider_key
    assert provider_key.workspace_id == scope.workspace_id
    assert provider_key.connection_id == scope.connection_id
    assert provider_key.mailbox_binding_id == scope.mailbox_binding_id
    assert provider_key.folder_binding_id == FOLDER_ID
    assert provider_key.uid_validity == 101
    assert provider_key.uid == 42
    assert first.revision.original_sha256 == hashlib.sha256(RAW_MIME).hexdigest()
    assert harness.uploader.objects[first.revision.original_reference.object_id] == RAW_MIME
    email_evidence = first.revision.revision_metadata.external_file_revision
    assert email_evidence is not None
    assert email_evidence.modified_at == INTERNAL_DATE
    assert email_evidence.provider_payload is not None
    assert email_evidence.provider_payload["envelope_sha256"] == hashlib.sha256(
        admission.result.envelope
    ).hexdigest()
    assert base64.b64decode(email_evidence.provider_payload["envelope_base64"]) == (
        admission.result.envelope
    )
    assert email_evidence.provider_payload["schema_version"] == (
        "provider-imap-revision-evidence:v1"
    )
    assert first.canonical_acceptance is not None
    assert harness.projection_intent_count == 1
    assert replay.replay is True
    assert replay.source == first.source
    assert replay.revision == first.revision
    assert replay.processing_receipt == first.processing_receipt
    assert admission.calls == 1
    assert restarted_admission.calls == 1


@pytest.mark.asyncio
async def test_copied_message_with_same_message_id_and_digest_remains_distinct() -> None:
    scope = _scope()
    harness = await build_canonical_ingestion_harness()

    async def ingest_uid(uid: int):
        reader = YandexMailCanonicalObservationReader(
            admission=_AdmittedRawMessage(_fetch_result(scope, uid=uid)),
            scope=scope,
            folder_reference=FOLDER_ID,
            uid_validity=101,
            uid=uid,
            max_message_bytes=4096,
        )
        orchestrator = replace(
            harness.orchestrator,
            observation_reader=reader,
            read_operation=PersistenceOperation("mailbox.read_raw_message"),
        )
        return await orchestrator.ingest(
            _command(scope, identity=f"copy-{uid}", uid=uid)
        )

    first = await ingest_uid(42)
    copied = await ingest_uid(43)

    assert first.revision.original_sha256 == copied.revision.original_sha256
    assert first.source.source_object_id != copied.source.source_object_id
    assert first.source.external_object_id != copied.source.external_object_id
    assert first.observation.external_identity.provider_key.uid == 42
    assert copied.observation.external_identity.provider_key.uid == 43
    assert harness.projection_intent_count == 2


@pytest.mark.asyncio
async def test_uidvalidity_reset_cannot_merge_reused_uid_by_message_id_or_digest() -> None:
    scope = _scope()
    harness = await build_canonical_ingestion_harness()

    async def ingest_generation(uid_validity: int):
        reader = YandexMailCanonicalObservationReader(
            admission=_AdmittedRawMessage(
                _fetch_result(scope, uid_validity=uid_validity, uid=42)
            ),
            scope=scope,
            folder_reference=FOLDER_ID,
            uid_validity=uid_validity,
            uid=42,
            max_message_bytes=4096,
        )
        orchestrator = replace(
            harness.orchestrator,
            observation_reader=reader,
            read_operation=PersistenceOperation("mailbox.read_raw_message"),
        )
        return await orchestrator.ingest(
            _command(scope, identity=f"reset-{uid_validity}", uid=42)
        )

    stale = await ingest_generation(101)
    successor = await ingest_generation(102)

    assert stale.revision.original_sha256 == successor.revision.original_sha256
    assert stale.source.source_object_id != successor.source.source_object_id
    assert stale.source.external_object_id != successor.source.external_object_id
    assert stale.source.external_generation == "imap-uidvalidity-v1:101"
    assert successor.source.external_generation == "imap-uidvalidity-v1:102"
    assert harness.projection_intent_count == 2


def test_same_imap_provider_key_is_isolated_between_valid_workspaces() -> None:
    first_scope = _scope()
    second_scope = _scope(workspace_id=stable_id("issue848-second-workspace"))

    first = yandex_mail_raw_message_observation(
        scope=first_scope,
        result=_fetch_result(first_scope),
    )
    second = yandex_mail_raw_message_observation(
        scope=second_scope,
        result=_fetch_result(second_scope),
    )

    assert first.external_identity.external_object_id != (
        second.external_identity.external_object_id
    )
    assert first.external_identity.provider_key.workspace_id == first_scope.workspace_id
    assert second.external_identity.provider_key.workspace_id == second_scope.workspace_id


@pytest.mark.asyncio
async def test_changed_bytes_under_immutable_imap_key_are_quarantined() -> None:
    scope = _scope()
    admission = _AdmittedRawMessage(_fetch_result(scope))
    reader = YandexMailCanonicalObservationReader(
        admission=admission,
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
        max_message_bytes=4096,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=reader,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
    )
    accepted = await orchestrator.ingest(_command(scope, identity="accepted"))
    changed_bytes = RAW_MIME.replace(b"issue 848 body", b"impossible changed body")
    admission.result = _fetch_result(scope, raw_bytes=changed_bytes)

    changed_command = _command(scope, identity="accepted")
    with pytest.raises(IdempotencyConflictError) as error:
        await orchestrator.ingest(changed_command)
    with pytest.raises(IdempotencyConflictError):
        await orchestrator.ingest(changed_command)

    assert changed_bytes.decode() not in str(error.value)
    assert RAW_MIME.decode() not in str(error.value)
    read_context = harness.context_factory(
        PersistenceOperation("canonical_acceptance"),
        stable_id("issue848-current-read"),
    )
    async with harness.persistence.uow_factory(read_context) as uow:
        current = await uow.canonical.resolve_current(accepted.source.source_object_id)
        items = await uow.ingestion.list_items(limit=10)
        changed_item = next(
            item
            for item in items
            if item.command_identity != changed_command.command_identity
        )
        changed_head = await uow.ingestion.resolve_item_head(
            changed_item.ingestion_run_source_id
        )
        failures = await uow.ingestion.resolve_failures(
            changed_item.ingestion_run_source_id
        )
        await uow.rollback()
    assert current is not None
    assert current.current_revision_id == accepted.revision.revision_id
    assert changed_head is not None
    assert changed_head.state is IngestionItemState.QUARANTINED
    assert changed_head.attempt == 2
    assert changed_item.created_at == orchestrator.clock()
    assert len(failures) == 1
    assert failures[0].code == "observation_conflict"
    assert failures[0].retryable is False
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_update",
    (
        {"workspace_id": stable_id("issue848-other-workspace")},
        {"authorization_generation": 99},
        {"configuration_generation": 99},
    ),
)
async def test_wrong_tenant_or_generation_is_denied_before_provider_read(
    command_update: dict[str, object],
) -> None:
    scope = _scope()
    admission = _AdmittedRawMessage(_fetch_result(scope))
    reader = YandexMailCanonicalObservationReader(
        admission=admission,
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
        max_message_bytes=4096,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=reader,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
    )
    command = _command(scope).model_copy(update=command_update)

    with pytest.raises(MailboxReadDeniedError) as error:
        await orchestrator.ingest(command)

    assert str(error.value) == "Mailbox read denied."
    assert admission.calls == 0
    assert harness.uploader.objects == {}
    assert harness.events.built_intents == []


@pytest.mark.asyncio
async def test_uidvalidity_reset_race_is_rejected_without_canonical_effect() -> None:
    scope = _scope()
    reset = _fetch_result(scope).model_copy(
        update={
            "code": YandexMailRawMessageFetchCode.RESET_RACE,
            "complete": False,
            "raw_bytes": None,
            "content_length": 0,
            "content_sha256": None,
            "envelope": None,
            "internal_date": None,
            "seen_preserved": None,
            "mutation_evidence_complete": False,
        }
    )
    admission = _AdmittedRawMessage(reset)
    reader = YandexMailCanonicalObservationReader(
        admission=admission,
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
        max_message_bytes=4096,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=reader,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
    )

    with pytest.raises(YandexMailCanonicalObservationError) as error:
        await orchestrator.ingest(_command(scope))

    assert str(error.value) == "IMAP message observation rejected."
    assert admission.calls == 1
    assert harness.uploader.objects == {}
    assert harness.events.built_intents == []


@pytest.mark.asyncio
async def test_retry_after_reset_race_accepts_one_canonical_effect() -> None:
    scope = _scope()
    reset = _fetch_result(scope).model_copy(
        update={
            "code": YandexMailRawMessageFetchCode.RESET_RACE,
            "complete": False,
            "raw_bytes": None,
            "content_length": 0,
            "content_sha256": None,
            "envelope": None,
            "internal_date": None,
            "seen_preserved": None,
            "mutation_evidence_complete": False,
        }
    )
    admission = _AdmittedRawMessage(reset)
    reader = YandexMailCanonicalObservationReader(
        admission=admission,
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
        max_message_bytes=4096,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=reader,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
    )

    with pytest.raises(YandexMailCanonicalObservationError):
        await orchestrator.ingest(_command(scope))
    admission.result = _fetch_result(scope)
    accepted = await orchestrator.ingest(_command(scope))

    assert accepted.canonical_acceptance is not None
    assert harness.projection_intent_count == 1
    assert admission.calls == 2


@pytest.mark.asyncio
async def test_wrong_returned_uid_is_rejected_before_canonical_ingestion() -> None:
    scope = _scope()
    admission = _AdmittedRawMessage(_fetch_result(scope, uid=43))
    reader = YandexMailCanonicalObservationReader(
        admission=admission,
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
        max_message_bytes=4096,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=reader,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
    )

    with pytest.raises(YandexMailCanonicalObservationError):
        await orchestrator.ingest(_command(scope))

    assert harness.uploader.objects == {}
    assert harness.events.built_intents == []


@pytest.mark.asyncio
async def test_protected_wrong_scope_output_is_rejected_without_disclosure() -> None:
    scope = _scope()
    protected = _fetch_result(scope).model_copy(
        update={"workspace_id": stable_id("issue848-other-workspace")}
    )
    admission = _AdmittedRawMessage(protected)
    reader = YandexMailCanonicalObservationReader(
        admission=admission,
        scope=scope,
        folder_reference=FOLDER_ID,
        uid_validity=101,
        uid=42,
        max_message_bytes=4096,
    )
    harness = await build_canonical_ingestion_harness()
    orchestrator = replace(
        harness.orchestrator,
        observation_reader=reader,
        read_operation=PersistenceOperation("mailbox.read_raw_message"),
    )

    with pytest.raises(YandexMailCanonicalObservationError) as error:
        await orchestrator.ingest(_command(scope))

    failure = str(error.value)
    assert "Synthetic IMAP qualification" not in failure
    assert "synthetic-sender" not in failure
    assert RAW_MIME.decode() not in failure
    assert harness.uploader.objects == {}
    assert harness.events.built_intents == []
