"""Offline fake for the canonical projection-result mapping gateway."""

from __future__ import annotations

from uuid import UUID

from spine.application.projection_result_mapping import (
    CanonicalProjectionMappingContext,
    ProjectionMappingConflictError,
    ProjectionMappingError,
    ProjectionMappingHandoff,
    ProjectionMappingResult,
)
from spine.memory.contracts import ProjectionCommand


class DeterministicCanonicalProjectionGateway:
    """In-memory canonical boundary fake for mapping conformance tests only."""

    def __init__(self, contexts: tuple[CanonicalProjectionMappingContext, ...] = ()) -> None:
        self._contexts: dict[
            tuple[UUID, UUID, UUID, UUID, str], CanonicalProjectionMappingContext
        ] = {}
        self._mapped: dict[tuple[UUID, UUID, str], ProjectionMappingHandoff] = {}
        for context in contexts:
            self.add_context(context)

    def add_context(self, context: CanonicalProjectionMappingContext) -> None:
        self._contexts[
            (
                context.run.workspace_id,
                context.run.environment_id,
                context.run.projection_run_id,
                context.partition.projection_partition_id,
                context.partition.expected_command_identity,
            )
        ] = context

    async def is_recorded(self, command: ProjectionCommand) -> bool:
        existing = self._mapped.get(_mapping_key(command))
        if existing is None:
            return False
        _require_same_command(existing.command, command)
        return True

    async def replay_for(self, command: ProjectionCommand) -> ProjectionMappingResult | None:
        existing = self._mapped.get(_mapping_key(command))
        if existing is None:
            return None
        _require_same_command(existing.command, command)
        return ProjectionMappingResult(handoff=existing, replay=True)

    async def resolve_context(
        self, command: ProjectionCommand
    ) -> CanonicalProjectionMappingContext:
        context = self._contexts.get(
            (
                command.workspace_id,
                command.environment_id,
                command.projection_run_id,
                command.projection_candidate.partition_id,
                command.command_identity,
            )
        )
        if context is None:
            raise ProjectionMappingError("canonical projection context is missing")
        return context

    async def accept(self, handoff: ProjectionMappingHandoff) -> ProjectionMappingResult:
        key = _mapping_key(handoff.command)
        existing = self._mapped.get(key)
        if existing is not None:
            _require_same_command(existing.command, handoff.command)
            return ProjectionMappingResult(handoff=existing, replay=True)
        self._mapped[key] = handoff
        return ProjectionMappingResult(handoff=handoff, replay=False)


def _mapping_key(command: ProjectionCommand) -> tuple[UUID, UUID, str]:
    return command.workspace_id, command.environment_id, command.command_identity


def _require_same_command(existing: ProjectionCommand, incoming: ProjectionCommand) -> None:
    if existing.command_digest != incoming.command_digest:
        raise ProjectionMappingConflictError("projection mapping digest conflict")
    if existing != incoming:
        raise ProjectionMappingConflictError("projection mapping command context conflict")


__all__ = ["DeterministicCanonicalProjectionGateway"]
