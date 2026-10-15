from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.projection_control import (
    CanonicalProjectionStatusService,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestService,
    ProjectionPublicationStatus,
)
from spine.domain import EnvironmentKind
from spine.domain.projections import (
    ProjectionHealth,
    ProjectionFreshness,
    ProjectionReconciliationResult,
    ProjectionReconciliationOutcome,
    PublishedActivePublication,
    UnpublishedActivePublication,
    projection_reconciliation_findings_digest,
)
from spine.domain.workspaces import Environment


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000073")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000073")


class _ReadUow:
    def __init__(self, publication: object) -> None:
        self.projections = _ProjectionReader(publication)

    async def __aenter__(self) -> "_ReadUow":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def commit(self) -> None:
        return None


class _ProjectionReader:
    def __init__(self, publication: object) -> None:
        self.publication = publication

    async def resolve_active_publication(self, projection_kind: str) -> object:
        assert projection_kind == "qa"
        return self.publication

    async def list_drifts(self, activation_id: UUID) -> tuple[object, ...]:
        assert activation_id.int != 0
        return ()


class _StatusFactory:
    def __init__(self, publication: object) -> None:
        self.publication = publication

    def __call__(self, context: object) -> _ReadUow:
        del context
        return _ReadUow(self.publication)


class _FailingProjectionReader:
    async def resolve_active_publication(self, projection_kind: str) -> object:
        del projection_kind
        raise RuntimeError("provider namespace must not escape")


class _FailingStatusFactory:
    def __call__(self, context: object) -> _ReadUow:
        del context
        uow = _ReadUow(
            UnpublishedActivePublication(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                projection_kind="qa",
            )
        )
        uow.projections = _FailingProjectionReader()  # type: ignore[assignment]
        return uow


def _reconciliation(
    publication: PublishedActivePublication,
    outcome: ProjectionReconciliationOutcome,
) -> ProjectionReconciliationResult:
    findings_schema_version = "reconciliation-findings:v1"
    findings_payload = {"missing_artifacts": 1}
    return ProjectionReconciliationResult(
        reconciliation_result_id=UUID(int=101),
        workspace_id=publication.workspace_id,
        environment_id=publication.environment_id,
        projection_kind=publication.projection_kind,
        projection_snapshot_id=publication.projection_snapshot.projection_snapshot_id,
        projection_activation_id=publication.projection_activation.projection_activation_id,
        activation_generation=publication.activation_generation,
        attempt_identity="reconcile-1",
        attempt_digest="f" * 64,
        outcome=outcome,
        findings_schema_version=findings_schema_version,
        findings_payload=findings_payload,
        findings_digest=projection_reconciliation_findings_digest(
            findings_schema_version=findings_schema_version,
            findings_payload=findings_payload,
        ),
        artifact_evidence_reference="opaque-evidence-1",
        artifact_evidence_digest="e" * 64,
        idempotency_receipt_id=UUID(int=102),
        audit_event_reference="audit-1",
        checked_at=NOW,
    )


def _status_context(
    *, workspace_id: UUID = WORKSPACE_ID, environment_id: UUID = ENVIRONMENT_ID
) -> object:
    return SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
    )


@pytest.mark.asyncio
async def test_projection_status_distinguishes_unpublished_without_fabricating_dimensions() -> None:
    publication = UnpublishedActivePublication(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind="qa",
    )
    service = CanonicalProjectionStatusService(
        uow_factory=_StatusFactory(publication),
    )

    result = await service.status(
        context=_status_context(
            workspace_id=publication.workspace_id,
            environment_id=publication.environment_id,
        ),  # type: ignore[arg-type]
        projection_kind="qa",
    )

    assert result.publication_status is ProjectionPublicationStatus.UNPUBLISHED
    assert result.snapshot_id is None
    assert result.canonical_boundary_id is None
    assert result.freshness is None
    assert result.health is None
    assert result.retryable is False


@pytest.mark.asyncio
async def test_projection_status_exposes_only_safe_published_metadata() -> None:
    from tests.application.test_projection_publication import _publication

    publication = _publication()
    service = CanonicalProjectionStatusService(
        uow_factory=_StatusFactory(publication),
    )

    result = await service.status(
        context=_status_context(
            workspace_id=publication.workspace_id,
            environment_id=publication.environment_id,
        ),  # type: ignore[arg-type]
        projection_kind="qa",
    )

    assert result.publication_status is ProjectionPublicationStatus.PUBLISHED
    assert result.snapshot_id == publication.projection_snapshot.projection_snapshot_id
    assert result.activation_id == publication.projection_activation.projection_activation_id
    assert result.canonical_boundary_id == publication.canonical_boundary_id
    assert result.as_of is not None
    assert result.as_of.canonical_boundary_id == publication.canonical_boundary_id
    assert result.as_of.accepted_through_generation == 12
    assert result.as_of.boundary_time == publication.as_of.boundary_time
    assert result.activation_generation == publication.activation_generation
    assert result.projection_config_version_id == (
        publication.projection_config_version.projection_config_version_id
    )
    assert result.freshness is ProjectionFreshness.CURRENT
    assert result.health is ProjectionHealth.HEALTHY
    assert result.reconciliation is None
    assert result.last_operation is not None
    assert result.last_operation.operation == "publish"
    assert result.last_operation.status == "completed"
    assert result.recovery_state == "not_requested"
    assert "physical_namespace" not in result.model_dump()


@pytest.mark.asyncio
async def test_projection_status_marks_stale_degraded_publication_retryable() -> None:
    from tests.application.test_projection_publication import _publication

    publication = _publication().model_copy(
        update={
            "freshness": ProjectionFreshness.STALE,
            "health": ProjectionHealth.DEGRADED,
        }
    )
    service = CanonicalProjectionStatusService(
        uow_factory=_StatusFactory(publication),
    )

    result = await service.status(
        context=_status_context(
            workspace_id=publication.workspace_id,
            environment_id=publication.environment_id,
        ),  # type: ignore[arg-type]
        projection_kind="qa",
    )

    assert result.publication_status is ProjectionPublicationStatus.PUBLISHED
    assert result.freshness is ProjectionFreshness.STALE
    assert result.health is ProjectionHealth.DEGRADED
    assert result.retryable is True
    assert result.recovery_state == "not_requested"


@pytest.mark.asyncio
async def test_projection_status_exposes_failed_reconciliation_as_bounded_failure() -> None:
    from tests.application.test_projection_publication import _publication

    publication = _publication()
    reconciliation = _reconciliation(
        publication,
        ProjectionReconciliationOutcome.FAILED,
    )
    publication = publication.model_copy(
        update={"effective_reconciliation": reconciliation}
    )
    service = CanonicalProjectionStatusService(
        uow_factory=_StatusFactory(publication),
    )

    result = await service.status(
        context=_status_context(
            workspace_id=publication.workspace_id,
            environment_id=publication.environment_id,
        ),  # type: ignore[arg-type]
        projection_kind="qa",
    )

    assert result.reconciliation is not None
    assert result.reconciliation.outcome is ProjectionReconciliationOutcome.FAILED
    assert result.last_operation is not None
    assert result.last_operation.operation == "reconciliation"
    assert result.last_operation.retryable is True
    assert result.recovery_state == "failed"
    assert result.retryable is True


@pytest.mark.asyncio
async def test_projection_status_fails_closed_without_provider_diagnostics() -> None:
    service = CanonicalProjectionStatusService(uow_factory=_FailingStatusFactory())

    result = await service.status(
        context=_status_context(),  # type: ignore[arg-type]
        projection_kind="qa",
    )

    assert result.publication_status is ProjectionPublicationStatus.UNAVAILABLE
    assert result.reason_code == "dependency.unavailable"
    assert "provider namespace" not in result.model_dump_json()


def test_unpublished_status_rejects_partial_publication_metadata() -> None:
    from spine.application.projection_control import ProjectionStatusSnapshot

    with pytest.raises(ValueError, match="publication metadata"):
        ProjectionStatusSnapshot(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="qa",
            publication_status=ProjectionPublicationStatus.UNPUBLISHED,
            canonical_boundary_id=UUID(int=104),
        )


@pytest.mark.asyncio
async def test_projection_recovery_request_replays_and_emits_one_typed_outbox_intent() -> None:
    from tests.contracts.persistence.conftest import create_in_memory_adapter

    persistence_adapter = await create_in_memory_adapter()

    async with persistence_adapter.uow_factory(
        persistence_adapter.workspace_context(UUID(int=901))
    ) as uow:
        await uow.environments.add(
            Environment(
                id=UUID(int=902),
                workspace_id=UUID(int=901),
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="Projection control test",
            )
        )
        await uow.commit()

    context = persistence_adapter.ingestion_context(
        UUID(int=901), UUID(int=902), "request_projection_recovery"
    )
    service = ProjectionRecoveryRequestService(
        uow_factory=persistence_adapter.uow_factory,
        outbox_events=persistence_adapter.outbox_events,
    )

    first = await service.request(
        context=context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.REBUILD,
        idempotency_key=IdempotencyKey("projection-recovery-1"),
    )
    replay = await service.request(
        context=context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.REBUILD,
        idempotency_key=IdempotencyKey("projection-recovery-1"),
    )

    assert first.command_id == replay.command_id
    assert first.replay is False
    assert replay.replay is True

    await persistence_adapter.delivery_repository.materialize(
        limit=10,
        materialized_at=NOW,
    )
    claims = await persistence_adapter.delivery_repository.claim_due(
        limit=10,
        worker_identity="projection_control_test",
        claimed_at=NOW,
        lease_duration=timedelta(minutes=1),
    )
    matching = tuple(
        claim.intent
        for claim in claims
        if claim.intent.event_type == "projection.recovery_requested"
    )
    assert len(matching) == 1
    assert matching[0].payload.action is ProjectionRecoveryAction.REBUILD
    assert matching[0].payload.command_id == first.command_id
    assert matching[0].payload_snapshot is not None
    assert b"namespace" not in matching[0].payload_snapshot
    assert b"candidate" not in matching[0].payload_snapshot

    with pytest.raises(IdempotencyConflictError):
        await service.request(
            context=context,
            projection_kind="qa",
            action=ProjectionRecoveryAction.RETRY,
            idempotency_key=IdempotencyKey("projection-recovery-1"),
        )
