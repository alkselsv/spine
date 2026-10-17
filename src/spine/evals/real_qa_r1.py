"""Local validation contracts for the approved real-document R1 dataset.

Loading this dataset validates provenance and evidence structure only. It does
not execute retrieval or measure answer quality.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
from typing import Annotated, Literal, TypeAlias, Union

from openpyxl.utils.cell import range_boundaries
from pydantic import BaseModel, ConfigDict, Field, model_validator


REAL_R1_DATASET_VERSION = "0.3.9-user-approved"
REAL_R1_DATASET_DIGEST = "7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62"
REAL_R1_VALIDATOR_VERSION = "1.0.1"
REAL_R1_SCHEMA_VERSION = "qa-manifest:v1"
REAL_R1_CASE_SCHEMA_VERSION = "qa-case:v1"
_SHA256 = r"^[0-9a-f]{64}$"


class RealDatasetError(ValueError):
    """Base class for disclosure-safe real-dataset validation errors."""


class RealDatasetUnavailableError(RealDatasetError):
    """Raised when an explicitly selected local real dataset is unavailable."""


class RealDatasetValidationError(RealDatasetError):
    """Raised when the selected real dataset fails integrity validation."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PDFLocator(_Model):
    kind: Literal["pdf_page"]
    schema_version: Literal["locator:pdf-page-char:v1"]
    page: int = Field(gt=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "PDFLocator":
        if self.char_end <= self.char_start:
            raise ValueError("PDF character range must be non-empty")
        return self


class DOCXLocator(_Model):
    kind: Literal["docx_paragraph"]
    schema_version: Literal["locator:docx-paragraph:v1"]
    paragraph: int = Field(gt=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    heading: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "DOCXLocator":
        if self.char_end <= self.char_start:
            raise ValueError("DOCX character range must be non-empty")
        return self


class XLSXLocator(_Model):
    kind: Literal["xlsx_cell_range"]
    schema_version: Literal["locator:xlsx-cell:v1"]
    sheet: str = Field(min_length=1)
    row: int = Field(gt=0)
    cell_range: str = Field(pattern=r"^[A-Z]+[0-9]+(?::[A-Z]+[0-9]+)?$")


RealLocator: TypeAlias = Annotated[
    Union[PDFLocator, DOCXLocator, XLSXLocator], Field(discriminator="kind")
]


class RealEvidence(_Model):
    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_revision_id: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    locator: RealLocator
    redaction_applied: bool


class RealClaim(_Model):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class RealCitation(_Model):
    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_revision_id: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    locator: RealLocator
    redaction_applied: bool | None = None


class RealQACase(_Model):
    case_schema_version: Literal["qa-case:v1"]
    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    language: str = Field(min_length=1)
    expected_outcome: Literal["answered", "abstained", "denied"]
    expected_answer: str | None = None
    golden_answer: str | None = None
    expected_claims: tuple[RealClaim, ...] = ()
    expected_evidence_ids: tuple[str, ...] = ()
    citations: tuple[RealCitation, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_files: tuple[str, ...] = ()
    source_revision_ids: tuple[str, ...] = ()
    available_source_revision_ids: tuple[str, ...] = ()
    expected_reason: str | None = None
    question_type: str = Field(min_length=1)
    difficulty: Literal["easy", "medium", "hard"]
    scenario_tags: tuple[str, ...] = ()
    question_review_status: str | None = None
    review_status: str | None = None
    semantic_verification_status: str | None = None
    synthetic: Literal[False] = False

    @model_validator(mode="after")
    def validate_outcome_and_links(self) -> "RealQACase":
        if self.expected_outcome == "answered":
            if not (self.expected_answer or self.golden_answer) or not self.expected_claims or not self.expected_evidence_ids:
                raise ValueError("answered cases require an answer, claims and evidence")
            if self.expected_answer and self.golden_answer and self.expected_answer != self.golden_answer:
                raise ValueError("answer fields disagree")
        elif self.expected_answer is not None or self.golden_answer is not None or self.expected_claims or self.expected_evidence_ids or self.citations:
            raise ValueError("non-answered cases must not disclose answer or evidence")
        return self


class RealSourceObject(_Model):
    source_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    display_title: str = Field(min_length=1)
    format: Literal["pdf", "docx", "xlsx", "txt", "markdown"]
    inventory_ref: str | None = None
    authorization_status: str | None = None
    privacy_review_status: str | None = None
    anonymization_status: str | None = None


class RealSourceRevision(_Model):
    source_id: str = Field(min_length=1)
    source_revision_id: str = Field(min_length=1)
    revision_number: int = Field(gt=0)
    source_sha256: str = Field(pattern=_SHA256)
    revision_digest: str = Field(pattern=_SHA256)
    anonymized_representation_sha256: str = Field(pattern=_SHA256)
    extraction_status: str = Field(min_length=1)
    media_type: str | None = None
    extraction_unit_count: int | None = Field(default=None, ge=0)
    redaction_status: str | None = None
    revision_schema_version: str | None = None
    evidence: tuple[RealEvidence, ...] = ()


class RealQADataset(_Model):
    dataset_id: Literal["qa-r1-real-draft"]
    dataset_kind: Literal["real_document_draft"]
    schema_version: Literal["qa-manifest:v1"]
    case_schema_version: Literal["qa-case:v1"]
    version: str = Field(min_length=1)
    dataset_integrity_digest: str = Field(pattern=_SHA256)
    approval_status: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    authorization_status: str = Field(min_length=1)
    integrity_digest_algorithm: str = Field(min_length=1)
    language: str = Field(min_length=1)
    privacy_review_status: str = Field(min_length=1)
    status: str = Field(min_length=1)
    change_log: dict[str, object]
    compatibility: dict[str, object]
    created_by: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    dataset_status: str = Field(min_length=1)
    question_approval: dict[str, object]
    authorization_approval: dict[str, object]
    anonymization_status: str = Field(min_length=1)
    anonymization_confirmation: dict[str, object]
    answer_evidence_approval: dict[str, object]
    total_abstention_candidates: int = Field(ge=0)
    review_only_abstention_candidates: tuple[dict[str, object], ...] = ()
    cases: tuple[RealQACase, ...] = Field(min_length=1)
    source_objects: tuple[RealSourceObject, ...] = Field(min_length=1)
    source_revisions: tuple[RealSourceRevision, ...] = Field(min_length=1)


class SourceManifestEntry(_Model):
    source_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    format: Literal["pdf", "docx", "xlsx", "txt", "markdown"]
    revision_id: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)
    anonymized_representation_sha256: str = Field(pattern=_SHA256)
    anonymized_sha256: str | None = None
    abstention_candidate_scope_count: int | None = Field(default=None, ge=0)
    anonymization_status: str | None = None
    confirmed_question_count: int | None = Field(default=None, ge=0)
    duplicate_group: str | None = None
    extracted_text_chars: int | None = Field(default=None, ge=0)
    extracted_units: int | None = Field(default=None, ge=0)
    extraction_status: str | None = None
    limitations: tuple[object, ...] = ()
    privacy_review_status: str | None = None
    processing_errors: tuple[object, ...] = ()
    proposed_question_count: int | None = Field(default=None, ge=0)
    question_count_confirmed: int | None = Field(default=None, ge=0)
    question_count_proposed: int | None = Field(default=None, ge=0)
    redacted_units_count: int | None = Field(default=None, ge=0)
    redaction_status: str | None = None
    security_notes: tuple[object, ...] = ()
    size_bytes: int | None = Field(default=None, ge=0)
    structure: dict[str, object] = Field(default_factory=dict)


class RealSourceManifest(_Model):
    schema_version: Literal["r1-source-inventory:v1"]
    dataset_id: Literal["r1-real-documents-draft"]
    dataset_version: str = Field(min_length=1)
    dataset_integrity_digest: str = Field(pattern=_SHA256)
    source_count: int = Field(ge=0)
    documents_processed: int = Field(ge=0)
    source_root: str = Field(min_length=1)
    documents: tuple[SourceManifestEntry, ...]
    authorization_status: str | None = None
    dataset_status: str | None = None
    documents_with_redactions: int | None = Field(default=None, ge=0)
    duplicate_groups: dict[str, object] = Field(default_factory=dict)
    duplicates: str | None = None
    generated_at: str | None = None
    privacy_review_status: str | None = None
    redaction_method: str | None = None
    total_question_candidates: int | None = Field(default=None, ge=0)
    total_questions_human_confirmed: int | None = Field(default=None, ge=0)
    total_answered_candidates: int | None = Field(default=None, ge=0)
    total_abstention_candidates: int | None = Field(default=None, ge=0)
    review_only_abstention_candidates: tuple[dict[str, object], ...] = ()
    question_prompt_approval: dict[str, object] = Field(default_factory=dict)
    authorization_approval: dict[str, object] = Field(default_factory=dict)
    anonymization_status: str | None = None
    anonymization_confirmation: dict[str, object] = Field(default_factory=dict)
    total_answers_evidence_human_confirmed: int | None = Field(default=None, ge=0)
    answer_evidence_approval: dict[str, object] = Field(default_factory=dict)
    human_approved_case_count: int | None = Field(default=None, ge=0)


class RealDatasetValidationReport(_Model):
    validation_status: Literal["passed", "blocked"]
    evaluation_status: Literal["not_run"] = "not_run"
    dataset_id: str
    version: str
    digest: str = Field(pattern=_SHA256)
    validator_version: str
    csv_consistency_status: Literal["passed", "failed"]
    csv_mismatch_case_count: int = Field(ge=0)
    csv_mismatch_fields: tuple[str, ...] = ()
    evidence_integrity_status: Literal["passed", "failed"]
    locator_mismatch_count: int = Field(ge=0)
    locator_mismatch_by_kind: dict[str, int] = Field(default_factory=dict)
    case_count: int = Field(ge=0)
    answered_case_count: int = Field(ge=0)
    abstained_case_count: int = Field(ge=0)
    denied_case_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    source_files_verified: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    claim_count: int = Field(ge=0)
    quality_metrics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome_counts(self) -> "RealDatasetValidationReport":
        if self.answered_case_count + self.abstained_case_count + self.denied_case_count != self.case_count:
            raise ValueError("outcome counts must sum to the case count")
        return self


class LoadedRealDataset(_Model):
    dataset: RealQADataset | None
    validation_report: RealDatasetValidationReport

    @model_validator(mode="after")
    def validate_readiness(self) -> "LoadedRealDataset":
        if (self.validation_report.validation_status == "passed") != (self.dataset is not None):
            raise ValueError("blocked datasets cannot expose cases and passed datasets require the dataset")
        return self


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_read_json(path: Path) -> dict[str, object]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RealDatasetValidationError(f"invalid JSON input: {path.name}") from exc
    if not isinstance(result, dict):
        raise RealDatasetValidationError(f"JSON root must be an object: {path.name}")
    return result


def _validate_locator_excerpt(citation: RealCitation, units: list[dict[str, object]]) -> None:
    locator = citation.locator
    for unit in units:
        if locator.kind in {"pdf_page", "docx_paragraph"}:
            if locator.kind == "pdf_page":
                matches = unit.get("kind") == "pdf_page" and unit.get("page") == locator.page
            else:
                matches = unit.get("kind") == "docx_paragraph" and unit.get("paragraph") == locator.paragraph
                if matches and locator.heading is not None:
                    matches = unit.get("heading") == locator.heading
            text = unit.get("text")
            if matches and isinstance(text, str) and locator.char_end <= len(text):
                if text[locator.char_start:locator.char_end] == citation.excerpt:
                    return
        elif unit.get("kind") == "xlsx_row" and unit.get("sheet") == locator.sheet and unit.get("row") == locator.row:
            cell_values = unit.get("cells")
            if isinstance(cell_values, list):
                try:
                    min_column, min_row, max_column, max_row = range_boundaries(locator.cell_range)
                except ValueError:
                    continue
                if min_row != locator.row or max_row != locator.row:
                    continue
                cells = {
                    str(item.get("cell")): "" if item.get("value") is None else str(item.get("value"))
                    for item in cell_values
                    if isinstance(item, dict)
                }
                selected = [
                    (cell, value)
                    for cell, value in cells.items()
                    if _cell_row(cell) == locator.row
                    and min_column <= _cell_column(cell) <= max_column
                ]
                if not selected:
                    continue
                # Some extracted XLSX evidence is a complete row serialized as
                # stable A1 references. Accept it only when the locator bounds
                # exactly the referenced non-empty cells; a wider range or a
                # single-cell locator cannot borrow text from its row neighbors.
                nonempty_selected = [(cell, value) for cell, value in selected if value]
                labeled_excerpt = " | ".join(f"{cell}={value}" for cell, value in nonempty_selected)
                excerpt_cells = re.findall(r"(?<![A-Z0-9])([A-Z]+[0-9]+)=", citation.excerpt)
                if (
                    len(excerpt_cells) == len(nonempty_selected)
                    and set(excerpt_cells) == {cell for cell, _ in nonempty_selected}
                    and min_column == min((_cell_column(cell) for cell in excerpt_cells), default=-1)
                    and max_column == max((_cell_column(cell) for cell in excerpt_cells), default=-1)
                    and citation.excerpt == labeled_excerpt
                ):
                    return
                if len(selected) == 1 and citation.excerpt in selected[0][1]:
                    return
    raise RealDatasetValidationError("citation locator excerpt does not match anonymized source unit")


def _column_number(column: str) -> int:
    value = 0
    for character in column:
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def _cell_column(cell: str) -> int:
    match = re.match(r"[A-Z]+", cell)
    return _column_number(match.group()) if match else -1


def _cell_row(cell: str) -> int:
    match = re.search(r"[0-9]+$", cell)
    return int(match.group()) if match else -1


def load_real_r1_dataset(
    dataset_dir: Path | str,
    *,
    source_root: Path | str,
) -> LoadedRealDataset:
    """Load and validate the explicitly selected local user-approved dataset."""
    root = Path(dataset_dir).expanduser().resolve()
    source_dir = Path(source_root).expanduser().resolve()
    json_path = root / "golden_qa.json"
    manifest_path = root / "sources_manifest.json"
    csv_path = root / "golden_qa.csv"
    if not root.is_dir() or not source_dir.is_dir() or not all(p.is_file() for p in (json_path, manifest_path, csv_path)):
        raise RealDatasetUnavailableError("selected local real dataset or required manifest files are unavailable")

    raw = _safe_read_json(json_path)
    supplied_digest = raw.get("dataset_integrity_digest")
    actual_digest = hashlib.sha256(_canonical_json({key: value for key, value in raw.items() if key != "dataset_integrity_digest"})).hexdigest()
    if supplied_digest != actual_digest:
        raise RealDatasetValidationError("dataset digest does not match canonical JSON")
    if raw.get("version") != REAL_R1_DATASET_VERSION or supplied_digest != REAL_R1_DATASET_DIGEST:
        raise RealDatasetValidationError("dataset version or approved digest does not match pinned identity")
    try:
        dataset = RealQADataset.model_validate(raw)
    except Exception as exc:
        raise RealDatasetValidationError("real dataset does not satisfy the versioned schema") from exc

    manifest_raw = _safe_read_json(manifest_path)
    try:
        source_manifest = RealSourceManifest.model_validate(manifest_raw)
    except Exception as exc:
        raise RealDatasetValidationError("source manifest does not satisfy the versioned schema") from exc
    if source_manifest.dataset_version != dataset.version or source_manifest.dataset_integrity_digest != actual_digest:
        raise RealDatasetValidationError("source manifest dataset identity does not match")
    manifest_entries: dict[str, SourceManifestEntry] = {}
    for entry in source_manifest.documents:
        if entry.source_id in manifest_entries:
            raise RealDatasetValidationError("source manifest contains duplicate source identities")
        manifest_entries[entry.source_id] = entry

    objects = {item.source_id: item for item in dataset.source_objects}
    revisions = {item.source_revision_id: item for item in dataset.source_revisions}
    if len(objects) != len(dataset.source_objects) or len(revisions) != len(dataset.source_revisions):
        raise RealDatasetValidationError("source and revision identities must be unique")
    if set(objects) != set(manifest_entries):
        raise RealDatasetValidationError("source manifest and dataset sources do not match")
    if source_manifest.source_count != len(manifest_entries) or source_manifest.documents_processed != len(manifest_entries):
        raise RealDatasetValidationError("source manifest count does not match its entries")
    citation_by_id: dict[str, tuple[RealEvidence, str]] = {}
    for revision in dataset.source_revisions:
        if revision.source_id not in objects:
            raise RealDatasetValidationError("source revision references an unknown source")
        entry = manifest_entries[revision.source_id]
        if objects[revision.source_id].format != entry.format:
            raise RealDatasetValidationError("source format and manifest do not match")
        if revision.source_revision_id != entry.revision_id or revision.source_sha256 != entry.sha256:
            raise RealDatasetValidationError("source revision and manifest identity do not match")
        if revision.revision_digest != revision.source_sha256:
            raise RealDatasetValidationError("source revision digest does not match source digest")
        if revision.anonymized_representation_sha256 != entry.anonymized_representation_sha256:
            raise RealDatasetValidationError("anonymized source digest and manifest do not match")
        source_path = (source_dir / entry.path).resolve()
        if source_dir not in source_path.parents or not source_path.is_file():
            raise RealDatasetValidationError("manifest source path is unavailable or outside the source root")
        if _sha256(source_path) != entry.sha256:
            raise RealDatasetValidationError("source file digest does not match manifest")
        anon_path = root / "anonymized" / f"{revision.source_id}.json"
        if not anon_path.is_file() or _sha256(anon_path) != revision.anonymized_representation_sha256:
            raise RealDatasetValidationError("anonymized source representation digest does not match")
        anon = _safe_read_json(anon_path)
        if anon.get("source_id") != revision.source_id or anon.get("revision_id") != revision.source_revision_id or anon.get("sha256") != revision.source_sha256:
            raise RealDatasetValidationError("anonymized source representation identity does not match")
        units = anon.get("units")
        if not isinstance(units, list) or any(not isinstance(unit, dict) for unit in units):
            raise RealDatasetValidationError("anonymized source units are invalid")
        for evidence in revision.evidence:
            if evidence.source_id != revision.source_id or evidence.source_revision_id != revision.source_revision_id:
                raise RealDatasetValidationError("evidence does not match its containing source revision")
            if evidence.evidence_id in citation_by_id:
                raise RealDatasetValidationError("evidence identities must be unique")
            citation_by_id[evidence.evidence_id] = (evidence, str(revision.source_revision_id))

    case_ids: set[str] = set()
    claim_count = 0
    locator_mismatch_count = 0
    locator_mismatch_by_kind: dict[str, int] = {}
    for case in dataset.cases:
        if case.case_id in case_ids:
            raise RealDatasetValidationError("case identities must be unique")
        case_ids.add(case.case_id)
        if (
            len(set(case.source_ids)) != len(case.source_ids)
            or len(set(case.source_files)) != len(case.source_files)
            or len(set(case.source_revision_ids)) != len(case.source_revision_ids)
        ):
            raise RealDatasetValidationError("case source references must be unique")
        if not set(case.source_ids) <= set(objects) or not set(case.source_revision_ids) <= set(revisions):
            raise RealDatasetValidationError("case references an unknown source or revision")
        if set(case.source_files) != {manifest_entries[source_id].path for source_id in case.source_ids}:
            raise RealDatasetValidationError("case source filenames do not match source identities")
        revision_ids_by_source = {revision.source_id: revision.source_revision_id for revision in dataset.source_revisions}
        if any(revision_ids_by_source.get(source_id) not in case.source_revision_ids for source_id in case.source_ids):
            raise RealDatasetValidationError("case source revisions do not match source identities")
        if not set(case.available_source_revision_ids) <= set(revisions):
            raise RealDatasetValidationError("case available revisions reference an unknown revision")
        claim_count += len(case.expected_claims)
        claim_ids = [claim.claim_id for claim in case.expected_claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise RealDatasetValidationError("case claim identities must be unique")
        expected = set(case.expected_evidence_ids)
        cited = {citation.evidence_id for citation in case.citations}
        if len(expected) != len(case.expected_evidence_ids) or cited != expected:
            raise RealDatasetValidationError("case evidence IDs and citations do not match")
        claims_evidence = {evidence_id for claim in case.expected_claims for evidence_id in claim.evidence_ids}
        if not claims_evidence <= expected:
            raise RealDatasetValidationError("claim references evidence outside the case evidence set")
        if any(len(set(claim.evidence_ids)) != len(claim.evidence_ids) for claim in case.expected_claims):
            raise RealDatasetValidationError("claim evidence references must be unique")
        for citation in case.citations:
            stored = citation_by_id.get(citation.evidence_id)
            if stored is None or any(
                getattr(citation, field) != getattr(stored[0], field)
                for field in ("evidence_id", "source_id", "source_revision_id", "excerpt", "locator")
            ):
                raise RealDatasetValidationError("case citation does not match its source revision evidence")
            if citation.source_id not in case.source_ids or citation.source_revision_id not in case.source_revision_ids:
                raise RealDatasetValidationError("citation source is not included in the case provenance")
            anon_path = root / "anonymized" / f"{citation.source_id}.json"
            anon = _safe_read_json(anon_path)
            try:
                _validate_locator_excerpt(citation, anon["units"])
            except RealDatasetValidationError:
                locator_mismatch_count += 1
                locator_mismatch_by_kind[citation.locator.kind] = locator_mismatch_by_kind.get(citation.locator.kind, 0) + 1

    csv_mismatch_cases: set[str] = set()
    csv_mismatch_fields: set[str] = set()
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        csv_by_id = {row.get("case_id"): row for row in rows}
        raw_cases = {item.get("case_id"): item for item in raw.get("cases", []) if isinstance(item, dict)}
        if len(csv_by_id) != len(rows) or set(csv_by_id) != case_ids:
            raise RealDatasetValidationError("CSV and JSON case identities do not match")
        for case in dataset.cases:
            row = csv_by_id[case.case_id]
            expected_fields = {
                "dataset_version": dataset.version,
                "dataset_digest": actual_digest,
                "question": case.question,
                "golden_answer": case.golden_answer or case.expected_answer or "",
                "expected_outcome": case.expected_outcome,
                "question_type": case.question_type,
                "difficulty": case.difficulty,
                "source_files": ";".join(case.source_files),
                "source_revision_ids": ";".join(case.source_revision_ids),
                "evidence_ids": ";".join(case.expected_evidence_ids),
                "scenario_tags": ";".join(case.scenario_tags),
                "question_review_status": case.question_review_status or "",
                "review_status": case.review_status or "",
            }
            for field, expected_value in expected_fields.items():
                if row.get(field) != expected_value:
                    csv_mismatch_cases.add(case.case_id)
                    csv_mismatch_fields.add(field)
            try:
                csv_citations = json.loads(row.get("citations", ""))
            except json.JSONDecodeError:
                csv_mismatch_cases.add(case.case_id)
                csv_mismatch_fields.add("citations")
            else:
                if csv_citations != raw_cases[case.case_id].get("citations"):
                    csv_mismatch_cases.add(case.case_id)
                    csv_mismatch_fields.add("citations")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RealDatasetValidationError("CSV dataset export is invalid") from exc

    report = RealDatasetValidationReport(
        validation_status="blocked" if csv_mismatch_cases or locator_mismatch_count else "passed",
        dataset_id=dataset.dataset_id,
        version=dataset.version,
        digest=actual_digest,
        validator_version=REAL_R1_VALIDATOR_VERSION,
        csv_consistency_status="failed" if csv_mismatch_cases else "passed",
        csv_mismatch_case_count=len(csv_mismatch_cases),
        csv_mismatch_fields=tuple(sorted(csv_mismatch_fields)),
        evidence_integrity_status="failed" if locator_mismatch_count else "passed",
        locator_mismatch_count=locator_mismatch_count,
        locator_mismatch_by_kind=locator_mismatch_by_kind,
        case_count=len(dataset.cases),
        answered_case_count=sum(case.expected_outcome == "answered" for case in dataset.cases),
        abstained_case_count=sum(case.expected_outcome == "abstained" for case in dataset.cases),
        denied_case_count=sum(case.expected_outcome == "denied" for case in dataset.cases),
        source_count=len(objects),
        source_files_verified=len(objects),
        evidence_count=len(citation_by_id),
        claim_count=claim_count,
    )
    if report.validation_status == "blocked":
        return LoadedRealDataset(dataset=None, validation_report=report)
    return LoadedRealDataset(dataset=dataset, validation_report=report)
