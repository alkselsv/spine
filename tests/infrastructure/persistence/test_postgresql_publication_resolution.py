from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from spine.application.persistence.context import EnvironmentScope
from spine.domain.projections import UnpublishedActivePublication
from spine.infrastructure.persistence.postgresql import (
    _PostgreSQLProjectionRepository,
)


class _Result:
    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> None:
        return None


class _UnitOfWork:
    scope = EnvironmentScope(
        workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
        environment_id=UUID("00000000-0000-0000-0000-000000000002"),
    )

    def __init__(self) -> None:
        self.statements: list[object] = []

    def _guard_active(self) -> None:
        return None

    async def _execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result()


@pytest.mark.asyncio
async def test_active_publication_resolution_uses_one_consistent_statement_snapshot() -> None:
    uow = _UnitOfWork()
    repository = _PostgreSQLProjectionRepository(uow)  # type: ignore[arg-type]

    resolved = await repository.resolve_active_publication("qa")

    assert isinstance(resolved, UnpublishedActivePublication)
    assert len(uow.statements) == 1
    compiled = str(
        uow.statements[0].compile(dialect=postgresql.dialect())  # type: ignore[union-attr]
    )
    assert "LEFT OUTER JOIN LATERAL" in compiled
    assert "EXISTS (SELECT 1" in compiled
    assert "active_projections.activation_generation" in compiled
