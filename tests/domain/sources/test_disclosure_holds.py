from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.disclosure_holds import (
    DisclosureHold,
    DisclosureHoldEffect,
    DisclosureHoldReason,
    DisclosureHoldRelease,
    disclosure_hold_dependency_id,
)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _hold(**overrides: object) -> DisclosureHold:
    values: dict[str, object] = {
        "hold_id": _id(1),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "source_object_id": _id(4),
        "source_revision_ids": (_id(5),),
        "dependency_ids": (
            disclosure_hold_dependency_id(_id(2), _id(3), _id(4), _id(5)),
        ),
        "artifact_ids": (_id(7),),
        "review_decision_id": _id(8),
        "evidence_observation_id": _id(9),
        "prohibited_effects": (
            DisclosureHoldEffect.RETRIEVAL,
            DisclosureHoldEffect.FINAL_DISCLOSURE,
        ),
        "authority_reference": _id(10),
        "authority_version_id": _id(11),
        "authority_digest": "a" * 64,
        "actor_id": _id(12),
        "reason_code": DisclosureHoldReason.GOVERNED_SOURCE_REVIEW,
        "effective_from": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "effective_until": datetime(2026, 2, 2, tzinfo=timezone.utc),
        "audit_event_id": _id(13),
        "outbox_event_id": _id(14),
        "supersedes_hold_id": None,
    }
    values.update(overrides)
    return DisclosureHold.model_validate(values)


def _release(hold: DisclosureHold, **overrides: object) -> DisclosureHoldRelease:
    values: dict[str, object] = {
        "release_id": _id(20),
        "workspace_id": hold.workspace_id,
        "environment_id": hold.environment_id,
        "source_object_id": hold.source_object_id,
        "hold_id": hold.hold_id,
        "authority_reference": _id(21),
        "authority_version_id": _id(22),
        "authority_digest": "b" * 64,
        "actor_id": _id(23),
        "reason_code": DisclosureHoldReason.REVIEW_COMPLETED,
        "released_at": datetime(2026, 1, 5, tzinfo=timezone.utc),
        "audit_event_id": _id(24),
        "outbox_event_id": _id(25),
        "superseded_by_hold_id": None,
    }
    values.update(overrides)
    return DisclosureHoldRelease.model_validate(values)


def test_disclosure_hold_covers_exact_immutable_source_lineage() -> None:
    hold = _hold()

    assert hold.schema_version == "disclosure-hold:v1"
    assert hold.source_revision_ids == (_id(5),)
    assert hold.dependency_ids == (
        disclosure_hold_dependency_id(_id(2), _id(3), _id(4), _id(5)),
    )
    assert hold.artifact_ids == (_id(7),)
    assert hold.prohibited_effects == (
        DisclosureHoldEffect.RETRIEVAL,
        DisclosureHoldEffect.FINAL_DISCLOSURE,
    )


@pytest.mark.parametrize(
    "field",
    ("source_revision_ids", "dependency_ids", "artifact_ids", "prohibited_effects"),
)
def test_disclosure_hold_rejects_empty_or_broad_scope(field: str) -> None:
    with pytest.raises(ValidationError):
        _hold(**{field: ()})


def test_disclosure_hold_rejects_duplicate_lineage_and_unbounded_interval() -> None:
    with pytest.raises(ValidationError):
        _hold(source_revision_ids=(_id(5), _id(5)))

    with pytest.raises(ValidationError):
        _hold(effective_until=datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_disclosure_hold_rejects_fabricated_dependency_lineage() -> None:
    with pytest.raises(ValidationError, match="exactly bind"):
        _hold(dependency_ids=(_id(999),))


def test_disclosure_hold_release_is_separate_and_exactly_bound() -> None:
    hold = _hold()
    release = _release(hold)

    release.validate_hold(hold)
    assert release.schema_version == "disclosure-hold-release:v1"

    with pytest.raises(ValueError, match="exact hold"):
        _release(hold, source_object_id=_id(99)).validate_hold(hold)


def test_disclosure_hold_and_release_reject_provider_payload_and_naive_time() -> None:
    with pytest.raises(ValidationError):
        _hold(provider_payload={"name": "protected"})
    with pytest.raises(ValidationError):
        _hold(effective_from=datetime(2026, 1, 2))
    with pytest.raises(ValidationError):
        _release(_hold(), released_at=datetime(2026, 1, 5))


def test_release_must_follow_hold_start() -> None:
    hold = _hold()

    with pytest.raises(ValueError, match="after the hold became effective"):
        _release(
            hold,
            released_at=hold.effective_from - timedelta(seconds=1),
        ).validate_hold(hold)
