"""Application orchestration for bounded original-content validation.

The upload and validation boundaries are intentionally separate.  A physical
write is finalized and represented by an Issue #6 ``WriteReceipt`` before a
validator receives any content.  Validation returns an immutable evidence
record that callers can hand to the Issue #7 admission boundary; it never
changes the storage receipt.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import PurePath
from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, PositiveInt, model_validator

from spine.application.admission import (
    AdmissionDecisionService,
    AdmissionValidationEvidence,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.storage import OriginalUploadOutcome
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources import (
    AdmissionDisposition,
    AdmissionReference,
    ReasonCode,
    SourceRevision,
    SourceRevisionProvenance,
)
from spine.infrastructure.object_storage.contracts import (
    AuthorizedReadGrantRegistry,
    OriginalUploadCommandInput,
    WriteReceipt,
)
from spine.infrastructure.object_storage.port import OriginalObjectStore


_VALIDATOR_VERSION = "content-validator:r1"
_REASON_SCHEMA_VERSION = "content-validation-reasons:r1"
_VALIDATION_PURPOSE = "document.validation.v1"
_VALIDATION_AUTHORITY = "content-validator:r1"
_VALIDATION_LIFECYCLE = "storage-finalized:v1"
_DEFAULT_GRANT_LIFETIME = timedelta(minutes=5)
_MAX_PREFIX_BYTES = 8


class ValidationOutcome(str, Enum):
    VALID = "valid"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class ValidatedContentEvidence(DefinitionModel):
    """Bounded post-stream evidence owned by the ingestion validator.

    The model contains classification and stable identities only.  It never
    stores filenames, excerpts, raw bytes, parser errors or provider payloads.
    ``observed_*`` values let the handoff prove that validation covered the
    exact finalized receipt without making validation part of the receipt.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: PositiveInt = 1
    producer: VersionIdentifier
    reason_schema_version: VersionIdentifier = _REASON_SCHEMA_VERSION
    content_class: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    outcome: ValidationOutcome
    reason_codes: tuple[ReasonCode, ...] = Field(
        default=("validated",), min_length=1
    )
    observed_sha256: str | None = Field(
        default=None, pattern=r"[0-9a-f]{64}"
    )
    observed_byte_length: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_observed_identity(self) -> "ValidatedContentEvidence":
        if (self.observed_sha256 is None) != (self.observed_byte_length is None):
            raise ValueError("observed content identity must be complete")
        return self


class ContentValidationEvidenceConflict(ValueError):
    """The validator did not describe the finalized bytes it received."""


class ContentValidationReader(Protocol):
    """Read finalized original bytes through a trusted validation boundary."""

    def read_finalized(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        receipt: WriteReceipt,
        max_bytes: int,
    ) -> AsyncIterable[bytes]: ...


class ContentValidator(Protocol):
    async def validate(
        self,
        content: AsyncIterable[bytes],
        *,
        declared_filename: str | None,
        declared_media_type: str | None,
    ) -> ValidatedContentEvidence: ...


class OriginalUploadService(Protocol):
    async def upload(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
        chunks: AsyncIterable[bytes],
    ) -> OriginalUploadOutcome: ...


class ContentValidationHandoff(Protocol):
    async def handoff(
        self,
        *,
        receipt: WriteReceipt,
        evidence: ValidatedContentEvidence,
    ) -> None: ...


class BoundedContentValidator:
    """Deterministic, bounded admission validation for document uploads."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_chunk_bytes: int,
        max_duration_seconds: float = 30.0,
        producer: str = _VALIDATOR_VERSION,
    ) -> None:
        if max_bytes <= 0 or max_chunk_bytes <= 0 or max_duration_seconds <= 0:
            raise ValueError("content validation limits must be positive")
        self._max_bytes = max_bytes
        self._max_chunk_bytes = max_chunk_bytes
        self._max_duration_seconds = max_duration_seconds
        self._producer = producer

    async def validate(
        self,
        content: AsyncIterable[bytes],
        *,
        declared_filename: str | None,
        declared_media_type: str | None,
    ) -> ValidatedContentEvidence:
        digest = hashlib.sha256()
        observed_length = 0
        prefix = bytearray()
        tail = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        has_nul = False
        saw_content = False
        invalid_utf8 = False
        zip_end_marker = False

        iterator = content.__aiter__()
        deadline = asyncio.get_running_loop().time() + self._max_duration_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self._evidence(
                    content_class="unknown",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="validation_timeout",
                )
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                return self._evidence(
                    content_class="unknown",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="validation_timeout",
                )
            if not isinstance(chunk, bytes):
                return self._evidence(
                    content_class="unknown",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="invalid_chunk",
                )
            if len(chunk) > self._max_chunk_bytes:
                return self._evidence(
                    content_class="unknown",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="chunk_limit_exceeded",
                )
            if observed_length + len(chunk) > self._max_bytes:
                return self._evidence(
                    content_class="unknown",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="validation_limit_exceeded",
                )
            saw_content = saw_content or bool(chunk)
            digest.update(chunk)
            observed_length += len(chunk)
            if len(prefix) < _MAX_PREFIX_BYTES:
                prefix.extend(chunk[: _MAX_PREFIX_BYTES - len(prefix)])
            tail.extend(chunk)
            del tail[:-_MAX_PREFIX_BYTES]
            has_nul = has_nul or b"\x00" in chunk
            zip_end_marker = zip_end_marker or b"PK\x05\x06" in chunk or b"PK\x06\x07" in chunk
            try:
                decoder.decode(chunk, final=False)
            except UnicodeDecodeError:
                invalid_utf8 = True

        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            invalid_utf8 = True

        identity = {
            "observed_sha256": digest.hexdigest(),
            "observed_byte_length": observed_length,
        }
        if not saw_content:
            return self._evidence(
                content_class="unknown",
                outcome=ValidationOutcome.REJECTED,
                reason="empty_content",
                **identity,
            )
        suffix = _filename_suffix(declared_filename)
        media_type = (declared_media_type or "").strip().lower()
        if prefix.startswith(b"%PDF-"):
            if not _declares(suffix, media_type, extensions=(".pdf",), media_prefix="application/pdf"):
                return self._evidence(
                    content_class="pdf",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="declared_type_mismatch",
                    **identity,
                )
            if not bytes(tail).rstrip().endswith(b"%%EOF"):
                return self._evidence(
                    content_class="pdf",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="malformed_pdf",
                    **identity,
                )
            return self._evidence(
                content_class="pdf", outcome=ValidationOutcome.VALID, **identity
            )

        if prefix.startswith(b"PK\x03\x04"):
            if not zip_end_marker:
                return self._evidence(
                    content_class="container",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="malformed_container",
                    **identity,
                )
            if suffix == ".docx" or "wordprocessingml.document" in media_type:
                content_class = "docx"
            elif suffix == ".xlsx" or "spreadsheetml.sheet" in media_type:
                content_class = "xlsx"
            else:
                return self._evidence(
                    content_class="container",
                    outcome=ValidationOutcome.QUARANTINED,
                    reason="unsupported_container",
                    **identity,
                )
            return self._evidence(
                content_class=content_class,
                outcome=ValidationOutcome.VALID,
                **identity,
            )

        if invalid_utf8 or has_nul:
            return self._evidence(
                content_class="binary",
                outcome=ValidationOutcome.REJECTED,
                reason="invalid_utf8" if invalid_utf8 else "binary_content",
                **identity,
            )

        if suffix == ".pdf" or media_type == "application/pdf":
            reason = "declared_type_mismatch"
        elif suffix in {".docx", ".xlsx"} or "openxmlformats" in media_type:
            reason = "declared_type_mismatch"
        else:
            content_class = (
                "markdown"
                if suffix == ".md" or media_type == "text/markdown"
                else "text"
            )
            return self._evidence(
                content_class=content_class,
                outcome=ValidationOutcome.VALID,
                **identity,
            )
        return self._evidence(
            content_class="unknown",
            outcome=ValidationOutcome.QUARANTINED,
            reason=reason,
            **identity,
        )

    def _evidence(
        self,
        *,
        content_class: str,
        outcome: ValidationOutcome,
        reason: str = "validated",
        observed_sha256: str | None = None,
        observed_byte_length: int | None = None,
    ) -> ValidatedContentEvidence:
        return ValidatedContentEvidence(
            producer=self._producer,
            reason_schema_version=_REASON_SCHEMA_VERSION,
            content_class=content_class,
            outcome=outcome,
            reason_codes=(reason,),
            observed_sha256=observed_sha256,
            observed_byte_length=observed_byte_length,
        )


class OpaqueGrantContentValidationReader:
    """Read a finalized receipt using an internal one-shot storage lease."""

    def __init__(
        self,
        *,
        object_store: OriginalObjectStore,
        grant_registry: AuthorizedReadGrantRegistry,
        clock: Callable[[], datetime] | None = None,
        grant_lifetime: timedelta = _DEFAULT_GRANT_LIFETIME,
    ) -> None:
        if grant_lifetime <= timedelta(0):
            raise ValueError("grant_lifetime must be positive")
        self._object_store = object_store
        self._grant_registry = grant_registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._grant_lifetime = grant_lifetime

    def read_finalized(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        receipt: WriteReceipt,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("validation clock must be timezone-aware")
        grant = self._grant_registry.issue(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
            object_reference=receipt.object_reference,
            purpose=_VALIDATION_PURPOSE,
            authorization_decision_version=_VALIDATION_AUTHORITY,
            lifecycle_version=_VALIDATION_LIFECYCLE,
            expires_at=now + self._grant_lifetime,
        )
        lease = self._grant_registry.consume(
            grant,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
            object_reference=receipt.object_reference,
            purpose=_VALIDATION_PURPOSE,
            authorization_decision_version=_VALIDATION_AUTHORITY,
            lifecycle_version=_VALIDATION_LIFECYCLE,
            now=now,
        )
        return self._object_store.open_bounded_read(lease, max_bytes=max_bytes)


@dataclass(frozen=True, slots=True)
class ContentValidationResult:
    upload: OriginalUploadOutcome
    evidence: ValidatedContentEvidence


class ContentValidationOrchestrator:
    """Finalize an upload, validate its finalized bytes, then hand off evidence."""

    def __init__(
        self,
        *,
        uploader: OriginalUploadService,
        reader: ContentValidationReader,
        validator: ContentValidator,
        handoff: ContentValidationHandoff | None = None,
    ) -> None:
        self._uploader = uploader
        self._reader = reader
        self._validator = validator
        self._handoff = handoff

    async def ingest(
        self,
        *,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
        chunks: AsyncIterable[bytes],
        source_revision_id: UUID,
        max_validation_bytes: int,
    ) -> ContentValidationResult:
        if not isinstance(source_revision_id, UUID) or source_revision_id.int == 0:
            raise ValueError("source_revision_id must be a non-zero UUID")
        if not isinstance(max_validation_bytes, int) or isinstance(max_validation_bytes, bool) or max_validation_bytes <= 0:
            raise ValueError("max_validation_bytes must be positive")

        upload = await self._uploader.upload(context, command, chunks)
        receipt = upload.write_receipt
        if receipt.object_reference.byte_length > max_validation_bytes:
            evidence = ValidatedContentEvidence(
                producer=_VALIDATOR_VERSION,
                reason_schema_version=_REASON_SCHEMA_VERSION,
                content_class="unknown",
                outcome=ValidationOutcome.QUARANTINED,
                reason_codes=("validation_limit_exceeded",),
                observed_sha256=receipt.observed_content.observed_sha256,
                observed_byte_length=receipt.observed_content.observed_byte_length,
            )
        else:
            content = self._reader.read_finalized(
                workspace_id=command.workspace_id,
                environment_id=_environment_id(command),
                source_revision_id=source_revision_id,
                receipt=receipt,
                max_bytes=max_validation_bytes,
            )
            evidence = await self._validator.validate(
                content,
                declared_filename=command.declared_filename,
                declared_media_type=command.declared_media_type,
            )

        evidence = _bind_evidence_to_receipt(evidence, receipt)
        _assert_evidence_matches_receipt(evidence, receipt)
        if self._handoff is not None:
            await self._handoff.handoff(receipt=receipt, evidence=evidence)
        return ContentValidationResult(upload=upload, evidence=evidence)


def _assert_evidence_matches_receipt(
    evidence: ValidatedContentEvidence,
    receipt: WriteReceipt,
) -> None:
    if evidence.observed_sha256 is None:
        if evidence.outcome is ValidationOutcome.VALID:
            raise ContentValidationEvidenceConflict(
                "valid content evidence must identify the finalized bytes"
            )
        return
    if (
        evidence.observed_sha256 != receipt.observed_content.observed_sha256
        or evidence.observed_byte_length != receipt.observed_content.observed_byte_length
    ):
        raise ContentValidationEvidenceConflict(
            "content validation evidence does not match the finalized receipt"
        )


def _bind_evidence_to_receipt(
    evidence: ValidatedContentEvidence,
    receipt: WriteReceipt,
) -> ValidatedContentEvidence:
    if evidence.observed_sha256 is not None:
        return evidence
    if evidence.outcome is ValidationOutcome.VALID:
        return evidence
    return evidence.model_copy(
        update={
            "observed_sha256": receipt.observed_content.observed_sha256,
            "observed_byte_length": receipt.observed_content.observed_byte_length,
        }
    )


def _environment_id(command: OriginalUploadCommandInput) -> UUID:
    if command.environment_id is None:
        raise ValueError("content validation requires an environment-scoped upload")
    return command.environment_id


@dataclass(frozen=True, slots=True)
class AdmissionContentValidationHandoff:
    """Map validator evidence to the existing Issue #7 admission service."""

    admission_service: AdmissionDecisionService
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    authority_reference: AdmissionReference
    trace_id: UUID
    audit_event_id: UUID
    decided_at: datetime

    async def handoff(
        self,
        *,
        receipt: WriteReceipt,
        evidence: ValidatedContentEvidence,
    ) -> None:
        if (
            self.revision.original_reference != receipt.object_reference
            or self.revision.original_sha256 != receipt.observed_content.observed_sha256
            or self.revision.byte_length != receipt.observed_content.observed_byte_length
        ):
            raise ContentValidationEvidenceConflict(
                "source revision evidence does not match the finalized receipt"
            )
        await self.admission_service.record(
            revision=self.revision,
            provenance=self.provenance,
            evidence=AdmissionValidationEvidence(
                disposition=_admission_disposition(evidence.outcome),
                validator_profile_version=evidence.producer,
                reason_schema_version=evidence.reason_schema_version,
                reason_codes=evidence.reason_codes,
                authority_reference=self.authority_reference,
                trace_id=self.trace_id,
                audit_event_id=self.audit_event_id,
                decided_at=self.decided_at,
            ),
        )


def _admission_disposition(outcome: ValidationOutcome) -> AdmissionDisposition:
    return {
        ValidationOutcome.VALID: AdmissionDisposition.ADMITTED,
        ValidationOutcome.QUARANTINED: AdmissionDisposition.QUARANTINED,
        ValidationOutcome.REJECTED: AdmissionDisposition.REJECTED,
    }[outcome]


def _filename_suffix(filename: str | None) -> str:
    if not filename:
        return ""
    return PurePath(filename.replace("\\", "/")).suffix.lower()


def _declares(
    suffix: str,
    media_type: str,
    *,
    extensions: tuple[str, ...],
    media_prefix: str,
) -> bool:
    return suffix in extensions or media_type == media_prefix


__all__ = [
    "BoundedContentValidator",
    "AdmissionContentValidationHandoff",
    "ContentValidationEvidenceConflict",
    "ContentValidationHandoff",
    "ContentValidationOrchestrator",
    "ContentValidationReader",
    "ContentValidationResult",
    "ContentValidator",
    "OriginalUploadService",
    "OpaqueGrantContentValidationReader",
    "ValidatedContentEvidence",
    "ValidationOutcome",
]
