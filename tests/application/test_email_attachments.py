from __future__ import annotations

from collections.abc import AsyncIterator
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from spine.application.email_attachments import (
    AcceptedEmailAttachmentParent,
    EmailAttachmentExtractionProfile,
    EmailAttachmentProcessor,
    EmailAttachmentUnavailable,
)
from spine.application.processing_disposition import (
    OrdinaryProcessingEligibility,
    OrdinaryProcessingEligibilityReason,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentWriteReceipt,
    email_attachment_id,
)
from spine.ingestion.email_mime import MimeParseResult
from spine.infrastructure.object_storage.contracts import (
    IntegrityResult,
    IntegrityStatus,
    ObservedContentIdentity,
    WriteReceipt,
)
from spine.infrastructure.persistence.email_attachments import InMemoryEmailAttachmentRepository
from spine.ingestion.email_mime import MimeParser, MimeParserProfile


ROOT = Path(__file__).parents[1]
RAW = (ROOT / "fixtures/email_mime/golden_mixed.eml").read_bytes()
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
SOURCE = UUID("30000000-0000-4000-8000-000000000001")
REVISION = UUID("40000000-0000-4000-8000-000000000001")
ACCEPTANCE = UUID("50000000-0000-4000-8000-000000000001")


class FakeEligibility:
    async def resolve(self, **_values):
        return OrdinaryProcessingEligibility(
            eligible=True,
            reason=OrdinaryProcessingEligibilityReason.ELIGIBLE,
            canonical_acceptance_id=ACCEPTANCE,
        )


class FakeParentReader:
    def __init__(self, *, workspace_id=WORKSPACE, source_revision_id=REVISION, raw=RAW):
        self.workspace_id = workspace_id
        self.source_revision_id = source_revision_id
        self.raw = raw

    async def resolve_accepted_email_parent(self, **_values):
        return AcceptedEmailAttachmentParent(
            workspace_id=self.workspace_id,
            environment_id=ENVIRONMENT,
            source_object_id=SOURCE,
            source_revision_id=self.source_revision_id,
            source_revision_digest="a" * 64,
            original_sha256=hashlib.sha256(self.raw).hexdigest(),
            raw_byte_length=len(self.raw),
        )


class FakeGateway:
    def __init__(self, raw: bytes = RAW):
        self.raw = raw
        self.calls = []

    def read_original(self, **kwargs) -> AsyncIterator[bytes]:
        self.calls.append(kwargs)

        async def stream():
            yield self.raw

        return stream()


class FakeOriginalWriter:
    def __init__(self):
        self.calls = []
        self.receipts = []

    async def store(self, **kwargs) -> EmailAttachmentWriteReceipt:
        self.calls.append(kwargs)
        content = kwargs["content"]
        digest = hashlib.sha256(content).hexdigest()
        upload_id = uuid5(NAMESPACE_URL, f"upload:{kwargs['source_revision_id']}:{kwargs['part_path']}")
        reference = ObjectReference(
            schema_version=1,
            object_id=uuid5(NAMESPACE_URL, f"object:{kwargs['source_revision_id']}:{kwargs['part_path']}"),
            storage_generation=uuid5(NAMESPACE_URL, f"generation:{kwargs['source_revision_id']}:{kwargs['part_path']}"),
            digest_algorithm="sha256",
            digest_hex=digest,
            byte_length=len(content),
        )
        receipt = WriteReceipt(
            schema_version=1,
            upload_id=upload_id,
            object_reference=reference,
            observed_content=ObservedContentIdentity(
                observed_sha256=digest, observed_byte_length=len(content)
            ),
            verification=IntegrityResult(
                status=IntegrityStatus.VERIFIED, algorithm="sha256", expected_digest=digest,
                observed_digest=digest, expected_length=len(content),
                observed_length=len(content),
            ),
            storage_status="finalized_verified",
            finalized_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        domain_receipt = EmailAttachmentWriteReceipt(
            upload_id=receipt.upload_id,
            object_reference=receipt.object_reference,
            observed_sha256=receipt.observed_content.observed_sha256,
            observed_byte_length=receipt.observed_content.observed_byte_length,
            verification_status=receipt.verification.status.value,
            storage_status=receipt.storage_status,
            finalized_at=receipt.finalized_at,
        )
        self.receipts.append(domain_receipt)
        return domain_receipt


def _parser():
    return MimeParser(profile=MimeParserProfile(
        max_total_bytes=20_000,
        max_header_bytes=10_000,
        max_parts=30,
        max_depth=10,
        max_decoded_bytes=10_000,
        max_parse_seconds=2.0,
    ))


def _processor(*, parent=None, gateway=None, repository=None, writer=None, parser=None, max_bytes=10_000):
    profile = EmailAttachmentExtractionProfile(
        max_attachment_bytes=max_bytes,
        max_attachment_parts=10,
    )
    return EmailAttachmentProcessor(
        gateway=gateway or FakeGateway(),
        eligibility=FakeEligibility(),
        parents=parent or FakeParentReader(),
        repository=repository or InMemoryEmailAttachmentRepository(),
        parser=parser or _parser(),
        profile=profile,
        originals=writer or FakeOriginalWriter(),
    )


async def _process(processor, *, source_revision_id=REVISION, expected_part_paths=None):
    return await processor.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=source_revision_id,
        max_bytes=20_000,
        expected_part_paths=expected_part_paths,
    )


@pytest.mark.asyncio
async def test_authorized_attachment_is_stored_with_exact_parent_part_lineage() -> None:
    repository = InMemoryEmailAttachmentRepository()
    writer = FakeOriginalWriter()
    processor = _processor(repository=repository, writer=writer)

    result = await _process(processor)

    assert len(result) == 1
    artifact = result[0].artifact
    assert artifact.source_revision_id == REVISION
    assert artifact.source_revision_digest == "a" * 64
    assert artifact.mime_part_path == "1.2"
    assert artifact.content_sha256 == "b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2"
    assert artifact.media_type == "application/octet-stream"
    assert artifact.filename == "synthetic.txt"
    assert artifact.disposition == "attachment"
    assert artifact.write_receipt.object_reference.digest_hex == artifact.content_sha256
    assert artifact.write_receipt.observed_byte_length == 9
    assert await repository.get(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, artifact_id=artifact.artifact_id
    ) == artifact


@pytest.mark.asyncio
async def test_attachment_replay_has_one_logical_child_effect() -> None:
    repository = InMemoryEmailAttachmentRepository()
    writer = FakeOriginalWriter()
    processor = _processor(repository=repository, writer=writer)

    first = await _process(processor)
    replay = await _process(processor)

    assert first[0].replay is False
    assert replay[0].replay is True
    assert replay[0].artifact == first[0].artifact
    assert len(await repository.list_for_revision(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_revision_id=REVISION
    )) == 1


@pytest.mark.asyncio
async def test_equal_attachment_bytes_in_another_parent_keep_distinct_identity() -> None:
    repository = InMemoryEmailAttachmentRepository()
    first = _processor(repository=repository)
    second_revision = UUID("40000000-0000-4000-8000-000000000002")
    second = _processor(
        repository=repository,
        parent=FakeParentReader(source_revision_id=second_revision),
    )

    first_result = await _process(first)
    second_result = await _process(second, source_revision_id=second_revision)

    assert first_result[0].artifact.content_sha256 == second_result[0].artifact.content_sha256
    assert first_result[0].artifact.artifact_id != second_result[0].artifact.artifact_id
    assert first_result[0].artifact.source_revision_id != second_result[0].artifact.source_revision_id


@pytest.mark.asyncio
async def test_inline_and_ordinary_parts_are_persisted_as_separate_children() -> None:
    raw = (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--b\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\n"
        b"Content-ID: <inline-1>\r\nContent-Transfer-Encoding: base64\r\n\r\ncG5n\r\n"
        b"--b\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=ordinary.bin\r\n\r\nfile-bytes\r\n--b--\r\n"
    )
    repository = InMemoryEmailAttachmentRepository()
    processor = _processor(
        repository=repository,
        gateway=FakeGateway(raw),
        parent=FakeParentReader(raw=raw),
    )

    result = await _process(processor)

    assert [(item.artifact.mime_part_path, item.artifact.disposition) for item in result] == [
        ("1.2", "inline"), ("1.3", "attachment")
    ]
    assert result[0].artifact.content_id == "<inline-1>"
    assert result[1].artifact.filename == "ordinary.bin"
    assert result[0].artifact.artifact_id != result[1].artifact.artifact_id


@pytest.mark.asyncio
async def test_duplicate_extracted_part_path_fails_before_any_original_write() -> None:
    parsed = _parser().parse_with_attachments(RAW)
    duplicate = parsed.attachments[0]

    class DuplicateParser:
        def parse_with_attachments(self, _raw):
            return MimeParseResult(parsed.artifact, (duplicate, duplicate))

    writer = FakeOriginalWriter()
    processor = _processor(parser=DuplicateParser(), writer=writer)

    with pytest.raises(EmailAttachmentUnavailable):
        await _process(processor)

    assert writer.calls == []


@pytest.mark.asyncio
async def test_missing_requested_mime_part_fails_without_child_or_original_write() -> None:
    repository = InMemoryEmailAttachmentRepository()
    writer = FakeOriginalWriter()
    processor = _processor(repository=repository, writer=writer)

    with pytest.raises(EmailAttachmentUnavailable):
        await _process(processor, expected_part_paths=("1.9",))

    assert writer.calls == []
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_revision_id=REVISION
    ) == ()


@pytest.mark.asyncio
async def test_database_failure_leaves_original_at_governed_orphan_cleanup_boundary() -> None:
    class FailingRepository(InMemoryEmailAttachmentRepository):
        async def persist_many(self, _artifacts):
            raise RuntimeError("synthetic persistence failure")

    writer = FakeOriginalWriter()
    repository = FailingRepository()
    processor = _processor(repository=repository, writer=writer)

    with pytest.raises(EmailAttachmentUnavailable):
        await _process(processor)

    assert len(writer.calls) == 1
    assert len(writer.receipts) == 1
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_revision_id=REVISION
    ) == ()
    assert await repository.get(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        artifact_id=email_attachment_id(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_revision_id=REVISION,
            mime_part_path=writer.calls[0]["part_path"],
            extraction_profile=EmailAttachmentExtractionProfile(
                max_attachment_bytes=10_000,
                max_attachment_parts=10,
            ).reference,
        ),
    ) is None


@pytest.mark.asyncio
async def test_oversized_attachment_fails_without_a_persisted_or_retrievable_child() -> None:
    repository = InMemoryEmailAttachmentRepository()
    writer = FakeOriginalWriter()
    processor = _processor(repository=repository, writer=writer, max_bytes=8)

    with pytest.raises(EmailAttachmentUnavailable) as error:
        await _process(processor)

    assert str(error.value) == "email attachment is unavailable"
    assert "synthetic" not in str(error.value)
    assert writer.calls == []
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_revision_id=REVISION
    ) == ()


@pytest.mark.asyncio
async def test_wrong_tenant_parent_is_rejected_before_read_or_child_creation() -> None:
    gateway = FakeGateway()
    repository = InMemoryEmailAttachmentRepository()
    processor = _processor(
        parent=FakeParentReader(workspace_id=UUID("10000000-0000-4000-8000-000000000099")),
        gateway=gateway,
        repository=repository,
    )

    with pytest.raises(EmailAttachmentUnavailable):
        await _process(processor)

    assert gateway.calls == []
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_revision_id=REVISION
    ) == ()


@pytest.mark.asyncio
async def test_truncated_mime_fails_without_child_or_filename_leakage() -> None:
    truncated = RAW[:-40]
    repository = InMemoryEmailAttachmentRepository()
    processor = _processor(repository=repository, gateway=FakeGateway(truncated))
    processor = _processor(
        repository=repository,
        gateway=FakeGateway(truncated),
        parent=FakeParentReader(raw=truncated),
    )

    with pytest.raises(EmailAttachmentUnavailable) as error:
        await _process(processor)

    assert str(error.value) == "email attachment is unavailable"
    assert "synthetic.txt" not in str(error.value)
    assert await repository.list_for_revision(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_revision_id=REVISION
    ) == ()
