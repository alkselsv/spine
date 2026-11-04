from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.citation_inspector import (
    CitationInspectorRequester,
    ProtectedCitationInspector,
)
from spine.application.disclosure_revalidation import (
    DisclosureRevalidationService,
    InMemoryDisclosureValidationResultRepository,
)
from spine.application.email_citations import (
    EmailMessageCitationExcerptResolver,
    build_email_message_citation_locator,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.artifacts.email_html import EmailHtmlSanitizerProfile
from spine.domain.artifacts.email_message import (
    EmailMessageArtifact,
    EmailMessageCitationLocator,
    email_message_artifact_digest,
    encode_email_message_locator,
)
from spine.domain.capabilities import Citation, compose_answer_disclosure
from spine.domain.context_bundles import EvidenceLocator
from spine.ingestion.email_html import EmailHtmlSanitizer
from spine.ingestion.email_mime import MimeParser, MimeParserProfile
from spine.memory.contracts import StableLocator

from tests.application.test_citation_validation import _reference
from tests.application.test_citation_validation import _bundle
from tests.application.test_citation_inspector import (
    CitationRepository,
    ContextBundleRepository,
    CurrentState,
    SUBJECT,
    context,
)
from tests.domain.capabilities.test_answer_disclosure import _request
from tests.domain.capabilities.test_answer_question_candidate import candidate_output


ARTIFACT_ID = UUID("50000000-0000-4000-8000-000000000858")
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
RAW = (
    b'MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary="b"\r\n\r\n'
    b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlain body evidence\r\n"
    b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<p>HTML <strong>body</strong> evidence</p><script>private()</script>\r\n"
    b"--b--\r\n"
)


class ArtifactReader:
    def __init__(self, artifact: EmailMessageArtifact) -> None:
        self.artifact = artifact

    async def get(self, *, workspace_id, environment_id, artifact_id):
        if (
            workspace_id == self.artifact.workspace_id
            and environment_id == self.artifact.environment_id
            and artifact_id == self.artifact.artifact_id
        ):
            return self.artifact
        return None


class FailingArtifactReader:
    async def get(self, **_kwargs):
        raise RuntimeError(
            "token-858 raw-provider-payload protected-subject "
            "sender@example.test protected.eml Plain body evidence"
        )


def _artifact() -> EmailMessageArtifact:
    base = candidate_output().answer.citations[0]
    evidence = base.evidence_reference
    parser = MimeParser(
        profile=MimeParserProfile(
            max_total_bytes=10_000,
            max_header_bytes=5_000,
            max_parts=30,
            max_depth=10,
            max_decoded_bytes=10_000,
            max_parse_seconds=2.0,
        )
    )
    sanitizer = EmailHtmlSanitizer(
        profile=EmailHtmlSanitizerProfile(
            max_total_input_bytes=10_000,
            max_total_output_bytes=10_000,
            max_parts=30,
            max_depth=10,
        )
    )
    mime = parser.parse(RAW)
    safe_html = sanitizer.sanitize_all(mime)
    provisional = EmailMessageArtifact.model_construct(
        artifact_id=ARTIFACT_ID,
        workspace_id=base.workspace_id,
        environment_id=base.environment_id,
        source_object_id=evidence.source_object_id,
        source_revision_id=evidence.source_revision_id,
        canonical_acceptance_id=UUID(int=858),
        source_revision_digest=evidence.revision_digest,
        raw_sha256=hashlib.sha256(RAW).hexdigest(),
        raw_byte_length=len(RAW),
        parser_profile=parser.profile.reference,
        sanitizer_profile=sanitizer.reference,
        mime=mime,
        sanitized_html=safe_html,
        selected_representation="plain_text",
        selected_text="Plain body evidence",
        artifact_sha256="0" * 64,
    )
    return EmailMessageArtifact.model_validate(
        provisional.model_copy(
            update={"artifact_sha256": email_message_artifact_digest(provisional)}
        ).model_dump()
    )


def _locator(
    artifact: EmailMessageArtifact,
    *,
    representation: str,
    part_path: str,
    text: str,
    start: int,
    end: int,
    **updates: object,
) -> EmailMessageCitationLocator:
    values: dict[str, object] = {
        "source_revision_id": artifact.source_revision_id,
        "source_revision_digest": artifact.source_revision_digest,
        "artifact_id": artifact.artifact_id,
        "artifact_digest": artifact.artifact_sha256,
        "parser_profile": artifact.parser_profile,
        "sanitizer_profile": artifact.sanitizer_profile,
        "part_path": part_path,
        "representation": representation,
        "start_codepoint": start,
        "end_codepoint": end,
        "representation_digest": hashlib.sha256(text.encode()).hexdigest(),
        "excerpt_digest": hashlib.sha256(text[start:end].encode()).hexdigest(),
    }
    values.update(updates)
    return EmailMessageCitationLocator(**values)  # type: ignore[arg-type]


def _citation(
    artifact: EmailMessageArtifact,
    locator: EmailMessageCitationLocator,
    *,
    workspace_id: UUID | None = None,
) -> Citation:
    base = candidate_output().answer.citations[0]
    effective_workspace_id = workspace_id or artifact.workspace_id
    reference = encode_email_message_locator(locator)
    stable = StableLocator(kind="message", value=reference)
    evidence = _reference(
        source_id=artifact.source_object_id,
        revision_id=artifact.source_revision_id,
        evidence_id="email-evidence-858",
    ).model_copy(
        update={
            "workspace_id": effective_workspace_id,
            "original_sha256": artifact.raw_sha256,
            "revision_digest": artifact.source_revision_digest,
            "locator_contract_version": locator.schema_version,
            "locator": EvidenceLocator(kind="message", value=reference),
            "locator_digest": canonical_projection_locator_digest(
                locator_contract_version=locator.schema_version,
                locator=stable,
            ),
        }
    )
    context_bundle = base.context_bundle.model_copy(
        update={"workspace_id": effective_workspace_id}
    )
    return base.model_copy(
        update={"context_bundle": context_bundle, "evidence_reference": evidence}
    )


def _inspector_fixture(
    artifact: EmailMessageArtifact,
    locator: EmailMessageCitationLocator,
    *,
    state: CurrentState,
    reader: object | None = None,
) -> tuple[ProtectedCitationInspector, Citation]:
    email_citation = _citation(artifact, locator)
    bundle = _bundle(email_citation.evidence_reference)
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    persisted = accepted.answer.citations[0]
    email_citation = email_citation.model_copy(
        update={
            "citation_id": persisted.citation_id,
            "answer_id": persisted.answer_id,
            "context_bundle": bundle.lineage_reference,
        }
    )
    accepted = accepted.model_copy(
        update={
            "answer": accepted.answer.model_copy(
                update={"citations": (email_citation,)}
            )
        }
    )
    return (
        ProtectedCitationInspector(
            citation_repository=CitationRepository(accepted),
            context_bundle_repository=ContextBundleRepository(bundle),
            disclosure_revalidator=DisclosureRevalidationService(
                state_provider=state,
                result_repository=InMemoryDisclosureValidationResultRepository(),
                clock=lambda: NOW,
            ),
            excerpt_resolver=EmailMessageCitationExcerptResolver(
                reader or ArtifactReader(artifact)  # type: ignore[arg-type]
            ),
        ),
        email_citation,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("representation", "part_path", "text", "start", "end", "expected"),
    (
        ("plain_text", "1.1", "Plain body evidence", 6, 10, "body"),
        ("sanitized_html_text", "1.2", "HTML body evidence", 5, 9, "body"),
    ),
)
async def test_exact_email_body_range_resolves_from_pinned_artifact(
    representation: str,
    part_path: str,
    text: str,
    start: int,
    end: int,
    expected: str,
) -> None:
    artifact = _artifact()
    locator = _locator(
        artifact,
        representation=representation,
        part_path=part_path,
        text=text,
        start=start,
        end=end,
    )

    assert build_email_message_citation_locator(
        artifact,
        part_path=part_path,
        representation=representation,  # type: ignore[arg-type]
        start_codepoint=start,
        end_codepoint=end,
    ) == locator

    result = await EmailMessageCitationExcerptResolver(ArtifactReader(artifact)).resolve(
        _citation(artifact, locator), historical=False
    )

    assert result is not None
    assert result.excerpt == expected
    assert result.locator_reference == encode_email_message_locator(locator)
    assert "private" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    (
        {"artifact_id": UUID(int=999)},
        {"artifact_digest": "f" * 64},
        {"parser_profile": f"mime-parser:v1:{'f' * 64}"},
        {"sanitizer_profile": f"email-html-sanitizer:v1:{'f' * 64}"},
        {"source_revision_digest": "f" * 64},
        {"end_codepoint": 999},
        {"representation_digest": "f" * 64},
        {"excerpt_digest": "f" * 64},
    ),
)
async def test_fabricated_email_locator_fails_closed_without_message_content(
    updates: dict[str, object],
) -> None:
    artifact = _artifact()
    text = "Plain body evidence"
    locator = _locator(
        artifact,
        representation="plain_text",
        part_path="1.1",
        text=text,
        start=6,
        end=10,
        **updates,
    )

    result = await EmailMessageCitationExcerptResolver(ArtifactReader(artifact)).resolve(
        _citation(artifact, locator), historical=False
    )

    assert result is None


@pytest.mark.asyncio
async def test_wrong_email_locator_digest_fails_closed() -> None:
    artifact = _artifact()
    text = "Plain body evidence"
    locator = _locator(
        artifact,
        representation="plain_text",
        part_path="1.1",
        text=text,
        start=6,
        end=10,
    )
    citation = _citation(artifact, locator)
    forged_evidence = citation.evidence_reference.model_copy(
        update={"locator_digest": "f" * 64}
    )

    result = await EmailMessageCitationExcerptResolver(ArtifactReader(artifact)).resolve(
        citation.model_copy(update={"evidence_reference": forged_evidence}),
        historical=False,
    )

    assert result is None


@pytest.mark.asyncio
async def test_email_locator_is_isolated_between_workspaces() -> None:
    artifact = _artifact()
    text = "Plain body evidence"
    locator = _locator(
        artifact,
        representation="plain_text",
        part_path="1.1",
        text=text,
        start=6,
        end=10,
    )

    result = await EmailMessageCitationExcerptResolver(ArtifactReader(artifact)).resolve(
        _citation(artifact, locator, workspace_id=UUID(int=999)), historical=False
    )

    assert result is None


@pytest.mark.asyncio
async def test_authorized_email_citation_inspection_releases_exact_range() -> None:
    artifact = _artifact()
    locator = _locator(
        artifact,
        representation="plain_text",
        part_path="1.1",
        text="Plain body evidence",
        start=6,
        end=10,
    )
    inspector, citation = _inspector_fixture(
        artifact,
        locator,
        state=CurrentState(),
    )

    result = await inspector.inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=citation.citation_id,
    )

    assert result.evidence is not None
    assert result.evidence.excerpt == "body"


@pytest.mark.asyncio
async def test_email_citation_repository_failure_does_not_disclose_sensitive_details() -> None:
    artifact = _artifact()
    locator = _locator(
        artifact,
        representation="plain_text",
        part_path="1.1",
        text="Plain body evidence",
        start=6,
        end=10,
    )
    inspector, citation = _inspector_fixture(
        artifact,
        locator,
        state=CurrentState(),
        reader=FailingArtifactReader(),
    )

    result = await inspector.inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=citation.citation_id,
    )

    serialized = result.model_dump_json()
    assert result.evidence is None
    for protected in (
        "token-858",
        "raw-provider-payload",
        "protected-subject",
        "sender@example.test",
        "protected.eml",
        "Plain body evidence",
    ):
        assert protected not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    (
        {"contributor_authorized": False},
        {"retention_eligible": False},
        {"tombstoned": True},
        {"revision_current": False, "current_revision_id": UUID(int=998)},
        {
            "superseded": True,
            "revision_current": False,
            "current_revision_id": UUID(int=999),
        },
    ),
)
async def test_email_citation_inspection_revalidates_current_authority_before_resolution(
    updates: dict[str, object],
) -> None:
    artifact = _artifact()
    locator = _locator(
        artifact,
        representation="plain_text",
        part_path="1.1",
        text="Plain body evidence",
        start=6,
        end=10,
    )
    inspector, email_citation = _inspector_fixture(
        artifact,
        locator,
        state=CurrentState(**updates),
    )

    result = await inspector.inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=email_citation.citation_id,
    )

    assert result.evidence is None
    assert "Plain body evidence" not in result.model_dump_json()
