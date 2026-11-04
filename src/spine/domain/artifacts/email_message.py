"""Immutable parsed and sanitized derivatives of an accepted email revision."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.artifacts.email_html import SanitizedEmailHtml
from spine.domain.artifacts.email_mime import MimeArtifact


class EmailMessageArtifact(DefinitionModel):
    """Versioned body artifact whose lineage is one canonical source revision."""

    schema_version: Literal["email-message-artifact:v1"] = "email-message-artifact:v1"
    artifact_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    canonical_acceptance_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_byte_length: StrictInt = Field(ge=0)
    parser_profile: str = Field(pattern=r"^mime-parser:v1:[0-9a-f]{64}$")
    sanitizer_profile: str = Field(pattern=r"^email-html-sanitizer:v1:[0-9a-f]{64}$")
    selection_profile: Literal["email-body-selection:v1"] = "email-body-selection:v1"
    mime: MimeArtifact
    sanitized_html: tuple[SanitizedEmailHtml, ...]
    selected_representation: Literal["plain_text", "sanitized_html_text", "none"]
    selected_text: str | None = None
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_lineage_and_digest(self) -> "EmailMessageArtifact":
        if (
            self.mime.raw_sha256 != self.raw_sha256
            or self.mime.raw_byte_length != self.raw_byte_length
            or self.mime.parser_profile != self.parser_profile
        ):
            raise ValueError("email artifact does not match its parsed source")
        if (self.selected_representation == "none") != (self.selected_text is None):
            raise ValueError("selected email body representation is inconsistent")
        if self.artifact_sha256 != email_message_artifact_digest(self):
            raise ValueError("email artifact digest is invalid")
        return self


class EmailMessageCitationLocator(DefinitionModel):
    """Content-free pointer to one range in one immutable email body artifact."""

    schema_version: Literal["email-message-locator:v1"] = "email-message-locator:v1"
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: UUID
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_profile: str = Field(pattern=r"^mime-parser:v1:[0-9a-f]{64}$")
    sanitizer_profile: str = Field(
        pattern=r"^email-html-sanitizer:v1:[0-9a-f]{64}$"
    )
    part_path: str = Field(pattern=r"^1(?:\.[1-9][0-9]*)*$")
    representation: Literal["plain_text", "sanitized_html_text"]
    start_codepoint: StrictInt = Field(ge=0)
    end_codepoint: StrictInt = Field(gt=0)
    representation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity_and_range(self) -> "EmailMessageCitationLocator":
        if self.source_revision_id.int == 0 or self.artifact_id.int == 0:
            raise ValueError("email message locator identities must be non-zero")
        if self.end_codepoint <= self.start_codepoint:
            raise ValueError("email message locator range is invalid")
        return self


_REPRESENTATION_CODE = {
    "plain_text": "p",
    "sanitized_html_text": "h",
}
_CODE_REPRESENTATION = {value: key for key, value in _REPRESENTATION_CODE.items()}


def encode_email_message_locator(locator: EmailMessageCitationLocator) -> str:
    """Encode the locator into the bounded stable-locator value contract."""

    reference = "|".join(
        (
            "em1",
            locator.source_revision_id.hex,
            locator.source_revision_digest,
            locator.artifact_id.hex,
            locator.artifact_digest,
            locator.parser_profile.removeprefix("mime-parser:v1:"),
            locator.sanitizer_profile.removeprefix("email-html-sanitizer:v1:"),
            locator.part_path,
            _REPRESENTATION_CODE[locator.representation],
            str(locator.start_codepoint),
            str(locator.end_codepoint),
            locator.representation_digest,
            locator.excerpt_digest,
        )
    )
    if len(reference) > 512:
        raise ValueError("email message locator exceeds the stable locator limit")
    return reference


def decode_email_message_locator(
    reference: str,
) -> EmailMessageCitationLocator | None:
    """Decode only the canonical v1 representation; malformed values fail closed."""

    try:
        fields = reference.split("|")
        if len(fields) != 13 or fields[0] != "em1":
            return None
        representation = _CODE_REPRESENTATION.get(fields[8])
        if representation is None:
            return None
        locator = EmailMessageCitationLocator(
            source_revision_id=UUID(hex=fields[1]),
            source_revision_digest=fields[2],
            artifact_id=UUID(hex=fields[3]),
            artifact_digest=fields[4],
            parser_profile=f"mime-parser:v1:{fields[5]}",
            sanitizer_profile=f"email-html-sanitizer:v1:{fields[6]}",
            part_path=fields[7],
            representation=representation,
            start_codepoint=int(fields[9]),
            end_codepoint=int(fields[10]),
            representation_digest=fields[11],
            excerpt_digest=fields[12],
        )
        return locator if encode_email_message_locator(locator) == reference else None
    except (KeyError, TypeError, ValueError):
        return None


def email_message_artifact_digest(artifact: EmailMessageArtifact) -> str:
    payload = artifact.model_dump(mode="json", exclude={"artifact_sha256"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EmailMessageArtifact",
    "EmailMessageCitationLocator",
    "decode_email_message_locator",
    "email_message_artifact_digest",
    "encode_email_message_locator",
]
