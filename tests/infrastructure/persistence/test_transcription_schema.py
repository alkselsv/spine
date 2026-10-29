from pathlib import Path

from spine.infrastructure.persistence.postgresql_mappings import (
    transcript_segments,
    transcription_artifacts,
    transcription_jobs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
    assert transcription_artifacts.c.media_inspection.type.none_as_null is True
    assert transcription_artifacts.c.failure.type.none_as_null is True
    assert {
        "schema_version",
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


def test_bounded_transcription_migration_finalizes_check_constraint_names() -> None:
    source = (
        PROJECT_ROOT
        / "migrations/versions/20261211_79_bounded_transcription.py"
    ).read_text(encoding="utf-8")

    for constraint_name in (
        "ck_transcription_jobs_state",
        "ck_transcription_jobs_contract_version",
        "ck_transcription_jobs_terminal_shape",
        "ck_transcription_artifacts_bounded_shape",
    ):
        assert f'op.f("{constraint_name}")' in source
