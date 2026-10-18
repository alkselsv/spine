"""Canonical readiness proof for an explicitly selected source corpus.

The module is read-only.  It joins ingestion, acceptance, current-head and
active-publication evidence without treating a completed upload or a physical
index write as publication authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScopeOutcome,
    AuthorizedRetrievalScopeRequest,
    AuthorizedRetrievalScopeResolverPort,
)
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory
from spine.domain.ingestion import IngestionItemState, ProcessingCoverage
from spine.domain.projections import (
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionRunState,
    ProjectionSnapshotMemberDisposition,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)
from spine.domain.sources import RevisionKind


CORPUS_READINESS_SCHEMA = "r1-corpus-readiness:v1"
_OPAQUE_ID = r"^[^\x00-\x1f\x7f]{1,256}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CorpusReadinessStatus(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"
    UNAVAILABLE = "unavailable"


class CorpusRetrievalEligibility(str, Enum):
    ELIGIBLE = "eligible"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class CorpusReadinessReason(str, Enum):
    READY = "ready"
    READINESS_UNAVAILABLE = "readiness_unavailable"
    REVISION_MISSING = "revision_missing"
    REVISION_UNAVAILABLE = "revision_unavailable"
    ACCEPTANCE_MISSING = "acceptance_missing"
    ACCEPTANCE_UNAVAILABLE = "acceptance_unavailable"
    REVISION_STALE = "revision_stale"
    REVISION_TOMBSTONED = "revision_tombstoned"
    CURRENTNESS_UNAVAILABLE = "currentness_unavailable"
    INGESTION_MISSING = "ingestion_missing"
    INGESTION_REJECTED = "ingestion_rejected"
    INGESTION_PENDING = "ingestion_pending"
    INGESTION_UNAVAILABLE = "ingestion_unavailable"
    PROJECTION_PENDING = "projection_pending"
    PROJECTION_FAILED = "projection_failed"
    PROJECTION_PROCESSING_UNAVAILABLE = "projection_processing_unavailable"
    PROJECTION_LINEAGE_MISMATCH = "projection_lineage_mismatch"
    PROJECTION_UNAVAILABLE = "projection_unavailable"
    PROJECTION_UNPUBLISHED = "projection_unpublished"
    SCOPE_MISMATCH = "scope_mismatch"
    SNAPSHOT_MEMBER_MISSING = "snapshot_member_missing"
    SNAPSHOT_MEMBER_AMBIGUOUS = "snapshot_member_ambiguous"
    SNAPSHOT_MEMBER_EXCLUDED = "snapshot_member_excluded"
    SNAPSHOT_MEMBER_TOMBSTONED = "snapshot_member_tombstoned"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    PROJECTION_UNHEALTHY = "projection_unhealthy"
    PROJECTION_STALE = "projection_stale"
    RETRIEVAL_DENIED = "retrieval_denied"
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"


class CorpusRevisionExpectation(_Model):
    """One approved opaque revision bound to exact canonical identities."""

    dataset_source_id: str = Field(min_length=1, max_length=256, pattern=_OPAQUE_ID)
    dataset_revision_id: str = Field(min_length=1, max_length=256, pattern=_OPAQUE_ID)
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    canonical_acceptance_id: UUID

    @model_validator(mode="after")
    def validate_ids(self) -> "CorpusRevisionExpectation":
        if any(
            value.int == 0
            for value in (
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                self.source_revision_id,
                self.canonical_acceptance_id,
            )
        ):
            raise ValueError("corpus readiness identities must be non-nil")
        return self


class CorpusReadinessRequest(_Model):
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    revisions: tuple[CorpusRevisionExpectation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope_and_cardinality(self) -> "CorpusReadinessRequest":
        keys = tuple((item.dataset_source_id, item.dataset_revision_id) for item in self.revisions)
        canonical = tuple((item.source_object_id, item.source_revision_id) for item in self.revisions)
        if len(set(keys)) != len(keys) or len(set(canonical)) != len(canonical):
            raise ValueError("corpus readiness revisions must be unique")
        if any(
            item.workspace_id != self.workspace_id
            or item.environment_id != self.environment_id
            for item in self.revisions
        ):
            raise ValueError("corpus readiness revisions must share request scope")
        return self


class CorpusRevisionReadiness(_Model):
    dataset_source_id: str
    dataset_revision_id: str
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    canonical_acceptance_id: UUID
    status: CorpusReadinessStatus
    reason: CorpusReadinessReason
    ingestion_status: Literal["succeeded", "missing", "rejected", "pending", "unavailable"]
    acceptance_status: Literal["accepted", "missing", "unavailable"]
    currentness_status: Literal["current", "stale", "tombstoned", "missing", "unavailable"]
    projection_processing_status: Literal["processed", "pending", "failed", "unavailable"]
    activation_status: Literal["active", "unpublished", "unavailable"]
    membership_status: Literal["included", "missing", "excluded", "tombstoned", "ambiguous"]
    publication_health: Literal["healthy", "degraded", "unavailable"]
    publication_freshness: Literal["current", "stale", "unavailable"]
    retrieval_eligibility: CorpusRetrievalEligibility
    ingestion_run_source_id: UUID | None = None
    projection_run_id: UUID | None = None
    projection_snapshot_id: UUID | None = None


class CorpusReadinessReport(_Model):
    schema_version: Literal["r1-corpus-readiness:v1"] = CORPUS_READINESS_SCHEMA
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str
    status: CorpusReadinessStatus
    structural_validation_status: Literal["passed"] = "passed"
    preparation_status: Literal["completed", "incomplete", "unavailable"]
    production_observation_status: Literal["not_run"] = "not_run"
    evaluation_status: Literal["not_run"] = "not_run"
    release_qualification_status: Literal["not_run"] = "not_run"
    revisions: tuple[CorpusRevisionReadiness, ...]


class CorpusRetrievalEligibilityPort(Protocol):
    """Adapt the existing authorized retrieval path to an all-corpus preflight."""

    async def evaluate(
        self,
        *,
        context: TrustedPersistenceContext,
        publication: PublishedActivePublication,
        revisions: tuple[CorpusRevisionExpectation, ...],
    ) -> CorpusRetrievalEligibility: ...


class AuthorizedScopeCorpusEligibility:
    """Require the existing authorization resolver to return the exact corpus."""

    def __init__(
        self,
        *,
        resolver: AuthorizedRetrievalScopeResolverPort,
        request_factory: Callable[
            [TrustedPersistenceContext, PublishedActivePublication],
            AuthorizedRetrievalScopeRequest,
        ],
    ) -> None:
        self._resolver = resolver
        self._request_factory = request_factory

    async def evaluate(
        self,
        *,
        context: TrustedPersistenceContext,
        publication: PublishedActivePublication,
        revisions: tuple[CorpusRevisionExpectation, ...],
    ) -> CorpusRetrievalEligibility:
        try:
            request = self._request_factory(context, publication)
            resolution = await self._resolver.resolve(request)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return CorpusRetrievalEligibility.UNAVAILABLE
        if resolution.outcome is AuthorizedRetrievalScopeOutcome.DENIED:
            return CorpusRetrievalEligibility.DENIED
        if resolution.outcome is not AuthorizedRetrievalScopeOutcome.RESOLVED:
            return CorpusRetrievalEligibility.UNAVAILABLE
        scope = resolution.scope
        if scope is None:
            return CorpusRetrievalEligibility.UNAVAILABLE
        expected = {
            (item.source_object_id, item.source_revision_id) for item in revisions
        }
        actual = {
            (item.source_object_id, item.source_revision_id)
            for item in scope.source_constraints
        }
        return (
            CorpusRetrievalEligibility.ELIGIBLE
            if actual == expected
            else CorpusRetrievalEligibility.DENIED
        )


class CanonicalCorpusReadinessService:
    """Prove exact readiness from canonical state, never from a physical index."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        retrieval_eligibility: CorpusRetrievalEligibilityPort,
    ) -> None:
        self._uow_factory = uow_factory
        self._retrieval_eligibility = retrieval_eligibility

    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        request: CorpusReadinessRequest,
    ) -> CorpusReadinessReport:
        scope = context.scope
        if (
            not isinstance(scope, EnvironmentScope)
            or scope.workspace_id != request.workspace_id
            or scope.environment_id != request.environment_id
        ):
            return self._scope_mismatch(request)

        try:
            async with self._uow_factory(context) as uow:
                try:
                    publication = await uow.projections.resolve_active_publication(
                        request.projection_kind
                    )
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    return self._publication_unavailable(request)

                revisions = []
                for expected in request.revisions:
                    revisions.append(
                        await self._inspect_revision(
                            uow=uow,
                            expected=expected,
                            projection_kind=request.projection_kind,
                            publication=publication,
                        )
                    )
                await uow.commit()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._readiness_unavailable(request)

        ready_indexes = [
            index
            for index, item in enumerate(revisions)
            if item.reason is CorpusReadinessReason.READY
        ]
        if ready_indexes:
            try:
                eligibility = await self._retrieval_eligibility.evaluate(
                    context=context,
                    publication=publication,
                    revisions=tuple(request.revisions[index] for index in ready_indexes),
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                eligibility = CorpusRetrievalEligibility.UNAVAILABLE
            if not isinstance(eligibility, CorpusRetrievalEligibility):
                eligibility = CorpusRetrievalEligibility.UNAVAILABLE
            if eligibility is not CorpusRetrievalEligibility.ELIGIBLE:
                reason = (
                    CorpusReadinessReason.RETRIEVAL_DENIED
                    if eligibility is CorpusRetrievalEligibility.DENIED
                    else CorpusReadinessReason.RETRIEVAL_UNAVAILABLE
                )
                for index in ready_indexes:
                    revisions[index] = revisions[index].model_copy(
                        update={
                            "status": (
                                CorpusReadinessStatus.NOT_READY
                                if eligibility is CorpusRetrievalEligibility.DENIED
                                else CorpusReadinessStatus.UNAVAILABLE
                            ),
                            "reason": reason,
                            "retrieval_eligibility": eligibility,
                        }
                    )
            else:
                for index in ready_indexes:
                    revisions[index] = revisions[index].model_copy(
                        update={"retrieval_eligibility": eligibility}
                    )

        return self._report(request, tuple(revisions))

    async def _inspect_revision(
        self,
        *,
        uow: UnitOfWork,
        expected: CorpusRevisionExpectation,
        projection_kind: str,
        publication: PublishedActivePublication | UnavailableActivePublication | UnpublishedActivePublication,
    ) -> CorpusRevisionReadiness:
        base = self._base(expected)
        try:
            revision = await uow.sources.resolve_revision(expected.source_revision_id)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._rejected(
                base,
                CorpusReadinessReason.REVISION_UNAVAILABLE,
                unavailable=True,
            )
        if revision is None:
            return self._rejected(base, CorpusReadinessReason.REVISION_MISSING, currentness_status="missing")
        if not self._scoped_revision(revision, expected):
            return self._rejected(base, CorpusReadinessReason.SCOPE_MISMATCH)
        if revision.kind is RevisionKind.TOMBSTONE:
            return self._rejected(base, CorpusReadinessReason.REVISION_TOMBSTONED, currentness_status="tombstoned")

        try:
            acceptance = await uow.canonical.resolve_acceptance(
                expected.canonical_acceptance_id
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._rejected(
                base,
                CorpusReadinessReason.ACCEPTANCE_UNAVAILABLE,
                unavailable=True,
                acceptance_status="unavailable",
            )
        if acceptance is None:
            return self._rejected(base, CorpusReadinessReason.ACCEPTANCE_MISSING, currentness_status="missing")
        if not self._scoped_acceptance(acceptance, expected):
            return self._rejected(base, CorpusReadinessReason.SCOPE_MISMATCH)

        base.update(acceptance_status="accepted")
        try:
            current = await uow.canonical.resolve_current(expected.source_object_id)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._rejected(
                base,
                CorpusReadinessReason.CURRENTNESS_UNAVAILABLE,
                unavailable=True,
                currentness_status="unavailable",
            )
        if current is None:
            return self._rejected(base, CorpusReadinessReason.REVISION_STALE, currentness_status="stale")
        if not self._scope_matches(current, expected):
            return self._rejected(base, CorpusReadinessReason.SCOPE_MISMATCH)
        if current.current_revision_kind is RevisionKind.TOMBSTONE:
            return self._rejected(base, CorpusReadinessReason.REVISION_TOMBSTONED, currentness_status="tombstoned")
        if current.current_revision_id != expected.source_revision_id:
            return self._rejected(base, CorpusReadinessReason.REVISION_STALE, currentness_status="stale")
        base.update(currentness_status="current")

        try:
            bindings = await uow.ingestion.list_bindings_for_source_revision(
                expected.source_object_id, expected.source_revision_id
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._rejected(
                base,
                CorpusReadinessReason.INGESTION_UNAVAILABLE,
                unavailable=True,
                ingestion_status="unavailable",
            )
        if not bindings:
            return self._rejected(base, CorpusReadinessReason.INGESTION_MISSING)
        selected = None
        rejected = False
        for binding in bindings:
            if not self._scope_matches(binding, expected):
                continue
            try:
                head = await uow.ingestion.resolve_item_head(
                    binding.ingestion_run_source_id
                )
                receipt = await uow.ingestion.resolve_processing_receipt(
                    binding.ingestion_run_source_id
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return self._rejected(
                    base,
                    CorpusReadinessReason.INGESTION_UNAVAILABLE,
                    unavailable=True,
                    ingestion_status="unavailable",
                )
            if head is not None and head.state in {
                IngestionItemState.REJECTED,
                IngestionItemState.QUARANTINED,
                IngestionItemState.PROCESSING_FAILED,
            }:
                rejected = True
                continue
            if (
                head is not None
                and head.state is IngestionItemState.PROJECTION_REQUESTED
                and receipt is not None
                and receipt.coverage in {ProcessingCoverage.COMPLETE, ProcessingCoverage.PARTIAL}
                and self._scope_matches(receipt, expected)
                and receipt.source_revision_id == expected.source_revision_id
            ):
                selected = binding
                break
        if selected is None:
            return self._rejected(
                base,
                CorpusReadinessReason.INGESTION_REJECTED if rejected else CorpusReadinessReason.INGESTION_PENDING,
                ingestion_status="rejected" if rejected else "pending",
            )
        base.update(
            ingestion_status="succeeded",
            ingestion_run_source_id=selected.ingestion_run_source_id,
        )

        try:
            runs = await uow.projections.list_runs_for_source_revision(
                expected.source_object_id, expected.source_revision_id
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._rejected(
                base,
                CorpusReadinessReason.PROJECTION_PROCESSING_UNAVAILABLE,
                unavailable=True,
                projection_processing_status="unavailable",
            )
        selected_run = None
        failed = False
        completed_with_other_lineage = False
        active_run_id = (
            publication.projection_snapshot.projection_run_id
            if isinstance(publication, PublishedActivePublication)
            else None
        )
        for run in runs:
            if not self._scope_matches(run, expected) or run.projection_kind != projection_kind:
                continue
            try:
                transitions = await uow.projections.list_transitions(
                    run.projection_run_id
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return self._rejected(
                    base,
                    CorpusReadinessReason.PROJECTION_PROCESSING_UNAVAILABLE,
                    unavailable=True,
                    projection_processing_status="unavailable",
                )
            state = transitions[-1].state if transitions else None
            if state is ProjectionRunState.COMPLETE:
                if active_run_id is None or run.projection_run_id == active_run_id:
                    selected_run = run
                    break
                completed_with_other_lineage = True
            if state in {ProjectionRunState.FAILED, ProjectionRunState.ABANDONED}:
                failed = True
        if selected_run is None:
            if active_run_id is not None and completed_with_other_lineage:
                return self._rejected(
                    base,
                    CorpusReadinessReason.PROJECTION_LINEAGE_MISMATCH,
                )
            return self._rejected(
                base,
                CorpusReadinessReason.PROJECTION_FAILED if failed else CorpusReadinessReason.PROJECTION_PENDING,
                projection_processing_status="failed" if failed else "pending",
            )
        base.update(
            projection_processing_status="processed",
            projection_run_id=selected_run.projection_run_id,
        )

        if publication is None:
            return self._rejected(base, CorpusReadinessReason.PROJECTION_UNAVAILABLE, unavailable=True)
        if not isinstance(
            publication,
            (
                PublishedActivePublication,
                UnavailableActivePublication,
                UnpublishedActivePublication,
            ),
        ):
            return self._rejected(base, CorpusReadinessReason.PROJECTION_UNAVAILABLE, unavailable=True)
        if (
            publication.workspace_id != expected.workspace_id
            or publication.environment_id != expected.environment_id
            or publication.projection_kind != projection_kind
        ):
            return self._rejected(base, CorpusReadinessReason.SCOPE_MISMATCH)
        if isinstance(publication, UnavailableActivePublication):
            return self._rejected(base, CorpusReadinessReason.PROJECTION_UNAVAILABLE, unavailable=True)
        if isinstance(publication, UnpublishedActivePublication):
            return self._rejected(
                base,
                CorpusReadinessReason.PROJECTION_UNPUBLISHED,
                activation_status="unpublished",
            )
        snapshot_id = publication.projection_snapshot.projection_snapshot_id
        base.update(activation_status="active", projection_snapshot_id=snapshot_id)

        try:
            members = await uow.projections.list_snapshot_members(snapshot_id)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._rejected(
                base,
                CorpusReadinessReason.SNAPSHOT_UNAVAILABLE,
                unavailable=True,
            )
        exact = [
            member
            for member in members
            if member.source_object_id == expected.source_object_id
            and member.source_revision_id == expected.source_revision_id
        ]
        if not exact:
            return self._rejected(base, CorpusReadinessReason.SNAPSHOT_MEMBER_MISSING)
        if len(exact) != 1:
            return self._rejected(
                base, CorpusReadinessReason.SNAPSHOT_MEMBER_AMBIGUOUS, membership_status="ambiguous"
            )
        member = exact[0]
        if (
            member.projection_snapshot_id != snapshot_id
            or not self._scope_matches(member, expected)
        ):
            return self._rejected(base, CorpusReadinessReason.SCOPE_MISMATCH)
        if member.disposition is ProjectionSnapshotMemberDisposition.EXCLUDED:
            return self._rejected(
                base, CorpusReadinessReason.SNAPSHOT_MEMBER_EXCLUDED, membership_status="excluded"
            )
        if member.disposition is ProjectionSnapshotMemberDisposition.TOMBSTONED:
            return self._rejected(
                base, CorpusReadinessReason.SNAPSHOT_MEMBER_TOMBSTONED, membership_status="tombstoned"
            )
        base.update(membership_status="included")
        if publication.health is not ProjectionHealth.HEALTHY:
            return self._rejected(
                base,
                CorpusReadinessReason.PROJECTION_UNHEALTHY,
                publication_health="degraded",
            )
        base.update(publication_health="healthy")
        if publication.freshness is not ProjectionFreshness.CURRENT:
            return self._rejected(
                base,
                CorpusReadinessReason.PROJECTION_STALE,
                publication_freshness="stale",
            )
        base.update(publication_freshness="current")
        return CorpusRevisionReadiness(
            **base,
            status=CorpusReadinessStatus.READY,
            reason=CorpusReadinessReason.READY,
        )

    @staticmethod
    def _base(expected: CorpusRevisionExpectation) -> dict[str, object]:
        return {
            **expected.model_dump(),
            "ingestion_status": "missing",
            "acceptance_status": "missing",
            "currentness_status": "missing",
            "projection_processing_status": "unavailable",
            "activation_status": "unavailable",
            "membership_status": "missing",
            "publication_health": "unavailable",
            "publication_freshness": "unavailable",
            "retrieval_eligibility": CorpusRetrievalEligibility.UNAVAILABLE,
        }

    @staticmethod
    def _rejected(
        base: dict[str, object],
        reason: CorpusReadinessReason,
        *,
        unavailable: bool = False,
        **updates: object,
    ) -> CorpusRevisionReadiness:
        return CorpusRevisionReadiness(
            **{**base, **updates},
            status=CorpusReadinessStatus.UNAVAILABLE if unavailable else CorpusReadinessStatus.NOT_READY,
            reason=reason,
        )

    @staticmethod
    def _scope_matches(value: object, expected: CorpusRevisionExpectation) -> bool:
        return (
            getattr(value, "workspace_id", None) == expected.workspace_id
            and getattr(value, "environment_id", None) == expected.environment_id
        )

    @classmethod
    def _scoped_revision(cls, value: object, expected: CorpusRevisionExpectation) -> bool:
        return (
            cls._scope_matches(value, expected)
            and value.revision_id == expected.source_revision_id
            and value.source_object_id == expected.source_object_id
        )

    @classmethod
    def _scoped_acceptance(cls, value: object, expected: CorpusRevisionExpectation) -> bool:
        return (
            cls._scope_matches(value, expected)
            and value.canonical_acceptance_id == expected.canonical_acceptance_id
            and value.source_object_id == expected.source_object_id
            and value.source_revision_id == expected.source_revision_id
        )

    @classmethod
    def _scope_mismatch(cls, request: CorpusReadinessRequest) -> CorpusReadinessReport:
        revisions = tuple(
            cls._rejected(cls._base(item), CorpusReadinessReason.SCOPE_MISMATCH)
            for item in request.revisions
        )
        return cls._report(request, revisions)

    @classmethod
    def _publication_unavailable(cls, request: CorpusReadinessRequest) -> CorpusReadinessReport:
        revisions = tuple(
            cls._rejected(
                cls._base(item), CorpusReadinessReason.PROJECTION_UNAVAILABLE, unavailable=True
            )
            for item in request.revisions
        )
        return cls._report(request, revisions)

    @classmethod
    def _readiness_unavailable(cls, request: CorpusReadinessRequest) -> CorpusReadinessReport:
        revisions = tuple(
            cls._rejected(
                cls._base(item), CorpusReadinessReason.READINESS_UNAVAILABLE, unavailable=True
            )
            for item in request.revisions
        )
        return cls._report(request, revisions)

    @staticmethod
    def _report(
        request: CorpusReadinessRequest,
        revisions: tuple[CorpusRevisionReadiness, ...],
    ) -> CorpusReadinessReport:
        statuses = {item.status for item in revisions}
        status = (
            CorpusReadinessStatus.READY
            if statuses == {CorpusReadinessStatus.READY}
            else CorpusReadinessStatus.UNAVAILABLE
            if CorpusReadinessStatus.UNAVAILABLE in statuses
            else CorpusReadinessStatus.NOT_READY
        )
        return CorpusReadinessReport(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            projection_kind=request.projection_kind,
            status=status,
            preparation_status=(
                "completed"
                if all(item.ingestion_status == "succeeded" for item in revisions)
                else "unavailable"
                if status is CorpusReadinessStatus.UNAVAILABLE
                else "incomplete"
            ),
            revisions=revisions,
        )


__all__ = [
    "CORPUS_READINESS_SCHEMA",
    "AuthorizedScopeCorpusEligibility",
    "CanonicalCorpusReadinessService",
    "CorpusReadinessReason",
    "CorpusReadinessReport",
    "CorpusReadinessRequest",
    "CorpusReadinessStatus",
    "CorpusRetrievalEligibility",
    "CorpusRetrievalEligibilityPort",
    "CorpusRevisionExpectation",
    "CorpusRevisionReadiness",
]
