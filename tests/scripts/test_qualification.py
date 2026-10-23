from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.auth import AuthorizationRole
from spine.scripts.qualification import (
    QualificationBootstrapSettings,
    build_authorization_records,
    build_context_profiles,
    build_target_binding,
    qualification_workspace_slug,
)


def settings(**overrides: object) -> QualificationBootstrapSettings:
    values: dict[str, object] = {
        "deployment_mode": "development",
        "migration_database_url": "postgresql+psycopg://migration:secret@db/spine",
        "identity_id": UUID("50800000-0000-4000-8000-000000000001"),
        "authentication_alias_issuer": "https://local.spine.invalid",
        "authentication_alias_subject": "issue-508-operator",
        "workspace_a_id": UUID("50800000-0000-4000-8000-00000000000a"),
        "workspace_b_id": UUID("50800000-0000-4000-8000-00000000000b"),
        "environment_a_id": UUID("50800000-0000-4000-8000-00000000000e"),
        "environment_b_id": UUID("50800000-0000-4000-8000-00000000000f"),
        "authority_expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
        "target_reference": "deployment:source:fingerprint-v1",
        "target_scope_reference": "scope:workspace-environment:v1",
        "secret_provider_reference": "provider:local-env:v1",
        "secret_reference": "secret:qualification-source:v1",
        "database_role_reference": "postgres-role:spine-runtime:v1",
    }
    values.update(overrides)
    return QualificationBootstrapSettings(**values)


def test_authority_records_cover_approved_and_denied_qualification_scenarios() -> None:
    records = build_authorization_records(settings())

    scopes = {
        (record.workspace_id, record.environment_id)
        for record in records.environment_owners
    }

    assert scopes == {
        (
            UUID("50800000-0000-4000-8000-00000000000a"),
            UUID("50800000-0000-4000-8000-00000000000e"),
        ),
        (
            UUID("50800000-0000-4000-8000-00000000000b"),
            UUID("50800000-0000-4000-8000-00000000000f"),
        ),
    }
    assert {record.role for record in records.role_bindings} == {
        AuthorizationRole.ADMINISTRATOR
    }
    assert len(records.identities) == 5
    assert len(records.alias_bindings) == 5
    assert len(records.workspace_memberships) == 6
    assert len(records.environment_memberships) == 6
    assert len(records.role_bindings) == 5
    assert len(records.qualification_bindings) == 5
    assert sum(
        record.status.value == "revoked"
        for record in records.qualification_bindings
    ) == 1
    assert sum(
        record.expires_at < datetime(2026, 1, 1, tzinfo=timezone.utc)
        for record in records.qualification_bindings
    ) == 1
    primary_roles = [
        record
        for record in records.role_bindings
        if record.canonical_human_identity_id == settings().identity_id
    ]
    assert {(record.workspace_id, record.environment_id) for record in primary_roles} == {
        (settings().workspace_a_id, settings().environment_a_id),
        (settings().workspace_b_id, settings().environment_b_id),
    }
    change_ids = [
        record.provenance.change_id
        for group in (
            records.identities,
            records.alias_bindings,
            records.workspace_memberships,
            records.environment_memberships,
            records.role_bindings,
        )
        for record in group
    ]
    assert len(change_ids) == len(set(change_ids))


def test_context_profiles_are_stable_and_scoped_to_both_environments() -> None:
    first = build_context_profiles(settings())
    second = build_context_profiles(settings())

    assert first == second
    assert {(item.workspace_id, item.environment_id) for item in first} == {
        (
            UUID("50800000-0000-4000-8000-00000000000a"),
            UUID("50800000-0000-4000-8000-00000000000e"),
        ),
        (
            UUID("50800000-0000-4000-8000-00000000000b"),
            UUID("50800000-0000-4000-8000-00000000000f"),
        ),
    }
    assert all(item.server_approved and not item.revoked for item in first)


def test_target_binding_pins_approved_principal_and_access_policy() -> None:
    selected = settings()

    binding = build_target_binding(selected)

    assert binding.workspace_id == selected.workspace_a_id
    assert binding.environment_id == selected.environment_a_id
    assert binding.principal_id == selected.identity_id
    assert binding.target_reference == "deployment:source:fingerprint-v1"
    assert binding.access_policy_references


@pytest.mark.parametrize("field", ["workspace_b_id", "environment_b_id"])
def test_duplicate_qualification_scope_is_rejected(field: str) -> None:
    duplicate = (
        UUID("50800000-0000-4000-8000-00000000000a")
        if field == "workspace_b_id"
        else UUID("50800000-0000-4000-8000-00000000000e")
    )

    with pytest.raises(ValidationError):
        settings(**{field: duplicate})


def test_non_development_bootstrap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        settings(deployment_mode="production")


def test_qualification_workspace_slug_is_unique_for_disposable_scope() -> None:
    first = UUID("50800000-0000-4000-8000-00000000000a")
    second = UUID("50900000-0000-4000-8000-00000000000a")

    assert qualification_workspace_slug(first, "a") != qualification_workspace_slug(
        second, "a"
    )
