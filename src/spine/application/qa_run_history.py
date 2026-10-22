"""Safe read models for persisted answer-question runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from spine.application.persistence.answer_disclosure import AnswerDisclosureRepository
from spine.application.persistence.answer_question_runs import (
    AnswerQuestionRunPosition,
    AnswerQuestionRunQuery,
)
from spine.application.persistence.errors import AnswerQuestionRunNotFoundError
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.capabilities.answer_question_runs import (
    AnswerQuestionAttempt,
    AnswerQuestionRun,
)
from spine.domain.context_bundles import ContextBundle, ContextBundleEvidenceReference
from spine.domain.disclosure import (
    DisclosureDependencySet,
    DisclosureRevalidationCheckpoint,
    DisclosureRevalidationRequest,
)


class QARunNotFound(LookupError):
    """The requested run is not visible in the trusted scope."""


class InvalidQARunRequest(ValueError):
    """A run-history query cannot be safely executed."""


class QARunHistoryFilters(BaseModel):
    """Closed, bounded operational filters for the run collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: UUID | None = None
    session_id: UUID | None = None
    message_id: UUID | None = None
    outcome: AnswerQuestionOutcome | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "QARunHistoryFilters":
        for name in ("created_after", "created_before"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.created_after and self.created_before:
            if self.created_after > self.created_before:
                raise ValueError("created_after must not exceed created_before")
            if (self.created_before - self.created_after).days > 366:
                raise ValueError("run history time range is too wide")
        return self


@dataclass(frozen=True, slots=True)
class QARunHistoryRequester:
    subject_id: UUID
    authorization_generation: int = 1

    def __post_init__(self) -> None:
        if self.subject_id.int == 0 or self.authorization_generation < 1:
            raise InvalidQARunRequest("run history requester identity is invalid")


@dataclass(frozen=True, slots=True)
class QARunHistoryPage:
    items: tuple["QARunListItem", ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class QARunListItem:
    run: AnswerQuestionRun
    attempt_count: int
    message_link: object | None


@dataclass(frozen=True, slots=True)
class QARunEvidence:
    """Content-free accepted evidence identity exposed by the run API."""

    citation_id: UUID
    evidence: ContextBundleEvidenceReference


@dataclass(frozen=True, slots=True)
class QARunDetail:
    run: AnswerQuestionRun
    attempts: tuple[AnswerQuestionAttempt, ...]
    message_link: object | None
    contexts: tuple[ContextBundle | None, ...]
    accepted_evidence: tuple[QARunEvidence, ...]
    evidence_eligible: bool


class QARunHistoryService(Protocol):
    async def list_runs(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QARunHistoryRequester,
        filters: QARunHistoryFilters,
        limit: int,
        after: AnswerQuestionRunPosition | None,
    ) -> QARunHistoryPage: ...

    async def get_run(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QARunHistoryRequester,
        run_id: UUID,
    ) -> QARunDetail | None: ...


class RunDisclosureRevalidator(Protocol):
    async def revalidate(self, request: DisclosureRevalidationRequest) -> object: ...


@dataclass(frozen=True, slots=True)
class PersistedQARunHistoryService:
    """Project canonical run/attempt records without exposing protected content."""

    uow_factory: UnitOfWorkFactory
    disclosure_revalidator: RunDisclosureRevalidator | None = None

    async def list_runs(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QARunHistoryRequester,
        filters: QARunHistoryFilters,
        limit: int,
        after: AnswerQuestionRunPosition | None,
    ) -> QARunHistoryPage:
        self._validate(context, requester)
        if limit < 1 or limit > 100:
            raise InvalidQARunRequest("run history page size is invalid")
        query = AnswerQuestionRunQuery(
            subject_id=filters.subject_id,
            session_id=filters.session_id,
            message_id=filters.message_id,
            outcome=filters.outcome.value if filters.outcome else None,
            created_after=filters.created_after,
            created_before=filters.created_before,
        )
        async with self.uow_factory(context) as uow:
            page = await uow.answer_question_runs.list_runs(
                query=query,
                limit=limit,
                after=after,
            )
            materialized_items: list[QARunListItem] = []
            for run in page.runs:
                materialized_items.append(
                    QARunListItem(
                        run=run,
                        attempt_count=len(
                            await uow.answer_question_runs.list_attempts(run.run_id)
                        ),
                        message_link=(
                            await uow.answer_question_runs.resolve_message_link(
                                run.run_id
                            )
                        ),
                    )
                )
            items = tuple(materialized_items)
            await uow.commit()
        return QARunHistoryPage(items=items, has_more=page.has_more)

    async def get_run(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QARunHistoryRequester,
        run_id: UUID,
    ) -> QARunDetail | None:
        self._validate(context, requester)
        if run_id.int == 0:
            return None
        async with self.uow_factory(context) as uow:
            try:
                run = await uow.answer_question_runs.resolve_run(run_id)
            except AnswerQuestionRunNotFoundError:
                await uow.rollback()
                return None
            attempts = await uow.answer_question_runs.list_attempts(run.run_id)
            link = await uow.answer_question_runs.resolve_message_link(run.run_id)
            contexts = await self._contexts(uow, attempts)
            evidence, eligible = await self._accepted_evidence(
                uow.answer_disclosures,
                run=run,
                attempts=attempts,
                contexts=contexts,
                requester=requester,
            )
            await uow.commit()
        return QARunDetail(
            run=run,
            attempts=attempts,
            message_link=link,
            contexts=contexts,
            accepted_evidence=evidence,
            evidence_eligible=eligible,
        )

    async def _context(self, uow: object, attempt: AnswerQuestionAttempt) -> ContextBundle | None:
        reference = attempt.context_bundle_ref
        if reference is None:
            return None
        try:
            return await uow.context_bundles.resolve(
                bundle_id=reference.bundle_id,
                bundle_version=reference.bundle_version,
                bundle_digest=reference.bundle_digest,
            )
        except Exception:
            return None

    async def _contexts(
        self,
        uow: object,
        attempts: tuple[AnswerQuestionAttempt, ...],
    ) -> tuple[ContextBundle | None, ...]:
        return tuple([await self._context(uow, attempt) for attempt in attempts])

    async def _accepted_evidence(
        self,
        disclosures: AnswerDisclosureRepository,
        *,
        run: AnswerQuestionRun,
        attempts: tuple[AnswerQuestionAttempt, ...],
        contexts: tuple[ContextBundle | None, ...],
        requester: QARunHistoryRequester,
    ) -> tuple[tuple[QARunEvidence, ...], bool]:
        if self.disclosure_revalidator is None:
            return (), False
        for attempt, bundle in reversed(tuple(zip(attempts, contexts))):
            if bundle is None:
                continue
            record = await _resolve_disclosure(disclosures, run, attempt)
            if record is None or record.accepted_answer is None:
                continue
            try:
                dependencies = DisclosureDependencySet.from_context_bundle(bundle)
                result = await self.disclosure_revalidator.revalidate(
                    DisclosureRevalidationRequest(
                        request_id=record.request_id,
                        workspace_id=run.workspace_id,
                        environment_id=run.environment_id,
                        requester_id=requester.subject_id,
                        checkpoint=DisclosureRevalidationCheckpoint.BEFORE_HISTORY_READ,
                        dependencies=dependencies,
                        run_id=run.run_id,
                        attempt_id=attempt.attempt_id,
                        force_fresh=True,
                    )
                )
                if getattr(result, "can_disclose_content", False) is not True:
                    return (), False
                evidence_by_id = {
                    item.evidence_id: item for item in bundle.evidence_references
                }
                values = tuple(
                    QARunEvidence(
                        citation_id=citation.citation_id,
                        evidence=evidence_by_id[citation.evidence_reference.evidence_id],
                    )
                    for citation in record.accepted_answer.answer.citations
                    if citation.evidence_reference.evidence_id in evidence_by_id
                    and evidence_by_id[citation.evidence_reference.evidence_id]
                    == citation.evidence_reference
                )
                if len(values) != len(record.accepted_answer.answer.citations):
                    return (), False
                return values, True
            except Exception:
                return (), False
        return (), False

    @staticmethod
    def _validate(
        context: TrustedPersistenceContext, requester: QARunHistoryRequester
    ) -> None:
        context.validate_shape()
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidQARunRequest("run history requires an environment scope")
        if context.acting_subject_id != requester.subject_id:
            raise InvalidQARunRequest("run history requester does not match context")


async def _resolve_disclosure(
    repository: AnswerDisclosureRepository,
    run: AnswerQuestionRun,
    attempt: AnswerQuestionAttempt,
) -> object | None:
    resolver = getattr(repository, "resolve_for_attempt", None)
    if resolver is None:
        return None
    return await resolver(
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        run_id=run.run_id,
        attempt_id=attempt.attempt_id,
    )


__all__ = [
    "InvalidQARunRequest",
    "PersistedQARunHistoryService",
    "QARunDetail",
    "QARunEvidence",
    "QARunHistoryFilters",
    "QARunHistoryPage",
    "QARunHistoryRequester",
    "QARunHistoryService",
    "QARunListItem",
    "QARunNotFound",
]
