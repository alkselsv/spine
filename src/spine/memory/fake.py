"""Deterministic provider-neutral ContextProjection fake for contract tests."""

from __future__ import annotations

from typing import TypeVar
from uuid import UUID

from .contracts import (
    ContextProjection,
    ProjectionChunk,
    ProjectionCommand,
    ProjectionDiagnostic,
    ProjectionEntity,
    ProjectionFailure,
    ProjectionFailureCode,
    ProjectionCount,
    ProjectionOutcome,
    ProjectionRelation,
    ProjectionResult,
    ProjectionRetrievalResult,
    ProjectCommand,
    RebuildCommand,
    RemoveCommand,
    RetrieveCommand,
    UpdateCommand,
)


_Result = TypeVar("_Result", ProjectionResult, ProjectionRetrievalResult)


class DeterministicContextProjection(ContextProjection):
    """Small in-memory implementation of the public adapter port.

    The fake deliberately models replay and safe outcomes, but does not claim
    canonical currentness, access control, publication or receipt authority.
    Its seeded retrieval data is keyed by workspace and environment so a test
    cannot accidentally observe another tenant's projection.
    """

    def __init__(self) -> None:
        self._commands: list[ProjectionCommand] = []
        self._results: dict[tuple[UUID, UUID, str], _Result] = {}
        self._retrieval: dict[
            tuple[UUID, UUID],
            tuple[
                tuple[ProjectionChunk, ...],
                tuple[ProjectionEntity, ...],
                tuple[ProjectionRelation, ...],
            ],
        ] = {}

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
    ) -> None:
        self._retrieval[(workspace_id, environment_id)] = (chunks, entities, relations)

    async def project(self, command: ProjectCommand) -> ProjectionResult:
        return self._execute(command)

    async def update(self, command: UpdateCommand) -> ProjectionResult:
        return self._execute(command)

    async def remove(self, command: RemoveCommand) -> ProjectionResult:
        return self._execute(command)

    async def rebuild(self, command: RebuildCommand) -> ProjectionResult:
        return self._execute(command)

    async def retrieve(self, command: RetrieveCommand) -> ProjectionRetrievalResult:
        key = self._key(command)
        previous = self._results.get(key)
        if previous is not None:
            if previous.command_digest != command.command_digest:
                return self._retrieval_failure(command, ProjectionFailureCode.INTEGRITY_CONFLICT)
            return previous.model_copy(update={"outcome": ProjectionOutcome.REPLAYED})

        self._commands.append(command)
        unsafe = self._unsafe_source_status(command)
        if unsafe is not None:
            result = self._retrieval_failure(command, unsafe)
        else:
            seeded = self._retrieval.get((command.workspace_id, command.environment_id))
            if seeded is None:
                result = self._retrieval_failure(command, ProjectionFailureCode.MISSING_EVIDENCE)
                self._results[key] = result
                return result
            chunks, entities, relations = seeded
            limited_chunks = chunks[: command.limit]
            evidence = tuple(
                reference
                for item in (*limited_chunks, *entities, *relations)
                for reference in item.evidence
            )
            unsafe_evidence = next(
                (reference.status for reference in evidence if reference.status != "valid"),
                None,
            )
            if unsafe_evidence is not None:
                result = self._retrieval_failure(
                    command,
                    self._failure_code_for_status(unsafe_evidence),
                )
            elif any(
                reference.workspace_id != command.workspace_id
                or reference.environment_id != command.environment_id
                for item in (*limited_chunks, *entities, *relations)
                for reference in item.evidence
            ):
                result = self._retrieval_failure(command, ProjectionFailureCode.MISSING_EVIDENCE)
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
        self._results[key] = result
        return result

    def _execute(self, command: ProjectionCommand) -> ProjectionResult:
        key = self._key(command)
        previous = self._results.get(key)
        if previous is not None:
            if previous.command_digest != command.command_digest:
                return self._failure_result(command, ProjectionFailureCode.INTEGRITY_CONFLICT)
            return previous.model_copy(update={"outcome": ProjectionOutcome.REPLAYED})

        self._commands.append(command)
        unsafe = self._unsafe_source_status(command)
        if unsafe is None:
            result = ProjectionResult(
                operation=command.operation,
                outcome=ProjectionOutcome.SUCCEEDED,
                command_identity=command.command_identity,
                command_digest=command.command_digest,
                artifact_reference=self._artifact_reference(command),
                counts=(
                    ProjectionCount(
                        name="source_revisions",
                        value=len(command.source_revisions),
                    ),
                ),
            )
        else:
            result = self._failure_result(command, unsafe)
        self._results[key] = result
        return result

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
            failure=self._failure(code),
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
            failure=self._failure(code),
        )

    @staticmethod
    def _key(command: ProjectionCommand) -> tuple[UUID, UUID, str]:
        return (command.workspace_id, command.environment_id, command.command_identity)

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
            ProjectionFailureCode.TOMBSTONED_SOURCE: ProjectionOutcome.TOMBSTONED,
            ProjectionFailureCode.AMBIGUOUS_EVIDENCE: ProjectionOutcome.AMBIGUOUS,
            ProjectionFailureCode.INCOMPLETE_EVIDENCE: ProjectionOutcome.INCOMPLETE,
            ProjectionFailureCode.NAMESPACE_UNAVAILABLE: ProjectionOutcome.FAILED,
        }[code]

    @staticmethod
    def _failure(code: ProjectionFailureCode) -> ProjectionFailure:
        return ProjectionFailure(
            code=code,
            retryable=code
            in {
                ProjectionFailureCode.MISSING_EVIDENCE,
                ProjectionFailureCode.NAMESPACE_UNAVAILABLE,
            },
            safe_detail=ProjectionDiagnostic(),
        )


__all__ = ["DeterministicContextProjection"]
