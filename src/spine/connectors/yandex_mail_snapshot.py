"""Composition adapter for bounded Yandex Mail initial snapshot reads."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.connections.email_read import MailboxReadCommand
from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionOrchestrator,
)
from spine.application.yandex_mail_snapshot import (
    YandexMailSnapshotFolderCode,
    YandexMailSnapshotFolderDiscovery,
    YandexMailSnapshotFolderGeneration,
    YandexMailSnapshotGenerationCode,
    YandexMailSnapshotMessageMetadata,
    YandexMailSnapshotMessageCommand,
    YandexMailSnapshotMessageReceipt,
    YandexMailSnapshotUidPage,
    YandexMailSnapshotUidPageCode,
    YandexMailSnapshotUidPageRequest,
)
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.connectors.yandex_mail_folders import (
    YandexMailFolderBindingRepository,
    YandexMailFolderDiscoveryProfile,
    discover_approved_yandex_mail_folders,
)
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderGenerationCode,
    YandexMailFolderGenerationObservation,
    failed_yandex_mail_folder_generation_observation,
)
from spine.connectors.yandex_mail_uid_enumeration import (
    YandexMailUidEnumerationPageRequest,
    YandexMailUidEnumerationPageResult,
    YandexMailUidPageCode,
    failed_yandex_mail_uid_page,
)
from spine.connectors.yandex_mail_observation import (
    YandexMailCanonicalObservationReader,
    YandexMailRawMessageAdmission,
)
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


class YandexMailAdmittedSnapshotReader(Protocol):
    async def read(
        self,
        command: MailboxReadCommand,
        context: TrustedPersistenceContext,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class YandexMailInitialSnapshotAdapter:
    """Map coordinator reads onto the admitted, read-only IMAP boundary."""

    reader: YandexMailAdmittedSnapshotReader
    bindings: YandexMailFolderBindingRepository
    context_factory: Callable[
        [PersistenceOperation, UUID], TrustedPersistenceContext
    ]
    discovery_profile: YandexMailFolderDiscoveryProfile = (
        YandexMailFolderDiscoveryProfile()
    )

    async def discover_folders(
        self, scope: MailboxReadScope
    ) -> YandexMailSnapshotFolderDiscovery:
        context = self._context(scope, "mailbox_snapshot_discovery", "discovery")
        result = await discover_approved_yandex_mail_folders(
            reader=self.reader,
            bindings=self.bindings,
            command=MailboxReadCommand(
                scope=scope,
                operation=EmailOperation.ENUMERATE_MAILBOX,
                mailbox_subject_reference=scope.mailbox_subject_reference,
                enumeration_max_items=self.discovery_profile.max_folders,
                enumeration_max_response_bytes=(
                    self.discovery_profile.max_response_bytes
                ),
            ),
            context=context,
            profile=self.discovery_profile,
        )
        return YandexMailSnapshotFolderDiscovery(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            complete=result.complete,
            code=YandexMailSnapshotFolderCode(result.code.value),
            discovered_count=result.discovered_count,
            approved_count=result.approved_count,
            folders=result.folders,
        )

    async def observe_folder_generation(
        self, scope: MailboxReadScope, folder_reference: UUID
    ) -> YandexMailSnapshotFolderGeneration:
        command = MailboxReadCommand(
            scope=scope,
            operation=EmailOperation.OBSERVE_FOLDER_GENERATION,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=folder_reference,
        )
        try:
            result = await self.reader.read(
                command,
                self._context(
                    scope,
                    "mailbox_snapshot_generation",
                    f"generation:{folder_reference}",
                ),
            )
        except Exception:
            result = failed_yandex_mail_folder_generation_observation(
                scope=scope,
                code=YandexMailFolderGenerationCode.SERVER_REJECTED,
            )
        if not isinstance(result, YandexMailFolderGenerationObservation):
            result = failed_yandex_mail_folder_generation_observation(
                scope=scope,
                code=YandexMailFolderGenerationCode.MALFORMED_RESPONSE,
            )
        return YandexMailSnapshotFolderGeneration(
            complete=result.complete,
            code=YandexMailSnapshotGenerationCode(result.code.value),
            workspace_id=result.workspace_id,
            environment_id=result.environment_id,
            connection_id=result.connection_id,
            connection_version_id=result.connection_version_id,
            mailbox_binding_id=result.mailbox_binding_id,
            folder_reference=result.folder_reference,
            authorization_generation=result.authorization_generation,
            configuration_generation=result.configuration_generation,
            scope_generation=result.scope_generation,
            uid_validity=result.uid_validity,
            uid_next=result.uid_next,
            read_only=result.read_only,
        )

    async def enumerate_uid_page(
        self, request: YandexMailSnapshotUidPageRequest
    ) -> YandexMailSnapshotUidPage:
        scope = request.scope
        connector_request = YandexMailUidEnumerationPageRequest(
            scope=scope,
            operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=request.folder_reference,
            enumeration_max_items=request.max_items,
            enumeration_max_response_bytes=request.max_response_bytes,
            generation=YandexMailFolderGenerationObservation(
                complete=request.generation.complete,
                code=YandexMailFolderGenerationCode(request.generation.code.value),
                workspace_id=request.generation.workspace_id,
                environment_id=request.generation.environment_id,
                connection_id=request.generation.connection_id,
                connection_version_id=request.generation.connection_version_id,
                mailbox_binding_id=request.generation.mailbox_binding_id,
                folder_reference=request.generation.folder_reference,
                authorization_generation=(
                    request.generation.authorization_generation
                ),
                configuration_generation=(
                    request.generation.configuration_generation
                ),
                scope_generation=request.generation.scope_generation,
                uid_validity=request.generation.uid_validity,
                uid_next=request.generation.uid_next,
                selection_command="EXAMINE",
                read_only=request.generation.read_only,
            ),
            start_uid=request.start_uid,
        )
        try:
            result = await self.reader.read(
                connector_request,
                self._context(
                    scope,
                    "mailbox_snapshot_uid_page",
                    f"uids:{request.folder_reference}:{request.start_uid}",
                ),
            )
        except Exception:
            result = failed_yandex_mail_uid_page(
                request=connector_request,
                code=YandexMailUidPageCode.SERVER_REJECTED,
            )
        if not isinstance(result, YandexMailUidEnumerationPageResult):
            result = failed_yandex_mail_uid_page(
                request=connector_request,
                code=YandexMailUidPageCode.MALFORMED_RESPONSE,
            )
        return YandexMailSnapshotUidPage(
            complete=result.complete,
            code=YandexMailSnapshotUidPageCode(result.code.value),
            workspace_id=result.workspace_id,
            environment_id=result.environment_id,
            connection_id=result.connection_id,
            connection_version_id=result.connection_version_id,
            mailbox_binding_id=result.mailbox_binding_id,
            folder_reference=result.folder_reference,
            authorization_generation=result.authorization_generation,
            configuration_generation=result.configuration_generation,
            scope_generation=result.scope_generation,
            uid_validity=result.uid_validity,
            covered_uid_range=result.covered_uid_range,
            messages=tuple(
                YandexMailSnapshotMessageMetadata(
                    uid=message.uid,
                    size_octets=message.size_octets,
                )
                for message in result.messages
            ),
            continuation_uid=result.continuation_uid,
        )

    def _context(
        self,
        scope: MailboxReadScope,
        operation: str,
        identity: str,
    ) -> TrustedPersistenceContext:
        trace_id = uuid5(
            NAMESPACE_URL,
            "spine:yandex-mail-initial-snapshot:v1:"
            f"{scope.workspace_id}:{scope.environment_id}:{scope.connection_id}:"
            f"{scope.authorization_generation}:{scope.configuration_generation}:"
            f"{scope.scope_generation}:{identity}",
        )
        return self.context_factory(PersistenceOperation(operation), trace_id)


@dataclass(frozen=True, slots=True)
class YandexMailCanonicalSnapshotMessageAdapter:
    """Apply one qualified UID through raw fetch and canonical acceptance."""

    ingestion: ConnectorIngestionOrchestrator
    raw_messages: YandexMailRawMessageAdmission

    async def apply(
        self, command: YandexMailSnapshotMessageCommand
    ) -> YandexMailSnapshotMessageReceipt:
        scope = command.scope
        digest = hashlib.sha256(command.effect_key.encode("ascii")).hexdigest()
        ingestion_command = ConnectorIngestionCommand(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            command_identity=f"yandex_mail.snapshot.{digest[:32]}",
            idempotency_key=command.effect_key,
            connection_id=scope.connection_id,
            resource_scope_reference=scope.resource_scope_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            resource_object_reference=str(command.uid),
            access_policy_version_id=command.access_policy_version_id,
        )
        orchestrator = replace(
            self.ingestion,
            observation_reader=YandexMailCanonicalObservationReader(
                admission=self.raw_messages,
                scope=scope,
                folder_reference=command.folder_reference,
                uid_validity=command.uid_validity,
                uid=command.uid,
                max_message_bytes=command.max_message_bytes,
            ),
            read_operation=PersistenceOperation("mailbox.read_raw_message"),
        )
        result = await orchestrator.ingest(ingestion_command)
        acceptance = (
            None
            if result.canonical_acceptance is None
            else result.canonical_acceptance.acceptance
        )
        if acceptance is None:
            context = orchestrator.context_factory(
                PersistenceOperation("canonical_acceptance"),
                uuid5(
                    NAMESPACE_URL,
                    "spine:yandex-mail-snapshot:current-acceptance:"
                    f"{scope.workspace_id}:{scope.environment_id}:"
                    f"{result.source.source_object_id}:{result.revision.revision_id}",
                ),
            )
            async with orchestrator.uow_factory(context) as uow:
                head = await uow.canonical.resolve_current(
                    result.source.source_object_id
                )
                acceptance = (
                    None
                    if head is None
                    else await uow.canonical.resolve_acceptance(head.acceptance_id)
                )
                await uow.rollback()
        if (
            acceptance is None
            or result.source.workspace_id != scope.workspace_id
            or result.source.environment_id != scope.environment_id
            or acceptance.workspace_id != scope.workspace_id
            or acceptance.environment_id != scope.environment_id
            or acceptance.source_object_id != result.source.source_object_id
            or acceptance.source_revision_id != result.revision.revision_id
        ):
            raise ValueError("canonical IMAP message receipt is outside its scope")
        acceptance_id = acceptance.canonical_acceptance_id
        return YandexMailSnapshotMessageReceipt(
            effect_key=command.effect_key,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            canonical_acceptance_id=acceptance_id,
            receipt_reference=str(acceptance_id),
        )


__all__ = [
    "YandexMailAdmittedSnapshotReader",
    "YandexMailCanonicalSnapshotMessageAdapter",
    "YandexMailInitialSnapshotAdapter",
]
