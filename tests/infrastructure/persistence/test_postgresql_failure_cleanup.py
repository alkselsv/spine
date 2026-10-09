from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy.sql.dml import Insert
from sqlalchemy.exc import OperationalError

from spine.application.persistence.context import (
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    PersistenceUnavailableError,
    UnitOfWorkLifecycleError,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence


WORKSPACE_ID = UUID("45000000-0000-0000-0000-000000000001")


class ProviderConnectionError(Exception):
    sqlstate = "08006"


def connection_failure() -> OperationalError:
    return OperationalError(
        "protected SQL",
        {"secret": "protected"},
        ProviderConnectionError("provider detail"),
        connection_invalidated=True,
    )


class ControlledSession:
    def __init__(self) -> None:
        self.workspace_insert_error: BaseException | None = None
        self.commit_error: BaseException | None = None
        self.commit_started = asyncio.Event()
        self.allow_commit = asyncio.Event()
        self.block_commit = False
        self.rolled_back = False
        self.closed = False

    async def begin(self) -> None:
        return None

    async def execute(self, statement: object, parameters: object | None = None) -> object:
        if (
            isinstance(statement, Insert)
            and statement.table.name == "workspaces"
            and self.workspace_insert_error is not None
        ):
            raise self.workspace_insert_error
        return object()

    async def commit(self) -> None:
        self.commit_started.set()
        if self.block_commit:
            await self.allow_commit.wait()
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True


def persistence(session: ControlledSession) -> tuple[PostgreSQLPersistence, object]:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("45000000-0000-0000-0000-000000000002"),
        secret=b"issue-45-failure-cleanup-secret-01",
    )
    adapter = PostgreSQLPersistence(
        session_factory=lambda: session,  # type: ignore[arg-type]
        context_verifier=boundary,
    )
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=UUID("45000000-0000-0000-0000-000000000003"),
        purpose=PersistencePurpose("failure_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=UUID("45000000-0000-0000-0000-000000000004"),
    )
    return adapter, context


def candidate() -> Workspace:
    return Workspace(
        id=WORKSPACE_ID,
        slug="failure-cleanup",
        display_name="Failure Cleanup",
    )


@pytest.mark.asyncio
async def test_connection_invalidating_statement_failure_closes_unit_of_work() -> None:
    session = ControlledSession()
    session.workspace_insert_error = connection_failure()
    adapter, context = persistence(session)
    uow = adapter.tenant_uow_factory(context)  # type: ignore[arg-type]

    with pytest.raises(
        PersistenceUnavailableError,
        match="Persistence service is unavailable",
    ):
        async with uow:
            await uow.workspaces.add(candidate())

    assert session.rolled_back
    assert session.closed
    with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
        await uow.commit()


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_closes_and_redacts_provider_details() -> None:
    session = ControlledSession()
    session.commit_error = connection_failure()
    adapter, context = persistence(session)
    uow = adapter.tenant_uow_factory(context)  # type: ignore[arg-type]

    with pytest.raises(PersistenceUnavailableError) as raised:
        async with uow:
            await uow.workspaces.add(candidate())
            await uow.commit()

    assert str(WORKSPACE_ID) not in str(raised.value)
    assert "provider" not in str(raised.value)
    assert session.rolled_back
    assert session.closed


@pytest.mark.asyncio
async def test_cancelled_commit_rolls_back_and_closes_owned_session() -> None:
    session = ControlledSession()
    session.block_commit = True
    adapter, context = persistence(session)

    async def write() -> None:
        async with adapter.tenant_uow_factory(context) as uow:  # type: ignore[arg-type]
            await uow.workspaces.add(candidate())
            await uow.commit()

    task = asyncio.create_task(write())
    await session.commit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.rolled_back
    assert session.closed
