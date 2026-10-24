from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.repositories import SourceObservationCommand
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
from spine.domain.workspaces import Environment, Workspace
from tests.contracts.persistence.test_canonical_acceptance_contract import (
    _audit,
    _canonical_outbox,
    prepare_case,
)
from tests.contracts.persistence.test_source_observation_contract import (
    provenance,
    tombstone,
)


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


@pytest.mark.asyncio
async def test_projection_successor_retry_after_outbox_failure_commits_one_effect() -> None:
    from tests.contracts.persistence.conftest import create_in_memory_adapter

    adapter = await create_in_memory_adapter()
    case = await prepare_case(adapter, 57_600)
    async with adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    context = adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "request_projection_recovery",
    )

    class FailingOutbox:
        async def append(self, intent):
            del intent
            raise RuntimeError("synthetic outbox failure")

    class FailingUnitOfWork:
        def __init__(self, inner):
            self._inner = inner
            self.outbox = FailingOutbox()

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            await self._inner.__aexit__(exc_type, exc_value, traceback)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    failing = ProjectionRecoveryRequestService(
        uow_factory=lambda trusted: FailingUnitOfWork(
            adapter.uow_factory(trusted)
        ),
        outbox_events=adapter.outbox_events,
    )
    key = IdempotencyKey("projection-successor-partial-failure-575")
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await failing.request(
            context=context,
            projection_kind="qa",
            action=ProjectionRecoveryAction.SUCCESSOR,
            idempotency_key=key,
        )

    recovered = await ProjectionRecoveryRequestService(
        uow_factory=adapter.uow_factory,
        outbox_events=adapter.outbox_events,
    ).request(
        context=context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.SUCCESSOR,
        idempotency_key=key,
    )
    await adapter.delivery_repository.materialize(limit=20, materialized_at=NOW)
    claims = await adapter.delivery_repository.claim_due(
        limit=20,
        worker_identity="projection_successor_partial_failure_test",
        claimed_at=NOW,
        lease_duration=timedelta(minutes=1),
    )
    matching_claims = tuple(
        claim
        for claim in claims
        if claim.intent.event_type == "projection.successor_requested"
    )
    matching = tuple(claim.intent for claim in matching_claims)

    assert recovered.replay is False
    assert len(matching) == 1
    assert matching[0].payload.command_id == recovered.command_id


@pytest.mark.asyncio
async def test_projection_successor_after_head_change_captures_distinct_boundary() -> None:
    from tests.contracts.persistence.conftest import create_in_memory_adapter

    adapter = await create_in_memory_adapter()
    case = await prepare_case(adapter, 57_700)
    async with adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    context = adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "request_projection_recovery",
    )
    service = ProjectionRecoveryRequestService(
        uow_factory=adapter.uow_factory,
        outbox_events=adapter.outbox_events,
    )
    first = await service.request(
        context=context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.SUCCESSOR,
        idempotency_key=IdempotencyKey("projection-successor-before-change-575"),
    )

    deletion = tombstone(case.source, UUID(int=57_799))
    deletion_provenance = provenance(case.source, deletion.revision_id, "event-2")
    source_context = adapter.source_context(
        case.source.workspace_id, case.source.environment_id
    )
    async with adapter.uow_factory(source_context) as uow:
        await uow.sources.record_observation(
            SourceObservationCommand.create(
                source=case.source,
                revision=deletion,
                provenance=deletion_provenance,
                idempotency_key=IdempotencyKey("projection-successor-delete-575"),
            )
        )
        await uow.commit()
    acceptance_id = UUID(int=57_795)
    audit_id = UUID(int=57_796)
    outbox_id = UUID(int=57_797)
    command = CanonicalAcceptanceCommand.create(
        source=case.source,
        revision=deletion,
        provenance=deletion_provenance,
        expected_current_revision_id=case.revision.revision_id,
        idempotency_key=IdempotencyKey("projection-successor-delete-acceptance-575"),
        canonical_acceptance_id=acceptance_id,
        admission_decision_id=None,
        access_policy_version_id=None,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=_audit(
            context=case.context,
            source_revision_id=deletion.revision_id,
            event_id=audit_id,
            reason="source_tombstoned",
            transition="source.tombstoned",
        ),
        outbox_intent=_canonical_outbox(
            adapter,
            context=case.context,
            source_object_id=case.source.source_object_id,
            source_revision_id=deletion.revision_id,
            acceptance_id=acceptance_id,
            generation=2,
            audit_event_id=audit_id,
            event_id=outbox_id,
            transition="source.tombstoned",
        ),
    )
    async with adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(command)
        await uow.commit()

    second = await service.request(
        context=context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.SUCCESSOR,
        idempotency_key=IdempotencyKey("projection-successor-after-change-575"),
    )
    await adapter.delivery_repository.materialize(limit=20, materialized_at=NOW)
    claims = await adapter.delivery_repository.claim_due(
        limit=20,
        worker_identity="projection_successor_changed_head_test",
        claimed_at=NOW,
        lease_duration=timedelta(minutes=1),
    )
    intents = {
        claim.intent.event_id: claim.intent
        for claim in claims
        if claim.intent.event_type == "projection.successor_requested"
    }
    first_payload = intents[first.intent_id].payload
    second_payload = intents[second.intent_id].payload

    assert first.command_id != second.command_id
    assert first.intent_id != second.intent_id
    assert first_payload.canonical_boundary_id != second_payload.canonical_boundary_id
    assert first_payload.accepted_through_generation == 1
    assert second_payload.accepted_through_generation == 2


@pytest.mark.asyncio
async def test_projection_successor_request_captures_current_boundary_and_replays_one_intent() -> None:
    from tests.contracts.persistence.conftest import create_in_memory_adapter

    persistence_adapter = await create_in_memory_adapter()
    case = await prepare_case(persistence_adapter, 57_500)
    async with persistence_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()

    context = persistence_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "request_projection_recovery",
    )
    service = ProjectionRecoveryRequestService(
        uow_factory=persistence_adapter.uow_factory,
        outbox_events=persistence_adapter.outbox_events,
    )

    first = await service.request(
        context=context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.SUCCESSOR,
        idempotency_key=IdempotencyKey("projection-successor-1"),
    )
    replay = await service.request(
        context=context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.SUCCESSOR,
        idempotency_key=IdempotencyKey("projection-successor-1"),
    )

    assert first.command_id == replay.command_id
    assert first.replay is False
    assert replay.replay is True
    accepted_status = await service.successor_status(
        context=context,
        projection_kind="qa",
        command_id=first.command_id,
    )
    unknown_status = await service.successor_status(
        context=context,
        projection_kind="qa",
        command_id=UUID(int=57_599),
    )
    foreign_workspace_id = UUID(int=67_500)
    foreign_environment_id = UUID(int=67_501)
    async with persistence_adapter.uow_factory(
        persistence_adapter.workspace_context(foreign_workspace_id)
    ) as uow:
        await uow.workspaces.add(
            Workspace(
                id=foreign_workspace_id,
                slug="foreign-projection-status",
                display_name="Foreign projection status workspace",
            )
        )
        await uow.environments.add(
            Environment(
                id=foreign_environment_id,
                workspace_id=foreign_workspace_id,
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="Foreign projection status scope",
            )
        )
        await uow.commit()
    foreign_status = await service.successor_status(
        context=persistence_adapter.ingestion_context(
            foreign_workspace_id,
            foreign_environment_id,
            "inspect_projection_status",
        ),
        projection_kind="qa",
        command_id=first.command_id,
    )
    assert accepted_status.status == "unavailable"
    assert unknown_status.status == "unavailable"
    assert foreign_status.status == "unavailable"

    await persistence_adapter.delivery_repository.materialize(
        limit=20,
        materialized_at=NOW,
    )
    pending_status = await service.successor_status(
        context=context,
        projection_kind="qa",
        command_id=first.command_id,
    )
    assert pending_status.status == "accepted"
    claims = await persistence_adapter.delivery_repository.claim_due(
        limit=20,
        worker_identity="projection_successor_control_test",
        claimed_at=NOW,
        lease_duration=timedelta(minutes=1),
    )
    matching_claims = tuple(
        claim
        for claim in claims
        if claim.intent.event_type == "projection.successor_requested"
    )
    matching = tuple(claim.intent for claim in matching_claims)

    assert len(matching) == 1
    payload = matching[0].payload
    assert payload.command_id == first.command_id
    assert payload.projection_kind == "qa"
    assert payload.canonical_boundary_id is not None
    assert payload.canonical_boundary_digest != "0" * 64
    assert payload.accepted_through_generation == 1
    assert matching[0].payload_snapshot is not None
    assert b"source_revision" not in matching[0].payload_snapshot
    assert b"namespace" not in matching[0].payload_snapshot
    assert b"candidate" not in matching[0].payload_snapshot
    quarantined = await persistence_adapter.delivery_repository.quarantine(
        matching_claims[0],
        quarantined_at=NOW + timedelta(seconds=1),
        error_code="projection.lineage_invalid",
    )
    failed_status = await service.successor_status(
        context=context,
        projection_kind="qa",
        command_id=first.command_id,
    )
    after_status = await persistence_adapter.delivery_repository.resolve(
        quarantined.event_id
    )
    assert failed_status.status == "failed"
    assert after_status == quarantined
