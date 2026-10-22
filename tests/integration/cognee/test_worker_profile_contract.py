from __future__ import annotations

from decimal import Decimal

from spine.memory.cognee.worker_profile import (
    CogneeWorkerProfile,
    ProfileApproval,
    ProfileConfigurationStatus,
    profile_configuration_status,
)
from tests.memory.cognee.worker_profile_fixtures import cognee_worker_profile_values


def test_worker_profile_round_trip_has_provider_free_readiness() -> None:
    values = cognee_worker_profile_values()
    values["profile_digest"] = CogneeWorkerProfile.digest_for(values)
    profile = CogneeWorkerProfile.model_validate(values)
    persisted_shape = profile.model_dump(mode="json")
    reloaded = CogneeWorkerProfile.model_validate(persisted_shape)

    approval = ProfileApproval(
        profile_id=reloaded.profile_id,
        profile_digest=reloaded.profile_digest,
        approval_reference=reloaded.approval_reference,
        deployment_id=reloaded.deployment_id,
        release_id=reloaded.release_id,
        approved=True,
    )
    status = profile_configuration_status(
        profile=reloaded,
        approval=approval,
        deployment_id=reloaded.deployment_id,
        release_id=reloaded.release_id,
    )

    assert reloaded == profile
    assert status is ProfileConfigurationStatus.VALID
    assert persisted_shape["postgres_secret_ref"] == "secretref://production/spine/postgres"
    assert "password" not in str(persisted_shape).lower()
    assert isinstance(reloaded.limits.max_budget_amount, Decimal)
