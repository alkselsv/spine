from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthenticationAlias,
    AuthenticatedAlias,
    AuthorizationResolution,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
)
from spine.domain.common import DefinitionModel


SERVICE_PRINCIPAL_ID = UUID("10000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
IDENTITY_ID = UUID("40000000-0000-0000-0000-000000000001")


class FakeDiagnosticContext(DefinitionModel):
    trace_id: UUID


def alias() -> AuthenticationAlias:
    return AuthenticationAlias(
        issuer="https://identity.example.test",
        subject="provider-subject-123",
    )


def requested_scope() -> RequestedAuthorizationScope:
    return RequestedAuthorizationScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )


def route_policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("list_sources"),
        service_principal_id=SERVICE_PRINCIPAL_ID,
    )


def test_authentication_alias_contains_exact_provider_identity_only() -> None:
    assert alias().model_dump() == {
        "issuer": "https://identity.example.test",
        "subject": "provider-subject-123",
    }


def test_authentication_alias_is_immutable() -> None:
    value = alias()

    with pytest.raises(ValidationError):
        value.subject = "replacement"  # type: ignore[misc]


def test_authentication_alias_rejects_authority_fields() -> None:
    with pytest.raises(ValidationError):
        AuthenticationAlias(
            issuer="https://identity.example.test",
            subject="provider-subject-123",
            roles=["administrator"],
        )


def test_authentication_alias_preserves_exact_subject() -> None:
    exact_subject = "Case Sensitive Subject"

    assert AuthenticationAlias(
        issuer="https://identity.example.test",
        subject=exact_subject,
    ).subject == exact_subject


def test_route_policy_accepts_exact_r1_role() -> None:
    assert route_policy().required_roles == frozenset({AuthorizationRole.ADMINISTRATOR})


@pytest.mark.parametrize(
    "roles",
    [frozenset(), frozenset({"owner"})],
    ids=["empty", "unknown"],
)
def test_route_policy_rejects_non_administrator_role_vocabulary(
    roles: frozenset[object],
) -> None:
    with pytest.raises(ValidationError):
        RouteAuthorizationPolicy(
            required_roles=roles,
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("list_sources"),
        )


def test_route_policy_rejects_non_environment_scope() -> None:
    with pytest.raises(ValidationError):
        RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope="workspace",
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("list_sources"),
        )


def test_requested_scope_rejects_nil_environment() -> None:
    with pytest.raises(ValidationError):
        RequestedAuthorizationScope(
            workspace_id=WORKSPACE_ID,
            environment_id=UUID(int=0),
        )


def test_requested_scope_rejects_caller_authority_fields() -> None:
    with pytest.raises(ValidationError):
        RequestedAuthorizationScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            roles=["administrator"],
            purpose="knowledge_control",
            operation="list_sources",
            service_principal_id=SERVICE_PRINCIPAL_ID,
        )


def test_authenticated_alias_rejects_token_claims() -> None:
    with pytest.raises(ValidationError):
        AuthenticatedAlias(
            alias=alias(),
            authentication_configuration_version="oidc-2026-10-10",
            claims={"groups": ["administrator"]},
        )


def test_authorization_resolution_rejects_non_current_generation() -> None:
    with pytest.raises(ValidationError):
        AuthorizationResolution(
            acting_subject_id=IDENTITY_ID,
            scope=requested_scope(),
            roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            authorization_generation=0,
            authentication_configuration_version="oidc-2026-10-10",
        )


def test_authorized_request_context_has_closed_shape() -> None:
    property_names = {
        name
        for name, value in vars(AuthorizedRequestContext).items()
        if isinstance(value, property)
    }

    assert AuthorizedRequestContext.__slots__ == ()
    assert property_names == {
        "acting_subject_id",
        "scope",
        "roles",
        "authorization_generation",
        "authentication_configuration_version",
        "route_policy",
        "diagnostic_context",
    }


def test_authorized_request_context_is_immutable() -> None:
    forged = object.__new__(AuthorizedRequestContext)

    with pytest.raises(AttributeError):
        object.__setattr__(forged, "authorization_generation", 8)


def test_caller_data_cannot_construct_authorized_request_context() -> None:
    with pytest.raises(TypeError, match="trusted issuance"):
        AuthorizedRequestContext[FakeDiagnosticContext](
            acting_subject_id=IDENTITY_ID,
            scope=requested_scope(),
            roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            authorization_generation=7,
            authentication_configuration_version="oidc-2026-10-10",
            route_policy=route_policy(),
            diagnostic_context=FakeDiagnosticContext(
                trace_id=UUID("50000000-0000-0000-0000-000000000001")
            ),
        )


def test_caller_cannot_substitute_authorized_context_implementation() -> None:
    with pytest.raises(TypeError, match="implementations are sealed"):

        class ForgedAuthorizedRequestContext(
            AuthorizedRequestContext[FakeDiagnosticContext]
        ):
            pass
