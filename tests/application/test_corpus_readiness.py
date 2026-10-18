from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from spine.application.corpus_readiness import (
    AuthorizedScopeCorpusEligibility,
    CanonicalCorpusReadinessService,
    CorpusReadinessReason,
    CorpusReadinessRequest,
    CorpusReadinessStatus,
    CorpusRetrievalEligibility,
    CorpusRevisionExpectation,
)
from spine.application.authorized_retrieval_scope import AuthorizedRetrievalScopeOutcome
from spine.application.persistence.context import EnvironmentScope
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


WORKSPACE_ID = UUID(int=101)
OTHER_WORKSPACE_ID = UUID(int=102)
ENVIRONMENT_ID = UUID(int=201)
OTHER_ENVIRONMENT_ID = UUID(int=202)
SOURCE_ID = UUID(int=301)
REVISION_ID = UUID(int=401)
ACCEPTANCE_ID = UUID(int=501)
ITEM_ID = UUID(int=601)
RECEIPT_ID = UUID(int=701)
RUN_ID = UUID(int=801)
SNAPSHOT_ID = UUID(int=901)


def _expectation(**updates: object) -> CorpusRevisionExpectation:
    values: dict[str, object] = {
        "dataset_source_id": "source:approved-1",
        "dataset_revision_id": "revision:approved-1",
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "source_object_id": SOURCE_ID,
        "source_revision_id": REVISION_ID,
        "canonical_acceptance_id": ACCEPTANCE_ID,
    }
    values.update(updates)
    return CorpusRevisionExpectation.model_validate(values)


class _Sources:
    def __init__(self) -> None:
        self.revision = SimpleNamespace(
            revision_id=REVISION_ID,
            source_object_id=SOURCE_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            kind=RevisionKind.CONTENT,
        )

    async def resolve_revision(self, _: UUID):
        return self.revision


class _Canonical:
    def __init__(self) -> None:
        self.acceptance = SimpleNamespace(
            canonical_acceptance_id=ACCEPTANCE_ID,
            source_object_id=SOURCE_ID,
            source_revision_id=REVISION_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
        self.current = SimpleNamespace(
            source_object_id=SOURCE_ID,
            current_revision_id=REVISION_ID,
            current_revision_kind=RevisionKind.CONTENT,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )

    async def resolve_acceptance(self, _: UUID):
        return self.acceptance

    async def resolve_current(self, _: UUID):
        return self.current


class _Ingestion:
    def __init__(self) -> None:
        self.binding = SimpleNamespace(
            ingestion_run_source_id=ITEM_ID,
            source_object_id=SOURCE_ID,
            source_revision_id=REVISION_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
        self.head = SimpleNamespace(state=IngestionItemState.PROJECTION_REQUESTED)
        self.receipt = SimpleNamespace(
            processing_receipt_id=RECEIPT_ID,
            ingestion_run_source_id=ITEM_ID,
            source_object_id=SOURCE_ID,
            source_revision_id=REVISION_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            coverage=ProcessingCoverage.COMPLETE,
        )

    async def list_bindings_for_source_revision(self, *_: UUID):
        return (self.binding,)

    async def resolve_item_head(self, _: UUID):
        return self.head

    async def resolve_processing_receipt(self, _: UUID):
        return self.receipt


class _Projections:
    def __init__(self) -> None:
        self.run = SimpleNamespace(
            projection_run_id=RUN_ID,
            source_object_id=SOURCE_ID,
            source_revision_id=REVISION_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="context_graph",
            candidate_state=ProjectionRunState.COMPLETE,
        )
        self.transition = SimpleNamespace(state=ProjectionRunState.COMPLETE)
        self.publication = MagicMock(spec=PublishedActivePublication)
        self.publication.workspace_id = WORKSPACE_ID
        self.publication.environment_id = ENVIRONMENT_ID
        self.publication.projection_kind = "context_graph"
        self.publication.projection_snapshot = SimpleNamespace(
            projection_snapshot_id=SNAPSHOT_ID,
            projection_run_id=RUN_ID,
        )
        self.publication.freshness = ProjectionFreshness.CURRENT
        self.publication.health = ProjectionHealth.HEALTHY
        self.member = SimpleNamespace(
            projection_snapshot_id=SNAPSHOT_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            source_object_id=SOURCE_ID,
            source_revision_id=REVISION_ID,
            disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
            exclusion_code=None,
        )
        self.physical_write_calls = 0

    async def list_runs_for_source_revision(self, *_: UUID):
        return (self.run,)

    async def list_transitions(self, _: UUID):
        return (self.transition,)

    async def resolve_active_publication(self, _: str):
        return self.publication

    async def list_snapshot_members(self, _: UUID):
        return (self.member,)

    async def project(self, *_: object) -> None:
        self.physical_write_calls += 1
        raise AssertionError("readiness must not write to a vector index")


class _Uow:
    def __init__(self) -> None:
        self.sources = _Sources()
        self.canonical = _Canonical()
        self.ingestion = _Ingestion()
        self.projections = _Projections()
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self, _: object) -> _Uow:
        return self.uow


class _Eligibility:
    def __init__(self, result: CorpusRetrievalEligibility = CorpusRetrievalEligibility.ELIGIBLE) -> None:
        self.result = result

    async def evaluate(self, **_: object) -> CorpusRetrievalEligibility:
        return self.result


def _context(*, workspace_id: UUID = WORKSPACE_ID):
    return SimpleNamespace(scope=EnvironmentScope(workspace_id, ENVIRONMENT_ID))


async def _inspect(
    uow: _Uow | None = None,
    *,
    expectation: CorpusRevisionExpectation | None = None,
    eligibility: CorpusRetrievalEligibility = CorpusRetrievalEligibility.ELIGIBLE,
):
    selected = uow or _Uow()
    service = CanonicalCorpusReadinessService(
        uow_factory=_Factory(selected),
        retrieval_eligibility=_Eligibility(eligibility),
    )
    report = await service.inspect(
        context=_context(),
        request=CorpusReadinessRequest(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="context_graph",
            revisions=(_expectation() if expectation is None else expectation,),
        ),
    )
    return selected, report


@pytest.mark.asyncio
async def test_current_accepted_exact_member_is_ready_without_physical_index_write() -> None:
    uow, report = await _inspect()

    assert report.status is CorpusReadinessStatus.READY
    assert report.structural_validation_status == "passed"
    assert report.preparation_status == "completed"
    assert report.production_observation_status == "not_run"
    assert report.evaluation_status == "not_run"
    assert report.release_qualification_status == "not_run"
    assert report.revisions[0].reason is CorpusReadinessReason.READY
    assert report.revisions[0].projection_snapshot_id == SNAPSHOT_ID
    assert uow.projections.physical_write_calls == 0
    assert uow.committed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        (lambda u: setattr(u.sources, "revision", None), CorpusReadinessReason.REVISION_MISSING),
        (lambda u: setattr(u.canonical, "acceptance", None), CorpusReadinessReason.ACCEPTANCE_MISSING),
        (
            lambda u: setattr(u.canonical.current, "current_revision_id", UUID(int=999)),
            CorpusReadinessReason.REVISION_STALE,
        ),
        (
            lambda u: setattr(u.canonical.current, "current_revision_kind", RevisionKind.TOMBSTONE),
            CorpusReadinessReason.REVISION_TOMBSTONED,
        ),
        (
            lambda u: setattr(u.ingestion.head, "state", IngestionItemState.REJECTED),
            CorpusReadinessReason.INGESTION_REJECTED,
        ),
        (
            lambda u: setattr(u.projections.transition, "state", ProjectionRunState.FAILED),
            CorpusReadinessReason.PROJECTION_FAILED,
        ),
        (
            lambda u: setattr(u.projections, "publication", None),
            CorpusReadinessReason.PROJECTION_UNAVAILABLE,
        ),
        (
            lambda u: setattr(u.projections.member, "source_revision_id", UUID(int=998)),
            CorpusReadinessReason.SNAPSHOT_MEMBER_MISSING,
        ),
        (
            lambda u: setattr(
                u.projections.member,
                "disposition",
                ProjectionSnapshotMemberDisposition.EXCLUDED,
            ),
            CorpusReadinessReason.SNAPSHOT_MEMBER_EXCLUDED,
        ),
        (
            lambda u: setattr(
                u.projections.member,
                "disposition",
                ProjectionSnapshotMemberDisposition.TOMBSTONED,
            ),
            CorpusReadinessReason.SNAPSHOT_MEMBER_TOMBSTONED,
        ),
        (
            lambda u: setattr(u.projections.publication, "health", ProjectionHealth.DEGRADED),
            CorpusReadinessReason.PROJECTION_UNHEALTHY,
        ),
        (
            lambda u: setattr(u.projections.publication, "freshness", ProjectionFreshness.STALE),
            CorpusReadinessReason.PROJECTION_STALE,
        ),
    ),
)
async def test_non_ready_canonical_states_have_actionable_reason(mutate, reason) -> None:
    uow = _Uow()
    mutate(uow)

    _, report = await _inspect(uow)

    assert report.status is (
        CorpusReadinessStatus.UNAVAILABLE
        if reason is CorpusReadinessReason.PROJECTION_UNAVAILABLE
        else CorpusReadinessStatus.NOT_READY
    )
    assert report.revisions[0].reason is reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "foreign_id"),
    (("workspace_id", OTHER_WORKSPACE_ID), ("environment_id", OTHER_ENVIRONMENT_ID)),
)
async def test_cross_scope_projection_fails_closed(field: str, foreign_id: UUID) -> None:
    uow = _Uow()
    setattr(uow.projections.publication, field, foreign_id)
    _, report = await _inspect(uow)
    assert report.status is CorpusReadinessStatus.NOT_READY
    assert report.revisions[0].reason is CorpusReadinessReason.SCOPE_MISMATCH


@pytest.mark.asyncio
async def test_unavailable_projection_fails_closed() -> None:
    uow = _Uow()
    uow.projections.resolve_active_publication = lambda _: _raise(RuntimeError("offline"))
    _, report = await _inspect(uow)
    assert report.status is CorpusReadinessStatus.UNAVAILABLE
    assert report.revisions[0].reason is CorpusReadinessReason.PROJECTION_UNAVAILABLE


@pytest.mark.asyncio
async def test_unpublished_projection_reports_known_activation_state() -> None:
    uow = _Uow()
    uow.projections.publication = UnpublishedActivePublication(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind="context_graph",
    )

    _, report = await _inspect(uow)

    revision = report.revisions[0]
    assert report.status is CorpusReadinessStatus.NOT_READY
    assert revision.reason is CorpusReadinessReason.PROJECTION_UNPUBLISHED
    assert revision.activation_status == "unpublished"


@pytest.mark.asyncio
async def test_failed_rebuild_keeps_last_active_complete_projection_ready() -> None:
    uow = _Uow()
    failed_run_id = UUID(int=802)
    failed_rebuild = SimpleNamespace(
        projection_run_id=failed_run_id,
        source_object_id=SOURCE_ID,
        source_revision_id=REVISION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind="context_graph",
    )
    active_run = uow.projections.run

    async def list_runs(*_: UUID):
        return (failed_rebuild, active_run)

    async def list_transitions(run_id: UUID):
        state = (
            ProjectionRunState.FAILED
            if run_id == failed_run_id
            else ProjectionRunState.COMPLETE
        )
        return (SimpleNamespace(state=state),)

    uow.projections.list_runs_for_source_revision = list_runs
    uow.projections.list_transitions = list_transitions

    _, report = await _inspect(uow)

    revision = report.revisions[0]
    assert report.status is CorpusReadinessStatus.READY
    assert revision.reason is CorpusReadinessReason.READY
    assert revision.projection_run_id == RUN_ID
    assert revision.projection_snapshot_id == SNAPSHOT_ID


@pytest.mark.asyncio
async def test_active_snapshot_must_descend_from_reported_completed_run() -> None:
    uow = _Uow()
    uow.projections.publication.projection_snapshot.projection_run_id = UUID(int=999)

    _, report = await _inspect(uow)

    revision = report.revisions[0]
    assert report.status is CorpusReadinessStatus.NOT_READY
    assert revision.reason is CorpusReadinessReason.PROJECTION_LINEAGE_MISMATCH


@pytest.mark.asyncio
async def test_successful_rebuild_selects_newer_active_completed_run() -> None:
    uow = _Uow()
    old_run_id = UUID(int=800)
    old_complete = SimpleNamespace(
        projection_run_id=old_run_id,
        source_object_id=SOURCE_ID,
        source_revision_id=REVISION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind="context_graph",
    )
    active_complete = uow.projections.run

    async def list_runs(*_: UUID):
        return (old_complete, active_complete)

    async def list_transitions(_: UUID):
        return (SimpleNamespace(state=ProjectionRunState.COMPLETE),)

    uow.projections.list_runs_for_source_revision = list_runs
    uow.projections.list_transitions = list_transitions

    _, report = await _inspect(uow)

    revision = report.revisions[0]
    assert report.status is CorpusReadinessStatus.READY
    assert revision.projection_run_id == RUN_ID
    assert revision.projection_snapshot_id == SNAPSHOT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("publication_kind", ("unpublished", "unavailable"))
async def test_non_published_projection_variants_reject_foreign_scope(
    publication_kind: str,
) -> None:
    uow = _Uow()
    if publication_kind == "unpublished":
        uow.projections.publication = UnpublishedActivePublication(
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="context_graph",
        )
    else:
        uow.projections.publication = UnavailableActivePublication(
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="context_graph",
            reason_code="repository_unavailable",
        )

    _, report = await _inspect(uow)

    assert report.status is CorpusReadinessStatus.NOT_READY
    assert report.revisions[0].reason is CorpusReadinessReason.SCOPE_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repository", "method", "reason"),
    (
        ("sources", "resolve_revision", CorpusReadinessReason.REVISION_UNAVAILABLE),
        ("canonical", "resolve_acceptance", CorpusReadinessReason.ACCEPTANCE_UNAVAILABLE),
        ("canonical", "resolve_current", CorpusReadinessReason.CURRENTNESS_UNAVAILABLE),
        (
            "ingestion",
            "list_bindings_for_source_revision",
            CorpusReadinessReason.INGESTION_UNAVAILABLE,
        ),
        (
            "projections",
            "list_runs_for_source_revision",
            CorpusReadinessReason.PROJECTION_PROCESSING_UNAVAILABLE,
        ),
        (
            "projections",
            "list_snapshot_members",
            CorpusReadinessReason.SNAPSHOT_UNAVAILABLE,
        ),
    ),
)
async def test_dependency_failure_reports_exact_unavailable_stage(
    repository: str, method: str, reason: CorpusReadinessReason
) -> None:
    uow = _Uow()
    setattr(getattr(uow, repository), method, lambda *_: _raise(RuntimeError("offline")))

    _, report = await _inspect(uow)

    assert report.status is CorpusReadinessStatus.UNAVAILABLE
    assert report.revisions[0].reason is reason


@pytest.mark.asyncio
async def test_retrieval_authorization_is_distinct_from_projection_membership() -> None:
    _, report = await _inspect(eligibility=CorpusRetrievalEligibility.DENIED)

    assert report.status is CorpusReadinessStatus.NOT_READY
    assert report.revisions[0].reason is CorpusReadinessReason.RETRIEVAL_DENIED
    assert report.revisions[0].membership_status == "included"


@pytest.mark.asyncio
async def test_authorized_scope_adapter_requires_exact_requested_revision_set() -> None:
    class Resolver:
        def __init__(self, constraints) -> None:
            self.constraints = constraints

        async def resolve(self, _: object):
            return SimpleNamespace(
                outcome=AuthorizedRetrievalScopeOutcome.RESOLVED,
                scope=SimpleNamespace(source_constraints=self.constraints),
            )

    exact = (SimpleNamespace(source_object_id=SOURCE_ID, source_revision_id=REVISION_ID),)
    eligibility = AuthorizedScopeCorpusEligibility(
        resolver=Resolver(exact), request_factory=lambda *_: object()
    )
    assert await eligibility.evaluate(
        context=_context(), publication=object(), revisions=(_expectation(),)
    ) is CorpusRetrievalEligibility.ELIGIBLE

    extra = exact + (
        SimpleNamespace(source_object_id=UUID(int=999), source_revision_id=UUID(int=1000)),
    )
    eligibility = AuthorizedScopeCorpusEligibility(
        resolver=Resolver(extra), request_factory=lambda *_: object()
    )
    assert await eligibility.evaluate(
        context=_context(), publication=object(), revisions=(_expectation(),)
    ) is CorpusRetrievalEligibility.DENIED


async def _raise(error: Exception):
    raise error
