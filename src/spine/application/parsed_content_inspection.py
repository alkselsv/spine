"""Fail-closed application query for current parsed Knowledge content."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from spine.application.content_authorization import (
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    TrustedAuthorizationContext,
)
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.parsed_artifacts import (
    ParsedArtifactMetadata,
    ParsedArtifactRepository,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.artifacts import Locator, ParseCoverage, ParsedSegment
from spine.domain.ingestion import ProcessingCoverage
from spine.domain.sources import RevisionKind


DEFAULT_PARSED_CONTENT_PAGE_SIZE = 50
MAX_PARSED_CONTENT_PAGE_SIZE = 100
MAX_SEGMENT_TEXT_BYTES = 65_536
MAX_TOTAL_TEXT_BYTES = 262_144


class ParsedContentInspectionStatus(str, Enum):
    READY = "ready"
    CONTENT_UNAVAILABLE = "content_unavailable"
    REVISION_NOT_CURRENT = "revision_not_current"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    PROCESSING_INCOMPLETE = "processing_incomplete"
    LOCATOR_INVALID = "locator_invalid"
    INTEGRITY_MISMATCH = "integrity_mismatch"
    LIMIT_EXCEEDED = "limit_exceeded"
    SERVICE_UNAVAILABLE = "service_unavailable"


class InvalidParsedContentRequest(ValueError):
    """The trusted parsed-content query is outside its bounded contract."""


@dataclass(frozen=True, slots=True)
class ParsedContentInspectionRequester:
    subject_id: UUID
    authorization_generation: int = 1
    effective_team_ids: frozenset[UUID] = frozenset()

    def __post_init__(self) -> None:
        if self.subject_id.int == 0 or self.authorization_generation < 1:
            raise InvalidParsedContentRequest("parsed content requester is invalid")
        if any(team_id.int == 0 for team_id in self.effective_team_ids):
            raise InvalidParsedContentRequest("parsed content teams are invalid")


@dataclass(frozen=True, slots=True)
class ParsedContentInspectionQuery:
    source_object_id: UUID
    source_revision_id: UUID
    processing_receipt_id: UUID
    selector: Locator | None = None
    after_segment_id: UUID | None = None
    after_artifact_id: UUID | None = None
    after_artifact_digest: str | None = None
    after_original_sha256: str | None = None
    page_size: int = DEFAULT_PARSED_CONTENT_PAGE_SIZE
    max_segment_text_bytes: int = MAX_SEGMENT_TEXT_BYTES
    max_total_text_bytes: int = MAX_TOTAL_TEXT_BYTES

    def __post_init__(self) -> None:
        for name in (
            "source_object_id",
            "source_revision_id",
            "processing_receipt_id",
        ):
            if getattr(self, name).int == 0:
                raise InvalidParsedContentRequest(f"{name} is invalid")
        if self.page_size < 1 or self.page_size > MAX_PARSED_CONTENT_PAGE_SIZE:
            raise InvalidParsedContentRequest("parsed content page size is invalid")
        if self.max_segment_text_bytes != MAX_SEGMENT_TEXT_BYTES:
            raise InvalidParsedContentRequest("segment text bound is not supported")
        if self.max_total_text_bytes != MAX_TOTAL_TEXT_BYTES:
            raise InvalidParsedContentRequest("total text bound is not supported")
        if (self.after_artifact_id is None) != (self.after_artifact_digest is None):
            raise InvalidParsedContentRequest("cursor artifact identity is incomplete")
        if (self.after_artifact_id is None) != (self.after_original_sha256 is None):
            raise InvalidParsedContentRequest("cursor source identity is incomplete")
        if self.after_segment_id is not None and self.after_segment_id.int == 0:
            raise InvalidParsedContentRequest("cursor segment identity is invalid")


@dataclass(frozen=True, slots=True)
class ParsedContentInspectionResult:
    status: ParsedContentInspectionStatus
    metadata: ParsedArtifactMetadata | None = None
    segments: tuple[ParsedSegment, ...] = ()
    next_after_segment_id: UUID | None = None


class ParsedContentAuthorizer(Protocol):
    """Adapter to the existing current server-side ``read_content`` decision."""

    async def decide_read_content(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> ContentAuthorizationDecision: ...


class TrustedContentAuthorizationContextFactory(Protocol):
    async def create(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
    ) -> TrustedAuthorizationContext: ...


@dataclass(frozen=True, slots=True)
class DecisionServiceParsedContentAuthorizer:
    """Bridge the approved current ``read_content`` decision into this query."""

    decision_service: ContentAuthorizationDecisionService
    context_factory: TrustedContentAuthorizationContextFactory

    async def decide_read_content(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> ContentAuthorizationDecision:
        del source_revision_id
        trusted = await self.context_factory.create(
            context=context,
            requester=requester,
        )
        return await self.decision_service.decide_human_disclosure(
            trusted,
            [source_object_id],
        )


class ParsedContentInspectionService(Protocol):
    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        query: ParsedContentInspectionQuery,
    ) -> ParsedContentInspectionResult: ...


@dataclass(frozen=True, slots=True)
class PersistedParsedContentInspectionService:
    """Resolve one exact current receipt/artifact through canonical ports."""

    uow_factory: UnitOfWorkFactory
    parsed_artifacts: ParsedArtifactRepository
    authorizer: ParsedContentAuthorizer

    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        query: ParsedContentInspectionQuery,
    ) -> ParsedContentInspectionResult:
        try:
            _validate_request(context, requester, query)
        except Exception:
            raise InvalidParsedContentRequest("parsed content request is invalid") from None

        try:
            decision = await self._authorize(
                context=context,
                requester=requester,
                query=query,
            )
        except Exception:
            return _status(ParsedContentInspectionStatus.SERVICE_UNAVAILABLE)
        if not _decision_matches(decision, context, query):
            return _safe_unavailable()

        async with self.uow_factory(context) as uow:
            try:
                source = await uow.sources.resolve_source(query.source_object_id)
                head = await uow.canonical.resolve_current(query.source_object_id)
                if not _current_revision_matches(source, head, context, query):
                    await uow.commit()
                    return _safe_unavailable()
                revision = await uow.sources.resolve_revision(query.source_revision_id)
                if (
                    revision is None
                    or revision.kind is not RevisionKind.CONTENT
                    or revision.source_object_id != source.source_object_id
                    or revision.workspace_id != context.scope.workspace_id
                    or revision.environment_id != context.scope.environment_id
                ):
                    await uow.commit()
                    return _safe_unavailable()

                receipt = await uow.ingestion.resolve_processing_receipt_by_id(
                    query.processing_receipt_id
                )
                if receipt is None or not _receipt_matches(receipt, source, revision):
                    await uow.commit()
                    return _status(ParsedContentInspectionStatus.PROCESSING_INCOMPLETE)
                if receipt.coverage is not ProcessingCoverage.COMPLETE:
                    await uow.commit()
                    return _status(ParsedContentInspectionStatus.PROCESSING_INCOMPLETE)
                if receipt.output_reference is None or receipt.output_digest is None:
                    await uow.commit()
                    return _status(ParsedContentInspectionStatus.ARTIFACT_UNAVAILABLE)

                page = await self.parsed_artifacts.list_segments(
                    workspace_id=context.scope.workspace_id,
                    environment_id=context.scope.environment_id,
                    source_object_id=query.source_object_id,
                    source_revision_id=query.source_revision_id,
                    source_revision_digest=revision.revision_digest,
                    original_sha256=revision.original_sha256,
                    artifact_id=receipt.output_reference,
                    artifact_digest=receipt.output_digest,
                    parser_name=receipt.parser_name,
                    parser_version=receipt.parser_version,
                    parser_config_reference=receipt.parser_config_reference,
                    locator_contract_version=receipt.locator_contract_version,
                    selector=query.selector,
                    after_segment_id=query.after_segment_id,
                    limit=query.page_size,
                )
                if page is None:
                    await uow.commit()
                    return _status(ParsedContentInspectionStatus.ARTIFACT_UNAVAILABLE)
                if not _metadata_matches(page.metadata, receipt, source, revision, context):
                    await uow.commit()
                    return _status(ParsedContentInspectionStatus.INTEGRITY_MISMATCH)
                if (
                    query.after_artifact_id is not None
                    and (
                        query.after_artifact_id != page.metadata.artifact_id
                        or query.after_artifact_digest != page.metadata.artifact_digest
                        or query.after_original_sha256 != page.metadata.original_sha256
                    )
                ):
                    await uow.commit()
                    return _status(ParsedContentInspectionStatus.INTEGRITY_MISMATCH)
                result = _bounded_result(page, query)
                await uow.commit()
            except InvalidParsedContentRequest:
                await uow.rollback()
                raise
            except Exception:
                await uow.rollback()
                return _status(ParsedContentInspectionStatus.SERVICE_UNAVAILABLE)

        try:
            final_decision = await self._authorize(
                context=context,
                requester=requester,
                query=query,
            )
        except Exception:
            return _status(ParsedContentInspectionStatus.SERVICE_UNAVAILABLE)
        if not _decision_matches(final_decision, context, query):
            return _safe_unavailable()

        async with self.uow_factory(context) as uow:
            try:
                source = await uow.sources.resolve_source(query.source_object_id)
                final_head = await uow.canonical.resolve_current(query.source_object_id)
                if not _current_revision_matches(source, final_head, context, query):
                    await uow.commit()
                    return _safe_unavailable()
                await uow.commit()
                return result
            except Exception:
                await uow.rollback()
                return _status(ParsedContentInspectionStatus.SERVICE_UNAVAILABLE)

    async def _authorize(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        query: ParsedContentInspectionQuery,
    ) -> ContentAuthorizationDecision:
        return await self.authorizer.decide_read_content(
            context=context,
            requester=requester,
            source_object_id=query.source_object_id,
            source_revision_id=query.source_revision_id,
        )


def _validate_request(
    context: TrustedPersistenceContext,
    requester: ParsedContentInspectionRequester,
    query: ParsedContentInspectionQuery,
) -> None:
    context.validate_shape()
    if not isinstance(context.scope, EnvironmentScope):
        raise InvalidParsedContentRequest("parsed content requires environment scope")
    if context.acting_subject_id != requester.subject_id:
        raise InvalidParsedContentRequest("parsed content requester does not match context")
    if context.purpose.value != "knowledge_control" or context.operation.value != "inspect_parsed_content":
        raise InvalidParsedContentRequest("parsed content operation is invalid")
    if not isinstance(query, ParsedContentInspectionQuery):
        raise InvalidParsedContentRequest("parsed content query is invalid")


def _current_revision_matches(source, head, context, query) -> bool:
    return bool(
        source is not None
        and head is not None
        and isinstance(context.scope, EnvironmentScope)
        and source.workspace_id == context.scope.workspace_id
        and source.environment_id == context.scope.environment_id
        and head.current_revision_id == query.source_revision_id
        and head.current_revision_kind is RevisionKind.CONTENT
    )


def _decision_matches(decision, context, query) -> bool:
    return bool(
        isinstance(decision, ContentAuthorizationDecision)
        and decision.allowed
        and decision.decision_kind.value == "human_disclosure"
        and decision.reason_code.value == "allowed"
        and decision.workspace_id == context.scope.workspace_id
        and decision.environment_id == context.scope.environment_id
        and decision.source_object_ids == (query.source_object_id,)
        and len(decision.policy_evidence) == 1
        and decision.policy_evidence[0].source_revision_id == query.source_revision_id
    )


def _receipt_matches(receipt, source, revision) -> bool:
    return bool(
        receipt.workspace_id == source.workspace_id
        and receipt.environment_id == source.environment_id
        and receipt.source_object_id == source.source_object_id
        and receipt.source_revision_id == revision.revision_id
        and receipt.original_sha256 == revision.original_sha256
    )


def _metadata_matches(metadata, receipt, source, revision, context) -> bool:
    return bool(
        metadata.workspace_id == context.scope.workspace_id
        and metadata.environment_id == context.scope.environment_id
        and metadata.source_object_id == source.source_object_id
        and metadata.source_revision_id == revision.revision_id
        and metadata.source_revision_digest == revision.revision_digest
        and metadata.original_sha256 == revision.original_sha256
        and metadata.artifact_id == receipt.output_reference
        and metadata.artifact_digest == receipt.output_digest
        and metadata.parser_name == receipt.parser_name
        and metadata.parser_version == receipt.parser_version
        and metadata.parser_config_reference == receipt.parser_config_reference
        and metadata.locator_contract_version == receipt.locator_contract_version
        and metadata.coverage is ParseCoverage.COMPLETE
    )


def _bounded_result(page, query) -> ParsedContentInspectionResult:
    selected: list[ParsedSegment] = []
    total_bytes = 0
    overflow = False
    for segment in page.segments:
        segment_bytes = len(segment.text.encode("utf-8"))
        if segment_bytes > query.max_segment_text_bytes:
            return _status(ParsedContentInspectionStatus.LIMIT_EXCEEDED)
        if total_bytes + segment_bytes > query.max_total_text_bytes:
            overflow = True
            break
        selected.append(segment)
        total_bytes += segment_bytes
        if len(selected) == query.page_size:
            break
    if query.selector is not None and not selected and not page.has_more:
        return _status(ParsedContentInspectionStatus.LOCATOR_INVALID)
    has_more = overflow or page.has_more
    return ParsedContentInspectionResult(
        status=ParsedContentInspectionStatus.READY,
        metadata=page.metadata,
        segments=tuple(selected),
        next_after_segment_id=(selected[-1].segment_id if has_more and selected else None),
    )


def _safe_unavailable() -> ParsedContentInspectionResult:
    return _status(ParsedContentInspectionStatus.CONTENT_UNAVAILABLE)


def _status(status: ParsedContentInspectionStatus) -> ParsedContentInspectionResult:
    return ParsedContentInspectionResult(status=status)


__all__ = [
    "DEFAULT_PARSED_CONTENT_PAGE_SIZE",
    "InvalidParsedContentRequest",
    "MAX_PARSED_CONTENT_PAGE_SIZE",
    "MAX_SEGMENT_TEXT_BYTES",
    "MAX_TOTAL_TEXT_BYTES",
    "ParsedContentAuthorizer",
    "DecisionServiceParsedContentAuthorizer",
    "ParsedContentInspectionQuery",
    "ParsedContentInspectionRequester",
    "ParsedContentInspectionResult",
    "ParsedContentInspectionService",
    "ParsedContentInspectionStatus",
    "PersistedParsedContentInspectionService",
    "TrustedContentAuthorizationContextFactory",
]
