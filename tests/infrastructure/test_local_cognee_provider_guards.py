from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from spine.infrastructure.local_cognee_provider_guards import (
    LocalCogneeProviderAuthorization,
    LocalCogneeProviderAuthorizationSet,
    LocalCogneeProviderAuthorizationSettings,
    LocalCogneeProviderSecretPresenceSettings,
    build_local_cognee_provider_guards,
)
from spine.memory.cognee.projection_profile_binding import (
    CogneeProjectionProfileBinding,
    bind_cognee_projection_profile,
)
from spine.memory.cognee.worker_profile import ProfileApproval, RuntimeIdentity
from spine.memory.contracts import ProjectionLifecycle, ProjectionOutcome, ProjectionResult

from tests.application.test_projection_result_mapping import _command
from tests.memory.cognee.worker_profile_fixtures import cognee_worker_profile_values


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


class _RecordingProjection:
    def __init__(self) -> None:
        self.calls = 0

    async def project(self, command):
        self.calls += 1
        return ProjectionResult(
            operation=command.operation,
            outcome=ProjectionOutcome.SUCCEEDED,
            command_identity=command.command_identity,
            command_digest=command.command_digest,
            lifecycle=ProjectionLifecycle.SUCCEEDED,
        )


def _profile():
    from spine.memory.cognee.worker_profile import CogneeWorkerProfile

    values = cognee_worker_profile_values()
    values["profile_digest"] = CogneeWorkerProfile.digest_for(values)
    return CogneeWorkerProfile.model_validate(values)


def test_local_authorization_settings_are_explicit_and_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    command = _command()
    authorization = LocalCogneeProviderAuthorization(
        schema_version="cognee-local-provider-authorization:v1",
        deployment_mode="development",
        authorization_reference="operator-approved-q16-projection",
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        amount=Decimal("1.00"),
        currency="USD",
        expires_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        secret_references=(
            profile.postgres_secret_ref,
            profile.neo4j_secret_ref,
            profile.model_secret_ref,
        ),
    )
    monkeypatch.delenv("SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON", raising=False)
    assert LocalCogneeProviderAuthorizationSettings().load_optional() is None

    monkeypatch.setenv(
        "SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON",
        authorization.model_dump_json(),
    )

    assert LocalCogneeProviderAuthorizationSettings().load_optional() == authorization


def test_secret_presence_settings_resolve_only_available_profile_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    for name in (
        "DB_PASSWORD",
        "GRAPH_DATABASE_PASSWORD",
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    assert (
        LocalCogneeProviderSecretPresenceSettings().available_references(profile)
        == frozenset()
    )

    monkeypatch.setenv("DB_PASSWORD", "synthetic-postgres")
    monkeypatch.setenv("GRAPH_DATABASE_PASSWORD", "synthetic-graph")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-model")
    settings = LocalCogneeProviderSecretPresenceSettings()

    assert settings.available_references(profile) == frozenset(
        {
            profile.postgres_secret_ref,
            profile.neo4j_secret_ref,
            profile.model_secret_ref,
        }
    )
    assert "synthetic" not in repr(settings)


def test_embedding_key_without_llm_key_does_not_satisfy_model_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY", "synthetic-embedding")

    available = LocalCogneeProviderSecretPresenceSettings().available_references(
        profile
    )

    assert profile.model_secret_ref not in available


@pytest.mark.parametrize(
    "variable",
    (
        "DB_PASSWORD",
        "GRAPH_DATABASE_PASSWORD",
        "LLM_API_KEY",
    ),
)
def test_empty_secret_value_cannot_satisfy_profile_reference(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    profile = _profile()
    monkeypatch.setenv("DB_PASSWORD", "synthetic-postgres")
    monkeypatch.setenv("GRAPH_DATABASE_PASSWORD", "synthetic-graph")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-model")
    monkeypatch.setenv(variable, "")

    with pytest.raises(ValueError):
        LocalCogneeProviderSecretPresenceSettings()


@pytest.mark.asyncio
async def test_local_authorization_allows_only_the_exact_profile_scope_and_secret_set() -> None:
    profile = _profile()
    command = _command()
    authorization = LocalCogneeProviderAuthorization(
        schema_version="cognee-local-provider-authorization:v1",
        deployment_mode="development",
        authorization_reference="operator-approved-q16-projection",
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        amount=Decimal("1.00"),
        currency="USD",
        expires_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        secret_references=(
            profile.postgres_secret_ref,
            profile.neo4j_secret_ref,
            profile.model_secret_ref,
        ),
    )
    guards = build_local_cognee_provider_guards(
        profile=profile,
        authorization=authorization,
        available_secret_references=frozenset(authorization.secret_references),
        clock=lambda: NOW,
    )
    delegate = _RecordingProjection()
    projection = bind_cognee_projection_profile(
        delegate,
        CogneeProjectionProfileBinding(
            profile=profile,
            approval=ProfileApproval(
                profile_id=profile.profile_id,
                profile_digest=profile.profile_digest,
                approval_reference=profile.approval_reference,
                deployment_id=profile.deployment_id,
                release_id=profile.release_id,
                approved=True,
            ),
            runtime_identity=RuntimeIdentity(
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
            ),
            deployment_id=profile.deployment_id,
            release_id=profile.release_id,
            secret_resolver=guards.secret_resolver,
            budget_authorization_resolver=guards.budget_authorization_resolver,
            provider_call_executor=guards.provider_call_executor,
            clock=lambda: NOW,
        ),
    )

    result = await projection.project(command)

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert delegate.calls == 1


@pytest.mark.asyncio
async def test_local_authorization_set_resolves_each_exact_scope_without_wildcard() -> None:
    profile = _profile()
    first = _command()
    second = first.model_copy(
        update={
            "workspace_id": UUID("10000000-0000-4000-8000-000000000099"),
            "environment_id": UUID("20000000-0000-4000-8000-000000000099"),
        }
    )
    base = LocalCogneeProviderAuthorization(
        schema_version="cognee-local-provider-authorization:v1",
        deployment_mode="development",
        authorization_reference="operator-approved-q16-projection-a",
        workspace_id=first.workspace_id,
        environment_id=first.environment_id,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        amount=Decimal("1.00"),
        currency="USD",
        expires_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        secret_references=(
            profile.postgres_secret_ref,
            profile.neo4j_secret_ref,
            profile.model_secret_ref,
        ),
    )
    document = LocalCogneeProviderAuthorizationSet(
        schema_version="cognee-local-provider-authorization-set:v1",
        deployment_mode="development",
        authorizations=(
            base,
            base.model_copy(
                update={
                    "authorization_reference": "operator-approved-q16-projection-b",
                    "workspace_id": second.workspace_id,
                    "environment_id": second.environment_id,
                }
            ),
        ),
    )
    guards = build_local_cognee_provider_guards(
        profile=profile,
        authorization=document,
        available_secret_references=frozenset(base.secret_references),
        clock=lambda: NOW,
    )

    first_budget = await guards.budget_authorization_resolver(first)
    second_budget = await guards.budget_authorization_resolver(second)
    foreign_budget = await guards.budget_authorization_resolver(
        second.model_copy(
            update={"workspace_id": UUID("10000000-0000-4000-8000-000000000098")}
        )
    )

    assert first_budget is not None and first_budget.workspace_id == first.workspace_id
    assert second_budget is not None and second_budget.workspace_id == second.workspace_id
    assert foreign_budget is None
