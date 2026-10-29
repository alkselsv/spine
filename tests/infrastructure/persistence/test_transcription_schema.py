from spine.infrastructure.persistence.postgresql_mappings import (
    transcript_segments,
    transcription_artifacts,
    transcription_jobs,
)


def test_transcription_tables_are_tenant_scoped_and_keep_source_job_lineage() -> None:
    assert [column.name for column in transcription_jobs.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "job_id",
    ]
    assert "uq_transcription_jobs_command" in {
        constraint.name for constraint in transcription_jobs.constraints
    }
    assert [column.name for column in transcription_artifacts.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "artifact_id",
    ]
    assert {
        "source_revision_id",
        "processing_job_id",
        "processing_profile_reference",
        "adapter_reference",
        "model_config_reference",
    } <= {column.name for column in transcription_artifacts.columns}
    assert {
        "authorization_decision_id",
        "authorization_policy_reference",
        "execution_lease_id",
        "execution_lease_expires_at",
        "execution_attempt",
    } <= {
        column.name for column in transcription_jobs.columns
    }
    assert [column.name for column in transcript_segments.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "segment_id",
    ]
    job_source_fk = next(
        constraint
        for constraint in transcription_jobs.foreign_key_constraints
        if constraint.name == "fk_transcription_jobs_source_revision"
    )
    assert [column.name for column in job_source_fk.columns] == [
        "workspace_id",
        "environment_id",
        "source_object_id",
        "source_revision_id",
    ]
