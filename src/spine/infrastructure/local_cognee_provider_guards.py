"""Explicit development-only provider authorization for Cognee qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, TypeVar
from uuid import UUID

from pydantic import Field, SecretStr, TypeAdapter, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from spine.domain.common import DefinitionModel
from spine.memory.cognee.projection_profile_binding import (
    BudgetAuthorizationResolver,
    CogneeProviderCallExecutor,
)
from spine.memory.cognee.worker_profile import (
    CogneeWorkerProfile,
    RunBudgetAuthorization,
    SecretReferenceState,
)
from spine.memory.contracts import ProjectionCommand


T = TypeVar("T")


class LocalCogneeProviderAuthorization(DefinitionModel):
    """Operator-approved, scope-bound authorization for one local qualification."""

    schema_version: Literal["cognee-local-provider-authorization:v1"]
    deployment_mode: Literal["development"]
    authorization_reference: str = Field(min_length=1, max_length=256)
    workspace_id: UUID
    environment_id: UUID
    profile_id: str = Field(min_length=1, max_length=64)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    amount: Decimal = Field(gt=Decimal("0"), max_digits=18, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime
    secret_references: tuple[str, ...] = Field(min_length=1)

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_amount_only(cls, value: object) -> object:
        if not isinstance(value, (Decimal, str)):
            raise ValueError("budget amount must be decimal text")
        return value

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorization expiry must be timezone-aware")
        return value


class LocalCogneeProviderAuthorizationSet(DefinitionModel):
    """Explicit local authorizations for several isolated qualification scopes."""

    schema_version: Literal["cognee-local-provider-authorization-set:v1"]
    deployment_mode: Literal["development"]
    authorizations: tuple[LocalCogneeProviderAuthorization, ...] = Field(min_length=1)


LocalCogneeProviderAuthorizationDocument = (
    LocalCogneeProviderAuthorization | LocalCogneeProviderAuthorizationSet
)


class LocalCogneeProviderAuthorizationError(RuntimeError):
    """Bounded startup failure for invalid local operator authorization."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class LocalCogneeProviderAuthorizationSettings(BaseSettings):
    """Load an optional local authorization document without implicit approval."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        case_sensitive=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    authorization_json: str | None = Field(
        default=None,
        repr=False,
        validation_alias="SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON",
    )

    def load_optional(self) -> LocalCogneeProviderAuthorizationDocument | None:
        if not self.authorization_json:
            return None
        try:
            return TypeAdapter(LocalCogneeProviderAuthorizationDocument).validate_json(
                self.authorization_json
            )
        except (ValidationError, ValueError):
            raise LocalCogneeProviderAuthorizationError(
                "authorization_invalid"
            ) from None


class LocalCogneeProviderSecretPresenceSettings(BaseSettings):
    """Map deployment secret presence to approved profile references.

    Values remain ``SecretStr`` and never leave this settings boundary. Cognee's
    local profile permits ``LLM_API_KEY`` to back both LLM and embedding calls;
    an embedding-only key cannot satisfy the shared model secret reference.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        case_sensitive=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    postgres_password: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias="DB_PASSWORD",
    )
    graph_database_password: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias="GRAPH_DATABASE_PASSWORD",
    )
    llm_api_key: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias="LLM_API_KEY",
    )
    embedding_api_key: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias="EMBEDDING_API_KEY",
    )

    def available_references(
        self, profile: CogneeWorkerProfile
    ) -> frozenset[str]:
        references: set[str] = set()
        if self.postgres_password is not None:
            references.add(profile.postgres_secret_ref)
        if self.graph_database_password is not None:
            references.add(profile.neo4j_secret_ref)
        if self.llm_api_key is not None:
            references.add(profile.model_secret_ref)
        return frozenset(references)


@dataclass(frozen=True, slots=True)
class _LocalSecretReferenceResolver:
    approved: frozenset[str]
    available: frozenset[str]

    async def check(self, reference: str) -> SecretReferenceState:
        if reference not in self.approved:
            return SecretReferenceState.UNAPPROVED
        if reference not in self.available:
            return SecretReferenceState.MISSING
        return SecretReferenceState.VALID


@dataclass(frozen=True, slots=True)
class _LocalRunBudgetAuthorizationResolver:
    profile: CogneeWorkerProfile
    authorizations: tuple[LocalCogneeProviderAuthorization, ...]

    async def __call__(
        self, command: ProjectionCommand
    ) -> RunBudgetAuthorization | None:
        authorization = next(
            (
                item
                for item in self.authorizations
                if item.workspace_id == command.workspace_id
                and item.environment_id == command.environment_id
            ),
            None,
        )
        if authorization is None:
            return None
        return RunBudgetAuthorization(
            run_id=str(command.projection_run_id),
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            authorization_reference=authorization.authorization_reference,
            profile_id=self.profile.profile_id,
            profile_digest=self.profile.profile_digest,
            approved=True,
            amount=authorization.amount,
            currency=authorization.currency,
            expires_at=authorization.expires_at,
        )


class _LocalBoundedProviderCallExecutor:
    def __init__(self, profile: CogneeWorkerProfile) -> None:
        self._profile_digest = profile.profile_digest
        self._deadline_seconds = profile.limits.run_deadline_seconds
        self._semaphore = asyncio.Semaphore(profile.limits.max_concurrency)

    async def invoke(
        self,
        *,
        profile: CogneeWorkerProfile,
        command: ProjectionCommand,
        provider_call: Callable[[], Awaitable[T]],
    ) -> T:
        del command
        if profile.profile_digest != self._profile_digest:
            raise LocalCogneeProviderAuthorizationError("profile_mismatch")
        async with self._semaphore:
            return await asyncio.wait_for(
                provider_call(), timeout=self._deadline_seconds
            )


@dataclass(frozen=True, slots=True)
class LocalCogneeProviderGuards:
    secret_resolver: _LocalSecretReferenceResolver
    budget_authorization_resolver: BudgetAuthorizationResolver
    provider_call_executor: CogneeProviderCallExecutor


def build_local_cognee_provider_guards(
    *,
    profile: CogneeWorkerProfile,
    authorization: LocalCogneeProviderAuthorizationDocument,
    available_secret_references: frozenset[str],
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LocalCogneeProviderGuards:
    """Validate one explicit local authorization and return all provider guards."""

    required_references = frozenset(
        (
            profile.postgres_secret_ref,
            profile.neo4j_secret_ref,
            profile.model_secret_ref,
        )
    )
    authorizations = (
        authorization.authorizations
        if isinstance(authorization, LocalCogneeProviderAuthorizationSet)
        else (authorization,)
    )
    scopes = {(item.workspace_id, item.environment_id) for item in authorizations}
    if len(scopes) != len(authorizations):
        raise LocalCogneeProviderAuthorizationError("authorization_scope_conflict")
    for item in authorizations:
        approved_references = frozenset(item.secret_references)
        if (
            item.profile_id != profile.profile_id
            or item.profile_digest != profile.profile_digest
        ):
            raise LocalCogneeProviderAuthorizationError("profile_mismatch")
        if item.currency != profile.limits.budget_currency:
            raise LocalCogneeProviderAuthorizationError("budget_currency_mismatch")
        if item.amount > profile.limits.max_budget_amount:
            raise LocalCogneeProviderAuthorizationError("budget_over_limit")
        if item.expires_at <= clock():
            raise LocalCogneeProviderAuthorizationError("authorization_expired")
        if approved_references != required_references:
            raise LocalCogneeProviderAuthorizationError("secret_reference_set_mismatch")

    return LocalCogneeProviderGuards(
        secret_resolver=_LocalSecretReferenceResolver(
            approved=required_references,
            available=available_secret_references,
        ),
        budget_authorization_resolver=_LocalRunBudgetAuthorizationResolver(
            profile=profile,
            authorizations=authorizations,
        ),
        provider_call_executor=_LocalBoundedProviderCallExecutor(profile),
    )


__all__ = [
    "LocalCogneeProviderAuthorization",
    "LocalCogneeProviderAuthorizationDocument",
    "LocalCogneeProviderAuthorizationError",
    "LocalCogneeProviderAuthorizationSet",
    "LocalCogneeProviderAuthorizationSettings",
    "LocalCogneeProviderSecretPresenceSettings",
    "LocalCogneeProviderGuards",
    "build_local_cognee_provider_guards",
]
