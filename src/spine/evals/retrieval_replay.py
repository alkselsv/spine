"""Deterministic retrieval replay over the synthetic R1 dataset.

This adapter is an offline fixture boundary.  It exercises the provider-neutral
``ContextBroker`` contract without pretending to qualify Cognee or production
retrieval.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final
from uuid import UUID

from spine.evals.qa_r1 import EvidenceItem, QACase, QADataset, SourceRevisionFixture, load_r1_dataset
from spine.memory.contracts import ContextBroker, ContextReference, RetrievalQuery, RetrievalResult


REPLAY_INDEX_VERSION: Final[str] = "qa-r1-replay:v1"
_RELEVANT = "relevant"
_CONTRADICTORY = "contradictory"


class DeterministicRetrievalReplay(ContextBroker):
    """Replay the current, authorized fixture projection deterministically.

    The dataset is the immutable fixture input.  ``projected_revision_ids`` can
    be supplied to model a stale projection in tests; ``rebuild`` selects the
    current revision for every source object.  A current tombstone therefore
    remains a tombstone after rebuild and never revives its predecessor.
    """

    def __init__(
        self,
        dataset: QADataset | None = None,
        *,
        projected_revision_ids: Iterable[UUID] | None = None,
    ) -> None:
        self.dataset = load_r1_dataset() if dataset is None else dataset
        self._cases_by_query: dict[tuple[UUID, UUID, str], tuple[QACase, ...]] = {}
        for case in self.dataset.cases:
            key = (case.workspace_id, case.environment_id, case.question)
            self._cases_by_query[key] = (*self._cases_by_query.get(key, ()), case)
        self._cases_by_id = {case.case_id: case for case in self.dataset.cases}
        self._revisions_by_id = {revision.revision_id: revision for revision in self.dataset.source_revisions}
        self._current_revision_by_source = self._find_current_revisions()
        self._projected_revision_ids = (
            frozenset(self._current_revision_by_source.values())
            if projected_revision_ids is None
            else frozenset(projected_revision_ids)
        )
        self.index_version = REPLAY_INDEX_VERSION

    def rebuild(self) -> None:
        """Rebuild the fixture projection from canonical current revisions."""

        self._projected_revision_ids = frozenset(self._current_revision_by_source.values())

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Retrieve an unambiguous case matching the bounded query.

        A question that maps to multiple fixture cases is intentionally denied
        here.  Callers that have a trusted dataset case use ``retrieve_case``
        so one fixture cannot be selected by lossy text matching.
        """

        cases = self._cases_by_query.get((query.workspace_id, query.environment_id, query.query), ())
        if len(cases) != 1:
            return self._empty_result()
        case = cases[0]
        return await self.retrieve_case(case, query)

    async def retrieve_case(self, case: QACase, query: RetrievalQuery) -> RetrievalResult:
        """Retrieve one trusted fixture case through the Context Broker contract."""

        if self._cases_by_id.get(case.case_id) != case or not self._request_matches_case(case, query):
            return self._empty_result(case)

        revisions = self._current_projected_revisions(case)
        if not revisions:
            return self._empty_result(case)

        evidence = self._ranked_evidence(case, revisions, query.limit)
        if not evidence:
            return self._empty_result(case)

        return RetrievalResult(
            context=tuple(item.excerpt for _, item in evidence),
            references=tuple(self._reference(revision, item) for revision, item in evidence),
            retrieval_strategy=case.retrieval_strategy,
            index_version=self.index_version,
        )

    def _find_current_revisions(self) -> dict[UUID, UUID]:
        current: dict[UUID, SourceRevisionFixture] = {}
        for revision in self.dataset.source_revisions:
            previous = current.get(revision.source_object_id)
            if previous is None or (revision.revision_number, str(revision.revision_id)) > (
                previous.revision_number,
                str(previous.revision_id),
            ):
                current[revision.source_object_id] = revision
        return {source_id: revision.revision_id for source_id, revision in current.items()}

    @staticmethod
    def _request_matches_case(case: QACase, query: RetrievalQuery) -> bool:
        return (
            query.query == case.question
            and query.workspace_id == case.workspace_id
            and query.environment_id == case.environment_id
            and query.requested_by.id == case.principal_id
        )

    def _current_projected_revisions(self, case: QACase) -> tuple[SourceRevisionFixture, ...]:
        available = set(case.available_revision_ids)
        revisions: list[SourceRevisionFixture] = []
        for source_object_id, revision_id in self._current_revision_by_source.items():
            if revision_id not in available or revision_id not in self._projected_revision_ids:
                continue
            revision = self._revisions_by_id[revision_id]
            if revision.workspace_id != case.workspace_id or revision.environment_id != case.environment_id:
                continue
            if revision.kind == "content" and self._can_read(case, revision.source_object_id):
                revisions.append(revision)
        return tuple(revisions)

    def _ranked_evidence(
        self,
        case: QACase,
        revisions: tuple[SourceRevisionFixture, ...],
        limit: int,
    ) -> tuple[tuple[SourceRevisionFixture, EvidenceItem], ...]:
        evidence_by_id = {
            item.evidence_id: (revision, item)
            for revision in revisions
            for item in revision.evidence
        }
        labels = {label.evidence_id: label.label for label in case.relevance_labels}
        required = {evidence_id for evidence_id, label in labels.items() if label == _RELEVANT}

        # A missing relevant item means the answer would be partial.  The
        # fixture has no partial-fallback authorization, so fail closed.
        if required and not required <= set(evidence_by_id):
            return ()

        allowed_ids = (
            {evidence_id for evidence_id, label in labels.items() if label in {_RELEVANT, _CONTRADICTORY}}
            if labels
            else set(evidence_by_id)
        )
        ranked = [
            evidence_by_id[evidence_id]
            for evidence_id in allowed_ids
            if evidence_id in evidence_by_id
        ]
        ranked.sort(key=lambda pair: (-self._score(labels.get(pair[1].evidence_id)), pair[1].evidence_id))
        return tuple(ranked[:limit])

    def _can_read(self, case: QACase, source_object_id: UUID) -> bool:
        return any(
            grant.source_object_id == source_object_id
            and grant.principal_id == case.principal_id
            and grant.read_content
            for grant in case.acl
        )

    @staticmethod
    def _score(label: str | None) -> int:
        return {_RELEVANT: 2, _CONTRADICTORY: 1}.get(label, 0)

    def _reference(self, revision: SourceRevisionFixture, evidence: EvidenceItem) -> ContextReference:
        return ContextReference(
            source_id=str(revision.source_object_id),
            source_revision_id=revision.revision_id,
            workspace_id=revision.workspace_id,
            environment_id=revision.environment_id,
            evidence_id=evidence.evidence_id,
            locator=evidence.locator.model_dump(mode="json"),
            excerpt=evidence.excerpt,
        )

    def _empty_result(self, case: QACase | None = None) -> RetrievalResult:
        return RetrievalResult(
            retrieval_strategy=case.retrieval_strategy if case is not None else REPLAY_INDEX_VERSION,
            index_version=self.index_version,
        )


__all__ = ["DeterministicRetrievalReplay", "REPLAY_INDEX_VERSION"]
