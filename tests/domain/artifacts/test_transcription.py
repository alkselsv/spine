from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.artifacts.transcription import (
    AudioChunkBoundary,
    AudioMediaInspection,
    SpeechToTextResult,
    SpeechToTextSegment,
    TranscriptionArtifact,
    TranscriptionChunkReceipt,
    TranscriptionProcessingProfile,
    TranscriptionTimeRangeLocator,
    audio_chunk_id,
)


def processing_profile() -> TranscriptionProcessingProfile:
    return TranscriptionProcessingProfile(
        profile_reference="bounded-russian:v1",
        supported_media_types=("audio/wav", "audio/mpeg", "audio/mp4"),
        supported_codecs=("pcm_s16le", "mp3", "aac"),
        maximum_source_bytes=20_000_000,
        maximum_duration_ms=60_000,
        maximum_decoded_bytes=120_000_000,
        maximum_memory_bytes=64_000_000,
        maximum_working_bytes=160_000_000,
        maximum_decode_seconds=5.0,
        chunk_duration_ms=4_000,
        chunk_overlap_ms=1_000,
        maximum_chunks=20,
        maximum_model_attempts=2,
        decoder_reference="synthetic-decoder:v1",
        reassembly_reference="first-chunk-wins:v1",
    )


def test_processing_profile_plans_reproducible_overlapping_chunks() -> None:
    profile = processing_profile()
    inspection = AudioMediaInspection(
        inspected_media_type="audio/wav",
        container="wav",
        codec="pcm_s16le",
        byte_length=320_000,
        duration_ms=10_000,
        audio_stream_count=1,
        silent=False,
    )

    first = profile.plan_chunks(inspection)
    replay = profile.plan_chunks(inspection)

    assert first == replay
    assert [(item.start_ms, item.end_ms, item.overlap_before_ms) for item in first] == [
        (0, 4_000, 0),
        (3_000, 7_000, 1_000),
        (6_000, 10_000, 1_000),
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"inspected_media_type": "audio/ogg", "codec": "opus"}, "unsupported media"),
        ({"byte_length": 20_000_001}, "source byte limit"),
        ({"duration_ms": 60_001}, "duration limit"),
        ({"audio_stream_count": 0}, "audio stream"),
        ({"silent": True}, "silent audio"),
    ],
)
def test_processing_profile_rejects_ineligible_media_before_chunking(
    overrides: dict[str, object], message: str
) -> None:
    inspection = AudioMediaInspection(
        inspected_media_type="audio/wav",
        container="wav",
        codec="pcm_s16le",
        byte_length=320_000,
        duration_ms=10_000,
        audio_stream_count=1,
        silent=False,
    ).model_copy(update=overrides)

    with pytest.raises(ValueError, match=message):
        processing_profile().plan_chunks(inspection)


def test_transcript_segment_rejects_bounds_outside_original_timeline() -> None:
    with pytest.raises(ValidationError, match="recording duration"):
        TranscriptionArtifact.create(
            workspace_id=UUID(int=1),
            environment_id=UUID(int=2),
            source_object_id=UUID(int=3),
            source_revision_id=UUID(int=4),
            source_revision_digest="a" * 64,
            original_sha256="b" * 64,
            processing_job_id=UUID(int=5),
            processing_profile_reference="short:v1",
            adapter_reference="fake:v1",
            model_config_reference="ru:v1",
            recording_duration_ms=1000,
            language="ru",
            segments=(
                SpeechToTextSegment(start_ms=900, end_ms=1001, text="слишком поздно"),
            ),
        )


def test_transcript_segment_rejects_inverted_bounds() -> None:
    with pytest.raises(ValidationError, match="end_ms"):
        SpeechToTextSegment(start_ms=500, end_ms=499, text="invalid")


def test_v1_speech_result_serialization_keeps_original_contract_shape() -> None:
    result = SpeechToTextResult(
        adapter_reference="fake:v1",
        model_config_reference="ru:v1",
        detected_language="ru",
        segments=(SpeechToTextSegment(start_ms=0, end_ms=1, text="Да"),),
    )

    assert "timeline" not in result.model_dump()


def test_v1_artifact_rejects_v2_chunk_receipts() -> None:
    artifact = TranscriptionArtifact.create(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        source_object_id=UUID(int=3),
        source_revision_id=UUID(int=4),
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_job_id=UUID(int=5),
        processing_profile_reference="short:v1",
        adapter_reference="fake:v1",
        model_config_reference="ru:v1",
        recording_duration_ms=1000,
        language="ru",
        segments=(SpeechToTextSegment(start_ms=0, end_ms=1, text="Да"),),
    )
    receipt = TranscriptionChunkReceipt(
        chunk_id=UUID(int=6),
        boundary=AudioChunkBoundary(
            ordinal=1,
            start_ms=0,
            end_ms=1000,
            overlap_before_ms=0,
        ),
        model_attempts=1,
    )

    with pytest.raises(ValidationError, match="v2 outcome fields"):
        TranscriptionArtifact.model_validate(
            {**artifact.model_dump(), "chunk_receipts": (receipt,)}
        )


def test_time_range_locator_binds_exact_source_and_artifact_lineage() -> None:
    artifact = TranscriptionArtifact.create(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        source_object_id=UUID(int=3),
        source_revision_id=UUID(int=4),
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_job_id=UUID(int=5),
        processing_profile_reference="short:v1",
        adapter_reference="fake:v1",
        model_config_reference="ru:v1",
        recording_duration_ms=1000,
        language="ru",
        segments=(SpeechToTextSegment(start_ms=100, end_ms=900, text="Да"),),
    )

    locator = artifact.locate_time_range(start_ms=120, end_ms=700)

    artifact.validate_time_range_locator(locator)
    assert locator.source_revision_id == artifact.source_revision_id
    assert locator.artifact_id == artifact.artifact_id
    assert locator.artifact_digest == artifact.artifact_digest


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [(-1, 10), (10, 10), (10, 1001)],
)
def test_time_range_locator_rejects_invalid_or_out_of_range_offsets(
    start_ms: int, end_ms: int
) -> None:
    artifact = TranscriptionArtifact.create(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        source_object_id=UUID(int=3),
        source_revision_id=UUID(int=4),
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_job_id=UUID(int=5),
        processing_profile_reference="short:v1",
        adapter_reference="fake:v1",
        model_config_reference="ru:v1",
        recording_duration_ms=1000,
        language="ru",
        segments=(SpeechToTextSegment(start_ms=100, end_ms=900, text="Да"),),
    )

    with pytest.raises((ValueError, ValidationError)):
        artifact.locate_time_range(start_ms=start_ms, end_ms=end_ms)


def test_time_range_locator_rejects_another_artifact_version() -> None:
    locator = TranscriptionTimeRangeLocator(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        source_object_id=UUID(int=3),
        source_revision_id=UUID(int=4),
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        artifact_id=UUID(int=6),
        artifact_digest="c" * 64,
        artifact_schema_version="transcription-artifact:v1",
        start_ms=0,
        end_ms=100,
    )
    artifact = TranscriptionArtifact.create(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        source_object_id=UUID(int=3),
        source_revision_id=UUID(int=4),
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_job_id=UUID(int=5),
        processing_profile_reference="short:v1",
        adapter_reference="fake:v1",
        model_config_reference="ru:v1",
        recording_duration_ms=1000,
        language="ru",
        segments=(SpeechToTextSegment(start_ms=0, end_ms=100, text="Да"),),
    )

    with pytest.raises(ValueError, match="lineage"):
        artifact.validate_time_range_locator(locator)


def test_completed_bounded_artifact_rejects_incomplete_chunk_coverage() -> None:
    boundary = AudioChunkBoundary(
        ordinal=1, start_ms=0, end_ms=4000, overlap_before_ms=0
    )

    with pytest.raises(ValidationError, match="full recording chunk coverage"):
        TranscriptionArtifact.create(
            workspace_id=UUID(int=1),
            environment_id=UUID(int=2),
            source_object_id=UUID(int=3),
            source_revision_id=UUID(int=4),
            source_revision_digest="a" * 64,
            original_sha256="b" * 64,
            processing_job_id=UUID(int=5),
            processing_profile_reference="bounded:v1",
            adapter_reference="fake:v1",
            model_config_reference="ru:v1",
            recording_duration_ms=10_000,
            language="ru",
            media_inspection=AudioMediaInspection(
                inspected_media_type="audio/wav",
                container="wav",
                codec="pcm_s16le",
                byte_length=320_000,
                duration_ms=10_000,
                audio_stream_count=1,
                silent=False,
            ),
            decoder_reference="synthetic-decoder:v1",
            decoded_sha256="c" * 64,
            reassembly_reference="first-chunk-wins:v1",
            chunk_receipts=(
                TranscriptionChunkReceipt(
                    chunk_id=audio_chunk_id(
                        decoded_sha256="c" * 64,
                        decoder_reference="synthetic-decoder:v1",
                        boundary=boundary,
                    ),
                    boundary=boundary,
                    model_attempts=1,
                ),
            ),
            segments=(SpeechToTextSegment(start_ms=100, end_ms=500, text="Да"),),
        )
