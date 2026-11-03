"""Map admitted Gmail RAW results into provider-neutral source observations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import parsedate_to_datetime
from enum import Enum
import hashlib
import json
from typing import Protocol
from uuid import UUID

from spine.application.connections.email_read import (
    MailboxReadAdmission,
    MailboxReadCommand,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.connectors.gmail import GmailRawMessageResult, GmailRawMessageStatus
from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    EmailObservationEvidence,
    EmailObservationRepresentation,
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderEmailMessageKey,
    ProviderExternalIdentity,
)
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope
from spine.domain.connections.provider import OpaqueProviderReference, ProviderKind
from spine.domain.sources.gmail import GmailMessageKey, gmail_source_identity


class GmailObservationFailureCode(str, Enum):
    NOT_ELIGIBLE = "not_eligible"
    STALE_AUTHORITY = "stale_authority"
    MALFORMED_EVIDENCE = "malformed_evidence"


class GmailObservationMappingError(ValueError):
    """Disclosure-safe rejection at the Gmail observation boundary."""

    def __init__(self, code: GmailObservationFailureCode) -> None:
        self.code = code
        super().__init__("Gmail RAW result cannot be mapped.")


class GmailObservationCommand(Protocol):
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int
    configuration_generation: int
    resource_object_reference: str | None


@dataclass(frozen=True, slots=True)
class GmailCanonicalObservationReader:
    """Read and map one exact Gmail message behind mailbox admission."""

    admission: MailboxReadAdmission[GmailRawMessageResult]
    scope: MailboxReadScope
    authorized_label_reference: UUID
    clock: Callable[[], datetime]

    async def read(
        self,
        command: GmailObservationCommand,
        context: TrustedPersistenceContext,
    ) -> NormalizedExternalObservationV2:
        scope = self.scope
        if (
            command.workspace_id,
            command.environment_id,
            command.connection_id,
            command.resource_scope_reference,
            command.authorization_generation,
            command.configuration_generation,
        ) != (
            scope.workspace_id,
            scope.environment_id,
            scope.connection_id,
            scope.resource_scope_reference,
            scope.authorization_generation,
            scope.configuration_generation,
        ) or command.resource_object_reference is None:
            raise GmailObservationMappingError(
                GmailObservationFailureCode.STALE_AUTHORITY
            )
        try:
            result = await self.admission.read(
                MailboxReadCommand(
                    scope=scope,
                    operation=EmailOperation.READ_RAW_MESSAGE,
                    mailbox_subject_reference=scope.mailbox_subject_reference,
                    folder_or_label_reference=self.authorized_label_reference,
                    message_id=command.resource_object_reference,
                ),
                context,
            )
            if (
                result.mailbox_scope != scope
                or result.requested_message_id != command.resource_object_reference
                or result.authorized_label_reference
                != self.authorized_label_reference
            ):
                raise GmailObservationMappingError(
                    GmailObservationFailureCode.MALFORMED_EVIDENCE
                )
            return gmail_raw_message_observation(
                result,
                connection_version_id=scope.connection_version_id,
                received_at=self.clock(),
            )
        except GmailObservationMappingError:
            raise
        except Exception:
            raise GmailObservationMappingError(
                GmailObservationFailureCode.NOT_ELIGIBLE
            ) from None


def gmail_raw_message_observation(
    result: GmailRawMessageResult,
    *,
    connection_version_id: UUID,
    received_at: datetime,
) -> NormalizedExternalObservationV2:
    """Create one immutable observation from an already admitted Gmail RAW read."""

    if (
        result.status is not GmailRawMessageStatus.ACCEPTED
        or result.raw_mime is None
        or result.message_id is None
        or result.sha256 is None
        or result.internal_date_ms is None
        or result.history_id is None
        or received_at.tzinfo is None
        or received_at.utcoffset() is None
    ):
        raise GmailObservationMappingError(GmailObservationFailureCode.NOT_ELIGIBLE)

    scope = result.mailbox_scope
    if connection_version_id != scope.connection_version_id:
        raise GmailObservationMappingError(GmailObservationFailureCode.STALE_AUTHORITY)
    try:
        return _map_eligible_result(
            result,
            connection_version_id=connection_version_id,
            received_at=received_at,
        )
    except GmailObservationMappingError:
        raise
    except Exception:
        raise GmailObservationMappingError(
            GmailObservationFailureCode.MALFORMED_EVIDENCE
        ) from None


def _map_eligible_result(
    result: GmailRawMessageResult,
    *,
    connection_version_id: UUID,
    received_at: datetime,
) -> NormalizedExternalObservationV2:
    scope = result.mailbox_scope
    assert result.raw_mime is not None
    assert result.message_id is not None
    assert result.sha256 is not None
    assert result.internal_date_ms is not None
    assert result.history_id is not None
    internal_date = datetime.fromtimestamp(
        result.internal_date_ms / 1000, tz=timezone.utc
    )
    headers = _allowlisted_headers(result.raw_mime)
    header_date = _header_date(headers.get("date", ()))
    message_key = GmailMessageKey(
        connection_id=scope.connection_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        message_id=result.message_id,
    )
    gmail_identity = gmail_source_identity(message_key)
    evidence = EmailObservationEvidence(
        provider_message_id=result.message_id,
        provider_received_at=received_at,
        internal_date=internal_date,
        header_date=header_date,
        provider_thread_reference=result.thread_id,
        rfc_message_id_claims=headers.get("message-id", ()),
        references_claims=headers.get("references", ()),
        in_reply_to_claims=headers.get("in-reply-to", ()),
    )
    event_payload = {
        "connection_id": str(scope.connection_id),
        "mailbox_binding_id": str(scope.mailbox_binding_id),
        "message_id": result.message_id,
        "history_id": result.history_id,
        "raw_sha256": result.sha256,
        "evidence": evidence.model_dump(
            mode="json", exclude={"provider_received_at"}
        ),
    }
    event_digest = hashlib.sha256(
        json.dumps(
            event_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return NormalizedExternalObservationV2(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=connection_version_id,
        provider_kind=ProviderKind.GOOGLE,
        resource_scope_reference=OpaqueProviderReference(
            value=scope.resource_scope_reference
        ),
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        source_kind="email_message",
        external_identity=ProviderExternalIdentity(
            external_namespace=gmail_identity.external_namespace,
            external_generation=gmail_identity.external_generation,
            external_object_id=gmail_identity.external_object_id,
            provider_key=ProviderEmailMessageKey(
                connection_id=scope.connection_id,
                mailbox_binding_id=scope.mailbox_binding_id,
                provider_object_id=result.message_id,
            ),
            assurance=ExternalIdentityAssurance.PROVIDER_STABLE,
            assurance_evidence_reference="gmail_raw_message:v1",
        ),
        representation=EmailObservationRepresentation(
            original_bytes=result.raw_mime,
            media_type="message/rfc822",
            extraction_profile="raw_email_mime:v1",
            locator_contract_version="email_message:v1",
            email_evidence=evidence,
        ),
        coverage=ConnectorCoverage.COMPLETE,
        producer_reference="gmail_raw_observer",
        event_identity=f"gmail_message:{event_digest[:48]}",
        event_digest=event_digest,
        observer_service="gmail_connector",
        received_at=received_at,
        observed_at=internal_date,
        provider_media_type="message/rfc822",
        provider_version_before=result.history_id,
        provider_version_after=result.history_id,
    )


def _allowlisted_headers(raw_mime: bytes) -> dict[str, tuple[str, ...]]:
    boundary = raw_mime.find(b"\r\n\r\n")
    separator_length = 4
    if boundary < 0:
        boundary = raw_mime.find(b"\n\n")
        separator_length = 2
    header_bytes = (
        raw_mime
        if boundary < 0
        else raw_mime[: boundary + separator_length]
    )
    message = BytesHeaderParser(policy=compat32).parsebytes(header_bytes)
    return {
        name: tuple(str(value) for value in message.get_all(name, ())[:32])
        for name in ("date", "message-id", "references", "in-reply-to")
    }


def _header_date(values: tuple[str, ...]) -> datetime | None:
    if not values:
        return None
    try:
        value = parsedate_to_datetime(values[0])
    except (TypeError, ValueError, OverflowError):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


__all__ = [
    "GmailCanonicalObservationReader",
    "GmailObservationFailureCode",
    "GmailObservationMappingError",
    "gmail_raw_message_observation",
]
