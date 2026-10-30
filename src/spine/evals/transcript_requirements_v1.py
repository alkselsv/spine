"""Versioned deterministic evaluation for transcript requirement extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from spine.domain.capabilities import RequirementsExtractionOutput
from spine.domain.common import DefinitionModel
from spine.domain.workflows import RequirementCategory, RequirementState


TRANSCRIPT_REQUIREMENTS_DATASET_VERSION = "transcript-requirements:v1"
TRANSCRIPT_REQUIREMENTS_EVALUATOR_VERSION = "transcript-requirements-evaluator:v1"
_DATASET = Path(__file__).with_name("data") / "transcript_requirements_v1.json"


class TranscriptRequirementsEvalCase(DefinitionModel):
    case_id: str = Field(min_length=1, max_length=128)
    transcript: str = Field(min_length=1)
    expected_outcome: Literal["success", "partial", "abstain"]
    expected_categories: tuple[RequirementCategory, ...] = ()
    recognition_quality: Literal["reliable", "weak", "unknown"] = "reliable"
    speaker_known: bool = True
    availability: Literal["current", "denied", "stale", "missing"] = "current"


class TranscriptRequirementsEvalDataset(DefinitionModel):
    schema_version: Literal["transcript-requirements-dataset:v1"]
    dataset_version: Literal["transcript-requirements:v1"]
    cases: tuple[TranscriptRequirementsEvalCase, ...]


class TranscriptRequirementsEvalReport(DefinitionModel):
    evaluator_version: Literal["transcript-requirements-evaluator:v1"]
    dataset_version: Literal["transcript-requirements:v1"]
    passed: bool
    failures: tuple[str, ...] = ()


def load_transcript_requirements_dataset() -> TranscriptRequirementsEvalDataset:
    return TranscriptRequirementsEvalDataset.model_validate(
        json.loads(_DATASET.read_text(encoding="utf-8"))
    )


def evaluate_transcript_requirements(
    results: tuple[tuple[TranscriptRequirementsEvalCase, RequirementsExtractionOutput], ...],
) -> TranscriptRequirementsEvalReport:
    failures: list[str] = []
    for case, output in results:
        if output.outcome.value != case.expected_outcome:
            failures.append(f"{case.case_id}:outcome")
        categories = tuple(item.category for item in output.requirements)
        if categories != case.expected_categories:
            failures.append(f"{case.case_id}:categories")
        for requirement in output.requirements:
            if requirement.state is RequirementState.ACCEPTED:
                failures.append(f"{case.case_id}:auto_accepted")
            if len(requirement.evidence_refs) != 1:
                failures.append(f"{case.case_id}:exact_evidence")
                continue
            reference = requirement.evidence_refs[0]
            if reference.transcript_locator is None:
                failures.append(f"{case.case_id}:time_range_locator")
    return TranscriptRequirementsEvalReport(
        evaluator_version=TRANSCRIPT_REQUIREMENTS_EVALUATOR_VERSION,
        dataset_version=TRANSCRIPT_REQUIREMENTS_DATASET_VERSION,
        passed=not failures,
        failures=tuple(failures),
    )


__all__ = [
    "TRANSCRIPT_REQUIREMENTS_DATASET_VERSION",
    "TRANSCRIPT_REQUIREMENTS_EVALUATOR_VERSION",
    "TranscriptRequirementsEvalCase",
    "TranscriptRequirementsEvalDataset",
    "TranscriptRequirementsEvalReport",
    "evaluate_transcript_requirements",
    "load_transcript_requirements_dataset",
]
