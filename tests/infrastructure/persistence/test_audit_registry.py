from uuid import UUID

import pytest

from spine.application.diagnostics.audit import (
    AuditEventRegistry,
    AuditOutcome,
    CommandAuditPayload,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


def test_in_memory_persistence_rejects_unsealed_audit_registry() -> None:
    registry = AuditEventRegistry()
    registry.register(
        event_type="command.outcome",
        schema_version=1,
        payload_type=CommandAuditPayload,
        allowed_outcomes=frozenset({AuditOutcome.ACCEPTED}),
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID(int=1),
        secret=b"issue-63-sealed-audit-registry-secret",
    )

    with pytest.raises(RuntimeError, match="not sealed"):
        InMemoryPersistence(
            context_verifier=boundary,
            outbox_events=OutboxEventRegistry(),
            audit_events=registry,
        )
