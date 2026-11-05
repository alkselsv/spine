from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.email_citations import (
    EmailAttachmentCitationExcerptResolver,
    EmailAttachmentProjectionLocatorProjector,
    build_email_attachment_citation_locator,
)
from spine.application.citation_inspector import (
    CitationInspectorRequester,
    ProtectedCitationInspector,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.artifacts import (
    ParseCoverage,
    ParsedDocument,
    ParsedSegment,
    PhysicalLineLocator,
)
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentArtifact,
    EmailAttachmentProcessingDisposition,
    EmailAttachmentProcessingReceipt,
    EmailAttachmentWriteReceipt,
    email_attachment_artifact_digest,
    email_attachment_id,
    email_attachment_processing_receipt_digest,
    encode_email_attachment_locator,
)
from spine.domain.artifacts.models import segment_digest
from spine.domain.artifacts.email_html import EmailHtmlSanitizerProfile
from spine.domain.artifacts.email_message import (
    EmailMessageArtifact,
    email_message_artifact_digest,
)
from spine.domain.capabilities import Citation, compose_answer_disclosure
from spine.domain.context_bundles import EvidenceLocator
from spine.domain.disclosure import (
    CitationInspectionReason,
    CitationInspectionResult,
    CitationInspectionStatus,
    DisclosureRevalidationDisposition,
    DisclosureRevalidationFailureCode,
    DisclosureRevalidationResult,
)
from spine.domain.evaluations import EvaluationVerdict
from spine.evaluations.email_citation_resolution import (
    EMAIL_ATTACHMENT_CITATION_VECTORS,
    evaluate_email_citation_resolution,
)
from spine.domain.sources import RevisionKind, RevisionMetadata, content_revision
from spine.domain.sources.original_reference import ObjectReference
from spine.ingestion.email_html import EmailHtmlSanitizer
from spine.ingestion.email_mime import MimeParser, MimeParserProfile
from spine.infrastructure.persistence.email_attachment_processing import (
    InMemoryEmailAttachmentProcessingRepository,
    PostgreSQLEmailAttachmentProcessingRepository,
)
from spine.memory.contracts import StableLocator

from tests.application.test_citation_validation import _bundle, _reference
from tests.application.test_citation_inspector import (
    CitationRepository,
    ContextBundleRepository,
    SUBJECT,
    context,
)
from tests.domain.capabilities.test_answer_disclosure import _request
from tests.domain.capabilities.test_answer_question_candidate import candidate_output

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
PARENT_SOURCE = UUID(int=101)
PARENT_REVISION = UUID(int=102)
CHILD_SOURCE = UUID(int=103)
CHILD_REVISION = UUID(int=104)
RAW = (
    b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
    b"--b\r\nContent-Type: text/plain\r\n\r\nMessage body\r\n"
    b"--b\r\nContent-Type: text/plain\r\n"
    b'Content-Disposition: attachment; filename="secret.txt"\r\n\r\n'
    b"Attachment evidence\r\n--b--\r\n"
)
ATTACHMENT_BYTES = b"Attachment evidence"


def _evaluate_attachment_vector(
    vector_id: str, result: CitationInspectionResult
) -> None:
    vector = next(
        item
        for item in EMAIL_ATTACHMENT_CITATION_VECTORS
        if item.vector_id == vector_id
    )
    evaluation = evaluate_email_citation_resolution(
        result,
        expected_status=vector.expected_status,
        vector_id=vector.vector_id,
        citation_id=UUID(int=900 + len(vector_id)),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        locator_contract_version=vector.locator_contract_version,
    )
    assert evaluation.verdict is EvaluationVerdict.PASS


def _parent() -> EmailMessageArtifact:
    parser = MimeParser(
        profile=MimeParserProfile(
            max_total_bytes=10_000,
            max_header_bytes=5_000,
            max_parts=20,
            max_depth=5,
            max_decoded_bytes=10_000,
            max_parse_seconds=2.0,
        )
    )
    sanitizer = EmailHtmlSanitizer(
        profile=EmailHtmlSanitizerProfile(
            max_total_input_bytes=10_000,
            max_total_output_bytes=10_000,
            max_parts=20,
            max_depth=5,
        )
    )
    mime = parser.parse(RAW)
    provisional = EmailMessageArtifact.model_construct(
        artifact_id=UUID(int=105),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=PARENT_SOURCE,
        source_revision_id=PARENT_REVISION,
        canonical_acceptance_id=UUID(int=106),
        source_revision_digest="a" * 64,
        raw_sha256=hashlib.sha256(RAW).hexdigest(),
        raw_byte_length=len(RAW),
        parser_profile=parser.profile.reference,
        sanitizer_profile=sanitizer.reference,
        mime=mime,
        sanitized_html=sanitizer.sanitize_all(mime),
        selected_representation="plain_text",
        selected_text="Message body",
        artifact_sha256="0" * 64,
    )
    return EmailMessageArtifact.model_validate(
        provisional.model_copy(
            update={"artifact_sha256": email_message_artifact_digest(provisional)}
        ).model_dump()
    )


def _attachment(parent: EmailMessageArtifact) -> EmailAttachmentArtifact:
    content_digest = hashlib.sha256(ATTACHMENT_BYTES).hexdigest()
    extraction_profile = "email-attachment-extractor:v1:" + "b" * 64
    artifact_id = email_attachment_id(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision_id=parent.source_revision_id,
        mime_part_path="1.2",
        extraction_profile=extraction_profile,
    )
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID(int=107),
        storage_generation=UUID(int=108),
        digest_algorithm="sha256",
        digest_hex=content_digest,
        byte_length=len(ATTACHMENT_BYTES),
    )
    provisional = EmailAttachmentArtifact.model_construct(
        artifact_id=artifact_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=parent.source_object_id,
        source_revision_id=parent.source_revision_id,
        canonical_acceptance_id=parent.canonical_acceptance_id,
        source_revision_digest=parent.source_revision_digest,
        mime_part_path="1.2",
        content_sha256=content_digest,
        byte_length=len(ATTACHMENT_BYTES),
        media_type="text/plain",
        extraction_profile=extraction_profile,
        disposition="attachment",
        filename="secret.txt",
        content_id=None,
        write_receipt=EmailAttachmentWriteReceipt(
            upload_id=UUID(int=109),
            object_reference=reference,
            observed_sha256=content_digest,
            observed_byte_length=len(ATTACHMENT_BYTES),
            verification_status="verified",
            storage_status="finalized_verified",
            finalized_at=NOW,
        ),
        artifact_sha256="0" * 64,
    )
    return EmailAttachmentArtifact.model_validate(
        provisional.model_copy(
            update={"artifact_sha256": email_attachment_artifact_digest(provisional)}
        ).model_dump()
    )


def _parsed(attachment: EmailAttachmentArtifact) -> ParsedDocument:
    revision = content_revision(
        revision_id=CHILD_REVISION,
        source_object_id=CHILD_SOURCE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=RevisionMetadata(embedded_title="Synthetic attachment"),
        original_reference=attachment.write_receipt.object_reference,
        original_sha256=attachment.content_sha256,
        byte_length=attachment.byte_length,
        media_type=attachment.media_type,
        observed_at=NOW,
    )
    text = "Attachment evidence"
    text_digest = hashlib.sha256(text.encode()).hexdigest()
    provisional_digest = "0" * 64
    locator = PhysicalLineLocator(
        line_number=1,
        start_codepoint=0,
        end_codepoint=len(text),
    )
    provisional_segment_digest = segment_digest(
        artifact_digest=provisional_digest,
        locator=locator,
        text_digest=text_digest,
        heading_path=(),
    )
    segment = ParsedSegment(
        segment_id=uuid5(
            NAMESPACE_URL,
            f"spine:parsed-segment:v1:{provisional_segment_digest}",
        ),
        segment_digest=provisional_segment_digest,
        artifact_digest=provisional_digest,
        text=text,
        text_digest=text_digest,
        locator=locator,
    )
    return ParsedDocument.from_source(
        revision,
        parser_name="text",
        parser_version="text:v1",
        parser_config_reference="text-config:v1",
        locator_contract_version="email-attachment-locator:v1",
        media_type="text/plain",
        coverage=ParseCoverage.COMPLETE,
        segments=(segment,),
        exclusions=(),
        source_byte_length=len(ATTACHMENT_BYTES),
        source_codepoint_length=len("Attachment evidence"),
    )


def _receipt(
    parent: EmailMessageArtifact,
    attachment: EmailAttachmentArtifact,
    parsed: ParsedDocument,
) -> EmailAttachmentProcessingReceipt:
    provisional = EmailAttachmentProcessingReceipt.model_construct(
        processing_receipt_id=UUID(int=111),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        parent_source_object_id=parent.source_object_id,
        parent_source_revision_id=parent.source_revision_id,
        parent_source_revision_digest=parent.source_revision_digest,
        parent_canonical_acceptance_id=parent.canonical_acceptance_id,
        attachment_artifact_id=attachment.artifact_id,
        attachment_artifact_digest=attachment.artifact_sha256,
        mime_part_path=attachment.mime_part_path,
        attachment_content_sha256=attachment.content_sha256,
        attachment_byte_length=attachment.byte_length,
        media_type=attachment.media_type,
        policy_version="email-attachment-policy:v1",
        authority_digest="e" * 64,
        processor_profile="email-attachment-document-ingestion:v1",
        disposition=EmailAttachmentProcessingDisposition.PROCESSED,
        original_disposition="retained",
        child_source_object_id=parsed.source_object_id,
        child_source_revision_id=parsed.source_revision_id,
        child_processing_receipt_id=UUID(int=112),
        parsed_artifact_id=parsed.artifact_id,
        parsed_artifact_digest=parsed.artifact_digest,
        completed_at=NOW,
        receipt_sha256="0" * 64,
    )
    return EmailAttachmentProcessingReceipt.model_validate(
        provisional.model_copy(
            update={
                "receipt_sha256": email_attachment_processing_receipt_digest(
                    provisional
                )
            }
        ).model_dump()
    )


def _authority_successor(
    first: EmailAttachmentProcessingReceipt,
) -> EmailAttachmentProcessingReceipt:
    provisional = first.model_copy(
        update={
            "processing_receipt_id": UUID(int=999),
            "policy_version": "email-attachment-policy:v2",
            "authority_digest": "f" * 64,
            "completed_at": first.completed_at + timedelta(seconds=1),
            "receipt_sha256": "0" * 64,
        }
    )
    return EmailAttachmentProcessingReceipt.model_validate(
        provisional.model_copy(
            update={
                "receipt_sha256": email_attachment_processing_receipt_digest(
                    provisional
                )
            }
        ).model_dump()
    )


class _Reader:
    def __init__(self, value: object, identity_name: str) -> None:
        self.value = value
        self.identity_name = identity_name
        self.calls = 0

    async def get(self, **values):
        self.calls += 1
        return (
            self.value
            if values["artifact_id"] == getattr(self.value, self.identity_name)
            and values["workspace_id"] == WORKSPACE
            and values["environment_id"] == ENVIRONMENT
            else None
        )

    async def list_for_revision(self, **values):
        self.calls += 1
        if (
            values["workspace_id"] == WORKSPACE
            and values["environment_id"] == ENVIRONMENT
            and values["source_revision_id"]
            == getattr(self.value, "source_revision_id")
        ):
            return (self.value,)
        return ()


class _ReceiptReader:
    def __init__(self, receipt: EmailAttachmentProcessingReceipt) -> None:
        self.receipt = receipt
        self.calls = 0

    async def resolve_by_id(self, **values):
        self.calls += 1
        return (
            self.receipt
            if values["processing_receipt_id"] == self.receipt.processing_receipt_id
            else None
        )

    async def resolve_by_child_processing_receipt_id(self, **values):
        self.calls += 1
        return (
            self.receipt
            if values["child_processing_receipt_id"]
            == self.receipt.child_processing_receipt_id
            else None
        )


class _ParsedReader:
    def __init__(self, document: ParsedDocument) -> None:
        self.document = document
        self.get_calls = 0
        self.resolve_calls = 0

    async def get(self, **values):
        self.get_calls += 1
        if (
            values["workspace_id"] == self.document.workspace_id
            and values["environment_id"] == self.document.environment_id
            and values["artifact_id"] == self.document.artifact_id
        ):
            return self.document
        return None

    async def resolve_stable_segment(self, **values):
        self.resolve_calls += 1
        if (
            values["source_object_id"] == self.document.source_object_id
            and values["source_revision_id"] == self.document.source_revision_id
            and values["source_revision_digest"] == self.document.source_revision_digest
            and values["original_sha256"] == self.document.original_sha256
            and values["locator_kind"] == "character_range"
            and values["locator_value"] == "line:1:0-19"
        ):
            return self.document, self.document.segments[0]
        return None


def _fixture():
    parent = _parent()
    attachment = _attachment(parent)
    parsed = _parsed(attachment)
    receipt = _receipt(parent, attachment, parsed)
    locator = build_email_attachment_citation_locator(
        parent=parent,
        attachment=attachment,
        receipt=receipt,
        parsed_artifact=parsed,
        child_locator=StableLocator(kind="character_range", value="line:1:0-19"),
    )
    reference = encode_email_attachment_locator(locator)
    stable = StableLocator(kind="custom", value=reference)
    child_reference = _reference(
        source_id=CHILD_SOURCE,
        revision_id=CHILD_REVISION,
        evidence_id="attachment-child",
    ).model_copy(
        update={
            "original_sha256": attachment.content_sha256,
            "revision_digest": parsed.source_revision_digest,
            "processing_receipt_id": receipt.processing_receipt_id,
            "locator_contract_version": locator.schema_version,
            "locator": EvidenceLocator(kind="custom", value=reference),
            "locator_digest": canonical_projection_locator_digest(
                locator_contract_version=locator.schema_version, locator=stable
            ),
        }
    )
    parent_reference = _reference(
        source_id=PARENT_SOURCE,
        revision_id=PARENT_REVISION,
        evidence_id="attachment-parent",
    ).model_copy(
        update={
            "original_sha256": parent.raw_sha256,
            "revision_digest": parent.source_revision_digest,
        }
    )
    bundle = _bundle(parent_reference, child_reference)
    base = candidate_output().answer.citations[0]
    citation = Citation(
        citation_id=base.citation_id,
        answer_id=base.answer_id,
        context_bundle=bundle.lineage_reference,
        evidence_reference=child_reference,
    )
    resolver = EmailAttachmentCitationExcerptResolver(
        messages=_Reader(parent, "artifact_id"),
        attachments=_Reader(attachment, "artifact_id"),
        processing=_ReceiptReader(receipt),
        parsed_artifacts=_ParsedReader(parsed),
    )
    return resolver, citation, locator, parent, attachment, receipt, parsed, bundle


@pytest.mark.asyncio
async def test_exact_attachment_lineage_resolves_authorized_parsed_range() -> None:
    resolver, citation, locator, *_ = _fixture()

    result = await resolver.resolve(citation, historical=False)

    assert result is not None
    assert result.excerpt == "Attachment evidence"
    assert result.source_object_id == CHILD_SOURCE
    assert resolver.dependency_source_identities(citation) == (
        (PARENT_SOURCE, PARENT_REVISION),
        (CHILD_SOURCE, CHILD_REVISION),
    )
    assert locator.parent_mime_part_path == "1.2"


@pytest.mark.asyncio
async def test_projection_emits_attachment_locator_with_complete_lineage() -> None:
    _resolver, _citation, _locator, parent, attachment, receipt, parsed, _bundle_value = (
        _fixture()
    )
    assert receipt.child_processing_receipt_id is not None
    projected = await EmailAttachmentProjectionLocatorProjector(
        messages=_Reader(parent, "artifact_id"),
        attachments=_Reader(attachment, "artifact_id"),
        processing=_ReceiptReader(receipt),
    ).project(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        child_processing_receipt_id=receipt.child_processing_receipt_id,
        attachment_required=True,
        parsed_artifact=parsed,
        child_locator=StableLocator(
            kind="character_range", value="line:1:0-19"
        ),
    )

    assert projected is not None
    schema, stable = projected
    assert schema == "email-attachment-locator:v1"
    assert stable.kind == "custom"
    assert stable.value.startswith("ea1|")


@pytest.mark.asyncio
async def test_projection_rejects_attachment_when_parent_lineage_is_incomplete() -> None:
    _resolver, _citation, _locator, parent, attachment, receipt, parsed, _bundle_value = (
        _fixture()
    )
    assert receipt.child_processing_receipt_id is not None
    wrong_parent = parent.model_copy(
        update={"canonical_acceptance_id": UUID(int=999)}
    )

    with pytest.raises(ValueError, match="projection lineage is unavailable"):
        await EmailAttachmentProjectionLocatorProjector(
            messages=_Reader(wrong_parent, "artifact_id"),
            attachments=_Reader(attachment, "artifact_id"),
            processing=_ReceiptReader(receipt),
        ).project(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            child_processing_receipt_id=receipt.child_processing_receipt_id,
            attachment_required=True,
            parsed_artifact=parsed,
            child_locator=StableLocator(
                kind="character_range", value="line:1:0-19"
            ),
        )


@pytest.mark.asyncio
async def test_projection_does_not_downgrade_missing_attachment_lineage() -> None:
    _resolver, _citation, _locator, parent, attachment, receipt, parsed, _bundle_value = (
        _fixture()
    )
    projector = EmailAttachmentProjectionLocatorProjector(
        messages=_Reader(parent, "artifact_id"),
        attachments=_Reader(attachment, "artifact_id"),
        processing=_ReceiptReader(receipt),
    )

    with pytest.raises(ValueError, match="projection lineage is unavailable"):
        await projector.project(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            child_processing_receipt_id=UUID(int=999),
            attachment_required=True,
            parsed_artifact=parsed,
            child_locator=StableLocator(
                kind="character_range", value="line:1:0-19"
            ),
        )

    assert await projector.project(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        child_processing_receipt_id=UUID(int=999),
        attachment_required=False,
        parsed_artifact=parsed,
        child_locator=StableLocator(kind="character_range", value="line:1:0-19"),
    ) is None


@pytest.mark.asyncio
async def test_in_memory_processing_lookup_by_id_is_tenant_scoped() -> None:
    _resolver, _citation, _locator, _parent_value, _attachment_value, receipt, *_ = (
        _fixture()
    )
    repository = InMemoryEmailAttachmentProcessingRepository()
    await repository.persist(receipt)

    assert await repository.resolve_by_id(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        processing_receipt_id=receipt.processing_receipt_id,
    ) == receipt
    assert await repository.resolve_by_id(
        workspace_id=UUID(int=999),
        environment_id=ENVIRONMENT,
        processing_receipt_id=receipt.processing_receipt_id,
    ) is None
    assert receipt.child_processing_receipt_id is not None
    assert await repository.resolve_by_child_processing_receipt_id(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        child_processing_receipt_id=receipt.child_processing_receipt_id,
    ) == receipt


@pytest.mark.asyncio
async def test_child_processing_lookup_selects_latest_equivalent_authority_receipt() -> None:
    _resolver, _citation, _locator, _parent_value, _attachment_value, first, *_ = (
        _fixture()
    )
    later = _authority_successor(first)
    repository = InMemoryEmailAttachmentProcessingRepository()
    await repository.persist(first)
    await repository.persist(later)
    assert first.child_processing_receipt_id is not None

    selected = await repository.resolve_by_child_processing_receipt_id(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        child_processing_receipt_id=first.child_processing_receipt_id,
    )

    assert selected == later


@pytest.mark.asyncio
async def test_postgresql_child_lookup_uses_same_authority_selection_contract() -> None:
    _resolver, _citation, _locator, _parent_value, _attachment_value, first, *_ = (
        _fixture()
    )
    later = _authority_successor(first)

    class Rows:
        def scalars(self):
            return self

        def all(self):
            return [
                first.model_dump(mode="json"),
                later.model_dump(mode="json"),
            ]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_values):
            return None

        async def execute(self, _statement):
            return Rows()

    class Repository(PostgreSQLEmailAttachmentProcessingRepository):
        async def _bind_tenant_scope(self, *_values):
            return None

    repository = Repository(
        lambda: Session(),
        context_factory=lambda *_values: None,  # type: ignore[arg-type]
        context_verifier=None,  # type: ignore[arg-type]
    )
    assert first.child_processing_receipt_id is not None

    selected = await repository.resolve_by_child_processing_receipt_id(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        child_processing_receipt_id=first.child_processing_receipt_id,
    )

    assert selected == later


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        "parent_part",
        "parent_digest",
        "attachment_digest",
        "processing_digest",
        "child_revision",
        "cross_tenant",
        "stale_parsed_artifact",
    ),
)
async def test_wrong_attachment_lineage_discloses_no_text(mutation: str) -> None:
    resolver, citation, locator, parent, attachment, receipt, parsed, _ = _fixture()
    if mutation == "parent_part":
        locator = locator.model_copy(update={"parent_mime_part_path": "1.1"})
    elif mutation == "parent_digest":
        locator = locator.model_copy(update={"parent_locator_digest": "f" * 64})
    elif mutation == "attachment_digest":
        locator = locator.model_copy(update={"attachment_artifact_digest": "f" * 64})
    elif mutation == "processing_digest":
        locator = locator.model_copy(update={"processing_receipt_digest": "f" * 64})
    elif mutation == "child_revision":
        citation = citation.model_copy(
            update={
                "evidence_reference": citation.evidence_reference.model_copy(
                    update={"source_revision_id": UUID(int=999)}
                )
            }
        )
    elif mutation == "cross_tenant":
        resolver = EmailAttachmentCitationExcerptResolver(
            messages=_Reader(parent, "artifact_id"),
            attachments=_Reader(attachment, "artifact_id"),
            processing=_ReceiptReader(receipt),
            parsed_artifacts=_ParsedReader(parsed),
        )
        citation = citation.model_copy(
            update={
                "context_bundle": citation.context_bundle.model_copy(
                    update={"workspace_id": UUID(int=999)}
                ),
                "evidence_reference": citation.evidence_reference.model_copy(
                    update={"workspace_id": UUID(int=999)}
                ),
            }
        )
    elif mutation == "stale_parsed_artifact":
        resolver = EmailAttachmentCitationExcerptResolver(
            messages=_Reader(parent, "artifact_id"),
            attachments=_Reader(attachment, "artifact_id"),
            processing=_ReceiptReader(receipt),
            parsed_artifacts=_ParsedReader(
                parsed.model_copy(update={"artifact_id": UUID(int=998)})
            ),
        )
    if mutation in {
        "parent_part",
        "parent_digest",
        "attachment_digest",
        "processing_digest",
    }:
        reference = encode_email_attachment_locator(locator)
        evidence = citation.evidence_reference.model_copy(
            update={
                "locator": EvidenceLocator(kind="custom", value=reference),
                "locator_digest": canonical_projection_locator_digest(
                    locator_contract_version=locator.schema_version,
                    locator=StableLocator(kind="custom", value=reference),
                ),
            }
        )
        citation = citation.model_copy(update={"evidence_reference": evidence})

    assert await resolver.resolve(citation, historical=False) is None
    vector_ids = {
        "parent_part": "attachment_wrong_mime_part",
        "parent_digest": "attachment_wrong_parent",
        "attachment_digest": "attachment_wrong_artifact_digest",
        "processing_digest": "attachment_wrong_processing_digest",
        "child_revision": "attachment_stale_child_revision",
        "cross_tenant": "attachment_cross_workspace",
        "stale_parsed_artifact": "attachment_stale_parsed_artifact",
    }
    _evaluate_attachment_vector(
        vector_ids[mutation],
        CitationInspectionResult(
            status=CitationInspectionStatus.UNAVAILABLE,
            reason=CitationInspectionReason.CITATION_UNAVAILABLE,
        ),
    )


@pytest.mark.asyncio
async def test_invalid_parent_stops_before_child_artifacts_are_loaded() -> None:
    _resolver, citation, locator, parent, attachment, receipt, parsed, _bundle_value = (
        _fixture()
    )
    wrong_parent = parent.model_copy(update={"artifact_id": UUID(int=999)})
    messages = _Reader(wrong_parent, "artifact_id")
    attachments = _Reader(attachment, "artifact_id")
    processing = _ReceiptReader(receipt)
    parsed_artifacts = _ParsedReader(parsed)
    resolver = EmailAttachmentCitationExcerptResolver(
        messages=messages,
        attachments=attachments,
        processing=processing,
        parsed_artifacts=parsed_artifacts,
    )
    # Preserve the requested key so the invalid parent is returned and must be
    # rejected before any child repository is consulted.
    locator = locator.model_copy(
        update={"parent_message_artifact_id": wrong_parent.artifact_id}
    )
    reference = encode_email_attachment_locator(locator)
    citation = citation.model_copy(
        update={
            "evidence_reference": citation.evidence_reference.model_copy(
                update={
                    "locator": EvidenceLocator(kind="custom", value=reference),
                    "locator_digest": canonical_projection_locator_digest(
                        locator_contract_version=locator.schema_version,
                        locator=StableLocator(kind="custom", value=reference),
                    ),
                }
            )
        }
    )

    assert await resolver.resolve(citation, historical=False) is None
    assert messages.calls == 1
    assert attachments.calls == 0
    assert processing.calls == 0
    assert parsed_artifacts.get_calls == 0
    assert parsed_artifacts.resolve_calls == 0


@pytest.mark.parametrize(
    "field", ("receipt_workspace", "parsed_environment", "acceptance")
)
def test_locator_builder_rejects_mixed_scope_or_acceptance(field: str) -> None:
    _resolver, _citation, _locator, parent, attachment, receipt, parsed, _bundle_value = (
        _fixture()
    )
    if field == "receipt_workspace":
        receipt = receipt.model_copy(update={"workspace_id": UUID(int=999)})
    elif field == "parsed_environment":
        parsed = parsed.model_copy(update={"environment_id": UUID(int=999)})
    else:
        attachment = attachment.model_copy(
            update={"canonical_acceptance_id": UUID(int=999)}
        )

    with pytest.raises(ValueError, match="lineage is unavailable"):
        build_email_attachment_citation_locator(
            parent=parent,
            attachment=attachment,
            receipt=receipt,
            parsed_artifact=parsed,
            child_locator=StableLocator(
                kind="character_range", value="line:1:0-19"
            ),
        )


class _RecordingDisclosure:
    def __init__(self) -> None:
        self.request = None

    async def revalidate(self, request):
        self.request = request
        return DisclosureRevalidationResult(
            request_id=request.request_id,
            request_digest=request.command_digest,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            checkpoint=request.checkpoint,
            disposition=DisclosureRevalidationDisposition.SAFE_REUSE,
            safe_reason="citation_disclosure_allowed",
        )


class _DeniedDisclosure(_RecordingDisclosure):
    def __init__(self, denied_source: UUID) -> None:
        super().__init__()
        self.denied_source = denied_source

    async def revalidate(self, request):
        self.request = request
        assert self.denied_source in {
            item.source_object_id for item in request.dependencies.dependencies
        }
        return DisclosureRevalidationResult(
            request_id=request.request_id,
            request_digest=request.command_digest,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            checkpoint=request.checkpoint,
            disposition=DisclosureRevalidationDisposition.INELIGIBLE,
            failure_codes=(DisclosureRevalidationFailureCode.CONTRIBUTOR_UNAUTHORIZED,),
            safe_reason="citation_unavailable",
        )


@pytest.mark.asyncio
async def test_attachment_inspection_revalidates_parent_then_child_before_excerpt() -> (
    None
):
    resolver, citation, _locator, *_rest, bundle = _fixture()
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    persisted = accepted.answer.citations[0]
    citation = citation.model_copy(
        update={
            "citation_id": persisted.citation_id,
            "answer_id": persisted.answer_id,
        }
    )
    accepted = accepted.model_copy(
        update={"answer": accepted.answer.model_copy(update={"citations": (citation,)})}
    )
    disclosure = _RecordingDisclosure()

    result = await ProtectedCitationInspector(
        citation_repository=CitationRepository(accepted),
        context_bundle_repository=ContextBundleRepository(bundle),
        disclosure_revalidator=disclosure,
        excerpt_resolver=resolver,
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=citation.citation_id,
    )

    assert result.status is CitationInspectionStatus.AVAILABLE
    _evaluate_attachment_vector("attachment_exact_lineage", result)
    assert disclosure.request is not None
    assert tuple(
        (item.source_object_id, item.source_revision_id)
        for item in disclosure.request.dependencies.dependencies
    ) == (
        (PARENT_SOURCE, PARENT_REVISION),
        (CHILD_SOURCE, CHILD_REVISION),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_source", (PARENT_SOURCE, CHILD_SOURCE))
async def test_revoked_parent_or_child_denies_attachment_without_excerpt(
    denied_source: UUID,
) -> None:
    resolver, citation, _locator, *_rest, bundle = _fixture()
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    persisted = accepted.answer.citations[0]
    citation = citation.model_copy(
        update={
            "citation_id": persisted.citation_id,
            "answer_id": persisted.answer_id,
        }
    )
    accepted = accepted.model_copy(
        update={"answer": accepted.answer.model_copy(update={"citations": (citation,)})}
    )

    result = await ProtectedCitationInspector(
        citation_repository=CitationRepository(accepted),
        context_bundle_repository=ContextBundleRepository(bundle),
        disclosure_revalidator=_DeniedDisclosure(denied_source),
        excerpt_resolver=resolver,
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=citation.citation_id,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE
    assert result.evidence is None
    assert "Attachment evidence" not in result.model_dump_json()
    assert "secret.txt" not in result.model_dump_json()
    _evaluate_attachment_vector(
        (
            "attachment_revoked_parent"
            if denied_source == PARENT_SOURCE
            else "attachment_revoked_child"
        ),
        result,
    )
