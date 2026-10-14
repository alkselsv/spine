"""Recovery and physical cleanup for derived Cognee projection artifacts.

This module is deliberately outside the canonical persistence layer.  It only
coordinates content-free physical identifiers, immutable replay inputs and
provider adapters.  A cleanup can therefore be retried without changing a
source revision, tombstone or publication pointer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionNamespace,
    ProjectionNamespaceState,
    ProjectionSourceRevision,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter

from .rebuild import (
    ShadowRebuildPlan,
    ShadowRebuildResult,
    ShadowRebuildState,
    ShadowRebuildOrchestrator,
)
from .validation import ArtifactKind, PhysicalArtifact


_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"
_DIGEST = r"[0-9a-f]{64}"


class PhysicalCleanupStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    RECOVERABLE = "recoverable"
    FAILED = "failed"


class PhysicalCleanupEffectStatus(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    FAILED = "failed"


class PhysicalCleanupFailureCode(str, Enum):
    INTEGRITY_CONFLICT = "integrity_conflict"
    CROSS_WORKSPACE = "cross_workspace"
    NAMESPACE_UNAVAILABLE = "namespace_unavailable"
    PHYSICAL_FAILURE = "physical_failure"


class ProjectionRepairStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    RECOVERABLE = "recoverable"
    FAILED = "failed"
    FENCED = "fenced"


class ProjectionRepairFailureCode(str, Enum):
    INTEGRITY_CONFLICT = "integrity_conflict"
    REVISION_FENCED = "revision_fenced"
    TOMBSTONED_SOURCE = "tombstoned_source"
    PHYSICAL_FAILURE = "physical_failure"
    NAMESPACE_UNAVAILABLE = "namespace_unavailable"


def _require_id(value: UUID, name: str) -> None:
    if value.int == 0:
        raise ValueError(f"{name} must be a non-zero UUID")


class PhysicalCleanupPlan(DefinitionModel):
    """Immutable, tenant-scoped deletion work for one failed namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["physical-cleanup-plan:v1"] = "physical-cleanup-plan:v1"
    workspace_id: UUID
    environment_id: UUID
    projection_run_id: UUID
    projection_candidate: ProjectionCandidate
    projection_config: ProjectionConfigVersion
    canonical_boundary: CanonicalBoundary
    physical_namespace: ProjectionNamespace
    cleanup_identity: str = Field(pattern=_IDENTIFIER)
    cleanup_digest: str = Field(pattern=_DIGEST)
    artifacts: tuple[PhysicalArtifact, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> "PhysicalCleanupPlan":
        for name in (
            "workspace_id",
            "environment_id",
            "projection_run_id",
        ):
            _require_id(getattr(self, name), name)
        if self.physical_namespace.state not in {
            ProjectionNamespaceState.FAILED,
            ProjectionNamespaceState.RETIRED,
        }:
            raise ValueError("only failed or retired namespaces may be cleaned")
        if self.projection_candidate.run_id != self.projection_run_id:
            raise ValueError("cleanup candidate must belong to the projection run")
        context = ProjectionNamespaceContext(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            projection_kind=self.physical_namespace.projection_kind,
            projection_candidate=self.projection_candidate,
            projection_config=self.projection_config,
            canonical_boundary=self.canonical_boundary,
            state=self.physical_namespace.state,
        )
        ProjectionNamespaceRouter().validate(self.physical_namespace, context)

        keys: set[tuple[ArtifactKind, str]] = set()
        data_ids: set[UUID] = set()
        for artifact in self.artifacts:
            if (
                artifact.workspace_id != self.workspace_id
                or artifact.environment_id != self.environment_id
            ):
                raise ValueError("cleanup artifact is outside the workspace scope")
            if artifact.namespace_digest != self.physical_namespace.namespace_digest:
                raise ValueError("cleanup artifact is outside the namespace scope")
            if (
                artifact.projection_config_id != self.projection_config.config_id
                or artifact.projection_config_version != self.projection_config.version
                or artifact.projection_config_digest != self.projection_config.config_digest
            ):
                raise ValueError("cleanup artifact is outside the configuration scope")
            key = (artifact.artifact_kind, artifact.artifact_id)
            if key in keys:
                raise ValueError("cleanup artifacts must be unique")
            if artifact.data_id in data_ids:
                raise ValueError("cleanup data IDs must be unique")
            keys.add(key)
            data_ids.add(artifact.data_id)
        return self

    @property
    def plan_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"cleanup_digest"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class PhysicalCleanupEffect(DefinitionModel):
    """Safe result for deleting one physical artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["physical-cleanup-effect:v1"] = "physical-cleanup-effect:v1"
    effect_id: str = Field(min_length=1, max_length=256)
    artifact_kind: ArtifactKind
    artifact_id: str = Field(min_length=1, max_length=128)
    data_id: UUID
    namespace_digest: str = Field(pattern=_DIGEST)
    status: PhysicalCleanupEffectStatus
    failure_code: PhysicalCleanupFailureCode | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "PhysicalCleanupEffect":
        if self.status is PhysicalCleanupEffectStatus.FAILED and self.failure_code is None:
            raise ValueError("failed cleanup effect requires a failure code")
        if (
            self.status
            in {PhysicalCleanupEffectStatus.APPLIED, PhysicalCleanupEffectStatus.REPLAYED}
            and self.failure_code is not None
        ):
            raise ValueError("successful cleanup effect cannot contain a failure")
        return self


class PhysicalCleanupFailure(DefinitionModel):
    """Disclosure-safe terminal or retryable cleanup failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["physical-cleanup-failure:v1"] = (
        "physical-cleanup-failure:v1"
    )
    code: PhysicalCleanupFailureCode
    retryable: bool


class PhysicalCleanupResult(DefinitionModel):
    """Content-free outcome of a namespace cleanup attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["physical-cleanup-result:v1"] = "physical-cleanup-result:v1"
    cleanup_identity: str = Field(pattern=_IDENTIFIER)
    cleanup_digest: str = Field(pattern=_DIGEST)
    namespace_digest: str = Field(pattern=_DIGEST)
    status: PhysicalCleanupStatus
    effects: tuple[PhysicalCleanupEffect, ...] = ()
    deleted_artifact_ids: tuple[str, ...] = ()
    pending_artifact_ids: tuple[str, ...] = ()
    failure: PhysicalCleanupFailure | None = None


class PhysicalArtifactCleanupLane(Protocol):
    """Minimal physical deletion seam used by the recovery coordinator."""

    async def delete_artifact(
        self,
        effect: PhysicalCleanupEffect,
        *,
        namespace: ProjectionNamespace,
        artifact: PhysicalArtifact,
    ) -> PhysicalCleanupEffect: ...


class CogneePhysicalCleanupLane:
    """Delete one routed Cognee artifact through the public SDK seam."""

    def __init__(self, sdk: "CogneeSdk") -> None:
        self._sdk = sdk

    async def delete_artifact(
        self,
        effect: PhysicalCleanupEffect,
        *,
        namespace: ProjectionNamespace,
        artifact: PhysicalArtifact,
    ) -> PhysicalCleanupEffect:
        if (
            artifact.namespace_digest != namespace.namespace_digest
            or artifact.data_id != effect.data_id
        ):
            return effect.model_copy(
                update={
                    "status": PhysicalCleanupEffectStatus.FAILED,
                    "failure_code": PhysicalCleanupFailureCode.CROSS_WORKSPACE,
                }
            )
        try:
            dataset_name = (
                f"spine_{namespace.projection_kind}_{namespace.namespace_digest}"
            )
            dataset_id = await _resolve_dataset_id(self._sdk, dataset_name)
            if dataset_id is None:
                return effect.model_copy(
                    update={
                        "status": PhysicalCleanupEffectStatus.REPLAYED,
                        "failure_code": None,
                    }
                )
            await self._sdk.delete_data(
                dataset_id=dataset_id,
                data_id=artifact.data_id,
                mode="soft",
            )
        except Exception:
            return effect.model_copy(
                update={
                    "status": PhysicalCleanupEffectStatus.FAILED,
                    "failure_code": PhysicalCleanupFailureCode.PHYSICAL_FAILURE,
                }
            )
        return effect.model_copy(
            update={
                "status": PhysicalCleanupEffectStatus.APPLIED,
                "failure_code": None,
            }
        )


class DeterministicPhysicalCleanupStore:
    """Offline cleanup adapter with deterministic partial-failure injection."""

    def __init__(
        self,
        *,
        fail_once_for: Iterable[str] = (),
        fail_for: Iterable[str] = (),
    ) -> None:
        self._fail_once_for = set(fail_once_for)
        self._fail_for = set(fail_for)
        self._attempts: dict[str, int] = {}
        self._deleted: set[str] = set()

    async def delete_artifact(
        self,
        effect: PhysicalCleanupEffect,
        *,
        namespace: ProjectionNamespace,
        artifact: PhysicalArtifact,
    ) -> PhysicalCleanupEffect:
        attempts = self._attempts.get(artifact.artifact_id, 0) + 1
        self._attempts[artifact.artifact_id] = attempts
        if artifact.artifact_id in self._deleted:
            return effect.model_copy(
                update={
                    "status": PhysicalCleanupEffectStatus.REPLAYED,
                    "failure_code": None,
                }
            )
        if artifact.artifact_id in self._fail_for or (
            artifact.artifact_id in self._fail_once_for and attempts == 1
        ):
            return effect.model_copy(
                update={
                    "status": PhysicalCleanupEffectStatus.FAILED,
                    "failure_code": PhysicalCleanupFailureCode.PHYSICAL_FAILURE,
                }
            )
        self._deleted.add(artifact.artifact_id)
        return effect.model_copy(
            update={
                "status": PhysicalCleanupEffectStatus.APPLIED,
                "failure_code": None,
            }
        )

    def attempts_for(self, artifact_id: str) -> int:
        return self._attempts.get(artifact_id, 0)


@dataclass
class _CleanupExecution:
    digest: str
    effects: dict[str, PhysicalCleanupEffect] = field(default_factory=dict)
    terminal: PhysicalCleanupResult | None = None


class ProjectionRecoveryCoordinator:
    """Deep recovery module for cleanup and immutable-input projection repair."""

    def __init__(
        self,
        cleanup_lane: PhysicalArtifactCleanupLane,
        *,
        rebuild_orchestrator: ShadowRebuildOrchestrator | None = None,
    ) -> None:
        self._cleanup_lane = cleanup_lane
        self._rebuild = rebuild_orchestrator
        self._cleanup_executions: dict[
            tuple[UUID, UUID, str, str], _CleanupExecution
        ] = {}
        self._lock = asyncio.Lock()

    async def cleanup(self, plan: PhysicalCleanupPlan) -> PhysicalCleanupResult:
        """Delete only listed artifacts in a failed/retired namespace."""

        async with self._lock:
            invalid_scope = _cleanup_scope_failure(plan)
            if invalid_scope is not None:
                return self._cleanup_result(
                    plan,
                    status=PhysicalCleanupStatus.FAILED,
                    failure=PhysicalCleanupFailure(
                        code=invalid_scope,
                        retryable=False,
                    ),
                )
            key = (
                plan.workspace_id,
                plan.environment_id,
                plan.physical_namespace.namespace_digest,
                plan.cleanup_identity,
            )
            execution = self._cleanup_executions.get(key)
            if execution is not None and execution.digest != plan.cleanup_digest:
                return self._cleanup_result(
                    plan,
                    status=PhysicalCleanupStatus.FAILED,
                    failure=PhysicalCleanupFailure(
                        code=PhysicalCleanupFailureCode.INTEGRITY_CONFLICT,
                        retryable=False,
                    ),
                )
            if execution is None:
                execution = _CleanupExecution(digest=plan.cleanup_digest)
                self._cleanup_executions[key] = execution
            if execution.terminal is not None:
                if execution.terminal.status is PhysicalCleanupStatus.SUCCEEDED:
                    return execution.terminal.model_copy(
                        update={"status": PhysicalCleanupStatus.REPLAYED}
                    )
                return execution.terminal

            for artifact in sorted(
                plan.artifacts,
                key=lambda item: (item.artifact_kind.value, item.artifact_id),
            ):
                effect_key = f"{artifact.artifact_kind.value}:{artifact.artifact_id}"
                previous = execution.effects.get(effect_key)
                if previous is not None and previous.status in {
                    PhysicalCleanupEffectStatus.APPLIED,
                    PhysicalCleanupEffectStatus.REPLAYED,
                }:
                    continue
                effect = previous or PhysicalCleanupEffect(
                    effect_id=self._effect_id(plan, artifact),
                    artifact_kind=artifact.artifact_kind,
                    artifact_id=artifact.artifact_id,
                    data_id=artifact.data_id,
                    namespace_digest=plan.physical_namespace.namespace_digest,
                    status=PhysicalCleanupEffectStatus.FAILED,
                    failure_code=PhysicalCleanupFailureCode.PHYSICAL_FAILURE,
                )
                try:
                    effect = await self._cleanup_lane.delete_artifact(
                        effect,
                        namespace=plan.physical_namespace,
                        artifact=artifact,
                    )
                except Exception:
                    effect = effect.model_copy(
                        update={
                            "status": PhysicalCleanupEffectStatus.FAILED,
                            "failure_code": PhysicalCleanupFailureCode.PHYSICAL_FAILURE,
                        }
                    )
                if not _same_cleanup_effect(effect, previous or effect, artifact, plan):
                    effect = effect.model_copy(
                        update={
                            "effect_id": self._effect_id(plan, artifact),
                            "artifact_kind": artifact.artifact_kind,
                            "artifact_id": artifact.artifact_id,
                            "data_id": artifact.data_id,
                            "namespace_digest": plan.physical_namespace.namespace_digest,
                            "status": PhysicalCleanupEffectStatus.FAILED,
                            "failure_code": PhysicalCleanupFailureCode.INTEGRITY_CONFLICT,
                        }
                    )
                execution.effects[effect_key] = effect

                if effect.status is PhysicalCleanupEffectStatus.FAILED:
                    retryable = effect.failure_code is PhysicalCleanupFailureCode.PHYSICAL_FAILURE
                    result = self._cleanup_result(
                        plan,
                        status=(
                            PhysicalCleanupStatus.RECOVERABLE
                            if retryable
                            else PhysicalCleanupStatus.FAILED
                        ),
                        effects=tuple(execution.effects.values()),
                        failure=PhysicalCleanupFailure(
                            code=effect.failure_code
                            or PhysicalCleanupFailureCode.PHYSICAL_FAILURE,
                            retryable=retryable,
                        ),
                    )
                    if not retryable:
                        execution.terminal = result
                    return result

            result = self._cleanup_result(
                plan,
                status=PhysicalCleanupStatus.SUCCEEDED,
                effects=tuple(execution.effects.values()),
            )
            execution.terminal = result
            return result

    async def repair(
        self,
        plan: "ProjectionRepairPlan",
    ) -> "ProjectionRepairResult":
        """Replay only current immutable inputs; fence stale/tombstoned sources."""

        if self._rebuild is None:
            return ProjectionRepairResult(
                status=ProjectionRepairStatus.FAILED,
                projection_run_id=plan.rebuild_plan.projection_run_id,
                projection_candidate_id=plan.rebuild_plan.projection_candidate.candidate_id,
                namespace_digest=plan.rebuild_plan.physical_namespace.namespace_digest,
                failure_code=ProjectionRepairFailureCode.PHYSICAL_FAILURE,
            )
        failure = _repair_fence(plan)
        if failure is not None:
            return ProjectionRepairResult(
                status=ProjectionRepairStatus.FENCED,
                projection_run_id=plan.rebuild_plan.projection_run_id,
                projection_candidate_id=plan.rebuild_plan.projection_candidate.candidate_id,
                namespace_digest=plan.rebuild_plan.physical_namespace.namespace_digest,
                failure_code=failure,
            )
        result = await self._rebuild.recover(plan.rebuild_plan)
        status = {
            ShadowRebuildState.COMPLETE: (
                ProjectionRepairStatus.REPLAYED
                if result.replayed
                else ProjectionRepairStatus.SUCCEEDED
            ),
            ShadowRebuildState.RECOVERABLE: ProjectionRepairStatus.RECOVERABLE,
            ShadowRebuildState.FAILED: ProjectionRepairStatus.FAILED,
        }.get(result.state, ProjectionRepairStatus.FAILED)
        return ProjectionRepairResult(
            status=status,
            projection_run_id=result.projection_run_id,
            projection_candidate_id=result.projection_candidate_id,
            namespace_digest=result.namespace_digest,
            rebuild=result,
            failure_code=(
                ProjectionRepairFailureCode(result.failure_code.value)
                if result.failure_code is not None
                and result.failure_code.value
                in {item.value for item in ProjectionRepairFailureCode}
                else None
            ),
        )

    @staticmethod
    def _effect_id(plan: PhysicalCleanupPlan, artifact: PhysicalArtifact) -> str:
        return f"{plan.cleanup_identity}:{artifact.artifact_kind.value}:{artifact.artifact_id}"

    @staticmethod
    def _cleanup_result(
        plan: PhysicalCleanupPlan,
        *,
        status: PhysicalCleanupStatus,
        effects: tuple[PhysicalCleanupEffect, ...] = (),
        failure: PhysicalCleanupFailure | None = None,
    ) -> PhysicalCleanupResult:
        deleted = tuple(
            effect.artifact_id
            for effect in sorted(effects, key=lambda item: item.artifact_id)
            if effect.status
            in {
                PhysicalCleanupEffectStatus.APPLIED,
                PhysicalCleanupEffectStatus.REPLAYED,
            }
        )
        deleted_keys = {
            (effect.artifact_kind, effect.artifact_id)
            for effect in effects
            if effect.status
            in {
                PhysicalCleanupEffectStatus.APPLIED,
                PhysicalCleanupEffectStatus.REPLAYED,
            }
        }
        pending = tuple(
            artifact.artifact_id
            for artifact in sorted(plan.artifacts, key=lambda item: item.artifact_id)
            if (artifact.artifact_kind, artifact.artifact_id) not in deleted_keys
        )
        return PhysicalCleanupResult(
            cleanup_identity=plan.cleanup_identity,
            cleanup_digest=plan.cleanup_digest,
            namespace_digest=plan.physical_namespace.namespace_digest,
            status=status,
            effects=tuple(sorted(effects, key=lambda item: item.artifact_id)),
            deleted_artifact_ids=deleted,
            pending_artifact_ids=pending,
            failure=failure,
        )


class ProjectionRepairPlan(DefinitionModel):
    """Immutable replay inputs for a repair attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-repair-plan:v1"] = "projection-repair-plan:v1"
    rebuild_plan: ShadowRebuildPlan
    source_revisions: tuple[ProjectionSourceRevision, ...] = ()

    @model_validator(mode="after")
    def validate_inputs(self) -> "ProjectionRepairPlan":
        source_by_object = {
            source.source_object_id: source for source in self.source_revisions
        }
        if len(source_by_object) != len(self.source_revisions):
            raise ValueError("repair inputs must contain one source revision per object")
        for item in self.rebuild_plan.items:
            if item.effect is not None:
                source = source_by_object.get(item.effect.source_object_id)
                if source is None or (
                    source.source_revision_id != item.effect.source_revision_id
                    or source.revision_digest != item.effect.revision_digest
                    or source.revision_number != item.effect.revision_number
                ):
                    raise ValueError("repair effect is not backed by its source revision")
            else:
                assert item.graph_request is not None
                for request_source in item.graph_request.result.source_revisions:
                    source = source_by_object.get(request_source.source_object_id)
                    if source is None or source != request_source:
                        raise ValueError("repair graph input is not backed by its source revision")
        return self


class ProjectionRepairResult(DefinitionModel):
    """Safe outcome of replaying a repair plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-repair-result:v1"] = "projection-repair-result:v1"
    status: ProjectionRepairStatus
    projection_run_id: UUID
    projection_candidate_id: UUID
    namespace_digest: str = Field(pattern=_DIGEST)
    rebuild: ShadowRebuildResult | None = None
    failure_code: ProjectionRepairFailureCode | None = None


def _repair_fence(plan: ProjectionRepairPlan) -> ProjectionRepairFailureCode | None:
    for source in plan.source_revisions:
        if source.status == "tombstoned":
            return ProjectionRepairFailureCode.TOMBSTONED_SOURCE
        if source.status != "current":
            return ProjectionRepairFailureCode.REVISION_FENCED
    return None


def _cleanup_scope_failure(
    plan: PhysicalCleanupPlan,
) -> PhysicalCleanupFailureCode | None:
    """Re-check Pydantic-frozen plans after callers use model_copy()."""

    try:
        PhysicalCleanupPlan.model_validate(plan.model_dump(mode="python"))
    except ValueError as error:
        if "workspace scope" in str(error) or "environment scope" in str(error):
            return PhysicalCleanupFailureCode.CROSS_WORKSPACE
        if "namespace scope" in str(error):
            return PhysicalCleanupFailureCode.NAMESPACE_UNAVAILABLE
        return PhysicalCleanupFailureCode.INTEGRITY_CONFLICT
    return None


def _same_cleanup_effect(
    effect: PhysicalCleanupEffect,
    requested: PhysicalCleanupEffect,
    artifact: PhysicalArtifact,
    plan: PhysicalCleanupPlan,
) -> bool:
    return (
        effect.effect_id == requested.effect_id
        and effect.artifact_kind is artifact.artifact_kind
        and effect.artifact_id == artifact.artifact_id
        and effect.data_id == artifact.data_id
        and effect.namespace_digest == plan.physical_namespace.namespace_digest
    )


async def _resolve_dataset_id(sdk: "CogneeSdk", dataset_name: str) -> UUID | None:
    for dataset in await sdk.list_datasets():
        if getattr(dataset, "name", None) == dataset_name:
            value = getattr(dataset, "id", None)
            if isinstance(value, UUID):
                return value
            if value is not None:
                return UUID(str(value))
    return None


class CogneeSdk(Protocol):
    async def delete_data(self, *, dataset_id: UUID, data_id: UUID, mode: str) -> object: ...

    async def list_datasets(self) -> Iterable[object]: ...


__all__ = [
    "CogneePhysicalCleanupLane",
    "DeterministicPhysicalCleanupStore",
    "PhysicalArtifactCleanupLane",
    "PhysicalCleanupEffect",
    "PhysicalCleanupEffectStatus",
    "PhysicalCleanupFailure",
    "PhysicalCleanupFailureCode",
    "PhysicalCleanupPlan",
    "PhysicalCleanupResult",
    "PhysicalCleanupStatus",
    "ProjectionRecoveryCoordinator",
    "ProjectionRepairFailureCode",
    "ProjectionRepairPlan",
    "ProjectionRepairResult",
    "ProjectionRepairStatus",
]
