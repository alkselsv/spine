from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.retention import (
    RetentionApplicability,
    RetentionArtifactClass,
    RetentionAuthorityKind,
    RetentionTargetKind,
)


NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _applicability(**updates: object) -> RetentionApplicability:
    values: dict[str, object] = {
        "applicability_id": _uuid(1),
        "workspace_id": _uuid(2),
        "environment_id": _uuid(3),
        "target_kind": RetentionTargetKind.EXACT_SOURCE,
        "source_object_id": _uuid(4),
        "artifact_classes": (
            RetentionArtifactClass.ORIGINAL,
            RetentionArtifactClass.PARSED_CONTENT,
        ),
        "retention_policy_id": _uuid(5),
        "retention_policy_version_id": _uuid(6),
        "retention_policy_digest": "a" * 64,
        "applicability_evidence_reference": _uuid(7),
        "applicability_evidence_digest": "b" * 64,
        "approving_authority_reference": _uuid(8),
        "approving_authority_version_id": _uuid(9),
        "approving_authority_digest": "c" * 64,
        "approving_authority_kind": RetentionAuthorityKind.DATA_PORTAL,
        "effective_from": NOW,
        "effective_until": datetime(2027, 10, 31, tzinfo=timezone.utc),
        "exception_references": (_uuid(10),),
        "legal_hold_references": (_uuid(11),),
        "expected_current_applicability_id": None,
        "expected_current_generation": 0,
        "generation": 1,
        "actor_id": _uuid(12),
        "audit_event_id": _uuid(13),
        "outbox_event_id": _uuid(14),
        "approved_at": NOW,
    }
    values.update(updates)
    return RetentionApplicability.model_validate(values)


def test_exact_source_applicability_is_immutable_and_contains_no_duration() -> None:
    applicability = _applicability()

    assert applicability.schema_version == "retention-applicability:v1"
    assert applicability.source_object_id == _uuid(4)
    assert applicability.legal_hold_references == (_uuid(11),)
    with pytest.raises(ValidationError):
        RetentionApplicability.model_validate(
            {**applicability.model_dump(mode="python"), "duration_days": 30}
        )


def test_source_class_applicability_requires_approved_class_identity() -> None:
    applicability = _applicability(
        target_kind=RetentionTargetKind.SOURCE_CLASS,
        source_object_id=None,
        source_class="document",
        source_class_reference=_uuid(20),
        source_class_version_id=_uuid(21),
        source_class_digest="d" * 64,
    )

    assert applicability.source_class == "document"
    with pytest.raises(ValidationError):
        _applicability(
            target_kind=RetentionTargetKind.SOURCE_CLASS,
            source_object_id=None,
            source_class="document",
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"artifact_classes": ()},
        {
            "artifact_classes": (
                RetentionArtifactClass.ORIGINAL,
                RetentionArtifactClass.ORIGINAL,
            )
        },
        {"effective_until": NOW},
        {"expected_current_applicability_id": _uuid(30)},
        {"generation": 2},
    ],
)
def test_invalid_applicability_shape_is_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _applicability(**updates)
