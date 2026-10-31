from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.evaluations.private_asr_corpus import (
    AnnotationStatus,
    AudioDigestVerification,
    ClusterLineage,
    CorpusSplit,
    CoverageCounts,
    CoverageObservation,
    GroundTruthAnnotation,
    MediaMetadata,
    PrivateCorpusRecord,
    PrivateReference,
    SemanticAnnotation,
    SourceRevisionBinding,
    TranscriptSegment,
    build_private_manifest,
    calculate_coverage,
    validate_split_isolation,
    qualification_sufficiency,
)
from spine.domain.sources import IdentityMode, RevisionMetadata, SourceObject, SourceRevision, SourceKind, content_revision
from spine.domain.sources.original_reference import ObjectReference


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


def source_pair(*, source_number: int = 1):
    source_id = _id(100 + source_number)
    workspace, environment = _id(1), _id(2)
    source = SourceObject(
        source_object_id=source_id,
        workspace_id=workspace,
        environment_id=environment,
        source_kind=SourceKind.RECORD.value,
        identity_mode=IdentityMode.UPLOAD,
        upload_identity=f"synthetic-upload-{source_number}",
        created_at=NOW,
    )
    raw = f"synthetic audio bytes {source_number}".encode()
    digest = sha256(raw).hexdigest()
    original = ObjectReference(
        schema_version=1,
        object_id=_id(200 + source_number),
        storage_generation=_id(300 + source_number),
        digest_algorithm="sha256",
        digest_hex=digest,
        byte_length=len(raw),
    )
    revision = content_revision(
        revision_id=_id(400 + source_number),
        source_object_id=source_id,
        workspace_id=workspace,
        environment_id=environment,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        original_reference=original,
        media_type="audio/wav",
        revision_metadata=RevisionMetadata(document_language="ru"),
        original_sha256=digest,
        byte_length=len(raw),
        observed_at=NOW,
    )
    return source, revision


def private_ref(number: int, kind: str = "private") -> PrivateReference:
    return PrivateReference(reference_id=_id(number), reference_kind=kind)


def record(*, split: CorpusSplit, source_number: int = 1, lineage_number: int = 9):
    source, revision = source_pair(source_number=source_number)
    return PrivateCorpusRecord(
        record_id=_id(500 + source_number),
        source_binding=SourceRevisionBinding(
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            source_revision_number=source_number,
            source_revision_digest=revision.revision_digest,
            workspace_id=source.workspace_id,
            environment_id=source.environment_id,
            media_type=revision.media_type,
            byte_length=revision.byte_length,
        ),
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        admission={
            "purpose": "asr_model_evaluation",
            "purpose_approval_ref": private_ref(701),
            "legal_data_evidence_ref": private_ref(602),
            "attestation_ref": private_ref(603),
            "source_object_id": source.source_object_id,
            "source_revision_id": revision.revision_id,
            "source_revision_digest": revision.revision_digest,
            "workspace_id": source.workspace_id,
            "actor_id": _id(604),
            "read_grant_ref": private_ref(605),
            "process_grant_ref": private_ref(606),
            "access_policy_version": _id(900),
            "policy_version": "corpus-policy:v1",
            "retention_profile_ref": private_ref(607),
            "audit_receipt_ref": private_ref(608),
            "allowed_environment_id": source.environment_id,
            "admitted_at": NOW,
            "expires_at": NOW + timedelta(hours=1),
            "revocation_epoch": 0,
        },
        audio_verification=AudioDigestVerification(
            audio_sha256=revision.original_sha256,
            source_revision_id=revision.revision_id,
            byte_length=revision.byte_length,
            verified_at=NOW,
            audit_receipt_ref=private_ref(611, "content_verification_audit"),
        ),
        media=MediaMetadata(
            media_type="audio/wav",
            container="wav",
            codec="pcm_s16le",
            byte_length=revision.byte_length,
            duration_ms=1000,
            sample_rate_hz=16000,
            channels=1,
            audio_stream_count=1,
        ),
        original_storage_ref=private_ref(609, "original"),
        derived_staging_profile="staging:v1",
        annotation_ref=private_ref(610, "annotation"),
        annotation_sha256=sha256(f"label {source_number}".encode()).hexdigest(),
        split=split,
        split_assignment_version="split:v1",
        lineage=ClusterLineage(
            meeting_cluster=_id(lineage_number),
            source_lineage_cluster=_id(lineage_number + 1),
            customer_project_cluster=_id(lineage_number + 2),
            speaker_clusters=(_id(lineage_number + 3),),
        ),
        coverage={
            "tags": ("quiet",),
            "expected_annotation_spans": 1,
            "missing_label_count": 0,
            "unalignable_segment_count": 0,
            "insufficient_categories": (),
        },
    )


def test_private_contracts_are_frozen_and_reject_extra_fields() -> None:
    item = TranscriptSegment(start_ms=0, end_ms=1, human_text="synthetic")
    assert item.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        TranscriptSegment.model_validate(
            {"start_ms": 0, "end_ms": 1, "human_text": "synthetic", "file_path": "/private"}
        )
    with pytest.raises(ValidationError):
        item.start_ms = 1


def test_private_manifest_digest_covers_immutable_versioned_release() -> None:
    entry = record(split=CorpusSplit.DEVELOPMENT)
    manifest = build_private_manifest(
        dataset_id=_id(1), version="1.0.0", created_at=NOW,
        purpose="asr_model_evaluation", purpose_approval_ref=private_ref(701),
        policy_version="corpus-policy:v1", protocol_decision_ref=None,
        workspace_id=entry.workspace_id, environment_id=entry.environment_id,
        storage_profile_id="private-storage:v1", retention_profile_ref=private_ref(702),
        evaluator_schema="asr-evaluator:v1", evaluator_version="evaluator:v1",
        normalizer_version="normalizer:v1", split_method_version="split:v1",
        lineage_key_version="lineage:v1", records=(entry,),
    )
    assert manifest.schema_version == "r2-private-asr-evaluation-dataset:v1"
    assert manifest.calculate_digest() == manifest.manifest_sha256
    with pytest.raises(ValidationError):
        manifest.version = "1.0.1"
    with pytest.raises(ValueError, match="unsupported fields"):
        build_private_manifest(customer_name="not-allowed")


def test_manifest_rejects_reused_exact_revision_across_splits() -> None:
    first = record(split=CorpusSplit.DEVELOPMENT)
    second = first.model_copy(update={"record_id": _id(999), "split": CorpusSplit.QUALIFICATION})
    with pytest.raises(ValidationError, match="source lineage leaks"):
        build_private_manifest(
            dataset_id=_id(1), version="1.0.0", created_at=NOW,
            purpose="asr_model_evaluation", purpose_approval_ref=private_ref(701),
            policy_version="corpus-policy:v1", workspace_id=first.workspace_id,
            environment_id=first.environment_id, storage_profile_id="private-storage:v1",
            retention_profile_ref=private_ref(702), evaluator_schema="asr-evaluator:v1",
            evaluator_version="evaluator:v1", normalizer_version="normalizer:v1",
            split_method_version="split:v1", lineage_key_version="lineage:v1",
            records=(first, second),
        )


def test_record_requires_exact_source_revision_and_workspace_environment_binding() -> None:
    value = record(split=CorpusSplit.DEVELOPMENT)
    assert value.source_binding.source_revision_id == _id(401)
    with pytest.raises(ValidationError, match="scope does not match"):
        PrivateCorpusRecord.model_validate(
            {**value.model_dump(), "workspace_id": _id(999)}
        )


def test_transcript_rejects_guessed_text_for_unintelligible_span() -> None:
    with pytest.raises(ValidationError, match="guessed text"):
        TranscriptSegment(start_ms=20, end_ms=40, human_text="guess", unintelligible=True)


def test_asr_text_requires_independent_human_validation_and_review() -> None:
    with pytest.raises(ValidationError, match="independent human validation"):
        GroundTruthAnnotation(
            annotation_id=_id(1), version=1, source_record_id=_id(2),
            status=AnnotationStatus.DRAFT, authored_by=_id(3),
            author_authorization_ref=private_ref(4), authored_at=NOW,
            segments=(), asr_text_approved_as_ground_truth=True,
        )
    with pytest.raises(ValidationError, match="authorized review"):
        GroundTruthAnnotation(
            annotation_id=_id(10), version=1, source_record_id=_id(2),
            status=AnnotationStatus.APPROVED, authored_by=_id(3),
            author_authorization_ref=private_ref(4), authored_at=NOW,
            segments=(TranscriptSegment(start_ms=0, end_ms=1, human_text="label"),),
            independently_human_validated=True,
        )


def test_immutable_corrections_require_successor_version() -> None:
    base = GroundTruthAnnotation(
        annotation_id=_id(10), version=1, source_record_id=_id(2),
        status=AnnotationStatus.DRAFT, authored_by=_id(3),
        author_authorization_ref=private_ref(4), authored_at=NOW,
        segments=(TranscriptSegment(start_ms=0, end_ms=1, human_text="first"),),
    )
    corrected = GroundTruthAnnotation.model_validate(
        {
            **base.model_dump(),
            "annotation_id": _id(11),
            "version": 2,
            "predecessor_annotation_id": base.annotation_id,
            "segments": (TranscriptSegment(start_ms=0, end_ms=1, human_text="corrected"),),
        }
    )
    assert corrected.predecessor_annotation_id == base.annotation_id


def test_ground_truth_approval_binds_independent_authorized_reviewer() -> None:
    annotation = GroundTruthAnnotation(
        annotation_id=_id(20), version=1, source_record_id=_id(2),
        status=AnnotationStatus.APPROVED, authored_by=_id(3),
        author_authorization_ref=private_ref(4), authored_at=NOW,
        segments=(TranscriptSegment(start_ms=0, end_ms=1, human_text="verified synthetic label"),),
        independently_human_validated=True,
        reviews=({
            "reviewer_id": _id(5), "annotation_version": 1,
            "authorization_ref": private_ref(6), "status": "approved",
            "reviewed_at": NOW,
        },),
    )
    assert annotation.status is AnnotationStatus.APPROVED


def test_asr_assisted_text_cannot_be_approved_without_independent_review() -> None:
    with pytest.raises(ValidationError, match="independent human validation"):
        GroundTruthAnnotation(
            annotation_id=_id(30), version=1, source_record_id=_id(2),
            status=AnnotationStatus.APPROVED, authored_by=_id(3),
            author_authorization_ref=private_ref(4), authored_at=NOW,
            text_origin="asr_assisted",
            segments=(TranscriptSegment(start_ms=0, end_ms=1, human_text="machine text"),),
            reviews=({
                "reviewer_id": _id(5), "annotation_version": 1,
                "authorization_ref": private_ref(6), "status": "approved",
                "reviewed_at": NOW,
            },),
        )


def test_adjudicated_ground_truth_requires_independent_authorized_adjudicator() -> None:
    with pytest.raises(ValidationError, match="independent adjudicator"):
        GroundTruthAnnotation(
            annotation_id=_id(40), version=1, source_record_id=_id(2),
            status=AnnotationStatus.ADJUDICATED, authored_by=_id(3),
            author_authorization_ref=private_ref(4), authored_at=NOW,
            segments=(TranscriptSegment(start_ms=0, end_ms=1, human_text="label"),),
        )


def test_semantic_labels_cover_business_spans_without_freeform_category() -> None:
    label = SemanticAnnotation(
        start_ms=1, end_ms=3, category="negation", label="negation_scope", critical=True
    )
    assert label.critical
    with pytest.raises(ValidationError):
        SemanticAnnotation(start_ms=1, end_ms=3, category="customer_name", label="rare_text")


def test_split_isolation_rejects_shared_meeting_or_speaker_lineage() -> None:
    dev = record(split=CorpusSplit.DEVELOPMENT, source_number=1)
    qual = record(split=CorpusSplit.QUALIFICATION, source_number=2)
    with pytest.raises(ValueError, match="correlated lineage"):
        validate_split_isolation((dev, qual))


def test_split_isolation_accepts_independent_groups_and_checks_derivatives() -> None:
    dev = record(split=CorpusSplit.DEVELOPMENT, source_number=1, lineage_number=20)
    qual = record(split=CorpusSplit.QUALIFICATION, source_number=2, lineage_number=30)
    validate_split_isolation((dev, qual))


def test_coverage_keeps_missing_and_unalignable_spans_in_denominator() -> None:
    counts = CoverageCounts(
        eligible_observations=10, annotated_observations=7, scorable_observations=4,
        measured_observations=3, independent_lineages=6, annotation_spans=12,
        missing_labels=2, unalignable_segments=3, insufficient_categories=("overlap",),
    )
    assert counts.eligible_observations == 10
    assert counts.annotation_spans == 12
    with pytest.raises(ValidationError, match="coverage exclusions"):
        CoverageCounts(
            eligible_observations=1, annotated_observations=1, scorable_observations=1,
            measured_observations=1, independent_lineages=1, annotation_spans=1,
            missing_labels=1, unalignable_segments=1,
        )


def test_coverage_calculation_preserves_eligible_and_missing_denominators() -> None:
    result = calculate_coverage((
        CoverageObservation(
            lineage_id=_id(1), eligible=True, annotated=True, scorable=True, measured=True,
            annotation_spans=4, missing_labels=1, unalignable_segments=1,
        ),
        CoverageObservation(
            lineage_id=_id(2), eligible=True, annotation_spans=2, missing_labels=2,
            unalignable_segments=0, insufficient_categories=("overlap",),
        ),
        CoverageObservation(
            lineage_id=_id(3), eligible=False, annotation_spans=0,
            missing_labels=0, unalignable_segments=0,
        ),
    ))
    assert result.eligible_observations == 2
    assert result.annotated_observations == 1
    assert result.measured_observations == 1
    assert result.annotation_spans == 6
    assert result.missing_labels == 3
    assert result.unalignable_segments == 1
    assert result.independent_lineages == 2


@pytest.mark.parametrize("decision", [False, True])
def test_sufficiency_remains_not_verified_without_measured_protocol_gate(decision: bool) -> None:
    assert qualification_sufficiency(protocol_decision_present=decision).value == "NOT_VERIFIED"
