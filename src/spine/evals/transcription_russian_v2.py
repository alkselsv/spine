"""Typed, versioned dataset contract for synthetic ASR qualification.

This schema is shared with the Issue #758 operator runner.  It describes
ground-truth evidence and coverage only; it never stores candidate hypotheses.
"""

from __future__ import annotations

from collections import defaultdict
from enum import Enum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import wave
from typing import Literal, Sequence

from pydantic import Field, StrictBool, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


TRANSCRIPTION_V2_SCHEMA = "russian-transcription-qualification:v2"
TRANSCRIPTION_DATASET_ID = "russian-meeting-transcription-synthetic"
TRANSCRIPTION_V1_SHA256 = "0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FIXTURE_PATH_PATTERN = r"^audio/[a-z0-9][a-z0-9-]*\.(wav|mp3|m4a)$"
_FORMAT_EXTENSION = {"wav_pcm": ".wav", "mp3_layer_iii": ".mp3", "m4a_aac_lc": ".m4a"}
_PRIVATE_MARKER = re.compile(
    r"(?i)(?:[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b\+?\d[\d ()-]{8,}\d\b|"
    r"\b(?:password|passwd|api[_ -]?key|secret|token)\s*[:=])"
)


class QualificationSplit(str, Enum):
    DEVELOPMENT = "development"
    QUALIFICATION = "qualification"


class SyntheticMediaFormat(str, Enum):
    WAV_PCM = "wav_pcm"
    MP3_LAYER_III = "mp3_layer_iii"
    M4A_AAC_LC = "m4a_aac_lc"


class CriticalSpanCategory(str, Enum):
    AMOUNT_CURRENCY = "amount_currency"
    QUANTITY = "quantity"
    DATE_DEADLINE = "date_deadline"
    UNIT = "unit"
    NEGATION = "negation"
    OBLIGATION = "obligation"
    PERMISSION = "permission"
    CONDITION = "condition"
    CONSTRAINT = "constraint"


class AdversarialDimension(str, Enum):
    NEGATION = "negation"
    BOUNDS = "bounds"
    PERMISSION = "permission"
    INCLUSION = "inclusion"
    OBLIGATION = "obligation"


class SyntheticAudioAsset(DefinitionModel):
    fixture_path: str = Field(pattern=_FIXTURE_PATH_PATTERN)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    media_format: SyntheticMediaFormat
    source_sample_rate_hz: StrictInt = Field(gt=0)
    channels: StrictInt = Field(gt=0)
    sample_width_bits: StrictInt = Field(ge=0)
    derived_from_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def extension_matches_media_format(self) -> "SyntheticAudioAsset":
        if not self.fixture_path.endswith(_FORMAT_EXTENSION[self.media_format.value]):
            raise ValueError("fixture extension does not match media format")
        if self.channels != 1:
            raise ValueError("qualification fixtures must contain one audio channel")
        if self.media_format is SyntheticMediaFormat.WAV_PCM and self.sample_width_bits not in {16, 24}:
            raise ValueError("WAV/PCM fixture width must be 16 or 24 bits")
        if self.media_format is SyntheticMediaFormat.MP3_LAYER_III and self.sample_width_bits != 0:
            raise ValueError("compressed MP3 fixtures use sample_width_bits=0")
        if self.media_format is SyntheticMediaFormat.M4A_AAC_LC and self.sample_width_bits != 0:
            raise ValueError("compressed M4A fixtures use sample_width_bits=0")
        return self


class SyntheticAudioGeneration(DefinitionModel):
    recipe: str = Field(min_length=1, max_length=512)
    voice_id: VersionIdentifier
    voice_asset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    synthesizer_revision: VersionIdentifier
    license_reference: str = Field(min_length=1, max_length=512)
    source_dataset_id: VersionIdentifier
    source_dataset_version: VersionIdentifier
    source_fixture_sha256: str = Field(pattern=_SHA256_PATTERN)


class PreprocessingEvidence(DefinitionModel):
    profile_id: VersionIdentifier
    target_sample_rate_hz: Literal[16000] = 16000
    target_channels: Literal[1] = 1
    source_audio_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_audio_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    derived_audio_path: str | None = Field(default=None, pattern=_FIXTURE_PATH_PATTERN)

    @model_validator(mode="after")
    def output_asset_is_paired(self) -> "PreprocessingEvidence":
        if (self.output_audio_sha256 is None) != (self.derived_audio_path is None):
            raise ValueError("preprocessing output path and digest must be supplied together")
        return self


class ReferenceSegment(DefinitionModel):
    segment_id: VersionIdentifier
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    reference_text: str = Field(min_length=1)
    permissible_variants: tuple[str, ...] = ()
    anonymous_speaker: VersionIdentifier

    @model_validator(mode="after")
    def has_valid_text_and_time(self) -> "ReferenceSegment":
        if self.end_ms <= self.start_ms:
            raise ValueError("segment end must follow start")
        if any(not variant.strip() for variant in self.permissible_variants):
            raise ValueError("permissible transcript variants cannot be blank")
        return self


class CriticalSpanAnnotation(DefinitionModel):
    annotation_id: VersionIdentifier
    segment_id: VersionIdentifier
    category: CriticalSpanCategory
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)
    expected_text: str = Field(min_length=1)
    semantic_value: str = Field(min_length=1)


class AdversarialMeaningAnnotation(DefinitionModel):
    annotation_id: VersionIdentifier
    segment_id: VersionIdentifier
    dimension: AdversarialDimension
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)
    expected_text: str = Field(min_length=1)
    reference_interpretation: str = Field(min_length=1)
    forbidden_inversion: str = Field(min_length=1)

    @model_validator(mode="after")
    def interpretation_differs_from_inversion(self) -> "AdversarialMeaningAnnotation":
        if self.reference_interpretation.strip() == self.forbidden_inversion.strip():
            raise ValueError("adversarial reference and forbidden inversion must differ")
        return self


class TerminologyAnnotation(DefinitionModel):
    annotation_id: VersionIdentifier
    segment_id: VersionIdentifier
    start_char: StrictInt = Field(ge=0)
    end_char: StrictInt = Field(gt=0)
    expected_text: str = Field(min_length=1)
    canonical_term: str = Field(min_length=1)
    permissible_variants: tuple[str, ...] = ()
    term_category: VersionIdentifier

    @model_validator(mode="after")
    def variants_are_nonblank(self) -> "TerminologyAnnotation":
        if any(not variant.strip() for variant in self.permissible_variants):
            raise ValueError("terminology variants cannot be blank")
        return self


class TimeInterval(DefinitionModel):
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def interval_is_positive(self) -> "TimeInterval":
        if self.end_ms <= self.start_ms:
            raise ValueError("time interval end must follow start")
        return self


class ChunkExpectation(DefinitionModel):
    chunk_id: VersionIdentifier
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    overlap_before_ms: StrictInt = Field(ge=0)
    expected_outcome: Literal["complete", "partial_failure"]
    failure_after_segment_id: VersionIdentifier | None = None

    @model_validator(mode="after")
    def valid_bounded_chunk(self) -> "ChunkExpectation":
        if self.end_ms <= self.start_ms:
            raise ValueError("chunk end must follow start")
        if self.end_ms - self.start_ms > 25_000:
            raise ValueError("qualification chunks cannot exceed 25 seconds")
        if self.expected_outcome == "partial_failure" and self.failure_after_segment_id is None:
            raise ValueError("partial failure must identify the last completed segment")
        if self.expected_outcome == "complete" and self.failure_after_segment_id is not None:
            raise ValueError("complete chunk cannot declare a failure checkpoint")
        return self


class ChunkRecoveryExpectation(DefinitionModel):
    recovery_id: VersionIdentifier
    failed_chunk_id: VersionIdentifier
    completed_segment_ids: tuple[VersionIdentifier, ...]
    resume_from_chunk_id: VersionIdentifier
    expected_final_segment_ids: tuple[VersionIdentifier, ...]

    @model_validator(mode="after")
    def recovery_preserves_completed_work(self) -> "ChunkRecoveryExpectation":
        if len(self.completed_segment_ids) != len(set(self.completed_segment_ids)):
            raise ValueError("completed recovery segment identities must be unique")
        if len(self.expected_final_segment_ids) != len(set(self.expected_final_segment_ids)):
            raise ValueError("final recovery segment identities must be unique")
        if not set(self.completed_segment_ids).issubset(self.expected_final_segment_ids):
            raise ValueError("recovery must preserve every completed segment")
        return self


class SyntheticQualificationCase(DefinitionModel):
    case_id: VersionIdentifier
    split: QualificationSplit
    lineage_group_id: VersionIdentifier
    duration_ms: StrictInt = Field(gt=0)
    audio: SyntheticAudioAsset
    format_variants: tuple[SyntheticAudioAsset, ...] = ()
    format_variant_preprocessing: tuple[PreprocessingEvidence, ...] = ()
    generation: SyntheticAudioGeneration
    preprocessing: PreprocessingEvidence
    expected_transcript: str = ""
    anonymous_speaker_policy: Literal["labels_only_never_resolve_identity"] = "labels_only_never_resolve_identity"
    synthetic_only: StrictBool = True
    contains_personal_data: StrictBool = False
    contains_credentials: StrictBool = False
    reference_segments: tuple[ReferenceSegment, ...] = ()
    critical_spans: tuple[CriticalSpanAnnotation, ...] = ()
    adversarial_labels: tuple[AdversarialMeaningAnnotation, ...] = ()
    terminology_spans: tuple[TerminologyAnnotation, ...] = ()
    verified_silence_intervals: tuple[TimeInterval, ...] = ()
    chunks: tuple[ChunkExpectation, ...] = ()
    chunk_recoveries: tuple[ChunkRecoveryExpectation, ...] = ()
    media_category: VersionIdentifier
    preprocessing_pair_id: VersionIdentifier | None = None
    external_call_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_evidence_lineage(self) -> "SyntheticQualificationCase":
        if not self.synthetic_only or self.contains_personal_data or self.contains_credentials:
            raise ValueError("qualification audio must be synthetic and privacy-safe")
        ids = [segment.segment_id for segment in self.reference_segments]
        if len(ids) != len(set(ids)):
            raise ValueError("reference segment identities must be unique")
        by_id = {segment.segment_id: segment for segment in self.reference_segments}
        variant_formats = [variant.media_format for variant in self.format_variants]
        if len(variant_formats) != len(set(variant_formats)):
            raise ValueError("input format variants must be unique")
        if self.audio.media_format in variant_formats:
            raise ValueError("format variant cannot duplicate the primary input format")
        if any(variant.derived_from_sha256 != self.generation.source_fixture_sha256 for variant in self.format_variants):
            raise ValueError("format variant lineage does not match its frozen source fixture")
        preprocessing_by_source = {item.source_audio_sha256: item for item in self.format_variant_preprocessing}
        variant_hashes = {variant.sha256 for variant in self.format_variants}
        if len(preprocessing_by_source) != len(self.format_variant_preprocessing):
            raise ValueError("format variant preprocessing sources must be unique")
        if set(preprocessing_by_source) != variant_hashes:
            raise ValueError("every format variant must have its own preprocessing evidence")
        if any(not segment.anonymous_speaker.startswith("speaker-") for segment in self.reference_segments):
            raise ValueError("speaker labels must remain anonymous")
        if " ".join(segment.reference_text for segment in self.reference_segments) != self.expected_transcript:
            raise ValueError("expected transcript must match ordered reference segments")
        previous_start = -1
        for segment in self.reference_segments:
            if segment.end_ms > self.duration_ms:
                raise ValueError("reference segment exceeds audio duration")
            if segment.start_ms < previous_start:
                raise ValueError("reference segments must be ordered on the source timeline")
            previous_start = segment.start_ms
        for annotation in (*self.critical_spans, *self.adversarial_labels, *self.terminology_spans):
            segment = by_id.get(annotation.segment_id)
            if segment is None:
                raise ValueError("annotation references a fabricated segment")
            if annotation.end_char <= annotation.start_char:
                raise ValueError("annotation end must follow start")
            if annotation.end_char > len(segment.reference_text):
                raise ValueError("annotation span exceeds reference text")
            if segment.reference_text[annotation.start_char : annotation.end_char] != annotation.expected_text:
                raise ValueError("span text must match the exact reference transcript")
        annotation_ids = [item.annotation_id for item in (*self.critical_spans, *self.adversarial_labels, *self.terminology_spans)]
        if len(annotation_ids) != len(set(annotation_ids)):
            raise ValueError("annotation identities must be unique")
        self._reject_conflicting_annotations()
        for interval in self.verified_silence_intervals:
            if interval.end_ms > self.duration_ms:
                raise ValueError("silence interval exceeds audio duration")
            if any(interval.start_ms < segment.end_ms and segment.start_ms < interval.end_ms for segment in self.reference_segments):
                raise ValueError("silence interval overlaps speech")
        silence_intervals = sorted(self.verified_silence_intervals, key=lambda item: item.start_ms)
        if any(left.end_ms > right.start_ms for left, right in zip(silence_intervals, silence_intervals[1:])):
            raise ValueError("verified silence intervals cannot overlap")
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("chunk identities must be unique")
        for chunk in self.chunks:
            if chunk.end_ms > self.duration_ms:
                raise ValueError("chunk exceeds source duration")
            if chunk.failure_after_segment_id is not None and chunk.failure_after_segment_id not in by_id:
                raise ValueError("chunk recovery references a fabricated segment")
        if self.chunk_recoveries:
            chunks_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
            for recovery in self.chunk_recoveries:
                failed_chunk = chunks_by_id.get(recovery.failed_chunk_id)
                resume_chunk = chunks_by_id.get(recovery.resume_from_chunk_id)
                if failed_chunk is None or resume_chunk is None:
                    raise ValueError("recovery references a fabricated chunk")
                if failed_chunk.expected_outcome != "partial_failure":
                    raise ValueError("recovery must reference an expected partial failure")
                if resume_chunk.start_ms < failed_chunk.start_ms:
                    raise ValueError("recovery cannot resume before the failed chunk")
                if not set(recovery.expected_final_segment_ids).issubset(by_id):
                    raise ValueError("recovery references a fabricated final segment")
                if set(recovery.expected_final_segment_ids) != set(by_id):
                    raise ValueError("completed recovery must account for every reference segment")
                if not set(recovery.completed_segment_ids).issubset(by_id):
                    raise ValueError("recovery references a fabricated completed segment")
        ordered_chunks = sorted(self.chunks, key=lambda item: item.start_ms)
        if ordered_chunks:
            if ordered_chunks[0].start_ms != 0 or ordered_chunks[-1].end_ms != self.duration_ms:
                raise ValueError("chunk plan must cover the original source timeline")
            if any(left.end_ms < right.start_ms for left, right in zip(ordered_chunks, ordered_chunks[1:])):
                raise ValueError("chunk plan cannot leave gaps in the source timeline")
            for previous, current in zip(ordered_chunks, ordered_chunks[1:]):
                expected_overlap = max(0, previous.end_ms - current.start_ms)
                if current.overlap_before_ms != expected_overlap:
                    raise ValueError("chunk overlap annotation must match the original timeline")
        text = " ".join(
            [self.generation.recipe, *(segment.reference_text for segment in self.reference_segments),
             *(annotation.semantic_value for annotation in self.critical_spans),
             *(annotation.reference_interpretation for annotation in self.adversarial_labels),
             *(annotation.forbidden_inversion for annotation in self.adversarial_labels),
             *(annotation.canonical_term for annotation in self.terminology_spans)]
        )
        if _PRIVATE_MARKER.search(text):
            raise ValueError("fixture contains a private-data marker")
        if self.preprocessing.source_audio_sha256 != self.audio.sha256:
            raise ValueError("preprocessing lineage must identify the input asset")
        if self.audio.derived_from_sha256 is not None and self.audio.derived_from_sha256 != self.generation.source_fixture_sha256:
            raise ValueError("derived fixture lineage does not match its frozen source fixture")
        return self

    def _reject_conflicting_annotations(self) -> None:
        groups = (
            (
                self.critical_spans,
                lambda item: (item.segment_id, item.start_char, item.end_char, item.category),
                lambda item: item.semantic_value,
                "critical span",
            ),
            (
                self.adversarial_labels,
                lambda item: (item.segment_id, item.start_char, item.end_char, item.dimension),
                lambda item: (item.reference_interpretation, item.forbidden_inversion),
                "adversarial span",
            ),
            (
                self.terminology_spans,
                lambda item: (item.segment_id, item.start_char, item.end_char),
                lambda item: item.canonical_term,
                "terminology span",
            ),
        )
        for annotations, identity, meaning, label in groups:
            observed: dict[object, object] = {}
            for annotation in annotations:
                key = identity(annotation)
                value = meaning(annotation)
                if key in observed:
                    outcome = "conflicting" if observed[key] != value else "duplicate"
                    raise ValueError(f"{outcome} {label} annotation")
                observed[key] = value


class GateCoverageCategory(DefinitionModel):
    gate_id: VersionIdentifier
    category_id: VersionIdentifier
    eligibility_rule: str = Field(min_length=1)
    eligible_case_ids: tuple[VersionIdentifier, ...] = ()
    annotated_case_ids: tuple[VersionIdentifier, ...] = ()
    minimum_annotation_count: StrictInt | None = Field(default=None, gt=0)
    minimum_decision_reference: str | None = None

    @model_validator(mode="after")
    def coverage_members_and_minimum_have_evidence(self) -> "GateCoverageCategory":
        if len(self.eligible_case_ids) != len(set(self.eligible_case_ids)):
            raise ValueError("eligible case identities must be unique")
        if len(self.annotated_case_ids) != len(set(self.annotated_case_ids)):
            raise ValueError("annotated case identities must be unique")
        if not set(self.annotated_case_ids).issubset(self.eligible_case_ids):
            raise ValueError("annotated cases must be eligible")
        if self.minimum_annotation_count is not None and not self.minimum_decision_reference:
            raise ValueError("minimum coverage requires an attributable Protocol Owner decision")
        if self.minimum_annotation_count is None and self.minimum_decision_reference:
            raise ValueError("coverage decision reference requires a minimum")
        return self

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_case_ids)

    @property
    def annotated_count(self) -> int:
        return len(self.annotated_case_ids)


class CoverageStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    NOT_VERIFIED = "NOT_VERIFIED"


class GateCoverageResult(DefinitionModel):
    gate_id: VersionIdentifier
    category_id: VersionIdentifier
    eligible_count: StrictInt = Field(ge=0)
    annotated_count: StrictInt = Field(ge=0)
    minimum_annotation_count: StrictInt | None = Field(default=None, gt=0)
    status: CoverageStatus


class GateCoverageReport(DefinitionModel):
    report_schema: Literal["russian-transcription-gate-coverage:v2"]
    dataset_id: VersionIdentifier
    dataset_version: VersionIdentifier
    dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    case_count: StrictInt = Field(gt=0)
    qualification_case_count: StrictInt = Field(gt=0)
    coverage: tuple[GateCoverageResult, ...] = Field(min_length=1)


class RussianTranscriptionDatasetV2(DefinitionModel):
    dataset_id: Literal[TRANSCRIPTION_DATASET_ID]
    version: Literal["2.0.0"]
    schema_version: Literal[TRANSCRIPTION_V2_SCHEMA]
    asset_digest_algorithm: Literal["sha256"]
    manifest_digest_algorithm: Literal["sha256-canonical-json-v1"]
    timestamp_coordinate_system: Literal["source_audio_milliseconds"]
    text_span_coordinate_system: Literal["unicode_codepoint_offsets"]
    baseline_dataset_id: Literal[TRANSCRIPTION_DATASET_ID]
    baseline_version: Literal["1.0.0"]
    baseline_sha256: Literal[TRANSCRIPTION_V1_SHA256]
    generation_recipe_version: VersionIdentifier
    cases: tuple[SyntheticQualificationCase, ...] = Field(min_length=1)
    coverage: tuple[GateCoverageCategory, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cases_and_splits(self) -> "RussianTranscriptionDatasetV2":
        case_ids = [case.case_id for case in self.cases]
        paths = [asset.fixture_path for case in self.cases for asset in (case.audio, *case.format_variants)]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("dataset case identities must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("fixture paths must be unique")
        if {case.split for case in self.cases} != set(QualificationSplit):
            raise ValueError("dataset must contain development and qualification splits")
        groups: dict[str, set[QualificationSplit]] = defaultdict(set)
        digests: dict[str, set[QualificationSplit]] = defaultdict(set)
        for case in self.cases:
            groups[case.lineage_group_id].add(case.split)
            for asset in (case.audio, *case.format_variants):
                digests[asset.sha256].add(case.split)
                if asset.derived_from_sha256 is not None:
                    digests[asset.derived_from_sha256].add(case.split)
        if any(len(splits) > 1 for splits in (*groups.values(), *digests.values())):
            raise ValueError("lineage group cannot cross splits")
        coverage_keys = [(item.gate_id, item.category_id) for item in self.coverage]
        if len(coverage_keys) != len(set(coverage_keys)):
            raise ValueError("gate coverage categories must be unique")
        required = set(required_gate_categories())
        if set(coverage_keys) != required:
            raise ValueError("dataset must declare eligibility and coverage for every required gate category")
        cases_by_id = {case.case_id: case for case in self.cases}
        for item in self.coverage:
            eligible = set(item.eligible_case_ids)
            annotated = set(item.annotated_case_ids)
            if not eligible.issubset(cases_by_id):
                raise ValueError("coverage references a fabricated eligible case")
            if any(cases_by_id[case_id].split is not QualificationSplit.QUALIFICATION for case_id in eligible):
                raise ValueError("gate coverage may only count qualification split cases")
            expected_rule = eligibility_rule_for(item.gate_id, item.category_id)
            if item.eligibility_rule != expected_rule:
                raise ValueError("gate coverage eligibility rule does not match its category contract")
            expected_eligible = expected_eligible_case_ids(item.gate_id, item.category_id, self.cases)
            if eligible != expected_eligible:
                raise ValueError("eligible coverage does not match the declared category eligibility rule")
            if annotated != _annotated_case_ids(item.gate_id, item.category_id, self.cases):
                observed_annotations = _annotated_case_ids(item.gate_id, item.category_id, self.cases)
                raise ValueError(
                    "annotated coverage does not match dataset annotations for "
                    f"{item.gate_id}/{item.category_id}: declared={sorted(annotated)}, "
                    f"observed={sorted(observed_annotations)}"
                )
            if not annotated.issubset(eligible):
                raise ValueError("annotated cases must be eligible")
        return self


_GATE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("russian_transcription", ("clean", "noisy", "overlap", "long_form")),
    ("critical_business_fragments", tuple(item.value for item in CriticalSpanCategory)),
    ("adversarial_semantic_inversions", tuple(item.value for item in AdversarialDimension)),
    ("terminology", ("all_annotated_terms",)),
    ("segment_timestamps", ("boundary_error", "alignment_coverage")),
    ("silence", ("pure_silence", "verified_silence_intervals")),
    ("long_form_chunking", ("chunk_boundaries", "pause_handling", "reassembly", "partial_recovery")),
    ("preprocessing_effects", ("baseline", "preprocessing_comparison")),
    ("performance", ("cold_start", "warm_inference", "preprocessing", "inference", "reassembly", "end_to_end", "resources")),
    ("offline_integrity", ("asset_loading", "source_provenance", "dependency_provenance", "supported_wav_pcm", "supported_mp3", "supported_m4a", "network_egress")),
)


def required_gate_categories() -> tuple[tuple[str, str], ...]:
    """Return the complete frozen gate/category identity set shared with #758."""
    return tuple((gate_id, category_id) for gate_id, categories in _GATE_CATEGORIES for category_id in categories)


def eligibility_rule_for(gate_id: str, category_id: str) -> str:
    if gate_id == "russian_transcription":
        return "qualification_speech_cases_with_matching_media_category"
    if gate_id in {"critical_business_fragments", "adversarial_semantic_inversions", "terminology"}:
        return "qualification_cases_with_reference_speech"
    if gate_id == "segment_timestamps":
        return "qualification_cases_with_reference_speech"
    if gate_id == "silence":
        return "all_qualification_cases"
    if gate_id == "long_form_chunking":
        return "qualification_long_form_cases"
    if gate_id == "preprocessing_effects" and category_id == "baseline":
        return "qualification_cases_with_paired_baseline"
    if gate_id == "preprocessing_effects":
        return "qualification_cases_with_preprocessing_output"
    if gate_id == "offline_integrity" and category_id.startswith("supported_"):
        return "qualification_cases_with_supported_media_format"
    if gate_id == "performance" or gate_id == "offline_integrity":
        return "operator_evidence_not_fixture_cases"
    raise ValueError(f"no eligibility rule defined for {gate_id}/{category_id}")


def expected_eligible_case_ids(
    gate_id: str,
    category_id: str,
    cases: Sequence[SyntheticQualificationCase],
) -> set[str]:
    eligible: set[str] = set()
    for case in cases:
        if case.split is not QualificationSplit.QUALIFICATION:
            continue
        if gate_id == "russian_transcription" and case.media_category == category_id and case.reference_segments:
            eligible.add(case.case_id)
        elif gate_id in {"critical_business_fragments", "adversarial_semantic_inversions", "terminology", "segment_timestamps"} and case.reference_segments:
            eligible.add(case.case_id)
        elif gate_id == "silence":
            eligible.add(case.case_id)
        elif gate_id == "long_form_chunking" and case.media_category == "long_form":
            eligible.add(case.case_id)
        elif gate_id == "preprocessing_effects" and category_id == "baseline" and case.preprocessing_pair_id:
            eligible.add(case.case_id)
        elif gate_id == "preprocessing_effects" and category_id != "baseline" and case.preprocessing.output_audio_sha256:
            eligible.add(case.case_id)
        elif gate_id == "offline_integrity" and category_id.startswith("supported_"):
            media = {
                "supported_wav_pcm": SyntheticMediaFormat.WAV_PCM,
                "supported_mp3": SyntheticMediaFormat.MP3_LAYER_III,
                "supported_m4a": SyntheticMediaFormat.M4A_AAC_LC,
            }[category_id]
            if any(asset.media_format is media for asset in (case.audio, *case.format_variants)):
                eligible.add(case.case_id)
    return eligible


def evaluate_gate_coverage(
    coverage: tuple[GateCoverageCategory, ...],
) -> tuple[GateCoverageResult, ...]:
    """Report coverage without treating an unverified decision reference as authority.

    The v2 contract has no trusted Protocol Owner decision verifier. A string
    reference can be fabricated by a caller, so this evaluator must not emit
    ``SUFFICIENT`` even when a numeric minimum and reference are present.
    """
    results = []
    for item in coverage:
        minimum = item.minimum_annotation_count
        results.append(
            GateCoverageResult(
                gate_id=item.gate_id,
                category_id=item.category_id,
                eligible_count=item.eligible_count,
                annotated_count=item.annotated_count,
                minimum_annotation_count=minimum,
                status=CoverageStatus.NOT_VERIFIED,
            )
        )
    return tuple(results)


def build_gate_coverage_report(dataset: RussianTranscriptionDatasetV2) -> GateCoverageReport:
    return GateCoverageReport(
        report_schema="russian-transcription-gate-coverage:v2",
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_sha256=transcription_dataset_v2_sha256(dataset),
        case_count=len(dataset.cases),
        qualification_case_count=sum(case.split is QualificationSplit.QUALIFICATION for case in dataset.cases),
        coverage=evaluate_gate_coverage(dataset.coverage),
    )


def _annotated_case_ids(
    gate_id: str,
    category_id: str,
    cases: tuple[SyntheticQualificationCase, ...],
) -> set[str]:
    observed: set[str] = set()
    for case in cases:
        if case.split is not QualificationSplit.QUALIFICATION:
            continue
        if gate_id == "critical_business_fragments" and any(
            span.category.value == category_id for span in case.critical_spans
        ):
            observed.add(case.case_id)
        elif gate_id == "adversarial_semantic_inversions" and any(
            label.dimension.value == category_id for label in case.adversarial_labels
        ):
            observed.add(case.case_id)
        elif gate_id == "terminology" and category_id == "all_annotated_terms" and case.terminology_spans:
            observed.add(case.case_id)
        elif gate_id == "segment_timestamps" and category_id == "boundary_error" and case.reference_segments:
            observed.add(case.case_id)
        elif gate_id == "segment_timestamps" and category_id == "alignment_coverage" and case.reference_segments:
            observed.add(case.case_id)
        elif gate_id == "silence" and category_id == "pure_silence" and case.media_category == "silence":
            observed.add(case.case_id)
        elif gate_id == "silence" and category_id == "verified_silence_intervals" and case.verified_silence_intervals:
            observed.add(case.case_id)
        elif gate_id == "long_form_chunking" and category_id in {"chunk_boundaries", "pause_handling", "reassembly"} and case.chunks:
            observed.add(case.case_id)
        elif gate_id == "long_form_chunking" and category_id == "partial_recovery" and case.chunk_recoveries:
            observed.add(case.case_id)
        elif gate_id == "preprocessing_effects" and category_id == "baseline" and case.preprocessing_pair_id:
            observed.add(case.case_id)
        elif gate_id == "preprocessing_effects" and category_id == "preprocessing_comparison" and case.preprocessing.output_audio_sha256:
            observed.add(case.case_id)
        elif gate_id == "offline_integrity" and category_id == "supported_wav_pcm" and any(asset.media_format is SyntheticMediaFormat.WAV_PCM for asset in (case.audio, *case.format_variants)):
            observed.add(case.case_id)
        elif gate_id == "offline_integrity" and category_id == "supported_mp3" and any(asset.media_format is SyntheticMediaFormat.MP3_LAYER_III for asset in (case.audio, *case.format_variants)):
            observed.add(case.case_id)
        elif gate_id == "offline_integrity" and category_id == "supported_m4a" and any(asset.media_format is SyntheticMediaFormat.M4A_AAC_LC for asset in (case.audio, *case.format_variants)):
            observed.add(case.case_id)
        elif gate_id == "russian_transcription" and category_id == case.media_category and case.reference_segments:
            observed.add(case.case_id)
    return observed


def canonical_transcription_dataset_v2_json(dataset: RussianTranscriptionDatasetV2) -> str:
    return json.dumps(dataset.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def transcription_dataset_v2_sha256(dataset: RussianTranscriptionDatasetV2) -> str:
    return hashlib.sha256(canonical_transcription_dataset_v2_json(dataset).encode("utf-8")).hexdigest()


def load_russian_transcription_v2_dataset() -> RussianTranscriptionDatasetV2:
    data_root = Path(__file__).with_name("data")
    dataset = RussianTranscriptionDatasetV2.model_validate_json(
        (data_root / "russian_transcription_v2.json").read_text(encoding="utf-8")
    )
    expected_digest = (data_root / "russian_transcription_v2.sha256").read_text(encoding="ascii").split()[0]
    if transcription_dataset_v2_sha256(dataset) != expected_digest:
        raise ValueError("v2 dataset canonical digest mismatch")
    validate_transcription_v2_assets(dataset, data_root)
    return dataset


def validate_transcription_v2_assets(dataset: RussianTranscriptionDatasetV2, asset_root: Path) -> None:
    """Verify fixture bytes and container metadata without network access."""
    for case in dataset.cases:
        relative = PurePosixPath(case.audio.fixture_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("audio fixture path must stay inside the dataset root")
        path = asset_root.joinpath(*relative.parts)
        _validate_audio_asset(path, case.audio.sha256, case.case_id)
        if case.audio.media_format is SyntheticMediaFormat.WAV_PCM:
            if case.media_category == "corruption":
                try:
                    with wave.open(str(path), "rb"):
                        pass
                except (wave.Error, EOFError):
                    continue
                raise ValueError(f"corrupt-media fixture is a valid WAV: {case.case_id}")
            _validate_wav_metadata(path, case.audio.channels, case.audio.source_sample_rate_hz, case.audio.sample_width_bits, case.duration_ms, case.case_id)
            if case.media_category != "corruption":
                _validate_verified_silence(path, case.verified_silence_intervals, case.case_id)
        else:
            _validate_compressed_asset(path, case.audio.media_format, case.audio.source_sample_rate_hz, case.duration_ms, case.case_id)
        for variant in case.format_variants:
            variant_path = asset_root.joinpath(*PurePosixPath(variant.fixture_path).parts)
            _validate_audio_asset(variant_path, variant.sha256, case.case_id)
            if variant.media_format is SyntheticMediaFormat.WAV_PCM:
                _validate_wav_metadata(variant_path, variant.channels, variant.source_sample_rate_hz, variant.sample_width_bits, case.duration_ms, case.case_id)
            else:
                _validate_compressed_asset(variant_path, variant.media_format, variant.source_sample_rate_hz, case.duration_ms, case.case_id)
        if case.preprocessing.derived_audio_path is not None:
            output_path = asset_root.joinpath(*PurePosixPath(case.preprocessing.derived_audio_path).parts)
            _validate_audio_asset(output_path, case.preprocessing.output_audio_sha256, case.case_id)
            _validate_wav_metadata(output_path, 1, 16000, 16, case.duration_ms, case.case_id)
        for preprocessing in case.format_variant_preprocessing:
            if preprocessing.derived_audio_path is None:
                raise ValueError(f"format variant preprocessing output is missing: {case.case_id}")
            output_path = asset_root.joinpath(*PurePosixPath(preprocessing.derived_audio_path).parts)
            _validate_audio_asset(output_path, preprocessing.output_audio_sha256, case.case_id)
            _validate_wav_metadata(output_path, 1, 16000, 16, case.duration_ms, case.case_id)


def main() -> None:
    report = build_gate_coverage_report(load_russian_transcription_v2_dataset())
    json.dump(report.model_dump(mode="json"), sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def _validate_audio_asset(path: Path, expected_sha256: str | None, case_id: str) -> None:
    payload = path.read_bytes()
    if expected_sha256 is None or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(f"audio fixture digest mismatch: {case_id}")


def _validate_compressed_asset(
    path: Path,
    media_format: SyntheticMediaFormat,
    sample_rate_hz: int,
    duration_ms: int,
    case_id: str,
) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("ffprobe is required to inspect compressed synthetic fixtures")
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,profile,sample_rate,channels:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=10,
    )
    streams = json.loads(completed.stdout).get("streams", [])
    expected_codec = "mp3" if media_format is SyntheticMediaFormat.MP3_LAYER_III else "aac"
    profile_matches = media_format is SyntheticMediaFormat.MP3_LAYER_III or (
        len(streams) == 1 and streams[0].get("profile") == "LC"
    )
    duration = json.loads(completed.stdout).get("format", {}).get("duration")
    if (
        len(streams) != 1
        or streams[0].get("codec_name") != expected_codec
        or int(streams[0].get("channels", 0)) != 1
        or int(streams[0].get("sample_rate", 0)) != sample_rate_hz
        or not profile_matches
        or duration is None
        or abs(float(duration) * 1000 - duration_ms) > 250
    ):
        raise ValueError(f"compressed fixture codec/stream mismatch: {case_id}")


def _validate_wav_metadata(path: Path, channels: int, sample_rate_hz: int, sample_width_bits: int, duration_ms: int, case_id: str) -> None:
    try:
        with wave.open(str(path), "rb") as audio:
            metadata = audio.getnchannels(), audio.getframerate(), audio.getsampwidth() * 8
            if metadata != (channels, sample_rate_hz, sample_width_bits):
                raise ValueError(f"WAV fixture metadata mismatch: {case_id}")
            if round(audio.getnframes() * 1000 / audio.getframerate()) != duration_ms:
                raise ValueError(f"audio duration mismatch: {case_id}")
    except (wave.Error, EOFError):
        raise ValueError(f"WAV fixture is unreadable: {case_id}") from None


def _validate_verified_silence(path: Path, intervals: tuple[TimeInterval, ...], case_id: str) -> None:
    if not intervals:
        return
    with wave.open(str(path), "rb") as audio:
        sample_rate = audio.getframerate()
        samples = audio.readframes(audio.getnframes())
    pcm = memoryview(samples)
    for interval in intervals:
        bytes_per_sample = audio.getsampwidth()
        first = round(interval.start_ms * sample_rate / 1000) * bytes_per_sample
        last = round(interval.end_ms * sample_rate / 1000) * bytes_per_sample
        if any(pcm[first:last]):
            raise ValueError(f"verified silence contains nonzero audio: {case_id}")


__all__ = [
    "AdversarialDimension",
    "AdversarialMeaningAnnotation",
    "CoverageStatus",
    "CriticalSpanAnnotation",
    "CriticalSpanCategory",
    "ChunkExpectation",
    "ChunkRecoveryExpectation",
    "GateCoverageCategory",
    "GateCoverageReport",
    "GateCoverageResult",
    "QualificationSplit",
    "ReferenceSegment",
    "RussianTranscriptionDatasetV2",
    "SyntheticAudioAsset",
    "SyntheticAudioGeneration",
    "SyntheticMediaFormat",
    "SyntheticQualificationCase",
    "TerminologyAnnotation",
    "canonical_transcription_dataset_v2_json",
    "build_gate_coverage_report",
    "evaluate_gate_coverage",
    "eligibility_rule_for",
    "expected_eligible_case_ids",
    "load_russian_transcription_v2_dataset",
    "main",
    "required_gate_categories",
    "transcription_dataset_v2_sha256",
    "validate_transcription_v2_assets",
]


if __name__ == "__main__":
    main()
