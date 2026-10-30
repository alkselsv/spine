"""Executable offline acceptance evidence for issue #684."""

from __future__ import annotations

import pytest

from spine.application.transcript_inspection import TranscriptInspectionStatus
from spine.application.transcript_publication import TranscriptPublicationDeniedError
from spine.domain.artifacts.transcription import (
    TranscriptionArtifactOutcome,
    TranscriptionJobState,
)
from spine.domain.workflows import RequirementEvidenceStatus, RequirementState
from tests.qualification.recording_to_requirements_harness import (
    RecordingToRequirementsQualificationHarness,
)


@pytest.mark.asyncio
async def test_synthetic_recording_preserves_exact_requirement_evidence_lineage() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify()

    locator = result.evidence_reference.transcript_locator
    assert locator is not None
    assert result.transcription_artifact.source_revision_id == result.source_revision.revision_id
    assert result.transcription_artifact.source_revision_digest == result.source_revision.revision_digest
    assert locator.transcript_artifact_id == result.transcription_artifact.artifact_id
    assert locator.transcript_artifact_digest == result.transcription_artifact.artifact_digest
    assert result.requirement.evidence_refs == (result.evidence_reference,)
    assert result.inspection.locator == locator
    assert result.inspection.text == "SCHEDULE: Запуск до 20 декабря."


@pytest.mark.asyncio
async def test_recording_path_reuses_one_existing_business_run() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify()

    assert result.workflow_run.work_item_id == result.work_item.id
    assert result.requirements_artifact.work_item_id == result.work_item.id
    assert result.requirements_artifact.run_id == result.workflow_run.run_id
    assert result.work_items == (result.work_item,)
    assert result.workflow_runs == (result.workflow_run,)
    assert result.qa_sessions == ()
    assert result.execution_creations == ()


@pytest.mark.asyncio
async def test_exact_transcription_retry_reuses_one_artifact() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify()

    assert result.transcription_replay is True


@pytest.mark.asyncio
async def test_requirements_reload_reconstructs_exact_lineage() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify()

    assert result.reloaded_requirements_artifact == result.requirements_artifact
    assert result.reloaded_requirements_artifact is not result.requirements_artifact


@pytest.mark.asyncio
async def test_offline_path_uses_only_the_deterministic_fake_asr() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify()

    assert result.transcription_artifact.adapter_reference == "deterministic-fake-asr:v1"
    assert result.transcription_artifact.model_config_reference == "synthetic-russian:v1"


@pytest.mark.asyncio
async def test_required_synthetic_review_approval_fails_closed_when_absent() -> None:
    harness = RecordingToRequirementsQualificationHarness()

    with pytest.raises(TranscriptPublicationDeniedError, match="approval_required"):
        await harness.publish_without_required_approval()

    assert await harness.publication_history_count() == 0


@pytest.mark.asyncio
async def test_source_revocation_hides_requirement_evidence() -> None:
    harness = RecordingToRequirementsQualificationHarness()
    await harness.qualify()

    inspection = await harness.inspect_after_source_revocation()

    assert inspection.status is TranscriptInspectionStatus.PERMISSION_DENIED
    assert inspection.text is None
    assert inspection.locator is None


@pytest.mark.asyncio
async def test_source_change_makes_requirement_evidence_stale() -> None:
    harness = RecordingToRequirementsQualificationHarness()
    await harness.qualify()

    inspection = await harness.inspect_after_source_change()

    assert inspection.status is TranscriptInspectionStatus.STALE
    assert inspection.text is None
    assert inspection.locator is None


@pytest.mark.asyncio
async def test_other_workspace_cannot_inspect_requirement_evidence() -> None:
    harness = RecordingToRequirementsQualificationHarness()
    await harness.qualify()

    inspection = await harness.inspect_from_other_workspace()

    assert inspection.status is TranscriptInspectionStatus.PERMISSION_DENIED
    assert inspection.text is None
    assert inspection.locator is None


@pytest.mark.asyncio
async def test_weak_noisy_unknown_speaker_stays_an_attributed_partial_candidate() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify_weak_unknown_speaker()

    assert result.requirement.state is RequirementState.PARTIAL
    assert result.requirement.evidence_status is RequirementEvidenceStatus.REFERENCED
    assert result.requirement.evidence_refs == (result.evidence_reference,)
    assert "recognition" in (result.requirement.uncertainty or "").lower()
    assert "speaker" in (result.requirement.uncertainty or "").lower()
    assert result.requirements_artifact.clarifications


@pytest.mark.asyncio
async def test_correction_preserves_original_and_moves_new_evidence_to_successor() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify_correction()

    corrected = result.review_result.corrected_artifact
    assert corrected is not None
    assert corrected.original_artifact_id == result.transcription_artifact.artifact_id
    assert result.transcription_artifact.segments[0].text == (
        "SCHEDULE: Запуск до 20 декабря."
    )
    assert corrected.segments[0].text == "SCHEDULE: Запуск до 15 декабря."
    assert result.evidence_reference.transcript_locator is not None
    assert (
        result.evidence_reference.transcript_locator.transcript_artifact_id
        == corrected.artifact_id
    )
    assert result.requirements_artifact.upstream_artifact_ids == (
        corrected.artifact_id,
    )
    assert result.inspection.text == "SCHEDULE: Запуск до 15 декабря."


@pytest.mark.asyncio
async def test_partial_failure_replays_exact_source_lineage_without_requirements() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify_partial_failure()

    artifact = result.transcription.artifact
    assert result.transcription.job.state is TranscriptionJobState.PARTIAL
    assert artifact is not None
    assert artifact.outcome is TranscriptionArtifactOutcome.PARTIAL
    accepted_source = result.workflow_run.client_request.accepted_sources[0]
    assert artifact.source_revision_id == accepted_source.source_revision_id
    assert result.replay.artifact == artifact
    assert result.requirements_artifacts == ()
    assert result.workflow_run.work_item_id == result.work_item.id


@pytest.mark.asyncio
async def test_cancellation_replays_exact_source_lineage_without_requirements() -> None:
    result = await RecordingToRequirementsQualificationHarness().qualify_cancellation()

    artifact = result.transcription.artifact
    assert result.transcription.job.state is TranscriptionJobState.CANCELLED
    assert artifact is not None
    assert artifact.outcome is TranscriptionArtifactOutcome.PARTIAL
    accepted_source = result.workflow_run.client_request.accepted_sources[0]
    assert artifact.source_revision_id == accepted_source.source_revision_id
    assert result.replay.artifact == artifact
    assert result.requirements_artifacts == ()
    assert result.workflow_run.work_item_id == result.work_item.id
