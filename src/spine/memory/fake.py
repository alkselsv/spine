"""Deterministic provider-neutral ContextProjection fakes."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from .contracts import (
    ContextProjection,
    ProjectionChunk,
    ProjectionCommand,
    ProjectionDiagnostic,
    ProjectionEffect,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionEffectStore,
    ProjectionEntity,
    ProjectionFailure,
    ProjectionFailureCode,
    ProjectionCount,
    ProjectionLifecycle,
    ProjectionOutcome,
    ProjectionNamespace,
    ProjectionNamespaceState,
    ProjectionRelation,
    ProjectionRecovery,
    ProjectionResult,
    ProjectionRetrievalResult,
    ProjectCommand,
    RebuildCommand,
    RemoveCommand,
    RetrieveCommand,
    UpdateCommand,
)
from .namespace import (
    NamespaceRoutingError,
    ProjectionNamespaceContext,
    ProjectionNamespaceRouter,
)


class DeterministicPhysicalStore:
    """A provider-neutral physical store with deterministic failure injection.

    ``fail_once_for`` and ``fail_for`` use ``<command_identity>:<revision_number>``
    selectors. This keeps tests independent of provider IDs while allowing a
    partial write and its recovery to be observed explicitly.
    """

    def __init__(
        self,
        *,
        fail_once_for: set[str] | tuple[str, ...] = (),
        fail_for: set[str] | tuple[str, ...] = (),
    ) -> None:
        self._fail_once_for = set(fail_once_for)
        self._fail_for = set(fail_for)
        self._attempts: defaultdict[str, int] = defaultdict(int)
        self._applied_effects: set[str] = set()

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        selector = f"{effect.command_identity}:{effect.revision_number}"
        self._attempts[selector] += 1
        if effect.effect_id in self._applied_effects:
            return effect.model_copy(
                update={"status": ProjectionEffectStatus.REPLAYED, "failure_code": None}
            )
        if selector in self._fail_for or (
            selector in self._fail_once_for and self._attempts[selector] == 1
        ):
            return effect.model_copy(
                update={
                    "status": ProjectionEffectStatus.FAILED,
                    "failure_code": ProjectionFailureCode.PHYSICAL_FAILURE,
                }
            )
        self._applied_effects.add(effect.effect_id)
        return effect.model_copy(
            update={"status": ProjectionEffectStatus.APPLIED, "failure_code": None}
        )

    def attempts_for(self, selector: str) -> int:
        """Return deterministic physical-attempt count for a test selector."""

        return self._attempts[selector]


@dataclass
class _RevisionFence:
    revision_number: int
    source_revision_id: UUID
    revision_digest: str
    tombstoned: bool


@dataclass
class _Execution:
    command: ProjectionCommand
    effects: tuple[ProjectionEffect, ...]
    attempt: int = 0
    terminal_result: ProjectionResult | None = None


class DeterministicContextProjection(ContextProjection):
    """In-memory implementation of the public projection port.

    The fake models command identity/digest idempotency, source revision
    fencing, tombstones and recoverable physical partial failure. It never
    owns canonical currentness, access control, publication or receipt
    authority; those facts arrive on the command boundary.
    """

    def __init__(
        self,
        physical_store: ProjectionEffectStore | None = None,
        namespace_router: ProjectionNamespaceRouter | None = None,
    ) -> None:
        self._commands: list[ProjectionCommand] = []
        self._executions: dict[tuple[UUID, UUID, str, str, str], _Execution] = {}
        self._retrieval_results: dict[
            tuple[UUID, UUID, str, str, str], ProjectionRetrievalResult
        ] = {}
        self._retrieval: dict[
            tuple[UUID, UUID, str | None, str | None],
            tuple[
                tuple[ProjectionChunk, ...],
                tuple[ProjectionEntity, ...],
                tuple[ProjectionRelation, ...],
            ],
        ] = {}
        self._revision_fences: dict[tuple[UUID, UUID, str, str, UUID], _RevisionFence] = {}
        self._physical_store = physical_store or DeterministicPhysicalStore()
        self._namespace_router = namespace_router or ProjectionNamespaceRouter()
        self._lock = asyncio.Lock()

    @property
    def recorded_commands(self) -> tuple[ProjectionCommand, ...]:
        return tuple(self._commands)

    def seed_retrieval(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        chunks: tuple[ProjectionChunk, ...] = (),
        entities: tuple[ProjectionEntity, ...] = (),
        relations: tuple[ProjectionRelation, ...] = (),
        physical_namespace: ProjectionNamespace | str | None = None,
    ) -> None:
        self._retrieval[self._seed_key(workspace_id, environment_id, physical_namespace)] = (
            chunks,
            entities,
            relations,
        )

    async def project(self, command: ProjectCommand) -> ProjectionResult:
        return await self._execute(command)

    async def update(self, command: UpdateCommand) -> ProjectionResult:
        return await self._execute(command)

    async def remove(self, command: RemoveCommand) -> ProjectionResult:
        return await self._execute(command)

    async def rebuild(self, command: RebuildCommand) -> ProjectionResult:
        return await self._execute(command)

    async def retrieve(self, command: RetrieveCommand) -> ProjectionRetrievalResult:
        async with self._lock:
            key = self._key(command)
            previous = self._retrieval_results.get(key)
            if previous is not None:
                if previous.command_digest != command.command_digest:
                    return self._retrieval_failure(
                        command, ProjectionFailureCode.INTEGRITY_CONFLICT
                    )
                return previous.model_copy(update={"outcome": ProjectionOutcome.REPLAYED})

            self._commands.append(command)
            namespace_failure = self._namespace_failure(command)
            unsafe = self._unsafe_source_status(command)
            if namespace_failure is not None:
                result = self._retrieval_failure(command, namespace_failure)
            elif unsafe is not None:
                result = self._retrieval_failure(command, unsafe)
            else:
                seeded = self._retrieval.get(
                    self._seed_key(
                        command.workspace_id,
                        command.environment_id,
                        command.physical_namespace,
                    )
                )
                if seeded is None:
                    seeded = self._retrieval.get(
                        self._seed_key(command.workspace_id, command.environment_id, None)
                    )
                if seeded is None:
                    result = self._retrieval_failure(
                        command, ProjectionFailureCode.MISSING_EVIDENCE
                    )
                else:
                    chunks, entities, relations = seeded
                    limited_chunks = chunks[: command.limit]
                    evidence = tuple(
                        reference
                        for item in (*limited_chunks, *entities, *relations)
                        for reference in item.evidence
                    )
                    unsafe_evidence = next(
                        (
                            reference.status
                            for reference in evidence
                            if reference.status != "valid"
                        ),
                        None,
                    )
                    if unsafe_evidence is not None:
                        result = self._retrieval_failure(
                            command, self._failure_code_for_status(unsafe_evidence)
                        )
                    elif any(
                        reference.workspace_id != command.workspace_id
                        or reference.environment_id != command.environment_id
                        for item in (*limited_chunks, *entities, *relations)
                        for reference in item.evidence
                    ):
                        result = self._retrieval_failure(
                            command, ProjectionFailureCode.MISSING_EVIDENCE
                        )
                    else:
                        result = ProjectionRetrievalResult(
                            outcome=ProjectionOutcome.SUCCEEDED,
                            command_identity=command.command_identity,
                            command_digest=command.command_digest,
                            chunks=limited_chunks,
                            entities=entities,
                            relations=relations,
                            evidence=evidence,
                        )
            self._retrieval_results[key] = result
            return result

    async def _execute(self, command: ProjectionCommand) -> ProjectionResult:
        async with self._lock:
            key = self._key(command)
            execution = self._executions.get(key)
            if execution is not None:
                if (
                    execution.command.command_digest != command.command_digest
                    or execution.command.operation != command.operation
                ):
                    return self._failure_result(
                        command,
                        ProjectionFailureCode.INTEGRITY_CONFLICT,
                    )
                if execution.terminal_result is not None:
                    if execution.terminal_result.lifecycle in {
                        ProjectionLifecycle.SUCCEEDED,
                        ProjectionLifecycle.RECOVERED,
                    }:
                        return self._replayed(execution.terminal_result)
                    return execution.terminal_result
                fenced = self._preflight(command)
                if fenced is not None:
                    result = self._failure_result(command, fenced)
                    execution.terminal_result = result
                    return result
                return await self._continue_execution(execution)

            self._commands.append(command)
            fenced = self._preflight(command)
            if fenced is not None:
                result = self._failure_result(command, fenced)
                self._executions[key] = _Execution(
                    command=command,
                    effects=(),
                    terminal_result=result,
                )
                return result

            execution = _Execution(
                command=command,
                effects=self._effects_for(command),
            )
            self._executions[key] = execution
            return await self._continue_execution(execution)

    async def _continue_execution(self, execution: _Execution) -> ProjectionResult:
        execution.attempt += 1
        completed: list[ProjectionEffect] = []
        failed = False
        for effect in execution.effects:
            if effect.status in {
                ProjectionEffectStatus.APPLIED,
                ProjectionEffectStatus.REPLAYED,
            }:
                completed.append(effect)
                continue
            try:
                applied = await self._physical_store.apply(effect)
            except Exception:
                applied = effect.model_copy(
                    update={
                        "status": ProjectionEffectStatus.FAILED,
                        "failure_code": ProjectionFailureCode.PHYSICAL_FAILURE,
                    }
                )
            completed.append(applied)
            if applied.status not in {
                ProjectionEffectStatus.APPLIED,
                ProjectionEffectStatus.REPLAYED,
            }:
                failed = True
        execution.effects = tuple(completed)

        if failed:
            pending = tuple(
                effect.effect_id
                for effect in execution.effects
                if effect.status
                not in {
                    ProjectionEffectStatus.APPLIED,
                    ProjectionEffectStatus.REPLAYED,
                }
            )
            return ProjectionResult(
                operation=execution.command.operation,
                outcome=ProjectionOutcome.FAILED,
                command_identity=execution.command.command_identity,
                command_digest=execution.command.command_digest,
                lifecycle=ProjectionLifecycle.RECOVERABLE,
                effects=execution.effects,
                recovery=ProjectionRecovery(
                    attempt=execution.attempt,
                    pending_effect_ids=pending,
                ),
                failure=self._failure(ProjectionFailureCode.PHYSICAL_FAILURE),
            )

        result = ProjectionResult(
            operation=execution.command.operation,
            outcome=ProjectionOutcome.SUCCEEDED,
            command_identity=execution.command.command_identity,
            command_digest=execution.command.command_digest,
            lifecycle=(
                ProjectionLifecycle.RECOVERED
                if execution.attempt > 1
                else ProjectionLifecycle.SUCCEEDED
            ),
            artifact_reference=self._artifact_reference(execution.command),
            effects=execution.effects,
            counts=(
                ProjectionCount(
                    name="source_revisions",
                    value=len(execution.command.source_revisions),
                ),
            ),
        )
        execution.terminal_result = result
        return result

    def _preflight(self, command: ProjectionCommand) -> ProjectionFailureCode | None:
        namespace_failure = self._namespace_failure(command)
        if namespace_failure is not None:
            return namespace_failure

        for source in command.source_revisions:
            known = self._revision_fences.get(
                self._fence_key(command, source.source_object_id)
            )
            if known is not None and source.revision_number < known.revision_number:
                return ProjectionFailureCode.REVISION_FENCED
            if (
                known is not None
                and source.revision_number == known.revision_number
                and (
                    source.source_revision_id != known.source_revision_id
                    or source.revision_digest != known.revision_digest
                )
            ):
                return ProjectionFailureCode.REVISION_FENCED

            if (
                known is not None
                and known.tombstoned
                and source.revision_number <= known.revision_number
                and not (
                    command.operation.value == "remove"
                    and source.status == "tombstoned"
                    and source.source_revision_id == known.source_revision_id
                )
            ):
                return ProjectionFailureCode.TOMBSTONED_SOURCE

            if source.status != "current":
                if command.operation.value == "remove" and source.status == "tombstoned":
                    continue
                if source.status == "tombstoned":
                    self._revision_fences[
                        self._fence_key(command, source.source_object_id)
                    ] = _RevisionFence(
                        revision_number=source.revision_number,
                        source_revision_id=source.source_revision_id,
                        revision_digest=source.revision_digest,
                        tombstoned=True,
                    )
                return self._failure_code_for_status(source.status)

        for source in command.source_revisions:
            self._revision_fences[
                self._fence_key(command, source.source_object_id)
            ] = _RevisionFence(
                revision_number=source.revision_number,
                source_revision_id=source.source_revision_id,
                revision_digest=source.revision_digest,
                tombstoned=source.status == "tombstoned",
            )
        return None

    def _effects_for(self, command: ProjectionCommand) -> tuple[ProjectionEffect, ...]:
        action = (
            ProjectionEffectAction.REMOVE_SOURCE
            if command.operation.value == "remove"
            else ProjectionEffectAction.UPSERT_SOURCE
        )
        return tuple(
            ProjectionEffect(
                effect_id=(
                    f"{command.workspace_id.hex}:{command.environment_id.hex}:"
                    f"{self._namespace_key(command)}:{command.command_identity}:"
                    f"{source.source_object_id.hex}:"
                    f"{source.revision_number}:{action.value}"
                ),
                operation=command.operation,
                action=action,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                command_identity=command.command_identity,
                command_digest=command.command_digest,
                projection_candidate=command.projection_candidate,
                projection_config=command.projection_config,
                canonical_boundary=command.canonical_boundary,
                physical_namespace=command.physical_namespace,
                source_object_id=source.source_object_id,
                source_revision_id=source.source_revision_id,
                revision_digest=source.revision_digest,
                revision_number=source.revision_number,
            )
            for source in command.source_revisions
        )

    @staticmethod
    def _replayed(result: ProjectionResult) -> ProjectionResult:
        return result.model_copy(
            update={
                "outcome": ProjectionOutcome.REPLAYED,
                "lifecycle": ProjectionLifecycle.REPLAYED,
                "effects": tuple(
                    effect.model_copy(update={"status": ProjectionEffectStatus.REPLAYED})
                    for effect in result.effects
                ),
            }
        )

    def _failure_result(
        self,
        command: ProjectionCommand,
        code: ProjectionFailureCode,
    ) -> ProjectionResult:
        return ProjectionResult(
            operation=command.operation,
            outcome=self._outcome_for_code(code),
            command_identity=command.command_identity,
            command_digest=command.command_digest,
            lifecycle=(
                ProjectionLifecycle.FENCED
                if code is ProjectionFailureCode.REVISION_FENCED
                else ProjectionLifecycle.FAILED
            ),
            failure=self._failure(
                code,
                namespace_digest=(
                    command.physical_namespace.namespace_digest
                    if code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
                    else None
                ),
            ),
        )

    def _retrieval_failure(
        self,
        command: RetrieveCommand,
        code: ProjectionFailureCode,
    ) -> ProjectionRetrievalResult:
        return ProjectionRetrievalResult(
            outcome=self._outcome_for_code(code),
            command_identity=command.command_identity,
            command_digest=command.command_digest,
            failure=self._failure(
                code,
                namespace_digest=(
                    command.physical_namespace.namespace_digest
                    if code is ProjectionFailureCode.NAMESPACE_UNAVAILABLE
                    else None
                ),
            ),
        )

    @staticmethod
    def _key(command: ProjectionCommand) -> tuple[UUID, UUID, str, str, str]:
        return (
            command.workspace_id,
            command.environment_id,
            command.physical_namespace.namespace,
            command.physical_namespace.namespace_digest,
            command.command_identity,
        )

    @staticmethod
    def _seed_key(
        workspace_id: UUID,
        environment_id: UUID,
        physical_namespace: ProjectionNamespace | str | None,
    ) -> tuple[UUID, UUID, str | None, str | None]:
        if isinstance(physical_namespace, ProjectionNamespace):
            return (
                workspace_id,
                environment_id,
                physical_namespace.namespace,
                physical_namespace.namespace_digest,
            )
        return workspace_id, environment_id, physical_namespace, None

    def _namespace_failure(
        self,
        command: ProjectionCommand,
    ) -> ProjectionFailureCode | None:
        try:
            context = ProjectionNamespaceContext.from_command(command)
            self._namespace_router.validate(command.physical_namespace, context)
        except (NamespaceRoutingError, ValueError):
            return ProjectionFailureCode.NAMESPACE_UNAVAILABLE

        state = command.physical_namespace.state
        if state in {
            ProjectionNamespaceState.FAILED,
            ProjectionNamespaceState.RETIRED,
        }:
            return ProjectionFailureCode.NAMESPACE_UNAVAILABLE
        if (
            command.operation.value == "retrieve"
            and not self._namespace_router.is_ordinary_retrieval(command.physical_namespace)
        ):
            return ProjectionFailureCode.NAMESPACE_UNAVAILABLE
        return None

    @staticmethod
    def _namespace_key(command: ProjectionCommand) -> str:
        payload = (
            f"{command.physical_namespace.namespace}\0"
            f"{command.physical_namespace.namespace_digest}"
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    @classmethod
    def _fence_key(
        cls,
        command: ProjectionCommand,
        source_object_id: UUID,
    ) -> tuple[UUID, UUID, str, str, UUID]:
        return (
            command.workspace_id,
            command.environment_id,
            command.physical_namespace.namespace,
            command.physical_namespace.namespace_digest,
            source_object_id,
        )

    @staticmethod
    def _artifact_reference(command: ProjectionCommand) -> str:
        return f"artifact:{command.projection_candidate.candidate_id.hex}:{command.command_identity}"

    @staticmethod
    def _unsafe_source_status(command: ProjectionCommand) -> ProjectionFailureCode | None:
        for source in command.source_revisions:
            if source.status != "current":
                return DeterministicContextProjection._failure_code_for_status(source.status)
        return None

    @staticmethod
    def _failure_code_for_status(status: str) -> ProjectionFailureCode:
        return {
            "missing": ProjectionFailureCode.MISSING_EVIDENCE,
            "stale": ProjectionFailureCode.STALE_EVIDENCE,
            "tombstoned": ProjectionFailureCode.TOMBSTONED_SOURCE,
            "ambiguous": ProjectionFailureCode.AMBIGUOUS_EVIDENCE,
            "incomplete": ProjectionFailureCode.INCOMPLETE_EVIDENCE,
        }[status]

    @staticmethod
    def _outcome_for_code(code: ProjectionFailureCode) -> ProjectionOutcome:
        return {
            ProjectionFailureCode.INTEGRITY_CONFLICT: ProjectionOutcome.FAILED,
            ProjectionFailureCode.MISSING_EVIDENCE: ProjectionOutcome.MISSING,
            ProjectionFailureCode.STALE_EVIDENCE: ProjectionOutcome.STALE,
            ProjectionFailureCode.REVISION_FENCED: ProjectionOutcome.STALE,
            ProjectionFailureCode.TOMBSTONED_SOURCE: ProjectionOutcome.TOMBSTONED,
            ProjectionFailureCode.AMBIGUOUS_EVIDENCE: ProjectionOutcome.AMBIGUOUS,
            ProjectionFailureCode.INCOMPLETE_EVIDENCE: ProjectionOutcome.INCOMPLETE,
            ProjectionFailureCode.NAMESPACE_UNAVAILABLE: ProjectionOutcome.FAILED,
            ProjectionFailureCode.PHYSICAL_FAILURE: ProjectionOutcome.FAILED,
        }[code]

    @staticmethod
    def _failure(
        code: ProjectionFailureCode,
        *,
        namespace_digest: str | None = None,
    ) -> ProjectionFailure:
        return ProjectionFailure(
            code=code,
            retryable=code
            in {
                ProjectionFailureCode.MISSING_EVIDENCE,
                ProjectionFailureCode.NAMESPACE_UNAVAILABLE,
                ProjectionFailureCode.PHYSICAL_FAILURE,
            },
            safe_detail=ProjectionDiagnostic(namespace_digest=namespace_digest),
        )


__all__ = ["DeterministicContextProjection", "DeterministicPhysicalStore"]
