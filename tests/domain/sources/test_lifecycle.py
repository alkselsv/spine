from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.lifecycle import (
    LifecycleDecision,
    LifecycleDecisionEffect,
    LifecycleAuthorityKind,
    LifecycleEligibilityStatus,
    LifecycleObservation,
    LifecycleObservationConfidence,
    LifecycleObservationKind,
    LifecycleReasonCode,
    lifecycle_status_for_observation,
)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _observation(**overrides: object) -> LifecycleObservation:
    values: dict[str, object] = {
        "observation_id": _id(1),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "source_object_id": _id(4),
        "connection_id": _id(5),
        "connection_version_id": _id(6),
        "authorization_generation": 2,
        "configuration_generation": 3,
        "scope_reference": _id(7),
        "scope_version_id": _id(8),
        "scope_digest": "a" * 64,
        "provider_identity_reference": _id(9),
        "kind": LifecycleObservationKind.INCOMPLETE_SCAN,
        "evidence_reference": _id(10),
        "evidence_digest": "b" * 64,
        "confidence": LifecycleObservationConfidence.UNCERTAIN,
        "reason_code": LifecycleReasonCode.SCAN_INCOMPLETE,
        "observed_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return LifecycleObservation.model_validate(values)


def _decision(**overrides: object) -> LifecycleDecision:
    values: dict[str, object] = {
        "decision_id": _id(11),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "source_object_id": _id(4),
        "observation_id": _id(1),
        "connection_id": _id(5),
        "connection_version_id": _id(6),
        "scope_reference": _id(7),
        "scope_version_id": _id(8),
        "scope_digest": "a" * 64,
        "effect": LifecycleDecisionEffect.READ_WITHDRAWN,
        "authority_reference": _id(12),
        "authority_version_id": _id(13),
        "authority_digest": "c" * 64,
        "authority_kind": LifecycleAuthorityKind.SOURCE_GOVERNANCE,
        "status": LifecycleEligibilityStatus.READ_WITHDRAWN,
        "legal_hold_active": False,
        "expected_current_decision_id": None,
        "expected_current_generation": 0,
        "generation": 1,
        "actor_id": _id(14),
        "audit_event_id": _id(15),
        "outbox_event_id": _id(16),
        "decided_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return LifecycleDecision.model_validate(values)


def test_lifecycle_observation_binds_tenant_connection_scope_and_evidence() -> None:
    observation = _observation()

    assert observation.schema_version == "lifecycle-observation:v1"
    assert observation.workspace_id == _id(2)
    assert observation.connection_version_id == _id(6)
    assert observation.scope_digest == "a" * 64
    assert observation.evidence_digest == "b" * 64


def test_lifecycle_observation_rejects_raw_provider_payload() -> None:
    with pytest.raises(ValidationError):
        _observation(provider_payload={"name": "protected"})


def test_lifecycle_observation_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _observation(observed_at=datetime(2026, 1, 2))


@pytest.mark.parametrize(
    ("kind", "confidence", "expected"),
    [
        (
            LifecycleObservationKind.PROVIDER_OUTAGE,
            LifecycleObservationConfidence.UNCERTAIN,
            LifecycleDecisionEffect.AUTHORITY_UNAVAILABLE,
        ),
        (
            LifecycleObservationKind.FILTERED_ABSENCE,
            LifecycleObservationConfidence.UNCERTAIN,
            LifecycleDecisionEffect.AUTHORITY_UNAVAILABLE,
        ),
        (
            LifecycleObservationKind.MOVE_OUTSIDE_SCOPE,
            LifecycleObservationConfidence.QUALIFIED,
            LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED,
        ),
        (
            LifecycleObservationKind.DELETED_METADATA,
            LifecycleObservationConfidence.QUALIFIED,
            LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED,
        ),
        (
            LifecycleObservationKind.QUALIFIED_DELETION_EVIDENCE,
            LifecycleObservationConfidence.QUALIFIED,
            LifecycleDecisionEffect.TOMBSTONE_CANDIDATE,
        ),
    ],
)
def test_lifecycle_observation_maps_to_safe_non_authorizing_effect(
    kind: LifecycleObservationKind,
    confidence: LifecycleObservationConfidence,
    expected: LifecycleDecisionEffect,
) -> None:
    assert lifecycle_status_for_observation(kind, confidence) is expected


def test_lifecycle_decision_requires_exact_expected_authority_generation() -> None:
    decision = LifecycleDecision(
        decision_id=_id(11),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        observation_id=_id(1),
        connection_id=_id(5),
        connection_version_id=_id(6),
        scope_reference=_id(7),
        scope_version_id=_id(8),
        scope_digest="a" * 64,
        effect=LifecycleDecisionEffect.READ_WITHDRAWN,
        authority_reference=_id(12),
        authority_version_id=_id(13),
        authority_digest="c" * 64,
        authority_kind=LifecycleAuthorityKind.SOURCE_GOVERNANCE,
        status=LifecycleEligibilityStatus.READ_WITHDRAWN,
        legal_hold_active=False,
        expected_current_decision_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=_id(14),
        audit_event_id=_id(15),
        outbox_event_id=_id(16),
        decided_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    with pytest.raises((ValidationError, TypeError)):
        decision.expected_current_generation = -1  # type: ignore[misc]


def test_scope_withdrawal_requires_exact_governed_withdrawal_outcome() -> None:
    observation = _observation(
        kind=LifecycleObservationKind.SCOPE_WITHDRAWN,
        confidence=LifecycleObservationConfidence.QUALIFIED,
        reason_code=LifecycleReasonCode.SCOPE_WITHDRAWN,
    )
    decision = _decision(
        effect=LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED,
        status=LifecycleEligibilityStatus.DISCLOSURE_REVIEW_REQUIRED,
    )

    with pytest.raises(ValueError, match="scope withdrawal requires read withdrawal"):
        decision.validate_observation(observation)


@pytest.mark.parametrize(
    "kind",
    [
        LifecycleObservationKind.PROVIDER_OUTAGE,
        LifecycleObservationKind.FILTERED_ABSENCE,
        LifecycleObservationKind.SCOPE_WITHDRAWN,
        LifecycleObservationKind.MOVE_WITHIN_SCOPE,
        LifecycleObservationKind.QUALIFIED_DELETION_EVIDENCE,
    ],
)
def test_disclosure_review_requires_exact_positive_move_or_deleted_evidence(
    kind: LifecycleObservationKind,
) -> None:
    observation = _observation(
        kind=kind,
        confidence=LifecycleObservationConfidence.QUALIFIED,
        reason_code=LifecycleReasonCode.PROVIDER_OUTAGE,
    )
    decision = _decision(
        effect=LifecycleDecisionEffect.DISCLOSURE_REVIEW_REQUIRED,
        status=LifecycleEligibilityStatus.DISCLOSURE_REVIEW_REQUIRED,
    )

    with pytest.raises(ValueError):
        decision.validate_observation(observation)


@pytest.mark.parametrize("field", ("source_object_id", "scope_reference", "scope_version_id"))
def test_scope_withdrawal_cannot_be_widened_beyond_exact_source_and_scope(
    field: str,
) -> None:
    observation = _observation(
        kind=LifecycleObservationKind.SCOPE_WITHDRAWN,
        confidence=LifecycleObservationConfidence.QUALIFIED,
        reason_code=LifecycleReasonCode.SCOPE_WITHDRAWN,
    )
    decision = _decision(**{field: _id(99)})

    with pytest.raises(ValueError, match="binding is unavailable"):
        decision.validate_observation(observation)


def test_lifecycle_decision_requires_current_pointer_pair() -> None:
    with pytest.raises(ValidationError):
        LifecycleDecision(
            decision_id=_id(11),
            workspace_id=_id(2),
            environment_id=_id(3),
            source_object_id=_id(4),
            observation_id=_id(1),
            connection_id=_id(5),
            connection_version_id=_id(6),
            scope_reference=_id(7),
            scope_version_id=_id(8),
            scope_digest="a" * 64,
            effect=LifecycleDecisionEffect.READ_WITHDRAWN,
            authority_reference=_id(12),
            authority_version_id=_id(13),
            authority_digest="c" * 64,
            authority_kind=LifecycleAuthorityKind.SOURCE_GOVERNANCE,
            status=LifecycleEligibilityStatus.READ_WITHDRAWN,
            legal_hold_active=False,
            expected_current_decision_id=_id(16),
            expected_current_generation=0,
            generation=1,
            actor_id=_id(14),
            audit_event_id=_id(15),
            outbox_event_id=_id(16),
            decided_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )


def test_lifecycle_decision_cannot_convert_deletion_evidence_to_tombstone() -> None:
    with pytest.raises(ValidationError, match="cannot turn a tombstone candidate"):
        LifecycleDecision(
            decision_id=_id(11),
            workspace_id=_id(2),
            environment_id=_id(3),
            source_object_id=_id(4),
            observation_id=_id(1),
            connection_id=_id(5),
            connection_version_id=_id(6),
            scope_reference=_id(7),
            scope_version_id=_id(8),
            scope_digest="a" * 64,
            effect=LifecycleDecisionEffect.TOMBSTONE_CANDIDATE,
            authority_reference=_id(12),
            authority_version_id=_id(13),
            authority_digest="c" * 64,
            authority_kind=LifecycleAuthorityKind.SOURCE_GOVERNANCE,
            status=LifecycleEligibilityStatus.TOMBSTONED,
            legal_hold_active=False,
            expected_current_decision_id=None,
            expected_current_generation=0,
            generation=1,
            actor_id=_id(14),
            audit_event_id=_id(15),
            outbox_event_id=_id(16),
            decided_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
