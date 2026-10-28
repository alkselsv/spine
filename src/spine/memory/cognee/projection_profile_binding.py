"""Bind the approved Cognee worker profile to the ContextProjection seam."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import inspect
from typing import Protocol, TypeVar

from spine.memory.contracts import (
    ContextProjection,
    ProjectCommand,
    ProjectionCommand,
    ProjectionResult,
    ProjectionRetrievalResult,
    RebuildCommand,
    RemoveCommand,
    RetrieveCommand,
    UpdateCommand,
)
from spine.memory.cognee.worker_profile import (
    CogneeWorkerProfile,
    ProfileApproval,
    ProfileConfigurationStatus,
    RunBudgetAuthorization,
    RuntimeIdentity,
    SecretReferenceResolver,
    guarded_provider_call,
    profile_configuration_status,
    runtime_identity_matches_profile,
)


BudgetAuthorizationResult = (
    RunBudgetAuthorization | None | Awaitable[RunBudgetAuthorization | None]
)
BudgetAuthorizationResolver = Callable[[ProjectionCommand], BudgetAuthorizationResult]
T = TypeVar("T")


class CogneeProviderCallExecutor(Protocol):
    """Deployment adapter that enforces the selected profile's physical limits."""

    async def invoke(
        self,
        *,
        profile: CogneeWorkerProfile,
        command: ProjectionCommand,
        provider_call: Callable[[], Awaitable[T]],
    ) -> T: ...


@dataclass(frozen=True, slots=True)
class CogneeProjectionProfileBinding:
    """Immutable local inputs required before any Cognee provider operation."""

    profile: CogneeWorkerProfile | None
    approval: ProfileApproval | None
    runtime_identity: RuntimeIdentity | None
    deployment_id: str
    release_id: str
    secret_resolver: SecretReferenceResolver | None
    budget_authorization_resolver: BudgetAuthorizationResolver
    provider_call_executor: CogneeProviderCallExecutor | None
    clock: Callable[[], datetime]


class CogneeProjectionProfileRefusal(RuntimeError):
    """Bounded fail-closed result that never carries provider or source data."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = reason_codes
        super().__init__(",".join(reason_codes))


class _ProfileBoundCogneeProjection:
    def __init__(
        self,
        delegate: ContextProjection,
        binding: CogneeProjectionProfileBinding,
    ) -> None:
        self._delegate = delegate
        self._binding = binding

    async def project(self, command: ProjectCommand) -> ProjectionResult:
        return await self._invoke(command, lambda: self._delegate.project(command))

    async def update(self, command: UpdateCommand) -> ProjectionResult:
        return await self._invoke(command, lambda: self._delegate.update(command))

    async def remove(self, command: RemoveCommand) -> ProjectionResult:
        return await self._invoke(command, lambda: self._delegate.remove(command))

    async def rebuild(self, command: RebuildCommand) -> ProjectionResult:
        return await self._invoke(command, lambda: self._delegate.rebuild(command))

    async def retrieve(
        self, command: RetrieveCommand
    ) -> ProjectionRetrievalResult:
        return await self._invoke(command, lambda: self._delegate.retrieve(command))

    async def _invoke(
        self,
        command: ProjectionCommand,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        status = profile_configuration_status(
            profile=self._binding.profile,
            approval=self._binding.approval,
            deployment_id=self._binding.deployment_id,
            release_id=self._binding.release_id,
        )
        if status is not ProfileConfigurationStatus.VALID:
            raise CogneeProjectionProfileRefusal((status.value,))
        profile = self._binding.profile
        runtime = self._binding.runtime_identity
        if profile is None or runtime is None:
            raise CogneeProjectionProfileRefusal(("runtime_identity_missing",))
        if not runtime_identity_matches_profile(profile, runtime):
            raise CogneeProjectionProfileRefusal(("runtime_identity_mismatch",))
        executor = self._binding.provider_call_executor
        if executor is None:
            raise CogneeProjectionProfileRefusal(("provider_limits_unavailable",))
        try:
            authorization = self._binding.budget_authorization_resolver(command)
            if inspect.isawaitable(authorization):
                authorization = await authorization
        except Exception:
            authorization = None

        async def bounded_provider_call() -> T:
            return await executor.invoke(
                profile=profile,
                command=command,
                provider_call=operation,
            )

        decision, result = await guarded_provider_call(
            profile=profile,
            approval=self._binding.approval,
            runtime=runtime,
            deployment_id=self._binding.deployment_id,
            release_id=self._binding.release_id,
            secret_resolver=self._binding.secret_resolver,
            budget_authorization=authorization,
            run_id=str(command.projection_run_id),
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            now=self._binding.clock(),
            provider_call=bounded_provider_call,
        )
        if not decision.allowed or result is None:
            raise CogneeProjectionProfileRefusal(
                tuple(refusal.code for refusal in decision.refusals)
            )
        return result


def bind_cognee_projection_profile(
    projection: ContextProjection,
    binding: CogneeProjectionProfileBinding,
) -> ContextProjection:
    """Return one projection adapter guarded by the approved runtime profile."""

    return _ProfileBoundCogneeProjection(projection, binding)


__all__ = [
    "BudgetAuthorizationResolver",
    "CogneeProviderCallExecutor",
    "CogneeProjectionProfileBinding",
    "CogneeProjectionProfileRefusal",
    "bind_cognee_projection_profile",
]
