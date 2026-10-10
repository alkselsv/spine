"""Framework-independent immutable source observation models."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+\Z")
_LANGUAGE = re.compile(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*\Z")

BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=64)]
DigestHex = Annotated[str, Field(pattern=r"[0-9a-f]{64}")]


class SourceKind(str, Enum):
    DOCUMENT = "document"
    MESSAGE = "message"
    RECORD = "record"


class IdentityMode(str, Enum):
    CONNECTOR = "connector"
    UPLOAD = "upload"


class RevisionKind(str, Enum):
    CONTENT = "content"
    TOMBSTONE = "tombstone"


class RevisionMetadata(DefinitionModel):
    """The only revision-bearing metadata in the R1 document profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    embedded_title: str | None = None
    document_language: str | None = None

    @model_validator(mode="after")
    def validate_values(self) -> "RevisionMetadata":
        if self.embedded_title is not None:
            value = _normalize_text(self.embedded_title)
            if not value:
                raise ValueError("embedded_title must not normalize to empty")
            object.__setattr__(self, "embedded_title", value)
        if self.document_language is not None:
            value = _normalize_text(self.document_language)
            if not value or _LANGUAGE.fullmatch(value) is None:
                raise ValueError("document_language is not a valid BCP 47 tag")
            object.__setattr__(self, "document_language", value.lower())
        return self


class SourceObject(DefinitionModel):
    """Stable tenant-scoped logical identity, independent of content."""

    source_object_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_kind: BoundedIdentifier
    identity_mode: IdentityMode
    connection_id: UUID | None = None
    external_namespace: BoundedIdentifier | None = None
    external_generation: BoundedIdentifier | None = None
    external_object_id: BoundedIdentifier | None = None
    upload_identity: BoundedIdentifier | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_identity(self) -> "SourceObject":
        _nonzero(self.source_object_id, "source_object_id")
        _nonzero(self.workspace_id, "workspace_id")
        _nonzero(self.environment_id, "environment_id")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        connector = (
            self.connection_id,
            self.external_namespace,
            self.external_generation,
            self.external_object_id,
        )
        if self.identity_mode is IdentityMode.CONNECTOR:
            if any(value is None for value in connector) or self.upload_identity is not None:
                raise ValueError("connector identity must be complete and exclusive")
        elif self.identity_mode is IdentityMode.UPLOAD:
            if self.upload_identity is None or any(value is not None for value in connector):
                raise ValueError("upload identity must be complete and exclusive")
        else:
            raise ValueError("unsupported identity mode")
        return self


class SourceRevision(DefinitionModel):
    """Immutable observed revision; it never selects canonical current state."""

    revision_id: UUID
    source_object_id: UUID
    workspace_id: UUID
    environment_id: UUID
    kind: RevisionKind
    revision_digest: DigestHex
    revision_schema_version: BoundedIdentifier
    revision_metadata_schema: BoundedIdentifier
    revision_metadata: RevisionMetadata | None = None
    revision_metadata_digest: DigestHex | None = None
    # Opaque Issue #6 receipt/reference contract.  The domain deliberately
    # does not import the infrastructure-owned storage adapter.
    original_reference: Any = None
    original_sha256: DigestHex | None = None
    byte_length: int | None = Field(default=None, ge=0)
    media_type: str | None = None
    reappearance_after_tombstone_revision_id: UUID | None = None
    deletion_reason: BoundedIdentifier | None = None
    deletion_provenance: BoundedIdentifier | None = None
    observed_at: datetime

    @model_validator(mode="after")
    def validate_kind(self) -> "SourceRevision":
        for field_name in ("revision_id", "source_object_id", "workspace_id", "environment_id"):
            _nonzero(getattr(self, field_name), field_name)
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.kind is RevisionKind.CONTENT:
            if (
                self.original_reference is None
                or self.original_sha256 is None
                or self.byte_length is None
                or self.media_type is None
                or self.revision_metadata is None
                or self.revision_metadata_digest is None
            ):
                raise ValueError("content revision requires complete original evidence")
            reference_digest = getattr(self.original_reference, "digest_hex", None)
            reference_length = getattr(self.original_reference, "byte_length", None)
            if reference_digest != self.original_sha256 or reference_length != self.byte_length:
                raise ValueError("original evidence does not match content identity")
            if not _MEDIA_TYPE.fullmatch(self.media_type) or self.media_type != self.media_type.lower():
                raise ValueError("media_type must be lowercase ASCII type/subtype")
            if self.deletion_reason is not None or self.deletion_provenance is not None:
                raise ValueError("content revision cannot contain deletion provenance")
        elif self.kind is RevisionKind.TOMBSTONE:
            if any(
                value is not None
                for value in (
                    self.original_reference,
                    self.original_sha256,
                    self.byte_length,
                    self.media_type,
                    self.revision_metadata,
                    self.revision_metadata_digest,
                    self.reappearance_after_tombstone_revision_id,
                )
            ) or self.deletion_reason is None or self.deletion_provenance is None:
                raise ValueError("tombstone requires deletion provenance and no content")
        return self


class SourceRevisionProvenance(DefinitionModel):
    """One immutable delivery/observation fact attached to a revision."""

    provenance_id: UUID
    source_object_id: UUID
    revision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    producer_kind: BoundedIdentifier
    producer_reference: BoundedIdentifier
    event_identity: BoundedIdentifier
    event_digest: DigestHex
    connection_id: UUID | None = None
    upload_command_reference: BoundedIdentifier | None = None
    origin_locator_kind: BoundedIdentifier | None = None
    origin_locator_value: BoundedIdentifier | None = None
    origin_locator_schema: BoundedIdentifier | None = None
    origin_locator_digest: DigestHex | None = None
    order_scheme: BoundedIdentifier | None = None
    order_token: BoundedIdentifier | None = None
    observer_service: BoundedIdentifier
    received_at: datetime
    observed_at: datetime

    @model_validator(mode="after")
    def validate_provenance(self) -> "SourceRevisionProvenance":
        for field_name in ("provenance_id", "source_object_id", "revision_id", "workspace_id", "environment_id"):
            _nonzero(getattr(self, field_name), field_name)
        if (self.order_scheme is None) != (self.order_token is None):
            raise ValueError("order_scheme and order_token must be supplied together")
        for value in (self.received_at, self.observed_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("provenance timestamps must be timezone-aware")
        return self


def _normalize_text(value: str) -> str:
    import unicodedata

    # Unicode 15.1 White_Space is deliberately enumerated so this profile does
    # not inherit a host runtime's changing notion of whitespace.
    whitespace = "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    whitespace += "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    whitespace += "\u2028\u2029\u202f\u205f\u3000"
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return normalized.strip(whitespace)


def _nonzero(value: UUID, name: str) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{name} must be a non-zero UUID")
