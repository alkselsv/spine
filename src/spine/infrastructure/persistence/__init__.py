"""Persistence adapters."""

from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence

__all__ = ["InMemoryPersistence", "PostgreSQLPersistence"]
