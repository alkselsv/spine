"""Immutable evidence for bounded trusted-content verification."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import ValidatedDefinitionModel, VersionIdentifier

from .models import SourceRevision, SourceRevisionReceiptAssociation


class TrustedContentFormat(str, Enum):
    TEXT = "text"
    MARKDOWN = "markdown"
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


class TrustedContentVerificationOutcome(str, Enum):
    VERIFIED = "verified"
    DENIED = "denied"
    QUARANTINED = "quarantined"


class VerificationCleanupOutcome(str, Enum):
    COMPLETED = "completed"
    NOT_REQUIRED = "not_required"
    UNCERTAIN = "uncertain"


class TrustedContentVerificationEvidence(ValidatedDefinitionModel):
    """Content-free verifier result for one exact finalized receipt association."""

    schema_version: str = Field(default="trusted-content-verification:v1", frozen=True)
    evidence_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    receipt_association_id: UUID
    original_receipt_id: UUID
    original_upload_id: UUID
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    recognized_format: TrustedContentFormat
    format_profile_version: VersionIdentifier
    verifier_profile_version: VersionIdentifier
    bounds_profile_version: VersionIdentifier
    outcome: TrustedContentVerificationOutcome
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=8)
    cleanup_outcome: VerificationCleanupOutcome
    verified_at: datetime
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> "TrustedContentVerificationEvidence":
        for name in (
            "evidence_id",
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
            "receipt_association_id",
            "original_receipt_id",
            "original_upload_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("verified_at must be timezone-aware")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        if any(
            not code
            or len(code) > 64
            or not code.replace("_", "").replace("-", "").isalnum()
            for code in self.reason_codes
        ):
            raise ValueError("reason_codes must be disclosure-safe identifiers")
        if self.outcome is TrustedContentVerificationOutcome.VERIFIED:
            if self.recognized_format in {
                TrustedContentFormat.UNKNOWN,
                TrustedContentFormat.AMBIGUOUS,
            }:
                raise ValueError("verified evidence requires one recognized format")
            if self.cleanup_outcome is VerificationCleanupOutcome.UNCERTAIN:
                raise ValueError("cleanup uncertainty cannot be reported as success")
        if self.evidence_digest != _evidence_digest(self.model_dump(exclude={"evidence_digest"})):
            raise ValueError("evidence_digest does not match immutable verifier evidence")
        return self

    @classmethod
    def issue(
        cls,
        *,
        evidence_id: UUID,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
        recognized_format: TrustedContentFormat,
        format_profile_version: str,
        verifier_profile_version: str,
        bounds_profile_version: str,
        outcome: TrustedContentVerificationOutcome,
        reason_codes: tuple[str, ...],
        cleanup_outcome: VerificationCleanupOutcome,
        verified_at: datetime,
    ) -> "TrustedContentVerificationEvidence":
        assert_exact_revision_receipt_binding(
            revision=revision, association=association
        )
        values = {
            "schema_version": "trusted-content-verification:v1",
            "evidence_id": evidence_id,
            "workspace_id": revision.workspace_id,
            "environment_id": revision.environment_id,
            "source_object_id": revision.source_object_id,
            "source_revision_id": revision.revision_id,
            "receipt_association_id": association.association_id,
            "original_receipt_id": association.original_receipt_id,
            "original_upload_id": association.original_upload_id,
            "original_sha256": association.original_sha256,
            "byte_length": association.byte_length,
            "recognized_format": recognized_format,
            "format_profile_version": format_profile_version,
            "verifier_profile_version": verifier_profile_version,
            "bounds_profile_version": bounds_profile_version,
            "outcome": outcome,
            "reason_codes": reason_codes,
            "cleanup_outcome": cleanup_outcome,
            "verified_at": verified_at,
        }
        return cls(**values, evidence_digest=_evidence_digest(values))

    def assert_matches(
        self,
        *,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
    ) -> None:
        assert_exact_revision_receipt_binding(
            revision=revision, association=association
        )
        expected = (
            revision.workspace_id,
            revision.environment_id,
            revision.source_object_id,
            revision.revision_id,
            association.association_id,
            association.original_receipt_id,
            association.original_upload_id,
            association.original_sha256,
            association.byte_length,
        )
        actual = (
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.source_revision_id,
            self.receipt_association_id,
            self.original_receipt_id,
            self.original_upload_id,
            self.original_sha256,
            self.byte_length,
        )
        if actual != expected:
            raise ValueError("evidence does not bind the exact finalized receipt association")


def assert_exact_revision_receipt_binding(
    *, revision: SourceRevision, association: SourceRevisionReceiptAssociation
) -> None:
    if (
        revision.workspace_id != association.workspace_id
        or revision.environment_id != association.environment_id
        or revision.source_object_id != association.source_object_id
        or revision.revision_id != association.revision_id
        or revision.original_reference != association.original_reference
        or revision.original_sha256 != association.original_sha256
        or revision.byte_length != association.byte_length
        or association.storage_status != "finalized_verified"
        or association.verification_status != "verified"
    ):
        raise ValueError("revision does not match the exact finalized receipt association")


def _evidence_digest(values: object) -> str:
    encoded = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: value.value if isinstance(value, Enum) else str(value),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "TrustedContentFormat",
    "TrustedContentVerificationEvidence",
    "TrustedContentVerificationOutcome",
    "VerificationCleanupOutcome",
    "assert_exact_revision_receipt_binding",
]
