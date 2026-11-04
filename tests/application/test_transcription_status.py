from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.transcription import InMemoryTranscriptionRepository
from spine.application.transcription_status import (
    TranscriptionStatus,
    TranscriptionStatusAuthorization,
    TranscriptionStatusQuery,
    TranscriptionStatusSnapshot,
)
from spine.domain.artifacts import (
    SpeechToTextSegment,
    TranscriptionArtifact,
    TranscriptionJob,
    TranscriptionJobState,
)
from spine.domain.common import ContextOrigin


WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT = UUID("20000000-0000-0000-0000-000000000001")
JOB_ID = UUID("50000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)
SUBJECT = UUID("80000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE = UUID("10000000-0000-0000-0000-000000000002")


def context(
    *,
    workspace_id: UUID = WORKSPACE,
    environment_id: UUID = ENVIRONMENT,
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_transcription_status"),
        trace_id=UUID("90000000-0000-0000-0000-000000000001"),
        acting_subject_id=SUBJECT,
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=UUID("a0000000-0000-0000-0000-000000000001"),
            signature=b"trusted",
        ),
    )


def requester() -> ParsedContentInspectionRequester:
    return ParsedContentInspectionRequester(
        subject_id=SUBJECT,
        authorization_generation=2,
    )


def pending_job() -> TranscriptionJob:
    return TranscriptionJob(
        schema_version="transcription-job:v2",
        job_id=JOB_ID,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=UUID("30000000-0000-0000-0000-000000000001"),
        source_revision_id=UUID("40000000-0000-0000-0000-000000000001"),
        source_revision_digest="a" * 64,
        original_sha256="b" * 64,
        processing_profile_reference="transcription:russian-v1",
        adapter_reference="local-asr:v1",
        model_config_reference="russian-model:v1",
        authorization_decision_id=UUID("60000000-0000-0000-0000-000000000001"),
        authorization_policy_reference="recording-processing:v1",
        idempotency_key="meeting-audio-1",
        command_digest="c" * 64,
        execution_lease_id=UUID("70000000-0000-0000-0000-000000000001"),
        execution_lease_expires_at=NOW + timedelta(minutes=5),
        execution_attempt=1,
        state=TranscriptionJobState.PENDING,
        created_at=NOW,
    )


class DenyingAuthorization:
    async def recheck(self, **kwargs: object) -> TranscriptionStatusAuthorization:
        job = kwargs["job"]
        assert isinstance(job, TranscriptionJob)
        return TranscriptionStatusAuthorization(
            allowed=False,
            authorization_generation=2,
            reason_code="source_revoked",
        )


class AllowingAuthorization:
    async def recheck(self, **kwargs: object) -> TranscriptionStatusAuthorization:
        job = kwargs["job"]
        assert isinstance(job, TranscriptionJob)
        return TranscriptionStatusAuthorization(
            allowed=True,
            authorization_generation=2,
            reason_code="allowed",
            workspace_id=job.workspace_id,
            environment_id=job.environment_id,
            job_id=job.job_id,
            source_object_id=job.source_object_id,
            source_revision_id=job.source_revision_id,
            source_revision_digest=job.source_revision_digest,
        )


class MismatchedAuthorization(AllowingAuthorization):
    async def recheck(self, **kwargs: object) -> TranscriptionStatusAuthorization:
        decision = await super().recheck(**kwargs)
        return decision.model_copy(
            update={"job_id": UUID("50000000-0000-0000-0000-000000000099")}
        )


@pytest.mark.asyncio
async def test_revoked_transcription_status_is_metadata_free() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    await repository.admit(pending_job(), now=NOW)
    query = TranscriptionStatusQuery(
        repository=repository,
        authorization=DenyingAuthorization(),
    )

    result = await query.get_status(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=JOB_ID,
    )

    assert result.status is TranscriptionStatus.UNAVAILABLE
    assert (
        result.model_dump(
            exclude={"schema_version", "status"},
            exclude_none=True,
        )
        == {}
    )


@pytest.mark.asyncio
async def test_completed_transcription_status_does_not_read_or_disclose_artifact() -> None:
    class ArtifactReadCountingRepository(InMemoryTranscriptionRepository):
        artifact_reads = 0

        async def get_artifact(self, **kwargs):
            self.artifact_reads += 1
            return await super().get_artifact(**kwargs)

    repository = ArtifactReadCountingRepository(clock=lambda: NOW)
    job = pending_job()
    admission = await repository.admit(job, now=NOW)
    artifact = TranscriptionArtifact.create(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=job.source_object_id,
        source_revision_id=job.source_revision_id,
        source_revision_digest=job.source_revision_digest,
        original_sha256=job.original_sha256,
        processing_job_id=job.job_id,
        processing_profile_reference=job.processing_profile_reference,
        adapter_reference=job.adapter_reference,
        model_config_reference=job.model_config_reference,
        recording_duration_ms=10_000,
        language="ru",
        segments=(
            SpeechToTextSegment(
                start_ms=1_000,
                end_ms=2_500,
                text="защищённый текст",
            ),
        ),
        created_at=NOW,
    )
    await repository.finish(
        job_id=job.job_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        state=TranscriptionJobState.COMPLETED,
        completed_at=NOW + timedelta(seconds=1),
        artifact=artifact,
        terminal_code="completed",
        retryable=None,
        lease_id=admission.lease_id,
    )

    result = await TranscriptionStatusQuery(
        repository=repository,
        authorization=AllowingAuthorization(),
    ).get_status(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=JOB_ID,
    )

    assert result.status is TranscriptionStatus.COMPLETED
    assert repository.artifact_reads == 0
    assert "artifact" not in result.model_dump()
    assert "защищённый текст" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_mismatched_authorization_cannot_disclose_transcription_status() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    await repository.admit(pending_job(), now=NOW)

    result = await TranscriptionStatusQuery(
        repository=repository,
        authorization=MismatchedAuthorization(),
    ).get_status(
        context=context(),
        requester=requester(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=JOB_ID,
    )

    assert result.status is TranscriptionStatus.UNAVAILABLE
    assert (
        result.model_dump(
            exclude={"schema_version", "status"},
            exclude_none=True,
        )
        == {}
    )


def test_cancelled_status_reports_terminal_processing_without_artifact_metadata() -> None:
    snapshot = TranscriptionStatusSnapshot(
        status=TranscriptionStatus.CANCELLED,
        job_id=JOB_ID,
        source_object_id=UUID("30000000-0000-0000-0000-000000000001"),
        source_revision_id=UUID("40000000-0000-0000-0000-000000000001"),
        processing_profile_reference="transcription:russian-v1",
        adapter_reference="local-asr:v1",
        model_config_reference="russian-model:v1",
        terminal_code="cancelled",
        retryable=False,
        cancellation_requested=True,
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )

    assert snapshot.status is TranscriptionStatus.CANCELLED
    assert "artifact" not in snapshot.model_dump()


@pytest.mark.asyncio
async def test_transcription_status_is_isolated_between_two_workspaces() -> None:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    await repository.admit(pending_job(), now=NOW)

    result = await TranscriptionStatusQuery(
        repository=repository,
        authorization=AllowingAuthorization(),
    ).get_status(
        context=context(workspace_id=OTHER_WORKSPACE),
        requester=requester(),
        workspace_id=OTHER_WORKSPACE,
        environment_id=ENVIRONMENT,
        job_id=JOB_ID,
    )

    assert result.status is TranscriptionStatus.UNAVAILABLE
    assert (
        result.model_dump(
            exclude={"schema_version", "status"},
            exclude_none=True,
        )
        == {}
    )
