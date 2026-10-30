from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.artifacts import (
    CorrectedTranscriptArtifact,
    PhysicalLineLocator,
    SpeechToTextSegment,
    TimeRangeLocator,
    TranscriptionArtifact,
    transcript_artifact_language,
    validate_transcript_artifact_locator,
)


WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT = UUID("20000000-0000-0000-0000-000000000001")
SOURCE_OBJECT = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_REVISION = UUID("40000000-0000-0000-0000-000000000001")
PROCESSING_JOB = UUID("50000000-0000-0000-0000-000000000001")


def artifact() -> TranscriptionArtifact:
    return TranscriptionArtifact.create(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE_OBJECT,
        source_revision_id=SOURCE_REVISION,
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_job_id=PROCESSING_JOB,
        processing_profile_reference="transcription:v1",
        adapter_reference="fake-stt:v1",
        model_config_reference="fake-model:v1",
        recording_duration_ms=10_000,
        language="ru",
        segments=(
            SpeechToTextSegment(start_ms=1_000, end_ms=2_500, text="Первый сегмент"),
        ),
    )


def corrected_artifact() -> CorrectedTranscriptArtifact:
    original = artifact()
    return CorrectedTranscriptArtifact.create(
        original=original,
        review_id=UUID("60000000-0000-0000-0000-000000000001"),
        segments=(
            SpeechToTextSegment(
                start_ms=1_000,
                end_ms=2_500,
                text="Исправленный сегмент",
            ),
        ),
        created_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
    )


def test_time_range_locator_pins_exact_source_and_transcript_version() -> None:
    transcript = artifact()

    locator = TimeRangeLocator.from_segment(
        artifact=transcript,
        segment=transcript.segments[0],
    )

    assert locator.source_revision_id == SOURCE_REVISION
    assert locator.source_revision_digest == "a" * 64
    assert locator.transcript_artifact_id == transcript.artifact_id
    assert locator.transcript_artifact_digest == transcript.artifact_digest
    assert locator.transcript_artifact_schema_version == transcript.schema_version
    assert (locator.start_ms, locator.end_ms) == (1_000, 2_500)
    assert locator.segment_ids == (transcript.segments[0].segment_id,)
    assert len(locator.digest) == 64


def test_time_range_locator_rejects_cross_artifact_or_invalid_range() -> None:
    transcript = artifact()
    locator = TimeRangeLocator.from_segment(
        artifact=transcript,
        segment=transcript.segments[0],
    )

    with pytest.raises(ValidationError, match="end_ms"):
        TimeRangeLocator.model_validate(
            {**locator.model_dump(), "end_ms": locator.start_ms}
        )

    with pytest.raises(ValueError, match="does not belong"):
        TimeRangeLocator.from_segment(
            artifact=transcript,
            segment=transcript.segments[0].model_copy(
                update={"segment_id": UUID("60000000-0000-0000-0000-000000000001")}
            ),
        )


def test_corrected_transcript_locator_validates_shared_lineage_contract() -> None:
    corrected = corrected_artifact()
    locator = TimeRangeLocator.from_segment(
        artifact=corrected,
        segment=corrected.segments[0],
    )

    validate_transcript_artifact_locator(corrected, locator)

    assert transcript_artifact_language(corrected) is None


@pytest.mark.parametrize(
    "locator_update",
    [
        {"transcript_artifact_digest": "f" * 64},
        {"transcript_artifact_schema_version": "fabricated-transcript:v1"},
        {"end_ms": 10_001},
    ],
)
def test_corrected_transcript_locator_rejects_fabricated_lineage(
    locator_update: dict[str, object],
) -> None:
    corrected = corrected_artifact()
    locator = TimeRangeLocator.from_segment(
        artifact=corrected,
        segment=corrected.segments[0],
    ).model_copy(update=locator_update)

    with pytest.raises(ValueError, match="lineage"):
        validate_transcript_artifact_locator(corrected, locator)


def test_existing_document_locators_remain_unchanged() -> None:
    locator = PhysicalLineLocator(
        line_number=1,
        start_codepoint=0,
        end_codepoint=4,
    )

    assert locator.kind == "physical_line"
