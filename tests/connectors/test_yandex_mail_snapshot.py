from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from uuid import UUID

import pytest

from spine.application.connections.email_read import MailboxReadCommand
from spine.application.email_sync import (
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.yandex_mail_snapshot import (
    YandexMailInitialSnapshotCommand,
    YandexMailSnapshotFolderCode,
    YandexMailSnapshotGenerationCode,
    YandexMailSnapshotUidPageCode,
    YandexMailSnapshotUidPageRequest,
    YandexMailSnapshotMessageCommand,
)
from spine.workers.yandex_mail_snapshot import (
    create_yandex_mail_initial_snapshot_operation,
)
from spine.connectors.yandex_mail_folders import (
    YandexMailFolderBinding,
    YandexMailFolderListing,
    YandexMailListedFolder,
)
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderGenerationCode,
    YandexMailFolderGenerationObservation,
)
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchResult,
)
from spine.connectors.yandex_mail_snapshot import (
    YandexMailCanonicalSnapshotMessageAdapter,
    YandexMailInitialSnapshotAdapter,
)
from spine.connectors.yandex_mail_uid_enumeration import (
    YandexMailUidEnumerationPageRequest,
    YandexMailUidEnumerationPageResult,
    YandexMailUidPageCode,
)
from spine.domain.connections.email_scope import (
    EmailOperation,
    EmailProviderOperation,
    MailboxReadScope,
)
from tests.qualification.canonical_ingestion_harness import (
    POLICY_ID,
    build_canonical_ingestion_harness,
)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=_id(1),
        environment_id=_id(2),
        connection_id=_id(3),
        connection_version_id=_id(4),
        provider_kind="yandex_mail",
        resource_scope_reference=_id(5),
        mailbox_subject_reference=_id(6),
        mailbox_binding_id=_id(7),
        authorization_generation=1,
        configuration_generation=2,
        scope_generation=3,
        allowed_folder_references=(_id(8),),
        allowed_operations=tuple(EmailProviderOperation),
    )


class Bindings:
    async def resolve_exact(self, scope, context):
        del context
        return (
            YandexMailFolderBinding(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                connection_id=scope.connection_id,
                connection_version_id=scope.connection_version_id,
                mailbox_binding_id=scope.mailbox_binding_id,
                mailbox_subject_reference=scope.mailbox_subject_reference,
                folder_reference=_id(8),
                authorization_generation=scope.authorization_generation,
                configuration_generation=scope.configuration_generation,
                scope_generation=scope.scope_generation,
                folder_name="INBOX",
            ),
        )


class AdmittedReader:
    def __init__(self) -> None:
        self.commands: list[MailboxReadCommand] = []

    async def read(self, command, context):
        del context
        self.commands.append(command)
        scope = command.scope
        if command.operation is EmailOperation.ENUMERATE_MAILBOX:
            return YandexMailFolderListing(
                folders=(
                    YandexMailListedFolder(
                        flags=b"\\HasNoChildren", delimiter=b"/", name=b"INBOX"
                    ),
                ),
                complete=True,
            )
        if command.operation is EmailOperation.OBSERVE_FOLDER_GENERATION:
            return YandexMailFolderGenerationObservation(
                complete=True,
                code=YandexMailFolderGenerationCode.COMPLETE,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                connection_id=scope.connection_id,
                connection_version_id=scope.connection_version_id,
                mailbox_binding_id=scope.mailbox_binding_id,
                folder_reference=_id(8),
                authorization_generation=scope.authorization_generation,
                configuration_generation=scope.configuration_generation,
                scope_generation=scope.scope_generation,
                uid_validity=101,
                uid_next=1,
                selection_command="EXAMINE",
                read_only=True,
            )
        assert isinstance(command, YandexMailUidEnumerationPageRequest)
        return YandexMailUidEnumerationPageResult(
            complete=True,
            code=YandexMailUidPageCode.COMPLETE,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=_id(8),
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=101,
            covered_uid_range=None,
        )


@pytest.mark.asyncio
async def test_snapshot_adapter_uses_only_admitted_read_only_operations() -> None:
    scope = _scope()
    admitted = AdmittedReader()
    adapter = YandexMailInitialSnapshotAdapter(
        reader=admitted,
        bindings=Bindings(),
        context_factory=lambda operation, trace_id: object(),  # type: ignore[arg-type]
    )

    discovery = await adapter.discover_folders(scope)
    generation = await adapter.observe_folder_generation(scope, _id(8))
    page = await adapter.enumerate_uid_page(
        YandexMailSnapshotUidPageRequest(
            scope=scope,
            folder_reference=_id(8),
            max_items=10,
            max_response_bytes=4096,
            generation=generation,
            start_uid=1,
        )
    )

    assert discovery.code is YandexMailSnapshotFolderCode.COMPLETE
    assert generation.code is YandexMailSnapshotGenerationCode.COMPLETE
    assert page.code is YandexMailSnapshotUidPageCode.COMPLETE
    assert [command.operation for command in admitted.commands] == [
        EmailOperation.ENUMERATE_MAILBOX,
        EmailOperation.OBSERVE_FOLDER_GENERATION,
        EmailOperation.ENUMERATE_FOLDER_UIDS,
    ]
    assert all(command.message_id is None for command in admitted.commands)


class RawMessages:
    def __init__(self, scope: MailboxReadScope) -> None:
        raw = b"From: synthetic@example.test\r\n\r\nSynthetic body.\r\n"
        self.result = YandexMailRawMessageFetchResult(
            code=YandexMailRawMessageFetchCode.COMPLETE,
            complete=True,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=_id(8),
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=101,
            uid=1,
            raw_bytes=raw,
            content_length=len(raw),
            content_sha256=hashlib.sha256(raw).hexdigest(),
            declared_octets=len(raw),
            envelope=b'("Synthetic")',
            internal_date=datetime(2026, 11, 3, tzinfo=timezone.utc),
            fetch_data_item="BODY.PEEK[]",
            seen_preserved=True,
            mutation_evidence_complete=True,
        )

    async def read(self, command, context):
        del command, context
        return self.result


@pytest.mark.asyncio
async def test_canonical_message_adapter_returns_stable_durable_receipt() -> None:
    scope = _scope()
    harness = await build_canonical_ingestion_harness()
    adapter = YandexMailCanonicalSnapshotMessageAdapter(
        ingestion=harness.orchestrator,
        raw_messages=RawMessages(scope),
    )
    command = YandexMailSnapshotMessageCommand(
        scope=scope,
        folder_reference=_id(8),
        uid_validity=101,
        uid=1,
        size_octets=50,
        max_message_bytes=4096,
        access_policy_version_id=POLICY_ID,
        effect_key="imap.message.synthetic.101.1",
    )

    first = await adapter.apply(command)
    replay = await adapter.apply(command)

    assert first == replay
    assert first.workspace_id == scope.workspace_id
    assert first.environment_id == scope.environment_id
    assert first.receipt_reference == str(first.canonical_acceptance_id)
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
async def test_canonical_message_adapter_returns_existing_durable_acceptance() -> None:
    scope = _scope()
    harness = await build_canonical_ingestion_harness()
    adapter = YandexMailCanonicalSnapshotMessageAdapter(
        ingestion=harness.orchestrator,
        raw_messages=RawMessages(scope),
    )
    base = YandexMailSnapshotMessageCommand(
        scope=scope,
        folder_reference=_id(8),
        uid_validity=101,
        uid=1,
        size_octets=50,
        max_message_bytes=4096,
        access_policy_version_id=POLICY_ID,
        effect_key="imap.message.prior.101.1",
    )

    prior = await adapter.apply(base)
    snapshot = await adapter.apply(
        base.model_copy(update={"effect_key": "imap.message.snapshot.101.1"})
    )

    assert snapshot.canonical_acceptance_id == prior.canonical_acceptance_id
    assert snapshot.receipt_reference == prior.receipt_reference
    assert harness.projection_intent_count == 1


@pytest.mark.asyncio
async def test_worker_composition_executes_bounded_snapshot_operation() -> None:
    class Admission:
        async def check(self, admission):
            del admission
            return EmailSyncAdmissionState.ALLOWED

    class Effects:
        async def apply(self, effect):
            del effect
            raise AssertionError("empty scripted folder has no message effects")

    scope = _scope()
    harness = await build_canonical_ingestion_harness()
    operation = create_yandex_mail_initial_snapshot_operation(
        sync=EmailSyncCoordinator(
            repository=InMemoryEmailSyncRepository(),
            admission=Admission(),
            effects=Effects(),
        ),
        admitted_reader=AdmittedReader(),
        folder_bindings=Bindings(),
        context_factory=lambda operation, trace_id: object(),  # type: ignore[arg-type]
        ingestion=harness.orchestrator,
        raw_messages=RawMessages(scope),
    )

    result = await operation.run(
        YandexMailInitialSnapshotCommand(
            scope=scope,
            attempt_key="issue-849-worker-composition",
            uid_page_size=10,
            uid_page_response_bytes=4096,
            max_uid_pages_per_folder=2,
            max_message_bytes=4096,
            access_policy_version_id=POLICY_ID,
        )
    )

    assert result.attempt.status is EmailSyncStatus.COMPLETE
    assert result.completed_folders == 1
    assert result.completed_uid_pages == 1
