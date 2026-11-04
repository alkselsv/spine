from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.email_attachment_ingestion import (
    EmailAttachmentIngestionCoordinator,
    EmailAttachmentIngestionUnavailable,
    EmailAttachmentOriginalMissing,
    EmailAttachmentProcessingDecision,
)
from spine.application.email_attachments import EmailAttachmentPersistenceResult
from spine.application.processing_disposition import (
    OrdinaryProcessingEligibility,
    OrdinaryProcessingEligibilityReason,
)
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentArtifact,
    EmailAttachmentProcessingDisposition,
    EmailAttachmentProcessingReceipt,
    EmailAttachmentWriteReceipt,
    email_attachment_artifact_digest,
    email_attachment_id,
    email_attachment_processing_receipt_digest,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.persistence.email_attachment_processing import (
    InMemoryEmailAttachmentProcessingRepository,
)
from spine.infrastructure.email_attachment_ingestion import (
    ObjectStoreEmailAttachmentOriginalReader,
    build_email_attachment_ingestion,
)
from spine.infrastructure.object_storage.contracts import AuthorizedReadGrantRegistry
from spine.infrastructure.persistence.email_attachments import (
    InMemoryEmailAttachmentRepository,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
PARENT_SOURCE = UUID("30000000-0000-4000-8000-000000000001")
PARENT_REVISION = UUID("40000000-0000-4000-8000-000000000001")
ACCEPTANCE = UUID("50000000-0000-4000-8000-000000000001")
EXTRACTION_PROFILE = "email-attachment-extractor:v1:" + "a" * 64


def _attachment(
    *,
    content: bytes,
    media_type: str,
    part_path: str = "1.2",
    filename: str | None = "../../customer-secret.txt",
) -> EmailAttachmentArtifact:
    digest = hashlib.sha256(content).hexdigest()
    artifact_id = email_attachment_id(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=PARENT_REVISION,
        mime_part_path=part_path,
        extraction_profile=EXTRACTION_PROFILE,
    )
    reference = ObjectReference(
        schema_version=1,
        object_id=uuid5(NAMESPACE_URL, f"object:{artifact_id}"),
        storage_generation=uuid5(NAMESPACE_URL, f"generation:{artifact_id}"),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=len(content),
    )
    receipt = EmailAttachmentWriteReceipt(
        upload_id=uuid5(NAMESPACE_URL, f"upload:{artifact_id}"),
        object_reference=reference,
        observed_sha256=digest,
        observed_byte_length=len(content),
        verification_status="verified",
        storage_status="finalized_verified",
        finalized_at=NOW,
    )
    provisional = EmailAttachmentArtifact.model_construct(
        artifact_id=artifact_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=PARENT_SOURCE,
        source_revision_id=PARENT_REVISION,
        canonical_acceptance_id=ACCEPTANCE,
        source_revision_digest="b" * 64,
        mime_part_path=part_path,
        content_sha256=digest,
        byte_length=len(content),
        media_type=media_type,
        extraction_profile=EXTRACTION_PROFILE,
        disposition="attachment",
        filename=filename,
        content_id=None,
        write_receipt=receipt,
        artifact_sha256="0" * 64,
    )
    return EmailAttachmentArtifact.model_validate(
        provisional.model_copy(
            update={"artifact_sha256": email_attachment_artifact_digest(provisional)}
        ).model_dump()
    )


class ParentAuthority:
    def __init__(self, *, eligible: bool = True) -> None:
        self.eligible = eligible
        self.calls = 0
        self.raise_error = False

    async def resolve(self, **_values):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("synthetic authority outage")
        return OrdinaryProcessingEligibility(
            eligible=self.eligible,
            reason=(
                OrdinaryProcessingEligibilityReason.ELIGIBLE
                if self.eligible
                else OrdinaryProcessingEligibilityReason.CURRENT_REVISION_MISMATCH
            ),
            canonical_acceptance_id=ACCEPTANCE if self.eligible else None,
        )


class AttachmentAuthority:
    def __init__(self, *, allowed: bool = True, max_bytes: int = 10_000) -> None:
        self.allowed = allowed
        self.max_bytes = max_bytes
        self.calls = 0
        self.change_after_first = False

    async def resolve(self, **_values):
        self.calls += 1
        version = (
            "email-attachment-policy:v2"
            if self.change_after_first and self.calls > 1
            else "email-attachment-policy:v1"
        )
        return EmailAttachmentProcessingDecision(
            allowed=self.allowed,
            policy_version=version,
            max_attachment_bytes=self.max_bytes,
            allowed_media_types=(
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/markdown",
                "text/plain",
                "text/x-markdown",
            ),
        )


class Originals:
    def __init__(self, contents: dict[UUID, bytes]) -> None:
        self.contents = contents
        self.calls = []
        self.raise_error = False

    def read(self, **values):
        self.calls.append(values)
        artifact_id = values["attachment"].artifact_id

        async def stream():
            if self.raise_error:
                raise RuntimeError("synthetic object-store outage")
            if artifact_id not in self.contents:
                raise EmailAttachmentOriginalMissing()
            yield self.contents[artifact_id]

        return stream()


class Ingestion:
    def __init__(self) -> None:
        self.calls = []
        self.logical_effects: set[str] = set()
        self.fail_first = False
        self.cross_tenant_child = False

    async def ingest(self, *, request, chunks):
        content = b"".join([chunk async for chunk in chunks])
        self.calls.append((request, content))
        self.logical_effects.add(request.idempotency_key)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("synthetic result-loss after downstream acceptance")
        child_source_id = uuid5(
            NAMESPACE_URL, f"child-source:{request.upload_identity}"
        )
        child_revision_id = uuid5(
            NAMESPACE_URL, f"child-revision:{request.idempotency_key}"
        )
        parsed_id = uuid5(NAMESPACE_URL, f"parsed:{request.idempotency_key}")
        processing_id = uuid5(NAMESPACE_URL, f"processing:{request.idempotency_key}")
        parsed = SimpleNamespace(
            artifact_id=parsed_id,
            artifact_digest="d" * 64,
            workspace_id=(
                UUID("10000000-0000-4000-8000-000000000099")
                if self.cross_tenant_child
                else request.workspace_id
            ),
            environment_id=request.environment_id,
            source_object_id=child_source_id,
            source_revision_id=child_revision_id,
        )
        processing = SimpleNamespace(
            processing_receipt_id=processing_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            source_object_id=child_source_id,
            source_revision_id=child_revision_id,
            output_reference=parsed_id,
            output_digest="d" * 64,
        )
        return SimpleNamespace(
            status="succeeded",
            source=SimpleNamespace(
                source_object_id=child_source_id,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
            ),
            revision=SimpleNamespace(
                revision_id=child_revision_id,
                source_object_id=child_source_id,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                original_sha256=hashlib.sha256(content).hexdigest(),
                byte_length=len(content),
                media_type=request.declared_media_type,
            ),
            artifact=parsed,
            processing_receipt=processing,
        )


async def _coordinator(
    *attachments: tuple[EmailAttachmentArtifact, bytes],
    parent_authority: ParentAuthority | None = None,
    attachment_authority: AttachmentAuthority | None = None,
    ingestion: Ingestion | None = None,
):
    repository = InMemoryEmailAttachmentRepository()
    contents = {}
    for artifact, content in attachments:
        await repository.persist_many((artifact,))
        contents[artifact.artifact_id] = content
    reader = Originals(contents)
    document_ingestion = ingestion or Ingestion()
    coordinator = EmailAttachmentIngestionCoordinator(
        attachments=repository,
        parent_authority=parent_authority or ParentAuthority(),
        attachment_authority=attachment_authority or AttachmentAuthority(),
        originals=reader,
        ingestion=document_ingestion,
        receipts=InMemoryEmailAttachmentProcessingRepository(),
        clock=lambda: NOW,
    )
    return coordinator, reader, document_ingestion


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_type", "content"),
    [
        ("application/pdf", b"%PDF-1.7 synthetic"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"synthetic-docx",
        ),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b"synthetic-xlsx",
        ),
        ("text/plain", b"synthetic text"),
    ],
)
async def test_supported_attachment_routes_to_existing_ingestion_with_exact_lineage(
    media_type: str, content: bytes
) -> None:
    attachment = _attachment(content=content, media_type=media_type)
    coordinator, _reader, ingestion = await _coordinator((attachment, content))

    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    receipt = result.receipt
    assert receipt.disposition is EmailAttachmentProcessingDisposition.PROCESSED
    assert receipt.parent_source_revision_id == PARENT_REVISION
    assert receipt.attachment_artifact_id == attachment.artifact_id
    assert receipt.attachment_artifact_digest == attachment.artifact_sha256
    assert receipt.parsed_artifact_id is not None
    assert receipt.child_source_revision_id is not None
    assert ingestion.calls[0][0].declared_media_type == media_type
    assert ingestion.calls[0][0].declared_filename is None
    assert ingestion.calls[0][1] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_type",
    [
        "application/zip",
        "application/vnd.ms-word.document.macroenabled.12",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/x-dosexec",
    ],
)
async def test_dangerous_attachment_remains_fenced_and_is_never_executed(
    media_type: str,
) -> None:
    content = b"dangerous synthetic bytes"
    attachment = _attachment(content=content, media_type=media_type)
    coordinator, reader, ingestion = await _coordinator((attachment, content))

    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.DANGEROUS
    assert result.receipt.original_disposition == "retained_fenced"
    assert reader.calls == []
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_unsupported_attachment_has_independent_retained_disposition() -> None:
    content = b"synthetic image"
    attachment = _attachment(content=content, media_type="image/png")
    coordinator, reader, ingestion = await _coordinator((attachment, content))

    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert (
        result.receipt.disposition is EmailAttachmentProcessingDisposition.UNSUPPORTED
    )
    assert result.receipt.original_disposition == "retained_fenced"
    assert reader.calls == []
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_oversized_attachment_is_fenced_before_read() -> None:
    content = b"too large"
    attachment = _attachment(content=content, media_type="text/plain")
    authority = AttachmentAuthority(max_bytes=len(content) - 1)
    coordinator, reader, ingestion = await _coordinator(
        (attachment, content), attachment_authority=authority
    )

    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.OVERSIZED
    assert reader.calls == []
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_missing_original_is_fenced_without_filename_leakage() -> None:
    content = b"missing"
    attachment = _attachment(content=content, media_type="text/plain")
    coordinator, reader, ingestion = await _coordinator((attachment, content))
    reader.contents.clear()

    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.MISSING
    assert "customer-secret" not in result.receipt.model_dump_json()
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_transient_original_failure_is_retryable_and_not_recorded_missing() -> (
    None
):
    content = b"temporarily unavailable"
    attachment = _attachment(content=content, media_type="text/plain")
    coordinator, reader, ingestion = await _coordinator((attachment, content))
    reader.raise_error = True

    with pytest.raises(EmailAttachmentIngestionUnavailable):
        await coordinator.process(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            attachment_artifact_id=attachment.artifact_id,
        )
    reader.raise_error = False
    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.PROCESSED
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_transient_authority_failure_is_retryable_and_not_policy_denied() -> None:
    content = b"authority unavailable"
    attachment = _attachment(content=content, media_type="text/plain")
    parent = ParentAuthority()
    parent.raise_error = True
    coordinator, reader, ingestion = await _coordinator(
        (attachment, content), parent_authority=parent
    )

    with pytest.raises(EmailAttachmentIngestionUnavailable):
        await coordinator.process(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            attachment_artifact_id=attachment.artifact_id,
        )
    parent.raise_error = False
    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.PROCESSED
    assert len(reader.calls) == 1
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_revoked_parent_denies_child_before_original_read() -> None:
    content = b"protected"
    attachment = _attachment(content=content, media_type="text/plain")
    coordinator, reader, ingestion = await _coordinator(
        (attachment, content), parent_authority=ParentAuthority(eligible=False)
    )

    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert (
        result.receipt.disposition is EmailAttachmentProcessingDisposition.POLICY_DENIED
    )
    assert reader.calls == []
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_other_workspace_cannot_discover_or_process_attachment() -> None:
    content = b"tenant protected"
    attachment = _attachment(content=content, media_type="text/plain")
    coordinator, reader, ingestion = await _coordinator((attachment, content))

    with pytest.raises(EmailAttachmentIngestionUnavailable) as error:
        await coordinator.process(
            workspace_id=UUID("10000000-0000-4000-8000-000000000099"),
            environment_id=ENVIRONMENT,
            attachment_artifact_id=attachment.artifact_id,
        )

    assert str(error.value) == "email attachment processing is unavailable"
    assert "tenant protected" not in str(error.value)
    assert "customer-secret" not in str(error.value)
    assert reader.calls == []
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_allowed_authority_change_after_read_retries_without_false_denial() -> (
    None
):
    content = b"policy race"
    attachment = _attachment(content=content, media_type="text/plain")
    authority = AttachmentAuthority()
    authority.change_after_first = True
    coordinator, reader, ingestion = await _coordinator(
        (attachment, content), attachment_authority=authority
    )

    with pytest.raises(EmailAttachmentIngestionUnavailable):
        await coordinator.process(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            attachment_artifact_id=attachment.artifact_id,
        )
    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.PROCESSED
    assert len(reader.calls) == 2
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_replay_returns_one_logical_processing_effect() -> None:
    content = b"replay"
    attachment = _attachment(content=content, media_type="text/plain")
    coordinator, _reader, ingestion = await _coordinator((attachment, content))

    first = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )
    replay = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert first.replay is False
    assert replay.replay is True
    assert replay.receipt == first.receipt
    assert len(ingestion.calls) == 1
    assert len(ingestion.logical_effects) == 1


@pytest.mark.asyncio
async def test_allowed_policy_version_change_reuses_one_downstream_effect() -> None:
    content = b"policy replay"
    attachment = _attachment(content=content, media_type="text/plain")
    authority = AttachmentAuthority()
    coordinator, _reader, ingestion = await _coordinator(
        (attachment, content), attachment_authority=authority
    )

    first = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )
    authority.change_after_first = True
    current_policy = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert first.receipt.authority_digest != current_policy.receipt.authority_digest
    assert len(ingestion.calls) == 2
    assert (
        ingestion.calls[0][0].idempotency_key == ingestion.calls[1][0].idempotency_key
    )
    assert len(ingestion.logical_effects) == 1


@pytest.mark.asyncio
async def test_replay_rechecks_current_parent_authority_before_returning_receipt() -> (
    None
):
    content = b"revoked after processing"
    attachment = _attachment(content=content, media_type="text/plain")
    parent = ParentAuthority()
    coordinator, _reader, ingestion = await _coordinator(
        (attachment, content), parent_authority=parent
    )

    processed = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )
    parent.eligible = False
    denied = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert (
        processed.receipt.disposition is EmailAttachmentProcessingDisposition.PROCESSED
    )
    assert (
        denied.receipt.disposition is EmailAttachmentProcessingDisposition.POLICY_DENIED
    )
    assert denied.receipt.authority_digest != processed.receipt.authority_digest
    assert (
        denied.receipt.processing_receipt_id != processed.receipt.processing_receipt_id
    )
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_retry_after_lost_result_reuses_downstream_idempotency_identity() -> None:
    content = b"partial failure"
    attachment = _attachment(content=content, media_type="text/plain")
    ingestion = Ingestion()
    ingestion.fail_first = True
    coordinator, _reader, ingestion = await _coordinator(
        (attachment, content), ingestion=ingestion
    )

    with pytest.raises(EmailAttachmentIngestionUnavailable):
        await coordinator.process(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            attachment_artifact_id=attachment.artifact_id,
        )
    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.PROCESSED
    assert len(ingestion.calls) == 2
    assert (
        ingestion.calls[0][0].idempotency_key == ingestion.calls[1][0].idempotency_key
    )
    assert len(ingestion.logical_effects) == 1


@pytest.mark.asyncio
async def test_cross_tenant_child_lineage_is_rejected_without_receipt() -> None:
    content = b"wrong tenant lineage"
    attachment = _attachment(content=content, media_type="text/plain")
    ingestion = Ingestion()
    ingestion.cross_tenant_child = True
    coordinator, _reader, _ingestion = await _coordinator(
        (attachment, content), ingestion=ingestion
    )

    with pytest.raises(EmailAttachmentIngestionUnavailable):
        await coordinator.process(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            attachment_artifact_id=attachment.artifact_id,
        )


@pytest.mark.asyncio
async def test_failed_disposition_rejects_foreign_processing_lineage() -> None:
    content = b"failed foreign lineage"
    attachment = _attachment(content=content, media_type="text/plain")

    class FailedIngestion(Ingestion):
        async def ingest(self, *, request, chunks):
            result = await super().ingest(request=request, chunks=chunks)
            result.status = "failed"
            result.artifact = None
            result.processing_receipt.workspace_id = UUID(
                "10000000-0000-4000-8000-000000000099"
            )
            return result

    coordinator, _reader, _ingestion = await _coordinator(
        (attachment, content), ingestion=FailedIngestion()
    )

    with pytest.raises(EmailAttachmentIngestionUnavailable):
        await coordinator.process(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            attachment_artifact_id=attachment.artifact_id,
        )


@pytest.mark.asyncio
async def test_multiple_parts_keep_independent_safe_dispositions() -> None:
    text = _attachment(content=b"text", media_type="text/plain", part_path="1.2")
    archive = _attachment(content=b"zip", media_type="application/zip", part_path="1.3")
    coordinator, _reader, ingestion = await _coordinator(
        (text, b"text"), (archive, b"zip")
    )

    results = await coordinator.process_many(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_ids=(text.artifact_id, archive.artifact_id),
    )

    assert tuple(result.receipt.disposition for result in results.completed) == (
        EmailAttachmentProcessingDisposition.PROCESSED,
        EmailAttachmentProcessingDisposition.DANGEROUS,
    )
    assert results.retryable_attachment_ids == ()
    assert len(ingestion.calls) == 1


@pytest.mark.asyncio
async def test_multiple_parts_continue_after_retryable_partial_failure() -> None:
    missing = _attachment(content=b"missing", media_type="text/plain", part_path="1.2")
    archive = _attachment(content=b"zip", media_type="application/zip", part_path="1.3")
    coordinator, reader, ingestion = await _coordinator(
        (missing, b"missing"), (archive, b"zip")
    )
    reader.raise_error = True

    result = await coordinator.process_many(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_ids=(missing.artifact_id, archive.artifact_id),
    )

    assert result.retryable_attachment_ids == (missing.artifact_id,)
    assert tuple(item.receipt.disposition for item in result.completed) == (
        EmailAttachmentProcessingDisposition.DANGEROUS,
    )
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_same_logical_receipt_with_later_clock_is_a_replay() -> None:
    content = b"stable replay"
    attachment = _attachment(content=content, media_type="text/plain")
    coordinator, _reader, _ingestion = await _coordinator((attachment, content))
    first = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )
    repository = InMemoryEmailAttachmentProcessingRepository()
    await repository.persist(first.receipt)
    provisional = first.receipt.model_copy(
        update={"completed_at": NOW + timedelta(seconds=1), "receipt_sha256": "0" * 64}
    )
    later = EmailAttachmentProcessingReceipt.model_validate(
        provisional.model_copy(
            update={
                "receipt_sha256": email_attachment_processing_receipt_digest(
                    provisional
                )
            }
        ).model_dump()
    )

    replay = await repository.persist(later)

    assert replay.replay is True
    assert replay.receipt == first.receipt


@pytest.mark.asyncio
async def test_object_store_reader_uses_scope_bound_one_shot_grant() -> None:
    content = b"governed object bytes"
    attachment = _attachment(content=content, media_type="text/plain")
    grants = AuthorizedReadGrantRegistry()

    class Store:
        binding = None

        def open_bounded_read(self, lease, *, max_bytes):
            self.binding = grants.consume_read_lease(lease, now=NOW)

            async def stream():
                assert max_bytes == 100
                yield content

            return stream()

    store = Store()
    reader = ObjectStoreEmailAttachmentOriginalReader(
        object_store=store,  # type: ignore[arg-type]
        grant_registry=grants,
        clock=lambda: NOW,
    )

    observed = b"".join(
        [
            chunk
            async for chunk in reader.read(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                attachment=attachment,
                max_bytes=100,
                policy_version="email-attachment-policy:v1",
                authority_digest="f" * 64,
            )
        ]
    )

    assert observed == content
    assert store.binding.workspace_id == WORKSPACE
    assert store.binding.environment_id == ENVIRONMENT
    assert store.binding.object_reference == attachment.write_receipt.object_reference


@pytest.mark.asyncio
async def test_production_composition_routes_attachment_to_existing_ingestion() -> None:
    content = b"composed attachment route"
    attachment = _attachment(content=content, media_type="text/plain")
    attachments = InMemoryEmailAttachmentRepository()
    await attachments.persist_many((attachment,))
    grants = AuthorizedReadGrantRegistry()

    class Store:
        def open_bounded_read(self, lease, *, max_bytes):
            grants.consume_read_lease(lease, now=NOW)

            async def stream():
                assert max_bytes == 10_000
                yield content

            return stream()

    ingestion = Ingestion()
    decision = await AttachmentAuthority().resolve()
    assert decision is not None
    coordinator = build_email_attachment_ingestion(
        attachments=attachments,
        parent_authority=ParentAuthority(),
        decision=decision,
        object_store=Store(),  # type: ignore[arg-type]
        grant_registry=grants,
        ingestion=ingestion,
        receipts=InMemoryEmailAttachmentProcessingRepository(),
        clock=lambda: NOW,
    )

    result = await coordinator.process(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        attachment_artifact_id=attachment.artifact_id,
    )

    assert result.receipt.disposition is EmailAttachmentProcessingDisposition.PROCESSED
    assert len(ingestion.logical_effects) == 1
