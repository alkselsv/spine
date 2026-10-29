from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.artifacts.transcription import SpeechToTextSegment, TranscriptionArtifact


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
