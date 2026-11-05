from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.commit_fence import (
    CommitFenceBinding,
    CommitFenceOutcome,
    CommitFenceOutcomeKind,
    canonical_commit_fence_lineage_digest,
)
from spine.application.private_asr_corpus import PrivateArtifactSourceLineage


NOW = datetime(2026, 11, 5, 14, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _source(number: int = 1, **updates: object) -> PrivateArtifactSourceLineage:
    values: dict[str, object] = {
        "source_object_id": _id(100 + number),
        "source_revision_id": _id(200 + number),
        "source_revision_digest": f"{number:064x}",
    }
    values.update(updates)
    return PrivateArtifactSourceLineage.model_validate(values)


def _binding(**updates: object) -> CommitFenceBinding:
    lineage = (_source(1), _source(2))
    values: dict[str, object] = {
        "issuer_id": _id(1),
        "approval_version": "approval:v3",
        "approval_digest": "a" * 64,
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "purpose": "private_report_publication",
        "action": "commit_report",
        "subject_id": _id(4),
        "state_digest": "b" * 64,
        "source_lineage": lineage,
        "lineage_digest": canonical_commit_fence_lineage_digest(
            workspace_id=_id(2), environment_id=_id(3), source_lineage=lineage
        ),
        "generation": 2,
        "revocation_epoch": 7,
        "operation_id": _id(5),
        "idempotency_key": "report-run-2026-11-05-001",
        "valid_from": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(minutes=5),
    }
    values.update(updates)
    if "lineage_digest" not in updates and (
        "workspace_id" in updates or "environment_id" in updates or "source_lineage" in updates
    ) and isinstance(values["workspace_id"], UUID) and isinstance(values["environment_id"], UUID):
        values["lineage_digest"] = canonical_commit_fence_lineage_digest(
            workspace_id=values["workspace_id"],  # type: ignore[arg-type]
            environment_id=values["environment_id"],  # type: ignore[arg-type]
            source_lineage=values["source_lineage"],  # type: ignore[arg-type]
        )
    return CommitFenceBinding.model_validate(values)


def test_fence_binding_is_immutable_versioned_and_binds_all_authority_inputs() -> None:
    binding = _binding()

    assert binding.schema_version == "external-commit-fence-binding:v1"
    assert binding.is_valid_at(NOW)
    assert not binding.is_valid_at(binding.valid_until)
    with pytest.raises(ValidationError):
        binding.model_copy(update={"workspace_id": _id(99)})
    with pytest.raises(ValidationError):
        binding.workspace_id = _id(99)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer_id", None),
        ("approval_version", ""),
        ("approval_digest", "not-a-digest"),
        ("workspace_id", None),
        ("environment_id", None),
        ("purpose", ""),
        ("action", ""),
        ("subject_id", None),
        ("state_digest", None),
        ("lineage_digest", "c" * 64),
        ("generation", 0),
        ("revocation_epoch", -1),
        ("operation_id", None),
        ("idempotency_key", ""),
    ],
)
def test_fence_binding_rejects_missing_or_forged_authority_inputs(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        _binding(**{field: value})


@pytest.mark.parametrize("field", ["workspace_id", "environment_id", "purpose", "action", "subject_id", "state_digest", "generation", "revocation_epoch", "issuer_id"])
def test_fence_binding_digest_changes_when_authority_scope_is_substituted(field: str) -> None:
    binding = _binding()
    replacement: object = "different-purpose" if field in {"purpose", "action"} else _id(99)

    if field == "generation":
        replacement = binding.generation + 1
    elif field == "revocation_epoch":
        replacement = binding.revocation_epoch + 1
    elif field == "state_digest":
        replacement = "d" * 64

    assert binding.calculate_digest() != _binding(**{field: replacement}).calculate_digest()


def test_composite_lineage_digest_is_unique_and_order_canonical() -> None:
    first, second = _source(1), _source(2)

    forward = canonical_commit_fence_lineage_digest(
        workspace_id=_id(2), environment_id=_id(3), source_lineage=(first, second)
    )
    reverse = canonical_commit_fence_lineage_digest(
        workspace_id=_id(2), environment_id=_id(3), source_lineage=(second, first)
    )

    assert forward == reverse
    with pytest.raises(ValueError, match="unique"):
        canonical_commit_fence_lineage_digest(
            workspace_id=_id(2), environment_id=_id(3), source_lineage=(first, first)
        )


def test_binding_digest_is_order_canonical_for_composite_source_lineage() -> None:
    forward = _binding()
    reverse = _binding(source_lineage=tuple(reversed(forward.source_lineage)))

    assert reverse.source_lineage == forward.source_lineage
    assert reverse.calculate_digest() == forward.calculate_digest()


def test_fence_outcome_requires_opaque_permit_only_for_acquired_or_released() -> None:
    acquired = CommitFenceOutcome(
        binding=_binding(),
        outcome=CommitFenceOutcomeKind.ACQUIRED,
        evaluated_at=NOW,
        permit_reference=_id(90),
        permit_valid_until=NOW + timedelta(minutes=1),
    )

    assert acquired.outcome is CommitFenceOutcomeKind.ACQUIRED
    with pytest.raises(ValidationError):
        CommitFenceOutcome(
            binding=_binding(),
            outcome=CommitFenceOutcomeKind.ACQUIRED,
            evaluated_at=NOW,
        )


@pytest.mark.parametrize(
    "outcome",
    [
        CommitFenceOutcomeKind.DENIED,
        CommitFenceOutcomeKind.STALE,
        CommitFenceOutcomeKind.EXPIRED,
        CommitFenceOutcomeKind.AMBIGUOUS,
        CommitFenceOutcomeKind.RELEASED,
    ],
)
def test_fence_outcomes_have_explicit_non_acquired_states(outcome: CommitFenceOutcomeKind) -> None:
    values: dict[str, object] = {
        "binding": _binding(),
        "outcome": outcome,
        "evaluated_at": NOW,
    }
    if outcome is CommitFenceOutcomeKind.RELEASED:
        values["permit_reference"] = _id(90)

    result = CommitFenceOutcome.model_validate(values)

    assert result.outcome is outcome
    assert result.outcome is not CommitFenceOutcomeKind.ACQUIRED


def test_fence_binding_requires_utc_validity_interval_and_expiry_equality_denies() -> None:
    with pytest.raises(ValidationError):
        _binding(valid_until=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        _binding(valid_until=NOW.astimezone(timezone(timedelta(hours=3))))

    binding = _binding(valid_until=NOW)
    assert not binding.is_valid_at(binding.valid_until)
