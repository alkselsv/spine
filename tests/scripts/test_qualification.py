from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.auth import AuthorizationRole
from spine.scripts.qualification import (
    QualificationBootstrapSettings,
    build_authorization_records,
    build_context_profiles,
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
    }
    values.update(overrides)
    return QualificationBootstrapSettings(**values)


def test_authority_records_bind_each_environment_to_exactly_one_workspace() -> None:
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
    assert len(records.workspace_memberships) == 2
    assert len(records.environment_memberships) == 2
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
