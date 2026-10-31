"""Bounded trusted-content verification for finalized source receipts."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from multiprocessing.connection import Connection
from pathlib import PurePath
from typing import Protocol
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from pydantic import model_validator

from spine.domain.artifacts import ParseCoverage
from spine.domain.common import (
    DefinitionModel,
    VersionIdentifier,
    require_version_identifier,
)
from spine.domain.sources import (
    SourceRevision,
    SourceRevisionReceiptAssociation,
    TrustedContentFormat,
    TrustedContentVerificationEvidence,
    TrustedContentVerificationOutcome,
    VerificationCleanupOutcome,
    assert_exact_revision_receipt_binding,
)
from spine.ingestion.parsers import (
    DOCX_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    parse_document,
)
class _BoundExceeded(ValueError):
    pass


class TrustedContentVerificationResult(DefinitionModel):
    """Disclosure-safe result; evidence exists only after exact binding succeeds."""

    outcome: TrustedContentVerificationOutcome
    reason_codes: tuple[str, ...]
    evidence: TrustedContentVerificationEvidence | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "TrustedContentVerificationResult":
        if not self.reason_codes:
            raise ValueError("verification result requires a safe reason")
        if self.outcome is TrustedContentVerificationOutcome.VERIFIED and self.evidence is None:
            raise ValueError("verified result requires immutable evidence")
        if self.evidence is not None and (
            self.evidence.outcome != self.outcome
            or self.evidence.reason_codes != self.reason_codes
        ):
            raise ValueError("result and evidence disagree")
        return self


class TrustedContentVerificationReader(Protocol):
    """Internal exact-association read and verifier-workspace cleanup seam."""

    def read_exact(
        self,
        *,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
        max_bytes: int,
    ) -> AsyncIterator[bytes]: ...

    async def cleanup(
        self,
        *,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
    ) -> VerificationCleanupOutcome: ...


@dataclass(frozen=True, slots=True)
class TrustedContentVerificationProfile:
    """Offline-only qualified format and resource bounds."""

    profile_version: VersionIdentifier
    bounds_profile_version: VersionIdentifier
    qualified_formats: frozenset[TrustedContentFormat]
    max_bytes: int
    max_chunk_bytes: int
    max_duration_seconds: float
    max_archive_entries: int = 10_000
    max_uncompressed_bytes: int = 100_000_000
    max_lines: int = 1_000_000
    max_cells: int = 1_000_000
    max_worksheets: int = 256

    def __post_init__(self) -> None:
        require_version_identifier(
            self.profile_version, field_name="profile_version"
        )
        require_version_identifier(
            self.bounds_profile_version, field_name="bounds_profile_version"
        )
        if not self.qualified_formats or any(
            value in {TrustedContentFormat.UNKNOWN, TrustedContentFormat.AMBIGUOUS}
            for value in self.qualified_formats
        ):
            raise ValueError("qualified_formats must contain only explicit formats")
        bounds = (
            self.max_bytes,
            self.max_chunk_bytes,
            self.max_duration_seconds,
            self.max_archive_entries,
            self.max_uncompressed_bytes,
            self.max_lines,
            self.max_cells,
            self.max_worksheets,
        )
        if min(bounds) <= 0:
            raise ValueError("trusted content verification bounds must be positive")


class BoundedTrustedContentVerifier:
    """Deep verifier module: exact binding, bounded read, recognition and parsing."""

    def __init__(
        self,
        *,
        reader: TrustedContentVerificationReader,
        profile: TrustedContentVerificationProfile,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._reader = reader
        self._profile = profile
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory

    async def verify(
        self,
        *,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
        declared_filename: str | None,
        declared_media_type: str | None,
    ) -> TrustedContentVerificationResult:
        if not _binding_matches(revision, association):
            return TrustedContentVerificationResult(
                outcome=TrustedContentVerificationOutcome.DENIED,
                reason_codes=("exact_receipt_mismatch",),
            )
        if association.byte_length > self._profile.max_bytes:
            return self._result(
                revision,
                association,
                recognized_format=TrustedContentFormat.UNKNOWN,
                format_profile="unrecognized:v1",
                outcome=TrustedContentVerificationOutcome.QUARANTINED,
                reason="bounded_input",
                cleanup=VerificationCleanupOutcome.NOT_REQUIRED,
            )

        content, read_reason, cleanup = await self._read_and_cleanup(revision, association)
        if cleanup is VerificationCleanupOutcome.UNCERTAIN:
            return self._result(
                revision,
                association,
                recognized_format=TrustedContentFormat.UNKNOWN,
                format_profile="unrecognized:v1",
                outcome=TrustedContentVerificationOutcome.QUARANTINED,
                reason="cleanup_uncertain",
                cleanup=cleanup,
            )
        if read_reason is not None or content is None:
            return self._result(
                revision,
                association,
                recognized_format=TrustedContentFormat.UNKNOWN,
                format_profile="unrecognized:v1",
                outcome=TrustedContentVerificationOutcome.QUARANTINED,
                reason=read_reason or "verifier_unavailable",
                cleanup=cleanup,
            )
        if len(content) != association.byte_length:
            return self._result(
                revision,
                association,
                recognized_format=TrustedContentFormat.UNKNOWN,
                format_profile="unrecognized:v1",
                outcome=TrustedContentVerificationOutcome.DENIED,
                reason="length_mismatch",
                cleanup=cleanup,
            )
        if hashlib.sha256(content).hexdigest() != association.original_sha256:
            return self._result(
                revision,
                association,
                recognized_format=TrustedContentFormat.UNKNOWN,
                format_profile="unrecognized:v1",
                outcome=TrustedContentVerificationOutcome.DENIED,
                reason="digest_mismatch",
                cleanup=cleanup,
            )

        recognized, detection_reason = _detect_format(content, self._profile)
        if detection_reason is not None:
            return self._result(
                revision,
                association,
                recognized_format=recognized,
                format_profile="unrecognized:v1",
                outcome=TrustedContentVerificationOutcome.QUARANTINED,
                reason=detection_reason,
                cleanup=cleanup,
            )
        if recognized not in self._profile.qualified_formats:
            return self._result(
                revision,
                association,
                recognized_format=recognized,
                format_profile="unqualified:v1",
                outcome=TrustedContentVerificationOutcome.DENIED,
                reason="unsupported_format",
                cleanup=cleanup,
            )
        if _declared_mismatch(recognized, declared_filename, declared_media_type):
            return self._result(
                revision,
                association,
                recognized_format=recognized,
                format_profile="declared-mismatch:v1",
                outcome=TrustedContentVerificationOutcome.QUARANTINED,
                reason="declared_type_mismatch",
                cleanup=cleanup,
            )

        format_profile, structural_reason = await self._structural_verify(
            revision, content, recognized
        )
        if structural_reason is not None:
            return self._result(
                revision,
                association,
                recognized_format=recognized,
                format_profile=format_profile,
                outcome=TrustedContentVerificationOutcome.QUARANTINED,
                reason=structural_reason,
                cleanup=cleanup,
            )
        return self._result(
            revision,
            association,
            recognized_format=recognized,
            format_profile=format_profile,
            outcome=TrustedContentVerificationOutcome.VERIFIED,
            reason="verified",
            cleanup=cleanup,
        )

    async def _read_and_cleanup(
        self,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
    ) -> tuple[bytes | None, str | None, VerificationCleanupOutcome]:
        content: bytes | None = None
        reason: str | None = None
        try:
            content = await asyncio.wait_for(
                self._collect(
                    self._reader.read_exact(
                        revision=revision,
                        association=association,
                        max_bytes=self._profile.max_bytes,
                    )
                ),
                timeout=self._profile.max_duration_seconds,
            )
        except TimeoutError:
            reason = "verification_timeout"
        except _BoundExceeded:
            reason = "bounded_input"
        except Exception:
            reason = "verifier_unavailable"
        try:
            cleanup = await asyncio.wait_for(
                self._reader.cleanup(revision=revision, association=association),
                timeout=self._profile.max_duration_seconds,
            )
        except Exception:
            cleanup = VerificationCleanupOutcome.UNCERTAIN
        return content, reason, cleanup

    async def _collect(self, stream: AsyncIterator[bytes]) -> bytes:
        result = bytearray()
        async for chunk in stream:
            if not isinstance(chunk, bytes):
                raise TypeError("invalid verifier chunk")
            if len(chunk) > self._profile.max_chunk_bytes:
                raise _BoundExceeded("chunk bound exceeded")
            if len(result) + len(chunk) > self._profile.max_bytes:
                raise _BoundExceeded("content bound exceeded")
            result.extend(chunk)
        return bytes(result)

    async def _structural_verify(
        self,
        revision: SourceRevision,
        content: bytes,
        recognized: TrustedContentFormat,
    ) -> tuple[str, str | None]:
        media_type = {
            TrustedContentFormat.TEXT: "text/plain",
            TrustedContentFormat.MARKDOWN: "text/markdown",
            TrustedContentFormat.PDF: "application/pdf",
            TrustedContentFormat.DOCX: DOCX_MEDIA_TYPE,
            TrustedContentFormat.XLSX: XLSX_MEDIA_TYPE,
        }[recognized]
        status, profile, coverage, exclusion_codes = await _parse_in_bounded_process(
            revision=revision,
            content=content,
            media_type=media_type,
            profile=self._profile,
        )
        if status == "timeout":
            return "parser-timeout:v1", "verification_timeout"
        if status != "ok" or profile is None or coverage is None:
            return "parser-safe-error:v1", "malformed_document"
        if coverage == ParseCoverage.FAILED.value:
            return profile, "malformed_document"
        if coverage == ParseCoverage.PARTIAL.value or exclusion_codes:
            return (
                profile,
                "bounded_input"
                if "bounded_input" in exclusion_codes
                else "structural_reject",
            )
        return profile, None

    def _result(
        self,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
        *,
        recognized_format: TrustedContentFormat,
        format_profile: str,
        outcome: TrustedContentVerificationOutcome,
        reason: str,
        cleanup: VerificationCleanupOutcome,
    ) -> TrustedContentVerificationResult:
        evidence = TrustedContentVerificationEvidence.issue(
            evidence_id=self._id_factory(),
            revision=revision,
            association=association,
            recognized_format=recognized_format,
            format_profile_version=format_profile,
            verifier_profile_version=self._profile.profile_version,
            bounds_profile_version=self._profile.bounds_profile_version,
            outcome=outcome,
            reason_codes=(reason,),
            cleanup_outcome=cleanup,
            verified_at=self._now(),
        )
        return TrustedContentVerificationResult(
            outcome=outcome, reason_codes=(reason,), evidence=evidence
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("verification clock must be timezone-aware")
        return value


def _binding_matches(
    revision: SourceRevision, association: SourceRevisionReceiptAssociation
) -> bool:
    try:
        assert_exact_revision_receipt_binding(
            revision=revision, association=association
        )
    except ValueError:
        return False
    return True


def _detect_format(
    content: bytes, profile: TrustedContentVerificationProfile
) -> tuple[TrustedContentFormat, str | None]:
    if not content:
        return TrustedContentFormat.UNKNOWN, "empty_content"
    is_pdf = content.startswith(b"%PDF-")
    has_zip = b"PK\x03\x04" in content
    if is_pdf and has_zip:
        return TrustedContentFormat.AMBIGUOUS, "ambiguous_format"
    if is_pdf:
        return TrustedContentFormat.PDF, None
    if has_zip:
        try:
            with ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > profile.max_archive_entries
                    or sum(entry.file_size for entry in entries)
                    > profile.max_uncompressed_bytes
                ):
                    return TrustedContentFormat.UNKNOWN, "bounded_expansion"
                names = {entry.filename for entry in entries}
        except (BadZipFile, OSError, ValueError):
            return TrustedContentFormat.UNKNOWN, "malformed_document"
        is_docx = "[Content_Types].xml" in names and "word/document.xml" in names
        is_xlsx = "[Content_Types].xml" in names and "xl/workbook.xml" in names
        if is_docx and is_xlsx:
            return TrustedContentFormat.AMBIGUOUS, "ambiguous_format"
        if any(name.lower().endswith("vbaproject.bin") for name in names):
            return TrustedContentFormat.UNKNOWN, "unsupported_format"
        if is_docx:
            return TrustedContentFormat.DOCX, None
        if is_xlsx:
            return TrustedContentFormat.XLSX, None
        return TrustedContentFormat.UNKNOWN, "unsupported_format"
    if b"\x00" in content:
        return TrustedContentFormat.UNKNOWN, "unsupported_format"
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return TrustedContentFormat.UNKNOWN, "unsupported_format"
    return (
        TrustedContentFormat.MARKDOWN
        if _looks_like_markdown(text)
        else TrustedContentFormat.TEXT,
        None,
    )


def _looks_like_markdown(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.lstrip()
        if (
            stripped.startswith(("```", "~~~", "> ", "- ", "* ", "+ "))
            or (stripped.startswith("#") and stripped.lstrip("#").startswith(" "))
            or (
                len(stripped) > 3
                and stripped[0].isdigit()
                and ". " in stripped[:5]
            )
        ):
            return True
    return False


def _declared_mismatch(
    recognized: TrustedContentFormat,
    filename: str | None,
    media_type: str | None,
) -> bool:
    suffix = PurePath((filename or "").replace("\\", "/")).suffix.lower()
    normalized_media = (media_type or "").strip().lower()
    suffix_format = {
        ".txt": TrustedContentFormat.TEXT,
        ".md": TrustedContentFormat.MARKDOWN,
        ".markdown": TrustedContentFormat.MARKDOWN,
        ".pdf": TrustedContentFormat.PDF,
        ".docx": TrustedContentFormat.DOCX,
        ".xlsx": TrustedContentFormat.XLSX,
    }.get(suffix)
    media_format = {
        "text/plain": TrustedContentFormat.TEXT,
        "text/markdown": TrustedContentFormat.MARKDOWN,
        "text/x-markdown": TrustedContentFormat.MARKDOWN,
        "application/pdf": TrustedContentFormat.PDF,
        DOCX_MEDIA_TYPE: TrustedContentFormat.DOCX,
        XLSX_MEDIA_TYPE: TrustedContentFormat.XLSX,
    }.get(normalized_media)
    declared = {value for value in (suffix_format, media_format) if value is not None}
    if len(declared) > 1:
        return True
    if recognized is TrustedContentFormat.TEXT and declared == {TrustedContentFormat.MARKDOWN}:
        return False
    return bool(declared and recognized not in declared)


def _parser_process_entry(
    sender: Connection,
    revision: SourceRevision,
    content: bytes,
    media_type: str,
    max_bytes: int,
    max_lines: int,
    max_cells: int,
    max_worksheets: int,
) -> None:
    """Parse in a killable process; never send document content to the parent."""

    try:
        document = parse_document(
            revision,
            content,
            media_type=media_type,
            max_bytes=max_bytes,
            max_lines=max_lines,
            max_cells=max_cells,
            max_worksheets=max_worksheets,
        )
        sender.send(
            (
                "ok",
                document.parser_version,
                document.coverage.value,
                tuple(item.code for item in document.exclusions),
            )
        )
    except BaseException:
        sender.send(("error", None, None, ()))
    finally:
        sender.close()


async def _parse_in_bounded_process(
    *,
    revision: SourceRevision,
    content: bytes,
    media_type: str,
    profile: TrustedContentVerificationProfile,
) -> tuple[str, str | None, str | None, tuple[str, ...]]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_parser_process_entry,
        args=(
            sender,
            revision,
            content,
            media_type,
            profile.max_bytes,
            profile.max_lines,
            profile.max_cells,
            profile.max_worksheets,
        ),
        daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        sender.close()
        await asyncio.to_thread(process.join, profile.max_duration_seconds)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 1.0)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join)
            return "timeout", None, None, ()
        if receiver.poll():
            result = receiver.recv()
            if isinstance(result, tuple) and len(result) == 4:
                return result
        return "error", None, None, ()
    except Exception:
        if started and process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 1.0)
        return "error", None, None, ()
    finally:
        receiver.close()
        sender.close()
        if started:
            process.close()


__all__ = [
    "BoundedTrustedContentVerifier",
    "TrustedContentVerificationProfile",
    "TrustedContentVerificationReader",
    "TrustedContentVerificationResult",
]
