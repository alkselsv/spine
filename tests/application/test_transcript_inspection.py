from uuid import UUID

import pytest

from spine.application.transcript_inspection import (
    TranscriptEvidenceInspector,
    TranscriptInspectionStatus,
    TranscriptReviewState,
)
from spine.application.transcript_publication import (
    TranscriptAuthorizationSnapshot,
)
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.artifacts import TimeRangeLocator

from .test_transcript_publication import published_corrected_fixture, published_fixture


WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT = UUID("20000000-0000-0000-0000-000000000001")
SUBJECT = UUID("80000000-0000-0000-0000-000000000001")


def context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_parsed_content"),
        trace_id=UUID("90000000-0000-0000-0000-000000000001"),
        acting_subject_id=SUBJECT,
        service_principal_id=None,
        provenance=TrustedContextProvenance(issuer_id=UUID("a0000000-0000-0000-0000-000000000001"), signature=b"trusted"),
    )


def requester() -> ParsedContentInspectionRequester:
    return ParsedContentInspectionRequester(subject_id=SUBJECT)


@pytest.mark.asyncio
async def test_inspection_returns_exact_authorized_locator_and_safe_navigation_bounds() -> None:
    transcript, repository, authorization, publication = await published_fixture()
    locator = TimeRangeLocator.from_segment(
        artifact=transcript,
        segment=transcript.segments[0],
    )
    inspector = TranscriptEvidenceInspector(
        publications=repository, authorization=authorization
    )

    result = await inspector.inspect(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        locator=locator,
    )

    assert result.status is TranscriptInspectionStatus.CURRENT
    assert result.locator == locator
    assert result.source_revision_id == transcript.source_revision_id
    assert result.transcript_artifact_id == transcript.artifact_id
    assert result.publication_id == publication.publication_id
    assert result.review_state is TranscriptReviewState.APPROVED
    assert result.recording_duration_ms == 10_000
    assert result.text == "protected transcript"
    assert "original_reference" not in result.model_dump()


@pytest.mark.asyncio
async def test_inspection_returns_current_corrected_transcript_evidence() -> None:
    corrected, repository, authorization, publication = (
        await published_corrected_fixture()
    )
    locator = publication.segments[0].locator

    result = await TranscriptEvidenceInspector(
        publications=repository,
        authorization=authorization,
    ).inspect(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        locator=locator,
    )

    assert result.status is TranscriptInspectionStatus.CURRENT
    assert result.locator == locator
    assert result.transcript_artifact_id == corrected.artifact_id
    assert result.text == "corrected transcript"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locator_update", "expected_status"),
    [
        (
            {"transcript_artifact_id": UUID("81000000-0000-0000-0000-000000000001")},
            TranscriptInspectionStatus.REVIEW_REQUIRED,
        ),
        ({"transcript_artifact_digest": "f" * 64}, TranscriptInspectionStatus.STALE),
        (
            {"transcript_artifact_schema_version": "fabricated-transcript:v1"},
            TranscriptInspectionStatus.STALE,
        ),
        ({"end_ms": 10_001}, TranscriptInspectionStatus.STALE),
    ],
)
async def test_inspection_rejects_stale_corrected_transcript_locator(
    locator_update: dict[str, object],
    expected_status: TranscriptInspectionStatus,
) -> None:
    _, repository, authorization, publication = await published_corrected_fixture()
    locator = publication.segments[0].locator.model_copy(update=locator_update)

    result = await TranscriptEvidenceInspector(
        publications=repository,
        authorization=authorization,
    ).inspect(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        locator=locator,
    )

    assert result.status is expected_status
    assert result.text is None
    assert result.locator is None


@pytest.mark.asyncio
async def test_inspection_denial_returns_no_transcript_or_recording_metadata() -> None:
    transcript, repository, authorization, _ = await published_fixture()
    authorization.allowed = False
    locator = TimeRangeLocator.from_segment(
        artifact=transcript,
        segment=transcript.segments[0],
    )
    inspector = TranscriptEvidenceInspector(
        publications=repository,
        authorization=authorization,
    )

    result = await inspector.inspect(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        locator=locator,
    )

    assert result.status is TranscriptInspectionStatus.PERMISSION_DENIED
    assert result.locator is None
    assert result.source_revision_id is None
    assert result.transcript_artifact_id is None
    assert result.recording_duration_ms is None
    assert result.text is None


@pytest.mark.asyncio
async def test_inspection_rejects_time_range_beyond_recording_bounds() -> None:
    transcript, repository, authorization, _ = await published_fixture()
    locator = TimeRangeLocator.from_segment(
        artifact=transcript,
        segment=transcript.segments[0],
    ).model_copy(update={"end_ms": 10_001})
    inspector = TranscriptEvidenceInspector(
        publications=repository,
        authorization=authorization,
    )

    result = await inspector.inspect(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        locator=locator,
    )

    assert result.status is TranscriptInspectionStatus.STALE
    assert result.text is None
