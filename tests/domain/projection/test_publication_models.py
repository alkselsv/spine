from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.projections import (
    ActiveProjection,
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionDrift,
    ProjectionDriftDetectedBy,
    UnpublishedActivePublication,
)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
IDS = [UUID(int=value) for value in range(1, 20)]


def test_activation_and_pointer_are_tenant_scoped_and_monotonic() -> None:
    activation = ProjectionActivation(
        projection_activation_id=IDS[0],
        workspace_id=IDS[1],
        environment_id=IDS[2],
        projection_kind="qa",
        projection_snapshot_id=IDS[3],
        activation_generation=1,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activation-1",
        command_digest="a" * 64,
        idempotency_receipt_id=IDS[4],
        audit_event_id=IDS[5],
        activated_at=NOW,
    )
    pointer = ActiveProjection(
        workspace_id=activation.workspace_id,
        environment_id=activation.environment_id,
        projection_kind=activation.projection_kind,
        projection_snapshot_id=activation.projection_snapshot_id,
        projection_activation_id=activation.projection_activation_id,
        activation_generation=activation.activation_generation,
        cas_version=1,
    )

    assert pointer.projection_activation_id == activation.projection_activation_id
    with pytest.raises(ValidationError):
        ActiveProjection.model_validate(
            {**pointer.model_dump(), "activation_generation": 0}
        )


def test_drift_requires_a_specific_canonical_acceptance_and_detection_source() -> None:
    drift = ProjectionDrift(
        projection_drift_id=IDS[0],
        workspace_id=IDS[1],
        environment_id=IDS[2],
        projection_kind="qa",
        projection_snapshot_id=IDS[3],
        projection_activation_id=IDS[4],
        activation_generation=2,
        canonical_acceptance_id=IDS[5],
        source_object_id=IDS[6],
        source_revision_id=IDS[7],
        accepted_generation=3,
        detected_by=ProjectionDriftDetectedBy.CANONICAL_ACCEPTANCE,
        detected_at=NOW,
    )

    assert drift.detected_by is ProjectionDriftDetectedBy.CANONICAL_ACCEPTANCE
    assert drift.accepted_generation > drift.activation_generation


def test_unpublished_resolution_does_not_fabricate_freshness_or_health() -> None:
    result = UnpublishedActivePublication(
        workspace_id=IDS[1],
        environment_id=IDS[2],
        projection_kind="qa",
    )

    assert result.status == "unpublished"
    assert not hasattr(result, "freshness")
    assert not hasattr(result, "health")


def test_canonical_as_of_rejects_naive_boundary_time() -> None:
    with pytest.raises(ValidationError):
        CanonicalAsOf(
            canonical_boundary_id=IDS[1],
            accepted_through_generation=1,
            boundary_time=datetime(2026, 10, 14, 12, 0),
        )
