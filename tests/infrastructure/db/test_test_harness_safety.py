from __future__ import annotations

import pytest
from pydantic import SecretStr

from spine.infrastructure.db.test_harness import (
    CleanupPlan,
    DatabaseIdentity,
    NamespaceOwnership,
    OwnedResources,
    TestTarget as DatabaseTestTarget,
    UnsafeTestTargetError,
    database_name_from_url,
    validate_cleanup_plan,
    validate_database_identity,
    validate_disposable_marker,
    validate_target_database_name,
)


@pytest.mark.parametrize(
    "database_name",
    ["postgres", "template0", "template1", "spine", "spine_test", "spine_test-*"],
)
def test_unsafe_database_names_are_rejected(database_name: str) -> None:
    with pytest.raises(UnsafeTestTargetError):
        validate_target_database_name(database_name)


def test_non_default_prefixed_database_name_is_accepted() -> None:
    assert validate_target_database_name("spine_test_42_a1b2") == "spine_test_42_a1b2"


def test_database_name_is_extracted_without_exposing_credentials() -> None:
    url = SecretStr(
        "postgresql+psycopg://test_user:test_secret@localhost/spine_test_explicit"
    )

    assert database_name_from_url(url) == "spine_test_explicit"


def test_absent_or_incorrect_disposable_marker_is_rejected_without_leakage() -> None:
    expected = SecretStr("spine-test-harness:v1:expected-secret-marker")

    for actual in (None, "spine-test-harness:v1:wrong-secret-marker"):
        with pytest.raises(UnsafeTestTargetError) as error:
            validate_disposable_marker(expected, actual)
        rendered = str(error.value)
        assert "expected-secret-marker" not in rendered
        assert "wrong-secret-marker" not in rendered


def test_unexpected_database_identity_is_rejected() -> None:
    target = DatabaseTestTarget(
        database_name="spine_test_expected",
        disposable_marker=SecretStr("spine-test-harness:v1:marker"),
    )
    actual = DatabaseIdentity(
        database_name="spine_test_other",
        disposable_marker="spine-test-harness:v1:marker",
        postgresql_major=17,
    )

    with pytest.raises(UnsafeTestTargetError, match="identity"):
        validate_database_identity(target, actual)


def test_database_identity_representation_redacts_marker() -> None:
    identity = DatabaseIdentity(
        database_name="spine_test_expected",
        disposable_marker=SecretStr("spine-test-harness:v1:secret-marker"),
        postgresql_major=17,
    )

    assert "secret-marker" not in repr(identity)


def test_unexpected_postgresql_major_is_rejected_as_server_identity() -> None:
    target = DatabaseTestTarget(
        database_name="spine_test_expected",
        disposable_marker=SecretStr("spine-test-harness:v1:marker"),
    )
    actual = DatabaseIdentity(
        database_name="spine_test_expected",
        disposable_marker="spine-test-harness:v1:marker",
        postgresql_major=16,
    )

    with pytest.raises(UnsafeTestTargetError, match="server identity"):
        validate_database_identity(target, actual)


def test_namespace_identifiers_are_unique_per_run_and_worker() -> None:
    first = NamespaceOwnership.generate(run_id="run-a", worker_id="gw0")
    second = NamespaceOwnership.generate(run_id="run-a", worker_id="gw1")

    assert first.schema_name != second.schema_name
    assert first.role_name != second.role_name
    assert first.owns(first.schema_name)
    assert first.owns(first.role_name)
    assert not first.owns(second.schema_name)


def test_forged_namespace_ownership_is_rejected() -> None:
    with pytest.raises(UnsafeTestTargetError, match="ownership proof"):
        NamespaceOwnership(
            run_id="run_a",
            worker_id="gw0",
            nonce="owned123",
            schema_name="foreign_schema",
            role_name="foreign_role",
        )


def test_foreign_or_broad_cleanup_targets_are_rejected() -> None:
    ownership = NamespaceOwnership.generate(
        run_id="run-a", worker_id="gw0", nonce="owned123"
    )
    resources = OwnedResources(
        schemas=frozenset({ownership.schema_name}),
        roles=frozenset({ownership.role_name}),
    )

    unsafe_plans = (
        CleanupPlan(schemas=("spine_test_schema_%",), roles=()),
        CleanupPlan(schemas=("foreign_schema",), roles=()),
        CleanupPlan(schemas=(), roles=("spine_test_role_*",)),
        CleanupPlan(schemas=(), roles=("foreign_role",)),
    )

    for plan in unsafe_plans:
        with pytest.raises(UnsafeTestTargetError):
            validate_cleanup_plan(plan, ownership, resources)


def test_cleanup_accepts_only_exact_recorded_owned_identifiers() -> None:
    ownership = NamespaceOwnership.generate(
        run_id="run-a", worker_id="gw0", nonce="owned123"
    )
    resources = OwnedResources(
        schemas=frozenset({ownership.schema_name}),
        roles=frozenset({ownership.role_name}),
    )
    plan = CleanupPlan(
        schemas=(ownership.schema_name,),
        roles=(ownership.role_name,),
    )

    assert validate_cleanup_plan(plan, ownership, resources) == plan


def test_unrecorded_owned_identifier_is_rejected() -> None:
    ownership = NamespaceOwnership.generate(
        run_id="run-a", worker_id="gw0", nonce="owned123"
    )

    with pytest.raises(UnsafeTestTargetError, match="recorded"):
        validate_cleanup_plan(
            CleanupPlan(schemas=(ownership.schema_name,), roles=()),
            ownership,
            OwnedResources(),
        )
