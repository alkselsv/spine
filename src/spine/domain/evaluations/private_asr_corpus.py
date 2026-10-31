"""Private, metadata-safe contract for customer-meeting ASR evaluation.

These contracts describe a possible governed corpus. They do not authorize
real data access or establish qualification sufficiency.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBool, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


PRIVATE_ASR_DATASET_SCHEMA = "r2-private-asr-evaluation-dataset:v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CorpusSplit(str, Enum):
    DEVELOPMENT = "development"
    QUALIFICATION = "qualification"


class QualificationSufficiency(str, Enum):
    NOT_VERIFIED = "NOT_VERIFIED"


class AnnotationStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    ADJUDICATED = "adjudicated"
    APPROVED = "approved"


class AggregateCategory(str, Enum):
    OVERALL = "overall"
    DURATION_BAND = "duration_band"
    NOISE_BAND = "noise_band"
    OVERLAP_PRESENCE = "overlap_presence"
    SILENCE_PRESENCE = "silence_presence"
    CODEC_FAMILY = "codec_family"
    TERMINOLOGY_CLASS = "terminology_class"
    CRITICAL_SPAN_CLASS = "critical_span_class"


class AggregateMetric(str, Enum):
    WER = "wer"
    CER = "cer"
    TERMINOLOGY_PRECISION = "terminology_precision"
    TERMINOLOGY_RECALL = "terminology_recall"
    CRITICAL_SPAN_ERROR_RATE = "critical_span_error_rate"
    TIMESTAMP_ALIGNMENT_RATE = "timestamp_alignment_rate"
    SILENCE_RECALL = "silence_recall"
    LONG_FORM_COVERAGE = "long_form_coverage"
    PREPROCESSING_DELTA = "preprocessing_delta"


class PrivateCorpusContract(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    contract_version: Literal["r2-private-asr-corpus-contract:v1"] = "r2-private-asr-corpus-contract:v1"


class PrivateReference(PrivateCorpusContract):
    """Opaque, non-resolvable reference whose value is private by policy."""

    reference_id: UUID
    reference_kind: VersionIdentifier

    @model_validator(mode="after")
    def validate_reference_id(self) -> "PrivateReference":
        if self.reference_id.int == 0:
            raise ValueError("private reference identity must be non-zero")
        return self


class MediaMetadata(PrivateCorpusContract):
    media_type: str = Field(pattern=r"^audio/[a-z0-9.+-]+$")
    container: VersionIdentifier
    codec: VersionIdentifier
    byte_length: StrictInt = Field(ge=0)
    duration_ms: StrictInt = Field(ge=0)
    sample_rate_hz: StrictInt | None = Field(default=None, gt=0)
    channels: StrictInt | None = Field(default=None, gt=0)
    audio_stream_count: StrictInt = Field(ge=0)


class AdmissionEvidence(PrivateCorpusContract):
    """Evidence required before an admitted-revision content read."""

    purpose: Literal["asr_model_evaluation"]
    purpose_approval_ref: PrivateReference
    legal_data_evidence_ref: PrivateReference
    attestation_ref: PrivateReference
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=SHA256_PATTERN)
    workspace_id: UUID
    actor_id: UUID
    read_grant_ref: PrivateReference
    process_grant_ref: PrivateReference
    access_policy_version: UUID
    policy_version: VersionIdentifier
    retention_profile_ref: PrivateReference
    audit_receipt_ref: PrivateReference
    allowed_environment_id: UUID
    admitted_at: datetime
    expires_at: datetime
    revocation_epoch: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_window(self) -> "AdmissionEvidence":
        if (
            self.admitted_at.tzinfo is None
            or self.admitted_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("admission timestamps must be timezone-aware")
        if self.expires_at <= self.admitted_at:
            raise ValueError("admission expiry must follow admission")
        if any(
            value.int == 0
            for value in (
                self.actor_id,
                self.source_object_id,
                self.source_revision_id,
                self.workspace_id,
                self.allowed_environment_id,
            )
        ):
            raise ValueError("admission identities must be non-zero")
        return self


class ClusterLineage(PrivateCorpusContract):
    meeting_cluster: UUID
    source_lineage_cluster: UUID
    customer_project_cluster: UUID
    speaker_clusters: tuple[UUID, ...] = ()


class CoverageAnnotations(PrivateCorpusContract):
    tags: tuple[VersionIdentifier, ...] = ()
    expected_annotation_spans: StrictInt = Field(ge=0)
    missing_label_count: StrictInt = Field(ge=0)
    unalignable_segment_count: StrictInt = Field(ge=0)
    insufficient_categories: tuple[VersionIdentifier, ...] = ()

    @model_validator(mode="after")
    def validate_labels(self) -> "CoverageAnnotations":
        if self.missing_label_count + self.unalignable_segment_count > self.expected_annotation_spans:
            raise ValueError("missing and unalignable labels exceed expected spans")
        return self


class PreprocessingDerivative(PrivateCorpusContract):
    derivative_ref: PrivateReference
    sha256: str = Field(pattern=SHA256_PATTERN)
    profile_version: VersionIdentifier
    parent_source_revision_id: UUID
    split: CorpusSplit


class SourceRevisionBinding(PrivateCorpusContract):
    """Non-resolving source identity; excludes connector names, URLs and paths."""

    source_object_id: UUID
    source_revision_id: UUID
    source_revision_number: StrictInt = Field(ge=1)
    source_revision_digest: str = Field(pattern=SHA256_PATTERN)
    workspace_id: UUID
    environment_id: UUID
    media_type: str = Field(pattern=r"^audio/[a-z0-9.+-]+$")
    byte_length: StrictInt = Field(ge=0)


class AudioDigestVerification(PrivateCorpusContract):
    """SHA-256 evidence produced only after authorized exact-revision streaming."""

    audio_sha256: str = Field(pattern=SHA256_PATTERN)
    source_revision_id: UUID
    byte_length: StrictInt = Field(ge=0)
    verified_at: datetime
    audit_receipt_ref: PrivateReference

    @model_validator(mode="after")
    def validate_verification_time(self) -> "AudioDigestVerification":
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("verified_at must be timezone-aware")
        return self


class PrivateCorpusRecord(PrivateCorpusContract):
    record_id: UUID
    source_binding: SourceRevisionBinding
    workspace_id: UUID
    environment_id: UUID
    admission: AdmissionEvidence
    audio_verification: AudioDigestVerification | None = None
    media: MediaMetadata
    original_storage_ref: PrivateReference
    derived_staging_profile: VersionIdentifier
    annotation_ref: PrivateReference
    annotation_sha256: str = Field(pattern=SHA256_PATTERN)
    split: CorpusSplit
    split_assignment_version: VersionIdentifier
    lineage: ClusterLineage
    correlated_artifact_clusters: tuple[UUID, ...] = ()
    coverage: CoverageAnnotations
    preprocessing_derivatives: tuple[PreprocessingDerivative, ...] = ()

    @model_validator(mode="after")
    def validate_source_binding(self) -> "PrivateCorpusRecord":
        binding = self.source_binding
        if (self.workspace_id, self.environment_id) != (
            binding.workspace_id,
            binding.environment_id,
        ):
            raise ValueError("record scope does not match source")
        if self.admission.allowed_environment_id != self.environment_id:
            raise ValueError("admission environment does not match record")
        if (
            self.admission.source_object_id,
            self.admission.source_revision_id,
            self.admission.source_revision_digest,
            self.admission.workspace_id,
        ) != (
            binding.source_object_id,
            binding.source_revision_id,
            binding.source_revision_digest,
            binding.workspace_id,
        ):
            raise ValueError("admission evidence is not bound to exact source revision")
        if self.media.byte_length != binding.byte_length or self.media.media_type != binding.media_type:
            raise ValueError("media metadata differs from admitted revision")
        if self.audio_verification is not None and (
            self.audio_verification.source_revision_id != binding.source_revision_id
            or self.audio_verification.byte_length != binding.byte_length
        ):
            raise ValueError("audio digest verification is not bound to exact source revision")
        for derivative in self.preprocessing_derivatives:
            if derivative.parent_source_revision_id != binding.source_revision_id:
                raise ValueError("derivative must retain exact source revision lineage")
            if derivative.split is not self.split:
                raise ValueError("derivative cannot cross corpus split")
        return self


class PrivateCorpusManifest(PrivateCorpusContract):
    schema_version: Literal["r2-private-asr-evaluation-dataset:v1"] = PRIVATE_ASR_DATASET_SCHEMA
    dataset_id: UUID
    version: VersionIdentifier
    created_at: datetime
    purpose: Literal["asr_model_evaluation"]
    purpose_approval_ref: PrivateReference
    policy_version: VersionIdentifier
    protocol_decision_ref: PrivateReference | None = None
    workspace_id: UUID
    environment_id: UUID
    storage_profile_id: VersionIdentifier
    retention_profile_ref: PrivateReference
    evaluator_schema: VersionIdentifier
    evaluator_version: VersionIdentifier
    normalizer_version: VersionIdentifier
    split_method_version: VersionIdentifier
    lineage_key_version: VersionIdentifier
    records: tuple[PrivateCorpusRecord, ...]
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_release(self) -> "PrivateCorpusManifest":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if len({record.record_id for record in self.records}) != len(self.records):
            raise ValueError("record identities must be unique")
        if any(
            (record.workspace_id, record.environment_id)
            != (self.workspace_id, self.environment_id)
            for record in self.records
        ):
            raise ValueError("all records must share manifest scope")
        if any(
            record.admission.purpose_approval_ref != self.purpose_approval_ref
            or record.admission.policy_version != self.policy_version
            for record in self.records
        ):
            raise ValueError("record purpose or policy approval differs from manifest")
        validate_split_isolation(self.records)
        if self.manifest_sha256 != self.calculate_digest():
            raise ValueError("manifest digest does not match canonical payload")
        return self

    def calculate_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def build_private_manifest(**payload: object) -> PrivateCorpusManifest:
    """Create a release with a canonical digest, then validate its full schema."""

    unknown = set(payload) - set(PrivateCorpusManifest.model_fields)
    if unknown:
        raise ValueError("manifest contains unsupported fields")
    candidate = PrivateCorpusManifest.model_construct(manifest_sha256="0" * 64, **payload)
    complete_payload = candidate.model_dump(mode="json", exclude={"manifest_sha256"})
    digest = hashlib.sha256(
        json.dumps(complete_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PrivateCorpusManifest.model_validate(
        {
            **candidate.model_dump(mode="python", exclude={"manifest_sha256"}),
            "manifest_sha256": digest,
        }
    )


class TranscriptSegment(PrivateCorpusContract):
    timeline: Literal["source_original"] = "source_original"
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    human_text: str | None = Field(default=None, max_length=20_000)
    speaker_label: VersionIdentifier | None = None
    unintelligible: StrictBool = False
    uncertain: StrictBool = False
    overlap: StrictBool = False
    verified_silence: StrictBool = False
    unalignable: StrictBool = False

    @model_validator(mode="after")
    def validate_span(self) -> "TranscriptSegment":
        if self.end_ms <= self.start_ms:
            raise ValueError("segment end must follow start")
        if self.unintelligible and self.human_text is not None:
            raise ValueError("unintelligible spans cannot contain guessed text")
        if self.verified_silence and self.human_text is not None:
            raise ValueError("verified silence cannot contain transcript text")
        if self.human_text == "":
            raise ValueError("empty transcript text must be represented as a missing label")
        return self


class SemanticAnnotation(PrivateCorpusContract):
    timeline: Literal["source_original"] = "source_original"
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    category: Literal[
        "terminology", "amount", "currency", "quantity", "unit", "date",
        "deadline", "negation", "obligation", "condition", "constraint",
        "permission", "prohibition",
    ]
    label: VersionIdentifier
    critical: StrictBool = False

    @model_validator(mode="after")
    def validate_span(self) -> "SemanticAnnotation":
        if self.end_ms <= self.start_ms:
            raise ValueError("semantic annotation end must follow start")
        return self


class AnnotationReview(PrivateCorpusContract):
    annotation_version: StrictInt = Field(ge=1)
    reviewer_id: UUID
    authorization_ref: PrivateReference
    status: Literal["approved", "rejected", "adjudicated"]
    reviewed_at: datetime
    rationale_ref: PrivateReference | None = None

    @model_validator(mode="after")
    def validate_review_time(self) -> "AnnotationReview":
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        return self


class AnnotationCorrection(PrivateCorpusContract):
    correction_id: UUID
    version: StrictInt = Field(ge=2)
    predecessor_annotation_id: UUID
    created_by: UUID
    created_at: datetime
    reason_ref: PrivateReference
    segments: tuple[TranscriptSegment, ...]

    @model_validator(mode="after")
    def validate_correction(self) -> "AnnotationCorrection":
        if self.predecessor_annotation_id == self.correction_id:
            raise ValueError("correction cannot precede itself")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("correction created_at must be timezone-aware")
        return self


class GroundTruthAnnotation(PrivateCorpusContract):
    annotation_id: UUID
    version: StrictInt = Field(ge=1)
    predecessor_annotation_id: UUID | None = None
    source_record_id: UUID
    status: AnnotationStatus
    authored_by: UUID
    author_authorization_ref: PrivateReference
    authored_at: datetime
    text_origin: Literal["human_authored", "asr_assisted"] = "human_authored"
    segments: tuple[TranscriptSegment, ...]
    semantic_annotations: tuple[SemanticAnnotation, ...] = ()
    reviews: tuple[AnnotationReview, ...] = ()
    corrections: tuple[AnnotationCorrection, ...] = ()
    independently_human_validated: StrictBool = False
    asr_text_approved_as_ground_truth: StrictBool = False

    @model_validator(mode="after")
    def validate_approval(self) -> "GroundTruthAnnotation":
        if (self.version == 1) != (self.predecessor_annotation_id is None):
            raise ValueError("annotation version/predecessor chain is invalid")
        if self.predecessor_annotation_id == self.annotation_id:
            raise ValueError("annotation cannot precede itself")
        if self.asr_text_approved_as_ground_truth and not self.independently_human_validated:
            raise ValueError("ASR text requires independent human validation")
        if self.status is AnnotationStatus.APPROVED:
            if not self.independently_human_validated:
                raise ValueError("approved ground truth requires independent human validation")
            if not any(
                review.status == "approved"
                and review.reviewer_id != self.authored_by
                and review.annotation_version == self.version
                for review in self.reviews
            ):
                raise ValueError("approved ground truth requires independent authorized review")
        if self.status is AnnotationStatus.ADJUDICATED and not any(
            review.status == "adjudicated"
            and review.reviewer_id != self.authored_by
            and review.annotation_version == self.version
            for review in self.reviews
        ):
            raise ValueError("adjudicated ground truth requires an independent adjudicator")
        if self.authored_at.tzinfo is None or self.authored_at.utcoffset() is None:
            raise ValueError("annotation authored_at must be timezone-aware")
        if self.text_origin == "asr_assisted" and self.status is AnnotationStatus.APPROVED and not self.independently_human_validated:
            raise ValueError("ASR-assisted ground truth requires independent human validation")
        if any(segment.end_ms <= segment.start_ms for segment in self.segments):
            raise ValueError("invalid segment timestamp")
        return self


class CoverageCounts(PrivateCorpusContract):
    eligible_observations: StrictInt = Field(ge=0)
    annotated_observations: StrictInt = Field(ge=0)
    scorable_observations: StrictInt = Field(ge=0)
    measured_observations: StrictInt = Field(ge=0)
    independent_lineages: StrictInt = Field(ge=0)
    annotation_spans: StrictInt = Field(ge=0)
    missing_labels: StrictInt = Field(ge=0)
    unalignable_segments: StrictInt = Field(ge=0)
    insufficient_categories: tuple[VersionIdentifier, ...] = ()

    @model_validator(mode="after")
    def validate_denominators(self) -> "CoverageCounts":
        if self.annotated_observations > self.eligible_observations:
            raise ValueError("annotated observations exceed eligible denominator")
        if self.scorable_observations > self.annotated_observations:
            raise ValueError("scorable observations exceed annotated denominator")
        if self.measured_observations > self.scorable_observations:
            raise ValueError("measured observations exceed scorable denominator")
        if self.independent_lineages > self.eligible_observations:
            raise ValueError("lineages exceed eligible observations")
        if self.missing_labels + self.unalignable_segments > self.annotation_spans:
            raise ValueError("coverage exclusions exceed annotation spans")
        return self


class CoverageObservation(PrivateCorpusContract):
    """One denominator-preserving coverage row for an eligible observation."""

    lineage_id: UUID
    eligible: StrictBool = True
    annotated: StrictBool = False
    scorable: StrictBool = False
    measured: StrictBool = False
    annotation_spans: StrictInt = Field(ge=0)
    missing_labels: StrictInt = Field(ge=0)
    unalignable_segments: StrictInt = Field(ge=0)
    insufficient_categories: tuple[VersionIdentifier, ...] = ()

    @model_validator(mode="after")
    def validate_progression(self) -> "CoverageObservation":
        if self.annotated and not self.eligible:
            raise ValueError("ineligible observation cannot be annotated")
        if self.scorable and not self.annotated:
            raise ValueError("unannotated observation cannot be scorable")
        if self.measured and not self.scorable:
            raise ValueError("unscorable observation cannot be measured")
        if self.missing_labels + self.unalignable_segments > self.annotation_spans:
            raise ValueError("missing and unalignable labels exceed observation spans")
        return self


class SanitizedAggregate(PrivateCorpusContract):
    category: AggregateCategory
    metric_name: AggregateMetric
    denominator: StrictInt = Field(ge=0)
    numerator: StrictInt = Field(ge=0)
    rate_millionths: StrictInt | None = Field(default=None, ge=0, le=1_000_000)


class PrivacyPolicy(PrivateCorpusContract):
    policy_version: VersionIdentifier
    minimum_cell_size: StrictInt = Field(gt=0)
    approved_categories: tuple[AggregateCategory, ...]
    approved_metric_names: tuple[AggregateMetric, ...]
    allow_rates: StrictBool = True
    allow_sufficiency: StrictBool = False


class ApprovedPrivacyPolicy(PrivateCorpusContract):
    """Scope-bound approval record resolved by an authorization adapter."""

    policy: PrivacyPolicy
    approval_reference: PrivateReference
    approval_version: VersionIdentifier
    workspace_id: UUID
    environment_id: UUID
    approved_by: UUID
    approved_at: datetime
    expires_at: datetime
    revoked: StrictBool = False

    @model_validator(mode="after")
    def validate_approval(self) -> "ApprovedPrivacyPolicy":
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("privacy policy approval time must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("privacy policy expiry must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("privacy policy expiry must follow approval")
        if self.approved_by.int == 0 or self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("privacy policy approval identity must be non-zero")
        return self


class SanitizedCorpusReport(PrivateCorpusContract):
    schema_version: Literal["r2-private-asr-sanitized-report:v1"]
    privacy_policy_version: VersionIdentifier
    privacy_policy_approval_version: VersionIdentifier
    generated_at: datetime = Field(exclude=True, repr=False)
    privacy_policy_approval: ApprovedPrivacyPolicy = Field(exclude=True, repr=False)
    workspace_id: UUID = Field(exclude=True, repr=False)
    environment_id: UUID = Field(exclude=True, repr=False)
    privacy_policy: PrivacyPolicy
    status: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    sufficiency: QualificationSufficiency
    aggregates: tuple[SanitizedAggregate, ...]
    metric_names: tuple[AggregateMetric, ...] = ()

    @model_validator(mode="after")
    def validate_safe_aggregate(self) -> "SanitizedCorpusReport":
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("report generated_at must be timezone-aware")
        if self.privacy_policy_version != self.privacy_policy.policy_version:
            raise ValueError("report policy version does not match privacy policy")
        approval = self.privacy_policy_approval
        if (
            approval.policy != self.privacy_policy
            or approval.approval_version != self.privacy_policy_approval_version
            or approval.workspace_id != self.workspace_id
            or approval.environment_id != self.environment_id
            or approval.revoked
            or approval.approved_at > self.generated_at
            or approval.expires_at <= self.generated_at
        ):
            raise ValueError("report is not bound to a current approved privacy policy")
        if not self.aggregates:
            raise ValueError("sanitized report requires aggregates")
        for aggregate in self.aggregates:
            if aggregate.category not in self.privacy_policy.approved_categories:
                raise ValueError("report contains an unapproved category")
            if aggregate.metric_name not in self.privacy_policy.approved_metric_names:
                raise ValueError("report contains an unapproved metric")
            if aggregate.denominator < self.privacy_policy.minimum_cell_size:
                raise ValueError("report contains a small-cell disclosure")
            if aggregate.numerator > aggregate.denominator:
                raise ValueError("aggregate numerator exceeds denominator")
            if (
                aggregate.numerator < self.privacy_policy.minimum_cell_size
                or aggregate.denominator - aggregate.numerator
                < self.privacy_policy.minimum_cell_size
            ):
                raise ValueError("report contains a rare numerator or complement cell")
            expected_rate = (
                aggregate.numerator * 1_000_000 // aggregate.denominator
                if self.privacy_policy.allow_rates
                else None
            )
            if aggregate.rate_millionths != expected_rate:
                raise ValueError("aggregate rate does not match privacy policy")
        aggregate_metrics = {item.metric_name for item in self.aggregates}
        if aggregate_metrics != set(self.metric_names):
            raise ValueError("report metric list does not match aggregate metrics")
        return self


def validate_split_isolation(records: tuple[PrivateCorpusRecord, ...]) -> None:
    """Reject connected source, meeting, customer/project and speaker groups."""

    keys_by_split: dict[CorpusSplit, set[tuple[str, object]]] = {
        split: set() for split in CorpusSplit
    }
    revision_splits: dict[UUID, CorpusSplit] = {}
    object_splits: dict[UUID, CorpusSplit] = {}
    artifact_splits: dict[UUID, CorpusSplit] = {}
    content_splits: dict[str, CorpusSplit] = {}
    for record in records:
        for identity, seen in (
            (record.source_binding.source_revision_id, revision_splits),
            (record.source_binding.source_object_id, object_splits),
        ):
            prior = seen.setdefault(identity, record.split)
            if prior is not record.split:
                raise ValueError("source lineage leaks across corpus splits")
        lineage = record.lineage
        keys = {
            ("meeting", lineage.meeting_cluster),
            ("source", lineage.source_lineage_cluster),
            ("customer_project", lineage.customer_project_cluster),
            *(("speaker", speaker) for speaker in lineage.speaker_clusters),
        }
        for key in keys:
            other_split = CorpusSplit.QUALIFICATION if record.split is CorpusSplit.DEVELOPMENT else CorpusSplit.DEVELOPMENT
            if key in keys_by_split[other_split]:
                raise ValueError("correlated lineage leaks across corpus splits")
            keys_by_split[record.split].add(key)
        for artifact_cluster in record.correlated_artifact_clusters:
            prior = artifact_splits.setdefault(artifact_cluster, record.split)
            if prior is not record.split:
                raise ValueError("audio/transcript derivative leaks across corpus splits")
        content_digests = {
            digest
            for digest in (
                None if record.audio_verification is None else record.audio_verification.audio_sha256,
                record.annotation_sha256,
                *(item.sha256 for item in record.preprocessing_derivatives),
            )
            if digest is not None
        }
        for digest in content_digests:
            prior = content_splits.setdefault(digest, record.split)
            if prior is not record.split:
                raise ValueError("audio/transcript content digest leaks across corpus splits")
        for derivative in record.preprocessing_derivatives:
            if derivative.split is not record.split:
                raise ValueError("clips and preprocessing derivatives cannot cross splits")


def qualification_sufficiency(*, protocol_decision_present: bool) -> QualificationSufficiency:
    """No minima are inferred; a decision still does not imply measured evidence."""

    del protocol_decision_present
    return QualificationSufficiency.NOT_VERIFIED


def calculate_coverage(observations: tuple[CoverageObservation, ...]) -> CoverageCounts:
    """Count every eligible observation and retain annotation exclusions."""

    eligible = tuple(item for item in observations if item.eligible)
    return CoverageCounts(
        eligible_observations=len(eligible),
        annotated_observations=sum(item.annotated for item in eligible),
        scorable_observations=sum(item.scorable for item in eligible),
        measured_observations=sum(item.measured for item in eligible),
        independent_lineages=len({item.lineage_id for item in eligible}),
        annotation_spans=sum(item.annotation_spans for item in eligible),
        missing_labels=sum(item.missing_labels for item in eligible),
        unalignable_segments=sum(item.unalignable_segments for item in eligible),
        insufficient_categories=tuple(sorted({
            category for item in eligible for category in item.insufficient_categories
        })),
    )


__all__ = [
    "AggregateCategory", "AggregateMetric", "AnnotationCorrection", "AnnotationReview", "AnnotationStatus", "AudioDigestVerification", "ClusterLineage",
    "CoverageAnnotations", "CoverageCounts", "CoverageObservation", "CorpusSplit", "GroundTruthAnnotation",
    "MediaMetadata", "PRIVATE_ASR_DATASET_SCHEMA", "PreprocessingDerivative",
    "ApprovedPrivacyPolicy", "PrivacyPolicy", "PrivateCorpusManifest", "PrivateCorpusRecord", "PrivateReference",
    "QualificationSufficiency", "SanitizedAggregate",
    "SanitizedCorpusReport", "SemanticAnnotation", "TranscriptSegment",
    "SourceRevisionBinding",
    "build_private_manifest", "calculate_coverage", "qualification_sufficiency", "validate_split_isolation",
]
