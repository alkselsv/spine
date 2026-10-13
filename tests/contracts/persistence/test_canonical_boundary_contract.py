from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.boundary import CanonicalBoundaryCaptureCommand
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.sources import canonical_boundary_manifest_digest
from tests.contracts.persistence.test_canonical_acceptance_contract import prepare_case


@pytest.mark.asyncio
async def test_boundary_capture_materializes_exact_current_heads_and_replays(
    persistence_adapter,
) -> None:
    case = await prepare_case(persistence_adapter, 23000)
    async with persistence_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    context = persistence_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "canonical_boundary_capture",
    )
    command = CanonicalBoundaryCaptureCommand.create(
        projection_kind="qa",
        scope_version="r1",
        idempotency_key=IdempotencyKey("boundary-23000"),
    )

    async with persistence_adapter.uow_factory(context) as uow:
        first = await uow.boundaries.capture(command)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        replay = await uow.canonical_boundaries.capture(command)
        await uow.rollback()

    assert first.replay is False
    assert first.boundary.accepted_through_generation == 1
    assert len(first.members) == 1
    assert first.members[0].source_revision_id == case.revision.revision_id
    assert canonical_boundary_manifest_digest(first.boundary, first.members) == (
        first.boundary.manifest_digest
    )
    assert replay.replay is True
    assert replay.boundary == first.boundary
    assert replay.members == first.members


def test_boundary_digest_is_independent_of_member_input_order() -> None:
    from spine.domain.sources import CanonicalBoundary, CanonicalBoundaryMember, RevisionKind

    boundary = CanonicalBoundary(
        boundary_id=UUID("00000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000002"),
        environment_id=UUID("00000000-0000-0000-0000-000000000003"),
        projection_kind="qa",
        scope_version="r1",
        accepted_through_generation=2,
        boundary_time=datetime(2026, 10, 14, tzinfo=timezone.utc),
        manifest_digest="0" * 64,
        idempotency_receipt_id=UUID("00000000-0000-0000-0000-000000000004"),
    )
    common = dict(
        boundary_id=boundary.boundary_id,
        workspace_id=boundary.workspace_id,
        environment_id=boundary.environment_id,
        revision_kind=RevisionKind.CONTENT,
        accepted_generation=1,
    )
    first = CanonicalBoundaryMember(
        source_object_id=UUID("00000000-0000-0000-0000-000000000010"),
        source_revision_id=UUID("00000000-0000-0000-0000-000000000011"),
        acceptance_id=UUID("00000000-0000-0000-0000-000000000012"),
        **common,
    )
    second = CanonicalBoundaryMember(
        source_object_id=UUID("00000000-0000-0000-0000-000000000020"),
        source_revision_id=UUID("00000000-0000-0000-0000-000000000021"),
        acceptance_id=UUID("00000000-0000-0000-0000-000000000022"),
        **common,
    )

    assert canonical_boundary_manifest_digest(boundary, (first, second)) == (
        canonical_boundary_manifest_digest(boundary, (second, first))
    )
