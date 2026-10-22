"""Strict, non-secret Cognee worker profile and invocation guard contracts.

This module validates configuration and gates a caller-supplied operation. It
does not configure Cognee, create provider clients, or compose production routes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
import logging
import math
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from spine.domain.common import DefinitionModel


COGNEE_WORKER_PROFILE_SCHEMA = "cognee-worker-profile:v1"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_PROFILE_ID_PATTERN = r"^[a-z][a-z0-9-]{1,63}$"
_REFERENCE_PATTERN = r"^secretref://[a-z0-9][a-z0-9._/-]{0,190}$"
_CURRENCY_PATTERN = r"^[A-Z]{3}$"
_logger = logging.getLogger(__name__)


class CogneeWorkerLimits(DefinitionModel):
    """Required provider bounds; no operational or spend limits are inferred."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_requests_per_run: StrictInt = Field(gt=0, le=100_000)
    max_concurrency: StrictInt = Field(gt=0, le=1_000)
    max_input_tokens: StrictInt = Field(gt=0, le=10_000_000)
    max_output_tokens: StrictInt = Field(gt=0, le=1_000_000)
    request_timeout_seconds: StrictInt = Field(gt=0, le=86_400)
    run_deadline_seconds: StrictInt = Field(gt=0, le=604_800)
    max_retries: StrictInt = Field(ge=0, le=20)
    max_budget_amount: Decimal = Field(gt=Decimal("0"), max_digits=18, decimal_places=6)
    budget_currency: str = Field(pattern=_CURRENCY_PATTERN)

    @model_validator(mode="after")
    def timeout_fits_run_deadline(self) -> "CogneeWorkerLimits":
        if self.request_timeout_seconds > self.run_deadline_seconds:
            raise ValueError("request timeout exceeds run deadline")
        return self

    @field_validator("max_budget_amount", mode="before")
    @classmethod
    def decimal_budget_only(cls, value: object) -> object:
        if not isinstance(value, (Decimal, str)):
            raise ValueError("budget limit must be supplied as Decimal or decimal text")
        return value


class CogneeWorkerProfile(DefinitionModel):
    """Approved worker configuration containing identities and secret refs only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[COGNEE_WORKER_PROFILE_SCHEMA]
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    deployment_id: str = Field(min_length=1, max_length=128)
    release_id: str = Field(min_length=1, max_length=128)
    approval_reference: str = Field(min_length=1, max_length=256)

    cognee_version: Literal["1.5.4"]
    postgres_server_major: Literal["17"]
    neo4j_apoc_server_version: Literal["5.26.32"]
    pgvector_extension_version: Literal["0.8.7"]
    neo4j_python_version: Literal["5.28.6"]
    pgvector_python_version: Literal["0.3.6"]
    llm_model: Literal["openai/gpt-5.4-nano"]
    llm_revision_required: bool
    llm_revision: str | None
    embedding_model: Literal["openai/text-embedding-3-small"]
    embedding_revision_required: bool
    embedding_revision: str | None
    generation_parameters_required: bool
    generation_parameters: tuple["GenerationParameter", ...]

    postgres_secret_ref: str = Field(pattern=_REFERENCE_PATTERN)
    neo4j_secret_ref: str = Field(pattern=_REFERENCE_PATTERN)
    model_secret_ref: str = Field(pattern=_REFERENCE_PATTERN)
    limits: CogneeWorkerLimits

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != COGNEE_WORKER_PROFILE_SCHEMA:
            raise ValueError("unsupported Cognee worker profile schema")
        return value

    @field_validator("approval_reference", "deployment_id", "release_id")
    @classmethod
    def bounded_nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("profile identity fields must be nonblank and trimmed")
        return value

    @field_validator("postgres_secret_ref", "neo4j_secret_ref", "model_secret_ref")
    @classmethod
    def validate_secret_reference(cls, value: str) -> str:
        path = value.removeprefix("secretref://").split("/")
        if (
            not value.startswith("secretref://")
            or len(path) < 2
            or any(part in ("", ".", "..") for part in path)
        ):
            raise ValueError("configuration accepts well-formed references only")
        return value

    @field_validator("generation_parameters")
    @classmethod
    def generation_parameters_are_unique(
        cls, value: tuple["GenerationParameter", ...]
    ) -> tuple["GenerationParameter", ...]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("generation parameter names must be unique")
        return value

    @model_validator(mode="after")
    def digest_matches_profile(self) -> "CogneeWorkerProfile":
        if self.llm_revision_required and not self.llm_revision:
            raise ValueError("required LLM revision is missing")
        if self.embedding_revision_required and not self.embedding_revision:
            raise ValueError("required embedding revision is missing")
        if self.generation_parameters_required and not self.generation_parameters:
            raise ValueError("required generation parameters are missing")
        expected = self.digest_for(
            self.model_dump(mode="json", exclude={"profile_digest"})
        )
        if self.profile_digest != expected:
            raise ValueError("Cognee worker profile digest mismatch")
        return self

    @staticmethod
    def digest_for(values: Mapping[str, object]) -> str:
        """Calculate the canonical profile digest, excluding its digest field."""

        normalized = dict(values)
        normalized.pop("profile_digest", None)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return sha256(payload).hexdigest()


class GenerationParameter(DefinitionModel):
    """One explicitly selected scalar model setting with no inferred values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value: str | int | float | bool

    @field_validator("value")
    @classmethod
    def finite_numeric_value(cls, value: str | int | float | bool) -> str | int | float | bool:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("generation parameter number must be finite")
        return value


class RuntimeIdentity(DefinitionModel):
    """Independently observed server, Python-client and model identities."""

    cognee_version: str
    postgres_server_major: str
    neo4j_apoc_server_version: str
    pgvector_extension_version: str
    neo4j_python_version: str
    pgvector_python_version: str
    llm_model: str
    llm_revision: str | None
    embedding_model: str
    embedding_revision: str | None


class ProfileApproval(DefinitionModel):
    """Independent approval binding for one profile and deployment release."""

    profile_id: str
    profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    approval_reference: str = Field(min_length=1, max_length=256)
    deployment_id: str = Field(min_length=1, max_length=128)
    release_id: str = Field(min_length=1, max_length=128)
    approved: bool


class SecretReferenceState(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    MALFORMED = "malformed"
    UNAPPROVED = "unapproved"
    EXPIRED = "expired"
    REVOKED = "revoked"


class SecretReferenceResolver(Protocol):
    """Check a reference without returning or exposing its secret value."""

    async def check(self, reference: str) -> SecretReferenceState: ...


class RunBudgetAuthorization(DefinitionModel):
    """Run-scoped spend authorization with an explicit amount and expiry."""

    run_id: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    environment_id: UUID
    authorization_reference: str = Field(min_length=1, max_length=256)
    profile_id: str = Field(min_length=1, max_length=64)
    profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    approved: bool
    amount: Decimal = Field(gt=Decimal("0"), max_digits=18, decimal_places=6)
    currency: str = Field(pattern=_CURRENCY_PATTERN)
    expires_at: datetime

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_amount_only(cls, value: object) -> object:
        if not isinstance(value, (Decimal, str)):
            raise ValueError("budget amount must be supplied as Decimal or decimal text")
        return value

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("budget authorization expiry must be timezone-aware")
        return value


class InvocationRefusal(DefinitionModel):
    """Bounded diagnostic suitable for logs and health/readiness responses."""

    code: str = Field(pattern=r"^[a-z_]{1,48}$")


class InvocationDecision(DefinitionModel):
    """Safe result of one provider-call authorization attempt."""

    allowed: bool
    refusals: tuple[InvocationRefusal, ...] = ()


class WorkerProfileLoadStatus(StrEnum):
    LOADED = "loaded"
    INVALID = "invalid"


class WorkerProfileLoadResult(DefinitionModel):
    """Safe profile-load outcome that omits Pydantic input/error details."""

    status: WorkerProfileLoadStatus
    profile: CogneeWorkerProfile | None
    validation_error_count: StrictInt = Field(ge=0, le=99)


def load_worker_profile(values: Mapping[str, object]) -> WorkerProfileLoadResult:
    """Validate untrusted settings without returning their values in diagnostics."""

    try:
        profile = CogneeWorkerProfile.model_validate(values)
    except ValidationError as error:
        return WorkerProfileLoadResult(
            status=WorkerProfileLoadStatus.INVALID,
            profile=None,
            validation_error_count=min(len(error.errors()), 99),
        )
    return WorkerProfileLoadResult(
        status=WorkerProfileLoadStatus.LOADED,
        profile=profile,
        validation_error_count=0,
    )


class ProfileConfigurationStatus(StrEnum):
    PROFILE_MISSING = "profile_missing"
    APPROVAL_MISSING = "approval_missing"
    PROFILE_UNAPPROVED = "profile_unapproved"
    APPROVAL_MISMATCH = "profile_approval_mismatch"
    DEPLOYMENT_MISMATCH = "deployment_mismatch"
    RELEASE_MISMATCH = "release_mismatch"
    VALID = "profile_binding_valid"


def profile_configuration_status(
    *,
    profile: CogneeWorkerProfile | None,
    approval: ProfileApproval | None,
    deployment_id: str,
    release_id: str,
) -> ProfileConfigurationStatus:
    """Return bounded local config status without resolving secrets or calling providers."""
    binding_refusals = _profile_binding_refusal_codes(
        profile=profile,
        approval=approval,
        deployment_id=deployment_id,
        release_id=release_id,
    )
    if not binding_refusals:
        return ProfileConfigurationStatus.VALID
    return ProfileConfigurationStatus(binding_refusals[0])


T = TypeVar("T")


async def guarded_provider_call(
    *,
    profile: CogneeWorkerProfile | None,
    approval: ProfileApproval | None,
    runtime: RuntimeIdentity | None,
    deployment_id: str,
    release_id: str,
    secret_resolver: SecretReferenceResolver | None,
    budget_authorization: RunBudgetAuthorization | None,
    run_id: str,
    workspace_id: UUID,
    environment_id: UUID,
    now: datetime,
    provider_call: Callable[[], Awaitable[T]],
) -> tuple[InvocationDecision, T | None]:
    """Invoke the supplied provider operation only after every guard passes."""

    decision = await check_invocation_eligibility(
        profile=profile,
        approval=approval,
        runtime=runtime,
        deployment_id=deployment_id,
        release_id=release_id,
        secret_resolver=secret_resolver,
        budget_authorization=budget_authorization,
        run_id=run_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        now=now,
    )
    if not decision.allowed:
        _logger.warning(
            "Cognee provider invocation refused: codes=%s",
            ",".join(item.code for item in decision.refusals),
        )
        return decision, None
    try:
        return decision, await provider_call()
    except Exception:
        _logger.warning("Cognee provider operation failed: code=provider_call_failed")
        return (
            InvocationDecision(
                allowed=False,
                refusals=(InvocationRefusal(code="provider_call_failed"),),
            ),
            None,
        )


async def check_invocation_eligibility(
    *,
    profile: CogneeWorkerProfile | None,
    approval: ProfileApproval | None,
    runtime: RuntimeIdentity | None,
    deployment_id: str,
    release_id: str,
    secret_resolver: SecretReferenceResolver | None,
    budget_authorization: RunBudgetAuthorization | None,
    run_id: str,
    workspace_id: UUID,
    environment_id: UUID,
    now: datetime,
) -> InvocationDecision:
    """Return bounded refusal codes; never include config, secret or exception data."""

    refusals: list[InvocationRefusal] = [
        InvocationRefusal(code=code)
        for code in _profile_binding_refusal_codes(
            profile=profile,
            approval=approval,
            deployment_id=deployment_id,
            release_id=release_id,
        )
    ]
    if profile is None:
        return InvocationDecision(allowed=False, refusals=tuple(refusals))

    if runtime is None:
        refusals.append(InvocationRefusal(code="runtime_identity_missing"))
    elif not _runtime_matches(profile, runtime):
        refusals.append(InvocationRefusal(code="runtime_identity_mismatch"))

    if budget_authorization is None:
        refusals.append(InvocationRefusal(code="run_budget_missing"))
    else:
        if budget_authorization.run_id != run_id:
            refusals.append(InvocationRefusal(code="run_budget_run_mismatch"))
        if not budget_authorization.approved:
            refusals.append(InvocationRefusal(code="run_budget_unapproved"))
        if (
            budget_authorization.profile_id != profile.profile_id
            or budget_authorization.profile_digest != profile.profile_digest
        ):
            refusals.append(InvocationRefusal(code="run_budget_profile_mismatch"))
        if (
            budget_authorization.workspace_id != workspace_id
            or budget_authorization.environment_id != environment_id
        ):
            refusals.append(InvocationRefusal(code="run_budget_scope_mismatch"))
        if budget_authorization.currency != profile.limits.budget_currency:
            refusals.append(InvocationRefusal(code="run_budget_currency_mismatch"))
        if budget_authorization.amount > profile.limits.max_budget_amount:
            refusals.append(InvocationRefusal(code="run_budget_over_limit"))
        if budget_authorization.expires_at <= _utc(now):
            refusals.append(InvocationRefusal(code="run_budget_expired"))

    if secret_resolver is None:
        refusals.append(InvocationRefusal(code="secret_resolver_unavailable"))
    elif not refusals:
        for reference in (
            profile.postgres_secret_ref,
            profile.neo4j_secret_ref,
            profile.model_secret_ref,
        ):
            try:
                state = await secret_resolver.check(reference)
            except Exception:
                state = None
            if state is not SecretReferenceState.VALID:
                refusals.append(
                    InvocationRefusal(
                        code=(
                            "secret_reference_unavailable"
                            if state in (SecretReferenceState.MISSING, None)
                            else "secret_reference_invalid"
                        )
                    )
                )
                break

    safe_refusals = tuple(dict.fromkeys(refusals))
    return InvocationDecision(allowed=not safe_refusals, refusals=safe_refusals)


def _profile_binding_refusal_codes(
    *,
    profile: CogneeWorkerProfile | None,
    approval: ProfileApproval | None,
    deployment_id: str,
    release_id: str,
) -> tuple[str, ...]:
    """Validate the common profile, approval, deployment and release binding."""

    if profile is None:
        return ("profile_missing",)
    refusals: list[str] = []
    if approval is None:
        refusals.append("approval_missing")
    else:
        if not approval.approved:
            refusals.append("profile_unapproved")
        if (
            approval.profile_id != profile.profile_id
            or approval.profile_digest != profile.profile_digest
            or approval.approval_reference != profile.approval_reference
        ):
            refusals.append("profile_approval_mismatch")
    if deployment_id != profile.deployment_id or (
        approval is not None and approval.deployment_id != profile.deployment_id
    ):
        refusals.append("deployment_mismatch")
    if release_id != profile.release_id or (
        approval is not None and approval.release_id != profile.release_id
    ):
        refusals.append("release_mismatch")
    return tuple(refusals)


def _runtime_matches(profile: CogneeWorkerProfile, runtime: RuntimeIdentity) -> bool:
    identity_fields = (
        "cognee_version",
        "postgres_server_major",
        "neo4j_apoc_server_version",
        "pgvector_extension_version",
        "neo4j_python_version",
        "pgvector_python_version",
        "llm_model",
        "llm_revision",
        "embedding_model",
        "embedding_revision",
    )
    return all(getattr(profile, field) == getattr(runtime, field) for field in identity_fields)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "COGNEE_WORKER_PROFILE_SCHEMA",
    "CogneeWorkerLimits",
    "CogneeWorkerProfile",
    "GenerationParameter",
    "InvocationDecision",
    "InvocationRefusal",
    "ProfileConfigurationStatus",
    "ProfileApproval",
    "RunBudgetAuthorization",
    "RuntimeIdentity",
    "SecretReferenceResolver",
    "SecretReferenceState",
    "WorkerProfileLoadResult",
    "WorkerProfileLoadStatus",
    "check_invocation_eligibility",
    "guarded_provider_call",
    "load_worker_profile",
    "profile_configuration_status",
]
