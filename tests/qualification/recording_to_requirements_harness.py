"""Reusable offline harness for the governed recording-to-requirements path."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.agents.runtime import AgentExecutionContext, AgentRequest
from spine.agents.runtime import CapabilitySchemaIdentity, TrustedExecutionIdentitySnapshot
from spine.application.diagnostics import DiagnosticContext
from spine.application.extract_requirements import (
    AuthorizedTranscriptRequirementsInputBuilder,
    deterministic_extract_requirements,
    extract_requirements_agent_version,
    extract_requirements_capability,
)
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.requirements_artifacts import (
    PersistedRequirementsArtifactService,
    RequirementsExtractionClarificationRevalidator,
)
from spine.application.transcript_inspection import (
    TranscriptEvidenceInspector,
    TranscriptInspectionResult,
)
from spine.application.transcript_publication import (
    ExplicitTranscriptPublicationPolicy,
    InMemoryTranscriptPublicationRepository,
    PublishTranscriptCommand,
    TranscriptAuthorizationSnapshot,
    TranscriptContextBroker,
    TranscriptPublicationService,
)
from spine.application.transcript_review import (
    ExplicitTranscriptReviewPolicy,
    InMemoryTranscriptReviewRepository,
    ReviewTranscriptCommand,
    TranscriptReviewPolicyOutcome,
    TranscriptReviewPublicationGateway,
    TranscriptReviewResult,
    TranscriptReviewService,
)
from spine.application.transcription import (
    AudioPreparationResult,
    AudioProcessingAuthorization,
    InMemoryTranscriptionRepository,
    SpeechToTextPort,
    SpeechToTextFailed,
    TranscribeAudioCommand,
    TranscriptionResult,
    TranscriptionService,
)
from spine.domain.artifacts import (
    AdapterSignal,
    CorrectedTranscriptArtifact,
    SpeechToTextResult,
    SpeechToTextSegment,
    TranscriptReviewDecision,
    TimeRangeLocator,
    TranscriptionArtifact,
)
from spine.domain.artifacts.transcription import (
    AudioMediaInspection,
    SpeechToTextResultV2,
    TranscriptionArtifactOutcome,
    TranscriptionJobState,
    TranscriptionProcessingProfile,
)
from spine.domain.common import ActorKind, ActorRef, ContextOrigin, EnvironmentKind
from spine.domain.sources import RevisionMetadata, SourceRevision, content_revision
from spine.domain.sources.original_reference import ObjectReference
from spine.domain.work_items import WorkItem
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    ClientRequestV1,
    Requirement,
    RequirementEvidenceRef,
    RequirementsArtifact,
    WorkflowRun,
)


def _id(value: int) -> UUID:
    return UUID(int=value)


WORKSPACE = _id(1)
ENVIRONMENT = _id(2)
WORK_ITEM = _id(3)
WORKFLOW_RUN = _id(4)
ACTOR = _id(5)
SOURCE_OBJECT = _id(6)
SOURCE_REVISION = _id(7)
PROVENANCE = _id(8)
NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


class _FakeSpeechToText:
    def __init__(self, segments: tuple[SpeechToTextSegment, ...]) -> None:
        self._segments = segments

    async def transcribe(self, request: Any) -> SpeechToTextResult:
        return SpeechToTextResult(
            adapter_reference=request.adapter_reference,
            model_config_reference=request.model_config_reference,
            detected_language="ru",
            segments=self._segments,
        )


class _ProcessingAuthorizer:
    async def authorize(self, request: Any) -> AudioProcessingAuthorization:
        return AudioProcessingAuthorization(
            allowed=True,
            decision_id=_id(9),
            policy_reference="synthetic-audio-processing:v1",
            reason_code="allowed",
        )


class _CurrentAuthorization:
    def __init__(self) -> None:
        self.allowed = True
        self.current_revision_id = SOURCE_REVISION
        self.current_revision_digest = _audio_revision().revision_digest

    async def recheck(self, **kwargs: Any) -> TranscriptAuthorizationSnapshot:
        return TranscriptAuthorizationSnapshot(
            allowed=self.allowed,
            workspace_id=kwargs["workspace_id"],
            environment_id=kwargs["environment_id"],
            source_object_id=kwargs["source_object_id"],
            current_revision_id=self.current_revision_id,
            current_revision_digest=self.current_revision_digest,
            authorization_generation=1,
            reason_code="allowed" if self.allowed else "authorization_denied",
        )


class _Cancellation:
    is_cancelled = False

    def raise_if_cancelled(self) -> None:
        return None


class _EventSink:
    async def emit(self, event: object) -> None:
        del event


class _RequirementsArtifacts:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def persist_candidate(self, artifact: RequirementsArtifact) -> RequirementsArtifact:
        current = self._reload()
        if current is None:
            self.payload = artifact.model_dump(mode="json")
            current = self._reload()
        elif current.production_digest != artifact.production_digest:
            raise ValueError("conflicting requirements replay")
        if current is None:
            raise AssertionError("requirements serialization round trip failed")
        return current

    async def get_for_run(self, **kwargs: Any) -> RequirementsArtifact | None:
        del kwargs
        return self._reload()

    async def versions(self, **kwargs: Any) -> tuple[RequirementsArtifact, ...]:
        del kwargs
        value = self._reload()
        return () if value is None else (value,)

    async def get(self, **kwargs: Any) -> RequirementsArtifact | None:
        value = self._reload()
        if value is None:
            return None
        if kwargs["artifact_id"] != value.artifact_id:
            return None
        artifact_version = kwargs.get("artifact_version")
        if artifact_version is not None and artifact_version != value.artifact_version:
            return None
        return value

    def _reload(self) -> RequirementsArtifact | None:
        if self.payload is None:
            return None
        return RequirementsArtifact.model_validate(self.payload)


class _TranscriptArtifactCatalog:
    def __init__(self, originals: InMemoryTranscriptionRepository) -> None:
        self.originals = originals
        self.reviews: InMemoryTranscriptReviewRepository | None = None

    async def get_artifact(
        self, **kwargs: Any
    ) -> TranscriptionArtifact | CorrectedTranscriptArtifact | None:
        original = await self.originals.get_artifact(**kwargs)
        if original is not None:
            return original
        if self.reviews is None:
            return None
        return await self.reviews.get_corrected_artifact(
            workspace_id=kwargs["workspace_id"],
            environment_id=kwargs["environment_id"],
            artifact_id=kwargs["artifact_id"],
        )


class _BusinessExecutionRecorder:
    def __init__(self, work_item: WorkItem, run: WorkflowRun) -> None:
        self.work_items = _WorkItems(self, work_item)
        self.workflow_runs = _WorkflowRuns(self, run)
        self.qa_sessions = _QASessions(self)
        self.reads: list[str] = []
        self.creations: list[str] = []


class _WorkflowRuns:
    def __init__(self, recorder: _BusinessExecutionRecorder, run: WorkflowRun) -> None:
        self._recorder = recorder
        self._runs = {run.run_id: run}

    async def get_run(self, **kwargs: Any) -> WorkflowRun | None:
        self._recorder.reads.append("workflow_run")
        return self._runs.get(kwargs["run_id"])

    async def all(self) -> tuple[WorkflowRun, ...]:
        return tuple(self._runs.values())

    async def start(self, run: WorkflowRun) -> None:
        self._recorder.creations.append("workflow_run")
        self._runs[run.run_id] = run

    async def wait_for_clarification(self, artifact: RequirementsArtifact) -> WorkflowRun:
        del artifact
        return next(iter(self._runs.values()))


class _WorkItems:
    def __init__(self, recorder: _BusinessExecutionRecorder, work_item: WorkItem) -> None:
        self._recorder = recorder
        self._items = {work_item.id: work_item}

    async def get(self, work_item_id: UUID) -> WorkItem | None:
        self._recorder.reads.append("work_item")
        return self._items.get(work_item_id)

    async def all(self) -> tuple[WorkItem, ...]:
        return tuple(self._items.values())

    async def create(self, work_item: WorkItem) -> None:
        self._recorder.creations.append("work_item")
        self._items[work_item.id] = work_item


class _QASessions:
    def __init__(self, recorder: _BusinessExecutionRecorder) -> None:
        self._recorder = recorder
        self._sessions: list[object] = []

    async def all(self) -> tuple[object, ...]:
        return tuple(self._sessions)

    async def create(self, session: object) -> None:
        self._recorder.creations.append("qa_session")
        self._sessions.append(session)


class _UnitOfWork:
    def __init__(self, execution: _BusinessExecutionRecorder) -> None:
        self.work_items = execution.work_items
        self.workflow_runs = execution.workflow_runs
        self.qa_sessions = execution.qa_sessions
        self.requirements_artifacts = _RequirementsArtifacts()

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _audio_revision() -> SourceRevision:
    original = ObjectReference(
        schema_version=1,
        object_id=_id(10),
        storage_generation=_id(11),
        digest_algorithm="sha256",
        digest_hex="a" * 64,
        byte_length=64_000,
    )
    return content_revision(
        revision_id=SOURCE_REVISION,
        source_object_id=SOURCE_OBJECT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        original_reference=original,
        media_type="audio/wav",
        revision_metadata=RevisionMetadata(
            embedded_title="synthetic governed meeting",
            document_language="ru",
        ),
        original_sha256=original.digest_hex,
        byte_length=original.byte_length,
        observed_at=NOW,
    )


def _reliable_segment() -> SpeechToTextSegment:
    return SpeechToTextSegment(
        start_ms=1_200,
        end_ms=3_600,
        text="SCHEDULE: Запуск до 20 декабря.",
        anonymous_speaker_label="speaker-1",
        signals=(AdapterSignal(name="recognition_confidence", value=0.96),),
    )


async def _transcribe_fixture(
    *,
    idempotency_key: str,
    segments: tuple[SpeechToTextSegment, ...] | None = None,
) -> tuple[
    InMemoryTranscriptionRepository,
    SourceRevision,
    TranscriptionResult,
    TranscriptionService,
    TranscribeAudioCommand,
]:
    repository = InMemoryTranscriptionRepository(clock=lambda: NOW)
    service = TranscriptionService(
        repository=repository,
        speech_to_text=_FakeSpeechToText(segments or (_reliable_segment(),)),
        authorizer=_ProcessingAuthorizer(),
        clock=lambda: NOW,
        lease_id_factory=lambda: _id(21),
    )
    source_revision = _audio_revision()
    command = TranscribeAudioCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision=source_revision,
        recording_duration_ms=5_000,
        processing_profile_reference="synthetic-short:v1",
        adapter_reference="deterministic-fake-asr:v1",
        model_config_reference="synthetic-russian:v1",
        language_hint="ru",
        idempotency_key=idempotency_key,
    )
    result = await service.transcribe(command)
    return repository, source_revision, result, service, command


def _bounded_profile() -> TranscriptionProcessingProfile:
    return TranscriptionProcessingProfile(
        profile_reference="synthetic-bounded:v1",
        supported_media_types=("audio/wav",),
        supported_codecs=("pcm_s16le",),
        maximum_source_bytes=2_000_000,
        maximum_duration_ms=60_000,
        maximum_decoded_bytes=8_000_000,
        maximum_memory_bytes=4_000_000,
        maximum_working_bytes=10_000_000,
        maximum_decode_seconds=5.0,
        chunk_duration_ms=4_000,
        chunk_overlap_ms=1_000,
        maximum_chunks=20,
        maximum_model_attempts=2,
        decoder_reference="deterministic-fake-decoder:v1",
        reassembly_reference="first-chunk-wins:v1",
    )


class _FakeAudioPreparation:
    async def prepare(self, request: Any) -> AudioPreparationResult:
        return AudioPreparationResult(
            inspection=AudioMediaInspection(
                inspected_media_type="audio/wav",
                container="wav",
                codec="pcm_s16le",
                byte_length=request.recording.original_reference.byte_length,
                duration_ms=10_000,
                audio_stream_count=1,
                silent=False,
            ),
            decoder_reference="deterministic-fake-decoder:v1",
            decoder_build_version="synthetic ffmpeg build",
            decoder_build_sha256="a" * 64,
            original_sha256=request.recording.original_sha256,
            decoded_reference=request.recording.original_reference.model_copy(
                update={"object_id": _id(25), "byte_length": 640_000}
            ),
            decoded_sha256=request.recording.original_sha256,
            decoded_byte_length=640_000,
            peak_memory_bytes=320_000,
            peak_working_bytes=960_000,
        )

    async def release(self, prepared: AudioPreparationResult) -> None:
        del prepared

    async def abort(self, request: object) -> None:
        del request


def _bounded_command(*, idempotency_key: str) -> TranscribeAudioCommand:
    return TranscribeAudioCommand(
        schema_version="transcribe-audio-command:v2",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_revision=_audio_revision(),
        recording_duration_ms=10_000,
        processing_profile_reference="synthetic-bounded:v1",
        processing_profile=_bounded_profile(),
        adapter_reference="deterministic-fake-asr:v1",
        model_config_reference="synthetic-russian:v1",
        language_hint="ru",
        idempotency_key=idempotency_key,
    )


def _bounded_scenario(
    *, speech_to_text: SpeechToTextPort, idempotency_key: str
) -> tuple[WorkItem, WorkflowRun, TranscriptionService, TranscribeAudioCommand]:
    work_item = _work_item()
    run = _workflow_run(work_item)
    service = TranscriptionService(
        repository=InMemoryTranscriptionRepository(clock=lambda: NOW),
        speech_to_text=speech_to_text,
        authorizer=_ProcessingAuthorizer(),
        media_preparation=_FakeAudioPreparation(),
        clock=lambda: NOW,
        lease_id_factory=lambda: _id(21),
    )
    return (
        work_item,
        run,
        service,
        _bounded_command(idempotency_key=idempotency_key),
    )


class _PartialSpeechToText:
    async def transcribe(self, request: Any) -> SpeechToTextResultV2:
        if request.chunk.boundary.ordinal == 2:
            raise SpeechToTextFailed(code="transient_unavailable", retryable=True)
        return SpeechToTextResultV2(
            schema_version="speech-to-text-result:v2",
            adapter_reference=request.adapter_reference,
            model_config_reference=request.model_config_reference,
            detected_language="ru",
            timeline="chunk",
            segments=(
                SpeechToTextSegment(
                    start_ms=100,
                    end_ms=500,
                    text="SCHEDULE: Запуск до 20 декабря.",
                ),
            ),
        )


class _PausingSpeechToText:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, request: Any) -> SpeechToTextResultV2:
        self.started.set()
        await self.release.wait()
        return SpeechToTextResultV2(
            schema_version="speech-to-text-result:v2",
            adapter_reference=request.adapter_reference,
            model_config_reference=request.model_config_reference,
            detected_language="ru",
            timeline="chunk",
            segments=(
                SpeechToTextSegment(
                    start_ms=100,
                    end_ms=500,
                    text="SCHEDULE: Запуск до 20 декабря.",
                ),
            ),
        )


def _work_item() -> WorkItem:
    return WorkItem(
        id=WORK_ITEM,
        workspace_id=WORKSPACE,
        environment=EnvironmentKind.DEVELOPMENT,
        workflow_version_id=_id(13),
        title="Synthetic proposal qualification",
        triggered_by=ActorRef(kind=ActorKind.HUMAN, id=ACTOR),
        idempotency_key="synthetic-proposal-work:v1",
    )


def _workflow_run(work_item: WorkItem) -> WorkflowRun:
    return WorkflowRun(
        run_id=WORKFLOW_RUN,
        command_id=_id(12),
        work_item_id=work_item.id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        workflow_key="proposal-generation",
        workflow_version_id=_id(13),
        workflow_version="v1",
        started_by=ActorRef(kind=ActorKind.HUMAN, id=ACTOR),
        client_request=ClientRequestV1(
            message="Подготовьте требования для КП.",
            accepted_sources=(
                AcceptedSourceRevisionRef(
                    source_object_id=SOURCE_OBJECT,
                    source_revision_id=SOURCE_REVISION,
                    provenance_id=PROVENANCE,
                ),
            ),
        ),
        trace_id=_id(14),
        correlation_id=_id(15),
        causation_id=_id(16),
        created_at=NOW,
    )


def _persistence_context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(WORKSPACE, ENVIRONMENT),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow_run_start"),
        trace_id=_id(14),
        acting_subject_id=ACTOR,
        service_principal_id=None,
        provenance=TrustedContextProvenance(_id(17), b"trusted"),
    )


def _agent_context() -> AgentExecutionContext:
    capability = extract_requirements_capability()
    return AgentExecutionContext(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        environment=EnvironmentKind.DEVELOPMENT,
        trusted_identity=TrustedExecutionIdentitySnapshot(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            acting_subject_id=ACTOR,
            authorization_generation=1,
        ),
        work_item_id=WORK_ITEM,
        workflow_run_id=WORKFLOW_RUN,
        step_run_id=_id(18),
        agent_run_id=_id(19),
        capability=capability,
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        agent_version=extract_requirements_agent_version(),
        context_profile=extract_requirements_agent_version().context_profile,
        idempotency_key="requirements:synthetic-recording:v1",
        diagnostic_context=DiagnosticContext(trace_id=_id(14)),
        input_artifact_ids=(_id(20),),
        cancellation=_Cancellation(),
        event_sink=_EventSink(),
    )


@dataclass(frozen=True, slots=True)
class RecordingToRequirementsQualificationResult:
    transcription_replay: bool
    source_revision: SourceRevision
    transcription_artifact: TranscriptionArtifact
    review_result: TranscriptReviewResult
    published_transcript_artifact: TranscriptionArtifact | CorrectedTranscriptArtifact
    work_item: WorkItem
    workflow_run: WorkflowRun
    requirements_artifact: RequirementsArtifact
    reloaded_requirements_artifact: RequirementsArtifact
    requirement: Requirement
    evidence_reference: RequirementEvidenceRef
    inspection: TranscriptInspectionResult
    work_items: tuple[WorkItem, ...]
    workflow_runs: tuple[WorkflowRun, ...]
    qa_sessions: tuple[object, ...]
    execution_creations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TerminalTranscriptionQualificationResult:
    work_item: WorkItem
    workflow_run: WorkflowRun
    transcription: TranscriptionResult
    replay: TranscriptionResult
    requirements_artifacts: tuple[RequirementsArtifact, ...] = ()


class RecordingToRequirementsQualificationHarness:
    """Compose the real public contracts with deterministic boundary fakes."""

    def __init__(self) -> None:
        self._publications: InMemoryTranscriptPublicationRepository | None = None
        self._authorization: _CurrentAuthorization | None = None
        self._context: TrustedPersistenceContext | None = None
        self._requester: ParsedContentInspectionRequester | None = None
        self._locator: TimeRangeLocator | None = None

    async def publish_without_required_approval(self) -> None:
        transcription_repository, _, result, _, _ = await _transcribe_fixture(
            idempotency_key="synthetic-recording:missing-approval:v1"
        )
        if result.artifact is None:
            raise AssertionError("completed fake transcription must create an artifact")
        self._publications = InMemoryTranscriptPublicationRepository(
            transcription_repository
        )
        await TranscriptPublicationService(
            repository=self._publications,
            policy=ExplicitTranscriptPublicationPolicy(
                policy_reference="synthetic-review:v1",
                require_human_approval=True,
                allow_partial=False,
            ),
            authorization=_CurrentAuthorization(),
            clock=lambda: NOW,
        ).publish(
            PublishTranscriptCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                transcript_artifact_id=result.artifact.artifact_id,
            )
        )

    async def publication_history_count(self) -> int:
        if self._publications is None:
            return 0
        return await self._publications.history_count()

    async def inspect_after_source_revocation(self) -> TranscriptInspectionResult:
        authorization = self._require_authorization()
        authorization.allowed = False
        return await self._inspect()

    async def inspect_after_source_change(self) -> TranscriptInspectionResult:
        authorization = self._require_authorization()
        authorization.current_revision_id = _id(23)
        authorization.current_revision_digest = "f" * 64
        return await self._inspect()

    async def inspect_from_other_workspace(self) -> TranscriptInspectionResult:
        if self._context is None or self._requester is None:
            raise AssertionError("qualification path must run before inspection")
        other_context = replace(
            self._context, scope=EnvironmentScope(_id(24), ENVIRONMENT)
        )
        return await self._inspector().inspect(
            context=other_context,
            requester=self._requester,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=self._require_locator(),
        )

    def _require_authorization(self) -> _CurrentAuthorization:
        if self._authorization is None:
            raise AssertionError("qualification path must run before inspection")
        return self._authorization

    def _require_locator(self) -> TimeRangeLocator:
        if self._locator is None:
            raise AssertionError("qualification path must run before inspection")
        return self._locator

    def _inspector(self) -> TranscriptEvidenceInspector:
        if self._publications is None:
            raise AssertionError("qualification path must run before inspection")
        return TranscriptEvidenceInspector(
            publications=self._publications,
            authorization=self._require_authorization(),
        )

    async def _inspect(self) -> TranscriptInspectionResult:
        if self._context is None or self._requester is None:
            raise AssertionError("qualification path must run before inspection")
        return await self._inspector().inspect(
            context=self._context,
            requester=self._requester,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=self._require_locator(),
        )

    async def qualify_weak_unknown_speaker(
        self,
    ) -> RecordingToRequirementsQualificationResult:
        return await self.qualify(
            segments=(
                SpeechToTextSegment(
                    start_ms=1_200,
                    end_ms=3_600,
                    text="FUNCTIONAL: Синхронизировать заказы каждый час.",
                    anonymous_speaker_label=None,
                    signals=(
                        AdapterSignal(name="background_noise", value=True),
                        AdapterSignal(name="recognition_confidence", value=0.31),
                    ),
                ),
            )
        )

    async def qualify_correction(self) -> RecordingToRequirementsQualificationResult:
        return await self.qualify(
            review_decision=TranscriptReviewDecision.CORRECT,
            corrected_segments=(
                SpeechToTextSegment(
                    start_ms=1_200,
                    end_ms=3_600,
                    text="SCHEDULE: Запуск до 15 декабря.",
                    anonymous_speaker_label="speaker-1",
                    signals=(
                        AdapterSignal(name="recognition_confidence", value=0.96),
                    ),
                ),
            ),
        )

    async def qualify_partial_failure(
        self,
    ) -> TerminalTranscriptionQualificationResult:
        work_item, run, service, command = _bounded_scenario(
            speech_to_text=_PartialSpeechToText(),
            idempotency_key="synthetic-partial:v1",
        )
        result = await service.transcribe(command)
        replay = await service.transcribe(command)
        return TerminalTranscriptionQualificationResult(
            work_item=work_item,
            workflow_run=run,
            transcription=result,
            replay=replay,
        )

    async def qualify_cancellation(
        self,
    ) -> TerminalTranscriptionQualificationResult:
        speech_to_text = _PausingSpeechToText()
        work_item, run, service, command = _bounded_scenario(
            speech_to_text=speech_to_text,
            idempotency_key="synthetic-cancelled:v1",
        )
        running = asyncio.create_task(service.transcribe(command))
        await speech_to_text.started.wait()
        job_id = uuid5(
            NAMESPACE_URL,
            "spine:transcription-job:v2:"
            f"{WORKSPACE}:{ENVIRONMENT}:{command.source_revision.revision_id}:"
            f"{command.processing_profile_reference}:{command.idempotency_key}",
        )
        await service.cancel(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            job_id=job_id,
        )
        speech_to_text.release.set()
        result = await running
        replay = await service.transcribe(command)
        return TerminalTranscriptionQualificationResult(
            work_item=work_item,
            workflow_run=run,
            transcription=result,
            replay=replay,
        )

    async def qualify(
        self,
        *,
        segments: tuple[SpeechToTextSegment, ...] | None = None,
        review_decision: TranscriptReviewDecision = TranscriptReviewDecision.APPROVE,
        corrected_segments: tuple[SpeechToTextSegment, ...] = (),
    ) -> RecordingToRequirementsQualificationResult:
        seeded_work_item = _work_item()
        seeded_run = _workflow_run(seeded_work_item)
        execution = _BusinessExecutionRecorder(seeded_work_item, seeded_run)
        work_item = await execution.work_items.get(seeded_work_item.id)
        run = await execution.workflow_runs.get_run(run_id=seeded_run.run_id)
        if work_item is None or run is None:
            raise AssertionError("seeded business execution must be available")
        (
            transcription_repository,
            source_revision,
            first,
            transcription,
            command,
        ) = await _transcribe_fixture(
            idempotency_key="synthetic-recording:v1",
            segments=segments,
        )
        replay = await transcription.transcribe(command)
        if first.artifact is None or replay.artifact is None:
            raise AssertionError("completed fake transcription must create an artifact")

        authorization = _CurrentAuthorization()
        artifact_catalog = _TranscriptArtifactCatalog(transcription_repository)
        reviews = InMemoryTranscriptReviewRepository(artifact_catalog)
        artifact_catalog.reviews = reviews
        publications = InMemoryTranscriptPublicationRepository(artifact_catalog)
        self._authorization = authorization
        self._publications = publications
        review_service = TranscriptReviewService(
            repository=reviews,
            policy=ExplicitTranscriptReviewPolicy(
                policy_reference="synthetic-review:v1",
                outcome=TranscriptReviewPolicyOutcome.REQUIRE,
            ),
            authorization=authorization,
            publication_gateway=TranscriptReviewPublicationGateway(
                repository=publications,
                authorization=authorization,
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
            identifier_factory=lambda: _id(22),
        )
        context = _persistence_context()
        requester = ParsedContentInspectionRequester(subject_id=ACTOR)
        self._context = context
        self._requester = requester
        review_result = await review_service.review(
            ReviewTranscriptCommand(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                transcript_artifact_id=first.artifact.artifact_id,
                expected_artifact_digest=first.artifact.artifact_digest,
                decision=review_decision,
                rationale="Synthetic fixture reviewed for offline qualification.",
                actor_id=ACTOR,
                actor_authority="synthetic_transcript_reviewer:v1",
                idempotency_key="synthetic-review:v1",
                correlation_id=_id(15),
                corrected_segments=corrected_segments,
            ),
            context=context,
            requester=requester,
        )
        published_transcript_artifact = (
            review_result.corrected_artifact or first.artifact
        )
        publication = await publications.get_publication(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            transcript_artifact_id=published_transcript_artifact.artifact_id,
        )
        if publication is None:
            raise AssertionError("reviewed fake transcript must be published")
        locator = publication.segments[0].locator
        self._locator = locator

        broker = TranscriptContextBroker(
            repository=publications,
            authorization=authorization,
        )
        extraction_input = await AuthorizedTranscriptRequirementsInputBuilder(
            context=broker
        ).build(
            run=run,
            environment=EnvironmentKind.DEVELOPMENT,
            locators=(locator,),
        )
        extraction = await deterministic_extract_requirements(
            AgentRequest(
                capability=extract_requirements_capability(),
                input=extraction_input,
            ),
            _agent_context(),
        )
        uow = _UnitOfWork(execution)
        requirements_service = PersistedRequirementsArtifactService(
            lambda _: uow,
            clarification_revalidator=RequirementsExtractionClarificationRevalidator(),
            clock=lambda: NOW,
        )
        requirements_artifact = await requirements_service.persist_extracted_candidate(
            context=context,
            run_id=run.run_id,
            extraction=extraction,
            upstream_artifact_ids=(published_transcript_artifact.artifact_id,),
        )
        if requirements_artifact is None:
            raise AssertionError("attributed candidate requirements artifact is required")
        reloaded_requirements_artifact = await requirements_service.get(
            context=context,
            artifact_id=requirements_artifact.artifact_id,
            artifact_version=requirements_artifact.artifact_version,
        )
        if reloaded_requirements_artifact is None:
            raise AssertionError("persisted requirements artifact must reload")
        requirement = requirements_artifact.requirements[0]
        evidence_reference = requirement.evidence_refs[0]
        if evidence_reference.transcript_locator is None:
            raise AssertionError("transcript evidence locator is required")
        inspection = await TranscriptEvidenceInspector(
            publications=publications,
            authorization=authorization,
        ).inspect(
            context=context,
            requester=requester,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            locator=evidence_reference.transcript_locator,
        )
        return RecordingToRequirementsQualificationResult(
            transcription_replay=replay.replay,
            source_revision=source_revision,
            transcription_artifact=first.artifact,
            review_result=review_result,
            published_transcript_artifact=published_transcript_artifact,
            work_item=work_item,
            workflow_run=run,
            requirements_artifact=requirements_artifact,
            reloaded_requirements_artifact=reloaded_requirements_artifact,
            requirement=requirement,
            evidence_reference=evidence_reference,
            inspection=inspection,
            work_items=await uow.work_items.all(),
            workflow_runs=await uow.workflow_runs.all(),
            qa_sessions=await uow.qa_sessions.all(),
            execution_creations=tuple(execution.creations),
        )
