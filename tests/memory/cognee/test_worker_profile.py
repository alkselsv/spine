from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.memory.cognee.worker_profile import (
    CogneeWorkerLimits,
    CogneeWorkerProfile,
    ProfileApproval,
    ProfileConfigurationStatus,
    RunBudgetAuthorization,
    RuntimeIdentity,
    SecretReferenceState,
    guarded_provider_call,
    load_worker_profile,
    profile_configuration_status,
    WorkerProfileLoadStatus,
)
from tests.memory.cognee.worker_profile_fixtures import cognee_worker_profile_values

WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")


def create_profile() -> CogneeWorkerProfile:
    payload = cognee_worker_profile_values()
    payload["profile_digest"] = CogneeWorkerProfile.digest_for(payload)
    return CogneeWorkerProfile.model_validate(payload)


def test_profile_is_immutable_versioned_and_digest_bound() -> None:
    profile = create_profile()

    assert profile.profile_digest == CogneeWorkerProfile.digest_for(
        cognee_worker_profile_values()
    )
    with pytest.raises(ValidationError):
        profile.profile_id = "changed"  # type: ignore[misc]


def test_profile_rejects_unknown_fields_and_wrong_pinned_identities() -> None:
    unknown = cognee_worker_profile_values() | {"fallback_model": "other/model"}
    unknown["profile_digest"] = CogneeWorkerProfile.digest_for(unknown)
    mismatched = cognee_worker_profile_values() | {"neo4j_apoc_server_version": "5.26.31"}
    mismatched["profile_digest"] = CogneeWorkerProfile.digest_for(mismatched)
    client_mismatch = cognee_worker_profile_values() | {"neo4j_python_version": "5.28.5"}
    client_mismatch["profile_digest"] = CogneeWorkerProfile.digest_for(client_mismatch)
    model_mismatch = cognee_worker_profile_values() | {"embedding_model": "other/embedding"}
    model_mismatch["profile_digest"] = CogneeWorkerProfile.digest_for(model_mismatch)

    with pytest.raises(ValidationError):
        CogneeWorkerProfile.model_validate(unknown)
    with pytest.raises(ValidationError):
        CogneeWorkerProfile.model_validate(mismatched)
    with pytest.raises(ValidationError):
        CogneeWorkerProfile.model_validate(client_mismatch)
    with pytest.raises(ValidationError):
        CogneeWorkerProfile.model_validate(model_mismatch)


def test_profile_requires_every_numeric_limit_and_a_well_formed_secret_reference() -> None:
    missing_limits = cognee_worker_profile_values()
    del missing_limits["limits"]
    missing_limits["profile_digest"] = CogneeWorkerProfile.digest_for(missing_limits)
    malformed_reference = cognee_worker_profile_values() | {
        "model_secret_ref": "secretref://production//openai"
    }
    malformed_reference["profile_digest"] = CogneeWorkerProfile.digest_for(
        malformed_reference
    )

    with pytest.raises(ValidationError):
        CogneeWorkerProfile.model_validate(missing_limits)
    with pytest.raises(ValidationError):
        CogneeWorkerProfile.model_validate(malformed_reference)


def test_profile_rejects_required_model_revision_or_parameters_when_absent() -> None:
    candidates = (
        cognee_worker_profile_values() | {"llm_revision_required": True},
        cognee_worker_profile_values() | {"embedding_revision_required": True},
        cognee_worker_profile_values() | {"generation_parameters_required": True},
    )
    for values in candidates:
        values["profile_digest"] = CogneeWorkerProfile.digest_for(values)
        with pytest.raises(ValidationError):
            CogneeWorkerProfile.model_validate(values)


def test_profile_load_failure_hides_untrusted_configuration_values() -> None:
    values = cognee_worker_profile_values() | {
        "model_secret_ref": "private-provider-secret-value",
        "unknown_secret_value": "never-include-this-in-diagnostics",
    }
    values["profile_digest"] = CogneeWorkerProfile.digest_for(values)

    result = load_worker_profile(values)

    assert result.status is WorkerProfileLoadStatus.INVALID
    assert result.profile is None
    assert result.validation_error_count == 2
    assert "private-provider-secret-value" not in str(result)
    assert "never-include-this-in-diagnostics" not in str(result)


@pytest.mark.parametrize(
    "change",
    [
        {"max_requests_per_run": 0},
        {"max_concurrency": -1},
        {"max_input_tokens": 0},
        {"request_timeout_seconds": 0},
        {"run_deadline_seconds": 30},
        {"max_retries": -1},
        {"max_budget_amount": "0"},
        {"budget_currency": "usd"},
    ],
)
def test_provider_limits_reject_missing_or_invalid_bounds(
    change: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "max_requests_per_run": 20,
        "max_concurrency": 2,
        "max_input_tokens": 32_000,
        "max_output_tokens": 2_000,
        "request_timeout_seconds": 60,
        "run_deadline_seconds": 600,
        "max_retries": 2,
        "max_budget_amount": Decimal("1.25"),
        "budget_currency": "USD",
    }

    with pytest.raises(ValidationError):
        CogneeWorkerLimits.model_validate(values | change)


def test_provider_limits_reject_implicit_integer_and_float_money_coercion() -> None:
    values: dict[str, object] = {
        "max_requests_per_run": 20,
        "max_concurrency": 2,
        "max_input_tokens": 32_000,
        "max_output_tokens": 2_000,
        "request_timeout_seconds": 60,
        "run_deadline_seconds": 600,
        "max_retries": 2,
        "max_budget_amount": Decimal("1.25"),
        "budget_currency": "USD",
    }

    with pytest.raises(ValidationError):
        CogneeWorkerLimits.model_validate(values | {"max_requests_per_run": "20"})
    with pytest.raises(ValidationError):
        CogneeWorkerLimits.model_validate(values | {"max_budget_amount": 1.25})


def test_profile_digest_covers_approval_identity_and_secret_references() -> None:
    profile = create_profile()
    changed = profile.model_dump(mode="json") | {
        "approval_reference": "different-approval"
    }

    with pytest.raises(ValidationError):
        CogneeWorkerProfile.model_validate(changed)


class _Resolver:
    def __init__(self, state: SecretReferenceState = SecretReferenceState.VALID) -> None:
        self.state = state
        self.references: list[str] = []

    async def check(self, reference: str) -> SecretReferenceState:
        self.references.append(reference)
        return self.state


class _RaisingResolver:
    async def check(self, reference: str) -> SecretReferenceState:
        del reference
        raise RuntimeError("private secret payload must never be logged")


def approval(profile: CogneeWorkerProfile) -> ProfileApproval:
    return ProfileApproval(
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        approval_reference=profile.approval_reference,
        deployment_id=profile.deployment_id,
        release_id=profile.release_id,
        approved=True,
    )


def runtime_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        cognee_version="1.5.4",
        postgres_server_major="17",
        neo4j_apoc_server_version="5.26.32",
        pgvector_extension_version="0.8.7",
        neo4j_python_version="5.28.6",
        pgvector_python_version="0.3.6",
        llm_model="openai/gpt-5.4-nano",
        llm_revision=None,
        embedding_model="openai/text-embedding-3-small",
        embedding_revision=None,
    )


def budget_authorization(
    *, profile: CogneeWorkerProfile | None = None, amount: Decimal = Decimal("1.00"),
) -> RunBudgetAuthorization:
    selected_profile = profile or create_profile()
    return RunBudgetAuthorization(
        run_id="run-1",
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        authorization_reference="approval-run-1",
        profile_id=selected_profile.profile_id,
        profile_digest=selected_profile.profile_digest,
        approved=True,
        amount=amount,
        currency="USD",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


def test_readiness_reports_bounded_binding_status_without_provider_access() -> None:
    profile = create_profile()

    assert profile_configuration_status(
        profile=profile,
        approval=approval(profile),
        deployment_id=profile.deployment_id,
        release_id=profile.release_id,
    ) is ProfileConfigurationStatus.VALID
    assert profile_configuration_status(
        profile=profile,
        approval=approval(profile),
        deployment_id=profile.deployment_id,
        release_id="different-release",
    ) is ProfileConfigurationStatus.RELEASE_MISMATCH
    assert profile_configuration_status(
        profile=None,
        approval=None,
        deployment_id="private-deployment-name",
        release_id="private-release-name",
    ) is ProfileConfigurationStatus.PROFILE_MISSING


@pytest.mark.asyncio
async def test_guard_invokes_provider_only_when_every_binding_is_valid() -> None:
    profile = create_profile()
    resolver = _Resolver()
    calls: list[str] = []

    async def provider_call() -> str:
        calls.append("called")
        return "done"

    decision, result = await guarded_provider_call(
        profile=profile,
        approval=approval(profile),
        runtime=runtime_identity(),
        deployment_id=profile.deployment_id,
        release_id=profile.release_id,
        secret_resolver=resolver,
        budget_authorization=budget_authorization(),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        run_id="run-1",
        now=datetime(2026, 10, 22, tzinfo=timezone.utc),
        provider_call=provider_call,
    )

    assert decision.allowed is True
    assert decision.refusals == ()
    assert result == "done"
    assert calls == ["called"]
    assert resolver.references == [
        profile.postgres_secret_ref,
        profile.neo4j_secret_ref,
        profile.model_secret_ref,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        (SecretReferenceState.MISSING, "secret_reference_unavailable"),
        (SecretReferenceState.MALFORMED, "secret_reference_invalid"),
        (SecretReferenceState.UNAPPROVED, "secret_reference_invalid"),
        (SecretReferenceState.EXPIRED, "secret_reference_invalid"),
        (SecretReferenceState.REVOKED, "secret_reference_invalid"),
    ],
)
async def test_guard_refuses_missing_or_invalid_secret_reference(
    state: SecretReferenceState, expected_code: str,
) -> None:
    profile = create_profile()
    calls: list[str] = []

    async def provider_call() -> str:
        calls.append("called")
        return "done"

    decision, result = await guarded_provider_call(
        profile=profile,
        approval=approval(profile),
        runtime=runtime_identity(),
        deployment_id=profile.deployment_id,
        release_id=profile.release_id,
        secret_resolver=_Resolver(state),
        budget_authorization=budget_authorization(),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        run_id="run-1",
        now=datetime(2026, 10, 22, tzinfo=timezone.utc),
        provider_call=provider_call,
    )

    assert decision.allowed is False
    assert tuple(item.code for item in decision.refusals) == (expected_code,)
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_guard_suppresses_secret_resolver_exception_details(caplog) -> None:
    profile = create_profile()

    async def provider_call() -> str:
        pytest.fail("provider operation must not run")

    decision, result = await guarded_provider_call(
        profile=profile,
        approval=approval(profile),
        runtime=runtime_identity(),
        deployment_id=profile.deployment_id,
        release_id=profile.release_id,
        secret_resolver=_RaisingResolver(),
        budget_authorization=budget_authorization(profile=profile),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        run_id="run-1",
        now=datetime(2026, 10, 22, tzinfo=timezone.utc),
        provider_call=provider_call,
    )

    assert decision.allowed is False
    assert tuple(item.code for item in decision.refusals) == (
        "secret_reference_unavailable",
    )
    assert result is None
    assert "private secret payload" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("approval", None, "approval_missing"),
        ("approval", "unapproved", "profile_unapproved"),
        ("approval", "digest_mismatch", "profile_approval_mismatch"),
        ("deployment_id", "other-deployment", "deployment_mismatch"),
        ("release_id", "other-release", "release_mismatch"),
        ("runtime", None, "runtime_identity_missing"),
        ("runtime", "mismatch", "runtime_identity_mismatch"),
        ("budget_authorization", None, "run_budget_missing"),
        ("budget_authorization", "over", "run_budget_over_limit"),
        ("budget_authorization", "expired", "run_budget_expired"),
        ("budget_authorization", "unapproved", "run_budget_unapproved"),
        ("budget_authorization", "wrong_scope", "run_budget_scope_mismatch"),
        ("budget_authorization", "wrong_run", "run_budget_run_mismatch"),
        ("secret_resolver", None, "secret_resolver_unavailable"),
    ],
)
async def test_guard_fails_closed_without_leaking_diagnostic_input(
    field: str, value: Any, expected_code: str,
) -> None:
    profile = create_profile()
    kwargs: dict[str, Any] = {
        "profile": profile,
        "approval": approval(profile),
        "runtime": runtime_identity(),
        "deployment_id": profile.deployment_id,
        "release_id": profile.release_id,
        "secret_resolver": _Resolver(),
        "budget_authorization": budget_authorization(),
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "run_id": "run-1",
        "now": datetime(2026, 10, 22, tzinfo=timezone.utc),
    }
    if field == "approval" and value == "unapproved":
        kwargs[field] = approval(profile).model_copy(update={"approved": False})
    elif field == "approval" and value == "digest_mismatch":
        kwargs[field] = approval(profile).model_copy(update={"profile_digest": "0" * 64})
    elif field == "runtime" and value == "mismatch":
        kwargs[field] = runtime_identity().model_copy(update={"llm_model": "other/model"})
    elif field == "budget_authorization" and value == "over":
        kwargs[field] = budget_authorization(
            profile=create_profile(), amount=Decimal("1.26")
        )
    elif field == "budget_authorization" and value == "wrong_scope":
        kwargs[field] = budget_authorization(profile=create_profile()).model_copy(
            update={"workspace_id": UUID("10000000-0000-4000-8000-000000000099")}
        )
    elif field == "budget_authorization" and value == "expired":
        kwargs[field] = budget_authorization(profile=create_profile()).model_copy(
            update={"expires_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        )
    elif field == "budget_authorization" and value == "unapproved":
        kwargs[field] = budget_authorization(profile=create_profile()).model_copy(
            update={"approved": False}
        )
    elif field == "budget_authorization" and value == "wrong_run":
        kwargs[field] = budget_authorization(profile=create_profile()).model_copy(
            update={"run_id": "another-run"}
        )
    else:
        kwargs[field] = value
    calls: list[str] = []
    async def provider_call() -> str:
        calls.append("called")
        return "done"
    kwargs["provider_call"] = provider_call

    decision, result = await guarded_provider_call(**kwargs)

    assert decision.allowed is False
    assert expected_code in tuple(item.code for item in decision.refusals)
    assert expected_code not in str(result)
    assert calls == []


@pytest.mark.asyncio
async def test_provider_exception_is_returned_as_bounded_diagnostic(caplog) -> None:
    profile = create_profile()

    async def provider_call() -> str:
        raise RuntimeError("provider response contains sensitive payload")

    decision, result = await guarded_provider_call(
        profile=profile,
        approval=approval(profile),
        runtime=runtime_identity(),
        deployment_id=profile.deployment_id,
        release_id=profile.release_id,
        secret_resolver=_Resolver(),
        budget_authorization=budget_authorization(profile=profile),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        run_id="run-1",
        now=datetime(2026, 10, 22, tzinfo=timezone.utc),
        provider_call=provider_call,
    )

    assert decision.allowed is False
    assert tuple(item.code for item in decision.refusals) == ("provider_call_failed",)
    assert result is None
    assert "provider response" not in caplog.text
