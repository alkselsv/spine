"""Process entry points for connectors, projections, agents, and workflows."""

from spine.workers.composition import (
    OutboxConsumerBinding,
    OutboxWorker,
    create_outbox_worker,
)

__all__ = ["OutboxConsumerBinding", "OutboxWorker", "create_outbox_worker"]
