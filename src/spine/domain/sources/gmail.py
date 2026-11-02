"""Provider-qualified Gmail identity and conversation evidence contracts.

These domain types deliberately do not parse or trust mail headers. Provider
IDs identify messages; RFC headers are retained as lower-authority evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictStr, model_validator

from spine.domain.common import DefinitionModel


GMAIL_SOURCE_IDENTITY_SCHEMA = "gmail-source-identity:v1"
GMAIL_EXTERNAL_NAMESPACE = "gmail"
GMAIL_EXTERNAL_GENERATION = "gmail-source-key:v1"
GMAIL_MESSAGE_KEY_SCHEMA = "gmail-message-key:v1"
GMAIL_THREAD_KEY_SCHEMA = "gmail-thread-key:v1"
GMAIL_THREAD_EVIDENCE_SCHEMA = "gmail-thread-evidence:v1"
GMAIL_HEADER_RELATIONSHIP_SCHEMA = "gmail-header-relationship-evidence:v1"
RFC_MESSAGE_ID_EVIDENCE_SCHEMA = "rfc-message-id-evidence:v1"

_PROVIDER_ID_PATTERN = r"^[!-~]{1,1024}$"
GmailProviderIdentifier = Annotated[
    StrictStr,
    Field(min_length=1, max_length=1024, pattern=_PROVIDER_ID_PATTERN),
]
HeaderValue = Annotated[StrictStr, Field(max_length=4096)]


class GmailMessageKey(DefinitionModel):
    """Canonical provider key scoped to one connection and approved mailbox."""

    schema_version: Literal[GMAIL_MESSAGE_KEY_SCHEMA] = GMAIL_MESSAGE_KEY_SCHEMA
    connection_id: UUID
    mailbox_binding_id: UUID
    message_id: GmailProviderIdentifier

    @model_validator(mode="after")
    def validate_scope(self) -> "GmailMessageKey":
        if self.connection_id.int == 0 or self.mailbox_binding_id.int == 0:
            raise ValueError("Gmail identity scope identifiers must be non-zero")
        return self


class GmailSourceIdentity(DefinitionModel):
    """Bounded opaque source ID paired with its inspectable provider key."""

    schema_version: Literal[GMAIL_SOURCE_IDENTITY_SCHEMA] = GMAIL_SOURCE_IDENTITY_SCHEMA
    external_namespace: Literal[GMAIL_EXTERNAL_NAMESPACE] = GMAIL_EXTERNAL_NAMESPACE
    external_generation: Literal[GMAIL_EXTERNAL_GENERATION] = GMAIL_EXTERNAL_GENERATION
    external_object_id: str = Field(pattern=r"^gm1:[a-z2-7]{52}$", max_length=56)
    provider_key: GmailMessageKey

    @model_validator(mode="after")
    def validate_opaque_identifier(self) -> "GmailSourceIdentity":
        if self.external_object_id != _encode_gmail_source_key(self.provider_key):
            raise ValueError("external_object_id does not match provider key")
        return self


class GmailThreadKey(DefinitionModel):
    """Mailbox-qualified provider thread ID; never a message identity."""

    schema_version: Literal[GMAIL_THREAD_KEY_SCHEMA] = GMAIL_THREAD_KEY_SCHEMA
    connection_id: UUID
    mailbox_binding_id: UUID
    thread_id: GmailProviderIdentifier

    @model_validator(mode="after")
    def validate_scope(self) -> "GmailThreadKey":
        if self.connection_id.int == 0 or self.mailbox_binding_id.int == 0:
            raise ValueError("Gmail identity scope identifiers must be non-zero")
        return self


class GmailThreadEvidence(DefinitionModel):
    """Provider-reported thread association within the same approved mailbox."""

    schema_version: Literal[GMAIL_THREAD_EVIDENCE_SCHEMA] = (
        GMAIL_THREAD_EVIDENCE_SCHEMA
    )
    evidence_kind: Literal["provider_reported_thread_id"] = "provider_reported_thread_id"
    message_key: GmailMessageKey
    thread_key: GmailThreadKey

    @model_validator(mode="after")
    def validate_mailbox_scope(self) -> "GmailThreadEvidence":
        if (
            self.message_key.connection_id != self.thread_key.connection_id
            or self.message_key.mailbox_binding_id != self.thread_key.mailbox_binding_id
        ):
            raise ValueError("Gmail thread evidence must share the message mailbox scope")
        return self


class RfcMessageIdEvidence(DefinitionModel):
    """Optional, untrusted RFC Message-ID header; duplicates are retained as-is."""

    schema_version: Literal[RFC_MESSAGE_ID_EVIDENCE_SCHEMA] = (
        RFC_MESSAGE_ID_EVIDENCE_SCHEMA
    )
    evidence_kind: Literal["untrusted_rfc_message_id"] = "untrusted_rfc_message_id"
    message_key: GmailMessageKey
    value: HeaderValue | None = None


class GmailHeaderRelationshipEvidence(DefinitionModel):
    """Header-inferred relationship claim, with raw value and kind preserved."""

    schema_version: Literal[GMAIL_HEADER_RELATIONSHIP_SCHEMA] = (
        GMAIL_HEADER_RELATIONSHIP_SCHEMA
    )
    evidence_kind: Literal["header_inferred"] = "header_inferred"
    relation: Literal["references", "in_reply_to"]
    message_key: GmailMessageKey
    header_value: HeaderValue


def gmail_source_identity(key: GmailMessageKey) -> GmailSourceIdentity:
    """Build the deterministic source identity from connection, mailbox and ID."""

    return GmailSourceIdentity(
        external_object_id=_encode_gmail_source_key(key),
        provider_key=key,
    )


def _encode_gmail_source_key(key: GmailMessageKey) -> str:
    payload = key.model_dump(mode="json")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"gm1:{encoded}"
