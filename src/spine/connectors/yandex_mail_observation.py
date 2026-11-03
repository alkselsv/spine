"""Map one admitted Yandex IMAP fetch into canonical connector ingestion."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol
from uuid import UUID

from spine.application.connections.email_read import (
    MailboxReadDeniedError,
)
from spine.application.connector_ingestion import ConnectorIngestionCommand
from spine.application.persistence.context import TrustedPersistenceContext
from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderImapMessageKey,
    ProviderImapRevisionEvidence,
    ProviderObservationDiscovery,
)
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchCommand,
    YandexMailRawMessageFetchResult,
)
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope
from spine.domain.connections.provider import OpaqueProviderReference, ProviderKind
from spine.domain.sources.imap import ImapMessageKey, imap_source_identity


class YandexMailCanonicalObservationError(ValueError):
    """Disclosure-safe rejection of incomplete or conflicting provider evidence."""

    def __init__(self) -> None:
        super().__init__("IMAP message observation rejected.")


class YandexMailRawMessageAdmission(Protocol):
    async def read(
        self,
        command: YandexMailRawMessageFetchCommand,
        context: TrustedPersistenceContext,
    ) -> YandexMailRawMessageFetchResult: ...


@dataclass(frozen=True, slots=True)
class YandexMailCanonicalObservationReader:
    """Authorized reader for one exact folder generation and UID."""

    admission: YandexMailRawMessageAdmission
    scope: MailboxReadScope
    folder_reference: UUID
    uid_validity: int
    uid: int
    max_message_bytes: int

    async def read(
        self,
        command: ConnectorIngestionCommand,
        context: TrustedPersistenceContext,
    ) -> NormalizedExternalObservationV2:
        if not self._matches_command(command):
            raise MailboxReadDeniedError()
        fetch_command = YandexMailRawMessageFetchCommand(
            scope=self.scope,
            operation=EmailOperation.READ_RAW_MESSAGE,
            mailbox_subject_reference=self.scope.mailbox_subject_reference,
            folder_or_label_reference=self.folder_reference,
            message_id=str(self.uid),
            uid_validity=self.uid_validity,
            uid=self.uid,
            max_message_bytes=self.max_message_bytes,
        )
        result = await self.admission.read(fetch_command, context)
        if (
            result.folder_reference != self.folder_reference
            or result.uid_validity != self.uid_validity
            or result.uid != self.uid
        ):
            raise YandexMailCanonicalObservationError()
        observation = yandex_mail_raw_message_observation(
            scope=self.scope,
            result=result,
        )
        if (
            command.expected_provider_revision is not None
            and command.expected_provider_revision
            != observation.provider_version_after
        ):
            raise YandexMailCanonicalObservationError()
        return observation

    def _matches_command(self, command: ConnectorIngestionCommand) -> bool:
        scope = self.scope
        return (
            scope.provider_kind == ProviderKind.YANDEX_MAIL.value
            and scope.allows(EmailOperation.READ_RAW_MESSAGE)
            and scope.allows_location(self.folder_reference)
            and command.workspace_id == scope.workspace_id
            and command.environment_id == scope.environment_id
            and command.connection_id == scope.connection_id
            and command.resource_scope_reference == scope.resource_scope_reference
            and command.authorization_generation == scope.authorization_generation
            and command.configuration_generation == scope.configuration_generation
            and command.resource_object_reference == str(self.uid)
        )


def yandex_mail_raw_message_observation(
    *,
    scope: MailboxReadScope,
    result: YandexMailRawMessageFetchResult,
) -> NormalizedExternalObservationV2:
    """Normalize complete BODY.PEEK evidence without parsing protected headers."""

    if not _complete_result_matches_scope(scope, result):
        raise YandexMailCanonicalObservationError()
    assert result.raw_bytes is not None
    assert result.content_sha256 is not None
    assert result.envelope is not None
    assert result.internal_date is not None

    key = ImapMessageKey(
        workspace_id=scope.workspace_id,
        connection_id=scope.connection_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_binding_id=result.folder_reference,
        uid_validity=result.uid_validity,
        uid=result.uid,
    )
    identity = imap_source_identity(key)
    key_payload = key.model_dump(mode="json")
    key_digest = _digest_json(key_payload)
    envelope_digest = hashlib.sha256(result.envelope).hexdigest()
    event_evidence = {
        "identity": key_payload,
        "content_sha256": result.content_sha256,
        "content_length": result.content_length,
        "declared_octets": result.declared_octets,
        "envelope_sha256": envelope_digest,
        "internal_date": result.internal_date.isoformat(),
        "fetch_data_item": result.fetch_data_item,
        "seen_preserved": result.seen_preserved,
        "mutation_evidence_complete": result.mutation_evidence_complete,
    }
    event_digest = _digest_json(event_evidence)
    provider_revision = (
        f"imap:{result.uid_validity}:{result.uid}:{result.content_sha256}"
    )
    return NormalizedExternalObservationV2(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        provider_kind=ProviderKind.YANDEX_MAIL,
        resource_scope_reference=OpaqueProviderReference(
            value=scope.resource_scope_reference
        ),
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        source_kind="email_message",
        external_identity=ProviderExternalIdentity(
            external_namespace=identity.external_namespace,
            external_generation=identity.external_generation,
            external_object_id=identity.external_object_id,
            provider_key=ProviderImapMessageKey(
                connection_id=scope.connection_id,
                workspace_id=scope.workspace_id,
                mailbox_binding_id=scope.mailbox_binding_id,
                folder_binding_id=result.folder_reference,
                uid_validity=result.uid_validity,
                uid=result.uid,
            ),
            assurance=ExternalIdentityAssurance.SCOPE_STABLE,
            assurance_evidence_reference="imap-message-key:v1",
        ),
        representation=BinaryObservationRepresentation(
            original_bytes=result.raw_bytes,
            media_type="message/rfc822",
            extraction_profile="raw_email_mime:v1",
            locator_contract_version="email_message:v1",
            imap_revision_evidence=ProviderImapRevisionEvidence(
                mailbox_binding_id=scope.mailbox_binding_id,
                location_binding_id=result.folder_reference,
                uid_validity=result.uid_validity,
                uid=result.uid,
                content_sha256=result.content_sha256,
                byte_length=result.content_length,
                declared_octets=result.declared_octets,
                envelope=result.envelope,
                envelope_sha256=envelope_digest,
                internal_date=result.internal_date,
                fetch_data_item="BODY.PEEK[]",
                seen_preserved=True,
                mutation_evidence_complete=True,
            ),
        ),
        coverage=ConnectorCoverage.COMPLETE,
        discovery=ProviderObservationDiscovery.DIRECT,
        producer_reference="yandex-mail-reader:v1",
        event_identity=f"imap-message:{identity.external_object_id[4:52]}",
        event_digest=event_digest,
        origin_locator_kind="imap-message-key",
        origin_locator_value=identity.external_object_id,
        origin_locator_schema="email_message:v1",
        origin_locator_digest=key_digest,
        observer_service="connector-worker",
        # The provider observation time is immutable replay evidence. A worker
        # wall clock would make crash recovery produce a different command.
        received_at=result.internal_date,
        observed_at=result.internal_date,
        provider_media_type="message/rfc822",
        provider_version_before=provider_revision,
        provider_version_after=provider_revision,
    )


def _complete_result_matches_scope(
    scope: MailboxReadScope,
    result: YandexMailRawMessageFetchResult,
) -> bool:
    return (
        result.code is YandexMailRawMessageFetchCode.COMPLETE
        and result.complete
        and result.workspace_id == scope.workspace_id
        and result.environment_id == scope.environment_id
        and result.connection_id == scope.connection_id
        and result.connection_version_id == scope.connection_version_id
        and result.mailbox_binding_id == scope.mailbox_binding_id
        and result.authorization_generation == scope.authorization_generation
        and result.configuration_generation == scope.configuration_generation
        and result.scope_generation == scope.scope_generation
        and scope.allows_location(result.folder_reference)
        and result.raw_bytes is not None
        and result.content_sha256 == hashlib.sha256(result.raw_bytes).hexdigest()
        and result.content_length == len(result.raw_bytes)
        and result.declared_octets == len(result.raw_bytes)
        and result.envelope is not None
        and result.internal_date is not None
        and result.internal_date.tzinfo is not None
        and result.internal_date.utcoffset() is not None
        and result.fetch_data_item == "BODY.PEEK[]"
        and result.seen_preserved is True
        and result.mutation_commands == ()
        and result.mutation_evidence_complete
    )


def _digest_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "YandexMailCanonicalObservationError",
    "YandexMailCanonicalObservationReader",
    "YandexMailRawMessageAdmission",
    "yandex_mail_raw_message_observation",
]
