from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import AsyncContextManager
from uuid import UUID

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory


@dataclass(frozen=True)
class PersistenceAdapter:
    """Adapter-neutral inputs consumed by the shared persistence contract suite."""

    uow_factory: UnitOfWorkFactory
    workspace_context: Callable[[UUID], TrustedPersistenceContext]
    worker_workspace_context: Callable[[UUID], TrustedPersistenceContext]
    environment_context: Callable[[UUID, UUID], TrustedPersistenceContext]
    hold_transactions: Callable[[], AsyncContextManager[None]]
