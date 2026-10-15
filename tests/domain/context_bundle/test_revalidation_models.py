from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.context_bundles import (
    RevalidationCheckpoint,
    RevalidationDisposition,
    RevalidationFailureCode,
    RevalidationPublicationImpact,
    RevalidationPublicationObservation,
    RevalidationRequest,
    RevalidationCheckpointResult,
)

from tests.domain.context_bundle.test_context_bundle_models import (
    ENVIRONMENT,
    SNAPSHOT,
    WORKSPACE,
    context_bundle,
)


def test_publication_observation_reports_activation_and_snapshot_changes() -> None:
    observation = RevalidationPublicationObservation(
        pinned_snapshot_id=SNAPSHOT,
        pinned_activation_generation=1,
        current_snapshot_id=UUID(int=90),
        current_activation_generation=2,
        impact=RevalidationPublicationImpact.ACTIVATION_ONLY,
    )

    assert observation.activation_changed
    assert observation.active_projection_changed


def test_revalidation_request_rejects_cross_workspace_bundle() -> None:
    with pytest.raises(ValidationError, match="scope"):
        RevalidationRequest(
            bundle=context_bundle(),
            workspace_id=UUID(int=99),
            environment_id=ENVIRONMENT,
            checkpoint=RevalidationCheckpoint.BEFORE_EVIDENCE_REUSE,
            idempotency_key="revalidate-1",
        )


def test_checkpoint_result_rejects_safe_replay_with_a_failure_reason() -> None:
    with pytest.raises(ValidationError, match="safe replay"):
        RevalidationCheckpointResult(
            disposition=RevalidationDisposition.SAFE_REPLAY,
            checkpoint=RevalidationCheckpoint.BEFORE_EVIDENCE_REUSE,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            bundle_reference=context_bundle().reference,
            idempotency_key="revalidate-1",
            command_digest="a" * 64,
            failure_codes=(RevalidationFailureCode.EVIDENCE_CORRUPT,),
            retry_attempt=1,
        )


def test_revalidation_command_digest_is_stable_for_the_same_request() -> None:
    first = RevalidationRequest(
        bundle=context_bundle(),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        checkpoint=RevalidationCheckpoint.BEFORE_EVIDENCE_REUSE,
        idempotency_key="revalidate-1",
    )
    second = first.model_copy()

    assert first.command_digest == second.command_digest
