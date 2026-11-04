from uuid import UUID

import httpx
import pytest

from spine.api.citation_inspector import install_citation_inspector_routes
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.artifacts.email_message import (
    EmailMessageCitationLocator,
    encode_email_message_locator,
)
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
    ContextBundleRef,
    EvidenceLocator,
)
from spine.domain.disclosure import (
    CitationInspectionEvidence,
    CitationInspectionReason,
    CitationInspectionResult,
    CitationInspectionStatus,
)
from spine.memory.contracts import StableLocator
from tests.api.test_authorization import ENVIRONMENT_ID, WORKSPACE_ID, authorized_app


class UnavailableCitationInspector:
    async def inspect(self, **kwargs):
        del kwargs
        return CitationInspectionResult(
            status=CitationInspectionStatus.UNAVAILABLE,
            reason=CitationInspectionReason.CITATION_UNAVAILABLE,
        )


class AvailableEmailCitationInspector:
    def __init__(self, representation: str) -> None:
        self.representation = representation

    async def inspect(self, **kwargs):
        del kwargs
        source_object_id = UUID(int=101)
        source_revision_id = UUID(int=102)
        locator = EmailMessageCitationLocator(
            source_revision_id=source_revision_id,
            source_revision_digest="1" * 64,
            artifact_id=UUID(int=103),
            artifact_digest="2" * 64,
            parser_profile=f"mime-parser:v1:{'3' * 64}",
            sanitizer_profile=f"email-html-sanitizer:v1:{'4' * 64}",
            part_path="1.1" if self.representation == "plain_text" else "1.2",
            representation=self.representation,
            start_codepoint=6,
            end_codepoint=10,
            representation_digest="5" * 64,
            excerpt_digest="6" * 64,
        )
        locator_reference = encode_email_message_locator(locator)
        locator_digest = canonical_projection_locator_digest(
            locator_contract_version=locator.schema_version,
            locator=StableLocator(kind="message", value=locator_reference),
        )
        reference = ContextBundleEvidenceReference(
            schema_version="context-evidence-reference:v2",
            evidence_id="email-evidence-858",
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_snapshot_id=UUID(int=104),
            activation_generation=1,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            original_sha256="7" * 64,
            revision_digest=locator.source_revision_digest,
            processing_receipt_id=UUID(int=105),
            projected_kind="chunk",
            locator_contract_version=locator.schema_version,
            locator=EvidenceLocator(kind="message", value=locator_reference),
            locator_digest=locator_digest,
            evidence_digest="8" * 64,
        )
        bundle = ContextBundleLineageRef(
            bundle=ContextBundleRef(
                bundle_id=UUID(int=106),
                bundle_version=1,
                bundle_digest="9" * 64,
            ),
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            run_id=UUID(int=107),
            attempt_id=UUID(int=108),
        )
        evidence = CitationInspectionEvidence(
            citation_id=UUID(int=109),
            answer_id=UUID(int=110),
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            original_sha256=reference.original_sha256,
            revision_digest=reference.revision_digest,
            locator_schema=locator.schema_version,
            locator_reference=locator_reference,
            locator_digest=locator_digest,
            context_bundle=bundle,
            evidence_reference=reference,
            excerpt="body",
        )
        return CitationInspectionResult(
            status=CitationInspectionStatus.AVAILABLE,
            reason=CitationInspectionReason.AVAILABLE,
            evidence=evidence,
        )


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_citation_inspector_uses_safe_error_for_unavailable_content() -> None:
    application = authorized_app([])
    install_citation_inspector_routes(
        application,
        service=UnavailableCitationInspector(),  # type: ignore[arg-type]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/qa/citations/{UUID(int=999)}", headers=headers()
        )

    assert response.status_code == 404
    assert response.json()["code"] == "spine.qa.citations.unavailable"
    assert "999" not in response.text
    assert "excerpt" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ("plain_text", "sanitized_html_text"))
async def test_citation_inspector_returns_authorized_email_range_and_locator_lineage(
    representation: str,
) -> None:
    application = authorized_app([])
    install_citation_inspector_routes(
        application,
        service=AvailableEmailCitationInspector(representation),  # type: ignore[arg-type]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/qa/citations/{UUID(int=109)}", headers=headers()
        )

    assert response.status_code == 200
    evidence = response.json()["data"]["evidence"]
    assert evidence["excerpt"] == "body"
    assert evidence["locator_schema"] == "email-message-locator:v1"
    assert evidence["evidence_reference"]["locator"]["kind"] == "message"
