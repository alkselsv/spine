from pathlib import Path

from spine.infrastructure.persistence.postgresql_mappings import (
    transcript_segments,
    transcript_publications,
    transcript_reviews,
    corrected_transcript_artifacts,
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
        "chunk_checkpoint",
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


def test_chunk_checkpoint_migration_fences_updates_by_execution_lease() -> None:
    source = (
        PROJECT_ROOT
        / "migrations/versions/20261212_80_transcription_chunk_checkpoints.py"
    ).read_text(encoding="utf-8")

    assert 'op.f("ck_transcription_jobs_chunk_checkpoint_shape")' in source
    assert "NEW.execution_lease_id IS DISTINCT FROM OLD.execution_lease_id" in source
    assert "GRANT UPDATE (chunk_checkpoint)" in source


def test_transcript_publication_is_tenant_scoped_and_pins_projection_lineage() -> None:
    assert [column.name for column in transcript_publications.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "publication_id",
    ]
    assert "uq_transcript_publications_artifact" in {
        index.name for index in transcript_publications.indexes
    }
    assert {
        "source_revision_id",
        "source_revision_digest",
        "transcript_artifact_id",
        "transcript_artifact_digest",
        "segments",
        "state",
        "projection_state",
    } <= {column.name for column in transcript_publications.columns}
    assert any(
        constraint.name == "fk_transcript_publications_source_revision"
        for constraint in transcript_publications.foreign_key_constraints
    )
    assert not any(
        constraint.name == "fk_transcript_publications_artifact"
        for constraint in transcript_publications.foreign_key_constraints
    )

    source = (
        PROJECT_ROOT / "migrations/versions/20261213_81_transcript_publication.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "20261213_81"' in source
    assert 'down_revision = "20261212_80"' in source
    assert 'op.f("ck_transcript_publications_state")' in source
    assert 'op.f("ck_transcript_publications_projection_state")' in source


def test_transcript_reviews_are_immutable_and_corrections_pin_their_predecessors() -> None:
    assert [column.name for column in transcript_reviews.primary_key.columns] == [
        "workspace_id",
        "environment_id",
        "review_id",
    ]
    assert {
        "input_artifact_id",
        "input_artifact_digest",
        "decision",
        "rationale",
        "actor_id",
        "actor_authority",
        "policy_reference",
        "publication_eligible",
        "correlation_id",
        "idempotency_key",
    } <= {column.name for column in transcript_reviews.columns}
    assert {
        "uq_transcript_reviews_artifact",
        "uq_transcript_reviews_command",
    } <= {constraint.name for constraint in transcript_reviews.constraints}
    assert [
        column.name for column in corrected_transcript_artifacts.primary_key.columns
    ] == ["workspace_id", "environment_id", "artifact_id"]
    assert {"original_artifact_id", "review_id", "artifact"} <= {
        column.name for column in corrected_transcript_artifacts.columns
    }

    source = (
        PROJECT_ROOT / "migrations/versions/20261214_82_transcript_review.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "20261214_82"' in source
    assert 'down_revision = "20261213_81"' in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "GRANT SELECT, INSERT" in source
    assert "REVOKE UPDATE, DELETE" in source

    corrected_publications = (
        PROJECT_ROOT
        / "migrations/versions/20261215_83_corrected_transcript_publication.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "20261215_83"' in corrected_publications
    assert 'down_revision = "20261214_82"' in corrected_publications
    assert 'op.drop_constraint(' in corrected_publications
