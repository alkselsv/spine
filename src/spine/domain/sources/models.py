"""Framework-independent immutable source observation models."""

from __future__ import annotations

import re
import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from .original_reference import ObjectReference
from .profile import BCP47_TABLE_DIGEST, REVISION_METADATA_SCHEMA, REVISION_PROFILE, REVISION_SCHEMA, UNICODE_TABLE_DIGEST, UNICODE_WHITESPACE_CODEPOINTS, normalize_language_tag, normalize_nfc

ALLOWED_TOMBSTONE_REASONS = frozenset(
    {"source_deleted", "provider_deleted", "legal_erasure", "retention_expired"}
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+\Z")

BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")]
DigestHex = Annotated[str, Field(pattern=r"[0-9a-f]{64}")]


class _VerifiedUploadResultReference(Protocol):
    receipt_id: UUID


class _VerifiedUploadResult(Protocol):
    issue8_result_reference: _VerifiedUploadResultReference
    upload_id: UUID
    object_id: UUID


class _VerifiedObservedContent(Protocol):
    observed_sha256: str
    observed_byte_length: int


class _VerifiedWriteReceipt(Protocol):
    upload_id: UUID
    object_reference: ObjectReference
    observed_content: _VerifiedObservedContent
    finalized_at: datetime


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


class ExternalFileRevisionMetadata(DefinitionModel):
    """Provider revision and identity evidence retained by canonical history."""

    schema_version: Literal["external-file-revision-metadata:v1"] = (
        "external-file-revision-metadata:v1"
    )
    provider_kind: BoundedIdentifier
    provider_revision: str = Field(min_length=1, max_length=128)
    provider_resource_id: str | None = Field(default=None, min_length=1, max_length=1024)
    resource_name: str | None = Field(default=None, min_length=1, max_length=1024)
    resource_path: str | None = Field(default=None, min_length=1, max_length=2048)
    content_md5: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    content_sha256: DigestHex | None = None
    provider_payload_digest: DigestHex | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    provider_payload: dict[str, str | int | None] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    byte_length: int = Field(ge=0)
    modified_at: datetime
    identity_assurance: BoundedIdentifier
    assurance_evidence_reference: BoundedIdentifier

    @model_validator(mode="after")
    def validate_modified_at(self) -> "ExternalFileRevisionMetadata":
        if self.modified_at.tzinfo is None or self.modified_at.utcoffset() is None:
            raise ValueError("external revision modified_at must be timezone-aware")
        return self


class RevisionMetadata(DefinitionModel):
    """Revision-bearing metadata shared by uploads and connector observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    embedded_title: str | None = None
    document_language: str | None = None
    representation_profile: BoundedIdentifier | None = None
    external_file_revision: ExternalFileRevisionMetadata | None = None

    @model_validator(mode="after")
    def validate_values(self) -> "RevisionMetadata":
        if self.embedded_title is not None:
            value = _normalize_text(self.embedded_title)
            if not value:
                raise ValueError("embedded_title must not normalize to empty")
            object.__setattr__(self, "embedded_title", value)
        if self.document_language is not None:
            value = _normalize_text(self.document_language)
            if not value:
                raise ValueError("document_language is not a valid BCP 47 tag")
            object.__setattr__(self, "document_language", normalize_language_tag(value))
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
            _nonzero(self.connection_id, "connection_id")
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
    revision_schema_version: Literal[REVISION_SCHEMA]
    canonicalization_profile: Literal[REVISION_PROFILE] = REVISION_PROFILE
    unicode_table_digest: Literal[UNICODE_TABLE_DIGEST] = UNICODE_TABLE_DIGEST
    bcp47_table_digest: Literal[BCP47_TABLE_DIGEST] = BCP47_TABLE_DIGEST
    revision_metadata_schema: Literal[REVISION_METADATA_SCHEMA]
    revision_metadata: RevisionMetadata | None = None
    revision_metadata_digest: DigestHex | None = None
    original_reference: ObjectReference | None = None
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
            if (
                self.original_reference.digest_hex != self.original_sha256
                or self.original_reference.byte_length != self.byte_length
            ):
                raise ValueError("original evidence does not match content identity")
            if self.revision_metadata_digest != canonical_revision_metadata_digest(self.revision_metadata):
                raise ValueError("revision metadata digest does not match metadata")
            media_type = _normalize_text(self.media_type).lower()
            if not _MEDIA_TYPE.fullmatch(media_type):
                raise ValueError("media_type must be lowercase ASCII type/subtype")
            object.__setattr__(self, "media_type", media_type)
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
            if self.deletion_reason not in ALLOWED_TOMBSTONE_REASONS:
                raise ValueError("tombstone deletion reason is not allowlisted")
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


class SourceRevisionReceiptAssociation(DefinitionModel):
    """Immutable verified physical evidence associated with one observation.

    The association is deliberately separate from ``SourceRevision``.  A
    repeated observation can reuse a revision while retaining a different
    finalized storage generation and its command/provenance lineage.
    """

    association_id: UUID
    source_object_id: UUID
    revision_id: UUID
    provenance_id: UUID
    workspace_id: UUID
    environment_id: UUID
    original_receipt_kind: Literal["issue6.write_receipt"]
    original_receipt_id: UUID
    original_command_result_id: UUID
    original_command_result_schema_version: int = Field(ge=1)
    original_upload_id: UUID
    original_reference: ObjectReference
    original_sha256: DigestHex
    byte_length: int = Field(ge=0)
    verification_status: Literal["verified"] = "verified"
    storage_status: Literal["finalized_verified"] = "finalized_verified"
    finalized_at: datetime
    associated_at: datetime

    @model_validator(mode="after")
    def validate_association(self) -> "SourceRevisionReceiptAssociation":
        for field_name in (
            "association_id",
            "source_object_id",
            "revision_id",
            "provenance_id",
            "workspace_id",
            "environment_id",
            "original_receipt_id",
            "original_command_result_id",
            "original_upload_id",
        ):
            _nonzero(getattr(self, field_name), field_name)
        if self.original_reference.digest_hex != self.original_sha256:
            raise ValueError("receipt digest does not match the original reference")
        if self.original_reference.byte_length != self.byte_length:
            raise ValueError("receipt length does not match the original reference")
        for field_name in ("finalized_at", "associated_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.associated_at < self.finalized_at:
            raise ValueError("association cannot precede physical finalization")
        return self

    @classmethod
    def from_verified_upload(
        cls,
        *,
        association_id: UUID,
        source_object_id: UUID,
        revision_id: UUID,
        provenance_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        original_receipt_id: UUID,
        command_result: _VerifiedUploadResult,
        write_receipt: _VerifiedWriteReceipt,
        associated_at: datetime,
    ) -> "SourceRevisionReceiptAssociation":
        """Create an association from the storage and upload result contracts.

        ``WriteReceipt`` performs the physical finalization/integrity checks;
        this boundary only carries their verified, immutable facts into the
        canonical source ledger and links them to the Issue #8 result.
        """

        if command_result.issue8_result_reference.receipt_id != original_receipt_id:
            raise ValueError("upload result is not linked to the supplied receipt")
        if command_result.upload_id != write_receipt.upload_id:
            raise ValueError("upload result and write receipt upload identities differ")
        if command_result.object_id != write_receipt.object_reference.object_id:
            raise ValueError("upload result and write receipt object identities differ")
        return cls(
            association_id=association_id,
            source_object_id=source_object_id,
            revision_id=revision_id,
            provenance_id=provenance_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            original_receipt_kind="issue6.write_receipt",
            original_receipt_id=original_receipt_id,
            original_command_result_id=command_result.upload_id,
            original_command_result_schema_version=1,
            original_upload_id=command_result.upload_id,
            original_reference=write_receipt.object_reference,
            original_sha256=write_receipt.observed_content.observed_sha256,
            byte_length=write_receipt.observed_content.observed_byte_length,
            finalized_at=write_receipt.finalized_at,
            associated_at=associated_at,
        )


def _normalize_text(value: str) -> str:
    # Unicode 15.1 White_Space is deliberately enumerated so this profile does
    # not inherit a host runtime's changing notion of whitespace.
    whitespace = "".join(chr(codepoint) for codepoint in UNICODE_WHITESPACE_CODEPOINTS)
    normalized = normalize_nfc(value)
    return normalized.strip(whitespace)


def canonical_revision_metadata_digest(metadata: RevisionMetadata) -> str:
    payload = {
        "document_language": metadata.document_language,
        "embedded_title": metadata.embedded_title,
    }
    if metadata.representation_profile is not None:
        payload["representation_profile"] = metadata.representation_profile
    if metadata.external_file_revision is not None:
        payload["external_file_revision"] = metadata.external_file_revision.model_dump(
            mode="json"
        )
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _nonzero(value: UUID, name: str) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{name} must be a non-zero UUID")
