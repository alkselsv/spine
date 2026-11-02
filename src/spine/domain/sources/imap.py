"""Conservative, UIDVALIDITY-qualified IMAP message identity contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, model_validator

from spine.domain.common import DefinitionModel


IMAP_MESSAGE_KEY_SCHEMA = "imap-message-key:v1"
IMAP_SOURCE_IDENTITY_SCHEMA = "imap-source-identity:v1"
IMAP_SIMILARITY_EVIDENCE_SCHEMA = "imap-message-similarity-evidence:v1"
IMAP_EXTERNAL_NAMESPACE = "imap"
IMAP_EXTERNAL_GENERATION_PREFIX = "imap-uidvalidity-v1:"

ImapUidValidity = Annotated[StrictInt, Field(ge=1, le=2**32 - 1)]
ImapUid = Annotated[StrictInt, Field(ge=1, le=2**32 - 1)]
MessageIdValue = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
Sha256Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class ImapMessageKey(DefinitionModel):
    """Identity in one approved tenant, connection, mailbox and folder scope."""

    schema_version: Literal[IMAP_MESSAGE_KEY_SCHEMA] = IMAP_MESSAGE_KEY_SCHEMA
    workspace_id: UUID
    connection_id: UUID
    mailbox_binding_id: UUID
    folder_binding_id: UUID
    uid_validity: ImapUidValidity
    uid: ImapUid

    @model_validator(mode="after")
    def validate_scope(self) -> "ImapMessageKey":
        for field_name in (
            "workspace_id",
            "connection_id",
            "mailbox_binding_id",
            "folder_binding_id",
        ):
            if getattr(self, field_name).int == 0:
                raise ValueError(f"{field_name} must be a non-zero UUID")
        return self


class ImapSourceIdentity(DefinitionModel):
    """Bounded opaque identity paired with its inspectable IMAP key."""

    schema_version: Literal[IMAP_SOURCE_IDENTITY_SCHEMA] = IMAP_SOURCE_IDENTITY_SCHEMA
    external_namespace: Literal[IMAP_EXTERNAL_NAMESPACE] = IMAP_EXTERNAL_NAMESPACE
    external_generation: str = Field(min_length=1, max_length=64)
    external_object_id: str = Field(pattern=r"^im1:[a-z2-7]{52}$", max_length=64)
    provider_key: ImapMessageKey

    @model_validator(mode="after")
    def validate_identity_matches_key(self) -> "ImapSourceIdentity":
        expected_generation = imap_external_generation(self.provider_key.uid_validity)
        if self.external_generation != expected_generation:
            raise ValueError("external_generation does not match UIDVALIDITY")
        if self.external_object_id != _encode_imap_source_key(self.provider_key):
            raise ValueError("external_object_id does not match provider key")
        return self


class ImapMessageSimilarityEvidence(DefinitionModel):
    """Similarity evidence; matching values make no identity or continuity claim."""

    schema_version: Literal[IMAP_SIMILARITY_EVIDENCE_SCHEMA] = (
        IMAP_SIMILARITY_EVIDENCE_SCHEMA
    )
    evidence_kind: Literal["message_similarity_only"] = "message_similarity_only"
    first_message_key: ImapMessageKey
    second_message_key: ImapMessageKey
    basis: Literal["message_id", "content_sha256"]
    value: MessageIdValue | Sha256Digest

    @model_validator(mode="after")
    def validate_basis_value(self) -> "ImapMessageSimilarityEvidence":
        if (
            self.basis == "content_sha256"
            and re.fullmatch(r"[0-9a-f]{64}", self.value) is None
        ):
            raise ValueError("content_sha256 similarity value must be a SHA-256 digest")
        return self


def imap_external_generation(uid_validity: int) -> str:
    """Return the explicit identity generation established by UIDVALIDITY."""

    if isinstance(uid_validity, bool) or not isinstance(uid_validity, int) or not (
        1 <= uid_validity <= 2**32 - 1
    ):
        raise ValueError("UIDVALIDITY must be an unsigned non-zero 32-bit value")
    return f"{IMAP_EXTERNAL_GENERATION_PREFIX}{uid_validity}"


def imap_source_identity(key: ImapMessageKey) -> ImapSourceIdentity:
    """Create a bounded stable identity from the full approved IMAP message key."""

    return ImapSourceIdentity(
        external_generation=imap_external_generation(key.uid_validity),
        external_object_id=_encode_imap_source_key(key),
        provider_key=key,
    )


def _encode_imap_source_key(key: ImapMessageKey) -> str:
    canonical = json.dumps(
        key.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"im1:{encoded}"
