from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.persistence.context import (
    ContextOrigin, EnvironmentScope, PersistenceOperation, PersistencePurpose,
    TrustedContextProvenance, TrustedPersistenceContext,
)
from spine.application.private_asr_corpus import (
    AdmittedSourceRevision, ContentVerificationResult, CorpusAdmissionDenied,
    PrivateReportReceipt, PurposeAdmissionRequest, PrivacySanitizationError,
    VerifiedAuthority,
)
from spine.application.private_asr_qualification import (
    ApprovedPrivateAnnotation, PrivateEvaluationUnavailable,
    PrivateRealASREvaluationRunner, SpeechToTextCandidateRuntime,
    reassemble_private_asr_chunk_segments,
)
from spine.application.transcription import AudioPreparationResult
from spine.domain.artifacts.transcription import (
    AudioChunkBoundary, AudioMediaInspection, AudioRecordingHandle, SpeechToTextRequestV2,
    SpeechToTextResult, SpeechToTextResultV2, SpeechToTextSegment,
    TranscriptionProcessingProfile, audio_chunk_id,
)
from spine.domain.evaluations.private_asr_corpus import (
    AnnotationReview, AnnotationStatus, AudioDigestVerification, ClusterLineage,
    CorpusSplit, GroundTruthAnnotation, MediaMetadata, PrivateCorpusRecord,
    PrivateReference, SourceRevisionBinding, TranscriptSegment, build_private_manifest,
)
from spine.domain.evaluations.private_asr_qualification import PrivateCandidateConfiguration
from spine.domain.sources import (
    IdentityMode, RevisionMetadata, SourceKind, SourceObject, content_revision,
)
from spine.domain.sources.original_reference import ObjectReference


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
WORKSPACE, ENVIRONMENT, ACTOR = UUID(int=1), UUID(int=2), UUID(int=3)
RAW = b"synthetic private audio fixture"
DIGEST = sha256(RAW).hexdigest()


def _id(value: int) -> UUID:
    return UUID(int=value)


def ref(value: int, kind: str = "private") -> PrivateReference:
    return PrivateReference(reference_id=_id(value), reference_kind=kind)


def trusted_context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("asr_model_evaluation"),
        operation=PersistenceOperation("run_private_asr_evaluation"),
        trace_id=_id(50), acting_subject_id=ACTOR, service_principal_id=None,
        provenance=TrustedContextProvenance(issuer_id=_id(51), signature=b"synthetic-proof"),
    )


def corpus(
    *, recording_duration_ms: int = 1_000,
) -> tuple[object, PrivateCorpusRecord, SourceObject, object]:
    source_id, revision_id = _id(100), _id(101)
    original = ObjectReference(
        schema_version=1, object_id=_id(102), storage_generation=_id(103),
        digest_algorithm="sha256", digest_hex=DIGEST, byte_length=len(RAW),
    )
    source = SourceObject(
        source_object_id=source_id, workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        source_kind=SourceKind.RECORD.value, identity_mode=IdentityMode.UPLOAD,
        upload_identity="synthetic-asr-fixture", created_at=NOW,
    )
    revision = content_revision(
        revision_id=revision_id, source_object_id=source_id,
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT, kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        original_reference=original, media_type="audio/wav",
        revision_metadata=RevisionMetadata(document_language="ru"),
        original_sha256=DIGEST, byte_length=len(RAW), observed_at=NOW,
    )
    record = PrivateCorpusRecord(
        record_id=_id(110), source_binding=SourceRevisionBinding(
            source_object_id=source_id, source_revision_id=revision_id,
            source_revision_number=1, source_revision_digest=revision.revision_digest,
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
            media_type="audio/wav", byte_length=len(RAW),
        ), workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        admission={
            "purpose": "asr_model_evaluation", "purpose_approval_ref": ref(120),
            "legal_data_evidence_ref": ref(121), "attestation_ref": ref(122),
            "source_object_id": source_id, "source_revision_id": revision_id,
            "source_revision_digest": revision.revision_digest,
            "workspace_id": WORKSPACE, "actor_id": ACTOR,
            "read_grant_ref": ref(123), "process_grant_ref": ref(124),
            "access_policy_version": _id(125), "policy_version": "policy:v1",
            "retention_profile_ref": ref(126), "audit_receipt_ref": ref(127),
            "allowed_environment_id": ENVIRONMENT, "admitted_at": NOW,
            "expires_at": NOW + timedelta(hours=1), "revocation_epoch": 0,
        }, audio_verification=None,
        media=MediaMetadata(
            media_type="audio/wav", container="wav", codec="pcm_s16le",
            byte_length=len(RAW), duration_ms=recording_duration_ms, sample_rate_hz=16000,
            channels=1, audio_stream_count=1,
        ), original_storage_ref=ref(128, "original"),
        derived_staging_profile="stage:v1", annotation_ref=ref(129, "annotation"),
        annotation_sha256=sha256(b"synthetic annotation").hexdigest(),
        split=CorpusSplit.QUALIFICATION, split_assignment_version="split:v1",
        lineage=ClusterLineage(
            meeting_cluster=_id(130), source_lineage_cluster=_id(131),
            customer_project_cluster=_id(132), speaker_clusters=(_id(133),),
        ), coverage={
            "expected_annotation_spans": 1, "missing_label_count": 0,
            "unalignable_segment_count": 0,
        },
    )
    manifest = build_private_manifest(
        dataset_id=_id(140), version="dataset:v1", created_at=NOW,
        purpose="asr_model_evaluation", purpose_approval_ref=ref(120),
        policy_version="policy:v1", protocol_decision_ref=ref(141, "protocol_decision"),
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        storage_profile_id="private-storage:v1", retention_profile_ref=ref(126),
        evaluator_schema="asr-candidate-qualification:private-real:v1",
        evaluator_version="private_asr_evaluator:v1",
        normalizer_version="private-russian-casefold-whitespace:v1", split_method_version="split:v1",
        lineage_key_version="lineage:v1", records=(record,),
    )
    return manifest, record, source, revision


class ContextVerifier:
    def verify(self, context):
        return context


class FakeAdmission:
    def __init__(self, *, fail_at: str | None = None, protocol_decision_verified: bool = True):
        self.fail_at = fail_at
        self.protocol_decision_verified = protocol_decision_verified
        self.events: list[str] = []
        self.annotation_reads = 0
        self.release_checks = 0
        self.annotation_text = "синтетика"
        self.release_error: BaseException | None = None

    def _check(self, name: str) -> bool:
        self.events.append(name)
        return self.fail_at != name

    async def purpose_request(self, manifest, record, *, context):
        self.events.append("resolve_opaque_record")
        return PurposeAdmissionRequest(
            source_object=SOURCE, source_revision=REVISION, workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT, purpose_approval_ref=manifest.purpose_approval_ref,
            legal_data_evidence_ref=record.admission.legal_data_evidence_ref,
            retention_profile_ref=manifest.retention_profile_ref,
            authorized_actor_id=ACTOR, access_policy_version=record.admission.access_policy_version,
            policy_version=manifest.policy_version, allowed_environment_id=ENVIRONMENT,
        )

    async def verify_evaluation_purpose(self, request, *, context):
        return self._check("purpose")

    async def verify_legal_data_evidence(self, request, *, context):
        return self._check("legal_evidence")

    async def verify_exact_source_revision(self, request, *, context):
        return self._check("source_revision")

    async def verify_current_access_policy(self, request, *, context):
        return self._check("access_policy")

    async def verify_current_read_grant(self, request, *, context):
        return self._check("read_grant")

    async def verify_current_process_grant(self, request, *, context):
        return self._check("process_grant")

    async def verify_retention_policy(self, request, *, context):
        return self._check("retention")

    async def verify_admission_audit_receipt(self, request, *, context):
        return self._check("audit")

    async def verify_approved_environment(self, request, *, context):
        return self._check("environment")

    async def verify_dataset_and_split(self, manifest, record, *, requested_split, context):
        return requested_split is record.split and self._check("dataset_split")

    async def verify_host_no_egress(self, manifest, *, context):
        return self._check("host_no_egress")

    async def verify_protocol_owner_decision(self, manifest, *, context):
        self.events.append("protocol_decision")
        return self.protocol_decision_verified

    async def verify_complementary_disclosure_risk(self, report, sanitized_report, *, context):
        return self._check("complementary_risk")

    async def load_approved_annotation(self, manifest, record, *, admitted, context):
        self.annotation_reads += 1
        self.events.append("annotation_read")
        annotation = GroundTruthAnnotation(
            annotation_id=_id(150), version=1, source_record_id=record.record_id,
            status=AnnotationStatus.APPROVED, authored_by=_id(151),
            author_authorization_ref=ref(152), authored_at=NOW,
            segments=(TranscriptSegment(
                start_ms=0, end_ms=800, human_text=self.annotation_text,
            ),),
            reviews=(AnnotationReview(
                annotation_version=1, reviewer_id=_id(153),
                authorization_ref=ref(154), status="approved", reviewed_at=NOW,
            ),), independently_human_validated=True,
        )
        return ApprovedPrivateAnnotation(
            annotation=annotation, annotation_ref=record.annotation_ref,
            annotation_sha256=record.annotation_sha256, audit_receipt_ref=ref(155, "audit"),
        )

    async def audio_handle(self, record, verified, *, context):
        self.events.append("audio_handle")
        return AudioRecordingHandle(
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
            source_object_id=SOURCE.source_object_id,
            source_revision_id=REVISION.revision_id,
            source_revision_digest=REVISION.revision_digest,
            original_sha256=DIGEST, original_reference=REVISION.original_reference,
            media_type="audio/wav", recording_duration_ms=record.media.duration_ms,
        )

    async def current_release_authorized(self, report, *, context):
        self.release_checks += 1
        if self.release_error is not None:
            raise self.release_error
        return self._check("release_authorized")


class FakeAudioPreparation:
    def __init__(self, admission):
        self.admission = admission
        self.calls = 0
        self.releases = 0
        self.aborts = 0
        self.fail = False
        self.fail_release = False

    async def prepare(self, recording, content, profile, *, context):
        self.calls += 1
        self.admission.events.append("audio_prepare")
        if self.fail:
            raise RuntimeError("private staging locator")
        decoded = ObjectReference(
            schema_version=1, object_id=_id(190), storage_generation=_id(191),
            digest_algorithm="sha256", digest_hex=sha256(b"synthetic decoded audio").hexdigest(),
            byte_length=23,
        )
        return AudioPreparationResult(
            inspection=AudioMediaInspection(
                inspected_media_type="audio/wav", container="wav", codec="pcm_s16le",
                byte_length=len(RAW), duration_ms=recording.recording_duration_ms,
                audio_stream_count=1, silent=False,
            ), decoder_reference=profile.decoder_reference,
            decoded_reference=decoded, decoded_sha256=decoded.digest_hex,
            decoded_byte_length=decoded.byte_length, peak_memory_bytes=128, peak_working_bytes=256,
        )

    async def release(self, prepared, *, context):
        self.releases += 1
        self.admission.events.append("audio_release")
        if self.fail_release:
            raise RuntimeError("private staging cleanup detail")

    async def abort(self, recording, content, profile, *, context):
        self.aborts += 1
        self.admission.events.append("audio_abort")


class FakeSourceAdmission:
    def __init__(
        self, *, revoked_after_inference: bool = False, deny_admission: bool = False,
        revoke_at_recheck: int | None = None,
    ):
        self.admissions = 0
        self.content_verifications = 0
        self.rechecks = 0
        self.revoked_after_inference = revoked_after_inference
        self.deny_admission = deny_admission
        self.revoke_at_recheck = revoke_at_recheck
        self.revoke_after_next_recheck = False
        self.runtime_started = False
        self.previously_verified_hashes: list[str | None] = []
        self.events: list[str] = []

    async def admit(self, request, *, context):
        self.admissions += 1
        self.events.append("source_stage_one")
        if self.deny_admission:
            raise CorpusAdmissionDenied("current purpose admission denied")
        authority = VerifiedAuthority(
            purpose="asr_model_evaluation", process_granted=True,
            human_read_granted=True, legal_data_evidence_confirmed=True,
            purpose_approved=True, actor_authorized=True, retention_current=True,
            audit_permitted=True, environment_allowed=True, current=True,
            revoked=False, expires_at=NOW + timedelta(hours=1), revocation_epoch=0,
            attestation_ref=ref(160), read_grant_ref=ref(161), process_grant_ref=ref(162),
        )
        return AdmittedSourceRevision(
            source_object=SOURCE, source_revision=REVISION, source_revision_number=1,
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
            purpose_approval_ref=request.purpose_approval_ref,
            legal_data_evidence_ref=request.legal_data_evidence_ref,
            retention_profile_ref=request.retention_profile_ref, actor_id=ACTOR,
            audit_receipt_ref=ref(163), access_policy_version=request.access_policy_version,
            policy_version=request.policy_version, authority=authority, admitted_at=NOW,
        )

    async def verify_content(self, admitted, *, context, previously_verified_sha256=None):
        self.content_verifications += 1
        self.events.append("content_verify")
        self.previously_verified_hashes.append(previously_verified_sha256)
        self.runtime_started = True
        verification = AudioDigestVerification(
            audio_sha256=DIGEST, source_revision_id=REVISION.revision_id,
            byte_length=len(RAW), verified_at=NOW, audit_receipt_ref=ref(164),
        )
        return ContentVerificationResult(
            admitted=admitted, digest_verification=verification, byte_length=len(RAW),
            staged_ref=ref(165, "stage"), original_ref=ref(166, "original"),
        )

    async def recheck_current(self, admitted, *, context):
        self.rechecks += 1
        if self.revoke_at_recheck is not None and self.rechecks >= self.revoke_at_recheck:
            return False
        if self.revoke_after_next_recheck:
            self.revoke_after_next_recheck = False
            self.revoked_after_inference = True
            return True
        return not (self.revoked_after_inference and self.rechecks >= 6)


class FakeRuntime:
    def __init__(self, admission: FakeAdmission):
        self.admission = admission
        self.calls = 0
        self.fail_with_content = False
        self.wrong_identity = False
        self.invalid_timestamps = False
        self.out_of_order = False
        self.cancel = False
        self.legacy_result = False
        self.detected_language = None
        self.requests = []
        self.results = []
        self.segments_by_ordinal = {}
        self.missing_chunk_ordinals = set()
        self.after_result = None
        self.wrong_model_config = False

    async def transcribe(self, request):
        self.calls += 1
        self.requests.append(request)
        self.admission.events.append("inference")
        assert isinstance(request, SpeechToTextRequestV2)
        assert request.language_hint == "ru"
        assert request.recording.workspace_id == WORKSPACE
        assert request.recording.environment_id == ENVIRONMENT
        assert request.recording.source_revision_id == _id(101)
        assert request.recording.source_revision_digest == REVISION.revision_digest
        assert request.chunk.boundary.ordinal > 0
        assert request.chunk.boundary.end_ms - request.chunk.boundary.start_ms <= 25_000
        assert request.chunk.decoded_sha256 == request.chunk.decoded_reference.digest_hex
        if self.cancel:
            raise asyncio.CancelledError
        if self.fail_with_content:
            raise RuntimeError("synthetic private transcript must not escape")
        ordinal = request.chunk.boundary.ordinal
        if ordinal in self.missing_chunk_ordinals:
            return None
        segments = self.segments_by_ordinal.get(
            ordinal,
            (SpeechToTextSegment(start_ms=0, end_ms=500, text="синтетика"),),
        )
        if self.invalid_timestamps:
            segments = (SpeechToTextSegment(start_ms=0, end_ms=1500, text="синтетика"),)
        if self.out_of_order:
            segments = (
                SpeechToTextSegment(start_ms=400, end_ms=500, text="второе"),
                SpeechToTextSegment(start_ms=0, end_ms=100, text="первое"),
            )
        if self.legacy_result:
            result = SpeechToTextResult(
                adapter_reference=request.adapter_reference,
                model_config_reference=request.model_config_reference,
                segments=segments,
            )
        else:
            result = SpeechToTextResultV2(
                adapter_reference=(
                    "other-adapter:v1" if self.wrong_identity else request.adapter_reference
                ),
                model_config_reference=(
                    "other-model-config:v1" if self.wrong_model_config
                    else request.model_config_reference
                ),
                timeline="chunk",
                detected_language=self.detected_language,
                segments=segments,
            )
        self.results.append(result)
        if self.after_result is not None:
            self.after_result(request)
        return result


class FakeRuntimeFactory:
    def __init__(self, admission):
        self.admission = admission
        self.creates = 0
        self.runtime = FakeRuntime(admission)
        self.fail_with_content = False

    def create(self, *, candidate_id, configuration):
        self.creates += 1
        self.admission.events.append("runtime_init")
        if self.fail_with_content:
            raise RuntimeError("private model asset locator")
        return SpeechToTextCandidateRuntime(self.runtime)


class FakeReports:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0
        self.payloads = []
        self.receipts = []
        self.events = []
        self.before_commit = None

    async def store_private_report(
        self, payload, *, retention_profile_ref, audit_receipt_ref, authorize_commit,
    ):
        self.calls += 1
        self.events.append("prepare_write")
        if self.before_commit is not None:
            await self.before_commit()
        self.events.append("authorize_commit")
        await authorize_commit()
        if self.fail:
            raise OSError("synthetic storage failure")
        self.payloads.append(payload)
        self.events.append("atomic_visibility")
        receipt = PrivateReportReceipt(
            report_ref=ref(170, "report"), manifest_ref=ref(171, "manifest"),
            audit_receipt_ref=ref(172, "audit"), retention_profile_ref=retention_profile_ref,
        )
        self.receipts.append(receipt)
        return receipt


class FakeAudit:
    async def create_report_receipt(self, *, workspace_id, environment_id, report_id, retention_profile_ref, context):
        return ref(173, "audit")


class FakeAggregateBuilder:
    async def build(self, **kwargs):
        return FakeSanitizedReport()


class FakeSanitizedReport:
    def model_dump_json(self, **kwargs):
        return '{"schema_version":"synthetic-sanitized:v1","status":"NOT_VERIFIED"}'


class FakeAggregateExport:
    def __init__(self): self.calls = 0
    async def export_sanitized(self, payload, *, context): self.calls += 1


class FakeClock:
    def now(self): return NOW


def runner(
    admission, source_admission, *, reports=None, candidate_id="synthetic_candidate",
    configuration_candidate_id="synthetic_candidate",
    adapter_reference="fake_adapter:v1", model_config_reference="fake_config:v1",
    processing_profile=None,
):
    source_admission.events = admission.events
    factory = FakeRuntimeFactory(admission)
    audio_preparation = FakeAudioPreparation(admission)
    factory.audio_preparation = audio_preparation
    storage = FakeReports() if reports is None else reports
    aggregate_export = FakeAggregateExport()
    profile = processing_profile or TranscriptionProcessingProfile(
        profile_reference="fake_processing:v1", supported_media_types=("audio/wav",),
        supported_codecs=("pcm_s16le",), maximum_source_bytes=1000,
        maximum_duration_ms=10_000, maximum_decoded_bytes=1000,
        maximum_memory_bytes=1000, maximum_working_bytes=1000,
        maximum_decode_seconds=2.0, chunk_duration_ms=1000, chunk_overlap_ms=0,
        maximum_chunks=10, maximum_model_attempts=1,
        decoder_reference="fake_decoder:v1", reassembly_reference="fake_reassembly:v1",
    )
    subject = PrivateRealASREvaluationRunner(
        contexts=ContextVerifier(), admission=admission,
        source_admission=source_admission, runtime_factory=factory,
        audio_preparation=audio_preparation,
        processing_profile=profile,
        reports=storage, report_audit=FakeAudit(),
        aggregate_builder=FakeAggregateBuilder(), aggregate_export=aggregate_export,
        clock=FakeClock().now, candidate_id=candidate_id,
        configuration=PrivateCandidateConfiguration(
            candidate_id=configuration_candidate_id, adapter_reference=adapter_reference,
            model_config_reference=model_config_reference,
            processing_profile_reference=profile.profile_reference,
            configuration_digest="f" * 64,
        ),
        report_id_factory=lambda: _id(180),
    )
    return subject, factory, storage, aggregate_export


def multi_chunk_profile(**overrides) -> TranscriptionProcessingProfile:
    profile = {
        "profile_reference": "fake_processing:multi:v1",
        "supported_media_types": ("audio/wav",), "supported_codecs": ("pcm_s16le",),
        "maximum_source_bytes": 1000, "maximum_duration_ms": 30_000,
        "maximum_decoded_bytes": 1000, "maximum_memory_bytes": 1000,
        "maximum_working_bytes": 1000, "maximum_decode_seconds": 2.0,
        "chunk_duration_ms": 10_000, "chunk_overlap_ms": 2_000,
        "maximum_chunks": 5, "maximum_model_attempts": 1,
        "decoder_reference": "fake_decoder:v1", "reassembly_reference": "fake_reassembly:v1",
    }
    profile.update(overrides)
    return TranscriptionProcessingProfile(**profile)


@pytest.fixture(autouse=True)
def frozen_source_globals(monkeypatch):
    # Avoid any filesystem or external source resolver: the port returns these
    # in-memory synthetic SourceObject/SourceRevision contracts only.
    global SOURCE, REVISION
    _, _, source, revision = corpus()
    SOURCE, REVISION = source, revision
    yield


@pytest.mark.asyncio
async def test_private_runner_uses_ordered_fake_flow_and_never_qualifies_candidate():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)

    report, receipt = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.schema_version == "asr-candidate-qualification:private-real:v1"
    assert report.normalizer_version == "private-russian-casefold-whitespace:v1"
    assert report.status.value == "NOT_VERIFIED"
    assert report.sufficiency.value == "NOT_VERIFIED"
    assert report.denominators.eligible == report.denominators.admitted == 1
    assert report.denominators.verified == report.denominators.annotated == 1
    assert report.denominators.measured == 1
    assert receipt.report_ref.reference_id == _id(170)
    assert factory.creates == factory.runtime.calls == reports.calls == 1
    assert factory.audio_preparation.calls == factory.audio_preparation.releases == 1
    assert "inference" in admission.events
    assert admission.events.index("purpose") < admission.events.index("legal_evidence")
    assert admission.events.index("legal_evidence") < admission.events.index("source_revision")
    assert admission.events.index("source_revision") < admission.events.index("access_policy")
    assert admission.events.index("access_policy") < admission.events.index("read_grant")
    assert admission.events.index("read_grant") < admission.events.index("process_grant")
    assert admission.events.index("process_grant") < admission.events.index("retention")
    assert admission.events.index("retention") < admission.events.index("audit")
    assert admission.events.index("audit") < admission.events.index("environment")
    assert admission.events.index("environment") < admission.events.index("dataset_split")
    assert admission.events.index("dataset_split") < admission.events.index("host_no_egress")
    assert admission.events.index("protocol_decision") < admission.events.index("host_no_egress")
    assert admission.events.index("host_no_egress") < admission.events.index("source_stage_one")
    assert admission.events.index("source_stage_one") < admission.events.index("content_verify")
    assert admission.events.index("content_verify") < admission.events.index("annotation_read")
    assert admission.events.index("annotation_read") < admission.events.index("audio_prepare")
    assert admission.events.index("audio_prepare") < admission.events.index("runtime_init")
    assert admission.events.index("runtime_init") < admission.events.index("inference")
    assert source_admission.content_verifications == 1
    assert source_admission.previously_verified_hashes == [None]
    assert "синтетика" not in reports.payloads[0].decode()


@pytest.mark.parametrize(
    ("candidate_id", "adapter_reference", "model_config_reference"),
    [
        ("gigaam-v3-rnnt", "gigaam-v3-rnnt-adapter:v1", "gigaam-v3-rnnt-frozen:v1"),
        (
            "whisper-large-v3-faster-whisper",
            "whisper-large-v3-adapter:v1",
            "whisper-large-v3-frozen:v1",
        ),
    ],
)
@pytest.mark.asyncio
async def test_governed_v2_bridge_binds_each_candidate_identity(
    candidate_id, adapter_reference, model_config_reference,
):
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, _, _ = runner(
        admission, source_admission,
        candidate_id=candidate_id, configuration_candidate_id=candidate_id,
        adapter_reference=adapter_reference, model_config_reference=model_config_reference,
    )

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    request = factory.runtime.requests[0]
    assert isinstance(request, SpeechToTextRequestV2)
    assert request.adapter_reference == adapter_reference
    assert request.model_config_reference == model_config_reference
    assert request.language_hint == "ru"
    assert request.chunk.boundary.ordinal == 1
    assert report.status.value == "NOT_VERIFIED"


@pytest.mark.asyncio
async def test_private_bridge_reassembles_three_overlapping_chunks_on_recording_timeline():
    profile = multi_chunk_profile()
    manifest, _, _, _ = corpus(recording_duration_ms=26_000)
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    admission.annotation_text = "начало граница конец"
    subject, factory, reports, aggregate_export = runner(
        admission, source_admission, processing_profile=profile,
    )
    factory.runtime.segments_by_ordinal = {
        1: (SpeechToTextSegment(start_ms=9_000, end_ms=9_900, text="начало"),),
        2: (
            SpeechToTextSegment(start_ms=0, end_ms=1_000, text="полный-дубликат"),
            SpeechToTextSegment(start_ms=1_500, end_ms=2_500, text="граница"),
        ),
        3: (
            SpeechToTextSegment(start_ms=0, end_ms=1_000, text="полный-дубликат"),
            SpeechToTextSegment(start_ms=1_500, end_ms=2_500, text="конец"),
        ),
    }

    report, receipt = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    requests = factory.runtime.requests
    planned_boundaries = profile.plan_chunks(AudioMediaInspection(
        inspected_media_type="audio/wav", container="wav", codec="pcm_s16le",
        byte_length=len(RAW), duration_ms=26_000, audio_stream_count=1, silent=False,
    ))
    assert tuple(item.chunk.boundary for item in requests) == planned_boundaries
    assert [request.chunk.boundary.ordinal for request in requests] == [1, 2, 3]
    assert [
        (item.chunk.boundary.start_ms, item.chunk.boundary.end_ms,
         item.chunk.boundary.overlap_before_ms)
        for item in requests
    ] == [(0, 10_000, 0), (8_000, 18_000, 2_000), (16_000, 26_000, 2_000)]
    assert len({item.chunk.chunk_id for item in requests}) == 3
    assert [item.chunk.chunk_id for item in requests] == [
        audio_chunk_id(
            decoded_sha256=sha256(b"synthetic decoded audio").hexdigest(),
            decoder_reference=profile.decoder_reference,
            boundary=item.chunk.boundary,
        )
        for item in requests
    ]
    assert all(isinstance(item, SpeechToTextRequestV2) for item in requests)
    assert all(item.recording == requests[0].recording for item in requests)
    assert all(item.language_hint == "ru" for item in requests)
    assert all(item.adapter_reference == "fake_adapter:v1" for item in requests)
    assert all(item.model_config_reference == "fake_config:v1" for item in requests)
    assert all(item.recording.source_revision_id == _id(101) for item in requests)
    assert all(item.recording.source_revision_digest == REVISION.revision_digest for item in requests)
    assert all(item.recording.original_sha256 == DIGEST for item in requests)
    assert all(
        item.processing_profile_reference == profile.profile_reference for item in requests
    )
    assert all(result.detected_language is None for result in factory.runtime.results)
    assert all(item.chunk.decoder_reference == profile.decoder_reference for item in requests)
    assert all(
        item.chunk.decoded_sha256 == item.chunk.decoded_reference.digest_hex
        for item in requests
    )
    assert all(
        item.chunk.decoded_sha256 == sha256(b"synthetic decoded audio").hexdigest()
        for item in requests
    )
    assert all(
        item.chunk.boundary.end_ms - item.chunk.boundary.start_ms <= 25_000
        for item in requests
    )
    assert [
        (request.chunk.boundary.start_ms + local_start,
         request.chunk.boundary.start_ms + local_end)
        for request, (local_start, local_end) in zip(
            requests, ((9_000, 9_900), (1_500, 2_500), (1_500, 2_500))
        )
    ] == [(9_000, 9_900), (9_500, 10_500), (17_500, 18_500)]
    reassembled = []
    for request, result in zip(requests, factory.runtime.results):
        translated = reassemble_private_asr_chunk_segments(
            request.chunk.boundary, result.segments, prior_segments=reassembled,
        )
        assert translated is not None
        reassembled.extend(translated)
    assert [(item.start_ms, item.end_ms, item.text) for item in reassembled] == [
        (9_000, 9_900, "начало"),
        (9_500, 10_500, "граница"),
        (17_500, 18_500, "конец"),
    ]
    assert report.denominators.measured == 1
    assert report.outcomes[0].word_errors == 0
    assert report.outcomes[0].reference_words == 3
    assert report.outcomes[0].character_errors == 0
    assert report.outcomes[0].reference_characters == 20
    assert report.status.value == "NOT_VERIFIED"
    assert receipt.report_ref.reference_id == _id(170)
    assert reports.calls == 1
    assert aggregate_export.calls == 0
    assert reports.events == ["prepare_write", "authorize_commit", "atomic_visibility"]
    assert all(
        private_text.encode() not in reports.payloads[0]
        for private_text in ("начало", "граница", "конец", "полный-дубликат")
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ordinal": 0}, "ordinal"),
        ({"start_ms": 100, "end_ms": 100}, "end_ms"),
        ({"start_ms": 0, "end_ms": 1_000, "overlap_before_ms": 1_000}, "overlap"),
        ({"ordinal": 1, "overlap_before_ms": 1}, "first chunk"),
    ],
)
def test_chunk_boundary_contract_rejects_invalid_multi_chunk_window(overrides, message):
    values = {"ordinal": 2, "start_ms": 8_000, "end_ms": 18_000,
              "overlap_before_ms": 2_000}
    values.update(overrides)

    with pytest.raises(ValidationError, match=message):
        AudioChunkBoundary(**values)


@pytest.mark.parametrize(
    "failure",
    ["missing_chunk", "invalid_timestamp", "wrong_identity", "out_of_order"],
)
@pytest.mark.asyncio
async def test_invalid_multichunk_result_is_unavailable_and_not_measured(failure):
    manifest, _, _, _ = corpus(recording_duration_ms=26_000)
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(
        admission, source_admission, processing_profile=multi_chunk_profile(),
    )
    if failure == "missing_chunk":
        factory.runtime.missing_chunk_ordinals.add(2)
    elif failure == "invalid_timestamp":
        factory.runtime.segments_by_ordinal = {
            1: (SpeechToTextSegment(start_ms=100, end_ms=200, text="синтетика"),),
            2: (SpeechToTextSegment(start_ms=0, end_ms=10_001, text="private text"),),
        }
    elif failure == "wrong_identity":
        factory.runtime.wrong_model_config = True
    else:
        factory.runtime.out_of_order = True

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.denominators.measured == 0
    assert report.denominators.unavailable == 1
    assert report.outcomes[0].disposition.value == "unavailable"
    assert report.outcomes[0].word_errors is None
    assert report.outcomes[0].character_errors is None
    assert report.status.value == "NOT_VERIFIED"
    assert factory.runtime.calls == (
        2 if failure in {"missing_chunk", "invalid_timestamp"} else 1
    )
    assert factory.audio_preparation.releases == 1
    assert factory.creates == 1
    assert source_admission.admissions == source_admission.content_verifications == 1
    assert reports.calls == 1
    assert "private text" not in reports.payloads[0].decode()


@pytest.mark.asyncio
async def test_duplicate_chunk_ordinals_are_unavailable_before_runtime_initialization():
    class DuplicateOrdinalProfile(TranscriptionProcessingProfile):
        def plan_chunks(self, inspection):
            boundaries = super().plan_chunks(inspection)
            return (boundaries[0], boundaries[0], *boundaries[2:])

    profile = DuplicateOrdinalProfile(**multi_chunk_profile().model_dump())
    manifest, _, _, _ = corpus(recording_duration_ms=26_000)
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(
        admission, source_admission, processing_profile=profile,
    )

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.denominators.measured == 0
    assert report.denominators.unavailable == 1
    assert report.status.value == "NOT_VERIFIED"
    assert factory.audio_preparation.releases == 1
    assert factory.creates == factory.runtime.calls == 0
    assert reports.calls == 1


@pytest.mark.asyncio
async def test_revocation_between_chunks_stops_before_next_chunk_and_report():
    manifest, _, _, _ = corpus(recording_duration_ms=26_000)
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(
        admission, source_admission, processing_profile=multi_chunk_profile(),
    )
    factory.runtime.after_result = lambda request: setattr(
        source_admission, "revoke_after_next_recheck", True,
    )

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(
            manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
        )

    assert refused.value.outcome.kind.value == "revoked"
    assert factory.runtime.calls == 1
    assert factory.audio_preparation.releases == 1
    assert factory.creates == 1
    assert reports.calls == 0
    assert reports.payloads == reports.receipts == []


@pytest.mark.asyncio
async def test_report_commit_refusal_prevents_payload_and_receipt_visibility():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(fail_at="release_authorized"), FakeSourceAdmission()
    subject, _, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied):
        await subject.run(
            manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
        )

    assert reports.calls == 1
    assert reports.events == ["prepare_write", "authorize_commit"]
    assert reports.payloads == reports.receipts == []


@pytest.mark.asyncio
async def test_report_commit_callback_exception_is_content_free_and_not_visible():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    admission.release_error = RuntimeError("private report authorization locator")
    subject, _, reports, _ = runner(admission, source_admission)

    with pytest.raises(PrivateEvaluationUnavailable) as unavailable:
        await subject.run(
            manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
        )

    assert str(unavailable.value) == "private report storage failed"
    assert "private report authorization locator" not in str(unavailable.value)
    assert reports.calls == 1
    assert reports.events == ["prepare_write", "authorize_commit"]
    assert reports.payloads == reports.receipts == []


@pytest.mark.asyncio
async def test_report_commit_callback_cancellation_propagates_without_visibility_or_retry():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    admission.release_error = asyncio.CancelledError()
    subject, _, reports, _ = runner(admission, source_admission)

    with pytest.raises(asyncio.CancelledError):
        await subject.run(
            manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
        )

    assert reports.calls == 1
    assert reports.events == ["prepare_write", "authorize_commit"]
    assert reports.payloads == reports.receipts == []


@pytest.mark.parametrize(
    "failed_gate",
    ["purpose", "legal_evidence", "source_revision", "access_policy", "read_grant", "process_grant", "retention", "audit", "environment", "dataset_split", "host_no_egress"],
)
@pytest.mark.asyncio
async def test_private_runner_preflight_failure_never_reads_content_or_initializes_runtime(failed_gate):
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(fail_at=failed_gate), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert source_admission.content_verifications == 0
    assert admission.annotation_reads == 0
    assert factory.creates == factory.runtime.calls == 0
    assert reports.calls == 0
    expected_kind = "unavailable" if failed_gate == "source_revision" else "denied"
    assert refused.value.outcome.kind.value == expected_kind
    assert refused.value.outcome.eligible == 1
    assert refused.value.outcome.admitted == 0
    assert source_admission.admissions == 0


@pytest.mark.asyncio
async def test_private_runner_rejects_workspace_scope_before_any_record_or_content_access():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    wrong_context = replace(
        trusted_context(),
        scope=EnvironmentScope(workspace_id=_id(999), environment_id=ENVIRONMENT),
    )

    with pytest.raises(CorpusAdmissionDenied):
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=wrong_context)

    assert admission.events == []
    assert source_admission.content_verifications == factory.creates == reports.calls == 0


@pytest.mark.asyncio
async def test_source_admission_denial_never_reaches_content_annotation_runtime_or_report():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission(deny_admission=True)
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied):
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert source_admission.admissions == 1
    assert source_admission.content_verifications == 0
    assert admission.annotation_reads == 0
    assert factory.creates == factory.runtime.calls == reports.calls == 0


@pytest.mark.asyncio
async def test_unexpected_content_adapter_error_is_content_free_unavailable_refusal():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()

    async def broken_verify(*args, **kwargs):
        raise RuntimeError("private source locator")

    source_admission.verify_content = broken_verify
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert "private source locator" not in str(refused.value)
    assert refused.value.outcome.kind.value == "unavailable"
    assert refused.value.outcome.stage == "content_verification"
    assert admission.annotation_reads == factory.creates == reports.calls == 0


@pytest.mark.asyncio
async def test_missing_protocol_owner_decision_is_not_verified_and_never_runs_candidate():
    manifest, _, _, _ = corpus()
    manifest = build_private_manifest(
        dataset_id=manifest.dataset_id, version=manifest.version, created_at=manifest.created_at,
        purpose=manifest.purpose, purpose_approval_ref=manifest.purpose_approval_ref,
        policy_version=manifest.policy_version, protocol_decision_ref=None,
        workspace_id=manifest.workspace_id, environment_id=manifest.environment_id,
        storage_profile_id=manifest.storage_profile_id,
        retention_profile_ref=manifest.retention_profile_ref,
        evaluator_schema=manifest.evaluator_schema, evaluator_version=manifest.evaluator_version,
        normalizer_version=manifest.normalizer_version,
        split_method_version=manifest.split_method_version,
        lineage_key_version=manifest.lineage_key_version, records=manifest.records,
    )
    admission, source_admission = FakeAdmission(protocol_decision_verified=False), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert source_admission.content_verifications == admission.annotation_reads == 0
    assert source_admission.admissions == 0
    assert factory.creates == factory.runtime.calls == reports.calls == 0
    assert factory.audio_preparation.calls == 0
    assert refused.value.outcome.kind.value == "denied"
    assert refused.value.outcome.stage == "protocol_owner_decision"


@pytest.mark.asyncio
async def test_split_lineage_leakage_stops_before_stage_one_admission():
    manifest, record, _, _ = corpus()
    development_record = record.model_copy(
        update={"record_id": _id(199), "split": CorpusSplit.DEVELOPMENT}
    )
    # Simulate a frozen manifest received from an untrusted external source. The
    # model copy intentionally bypasses construction validation so the runner's
    # own whole-manifest preflight must reject it before source admission.
    manifest = manifest.model_copy(
        update={"records": (record, development_record)}
    )
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(
            manifest,
            requested_split=CorpusSplit.QUALIFICATION,
            context=trusted_context(),
        )

    assert refused.value.outcome.stage == "split_isolation"
    assert source_admission.admissions == source_admission.content_verifications == 0
    assert admission.annotation_reads == factory.creates == factory.runtime.calls == 0
    assert reports.calls == 0


@pytest.mark.asyncio
async def test_private_runner_never_uses_development_records_for_qualification():
    manifest, record, _, _ = corpus()
    dev_record = record.model_copy(update={"split": CorpusSplit.DEVELOPMENT})
    manifest = build_private_manifest(
        dataset_id=manifest.dataset_id, version="dataset:v2", created_at=NOW,
        purpose="asr_model_evaluation", purpose_approval_ref=manifest.purpose_approval_ref,
        policy_version=manifest.policy_version, protocol_decision_ref=ref(141, "protocol_decision"),
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        storage_profile_id=manifest.storage_profile_id,
        retention_profile_ref=manifest.retention_profile_ref,
        evaluator_schema=manifest.evaluator_schema, evaluator_version=manifest.evaluator_version,
        normalizer_version=manifest.normalizer_version,
        split_method_version=manifest.split_method_version,
        lineage_key_version=manifest.lineage_key_version, records=(dev_record,),
    )
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)

    report, _ = await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert report.outcomes == ()
    assert report.denominators.eligible == 0
    assert factory.creates == 0
    assert source_admission.content_verifications == 0
    assert reports.calls == 1


@pytest.mark.asyncio
async def test_revocation_during_inference_fences_case_and_report_is_not_verified():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission(revoked_after_inference=True)
    subject, factory, _, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert factory.runtime.calls == 1
    assert source_admission.rechecks == 6
    assert refused.value.outcome.kind.value == "revoked"
    assert refused.value.outcome.stage == "candidate_inference"


@pytest.mark.asyncio
async def test_revocation_before_inference_never_initializes_candidate_runtime():
    manifest, _, _, _ = corpus()
    admission = FakeAdmission()
    source_admission = FakeSourceAdmission(revoke_at_recheck=2)
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert admission.annotation_reads == 1
    assert factory.creates == factory.runtime.calls == 0
    assert reports.calls == 0
    assert refused.value.outcome.kind.value == "revoked"
    assert refused.value.outcome.stage == "before_audio_preparation"
    assert factory.audio_preparation.calls == 0


@pytest.mark.asyncio
async def test_revocation_while_audio_handle_resolves_fences_candidate_invocation():
    manifest, _, _, _ = corpus()
    admission = FakeAdmission()
    source_admission = FakeSourceAdmission(revoke_at_recheck=5)
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(
            manifest,
            requested_split=CorpusSplit.QUALIFICATION,
            context=trusted_context(),
        )

    assert "audio_handle" in admission.events
    assert factory.creates == 1
    assert factory.runtime.calls == 0
    assert reports.calls == 0
    assert refused.value.outcome.kind.value == "revoked"
    assert refused.value.outcome.stage == "before_inference"


@pytest.mark.asyncio
async def test_private_report_storage_failure_is_generic_and_does_not_export_aggregate():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    storage = FakeReports(fail=True)
    subject, factory, _, _ = runner(admission, source_admission, reports=storage)

    with pytest.raises(PrivateEvaluationUnavailable, match="private report storage failed"):
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert storage.calls == 1
    assert factory.runtime.calls == 1


@pytest.mark.asyncio
async def test_runtime_factory_error_becomes_unavailable_without_leaking_adapter_details():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.fail_with_content = True

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.outcomes[0].disposition.value == "unavailable"
    assert report.failure_code == "candidate_unavailable"
    assert reports.calls == 1
    assert factory.runtime.calls == 0
    assert "private model asset locator" not in reports.payloads[0].decode()


@pytest.mark.asyncio
async def test_audio_handle_error_becomes_unavailable_without_candidate_invocation():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()

    async def broken_audio_handle(*args, **kwargs):
        raise RuntimeError("private content locator")

    admission.audio_handle = broken_audio_handle
    subject, factory, reports, _ = runner(admission, source_admission)

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.outcomes[0].disposition.value == "unavailable"
    assert factory.runtime.calls == 0
    assert reports.calls == 1
    assert "private content locator" not in reports.payloads[0].decode()


@pytest.mark.asyncio
async def test_wrong_source_revision_handle_stops_before_decoder_and_model():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()

    async def switched_revision(*args, **kwargs):
        return AudioRecordingHandle(
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
            source_object_id=SOURCE.source_object_id, source_revision_id=_id(999),
            source_revision_digest=REVISION.revision_digest, original_sha256=DIGEST,
            original_reference=REVISION.original_reference, media_type="audio/wav",
            recording_duration_ms=1000,
        )

    admission.audio_handle = switched_revision
    subject, factory, reports, _ = runner(admission, source_admission)

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.denominators.unavailable == 1
    assert factory.audio_preparation.calls == 0
    assert factory.creates == factory.runtime.calls == 0
    assert reports.calls == 1


@pytest.mark.asyncio
async def test_candidate_failure_is_private_case_outcome_without_error_content():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.runtime.fail_with_content = True

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.status.value == "NOT_VERIFIED"
    assert report.failure_code == "candidate_unavailable"
    assert report.denominators.unavailable == 1
    assert report.outcomes[0].disposition.value == "unavailable"
    assert "synthetic private transcript must not escape" not in reports.payloads[0].decode()


@pytest.mark.parametrize("failure", ["invalid_timestamps", "out_of_order"])
@pytest.mark.asyncio
async def test_invalid_v2_chunk_timeline_is_unavailable_without_repair(failure):
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    setattr(factory.runtime, failure, True)

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.denominators.measured == 0
    assert report.denominators.unavailable == 1
    assert factory.runtime.calls == 1
    assert "синтетика" not in reports.payloads[0].decode()


@pytest.mark.asyncio
async def test_legacy_v1_candidate_result_is_not_counted_as_measurement():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.runtime.legacy_result = True

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.denominators.measured == 0
    assert report.denominators.unavailable == 1
    assert reports.calls == 1


@pytest.mark.asyncio
async def test_non_null_language_without_detection_evidence_is_not_measured():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.runtime.detected_language = "ru"

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.denominators.measured == 0
    assert report.denominators.unavailable == 1
    assert reports.calls == 1


@pytest.mark.asyncio
async def test_cancellation_during_candidate_call_releases_ephemeral_decode_and_never_reports():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.runtime.cancel = True

    with pytest.raises(asyncio.CancelledError):
        await subject.run(
            manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
        )

    assert factory.runtime.calls == 1
    assert factory.audio_preparation.releases == 1
    assert reports.calls == 0


@pytest.mark.asyncio
async def test_decoder_failure_does_not_initialize_candidate_and_keeps_errors_content_free():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.audio_preparation.fail = True

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert factory.audio_preparation.calls == 1
    assert factory.audio_preparation.aborts == 1
    assert factory.creates == factory.runtime.calls == 0
    assert report.denominators.unavailable == 1
    assert "private staging locator" not in reports.payloads[0].decode()


@pytest.mark.asyncio
async def test_ephemeral_cleanup_failure_blocks_private_report_persistence():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.audio_preparation.fail_release = True

    with pytest.raises(PrivateEvaluationUnavailable, match="audio preparation cleanup"):
        await subject.run(
            manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
        )

    assert factory.runtime.calls == 1
    assert reports.calls == 0


@pytest.mark.asyncio
async def test_revocation_during_release_authorization_fences_report_commit():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission(revoke_at_recheck=10)
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(
            manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
        )

    assert factory.runtime.calls == 1
    assert reports.calls == 1
    assert reports.payloads == []
    assert refused.value.outcome.kind.value == "revoked"
    assert refused.value.outcome.stage == "before_private_report_commit"


@pytest.mark.asyncio
async def test_candidate_result_identity_mismatch_is_unavailable_not_attributed():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, factory, reports, _ = runner(admission, source_admission)
    factory.runtime.wrong_identity = True

    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )

    assert report.outcomes[0].disposition.value == "unavailable"
    assert report.denominators.measured == 0
    assert report.failure_code == "candidate_unavailable"
    assert reports.calls == 1


def test_candidate_identity_mismatch_is_rejected_before_any_run():
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()

    with pytest.raises(ValueError, match="candidate identity"):
        runner(admission, source_admission, candidate_id="other_candidate")

    assert admission.events == []
    assert source_admission.admissions == source_admission.content_verifications == 0


@pytest.mark.asyncio
async def test_private_dataset_resolver_rejects_record_outside_frozen_manifest():
    from spine.application.private_asr_qualification import GovernedPrivateDatasetResolver

    manifest, record, _, _ = corpus()
    unlisted = record.model_copy(update={"record_id": _id(999)})
    admission = FakeAdmission()
    resolver = GovernedPrivateDatasetResolver(admission)

    with pytest.raises(CorpusAdmissionDenied, match="member of the frozen dataset"):
        await resolver.request_for(manifest, unlisted, context=trusted_context())

    assert admission.events == []


@pytest.mark.asyncio
async def test_annotation_reference_or_hash_mismatch_stops_before_runtime():
    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    original_loader = admission.load_approved_annotation

    async def mismatch(manifest, record, *, admitted, context):
        loaded = await original_loader(manifest, record, admitted=admitted, context=context)
        return loaded.model_copy(update={"annotation_sha256": "0" * 64})

    admission.load_approved_annotation = mismatch
    subject, factory, reports, _ = runner(admission, source_admission)

    with pytest.raises(CorpusAdmissionDenied) as refused:
        await subject.run(manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context())

    assert source_admission.content_verifications == 1
    assert factory.creates == factory.runtime.calls == reports.calls == 0
    assert refused.value.outcome.kind.value == "denied"
    assert refused.value.outcome.stage == "annotation_binding"


@pytest.mark.asyncio
async def test_complementary_disclosure_risk_refusal_prevents_public_export():
    from dataclasses import replace

    from spine.application.private_asr_corpus import PrivacySanitizationError

    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(fail_at="complementary_risk"), FakeSourceAdmission()
    subject, _, _, aggregate_export = runner(admission, source_admission)
    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )
    export_context = replace(
        trusted_context(),
        operation=PersistenceOperation("sanitize_private_asr_report"),
    )

    with pytest.raises(PrivacySanitizationError, match="disclosure risk"):
        await subject.export_aggregate(report, privacy_policy_version="privacy:v1", context=export_context)

    assert aggregate_export.calls == 0


@pytest.mark.asyncio
async def test_private_report_contract_rejects_fake_qualification_and_identifying_case_payload():
    from pydantic import ValidationError

    from spine.domain.evaluations.private_asr_qualification import (
        PrivateCaseDisposition, PrivateCaseOutcome, PrivateRealASREvaluationReport,
    )

    with pytest.raises(ValidationError):
        PrivateCaseOutcome(
            record_ref=ref(990), split=CorpusSplit.QUALIFICATION,
            disposition=PrivateCaseDisposition.UNSCORABLE, customer_name="synthetic-name",
        )

    manifest, _, _, _ = corpus()
    admission, source_admission = FakeAdmission(), FakeSourceAdmission()
    subject, _, _, _ = runner(admission, source_admission)
    report, _ = await subject.run(
        manifest, requested_split=CorpusSplit.QUALIFICATION, context=trusted_context()
    )
    payload = report.model_dump(mode="python")
    payload["status"] = "PASS"
    with pytest.raises(ValidationError):
        PrivateRealASREvaluationReport.model_validate(payload)
