"""Pinned loader for the private answer-quality Golden Dataset.

The loader validates protected gold and source references without exposing their
contents through its report.  Loading does not perform retrieval or evaluation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePath
from typing import Literal
import unicodedata

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from spine.evals.real_qa_r1 import RealCaseObservationInput


ANSWER_QUALITY_DATASET_SCHEMA = "answer-quality-dataset:v1"
ANSWER_QUALITY_VALIDATION_SCHEMA = "answer-quality-dataset-validation:v1"


class AnswerQualityDatasetError(ValueError):
    """Base class for disclosure-safe answer-quality dataset failures."""


class AnswerQualityDatasetValidationError(AnswerQualityDatasetError):
    """The selected input does not match its approved identity or schema."""


class AnswerQualityDatasetIdentity(DefinitionModel):
    """Approved immutable identity and population counts for one dataset."""

    version: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(gt=0)
    source_bound_case_count: int = Field(ge=0)
    unscoped_case_count: int = Field(ge=0)
    unique_document_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_population(self) -> "AnswerQualityDatasetIdentity":
        if self.source_bound_case_count + self.unscoped_case_count != self.case_count:
            raise ValueError("answer-quality population counts must sum to case count")
        return self


APPROVED_ANSWER_QUALITY_DATASET = AnswerQualityDatasetIdentity(
    version="0.4.0-user-approved-answer-quality",
    sha256="76d1e58241629c220e6bb8a5d532baf0901400a3f8b220aeec76e99c4e0aba01",
    source_manifest_sha256=(
        "376739b6fd8d4934edbfd1bcc9e2f2a44a58f65dcf452e91f7229e732921d168"
    ),
    case_count=197,
    source_bound_case_count=186,
    unscoped_case_count=11,
    unique_document_count=28,
)


class AnswerQualityCase(DefinitionModel):
    """One protected answer-quality case; gold is never an observation input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=256)
    question: str = Field(min_length=1)
    expected_answer: str = Field(min_length=1)
    document_refs: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_revision_ids: tuple[str, ...] = ()
    retrieval_source_ids: tuple[str, ...] = Field(min_length=1)
    retrieval_source_revision_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source_bindings(self) -> "AnswerQualityCase":
        if not (
            len(self.document_refs)
            == len(self.source_ids)
            == len(self.source_revision_ids)
        ):
            raise ValueError("answer-quality source bindings must be one-to-one")
        if (
            len(self.retrieval_source_ids) != len(self.retrieval_source_revision_ids)
            or len(set(self.retrieval_source_ids)) != len(self.retrieval_source_ids)
            or len(set(self.retrieval_source_revision_ids))
            != len(self.retrieval_source_revision_ids)
            or not set(self.source_ids) <= set(self.retrieval_source_ids)
            or not set(self.source_revision_ids)
            <= set(self.retrieval_source_revision_ids)
        ):
            raise ValueError("answer-quality retrieval bindings are invalid")
        return self


class AnswerQualitySourceManifestEntry(DefinitionModel):
    """Private immutable filename-to-opaque-revision crosswalk."""

    file_name: str = Field(min_length=1, max_length=512)
    source_id: str = Field(min_length=1, max_length=256)
    source_revision_id: str = Field(min_length=1, max_length=256)
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AnswerQualitySourceManifest(DefinitionModel):
    """Approved private source inventory used before canonical runtime mapping."""

    schema_version: Literal["answer-quality-source-manifest:v1"]
    documents: tuple[AnswerQualitySourceManifestEntry, ...] = Field(min_length=1)


class AnswerQualityDataset(DefinitionModel):
    """Validated protected dataset selected by its exact approved digest."""

    schema_version: Literal["answer-quality-dataset:v1"] = ANSWER_QUALITY_DATASET_SCHEMA
    version: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[AnswerQualityCase, ...] = Field(min_length=1)


class AnswerQualityDatasetValidationReport(DefinitionModel):
    """Content-free validation summary safe for qualification reporting."""

    schema_version: Literal["answer-quality-dataset-validation:v1"] = (
        ANSWER_QUALITY_VALIDATION_SCHEMA
    )
    validation_status: Literal["passed"] = "passed"
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(ge=0)
    source_bound_case_count: int = Field(ge=0)
    unscoped_case_count: int = Field(ge=0)
    unique_document_count: int = Field(ge=0)
    source_files_verified: int = Field(ge=0)


class LoadedAnswerQualityDataset(DefinitionModel):
    """Validated protected data paired with its disclosure-safe report."""

    dataset: AnswerQualityDataset
    validation_report: AnswerQualityDatasetValidationReport


def load_answer_quality_dataset(
    dataset_path: Path | str,
    *,
    source_root: Path | str,
    source_manifest_path: Path | str,
    identity: AnswerQualityDatasetIdentity,
) -> LoadedAnswerQualityDataset:
    """Validate an exact approved answer-only dataset and its document refs."""

    path = Path(dataset_path).expanduser().resolve()
    root = Path(source_root).expanduser().resolve()
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise AnswerQualityDatasetValidationError(
            "approved answer-quality dataset is unavailable"
        ) from exc
    if hashlib.sha256(encoded).hexdigest() != identity.sha256:
        raise AnswerQualityDatasetValidationError(
            "dataset digest does not match approved identity"
        )
    if not root.is_dir():
        raise AnswerQualityDatasetValidationError("approved source root is unavailable")
    source_manifest = _load_source_manifest(source_manifest_path, identity)
    try:
        raw = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AnswerQualityDatasetValidationError("dataset JSON is invalid") from exc
    if not isinstance(raw, list):
        raise AnswerQualityDatasetValidationError("dataset root must be an array")

    source_files = _source_files_by_normalized_name(root)
    manifest_by_name = _manifest_by_normalized_name(source_manifest)
    complete_manifest = tuple(manifest_by_name[name] for name in sorted(manifest_by_name))
    for file_name, entry in manifest_by_name.items():
        candidate = source_files.get(file_name)
        if candidate is None or _sha256(candidate) != entry.original_sha256:
            raise AnswerQualityDatasetValidationError(
                "approved source manifest does not match source root"
            )
    cases: list[AnswerQualityCase] = []
    seen_ids: set[int] = set()
    referenced_documents: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "document",
            "question",
            "answer",
        }:
            raise AnswerQualityDatasetValidationError("dataset case schema is invalid")
        case_id = item["id"]
        question = item["question"]
        answer = item["answer"]
        documents = item["document"]
        if (
            type(case_id) is not int
            or case_id < 1
            or case_id in seen_ids
            or not isinstance(question, str)
            or not question.strip()
            or not isinstance(answer, str)
            or not answer.strip()
            or not isinstance(documents, list)
            or any(not isinstance(value, str) or not value.strip() for value in documents)
        ):
            raise AnswerQualityDatasetValidationError("dataset case values are invalid")
        normalized_documents = tuple(_normalize_document_ref(value) for value in documents)
        if len(normalized_documents) != len(set(normalized_documents)):
            raise AnswerQualityDatasetValidationError(
                "dataset case document references are ambiguous"
            )
        if any(
            value not in source_files or value not in manifest_by_name
            for value in normalized_documents
        ):
            raise AnswerQualityDatasetValidationError(
                "dataset document reference is unavailable"
            )
        seen_ids.add(case_id)
        referenced_documents.update(normalized_documents)
        cases.append(
            AnswerQualityCase(
                case_id=str(case_id),
                question=question.strip(),
                expected_answer=answer.strip(),
                document_refs=normalized_documents,
                source_ids=tuple(
                    manifest_by_name[value].source_id
                    for value in normalized_documents
                ),
                source_revision_ids=tuple(
                    manifest_by_name[value].source_revision_id
                    for value in normalized_documents
                ),
                retrieval_source_ids=tuple(
                    manifest_by_name[value].source_id
                    for value in normalized_documents
                )
                or tuple(entry.source_id for entry in complete_manifest),
                retrieval_source_revision_ids=tuple(
                    manifest_by_name[value].source_revision_id
                    for value in normalized_documents
                )
                or tuple(entry.source_revision_id for entry in complete_manifest),
            )
        )

    source_bound = sum(bool(case.document_refs) for case in cases)
    observed_population = (
        len(cases),
        source_bound,
        len(cases) - source_bound,
        len(referenced_documents),
    )
    expected_population = (
        identity.case_count,
        identity.source_bound_case_count,
        identity.unscoped_case_count,
        identity.unique_document_count,
    )
    if observed_population != expected_population:
        raise AnswerQualityDatasetValidationError(
            "dataset population does not match approved identity"
        )

    dataset = AnswerQualityDataset(
        version=identity.version,
        sha256=identity.sha256,
        cases=tuple(cases),
    )
    return LoadedAnswerQualityDataset(
        dataset=dataset,
        validation_report=AnswerQualityDatasetValidationReport(
            dataset_version=identity.version,
            dataset_sha256=identity.sha256,
            case_count=len(cases),
            source_bound_case_count=source_bound,
            unscoped_case_count=len(cases) - source_bound,
            unique_document_count=len(referenced_documents),
            source_files_verified=len(referenced_documents),
        ),
    )


def answer_quality_observation_input(
    case: AnswerQualityCase,
    *,
    dataset: AnswerQualityDataset,
) -> RealCaseObservationInput:
    """Release only gold-blind fields needed by canonical runtime mapping."""

    if type(case) is not AnswerQualityCase or case not in dataset.cases:
        raise AnswerQualityDatasetValidationError(
            "answer-quality case does not belong to the validated dataset"
        )
    return RealCaseObservationInput.from_gold_blind_fields(
        dataset_version=dataset.version,
        dataset_sha256=dataset.sha256,
        case_id=case.case_id,
        question=case.question,
        language="ru",
        source_ids=case.retrieval_source_ids,
        source_revision_ids=case.retrieval_source_revision_ids,
        available_source_revision_ids=case.retrieval_source_revision_ids,
    )


def _source_files_by_normalized_name(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for candidate in root.iterdir():
        if not candidate.is_file() or candidate.is_symlink():
            continue
        normalized = unicodedata.normalize("NFC", candidate.name)
        if normalized in result:
            raise AnswerQualityDatasetValidationError(
                "approved source root contains ambiguous names"
            )
        result[normalized] = candidate
    return result


def _normalize_document_ref(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if PurePath(normalized).name != normalized or normalized in {".", ".."}:
        raise AnswerQualityDatasetValidationError(
            "dataset document reference must be a file name"
        )
    return normalized


def _load_source_manifest(
    path: Path | str,
    identity: AnswerQualityDatasetIdentity,
) -> AnswerQualitySourceManifest:
    try:
        encoded = Path(path).expanduser().resolve().read_bytes()
    except OSError as exc:
        raise AnswerQualityDatasetValidationError(
            "approved source manifest is unavailable"
        ) from exc
    if hashlib.sha256(encoded).hexdigest() != identity.source_manifest_sha256:
        raise AnswerQualityDatasetValidationError(
            "source manifest digest does not match approved identity"
        )
    try:
        return AnswerQualitySourceManifest.model_validate_json(encoded)
    except Exception as exc:
        raise AnswerQualityDatasetValidationError(
            "approved source manifest schema is invalid"
        ) from exc


def _manifest_by_normalized_name(
    manifest: AnswerQualitySourceManifest,
) -> dict[str, AnswerQualitySourceManifestEntry]:
    result: dict[str, AnswerQualitySourceManifestEntry] = {}
    source_ids: set[str] = set()
    revision_ids: set[str] = set()
    for entry in manifest.documents:
        file_name = _normalize_document_ref(entry.file_name)
        if (
            file_name in result
            or entry.source_id in source_ids
            or entry.source_revision_id in revision_ids
        ):
            raise AnswerQualityDatasetValidationError(
                "approved source manifest contains ambiguous identities"
            )
        result[file_name] = entry
        source_ids.add(entry.source_id)
        revision_ids.add(entry.source_revision_id)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ANSWER_QUALITY_DATASET_SCHEMA",
    "ANSWER_QUALITY_VALIDATION_SCHEMA",
    "APPROVED_ANSWER_QUALITY_DATASET",
    "AnswerQualityCase",
    "AnswerQualityDataset",
    "AnswerQualityDatasetError",
    "AnswerQualityDatasetIdentity",
    "AnswerQualityDatasetValidationError",
    "AnswerQualityDatasetValidationReport",
    "AnswerQualitySourceManifest",
    "AnswerQualitySourceManifestEntry",
    "LoadedAnswerQualityDataset",
    "load_answer_quality_dataset",
    "answer_quality_observation_input",
]
