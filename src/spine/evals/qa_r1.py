"""Versioned, synthetic offline Q&A evaluation contracts and dataset loader."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, TypeAlias, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


QA_R1_SCHEMA_VERSION = "qa-manifest:v1"
QA_R1_CASE_SCHEMA_VERSION = "qa-case:v1"
_DIGEST = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"
_POSITIVE_INT = Annotated[int, Field(gt=0)]
_NON_EMPTY = Annotated[str, Field(min_length=1)]
_Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=_IDENTIFIER)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PDFPageLocator(_FrozenModel):
    kind: Literal["pdf_page"]
    schema_version: Literal["locator:pdf-page:v1"]
    page: _POSITIVE_INT


class XLSXCellLocator(_FrozenModel):
    kind: Literal["xlsx_cell"]
    schema_version: Literal["locator:xlsx-cell:v1"]
    sheet: _NON_EMPTY
    row: _POSITIVE_INT
    cell_range: Annotated[str, Field(min_length=1, pattern=r"^[A-Z]+[0-9]+(?::[A-Z]+[0-9]+)?$")]


class TextRangeLocator(_FrozenModel):
    kind: Literal["text_range"]
    schema_version: Literal["locator:text-range:v1"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "TextRangeLocator":
        if self.end <= self.start:
            raise ValueError("text range end must be greater than start")
        return self


class MessageLocator(_FrozenModel):
    kind: Literal["message"]
    schema_version: Literal["locator:message:v1"]
    message_id: _NON_EMPTY


Locator: TypeAlias = Annotated[
    Union[PDFPageLocator, XLSXCellLocator, TextRangeLocator, MessageLocator],
    Field(discriminator="kind"),
]


class SourceObjectFixture(_FrozenModel):
    source_object_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_kind: Literal["document", "message", "record"]
    external_id: _Identifier
    title: _NON_EMPTY


class EvidenceItem(_FrozenModel):
    evidence_id: _Identifier
    source_revision_id: UUID
    excerpt: _NON_EMPTY
    locator: Locator


class SourceRevisionFixture(_FrozenModel):
    revision_id: UUID
    source_object_id: UUID
    workspace_id: UUID
    environment_id: UUID
    kind: Literal["content", "tombstone"]
    revision_number: _POSITIVE_INT
    media_type: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")] | None = None
    content: str | None = None
    tombstone_reason: _Identifier | None = None
    evidence: tuple[EvidenceItem, ...] = ()

    @model_validator(mode="after")
    def validate_kind(self) -> "SourceRevisionFixture":
        if self.kind == "content":
            if not self.media_type or self.content is None or self.tombstone_reason is not None:
                raise ValueError("content revisions require content metadata and no tombstone reason")
            if not self.evidence:
                raise ValueError("content revisions require evidence items")
        elif self.content is not None or self.media_type is not None or self.evidence or not self.tombstone_reason:
            raise ValueError("tombstones contain only deletion provenance")
        return self


class ACLFixture(_FrozenModel):
    source_object_id: UUID
    principal_id: UUID
    read_content: bool


class ExpectedClaim(_FrozenModel):
    claim_id: _Identifier
    text: _NON_EMPTY
    evidence_ids: tuple[_Identifier, ...] = Field(min_length=1)


class RelevanceLabel(_FrozenModel):
    evidence_id: _Identifier
    label: Literal["relevant", "irrelevant", "contradictory"]


class QACase(_FrozenModel):
    case_schema_version: Literal[QA_R1_CASE_SCHEMA_VERSION]
    case_id: _Identifier
    question: _NON_EMPTY
    language: _NON_EMPTY
    workspace_id: UUID
    environment_id: UUID
    principal_id: UUID
    expected_outcome: Literal["answered", "clarification_required", "abstained", "denied"]
    expected_answer: str | None = None
    expected_claims: tuple[ExpectedClaim, ...] = ()
    expected_evidence_ids: tuple[_Identifier, ...] = ()
    expected_reason: _Identifier | None = None
    available_revision_ids: tuple[UUID, ...] = Field(min_length=1)
    acl: tuple[ACLFixture, ...] = ()
    relevance_labels: tuple[RelevanceLabel, ...] = ()
    observation_ids: tuple[_Identifier, ...] = ()
    retrieval_strategy: _Identifier
    scenario_tags: tuple[_Identifier, ...] = Field(min_length=1)
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def validate_outcome(self) -> "QACase":
        answerable = self.expected_outcome == "answered"
        if answerable and (not self.expected_answer or not self.expected_claims or not self.expected_evidence_ids):
            raise ValueError("answered cases require an answer, claims and evidence")
        if not answerable and (self.expected_answer is not None or self.expected_claims or self.expected_evidence_ids):
            raise ValueError("non-answered cases must not disclose an expected answer or evidence")
        if self.expected_outcome == "answered" and self.expected_reason is not None:
            raise ValueError("answered cases do not have an abstention reason")
        if self.expected_outcome != "answered" and self.expected_reason is None:
            raise ValueError("non-answered cases require a disclosure-safe reason")
        if len(set(self.scenario_tags)) != len(self.scenario_tags):
            raise ValueError("scenario tags must be unique")
        return self


class QADataset(_FrozenModel):
    dataset_id: Literal["qa-r1-synthetic"]
    version: Literal["1.0.0"]
    schema_version: Literal[QA_R1_SCHEMA_VERSION]
    fixture_schema_version: Literal[QA_R1_CASE_SCHEMA_VERSION]
    dataset_digest: Annotated[str, Field(pattern=_DIGEST)]
    assumptions: tuple[_NON_EMPTY, ...] = ()
    release_thresholds: tuple[_Identifier, ...] = ()
    source_objects: tuple[SourceObjectFixture, ...] = ()
    source_revisions: tuple[SourceRevisionFixture, ...] = ()
    cases: tuple[QACase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references_and_digest(self) -> "QADataset":
        object_by_id = {item.source_object_id: item for item in self.source_objects}
        revision_by_id = {item.revision_id: item for item in self.source_revisions}
        evidence_by_id = {
            item.evidence_id: (revision, item)
            for revision in self.source_revisions
            for item in revision.evidence
        }
        if len(object_by_id) != len(self.source_objects):
            raise ValueError("source object identities must be unique")
        if len(revision_by_id) != len(self.source_revisions):
            raise ValueError("source revision identities must be unique")
        if len(evidence_by_id) != sum(len(revision.evidence) for revision in self.source_revisions):
            raise ValueError("evidence identities must be unique")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case identities must be unique")

        for revision in self.source_revisions:
            source_object = object_by_id.get(revision.source_object_id)
            if source_object is None:
                raise ValueError("source revision references an unknown source object")
            if (revision.workspace_id, revision.environment_id) != (
                source_object.workspace_id,
                source_object.environment_id,
            ):
                raise ValueError("source revision scope must match its source object")
            for evidence in revision.evidence:
                if evidence.source_revision_id != revision.revision_id:
                    raise ValueError("evidence must reference its containing source revision")

        for case in self.cases:
            revisions = [revision_by_id.get(revision_id) for revision_id in case.available_revision_ids]
            if any(revision is None for revision in revisions):
                raise ValueError("case references an unknown source revision")
            if any(
                (revision.workspace_id, revision.environment_id) != (case.workspace_id, case.environment_id)
                for revision in revisions
                if revision is not None
            ):
                raise ValueError("case cannot retrieve across workspace or environment boundaries")
            for evidence_id in case.expected_evidence_ids:
                revision, evidence = evidence_by_id.get(evidence_id, (None, None))
                if revision is None or evidence is None:
                    raise ValueError("case references unknown expected evidence")
                if revision.revision_id not in case.available_revision_ids:
                    raise ValueError("expected evidence must belong to an available revision")
                if (revision.workspace_id, revision.environment_id) != (case.workspace_id, case.environment_id):
                    raise ValueError("expected evidence crosses a workspace boundary")
            expected_evidence = set(case.expected_evidence_ids)
            for claim in case.expected_claims:
                if not set(claim.evidence_ids) <= expected_evidence:
                    raise ValueError("claim evidence must be listed as expected evidence")
            for label in case.relevance_labels:
                if label.evidence_id not in evidence_by_id:
                    raise ValueError("relevance label references unknown evidence")
            for grant in case.acl:
                source_object = object_by_id.get(grant.source_object_id)
                if source_object is None:
                    raise ValueError("ACL references an unknown source object")
                if (source_object.workspace_id, source_object.environment_id) != (
                    case.workspace_id,
                    case.environment_id,
                ):
                    raise ValueError("ACL fixture crosses a workspace boundary")
                if grant.principal_id != case.principal_id:
                    raise ValueError("ACL fixture must describe the case principal")

        expected_digest = hashlib.sha256(_canonical_payload(self)).hexdigest()
        if expected_digest != self.dataset_digest:
            raise ValueError("dataset digest does not match the canonical manifest")
        return self


def _canonical_payload(dataset: QADataset) -> bytes:
    payload = dataset.model_dump(mode="json", exclude={"dataset_digest"})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def serialize_dataset(dataset: QADataset) -> bytes:
    """Return the stable JSON representation used for fixture replay and CI."""

    payload = dataset.model_dump(mode="json")
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


@lru_cache(maxsize=1)
def load_r1_dataset() -> QADataset:
    """Load and validate the checked-in synthetic R1 Q&A manifest."""

    path = Path(__file__).with_name("data") / "qa_r1.json"
    return QADataset.model_validate_json(path.read_bytes())


__all__ = [
    "ACLFixture",
    "EvidenceItem",
    "ExpectedClaim",
    "Locator",
    "MessageLocator",
    "PDFPageLocator",
    "QACase",
    "QADataset",
    "QA_R1_CASE_SCHEMA_VERSION",
    "QA_R1_SCHEMA_VERSION",
    "RelevanceLabel",
    "SourceObjectFixture",
    "SourceRevisionFixture",
    "TextRangeLocator",
    "XLSXCellLocator",
    "load_r1_dataset",
    "serialize_dataset",
]
