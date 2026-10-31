from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.canonical_acceptance_selector import (
    CanonicalAcceptanceAuthorityKind,
    TrustedCanonicalAcceptanceSelector,
)


def ident(value: int) -> UUID:
    return UUID(int=value)


def selector(**overrides: object) -> TrustedCanonicalAcceptanceSelector:
    values: dict[str, object] = {
        "selector_id": ident(1),
        "selector_series_id": ident(2),
        "selector_version": 1,
        "workspace_id": ident(3),
        "environment_id": ident(4),
        "source_kind": "bitrix24_disk",
        "source_contract_id": "bitrix24-disk-content",
        "source_contract_version": "v2",
        "authority_kind": CanonicalAcceptanceAuthorityKind.PROVIDER_CONNECTION,
        "approved_scope_reference": ident(9),
        "provider_kind": "bitrix24",
        "provider_connection_id": ident(6),
        "provider_connection_version_id": ident(7),
        "provider_approval_id": ident(8),
        "resource_scope_reference": ident(9),
        "root_policy_id": ident(10),
        "root_policy_version": "v1",
        "root_policy_digest": "a" * 64,
        "content_policy_id": ident(11),
        "content_policy_version": "v1",
        "content_policy_digest": "b" * 64,
        "required_canonical_acceptance_schema_version": 2,
        "approval_reference": ident(8),
        "audit_reference": ident(13),
        "idempotency_identity": "selector:test:1",
        "created_at": datetime(2026, 10, 31, tzinfo=UTC),
    }
    values.update(overrides)
    return TrustedCanonicalAcceptanceSelector.model_validate(values)


def test_provider_selector_requires_exact_connection_and_policy_bindings() -> None:
    selected = selector()

    assert selected.required_canonical_acceptance_schema_version == 2
    assert selected.root_policy_digest == "a" * 64
    assert selected.content_policy_digest == "b" * 64


def test_selector_rejects_partial_policy_binding() -> None:
    with pytest.raises(ValidationError, match="policy references must be complete"):
        selector(content_policy_digest=None)


def test_selector_rejects_successor_without_predecessor() -> None:
    with pytest.raises(ValidationError, match="version and predecessor"):
        selector(selector_version=2)


def test_selector_series_identity_excludes_policy_successor_but_pins_scope() -> None:
    current = selector()
    successor = selector(
        selector_id=ident(20),
        selector_version=2,
        predecessor_selector_id=current.selector_id,
        root_policy_id=ident(21),
        content_policy_id=ident(22),
    )

    assert successor.series_identity == current.series_identity
    assert successor.series_identity != selector(
        approved_scope_reference=ident(23), resource_scope_reference=ident(23)
    ).series_identity


def test_upload_selector_requires_exact_upload_authority() -> None:
    upload = selector(
        authority_kind=CanonicalAcceptanceAuthorityKind.UPLOAD,
        required_canonical_acceptance_schema_version=1,
        source_contract_version="v1",
        provider_kind=None,
        provider_connection_id=None,
        provider_connection_version_id=None,
        provider_approval_id=None,
        resource_scope_reference=None,
        upload_authority_reference=ident(14),
        approved_scope_reference=ident(14),
        approval_reference=ident(14),
        root_policy_id=None,
        root_policy_version=None,
        root_policy_digest=None,
        content_policy_id=None,
        content_policy_version=None,
        content_policy_digest=None,
    )

    assert upload.upload_authority_reference == ident(14)
