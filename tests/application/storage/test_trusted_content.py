from collections.abc import AsyncIterator
from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID
from zipfile import ZipFile

import pytest

from spine.application.storage.trusted_content import (
    BoundedTrustedContentVerifier,
    TrustedContentVerificationProfile,
)
from spine.domain.sources import (
    SourceRevisionReceiptAssociation,
    TrustedContentFormat,
    TrustedContentVerificationOutcome,
    VerificationCleanupOutcome,
    content_revision,
)
from spine.domain.sources.original_reference import ObjectReference


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")


def _contracts(content: bytes, *, media_type: str = "text/plain"):
    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    source_id = UUID("30000000-0000-4000-8000-000000000001")
    revision_id = UUID("40000000-0000-4000-8000-000000000001")
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID("50000000-0000-4000-8000-000000000001"),
        storage_generation=UUID("60000000-0000-4000-8000-000000000001"),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=len(content),
    )
    revision = content_revision(
        revision_id=revision_id,
        source_object_id=source_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata={"embedded_title": "Synthetic"},
        original_reference=reference,
        original_sha256=digest,
        byte_length=len(content),
        media_type=media_type,
        observed_at=NOW,
    )
    association = SourceRevisionReceiptAssociation(
        association_id=UUID("70000000-0000-4000-8000-000000000001"),
        source_object_id=source_id,
        revision_id=revision_id,
        provenance_id=UUID("80000000-0000-4000-8000-000000000001"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=UUID("90000000-0000-4000-8000-000000000001"),
        original_command_result_id=UUID("a0000000-0000-4000-8000-000000000001"),
        original_command_result_schema_version=1,
        original_upload_id=UUID("b0000000-0000-4000-8000-000000000001"),
        original_reference=reference,
        original_sha256=digest,
        byte_length=len(content),
        finalized_at=NOW,
        associated_at=NOW,
    )
    return revision, association


class _Reader:
    def __init__(
        self,
        content: bytes,
        *,
        cleanup: VerificationCleanupOutcome = VerificationCleanupOutcome.COMPLETED,
    ) -> None:
        self.content = content
        self.cleanup_outcome = cleanup
        self.opened = False

    def read_exact(self, **kwargs: object) -> AsyncIterator[bytes]:
        self.opened = True

        async def stream() -> AsyncIterator[bytes]:
            yield self.content

        return stream()

    async def cleanup(self, **kwargs: object) -> VerificationCleanupOutcome:
        return self.cleanup_outcome


def _verifier(
    reader: _Reader,
    *,
    max_chunk_bytes: int = 1024 * 1024,
    max_duration_seconds: float = 1,
    max_archive_entries: int = 10_000,
) -> BoundedTrustedContentVerifier:
    return BoundedTrustedContentVerifier(
        reader=reader,
        profile=TrustedContentVerificationProfile(
            profile_version="trusted-content-offline:test-v1",
            bounds_profile_version="trusted-content-bounds:test-v1",
            qualified_formats=frozenset(
                {
                    TrustedContentFormat.TEXT,
                    TrustedContentFormat.MARKDOWN,
                    TrustedContentFormat.PDF,
                    TrustedContentFormat.DOCX,
                    TrustedContentFormat.XLSX,
                }
            ),
            max_bytes=1024 * 1024,
            max_chunk_bytes=max_chunk_bytes,
            max_duration_seconds=max_duration_seconds,
            max_archive_entries=max_archive_entries,
        ),
        clock=lambda: NOW,
        id_factory=lambda: UUID("c0000000-0000-4000-8000-000000000001"),
    )


async def test_verifier_accepts_qualified_utf8_content_and_binds_receipt() -> None:
    content = b"hello\n"
    revision, association = _contracts(content)

    result = await _verifier(_Reader(content)).verify(
        revision=revision,
        association=association,
        declared_filename="notes.txt",
        declared_media_type="text/plain",
    )

    assert result.outcome is TrustedContentVerificationOutcome.VERIFIED
    assert result.evidence is not None
    assert result.evidence.recognized_format is TrustedContentFormat.TEXT
    result.evidence.assert_matches(revision=revision, association=association)


async def test_verifier_recognizes_markdown_from_content_and_uses_markdown_parser() -> None:
    content = b"# Heading\nBody\n"
    revision, association = _contracts(content, media_type="text/markdown")

    result = await _verifier(_Reader(content)).verify(
        revision=revision,
        association=association,
        declared_filename="notes.md",
        declared_media_type="text/markdown",
    )

    assert result.outcome is TrustedContentVerificationOutcome.VERIFIED
    assert result.evidence is not None
    assert result.evidence.recognized_format is TrustedContentFormat.MARKDOWN


async def test_verifier_rejects_unsupported_markdown_structure() -> None:
    content = b"```python\nprint('unsafe')\n```\n"
    revision, association = _contracts(content, media_type="text/markdown")

    result = await _verifier(_Reader(content)).verify(
        revision=revision,
        association=association,
        declared_filename="notes.md",
        declared_media_type="text/markdown",
    )

    assert result.outcome is TrustedContentVerificationOutcome.QUARANTINED
    assert result.reason_codes == ("structural_reject",)


async def test_verifier_denies_substituted_association_before_reading() -> None:
    content = b"hello\n"
    revision, association = _contracts(content)
    reader = _Reader(content)
    substituted = association.model_copy(
        update={"revision_id": UUID("d0000000-0000-4000-8000-000000000001")}
    )

    result = await _verifier(reader).verify(
        revision=revision,
        association=substituted,
        declared_filename="notes.txt",
        declared_media_type="text/plain",
    )

    assert result.outcome is TrustedContentVerificationOutcome.DENIED
    assert result.reason_codes == ("exact_receipt_mismatch",)
    assert result.evidence is None
    assert reader.opened is False


@pytest.mark.parametrize(
    ("content", "filename", "media_type", "reason"),
    [
        (b"plain text", "spoofed.pdf", "application/pdf", "declared_type_mismatch"),
        (b"%PDF-1.7\ntruncated", "file.pdf", "application/pdf", "malformed_document"),
        (b"\x00\xff", "file.txt", "text/plain", "unsupported_format"),
    ],
)
async def test_verifier_returns_safe_typed_failure_for_untrusted_content(
    content: bytes, filename: str, media_type: str, reason: str
) -> None:
    revision, association = _contracts(content, media_type=media_type)

    result = await _verifier(_Reader(content)).verify(
        revision=revision,
        association=association,
        declared_filename=filename,
        declared_media_type=media_type,
    )

    assert result.outcome in {
        TrustedContentVerificationOutcome.DENIED,
        TrustedContentVerificationOutcome.QUARANTINED,
    }
    assert result.reason_codes == (reason,)
    assert result.evidence is not None
    assert "plain text" not in result.evidence.model_dump_json()


async def test_verifier_never_reports_success_when_cleanup_is_uncertain() -> None:
    content = b"hello\n"
    revision, association = _contracts(content)

    result = await _verifier(
        _Reader(content, cleanup=VerificationCleanupOutcome.UNCERTAIN)
    ).verify(
        revision=revision,
        association=association,
        declared_filename="notes.txt",
        declared_media_type="text/plain",
    )

    assert result.outcome is TrustedContentVerificationOutcome.QUARANTINED
    assert result.reason_codes == ("cleanup_uncertain",)
    assert result.evidence is not None
    assert result.evidence.cleanup_outcome is VerificationCleanupOutcome.UNCERTAIN


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"%PDF-1.7\nPK\x03\x04", "ambiguous_format"),
        (b"PK\x03\x04not-a-zip", "malformed_document"),
    ],
)
async def test_verifier_quarantines_ambiguous_or_truncated_container(
    content: bytes, reason: str
) -> None:
    revision, association = _contracts(content)

    result = await _verifier(_Reader(content)).verify(
        revision=revision,
        association=association,
        declared_filename=None,
        declared_media_type=None,
    )

    assert result.outcome is TrustedContentVerificationOutcome.QUARANTINED
    assert result.reason_codes == (reason,)


async def test_verifier_enforces_chunk_and_parser_time_bounds() -> None:
    content = b"hello\n"
    revision, association = _contracts(content)

    chunk_result = await _verifier(_Reader(content), max_chunk_bytes=2).verify(
        revision=revision,
        association=association,
        declared_filename="notes.txt",
        declared_media_type="text/plain",
    )
    timeout_result = await _verifier(
        _Reader(content), max_duration_seconds=0.001
    ).verify(
        revision=revision,
        association=association,
        declared_filename="notes.txt",
        declared_media_type="text/plain",
    )

    assert chunk_result.reason_codes == ("bounded_input",)
    assert timeout_result.reason_codes == ("verification_timeout",)


async def test_verifier_rejects_archive_expansion_before_parsing() -> None:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "types")
        archive.writestr("word/document.xml", "document")
    content = buffer.getvalue()
    revision, association = _contracts(content)

    result = await _verifier(_Reader(content), max_archive_entries=1).verify(
        revision=revision,
        association=association,
        declared_filename="document.docx",
        declared_media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )

    assert result.outcome is TrustedContentVerificationOutcome.QUARANTINED
    assert result.reason_codes == ("bounded_expansion",)


@pytest.mark.parametrize(
    ("observed", "reason"),
    [(b"jello\n", "digest_mismatch"), (b"short", "length_mismatch")],
)
async def test_verifier_denies_bytes_that_do_not_match_finalized_receipt(
    observed: bytes, reason: str
) -> None:
    revision, association = _contracts(b"hello\n")

    result = await _verifier(_Reader(observed)).verify(
        revision=revision,
        association=association,
        declared_filename="notes.txt",
        declared_media_type="text/plain",
    )

    assert result.outcome is TrustedContentVerificationOutcome.DENIED
    assert result.reason_codes == (reason,)
    assert result.evidence is not None
