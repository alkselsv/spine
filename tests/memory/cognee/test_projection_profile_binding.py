from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from spine.memory.cognee.projection_profile_binding import (
    CogneeProjectionProfileBinding,
    CogneeProjectionProfileRefusal,
    bind_cognee_projection_profile,
)
from spine.memory.cognee.worker_profile import (
    CogneeWorkerProfile,
    ProfileApproval,
    RunBudgetAuthorization,
    RuntimeIdentity,
    SecretReferenceState,
)
from spine.memory.contracts import (
    ProjectCommand,
    ProjectionLifecycle,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    ProjectionResult,
    RebuildCommand,
    RemoveCommand,
    RetrieveCommand,
    UpdateCommand,
)

from tests.application.test_projection_result_mapping import _command
from tests.memory.cognee.worker_profile_fixtures import cognee_worker_profile_values


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def successful_result(command) -> ProjectionResult:
    return ProjectionResult(
        operation=command.operation,
        outcome=ProjectionOutcome.SUCCEEDED,
        command_identity=command.command_identity,
        command_digest=command.command_digest,
        lifecycle=ProjectionLifecycle.SUCCEEDED,
    )


def successful_output(command) -> ProjectionResult | ProjectionRetrievalResult:
    if isinstance(command, RetrieveCommand):
        return ProjectionRetrievalResult(
            outcome=ProjectionOutcome.SUCCEEDED,
            command_identity=command.command_identity,
            command_digest=command.command_digest,
        )
    return successful_result(command)


def profile() -> CogneeWorkerProfile:
    values = cognee_worker_profile_values()
    values["profile_digest"] = CogneeWorkerProfile.digest_for(values)
    return CogneeWorkerProfile.model_validate(values)


def approval(value: CogneeWorkerProfile) -> ProfileApproval:
    return ProfileApproval(
        profile_id=value.profile_id,
        profile_digest=value.profile_digest,
        approval_reference=value.approval_reference,
        deployment_id=value.deployment_id,
        release_id=value.release_id,
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
    selected_profile: CogneeWorkerProfile,
    command,
) -> RunBudgetAuthorization:
    return RunBudgetAuthorization(
        run_id=str(command.projection_run_id),
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        authorization_reference="operator-run-533",
        profile_id=selected_profile.profile_id,
        profile_digest=selected_profile.profile_digest,
        approved=True,
        amount=Decimal("1.00"),
        currency="USD",
        expires_at=datetime(2026, 10, 22, 1, tzinfo=timezone.utc),
    )


class ValidSecretReferences:
    async def check(self, reference: str) -> SecretReferenceState:
        del reference
        return SecretReferenceState.VALID


class TestProviderCallExecutor:
    __test__ = False

    async def invoke(self, *, profile, command, provider_call):
        assert profile.limits.max_requests_per_run == 20
        assert command.projection_run_id.int != 0
        return await provider_call()


class RecordingProjection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def project(self, command):
        self.calls.append("project")
        return successful_result(command)

    async def update(self, command):
        self.calls.append("update")
        return successful_result(command)

    async def remove(self, command):
        self.calls.append("remove")
        return successful_result(command)

    async def rebuild(self, command):
        self.calls.append("rebuild")
        return successful_result(command)

    async def retrieve(self, command):
        self.calls.append("retrieve")
        return successful_output(command)


def command_for(command_type):
    values = _command().model_dump(mode="python", exclude={"operation"})
    if command_type is RetrieveCommand:
        values["query"] = "synthetic query"
    return command_type.model_validate(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "command_type"),
    [
        ("project", ProjectCommand),
        ("update", UpdateCommand),
        ("remove", RemoveCommand),
        ("rebuild", RebuildCommand),
        ("retrieve", RetrieveCommand),
    ],
)
async def test_missing_profile_refuses_every_operation_without_provider_call(
    method_name: str,
    command_type,
) -> None:
    delegate = RecordingProjection()
    projection = bind_cognee_projection_profile(
        delegate,
        CogneeProjectionProfileBinding(
            profile=None,
            approval=None,
            runtime_identity=None,
            deployment_id="spine-prod-eu-1",
            release_id="release-2026.10.22",
            secret_resolver=None,
            budget_authorization_resolver=lambda command: None,
            provider_call_executor=None,
            clock=lambda: NOW,
        ),
    )

    with pytest.raises(CogneeProjectionProfileRefusal) as error:
        await getattr(projection, method_name)(command_for(command_type))

    assert error.value.reason_codes == ("profile_missing",)
    assert delegate.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "command_type"),
    [
        ("project", ProjectCommand),
        ("update", UpdateCommand),
        ("remove", RemoveCommand),
        ("rebuild", RebuildCommand),
        ("retrieve", RetrieveCommand),
    ],
)
async def test_approved_profile_invokes_every_operation_with_run_scoped_budget(
    method_name: str,
    command_type,
) -> None:
    selected_profile = profile()
    command = command_for(command_type)
    delegate = RecordingProjection()
    resolved_commands = []

    async def resolve_budget(resolved_command):
        resolved_commands.append(resolved_command)
        return budget_authorization(selected_profile, resolved_command)

    projection = bind_cognee_projection_profile(
        delegate,
        CogneeProjectionProfileBinding(
            profile=selected_profile,
            approval=approval(selected_profile),
            runtime_identity=runtime_identity(),
            deployment_id=selected_profile.deployment_id,
            release_id=selected_profile.release_id,
            secret_resolver=ValidSecretReferences(),
            budget_authorization_resolver=resolve_budget,
            provider_call_executor=TestProviderCallExecutor(),
            clock=lambda: NOW,
        ),
    )

    result = await getattr(projection, method_name)(command)

    assert result == successful_output(command)
    assert resolved_commands == [command]
    assert delegate.calls == [method_name]


@pytest.mark.asyncio
async def test_missing_limit_executor_refuses_before_provider_call() -> None:
    selected_profile = profile()
    command = _command()
    delegate = RecordingProjection()
    projection = bind_cognee_projection_profile(
        delegate,
        CogneeProjectionProfileBinding(
            profile=selected_profile,
            approval=approval(selected_profile),
            runtime_identity=runtime_identity(),
            deployment_id=selected_profile.deployment_id,
            release_id=selected_profile.release_id,
            secret_resolver=ValidSecretReferences(),
            budget_authorization_resolver=lambda value: budget_authorization(
                selected_profile, value
            ),
            provider_call_executor=None,
            clock=lambda: NOW,
        ),
    )

    with pytest.raises(CogneeProjectionProfileRefusal) as error:
        await projection.project(command)

    assert error.value.reason_codes == ("provider_limits_unavailable",)
    assert delegate.calls == []


@pytest.mark.asyncio
async def test_budget_resolution_failure_is_bounded_and_skips_provider_call() -> None:
    selected_profile = profile()
    delegate = RecordingProjection()

    async def fail_budget_resolution(command):
        del command
        raise RuntimeError("operator payload and private identifier")

    projection = bind_cognee_projection_profile(
        delegate,
        CogneeProjectionProfileBinding(
            profile=selected_profile,
            approval=approval(selected_profile),
            runtime_identity=runtime_identity(),
            deployment_id=selected_profile.deployment_id,
            release_id=selected_profile.release_id,
            secret_resolver=ValidSecretReferences(),
            budget_authorization_resolver=fail_budget_resolution,
            provider_call_executor=TestProviderCallExecutor(),
            clock=lambda: NOW,
        ),
    )

    with pytest.raises(CogneeProjectionProfileRefusal) as error:
        await projection.project(_command())

    assert error.value.reason_codes == ("run_budget_missing",)
    assert "operator payload" not in str(error.value)
    assert delegate.calls == []
