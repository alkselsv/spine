from __future__ import annotations

from uuid import UUID

from spine.memory.contracts import (
    ProjectionOutcome,
    ProjectCommand,
    RebuildCommand,
    RemoveCommand,
    UpdateCommand,
)

from .adapter import ContextProjectionFactory
from .test_conformance import command_context


async def test_backend_implements_every_context_projection_operation(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    projection = context_projection_factory().projection
    context = command_context()

    results = (
        await projection.project(
            ProjectCommand(**context, command_identity="backend-project", command_digest="1" * 64)
        ),
        await projection.update(
            UpdateCommand(**context, command_identity="backend-update", command_digest="2" * 64)
        ),
        await projection.remove(
            RemoveCommand(**context, command_identity="backend-remove", command_digest="3" * 64)
        ),
        await projection.rebuild(
            RebuildCommand(**context, command_identity="backend-rebuild", command_digest="4" * 64)
        ),
    )

    assert all(result.outcome is ProjectionOutcome.SUCCEEDED for result in results)
    assert all(result.command_digest for result in results)
    assert all(result.command_identity for result in results)


async def test_backend_keeps_workspace_and_environment_in_the_command_scope(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    projection = context_projection_factory().projection
    command = ProjectCommand(
        **command_context(
            workspace_id=UUID("10000000-0000-4000-8000-000000000002"),
            environment_id=UUID("20000000-0000-4000-8000-000000000002"),
        ),
        command_identity="scoped-project",
        command_digest="5" * 64,
    )

    result = await projection.project(command)

    assert result.outcome is ProjectionOutcome.SUCCEEDED
