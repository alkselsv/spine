from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.transcript_publication import (
    ExplicitTranscriptPublicationPolicy,
    InMemoryTranscriptPublicationRepository,
    PublishTranscriptCommand,
    TranscriptAuthorizationSnapshot,
    TranscriptContextBroker,
    TranscriptContextOutcome,
    TranscriptContextRequest,
    TranscriptPublicationDeniedError,
    TranscriptPublicationService,
)
from spine.domain.artifacts import (
    AdapterSignal,
    CorrectedTranscriptArtifact,
    SpeechToTextSegment,
    TranscriptPublicationArtifact,
    TranscriptProjectionState,
    TranscriptPublicationState,
    TranscriptRecognitionQuality,
    TranscriptionArtifact,
)


WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE = UUID("10000000-0000-0000-0000-000000000002")
ENVIRONMENT = UUID("20000000-0000-0000-0000-000000000001")
SOURCE_OBJECT = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_REVISION = UUID("40000000-0000-0000-0000-000000000001")
PROCESSING_JOB = UUID("50000000-0000-0000-0000-000000000001")
APPROVAL = UUID("60000000-0000-0000-0000-000000000001")
APPROVER = UUID("70000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def artifact(
    *,
    workspace_id: UUID = WORKSPACE,
    signals: tuple[AdapterSignal, ...] = (),
    speaker: str | None = None,
) -> TranscriptionArtifact:
    return TranscriptionArtifact.create(
        workspace_id=workspace_id,
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
            SpeechToTextSegment(
                start_ms=1_000,
                end_ms=2_500,
                text="protected transcript",
                anonymous_speaker_label=speaker,
                signals=signals,
            ),
        ),
    )


def approval(transcript: TranscriptPublicationArtifact):
    from spine.domain.artifacts import TranscriptPublicationApproval

    return TranscriptPublicationApproval(
        approval_id=APPROVAL,
        workspace_id=transcript.workspace_id,
        environment_id=transcript.environment_id,
        transcript_artifact_id=transcript.artifact_id,
        transcript_artifact_digest=transcript.artifact_digest,
        policy_reference="transcript-review:v1",
        approved=True,
        approver_id=APPROVER,
        decided_at=NOW,
    )


def corrected_artifact(original: TranscriptionArtifact) -> CorrectedTranscriptArtifact:
    return CorrectedTranscriptArtifact.create(
        original=original,
        review_id=UUID("65000000-0000-0000-0000-000000000001"),
        segments=(
            SpeechToTextSegment(
                start_ms=1_000,
                end_ms=2_500,
                text="corrected transcript",
            ),
        ),
        created_at=NOW,
    )


class Authorization:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.current_revision_id = SOURCE_REVISION
        self.current_revision_digest = "a" * 64
        self.calls = 0

    async def recheck(self, **kwargs: object) -> TranscriptAuthorizationSnapshot:
        self.calls += 1
        return TranscriptAuthorizationSnapshot(
            allowed=self.allowed,
            workspace_id=kwargs["workspace_id"],
            environment_id=kwargs["environment_id"],
            source_object_id=kwargs["source_object_id"],
            current_revision_id=self.current_revision_id,
            current_revision_digest=self.current_revision_digest,
            authorization_generation=2,
            reason_code="allowed" if self.allowed else "authorization_denied",
        )


async def published_fixture(*, authorization: Authorization | None = None):
    transcript = artifact()

    class Artifacts:
        async def get_artifact(self, **kwargs: object):
            if (
                kwargs["workspace_id"] == transcript.workspace_id
                and kwargs["environment_id"] == transcript.environment_id
                and kwargs["artifact_id"] == transcript.artifact_id
            ):
                return transcript
            return None

    repository = InMemoryTranscriptPublicationRepository(Artifacts())
    source_authorization = authorization or Authorization()
    service = TranscriptPublicationService(
        repository=repository,
        policy=ExplicitTranscriptPublicationPolicy(
            policy_reference="transcript-review:v1",
            require_human_approval=True,
            allow_partial=False,
        ),
        authorization=source_authorization,
        clock=lambda: NOW,
    )
    result = await service.publish(
        PublishTranscriptCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            transcript_artifact_id=transcript.artifact_id,
            approval=approval(transcript),
        )
    )
    return transcript, repository, source_authorization, result.publication


async def published_corrected_fixture():
    original = artifact()
    corrected = corrected_artifact(original)

    class Artifacts:
        async def get_artifact(self, **kwargs: object):
            if (
                kwargs["workspace_id"] == corrected.workspace_id
                and kwargs["environment_id"] == corrected.environment_id
                and kwargs["artifact_id"] == corrected.artifact_id
            ):
                return corrected
            return None

    repository = InMemoryTranscriptPublicationRepository(Artifacts())
    authorization = Authorization()
    result = await TranscriptPublicationService(
        repository=repository,
        policy=ExplicitTranscriptPublicationPolicy(
            policy_reference="transcript-review:v1",
            require_human_approval=True,
        ),
        authorization=authorization,
        clock=lambda: NOW,
    ).publish(
        PublishTranscriptCommand(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            transcript_artifact_id=corrected.artifact_id,
            approval=approval(corrected),
        )
    )
    return corrected, repository, authorization, result.publication


@pytest.mark.asyncio
async def test_publish_creates_immutable_time_located_text_projection() -> None:
    transcript, repository, authorization, publication = await published_fixture()

    assert publication.source_revision_id == SOURCE_REVISION
    assert publication.transcript_artifact_digest == transcript.artifact_digest
    assert publication.segments[0].locator.start_ms == 1_000
    assert publication.segments[0].locator.end_ms == 2_500
    assert publication.segments[0].text == "protected transcript"
    assert authorization.calls == 1
    assert await repository.history_count() == 1


@pytest.mark.asyncio
async def test_publish_without_required_approval_fails_closed() -> None:
    transcript = artifact()

    class Artifacts:
        async def get_artifact(self, **kwargs: object):
            return transcript if kwargs["artifact_id"] == transcript.artifact_id else None

    repository = InMemoryTranscriptPublicationRepository(Artifacts())
    service = TranscriptPublicationService(
        repository=repository,
        policy=ExplicitTranscriptPublicationPolicy(
            policy_reference="transcript-review:v1",
            require_human_approval=True,
        ),
        authorization=Authorization(),
        clock=lambda: NOW,
    )

    with pytest.raises(TranscriptPublicationDeniedError, match="approval"):
        await service.publish(
            PublishTranscriptCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                transcript_artifact_id=transcript.artifact_id,
            )
        )

    assert await repository.history_count() == 0


@pytest.mark.asyncio
async def test_context_broker_returns_authorized_timestamped_segments() -> None:
    transcript, repository, authorization, publication = await published_fixture()
    broker = TranscriptContextBroker(repository=repository, authorization=authorization)

    response = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000001"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )

    assert response.outcome is TranscriptContextOutcome.ALLOWED
    assert response.segments[0].text == "protected transcript"
    assert response.segments[0].locator.transcript_artifact_id == transcript.artifact_id
    assert response.segments[0].locator.start_ms == 1_000
    assert authorization.calls == 2


@pytest.mark.asyncio
async def test_context_broker_returns_corrected_transcript_segment() -> None:
    corrected, repository, authorization, publication = (
        await published_corrected_fixture()
    )

    response = await TranscriptContextBroker(
        repository=repository,
        authorization=authorization,
    ).retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000009"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )

    assert response.outcome is TranscriptContextOutcome.ALLOWED
    assert response.segments[0].text == "corrected transcript"
    assert response.segments[0].locator.transcript_artifact_id == corrected.artifact_id


@pytest.mark.asyncio
async def test_context_broker_preserves_typed_speaker_and_weak_recognition() -> None:
    transcript = artifact(
        speaker="speaker-1",
        signals=(AdapterSignal(name="recognition_confidence", value=0.31),),
    )

    class Artifacts:
        async def get_artifact(self, **kwargs: object):
            return transcript

    repository = InMemoryTranscriptPublicationRepository(Artifacts())
    authorization = Authorization()
    publication = (await TranscriptPublicationService(
        repository=repository,
        policy=ExplicitTranscriptPublicationPolicy(
            policy_reference="transcript-review:v1",
            require_human_approval=True,
            allow_partial=False,
        ),
        authorization=authorization,
        clock=lambda: NOW,
    ).publish(PublishTranscriptCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        transcript_artifact_id=transcript.artifact_id,
        approval=approval(transcript),
    ))).publication

    response = await TranscriptContextBroker(
        repository=repository, authorization=authorization
    ).retrieve(TranscriptContextRequest(
        request_id=UUID("80000000-0000-0000-0000-000000000009"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        locator=publication.segments[0].locator,
    ))

    assert response.segments[0].anonymous_speaker_label == "speaker-1"
    assert response.segments[0].recognition_quality is TranscriptRecognitionQuality.WEAK


@pytest.mark.asyncio
async def test_context_broker_requires_exact_segment_time_range() -> None:
    _, repository, authorization, publication = await published_fixture()
    broker = TranscriptContextBroker(repository=repository, authorization=authorization)
    locator = publication.segments[0].locator.model_copy(
        update={"start_ms": 1_500, "end_ms": 2_000}
    )

    response = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000008"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=locator,
        )
    )

    assert response.outcome is TranscriptContextOutcome.UNAVAILABLE
    assert response.segments == ()


@pytest.mark.asyncio
async def test_context_broker_denied_response_never_contains_transcript_text() -> None:
    _, repository, _, publication = await published_fixture()
    denied_authorization = Authorization(allowed=False)
    broker = TranscriptContextBroker(repository=repository, authorization=denied_authorization)

    response = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000002"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )

    assert response.outcome is TranscriptContextOutcome.DENIED
    assert response.segments == ()
    assert "protected transcript" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_context_broker_hides_stale_source_and_projection_states() -> None:
    _, repository, authorization, publication = await published_fixture()
    broker = TranscriptContextBroker(repository=repository, authorization=authorization)
    authorization.current_revision_id = UUID("40000000-0000-0000-0000-000000000002")

    stale_source = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000003"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )
    assert stale_source.outcome is TranscriptContextOutcome.STALE
    assert stale_source.segments == ()

    authorization.current_revision_id = SOURCE_REVISION
    await repository.mark_projection_stale(
        publication_id=publication.publication_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
    )
    stale_projection = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000004"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )
    assert stale_projection.outcome is TranscriptContextOutcome.UNAVAILABLE
    assert stale_projection.segments == ()


@pytest.mark.asyncio
async def test_revoked_publication_is_unavailable_without_deleting_history() -> None:
    _, repository, authorization, publication = await published_fixture()
    await repository.revoke(
        publication_id=publication.publication_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
    )
    broker = TranscriptContextBroker(repository=repository, authorization=authorization)

    response = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000006"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )

    assert response.outcome is TranscriptContextOutcome.UNAVAILABLE
    assert response.segments == ()
    assert await repository.history_count() == 2


@pytest.mark.asyncio
async def test_projection_rebuild_and_rollback_keep_publication_history() -> None:
    _, repository, authorization, publication = await published_fixture()
    stale = await repository.mark_projection_stale(
        publication_id=publication.publication_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
    )
    rebuilt = await repository.rebuild_projection(
        publication_id=stale.publication_id,
        projection_version="transcript-projection:v2",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
    )
    rolled_back = await repository.rollback_projection(
        publication_id=rebuilt.publication_id,
        projection_version="transcript-projection:v1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
    )
    broker = TranscriptContextBroker(repository=repository, authorization=authorization)

    response = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000007"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )

    assert rebuilt.predecessor_publication_id != stale.publication_id
    assert rolled_back.predecessor_publication_id != rebuilt.publication_id
    assert rolled_back.projection_state is TranscriptProjectionState.CURRENT
    assert response.outcome is TranscriptContextOutcome.ALLOWED
    assert await repository.history_count() == 6
    assert any(
        item.state is TranscriptPublicationState.SUPERSEDED
        for item in await repository.history()
    )


@pytest.mark.asyncio
async def test_context_broker_isolates_workspace_scope() -> None:
    transcript, repository, _, publication = await published_fixture()
    broker = TranscriptContextBroker(repository=repository, authorization=Authorization())

    response = await broker.retrieve(
        TranscriptContextRequest(
            request_id=UUID("80000000-0000-0000-0000-000000000005"),
            workspace_id=OTHER_WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=publication.segments[0].locator,
        )
    )

    assert response.outcome is TranscriptContextOutcome.UNAVAILABLE
    assert response.segments == ()
    assert transcript.workspace_id != OTHER_WORKSPACE
