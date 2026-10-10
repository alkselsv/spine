from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    DuplicateRoutePolicyError,
    InvalidRoutePolicyError,
    MissingRoutePolicyError,
    RouteAuthorizationPolicy,
    RoutePolicyRegistry,
)


def policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("diagnostic_question"),
        service_principal_id=UUID("10000000-0000-0000-0000-000000000001"),
    )


def registered_policy() -> RoutePolicyRegistry:
    registry = RoutePolicyRegistry()
    registry.register("knowledge.ask", policy())
    return registry


def test_route_policy_registry_resolves_registered_policy() -> None:
    registry = registered_policy()

    assert registry.require("knowledge.ask") == policy()


def test_route_policy_registry_detaches_registered_input() -> None:
    registry = RoutePolicyRegistry()
    source = policy()
    registry.register("knowledge.ask", source)

    object.__setattr__(source.purpose, "value", "caller_substitution")

    assert registry.require("knowledge.ask").purpose.value == "knowledge_control"


def test_route_policy_registry_detaches_resolved_output() -> None:
    registry = registered_policy()
    resolved = registry.require("knowledge.ask")

    object.__setattr__(resolved.operation, "value", "caller_substitution")

    assert registry.require("knowledge.ask").operation.value == "diagnostic_question"


def test_route_policy_registry_rejects_duplicate_route() -> None:
    registry = registered_policy()

    with pytest.raises(DuplicateRoutePolicyError, match="already registered"):
        registry.register("knowledge.ask", policy())


def test_route_policy_registry_rejects_unknown_route() -> None:
    with pytest.raises(MissingRoutePolicyError, match="not registered"):
        RoutePolicyRegistry().require("knowledge.ingest")


def test_route_policy_registry_rejects_missing_policy() -> None:
    with pytest.raises(MissingRoutePolicyError, match="requires a policy"):
        RoutePolicyRegistry().register("knowledge.ingest", None)


def test_route_policy_registry_revalidates_policy_at_registration() -> None:
    malformed = RouteAuthorizationPolicy.model_construct(
        required_roles=frozenset(),
        required_scope="workspace",
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("diagnostic_question"),
        service_principal_id=None,
    )

    with pytest.raises(InvalidRoutePolicyError, match="invalid"):
        RoutePolicyRegistry().register("knowledge.ingest", malformed)
