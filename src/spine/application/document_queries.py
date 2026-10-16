"""Application contracts for authorized canonical document queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.ingestion import IngestionItemState, ProcessingCoverage
from spine.domain.sources import RevisionKind, SourceKind
from spine.domain.sources import AdmissionDisposition
from spine.domain.sources import CanonicalAcceptance, SourceObject, SourceRevision
from spine.domain.projections import (
    ProjectionCandidateState,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionRunState,
    ProjectionSnapshotMemberDisposition,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)


SnapshotT = TypeVar("SnapshotT")


class DocumentNotFound(LookupError):
    """The exact scope-visible canonical source is unavailable."""


class RevisionLifecycle(str, Enum):
    OBSERVED = "observed"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    CURRENT = "current"
    TOMBSTONED = "tombstoned"
    PROCESSING = "processing"


class DocumentPublicationStatus(str, Enum):
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"
    UNAVAILABLE = "unavailable"


class StableLocatorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locator_kind: str
    locator_value: str
    locator_schema: str


class RevisionContentSnapshot(BaseModel):
    """Protected fields released only after the current read_content decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_digest: str
    original_sha256: str
    byte_length: int = Field(ge=0)
    media_type: str
    embedded_title: str | None = None
    document_language: str | None = None
    locator: StableLocatorSnapshot | None = None
    original_reference: "OriginalReferenceSnapshot"


class OriginalReferenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_id: UUID
    storage_generation: UUID
    digest_algorithm: str
    digest_hex: str
    byte_length: int = Field(ge=0)


class ProcessingReferenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ingestion_run_id: UUID
    ingestion_item_id: UUID
    state: IngestionItemState
    processing_receipt_id: UUID | None = None
    coverage: ProcessingCoverage | None = None


class ProjectionReferenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_run_id: UUID
    projection_candidate_id: UUID
    projection_kind: str
    state: ProjectionRunState | ProjectionCandidateState
    projection_reference_id: UUID | None = None


class PublicationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_kind: str
    status: DocumentPublicationStatus
    snapshot_id: UUID | None = None
    freshness: ProjectionFreshness | None = None
    health: ProjectionHealth | None = None
    drift_count: int | None = Field(default=None, ge=0)
    manifest_complete: bool | None = None
    source_covered: bool | None = None


class DocumentIdentitySnapshot(BaseModel):
    """Protected source identity, absent from metadata-only responses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_mode: str
    connection_id: UUID | None = None
    external_namespace: str | None = None
    external_generation: str | None = None
    external_object_id: str | None = None
    upload_identity: str | None = None


class RevisionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source_revision_id: UUID
    kind: RevisionKind
    observed_at: datetime
    lifecycle: RevisionLifecycle
    admission_status: AdmissionDisposition | None = None
    predecessor_revision_id: UUID | None = None
    superseded_by_revision_id: UUID | None = None
    deletion_reason: str | None = None
    accepted_generation: int | None = Field(default=None, ge=1)
    accepted_at: datetime | None = None
    processing_references: tuple[ProcessingReferenceSnapshot, ...] = ()
    projection_references: tuple[ProjectionReferenceSnapshot, ...] = ()
    content: RevisionContentSnapshot | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class DocumentSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source_object_id: UUID
    source_kind: str
    created_at: datetime
    current_revision: RevisionSnapshot | None = None
    publication: PublicationSnapshot | None = None
    identity: DocumentIdentitySnapshot | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class DocumentQueryFilters(BaseModel):
    """Closed, operational-only filters for the document collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: SourceKind | None = None
    lifecycle: RevisionLifecycle | None = None
    processing_state: IngestionItemState | None = None
    publication_status: DocumentPublicationStatus | None = None
    publication_health: ProjectionHealth | None = None

    @model_validator(mode="after")
    def validate_combinations(self) -> "DocumentQueryFilters":
        if (
            self.publication_health is not None
            and self.publication_status in {
                DocumentPublicationStatus.UNPUBLISHED,
                DocumentPublicationStatus.UNAVAILABLE,
            }
        ):
            raise ValueError(
                "publication_health requires published publication_status"
            )
        return self

    def digest_payload(self) -> dict[str, str]:
        """Return the canonical non-empty filter object used by cursors."""

        return {
            name: value.value
            for name, value in (
                ("lifecycle", self.lifecycle),
                ("processing_state", self.processing_state),
                ("publication_health", self.publication_health),
                ("publication_status", self.publication_status),
                ("source_kind", self.source_kind),
            )
            if value is not None
        }

    def matches(self, snapshot: DocumentSnapshot) -> bool:
        if self.source_kind is not None and snapshot.source_kind != self.source_kind.value:
            return False
        revision = snapshot.current_revision
        if self.lifecycle is not None and (
            revision is None or revision.lifecycle != self.lifecycle
        ):
            return False
        if self.processing_state is not None and (
            revision is None
            or not any(
                reference.state == self.processing_state
                for reference in revision.processing_references
            )
        ):
            return False
        publication = snapshot.publication
        if self.publication_status is not None and (
            publication is None or publication.status != self.publication_status
        ):
            return False
        if self.publication_health is not None and (
            publication is None or publication.health != self.publication_health
        ):
            return False
        return True


@dataclass(frozen=True, slots=True)
class DocumentPosition:
    created_at: datetime
    source_object_id: UUID


@dataclass(frozen=True, slots=True)
class RevisionPosition:
    observed_at: datetime
    source_revision_id: UUID


@dataclass(frozen=True, slots=True)
class DocumentPage(Generic[SnapshotT]):
    items: tuple[SnapshotT, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class DocumentQueryRequester:
    canonical_human_id: UUID
    authorization_generation: int
    effective_team_ids: frozenset[UUID] = frozenset()


class DocumentQueryService(Protocol):
    async def list_documents(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        limit: int,
        after: DocumentPosition | None,
        filters: DocumentQueryFilters | None = None,
    ) -> DocumentPage[DocumentSnapshot]: ...

    async def get_document(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
    ) -> DocumentSnapshot | None: ...

    async def list_revisions(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
        limit: int,
        after: RevisionPosition | None,
    ) -> DocumentPage[RevisionSnapshot] | None: ...


@dataclass(frozen=True, slots=True)
class CanonicalDocumentQueryService:
    """Assemble safe reads from the canonical repositories in one scoped UoW."""

    uow_factory: UnitOfWorkFactory
    projection_kind: str = "qa"

    async def list_documents(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        limit: int,
        after: DocumentPosition | None,
        filters: DocumentQueryFilters | None = None,
    ) -> DocumentPage[DocumentSnapshot]:
        _validate_query(context, requester, limit)
        resolved_filters = filters or DocumentQueryFilters()
        async with self.uow_factory(context) as uow:
            source_after = (
                (after.created_at, after.source_object_id) if after else None
            )
            snapshots: list[DocumentSnapshot] = []
            has_more = False
            while True:
                sources = await uow.sources.list_sources(
                    limit=limit + 1,
                    after=source_after,
                )
                if not sources:
                    break
                previous_source_after = source_after
                source_after = (sources[-1].created_at, sources[-1].source_object_id)
                for source in sources:
                    if not _source_in_context(source, context):
                        continue
                    snapshot = await self._document_snapshot(
                        uow,
                        source,
                        requester,
                        disclose_protected=False,
                    )
                    if resolved_filters.matches(snapshot):
                        snapshots.append(snapshot)
                        if len(snapshots) > limit:
                            has_more = True
                            break
                if has_more or len(sources) < limit + 1:
                    break
                if source_after == previous_source_after:
                    break
            await uow.commit()
            return DocumentPage(items=tuple(snapshots[:limit]), has_more=has_more)

    async def get_document(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
    ) -> DocumentSnapshot | None:
        _validate_query(context, requester, 1)
        async with self.uow_factory(context) as uow:
            source = await uow.sources.resolve_source(source_object_id)
            if source is None or not _source_in_context(source, context):
                await uow.commit()
                return None
            snapshot = await self._document_snapshot(
                uow,
                source,
                requester,
                disclose_protected=True,
            )
            await uow.commit()
            return snapshot

    async def list_revisions(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
        limit: int,
        after: RevisionPosition | None,
    ) -> DocumentPage[RevisionSnapshot] | None:
        _validate_query(context, requester, limit)
        async with self.uow_factory(context) as uow:
            source = await uow.sources.resolve_source(source_object_id)
            if source is None or not _source_in_context(source, context):
                await uow.commit()
                return None
            revisions = await uow.sources.list_revisions(
                source_object_id,
                limit=limit + 1,
                after=(after.observed_at, after.source_revision_id) if after else None,
            )
            revisions = tuple(
                revision
                for revision in revisions
                if revision.source_object_id == source_object_id
                and revision.workspace_id == source.workspace_id
                and revision.environment_id == source.environment_id
            )
            head = await uow.canonical.resolve_current(source_object_id)
            acceptances = await uow.canonical.list_acceptances(source_object_id)
            snapshots = tuple([
                await self._revision_snapshot(
                    uow,
                    revision,
                    head_revision_id=(head.current_revision_id if head else None),
                    acceptances=acceptances,
                    disclose_content=False,
                )
                for revision in revisions[:limit]
            ])
            await uow.commit()
            return DocumentPage(items=snapshots, has_more=len(revisions) > limit)

    async def _document_snapshot(
        self,
        uow,
        source: SourceObject,
        requester: DocumentQueryRequester,
        *,
        disclose_protected: bool,
    ) -> DocumentSnapshot:
        head = await uow.canonical.resolve_current(source.source_object_id)
        revision = (
            await uow.sources.resolve_revision(head.current_revision_id)
            if head is not None
            else None
        )
        acceptances = await uow.canonical.list_acceptances(source.source_object_id)
        authorized_revision_id = (
            await self._authorized_revision_id(uow, source, head, requester)
            if disclose_protected
            else None
        )
        revision_snapshot = (
            await self._revision_snapshot(
                uow,
                revision,
                head_revision_id=head.current_revision_id,
                acceptances=acceptances,
                disclose_content=revision.revision_id == authorized_revision_id,
            )
            if revision is not None and head is not None
            else None
        )
        return DocumentSnapshot(
            source_object_id=source.source_object_id,
            source_kind=str(source.source_kind),
            created_at=source.created_at,
            current_revision=revision_snapshot,
            identity=(
                _protected_identity(source)
                if authorized_revision_id is not None
                else None
            ),
            publication=await self._publication_snapshot(
                uow,
                source.source_object_id,
                head.current_revision_id if head else None,
            ),
        )

    async def _authorized_revision_id(self, uow, source, head, requester) -> UUID | None:
        if head is None or head.current_revision_kind is not RevisionKind.CONTENT:
            return None
        try:
            provenance = await uow.sources.list_provenance(head.current_revision_id)
            current = await uow.access_policies.resolve_current(
                source.source_object_id,
                workspace_id=source.workspace_id,
                environment_id=source.environment_id,
            )
            if not provenance or current is None:
                return None
            policy = await uow.access_policies.resolve_policy(
                current.access_policy_version_id,
                workspace_id=source.workspace_id,
                environment_id=source.environment_id,
            )
        except Exception:
            return None
        if (
            current.source_object_id != source.source_object_id
            or current.workspace_id != source.workspace_id
            or current.environment_id != source.environment_id
            or policy is None
            or policy.source_object_id != source.source_object_id
            or policy.workspace_id != source.workspace_id
            or policy.environment_id != source.environment_id
            or not policy.allows_human_read(
                requester.canonical_human_id,
                requester.effective_team_ids,
            )
        ):
            return None
        return head.current_revision_id

    async def _revision_snapshot(
        self,
        uow,
        revision: SourceRevision,
        *,
        head_revision_id: UUID | None,
        acceptances: tuple[CanonicalAcceptance, ...],
        disclose_content: bool,
    ) -> RevisionSnapshot:
        acceptance_by_revision = {
            item.source_revision_id: item for item in acceptances
        }
        acceptance = acceptance_by_revision.get(revision.revision_id)
        successor = next(
            (
                item
                for item in acceptances
                if item.predecessor_revision_id == revision.revision_id
            ),
            None,
        )
        admission = await uow.admissions.resolve_effective(revision.revision_id)
        processing = await self._processing_references(uow, revision)
        projections = await self._projection_references(uow, revision)
        return RevisionSnapshot(
            source_revision_id=revision.revision_id,
            kind=revision.kind,
            observed_at=revision.observed_at,
            lifecycle=_revision_lifecycle(
                revision,
                head_revision_id=head_revision_id,
                acceptance=acceptance,
                admission=admission,
            ),
            admission_status=admission.disposition if admission else None,
            predecessor_revision_id=(acceptance.predecessor_revision_id if acceptance else None),
            superseded_by_revision_id=(successor.source_revision_id if successor else None),
            deletion_reason=revision.deletion_reason,
            accepted_generation=(acceptance.accepted_generation if acceptance else None),
            accepted_at=(acceptance.accepted_at if acceptance else None),
            processing_references=processing,
            projection_references=projections,
            content=(
                await _protected_content(uow, revision)
                if disclose_content and revision.kind is RevisionKind.CONTENT
                else None
            ),
        )

    async def _processing_references(self, uow, revision) -> tuple[ProcessingReferenceSnapshot, ...]:
        bindings = await uow.ingestion.list_bindings_for_source_revision(
            revision.source_object_id, revision.revision_id
        )
        results = []
        for binding in bindings[-100:]:
            item = await uow.ingestion.resolve_item(binding.ingestion_run_source_id)
            head = await uow.ingestion.resolve_item_head(binding.ingestion_run_source_id)
            receipt = await uow.ingestion.resolve_processing_receipt(
                binding.ingestion_run_source_id
            )
            if item is None:
                continue
            results.append(
                ProcessingReferenceSnapshot(
                    ingestion_run_id=item.ingestion_run_id,
                    ingestion_item_id=item.ingestion_run_source_id,
                    state=(head.state if head is not None else IngestionItemState.RECEIVED),
                    processing_receipt_id=(receipt.processing_receipt_id if receipt else None),
                    coverage=(receipt.coverage if receipt else None),
                )
            )
        return tuple(results)

    async def _projection_references(self, uow, revision) -> tuple[ProjectionReferenceSnapshot, ...]:
        runs = await uow.projections.list_runs_for_source_revision(
            revision.source_object_id, revision.revision_id
        )
        results = []
        for run in runs[-100:]:
            transitions = await uow.projections.list_transitions(run.projection_run_id)
            references = await uow.projections.list_source_references(run.projection_run_id)
            exact = next(
                (
                    value
                    for value in references
                    if value.source_object_id == revision.source_object_id
                    and value.source_revision_id == revision.revision_id
                ),
                None,
            )
            results.append(
                ProjectionReferenceSnapshot(
                    projection_run_id=run.projection_run_id,
                    projection_candidate_id=run.projection_candidate_id,
                    projection_kind=run.projection_kind,
                    state=(transitions[-1].state if transitions else run.candidate_state),
                    projection_reference_id=(
                        exact.source_projection_reference_id if exact else None
                    ),
                )
            )
        return tuple(results)

    async def _publication_snapshot(self, uow, source_object_id, revision_id) -> PublicationSnapshot:
        try:
            publication = await uow.projections.resolve_active_publication(
                self.projection_kind
            )
        except Exception:
            return PublicationSnapshot(
                projection_kind=self.projection_kind,
                status=DocumentPublicationStatus.UNAVAILABLE,
            )
        if isinstance(publication, UnpublishedActivePublication):
            return PublicationSnapshot(
                projection_kind=self.projection_kind,
                status=DocumentPublicationStatus.UNPUBLISHED,
            )
        if isinstance(publication, UnavailableActivePublication):
            return PublicationSnapshot(
                projection_kind=self.projection_kind,
                status=DocumentPublicationStatus.UNAVAILABLE,
            )
        if not isinstance(publication, PublishedActivePublication):
            return PublicationSnapshot(
                projection_kind=self.projection_kind,
                status=DocumentPublicationStatus.UNAVAILABLE,
            )
        members = await uow.projections.list_snapshot_members(
            publication.projection_snapshot.projection_snapshot_id
        )
        member = next(
            (
                value
                for value in members
                if value.source_object_id == source_object_id
                and value.source_revision_id == revision_id
            ),
            None,
        )
        drifts = await uow.projections.list_drifts(
            publication.projection_activation.projection_activation_id
        )
        source_drifts = tuple(
            value for value in drifts if value.source_object_id == source_object_id
        )
        return PublicationSnapshot(
            projection_kind=self.projection_kind,
            status=(
                DocumentPublicationStatus.PUBLISHED
                if member is not None
                else DocumentPublicationStatus.UNPUBLISHED
            ),
            snapshot_id=publication.projection_snapshot.projection_snapshot_id,
            freshness=(
                ProjectionFreshness.STALE if source_drifts else publication.freshness
            ),
            health=publication.health,
            drift_count=len(source_drifts),
            manifest_complete=(
                len(members) == publication.projection_snapshot.member_count
            ),
            source_covered=(
                member is not None
                and member.disposition
                is not ProjectionSnapshotMemberDisposition.EXCLUDED
            ),
        )


def _validate_query(
    context: TrustedPersistenceContext,
    requester: DocumentQueryRequester,
    limit: int,
) -> None:
    if not isinstance(context.scope, EnvironmentScope):
        raise ValueError("document queries require environment scope")
    if requester.canonical_human_id.int == 0 or requester.authorization_generation < 1:
        raise ValueError("document query requester is invalid")
    if limit < 1 or limit > 100:
        raise ValueError("document query limit is invalid")


def _protected_identity(source: SourceObject) -> DocumentIdentitySnapshot:
    return DocumentIdentitySnapshot(
        identity_mode=source.identity_mode.value,
        connection_id=source.connection_id,
        external_namespace=source.external_namespace,
        external_generation=source.external_generation,
        external_object_id=source.external_object_id,
        upload_identity=source.upload_identity,
    )


async def _protected_content(uow, revision: SourceRevision) -> RevisionContentSnapshot:
    provenance = await uow.sources.list_provenance(revision.revision_id)
    locator = next(
        (
            StableLocatorSnapshot(
                locator_kind=value.origin_locator_kind,
                locator_value=value.origin_locator_value,
                locator_schema=value.origin_locator_schema,
            )
            for value in provenance
            if value.origin_locator_kind is not None
            and value.origin_locator_value is not None
            and value.origin_locator_schema is not None
        ),
        None,
    )
    assert revision.original_sha256 is not None
    assert revision.byte_length is not None
    assert revision.media_type is not None
    assert revision.original_reference is not None
    return RevisionContentSnapshot(
        revision_digest=revision.revision_digest,
        original_sha256=revision.original_sha256,
        byte_length=revision.byte_length,
        media_type=revision.media_type,
        embedded_title=(revision.revision_metadata.embedded_title if revision.revision_metadata else None),
        document_language=(revision.revision_metadata.document_language if revision.revision_metadata else None),
        locator=locator,
        original_reference=OriginalReferenceSnapshot(
            object_id=revision.original_reference.object_id,
            storage_generation=revision.original_reference.storage_generation,
            digest_algorithm=revision.original_reference.digest_algorithm,
            digest_hex=revision.original_reference.digest_hex,
            byte_length=revision.original_reference.byte_length,
        ),
    )


def _revision_lifecycle(revision, *, head_revision_id, acceptance, admission) -> RevisionLifecycle:
    if revision.revision_id == head_revision_id:
        return (
            RevisionLifecycle.TOMBSTONED
            if revision.kind is RevisionKind.TOMBSTONE
            else RevisionLifecycle.CURRENT
        )
    if acceptance is not None:
        return RevisionLifecycle.SUPERSEDED
    if admission is None:
        return RevisionLifecycle.OBSERVED
    if admission.disposition is AdmissionDisposition.QUARANTINED:
        return RevisionLifecycle.QUARANTINED
    if admission.disposition is AdmissionDisposition.REJECTED:
        return RevisionLifecycle.REJECTED
    return RevisionLifecycle.PROCESSING


def _source_in_context(source: SourceObject, context: TrustedPersistenceContext) -> bool:
    scope = context.scope
    return (
        isinstance(scope, EnvironmentScope)
        and source.workspace_id == scope.workspace_id
        and source.environment_id == scope.environment_id
    )


__all__ = [
    "CanonicalDocumentQueryService",
    "DocumentPage",
    "DocumentPublicationStatus",
    "DocumentNotFound",
    "DocumentPosition",
    "DocumentQueryFilters",
    "DocumentQueryRequester",
    "DocumentQueryService",
    "DocumentIdentitySnapshot",
    "DocumentSnapshot",
    "OriginalReferenceSnapshot",
    "ProcessingReferenceSnapshot",
    "ProjectionReferenceSnapshot",
    "PublicationSnapshot",
    "RevisionContentSnapshot",
    "RevisionLifecycle",
    "RevisionSnapshot",
    "RevisionPosition",
    "StableLocatorSnapshot",
]
