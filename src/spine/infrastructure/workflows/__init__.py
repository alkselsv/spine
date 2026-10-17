"""Infrastructure adapters for durable workflow activities."""

from spine.infrastructure.workflows.ask_admission import (
    PersistenceAskAdmissionCommandSource,
)

__all__ = ["PersistenceAskAdmissionCommandSource"]
