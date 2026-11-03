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


def email_message_artifact_digest(artifact: EmailMessageArtifact) -> str:
    payload = artifact.model_dump(mode="json", exclude={"artifact_sha256"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
