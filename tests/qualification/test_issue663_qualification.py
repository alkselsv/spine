from spine.application.transcription import TranscribeAudioCommand, TranscriptionService
from spine.infrastructure.persistence.postgresql_mappings import (
    transcript_segments,
    transcription_artifacts,
    transcription_jobs,
)

def test_issue663_processing_contract_has_no_business_run_or_projection_identity() -> None:
    assert {
        "workspace_id",
        "environment_id",
        "source_revision",
        "processing_profile_reference",
        "adapter_reference",
        "model_config_reference",
        "idempotency_key",
    } <= set(TranscribeAudioCommand.model_fields)
    assert {
        "work_item_id",
        "workflow_run_id",
        "qa_session_id",
        "projection_id",
    }.isdisjoint(TranscribeAudioCommand.model_fields)
    assert set(TranscriptionService.__init__.__annotations__) >= {
        "repository",
        "speech_to_text",
        "authorizer",
    }


def test_issue663_canonical_tables_reference_only_source_job_and_artifact_lineage() -> None:
    tables = (transcription_jobs, transcription_artifacts, transcript_segments)
    foreign_tables = {
        foreign_key.column.table.name
        for table in tables
        for foreign_key in table.foreign_keys
    }

    assert foreign_tables == {
        "source_revisions",
        "transcription_jobs",
        "transcription_artifacts",
    }
    assert {"work_items", "workflow_runs", "qa_session_versions"}.isdisjoint(
        foreign_tables
    )
