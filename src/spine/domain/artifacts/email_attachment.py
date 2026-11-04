"""Immutable original bytes derived from one exact MIME part."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from pydantic import Field, StrictInt, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.sources.original_reference import ObjectReference


class EmailAttachmentWriteReceipt(DefinitionModel):
    """Domain evidence that attachment bytes were finalized and verified."""

    schema_version: Literal["email-attachment-write-receipt:v1"] = (
        "email-attachment-write-receipt:v1"
    )
    upload_id: UUID
    object_reference: ObjectReference
    observed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_byte_length: StrictInt = Field(ge=0)
    verification_status: Literal["verified"]
    storage_status: Literal["finalized_verified"]
    finalized_at: datetime

    @model_validator(mode="after")
    def validate_evidence(self) -> "EmailAttachmentWriteReceipt":
        if (
            self.upload_id.int == 0
            or self.object_reference.digest_hex != self.observed_sha256
            or self.object_reference.byte_length != self.observed_byte_length
            or self.finalized_at.tzinfo is None
            or self.finalized_at.utcoffset() is None
        ):
            raise ValueError("email attachment write receipt is inconsistent")
        return self


class EmailAttachmentArtifact(DefinitionModel):
    """A verified attachment original whose identity is its parent MIME part."""

    schema_version: Literal["email-attachment-artifact:v1"] = (
        "email-attachment-artifact:v1"
    )
    artifact_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    canonical_acceptance_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_part_path: str = Field(pattern=r"^1(?:\.[1-9][0-9]*)*$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: StrictInt = Field(ge=0)
    media_type: str = Field(pattern=r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
    extraction_profile: str = Field(
        pattern=r"^email-attachment-extractor:v1:[0-9a-f]{64}$"
    )
    disposition: Literal["inline", "attachment"]
    filename: str | None = Field(default=None, max_length=1024)
    content_id: str | None = Field(default=None, max_length=998)
    write_receipt: EmailAttachmentWriteReceipt
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity_and_receipt(self) -> "EmailAttachmentArtifact":
        if any(
            value.int == 0
            for value in (
                self.artifact_id,
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                self.source_revision_id,
                self.canonical_acceptance_id,
            )
        ):
            raise ValueError("email attachment identifiers must be non-zero")
        if self.artifact_id != email_attachment_id(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            source_revision_id=self.source_revision_id,
            mime_part_path=self.mime_part_path,
            extraction_profile=self.extraction_profile,
        ):
            raise ValueError("email attachment identity is invalid")
        if (
            self.write_receipt.object_reference.digest_hex != self.content_sha256
            or self.write_receipt.object_reference.byte_length != self.byte_length
            or self.write_receipt.observed_sha256 != self.content_sha256
            or self.write_receipt.observed_byte_length != self.byte_length
            or self.write_receipt.verification_status != "verified"
        ):
            raise ValueError("email attachment receipt does not match content")
        if self.artifact_sha256 != email_attachment_artifact_digest(self):
            raise ValueError("email attachment artifact digest is invalid")
        return self


class EmailAttachmentProcessingDisposition(str, Enum):
    """Safe, content-free outcome for one independently processed MIME part."""

    PROCESSED = "processed"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    DANGEROUS = "dangerous"
    MISSING = "missing"
    OVERSIZED = "oversized"
    POLICY_DENIED = "policy_denied"
    FAILED = "failed"


class EmailAttachmentProcessingReceipt(DefinitionModel):
    """Immutable lineage from an attachment original to document ingestion."""

    schema_version: Literal["email-attachment-processing-receipt:v1"] = (
        "email-attachment-processing-receipt:v1"
    )
    processing_receipt_id: UUID
    workspace_id: UUID
    environment_id: UUID
    parent_source_object_id: UUID
    parent_source_revision_id: UUID
    parent_source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_canonical_acceptance_id: UUID
    attachment_artifact_id: UUID
    attachment_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_part_path: str = Field(pattern=r"^1(?:\.[1-9][0-9]*)*$")
    attachment_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attachment_byte_length: StrictInt = Field(ge=0)
    media_type: str = Field(pattern=r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
    policy_version: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$"
    )
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    processor_profile: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$"
    )
    disposition: EmailAttachmentProcessingDisposition
    original_disposition: Literal["retained", "retained_fenced"]
    child_source_object_id: UUID | None = None
    child_source_revision_id: UUID | None = None
    child_processing_receipt_id: UUID | None = None
    parsed_artifact_id: UUID | None = None
    parsed_artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_lineage(self) -> "EmailAttachmentProcessingReceipt":
        required_ids = (
            self.processing_receipt_id,
            self.workspace_id,
            self.environment_id,
            self.parent_source_object_id,
            self.parent_source_revision_id,
            self.parent_canonical_acceptance_id,
            self.attachment_artifact_id,
        )
        if any(value.int == 0 for value in required_ids):
            raise ValueError("email attachment processing identifiers must be non-zero")
        child_ids = (
            self.child_source_object_id,
            self.child_source_revision_id,
            self.child_processing_receipt_id,
        )
        if any(value is not None and value.int == 0 for value in child_ids):
            raise ValueError("email attachment child identifiers must be non-zero")
        has_child_source = (
            self.child_source_object_id is not None
            and self.child_source_revision_id is not None
        )
        if (self.child_source_object_id is None) != (
            self.child_source_revision_id is None
        ):
            raise ValueError("email attachment child source lineage must be complete")
        if self.child_processing_receipt_id is not None and not has_child_source:
            raise ValueError("child processing receipt requires child source lineage")
        has_parsed = (
            self.parsed_artifact_id is not None
            or self.parsed_artifact_digest is not None
        )
        if has_parsed and (
            self.parsed_artifact_id is None
            or self.parsed_artifact_id.int == 0
            or self.parsed_artifact_digest is None
        ):
            raise ValueError("parsed artifact lineage must be complete")
        if self.disposition in {
            EmailAttachmentProcessingDisposition.PROCESSED,
            EmailAttachmentProcessingDisposition.PARTIAL,
        }:
            if (
                not has_child_source
                or self.child_processing_receipt_id is None
                or not has_parsed
                or self.original_disposition != "retained"
            ):
                raise ValueError(
                    "processed attachment requires complete parsed lineage"
                )
        elif has_parsed or self.original_disposition != "retained_fenced":
            raise ValueError("unprocessed attachment must remain fenced")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        if self.receipt_sha256 != email_attachment_processing_receipt_digest(self):
            raise ValueError("email attachment processing receipt digest is invalid")
        return self


def email_attachment_id(
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_revision_id: UUID,
    mime_part_path: str,
    extraction_profile: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:email-attachment:v1:{}:{}:{}:{}:{}".format(
            workspace_id,
            environment_id,
            source_revision_id,
            mime_part_path,
            extraction_profile,
        ),
    )


def email_attachment_artifact_digest(artifact: EmailAttachmentArtifact) -> str:
    payload = artifact.model_dump(mode="json", exclude={"artifact_sha256"})
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def email_attachment_processing_receipt_digest(
    receipt: EmailAttachmentProcessingReceipt,
) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_sha256"})
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def email_attachment_processing_effect_digest(
    receipt: EmailAttachmentProcessingReceipt,
) -> str:
    """Identify one logical effect independently of observation time."""

    payload = receipt.model_dump(
        mode="json", exclude={"completed_at", "receipt_sha256"}
    )
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
