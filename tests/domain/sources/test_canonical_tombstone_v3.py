from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources import CanonicalAcceptance, CanonicalOrdering, RevisionKind
from spine.domain.sources.canonical_tombstone_selector import (
    CanonicalTombstoneWriterMode,
    TrustedCanonicalTombstoneVersionSelector,
)


NOW = datetime(2026, 11, 5, tzinfo=UTC)


def ident(value: int) -> UUID:
    return UUID(int=value)


def acceptance(**overrides: object) -> CanonicalAcceptance:
    values: dict[str, object] = {
        "canonical_acceptance_schema_version": 3,
        "canonical_acceptance_id": ident(1),
        "source_object_id": ident(2),
        "source_revision_id": ident(3),
        "revision_kind": RevisionKind.TOMBSTONE,
        "workspace_id": ident(4),
        "environment_id": ident(5),
        "predecessor_revision_id": ident(6),
        "predecessor_acceptance_id": ident(7),
        "accepted_generation": 2,
        "ordering": CanonicalOrdering.EXPECTED_CURRENT,
        "tombstone_authorization_id": ident(8),
        "tombstone_authorization_digest": "a" * 64,
        "transition_id": ident(9),
        "provenance_id": ident(10),
        "idempotency_receipt_id": ident(11),
        "audit_event_id": ident(12),
        "outbox_event_id": ident(13),
        "accepted_at": NOW,
    }
    values.update(overrides)
    return CanonicalAcceptance.model_validate(values)


def selector(**overrides: object) -> TrustedCanonicalTombstoneVersionSelector:
    values: dict[str, object] = {
        "selector_id": ident(20),
        "selector_series_id": ident(21),
        "selector_version": 1,
        "workspace_id": ident(4),
        "environment_id": ident(5),
        "source_object_id": ident(6),
        "source_contract_id": "yandex_disk.file",
        "source_contract_version": "v1",
        "provider_kind": "yandex_disk",
        "connection_id": ident(22),
        "resource_scope_reference": ident(23),
        "mode": CanonicalTombstoneWriterMode.V3_REQUIRED,
        "approval_reference": ident(24),
        "audit_reference": ident(25),
        "idempotency_identity": "tombstone-selector:yandex:v1",
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "created_at": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return TrustedCanonicalTombstoneVersionSelector.model_validate(values)


def test_v3_acceptance_requires_exact_tombstone_authorization_link() -> None:
    result = acceptance()

    assert result.canonical_acceptance_schema_version == 3
    assert result.tombstone_authorization_id == ident(8)
    with pytest.raises(ValidationError, match="version 3 requires"):
        acceptance(tombstone_authorization_id=None)


def test_v3_acceptance_rejects_content_authority_links() -> None:
    with pytest.raises(ValidationError, match="tombstone-only"):
        acceptance(admission_decision_id=ident(30))
    with pytest.raises(ValidationError, match="tombstone-only"):
        acceptance(processing_admission_id=ident(31))
    with pytest.raises(ValidationError, match="tombstone-only"):
        acceptance(revision_kind=RevisionKind.CONTENT)


def test_historical_versions_cannot_fabricate_tombstone_authorization_link() -> None:
    with pytest.raises(ValidationError, match="only version 3"):
        acceptance(
            canonical_acceptance_schema_version=1,
            revision_kind=None,
        )


def test_selector_modes_are_explicit_and_expiry_denies_new_writes() -> None:
    selected = selector()

    assert selected.writer_mode_at(NOW) is CanonicalTombstoneWriterMode.V3_REQUIRED
    assert selected.writer_mode_at(NOW + timedelta(days=2)) is CanonicalTombstoneWriterMode.DENY_NEW


def test_legacy_selector_is_time_bounded_and_successor_is_immutable_history() -> None:
    legacy = selector(mode=CanonicalTombstoneWriterMode.LEGACY_V1_ALLOWED)
    successor = selector(
        selector_id=ident(26),
        selector_version=2,
        predecessor_selector_id=legacy.selector_id,
        mode=CanonicalTombstoneWriterMode.V3_REQUIRED,
    )

    assert legacy.writer_mode_at(NOW) is CanonicalTombstoneWriterMode.LEGACY_V1_ALLOWED
    assert successor.series_identity == legacy.series_identity
    with pytest.raises(ValidationError, match="version and predecessor"):
        selector(selector_version=2)
