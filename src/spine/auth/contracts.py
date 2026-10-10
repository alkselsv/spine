"""Framework-independent identity and request-authorization contracts."""

from __future__ import annotations

from enum import Enum
import re
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator

from spine.application.persistence.context import PersistenceOperation, PersistencePurpose
from spine.domain.common import DefinitionModel


_VERSION_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
DiagnosticContextT = TypeVar("DiagnosticContextT")


def _require_non_nil_uuid(value: UUID, *, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-nil UUID")
    return value


def _require_configuration_version(value: str) -> str:
    if _VERSION_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("authentication configuration version is invalid")
    return value


class AuthenticationAlias(DefinitionModel):
    """Exact provider-scoped identity; this value grants no authority."""

    issuer: str = Field(min_length=1, max_length=2048)
    subject: str = Field(min_length=1, max_length=512)

    @field_validator("issuer", "subject")
    @classmethod
    def _reject_ambiguous_identity_text(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("identity values must not contain control characters")
        return value


class AuthenticatedAlias(DefinitionModel):
    """Provider-neutral result of successful credential authentication."""

    alias: AuthenticationAlias
    authentication_configuration_version: str

    @field_validator("authentication_configuration_version")
    @classmethod
    def _validate_configuration_version(cls, value: str) -> str:
        return _require_configuration_version(value)


class AccessTokenCredential(DefinitionModel):
    """Opaque access-token credential crossing the authentication seam."""

    access_token: SecretStr = Field(repr=False)

    @field_validator("access_token")
    @classmethod
    def _bound_access_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not token or len(token) > 16_384:
            raise ValueError("access token has an invalid size")
        return value


class AuthorizationRole(str, Enum):
    """Closed R1 role vocabulary."""

    ADMINISTRATOR = "administrator"


class AuthorizationScopeRequirement(str, Enum):
    """Scope a protected route must prove before application execution."""

    WORKSPACE_ENVIRONMENT = "workspace_environment"


class RequestedAuthorizationScope(DefinitionModel):
    """Caller-requested selector that grants no authority by itself."""

    workspace_id: UUID
    environment_id: UUID

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def _reject_nil_scope_id(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _require_non_nil_uuid(value, field_name=info.field_name)


class RouteAuthorizationPolicy(DefinitionModel):
    """Static, server-owned authorization policy for one protected route."""

    required_roles: frozenset[AuthorizationRole]
    required_scope: AuthorizationScopeRequirement
    purpose: PersistencePurpose
    operation: PersistenceOperation
    service_principal_id: UUID | None = None

    @field_validator("service_principal_id")
    @classmethod
    def _reject_nil_service_principal(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("service_principal_id must be a non-nil UUID")
        return value

    @model_validator(mode="after")
    def _require_exact_r1_role(self) -> "RouteAuthorizationPolicy":
        if self.required_roles != frozenset({AuthorizationRole.ADMINISTRATOR}):
            raise ValueError("protected R1 routes require the administrator role")
        return self


def _snapshot_route_authorization_policy(
    policy: RouteAuthorizationPolicy,
) -> RouteAuthorizationPolicy:
    """Revalidate a detached copy for internal auth adapters and registries."""

    if type(policy) is not RouteAuthorizationPolicy:
        raise TypeError("route policy must use the canonical contract")
    return RouteAuthorizationPolicy(
        required_roles=policy.required_roles,
        required_scope=policy.required_scope,
        purpose=PersistencePurpose(policy.purpose.value),
        operation=PersistenceOperation(policy.operation.value),
        service_principal_id=policy.service_principal_id,
    )


class AuthorizationResolution(DefinitionModel):
    """One current directory snapshot for an allowed request."""

    acting_subject_id: UUID
    scope: RequestedAuthorizationScope
    roles: frozenset[AuthorizationRole]
    authorization_generation: int = Field(ge=1)
    authentication_configuration_version: str

    @field_validator("acting_subject_id")
    @classmethod
    def _reject_nil_actor(cls, value: UUID) -> UUID:
        return _require_non_nil_uuid(value, field_name="acting_subject_id")

    @field_validator("authentication_configuration_version")
    @classmethod
    def _validate_configuration_version(cls, value: str) -> str:
        return _require_configuration_version(value)

    @model_validator(mode="after")
    def _require_exact_r1_role(self) -> "AuthorizationResolution":
        if self.roles != frozenset({AuthorizationRole.ADMINISTRATOR}):
            raise ValueError("authorization resolution requires the administrator role")
        return self


class AuthorizedRequestContext(Generic[DiagnosticContextT]):
    """Sealed read-only shape reserved for trusted composition in ticket #72.

    The contract deliberately has no valid implementation or issuance path in
    this ticket. This keeps caller data from minting authority while allowing
    downstream interfaces to depend on one stable field vocabulary. Ticket #72
    introduces the composition-root-owned implementation, provenance and
    verifier without changing these read-only properties.
    """

    __slots__ = ()

    def __new__(cls, *args: object, **kwargs: object) -> "AuthorizedRequestContext":
        del args, kwargs
        raise TypeError("Authorized request contexts require trusted issuance.")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("Authorized request context implementations are sealed.")

    @property
    def acting_subject_id(self) -> UUID:
        raise NotImplementedError

    @property
    def scope(self) -> RequestedAuthorizationScope:
        raise NotImplementedError

    @property
    def roles(self) -> frozenset[AuthorizationRole]:
        raise NotImplementedError

    @property
    def authorization_generation(self) -> int:
        raise NotImplementedError

    @property
    def authentication_configuration_version(self) -> str:
        raise NotImplementedError

    @property
    def route_policy(self) -> RouteAuthorizationPolicy:
        raise NotImplementedError

    @property
    def diagnostic_context(self) -> DiagnosticContextT:
        raise NotImplementedError


__all__ = [
    "AccessTokenCredential",
    "AuthenticationAlias",
    "AuthenticatedAlias",
    "AuthorizationResolution",
    "AuthorizationRole",
    "AuthorizationScopeRequirement",
    "AuthorizedRequestContext",
    "RequestedAuthorizationScope",
    "RouteAuthorizationPolicy",
]
