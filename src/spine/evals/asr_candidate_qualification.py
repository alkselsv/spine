"""Provider-neutral, offline qualification contract for Issue #683.

The real GigaAM and faster-whisper runs are deliberately operator-owned.  This
module makes their evidence shape executable without importing either runtime:
CI can validate the frozen identities, independent gates and report boundary,
while a separately provisioned runner can later supply measured observations.
Missing model assets or host evidence therefore produce ``NOT_VERIFIED`` rather
than a synthetic or fake-backed PASS.
"""

from __future__ import annotations

from enum import Enum
import hashlib
import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.evals.transcription_russian_v1 import (
    canonical_russian_transcription_dataset_json,
    load_russian_transcription_v1_dataset,
)

ASR_CANDIDATE_QUALIFICATION_SCHEMA = "asr-candidate-qualification:v1"
QUALIFICATION_TICKET = 683


class QualificationStatus(str, Enum):
    """The only outcomes a candidate qualification gate may publish."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIED = "NOT_VERIFIED"


class Coverage(DefinitionModel):
    """Annotation/alignment coverage for one independent measurement."""

    annotated: StrictInt = Field(ge=0)
    eligible: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def annotated_cannot_exceed_eligible(self) -> "Coverage":
        if self.annotated > self.eligible:
            raise ValueError("annotated coverage cannot exceed eligible coverage")
        return self


class CandidateConfiguration(DefinitionModel):
    """Frozen candidate decoding and preprocessing configuration."""

    language: Literal["ru"]
    translation_enabled: StrictBool = False
    language_detection_enabled: StrictBool = False
    vad_profile: VersionIdentifier
    vocabulary_hints_profile: VersionIdentifier
    preprocessing_profile: VersionIdentifier
    chunking_profile: VersionIdentifier
    precision: VersionIdentifier
    configuration_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$|^$")

    @model_validator(mode="after")
    def fill_configuration_digest(self) -> "CandidateConfiguration":
        if self.configuration_digest:
            return self
        payload = self.model_dump(mode="json", exclude={"configuration_digest"})
        digest = hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "configuration_digest", digest)
        return self


class CandidateProfile(DefinitionModel):
    """Identity and provenance required before a model can be qualified."""

    candidate_id: Literal["gigaam-v3-rnnt", "whisper-large-v3-faster-whisper"]
    adapter_reference: VersionIdentifier
    model_reference: VersionIdentifier
    source_repository: str = Field(min_length=1)
    source_revision: str | None = None
    runtime_reference: VersionIdentifier
    runtime_revision: str | None = None
    checkpoint_reference: VersionIdentifier
    checkpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    asset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dependency_lock_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    configuration: CandidateConfiguration

    @property
    def language(self) -> str:
        return self.configuration.language

    @property
    def translation_enabled(self) -> bool:
        return self.configuration.translation_enabled

    @property
    def language_detection_enabled(self) -> bool:
        return self.configuration.language_detection_enabled


class SoftwareVersion(DefinitionModel):
    name: VersionIdentifier
    version: str = Field(min_length=1)


class HostEvidence(DefinitionModel):
    """The benchmark host identity, kept separate from production capacity."""

    cpu: str | None = None
    gpu: str | None = None
    ram_mb: StrictInt | None = Field(default=None, ge=0)
    vram_mb: StrictInt | None = Field(default=None, ge=0)
    operating_system: str | None = None
    software_versions: tuple[SoftwareVersion, ...] = ()
    precision: VersionIdentifier | None = None
    network_egress_disabled: StrictBool | None = None
    production_capacity_claim: StrictBool = False


class PrerequisiteCheck(DefinitionModel):
    name: VersionIdentifier
    status: QualificationStatus
    detail: str = Field(min_length=1)


class GateResult(DefinitionModel):
    """One independent #661 gate, including category and coverage evidence."""

    gate_id: VersionIdentifier
    categories: tuple[VersionIdentifier, ...] = Field(min_length=1)
    status: QualificationStatus
    error_count: StrictInt | None = Field(default=None, ge=0)
    coverage: Coverage
    observed_value: StrictFloat | None = None
    threshold: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class MetricObservation(DefinitionModel):
    """A separately reportable ASR/operational measurement."""

    status: QualificationStatus
    value: StrictFloat | None = None
    unit: VersionIdentifier
    error_count: StrictInt | None = Field(default=None, ge=0)
    coverage: Coverage
    detail: str = Field(min_length=1)


class AssetEvidence(DefinitionModel):
    """One staged model asset, identified without publishing a host path."""

    relative_path: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,511}$",
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: StrictInt = Field(gt=0)


class AnnotationCoverageEvidence(DefinitionModel):
    """One dataset category's eligibility and approved sufficiency evidence."""

    gate_id: VersionIdentifier
    category_id: VersionIdentifier
    evidence_source: Literal["fixture_annotations", "operator_evidence"]
    annotated_count: StrictInt = Field(ge=0)
    eligible_count: StrictInt = Field(ge=0)
    minimum_annotation_count: StrictInt | None = Field(default=None, gt=0)
    status: QualificationStatus


class SegmentObservation(DefinitionModel):
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    text: str = Field(min_length=1)


class CandidateCaseObservation(DefinitionModel):
    case_id: VersionIdentifier
    input_format: VersionIdentifier = "wav_pcm"
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript: str
    segments: tuple[SegmentObservation, ...]
    preprocessing_ms: StrictInt = Field(ge=0)
    inference_ms: StrictInt = Field(ge=0)
    reassembly_ms: StrictInt = Field(ge=0)
    matched_reference_segments: StrictInt = Field(default=0, ge=0)
    missing_reference_segments: StrictInt = Field(default=0, ge=0)
    unmatched_prediction_segments: StrictInt = Field(default=0, ge=0)
    timestamps_supported: StrictBool = True
    chunk_boundaries_verified: StrictBool = False
    partial_recovery_replay_observed: StrictBool = False

    @model_validator(mode="after")
    def validate_segment_times(self) -> "CandidateCaseObservation":
        if any(segment.end_ms <= segment.start_ms for segment in self.segments):
            raise ValueError("candidate segment end must follow start")
        return self


_GATE_DEFINITIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "russian_transcription",
        ("clean", "noisy", "overlap", "long_form", "terminology"),
        "WER <= 15% and CER <= 8% on the full frozen qualification set",
    ),
    (
        "critical_business_fragments",
        (
            "amount_currency",
            "quantities",
            "dates_deadlines",
            "units",
            "negations",
            "obligations",
            "permissions",
            "conditions",
            "constraints",
        ),
        "exact match >= 98% for every annotated critical-fragment category",
    ),
    (
        "adversarial_semantic_inversions",
        ("negation", "bounds", "permission", "inclusion", "obligation"),
        "zero semantic inversions on the frozen adversarial set",
    ),
    (
        "terminology",
        ("all_annotated_terms",),
        "span precision >= 95% and recall >= 95%",
    ),
    (
        "segment_timestamps",
        ("boundary_error", "alignment_coverage"),
        "boundary-error P50 <= 500 ms and P95 <= 1500 ms with sufficient coverage",
    ),
    (
        "silence",
        ("pure_silence", "verified_silence_intervals"),
        "zero words and zero non-empty segments wholly within verified silence",
    ),
    (
        "long_form_chunking",
        ("chunk_boundaries", "pause_handling", "reassembly", "partial_recovery"),
        "ordered original-timeline output with explicit recovery evidence",
    ),
    (
        "preprocessing_effects",
        ("baseline", "preprocessing_comparison"),
        "preprocessing effects are measured separately and no hidden correction is used",
    ),
    (
        "performance",
        (
            "cold_start",
            "warm_inference",
            "preprocessing",
            "inference",
            "reassembly",
            "end_to_end",
            "resources",
        ),
        "report cold/warm/end-to-end timing, RTF and peak RAM/VRAM on the recorded host",
    ),
    (
        "offline_integrity",
        (
            "asset_loading",
            "source_provenance",
            "dependency_provenance",
            "supported_formats",
            "network_egress",
        ),
        "offline assets load by hash and inference has no network egress",
    ),
)

_METRIC_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("wer", "ratio"),
    ("cer", "ratio"),
    ("critical_fragment_accuracy", "ratio"),
    ("semantic_inversions", "count"),
    ("terminology_precision", "ratio"),
    ("terminology_recall", "ratio"),
    ("segment_timestamp_p50_ms", "milliseconds"),
    ("segment_timestamp_p95_ms", "milliseconds"),
    ("silence_hallucinations", "count"),
    ("preprocessing_ms", "milliseconds"),
    ("inference_ms", "milliseconds"),
    ("reassembly_ms", "milliseconds"),
    ("cold_start_ms", "milliseconds"),
    ("warm_inference_rtf", "ratio"),
    ("end_to_end_rtf", "ratio"),
    ("peak_ram_mb", "megabytes"),
    ("peak_vram_mb", "megabytes"),
)


class CandidateQualificationReport(DefinitionModel):
    """Machine-readable, disclosure-safe result for one ASR candidate."""

    schema_version: Literal[ASR_CANDIDATE_QUALIFICATION_SCHEMA] = (
        ASR_CANDIDATE_QUALIFICATION_SCHEMA
    )
    ticket: Literal[QUALIFICATION_TICKET] = QUALIFICATION_TICKET
    candidate: CandidateProfile
    dataset_id: VersionIdentifier
    dataset_version: VersionIdentifier
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_mode: Literal["synthetic-only-offline"] = "synthetic-only-offline"
    production_recordings_used: StrictBool = False
    external_asr_calls: StrictBool = False
    host: HostEvidence
    prerequisites: tuple[PrerequisiteCheck, ...] = Field(min_length=1)
    gates: tuple[GateResult, ...] = Field(min_length=1)
    metrics: dict[str, MetricObservation]
    asset_inventory: tuple[AssetEvidence, ...] = ()
    annotation_coverage: tuple[AnnotationCoverageEvidence, ...] = ()
    no_egress_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    case_observations: tuple[CandidateCaseObservation, ...] = ()
    unsupported_capabilities: tuple[VersionIdentifier, ...]
    overall_status: QualificationStatus
    downstream_boundary: str = Field(min_length=1)
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def enforce_qualification_boundaries(self) -> "CandidateQualificationReport":
        if self.production_recordings_used:
            raise ValueError(
                "production recordings are forbidden for this qualification"
            )
        if self.external_asr_calls:
            raise ValueError("external ASR calls are forbidden for this qualification")
        if self.host.production_capacity_claim:
            raise ValueError("benchmark host evidence cannot claim production capacity")
        if self.overall_status is QualificationStatus.PASS:
            required_identity = (
                self.candidate.source_revision,
                self.candidate.runtime_revision,
                self.candidate.checkpoint_sha256,
                self.candidate.asset_sha256,
                self.candidate.dependency_lock_digest,
            )
            if any(value is None for value in required_identity):
                raise ValueError("a PASS requires complete candidate provenance")
            if any(gate.status is not QualificationStatus.PASS for gate in self.gates):
                raise ValueError("a PASS requires every independent gate to pass")
            if any(item.status is not QualificationStatus.PASS for item in self.prerequisites):
                raise ValueError("a PASS requires every qualification prerequisite")
            if not self.asset_inventory or self.no_egress_evidence_sha256 is None:
                raise ValueError("a PASS requires verified asset and no-egress evidence")
            if not self.annotation_coverage or any(
                item.status is not QualificationStatus.PASS
                for item in self.annotation_coverage
                if item.evidence_source == "fixture_annotations"
            ):
                raise ValueError("a PASS requires complete, sufficient dataset category coverage")
        return self


@lru_cache(maxsize=1)
def candidate_profiles() -> tuple[CandidateProfile, ...]:
    """Return the two provider-specific profiles under qualification."""

    return (
        CandidateProfile(
            candidate_id="gigaam-v3-rnnt",
            adapter_reference="gigaam-v3-rnnt-adapter:v1",
            model_reference="gigaam:v3_rnnt",
            source_repository="https://github.com/salute-developers/GigaAM",
            source_revision=None,
            runtime_reference="official-gigaam-runtime:v1",
            runtime_revision=None,
            checkpoint_reference="v3_rnnt",
            checkpoint_sha256=None,
            asset_sha256=None,
            dependency_lock_digest=None,
            configuration=CandidateConfiguration(
                language="ru",
                vad_profile="disabled-baseline:v1",
                vocabulary_hints_profile="disabled-baseline:v1",
                preprocessing_profile="pcm-s16le-mono-16khz:v1",
                chunking_profile="gigaam-25s-development-frozen:v1",
                precision="benchmark-host-selected:v1",
            ),
        ),
        CandidateProfile(
            candidate_id="whisper-large-v3-faster-whisper",
            adapter_reference="whisper-large-v3-faster-whisper-adapter:v1",
            model_reference="whisper:large-v3",
            source_repository="https://github.com/SYSTRAN/faster-whisper",
            source_revision=None,
            runtime_reference="faster-whisper-ctranslate2:v1",
            runtime_revision=None,
            checkpoint_reference="large-v3",
            checkpoint_sha256=None,
            asset_sha256=None,
            dependency_lock_digest=None,
            configuration=CandidateConfiguration(
                language="ru",
                vad_profile="disabled-baseline:v1",
                vocabulary_hints_profile="disabled-baseline:v1",
                preprocessing_profile="pcm-s16le-mono-16khz:v1",
                chunking_profile="whisper-large-v3-development-frozen:v1",
                precision="benchmark-host-selected:v1",
            ),
        ),
    )


def _not_verified_gate(
    gate_id: str,
    categories: tuple[str, ...],
    threshold: str,
    reason: str,
) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        categories=categories,
        status=QualificationStatus.NOT_VERIFIED,
        error_count=None,
        coverage=Coverage(annotated=0, eligible=0),
        observed_value=None,
        threshold=threshold,
        detail=reason,
    )


def _not_verified_metric(metric_id: str, unit: str, reason: str) -> MetricObservation:
    return MetricObservation(
        status=QualificationStatus.NOT_VERIFIED,
        value=None,
        unit=unit,
        error_count=None,
        coverage=Coverage(annotated=0, eligible=0),
        detail=f"{metric_id}: {reason}",
    )


def _dataset_digest() -> tuple[str, str, str]:
    dataset = load_russian_transcription_v1_dataset()
    canonical = canonical_russian_transcription_dataset_json(dataset)
    return (
        dataset.dataset_id,
        dataset.version,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def run_offline_candidate_qualification(
    *,
    reason: str = "Pinned model assets, isolated host measurements and operator-run observations are not provisioned in CI.",
) -> tuple[CandidateQualificationReport, ...]:
    """Build honest per-candidate reports without loading a model or making calls."""

    dataset_id, dataset_version, dataset_sha256 = _dataset_digest()
    reports: list[CandidateQualificationReport] = []
    for candidate in candidate_profiles():
        prerequisites = (
            PrerequisiteCheck(
                name="source_revision_pinned",
                status=QualificationStatus.NOT_VERIFIED,
                detail="The operator must record the exact official source commit before a model run.",
            ),
            PrerequisiteCheck(
                name="checkpoint_and_assets_hashed",
                status=QualificationStatus.NOT_VERIFIED,
                detail="The checkpoint and every staged asset require SHA-256 evidence.",
            ),
            PrerequisiteCheck(
                name="dependency_lock_recorded",
                status=QualificationStatus.NOT_VERIFIED,
                detail="The isolated candidate environment lock is not present in the repository.",
            ),
            PrerequisiteCheck(
                name="isolated_no_egress_host",
                status=QualificationStatus.NOT_VERIFIED,
                detail="CI does not run model qualification or prove host-level network isolation.",
            ),
            PrerequisiteCheck(
                name="frozen_qualification_observation",
                status=QualificationStatus.NOT_VERIFIED,
                detail="No candidate inference observations were supplied for the frozen dataset.",
            ),
        )
        gates = tuple(
            _not_verified_gate(gate_id, categories, threshold, reason)
            for gate_id, categories, threshold in _GATE_DEFINITIONS
        )
        metrics = {
            metric_id: _not_verified_metric(metric_id, unit, reason)
            for metric_id, unit in _METRIC_DEFINITIONS
        }
        reports.append(
            CandidateQualificationReport(
                candidate=candidate,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                dataset_sha256=dataset_sha256,
                host=HostEvidence(
                    network_egress_disabled=None,
                    production_capacity_claim=False,
                ),
                prerequisites=prerequisites,
                gates=gates,
                metrics=metrics,
                unsupported_capabilities=(
                    "word_timestamps",
                    "confidence",
                    "vad",
                    "diarization",
                    "speaker_identification",
                ),
                overall_status=QualificationStatus.NOT_VERIFIED,
                downstream_boundary=(
                    "Downstream requirements extraction is a separate qualification owned by #646/#682; "
                    "these reports contain ASR evidence only."
                ),
                limitations=(
                    reason,
                    "Synthetic or fake execution would not count as real candidate qualification.",
                    "No production capacity, cost, model winner or production approval is claimed.",
                ),
            )
        )
    return tuple(reports)


def main() -> None:
    """Print the safe CI report; operator runs must provide measured evidence separately."""

    print(
        json.dumps(
            [
                report.model_dump(mode="json")
                for report in run_offline_candidate_qualification()
            ],
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


__all__ = [
    "ASR_CANDIDATE_QUALIFICATION_SCHEMA",
    "AssetEvidence",
    "CandidateCaseObservation",
    "CandidateConfiguration",
    "CandidateProfile",
    "CandidateQualificationReport",
    "Coverage",
    "GateResult",
    "HostEvidence",
    "MetricObservation",
    "PrerequisiteCheck",
    "QualificationStatus",
    "SegmentObservation",
    "candidate_profiles",
    "main",
    "run_offline_candidate_qualification",
]
