from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.ingestion import (
    AdmissionContentValidationHandoff,
    BoundedContentValidator,
    ContentValidationEvidenceConflict,
    ContentValidationOrchestrator,
    OpaqueGrantContentValidationReader,
    ValidatedContentEvidence,
    ValidationOutcome,
)
from spine.application.persistence.command_digest import COMMAND_DIGEST_ALGORITHM_VERSION
from spine.application.storage import OriginalUploadOutcome
from spine.domain.sources import (
    AdmissionDisposition,
    AdmissionReference,
    SourceRevisionProvenance,
)
from spine.domain.sources.canonicalization import content_revision
from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    OriginalUploadCommandInput,
    ObservedContentIdentity,
    WriteReceipt,
)


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("30000000-0000-4000-8000-000000000001")
UPLOAD_ID = UUID("40000000-0000-4000-8000-000000000001")
OBJECT_ID = UUID("50000000-0000-4000-8000-000000000001")
GENERATION_ID = UUID("60000000-0000-4000-8000-000000000001")
RECEIPT_ID = UUID("70000000-0000-4000-8000-000000000001")


def command() -> OriginalUploadCommandInput:
    return OriginalUploadCommandInput(
        operation_name="original.upload",
        operation_schema_version=1,
        idempotency_key="validation-1",
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        declared_media_type="text/plain",
        declared_filename="notes.txt",
        purpose="document.upload.v1",
    )


def upload_outcome(content: bytes = b"hello") -> OriginalUploadOutcome:
    observed = ObservedContentIdentity(
        observed_sha256=hashlib.sha256(content).hexdigest(),
        observed_byte_length=len(content),
    )
    reference = ObjectReference(
        schema_version=1,
        object_id=OBJECT_ID,
        storage_generation=GENERATION_ID,
        digest_algorithm="sha256",
        digest_hex=observed.observed_sha256,
        byte_length=observed.observed_byte_length,
    )
    receipt = WriteReceipt(
        schema_version=1,
        upload_id=UPLOAD_ID,
        object_reference=reference,
        observed_content=observed,
        verification=IntegrityResult(
            status=IntegrityStatus.VERIFIED,
            algorithm="sha256",
            observed_digest=observed.observed_sha256,
            observed_length=observed.observed_byte_length,
        ),
        storage_status="finalized_verified",
        finalized_at=NOW,
    )
    from spine.infrastructure.object_storage.contracts import (
        Issue8ResultReference,
        OriginalUploadCommandResult,
        PreWriteRequestDigest,
    )

    request_digest = PreWriteRequestDigest(
        algorithm="sha256",
        algorithm_version=COMMAND_DIGEST_ALGORITHM_VERSION,
        digest_hex="a" * 64,
    )
    result = OriginalUploadCommandResult(
        request_digest=request_digest,
        upload_id=UPLOAD_ID,
        object_id=OBJECT_ID,
        issue8_result_reference=Issue8ResultReference(receipt_id=RECEIPT_ID),
    )
    return OriginalUploadOutcome(result=result, write_receipt=receipt, replay=False)


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _Uploader:
    def __init__(self, outcome: OriginalUploadOutcome) -> None:
        self.outcome = outcome
        self.events: list[str] = []

    async def upload(self, context: object, command: OriginalUploadCommandInput, chunks: AsyncIterable[bytes]) -> OriginalUploadOutcome:
        del context, command, chunks
        self.events.append("finalized")
        return self.outcome


class _Reader:
    def __init__(self, uploader: _Uploader, content: bytes) -> None:
        self.uploader = uploader
        self.content = content
        self.calls = 0

    def read_finalized(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        receipt: WriteReceipt,
        max_bytes: int,
    ) -> AsyncIterable[bytes]:
        assert workspace_id == WORKSPACE_ID
        assert environment_id == ENVIRONMENT_ID
        assert source_revision_id == REVISION_ID
        assert receipt.finalized_at == NOW
        assert self.uploader.events[-1] == "finalized"
        assert max_bytes >= len(self.content)
        self.calls += 1
        return stream(self.content[:3], self.content[3:])


class _Handoff:
    def __init__(self) -> None:
        self.evidence: list[ValidatedContentEvidence] = []

    async def handoff(
        self,
        *,
        receipt: WriteReceipt,
        evidence: ValidatedContentEvidence,
    ) -> None:
        assert receipt.finalized_at == NOW
        self.evidence.append(evidence)


def test_validated_content_evidence_is_versioned_and_content_free() -> None:
    evidence = ValidatedContentEvidence(
        schema_version=1,
        producer="content-validator:r1",
        content_class="text",
        outcome=ValidationOutcome.VALID,
        reason_codes=("validated",),
        observed_sha256="a" * 64,
        observed_byte_length=1,
    )

    assert evidence.schema_version == 1
    assert "bytes" not in evidence.model_dump()
    with pytest.raises(ValueError):
        ValidatedContentEvidence.model_validate(
            evidence.model_dump() | {"private_bytes": b"secret"}
        )


@pytest.mark.asyncio
async def test_bounded_validator_accepts_text_without_buffering_whole_content() -> None:
    validator = BoundedContentValidator(max_bytes=64, max_chunk_bytes=4)

    evidence = await validator.validate(
        stream(b"hel", b"lo\n"),
        declared_filename="notes.txt",
        declared_media_type="text/plain",
    )

    assert evidence.outcome is ValidationOutcome.VALID
    assert evidence.content_class == "text"
    assert evidence.observed_sha256 == hashlib.sha256(b"hello\n").hexdigest()
    assert evidence.observed_byte_length == 6


@pytest.mark.asyncio
async def test_bounded_validator_quarantines_a_stream_that_exceeds_time_limit() -> None:
    async def slow_stream() -> AsyncIterator[bytes]:
        await asyncio.sleep(0.02)
        yield b"late"

    evidence = await BoundedContentValidator(
        max_bytes=64, max_chunk_bytes=64, max_duration_seconds=0.001
    ).validate(slow_stream(), declared_filename="notes.txt", declared_media_type="text/plain")

    assert evidence.outcome is ValidationOutcome.QUARANTINED
    assert evidence.reason_codes == ("validation_timeout",)


@pytest.mark.parametrize(
    ("content", "filename", "media_type", "outcome", "reason"),
    [
        (b"PK\x03\x04archivePK\x05\x06trailer", "payload.bin", "application/octet-stream", ValidationOutcome.QUARANTINED, "unsupported_container"),
        (b"\x00\xff", "notes.txt", "text/plain", ValidationOutcome.REJECTED, "invalid_utf8"),
    ],
)
@pytest.mark.asyncio
async def test_bounded_validator_returns_typed_rejected_or_quarantined_evidence(
    content: bytes,
    filename: str,
    media_type: str,
    outcome: ValidationOutcome,
    reason: str,
) -> None:
    evidence = await BoundedContentValidator(max_bytes=64, max_chunk_bytes=64).validate(
        stream(content), declared_filename=filename, declared_media_type=media_type
    )

    assert evidence.outcome is outcome
    assert evidence.reason_codes == (reason,)


@pytest.mark.asyncio
async def test_orchestrator_finalizes_before_validation_and_hands_evidence_separately() -> None:
    outcome = upload_outcome(b"hello")
    uploader = _Uploader(outcome)
    handoff = _Handoff()
    orchestrator = ContentValidationOrchestrator(
        uploader=uploader,
        reader=_Reader(uploader, b"hello"),
        validator=BoundedContentValidator(max_bytes=64, max_chunk_bytes=64),
        handoff=handoff,
    )

    result = await orchestrator.ingest(
        context=object(),
        command=command(),
        chunks=stream(b"hello"),
        source_revision_id=REVISION_ID,
        max_validation_bytes=64,
    )

    assert result.upload is outcome
    assert result.evidence.outcome is ValidationOutcome.VALID
    assert result.evidence.observed_sha256 == outcome.write_receipt.observed_content.observed_sha256
    assert result.evidence not in result.upload.model_dump().values()
    assert handoff.evidence == [result.evidence]


@pytest.mark.asyncio
async def test_orchestrator_reruns_validation_on_finalized_replay_without_rewriting() -> None:
    outcome = upload_outcome(b"hello")
    uploader = _Uploader(outcome)
    reader = _Reader(uploader, b"hello")
    orchestrator = ContentValidationOrchestrator(
        uploader=uploader,
        reader=reader,
        validator=BoundedContentValidator(max_bytes=64, max_chunk_bytes=64),
    )

    first = await orchestrator.ingest(
        context=object(), command=command(), chunks=stream(b"hello"),
        source_revision_id=REVISION_ID, max_validation_bytes=64,
    )
    second = await orchestrator.ingest(
        context=object(), command=command(), chunks=stream(b"hello"),
        source_revision_id=REVISION_ID, max_validation_bytes=64,
    )

    assert first.evidence == second.evidence
    assert reader.calls == 2
    assert uploader.events == ["finalized", "finalized"]


@pytest.mark.asyncio
async def test_orchestrator_rejects_validation_evidence_for_different_finalized_bytes() -> None:
    outcome = upload_outcome(b"hello")
    uploader = _Uploader(outcome)
    orchestrator = ContentValidationOrchestrator(
        uploader=uploader,
        reader=_Reader(uploader, b"other"),
        validator=BoundedContentValidator(max_bytes=64, max_chunk_bytes=64),
    )

    with pytest.raises(ContentValidationEvidenceConflict):
        await orchestrator.ingest(
            context=object(), command=command(), chunks=stream(b"hello"),
            source_revision_id=REVISION_ID, max_validation_bytes=64,
        )


@pytest.mark.asyncio
async def test_orchestrator_quarantines_content_over_validation_bound_without_reading() -> None:
    outcome = upload_outcome(b"hello")
    uploader = _Uploader(outcome)
    reader = _Reader(uploader, b"hello")
    orchestrator = ContentValidationOrchestrator(
        uploader=uploader,
        reader=reader,
        validator=BoundedContentValidator(max_bytes=64, max_chunk_bytes=64),
    )

    result = await orchestrator.ingest(
        context=object(), command=command(), chunks=stream(b"hello"),
        source_revision_id=REVISION_ID, max_validation_bytes=4,
    )

    assert result.evidence.outcome is ValidationOutcome.QUARANTINED
    assert result.evidence.reason_codes == ("validation_limit_exceeded",)
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_opaque_validation_reader_uses_one_shot_internal_storage_lease() -> None:
    registry = AuthorizedReadGrantRegistry()

    class _Store:
        def open_bounded_read(self, lease: object, *, max_bytes: int) -> AsyncIterable[bytes]:
            registry.consume_read_lease(lease, now=NOW)  # type: ignore[arg-type]
            assert max_bytes == 64
            return stream(b"hello")

    reader = OpaqueGrantContentValidationReader(
        object_store=_Store(), grant_registry=registry, clock=lambda: NOW
    )
    result = reader.read_finalized(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_revision_id=REVISION_ID,
        receipt=upload_outcome().write_receipt,
        max_bytes=64,
    )

    assert b"".join([part async for part in result]) == b"hello"


@pytest.mark.asyncio
async def test_admission_handoff_maps_validation_outcome_and_binds_receipt() -> None:
    receipt = upload_outcome().write_receipt
    revision = content_revision(
        revision_id=REVISION_ID,
        source_object_id=UUID("80000000-0000-4000-8000-000000000001"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata={"embedded_title": "Notes"},
        original_reference=receipt.object_reference,
        original_sha256=receipt.observed_content.observed_sha256,
        byte_length=receipt.observed_content.observed_byte_length,
        media_type="text/plain",
        observed_at=NOW,
    )
    provenance = SourceRevisionProvenance(
        provenance_id=UUID("90000000-0000-4000-8000-000000000001"),
        source_object_id=revision.source_object_id,
        revision_id=revision.revision_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        producer_kind="upload",
        producer_reference="upload-command",
        event_identity="event-1",
        event_digest="b" * 64,
        observer_service="ingestion",
        received_at=NOW,
        observed_at=NOW,
    )

    class _Admission:
        def __init__(self) -> None:
            self.dispositions: list[AdmissionDisposition] = []

        async def record(self, *, revision: object, provenance: object, evidence: object) -> None:
            del revision, provenance
            self.dispositions.append(evidence.disposition)

    admission = _Admission()
    handoff = AdmissionContentValidationHandoff(
        admission_service=admission,  # type: ignore[arg-type]
        revision=revision,
        provenance=provenance,
        authority_reference=AdmissionReference(
            reference_type="validator",
            reference_id=UUID("a0000000-0000-4000-8000-000000000001"),
            schema_version=1,
        ),
        trace_id=UUID("b0000000-0000-4000-8000-000000000001"),
        audit_event_id=UUID("c0000000-0000-4000-8000-000000000001"),
        decided_at=NOW,
    )

    await handoff.handoff(
        receipt=receipt,
        evidence=ValidatedContentEvidence(
            producer="content-validator:r1",
            content_class="text",
            outcome=ValidationOutcome.QUARANTINED,
            reason_codes=("declared_type_mismatch",),
            observed_sha256=receipt.observed_content.observed_sha256,
            observed_byte_length=receipt.observed_content.observed_byte_length,
        ),
    )

    assert admission.dispositions == [AdmissionDisposition.QUARANTINED]
