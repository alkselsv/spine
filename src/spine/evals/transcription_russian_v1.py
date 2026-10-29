"""Versioned synthetic Russian transcription and requirements evaluation set."""

from __future__ import annotations

from functools import lru_cache
import hashlib
from itertools import product
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Literal
import wave

from pydantic import Field, StrictBool, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


class TranscriptionEvalSegment(DefinitionModel):
    segment_id: VersionIdentifier
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    reference_text: str = Field(min_length=1)
    permissible_variants: tuple[str, ...] = ()
    anonymous_speaker: VersionIdentifier | None = None

    @model_validator(mode="after")
    def validate_time(self) -> "TranscriptionEvalSegment":
        if self.end_ms <= self.start_ms:
            raise ValueError("segment end must follow start")
        if any(not variant.strip() for variant in self.permissible_variants):
            raise ValueError("permissible transcript variants cannot be blank")
        return self


class RequirementLabel(DefinitionModel):
    requirement_id: VersionIdentifier
    label: str = Field(min_length=1, max_length=256)
    evidence_segment_ids: tuple[VersionIdentifier, ...] = Field(min_length=1)


class FakeRequirement(DefinitionModel):
    requirement_id: VersionIdentifier
    evidence_segment_ids: tuple[VersionIdentifier, ...] = ()


class RussianTranscriptionCase(DefinitionModel):
    case_id: VersionIdentifier
    kind: Literal[
        "short_speech", "long_speech", "pause", "noise", "silence",
        "corruption", "weak_speech", "multiple_speakers", "malicious_instruction",
    ]
    duration_ms: StrictInt = Field(gt=0)
    audio_fixture: VersionIdentifier
    audio_fixture_path: str = Field(pattern=r"^audio/[a-z0-9-]+\.wav$")
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    synthetic_recipe: str = Field(min_length=1, max_length=256)
    synthetic_only: StrictBool
    contains_personal_data: StrictBool
    contains_credentials: StrictBool
    external_call_allowed: Literal[False] = False
    reference_segments: tuple[TranscriptionEvalSegment, ...] = ()
    expected_transcript: str = ""
    fake_asr_transcript: str = ""
    requirement_labels: tuple[RequirementLabel, ...] = ()
    fake_requirements: tuple[FakeRequirement, ...] = ()
    expected_requirements_outcome: Literal["extract", "abstain", "clarify"]
    fake_requirements_outcome: Literal["extract", "abstain", "clarify"]
    anonymous_speaker_policy: Literal["labels_only_never_resolve_identity"]
    malicious_spans: tuple[VersionIdentifier, ...] = ()

    @model_validator(mode="after")
    def validate_case(self) -> "RussianTranscriptionCase":
        if not self.synthetic_only or self.contains_personal_data or self.contains_credentials:
            raise ValueError("fixtures must be synthetic and contain no private data")
        no_reference_kinds = {"silence", "corruption", "weak_speech"}
        if self.kind in no_reference_kinds and self.reference_segments:
            raise ValueError("weak, silent and corrupt fixtures must not claim transcript segments")
        if self.kind not in no_reference_kinds and not self.reference_segments:
            raise ValueError("speech fixture requires reference transcript lineage")
        if self.kind == "silence" and (self.expected_transcript or self.expected_requirements_outcome != "abstain"):
            raise ValueError("silence must expect empty transcription and abstention")
        if self.kind in {"corruption", "weak_speech"} and self.expected_requirements_outcome != "clarify":
            raise ValueError("weak or undecodable speech must expect clarification")
        if self.kind in no_reference_kinds and (
            self.expected_transcript or self.fake_asr_transcript
        ):
            raise ValueError("no-speech fixtures must not contain a transcript hypothesis")
        if self.kind == "corruption" and self.expected_requirements_outcome != "clarify":
            raise ValueError("corrupt audio must expect clarification")
        segment_ids = [segment.segment_id for segment in self.reference_segments]
        if len(set(segment_ids)) != len(segment_ids):
            raise ValueError("reference segment identities must be unique")
        for segment in self.reference_segments:
            if segment.end_ms > self.duration_ms:
                raise ValueError("reference segment exceeds audio duration")
        if " ".join(segment.reference_text for segment in self.reference_segments) != self.expected_transcript:
            raise ValueError("reference transcript must match the ordered segment text")
        ids = set(segment_ids)
        fixture_path = PurePosixPath(self.audio_fixture_path)
        if fixture_path.is_absolute() or ".." in fixture_path.parts:
            raise ValueError("audio fixture path must remain within the dataset")
        for label in self.requirement_labels:
            if not set(label.evidence_segment_ids).issubset(ids):
                raise ValueError("requirement evidence references a fabricated segment")
        for prediction in self.fake_requirements:
            if not set(prediction.evidence_segment_ids).issubset(ids):
                raise ValueError("fake requirement evidence references a fabricated segment")
        if any(
            set(label.evidence_segment_ids) & set(self.malicious_spans)
            for label in self.requirement_labels
        ):
            raise ValueError("untrusted spoken instructions cannot support requirements")
        if len({item.requirement_id for item in self.requirement_labels}) != len(self.requirement_labels):
            raise ValueError("requirement labels must have unique identities")
        if len({item.requirement_id for item in self.fake_requirements}) != len(self.fake_requirements):
            raise ValueError("fake requirements must have unique identities")
        if self.expected_requirements_outcome != "extract" and self.requirement_labels:
            raise ValueError("abstention and clarification cases cannot label requirements")
        if self.fake_requirements_outcome != "extract" and self.fake_requirements:
            raise ValueError("abstention and clarification output cannot contain requirements")
        if self.kind == "malicious_instruction" and not self.malicious_spans:
            raise ValueError("malicious instruction case must label untrusted spans")
        if not set(self.malicious_spans).issubset(ids):
            raise ValueError("malicious span references a fabricated segment")
        if self.kind == "multiple_speakers":
            speakers = {segment.anonymous_speaker for segment in self.reference_segments}
            if None in speakers or len(speakers) < 2:
                raise ValueError("multiple speaker case needs anonymous labels for at least two speakers")
        text = " ".join((self.expected_transcript, self.fake_asr_transcript, self.synthetic_recipe,
                          *(segment.reference_text for segment in self.reference_segments),
                          *(label.label for label in self.requirement_labels)))
        if _PRIVATE_MARKER.search(text):
            raise ValueError("fixture contains a private-data marker")
        return self


class RussianTranscriptionDataset(DefinitionModel):
    dataset_id: Literal["russian-meeting-transcription-synthetic"]
    version: Literal["1.0.0"]
    schema_version: Literal["russian-transcription-eval:v1"]
    asr_metric_version: VersionIdentifier
    requirements_metric_version: VersionIdentifier
    fake_model_version: VersionIdentifier
    fake_config_version: VersionIdentifier
    audio_sample_rate_hz: StrictInt = Field(gt=0)
    cases: tuple[RussianTranscriptionCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cases(self) -> "RussianTranscriptionDataset":
        ids = [case.case_id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("dataset case identities must be unique")
        required = {"short_speech", "long_speech", "pause", "noise", "silence",
                    "corruption", "weak_speech", "multiple_speakers", "malicious_instruction"}
        if not required.issubset({case.kind for case in self.cases}):
            raise ValueError("dataset does not cover every required audio scenario")
        fixtures = [case.audio_fixture for case in self.cases]
        if len(set(fixtures)) != len(fixtures):
            raise ValueError("audio fixture identities must be unique")
        fixture_paths = [case.audio_fixture_path for case in self.cases]
        if len(set(fixture_paths)) != len(fixture_paths):
            raise ValueError("audio fixture paths must be unique")
        return self


class MetricValue(DefinitionModel):
    value: float
    numerator: StrictInt = Field(ge=0)
    denominator: StrictInt = Field(ge=0)


class TranscriptionEvaluationReport(DefinitionModel):
    report_schema: Literal["russian-transcription-evaluation-report:v1"]
    dataset_id: str
    dataset_version: str
    dataset_sha256: str
    model_version: str
    config_version: str
    case_count: StrictInt = Field(gt=0)
    asr: dict[str, MetricValue]
    requirements: dict[str, MetricValue]


_PRIVATE_MARKER = re.compile(
    r"(?i)(?:[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b\+?\d[\d ()-]{8,}\d\b|"
    r"\b(?:password|passwd|api[_ -]?key|secret|token)\s*[:=])"
)
_TOKEN = re.compile(r"[\w-]+", flags=re.UNICODE)


@lru_cache(maxsize=1)
def load_russian_transcription_v1_dataset() -> RussianTranscriptionDataset:
    path = Path(__file__).with_name("data") / "russian_transcription_v1.json"
    dataset = RussianTranscriptionDataset.model_validate_json(path.read_text(encoding="utf-8"))
    validate_russian_audio_fixtures(dataset, path.parent)
    return dataset


def validate_russian_audio_fixtures(
    dataset: RussianTranscriptionDataset, asset_root: Path
) -> None:
    """Reject missing, changed or incorrectly timed checked-in audio assets."""
    for case in dataset.cases:
        path = asset_root.joinpath(*PurePosixPath(case.audio_fixture_path).parts)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != case.audio_sha256:
            raise ValueError(f"audio fixture digest mismatch: {case.audio_fixture}")
        try:
            with wave.open(str(path), "rb") as audio:
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or audio.getframerate() != dataset.audio_sample_rate_hz
                ):
                    raise ValueError(f"audio fixture format mismatch: {case.audio_fixture}")
                duration_ms = round(audio.getnframes() * 1000 / audio.getframerate())
                frames = audio.readframes(audio.getnframes())
        except (wave.Error, EOFError):
            if case.kind != "corruption":
                raise ValueError(f"audio fixture is unreadable: {case.audio_fixture}") from None
            continue
        if case.kind == "corruption":
            raise ValueError("corrupt-media fixture must not be a valid WAV")
        if duration_ms != case.duration_ms:
            raise ValueError(f"audio fixture duration mismatch: {case.audio_fixture}")
        if (case.kind == "silence") != (not any(frames)):
            raise ValueError(f"audio fixture silence classification mismatch: {case.audio_fixture}")


def canonical_russian_transcription_dataset_json(
    dataset: RussianTranscriptionDataset | None = None,
) -> str:
    loaded = dataset or load_russian_transcription_v1_dataset()
    return json.dumps(loaded.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def run_russian_transcription_v1(
    dataset: RussianTranscriptionDataset | None = None,
) -> TranscriptionEvaluationReport:
    """Run deterministic fake ASR and extraction baselines without external calls."""
    loaded = dataset or load_russian_transcription_v1_dataset()
    wer_errors = wer_words = cer_errors = cer_chars = 0
    no_speech_cases = no_speech_hallucinations = 0
    exact = outcomes_correct = 0
    gold_requirements = predicted_requirements = true_requirements = 0
    evidence_predicted = evidence_gold = evidence_valid = 0
    for case in loaded.cases:
        hypothesis = _normalize(case.fake_asr_transcript)
        if not case.expected_transcript:
            no_speech_cases += 1
            no_speech_hallucinations += int(bool(hypothesis))
            reference = ""
        else:
            segment_alternatives = [
                (segment.reference_text, *segment.permissible_variants)
                for segment in case.reference_segments
            ]
            reference_variants = {
                case.expected_transcript,
                *(" ".join(parts) for parts in product(*segment_alternatives)),
            }
            reference = min(
                (_normalize(item) for item in reference_variants),
                key=lambda item: _edit_distance(item, hypothesis),
            )
            ref_words, hyp_words = reference.split(), hypothesis.split()
            wer_errors += _edit_distance(ref_words, hyp_words)
            wer_words += len(ref_words)
            cer_errors += _edit_distance(list(reference), list(hypothesis))
            cer_chars += len(reference)
        exact += int(reference == hypothesis)
        outcomes_correct += int(case.expected_requirements_outcome == case.fake_requirements_outcome)
        gold = {item.requirement_id: set(item.evidence_segment_ids) for item in case.requirement_labels}
        pred = {item.requirement_id: set(item.evidence_segment_ids) for item in case.fake_requirements}
        gold_requirements += len(gold)
        predicted_requirements += len(pred)
        common = gold.keys() & pred.keys()
        true_requirements += len(common)
        evidence_gold += sum(len(refs) for refs in gold.values())
        evidence_predicted += sum(len(refs) for refs in pred.values())
        for requirement_id in common:
            evidence_valid += len(gold[requirement_id] & pred[requirement_id])
    req_precision = _ratio(true_requirements, predicted_requirements)
    req_recall = _ratio(true_requirements, gold_requirements)
    req_f1 = _f1(req_precision, req_recall)
    canonical = canonical_russian_transcription_dataset_json(loaded)
    return TranscriptionEvaluationReport(
        report_schema="russian-transcription-evaluation-report:v1",
        dataset_id=loaded.dataset_id,
        dataset_version=loaded.version,
        dataset_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        model_version=loaded.fake_model_version,
        config_version=loaded.fake_config_version,
        case_count=len(loaded.cases),
        asr={
            "wer": _metric(wer_errors, wer_words),
            "cer": _metric(cer_errors, cer_chars),
            "exact_transcript_rate": _metric(exact, len(loaded.cases)),
            "no_speech_hallucination_rate": _metric(no_speech_hallucinations, no_speech_cases),
        },
        requirements={
            "precision": MetricValue(value=req_precision, numerator=true_requirements, denominator=predicted_requirements),
            "recall": MetricValue(value=req_recall, numerator=true_requirements, denominator=gold_requirements),
            "f1": MetricValue(value=req_f1, numerator=2 * true_requirements, denominator=predicted_requirements + gold_requirements),
            "evidence_precision": _metric(evidence_valid, evidence_predicted),
            "evidence_recall": _metric(evidence_valid, evidence_gold),
            "outcome_accuracy": _metric(outcomes_correct, len(loaded.cases)),
        },
    )


def _normalize(text: str) -> str:
    return " ".join(_TOKEN.findall(text.casefold().replace("ё", "е")))


def _edit_distance(left: list[str] | str, right: list[str] | str) -> int:
    previous = list(range(len(right) + 1))
    for index, token in enumerate(left, start=1):
        current = [index]
        for column, other in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (token != other)))
        previous = current
    return previous[-1]


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _metric(numerator: int, denominator: int) -> MetricValue:
    return MetricValue(value=_ratio(numerator, denominator), numerator=numerator, denominator=denominator)


def main() -> None:
    """Print the pinned deterministic fake-baseline report as JSON."""
    print(run_russian_transcription_v1().model_dump_json(indent=2))


__all__ = [
    "RequirementLabel", "RussianTranscriptionCase", "RussianTranscriptionDataset",
    "TranscriptionEvaluationReport", "canonical_russian_transcription_dataset_json",
    "load_russian_transcription_v1_dataset", "run_russian_transcription_v1",
    "main",
]
