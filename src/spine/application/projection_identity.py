"""Stable server-owned identities for projection delivery and publication."""

from uuid import NAMESPACE_URL, UUID, uuid5


def projection_recovery_entity_id(command_id: UUID, role: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:projection-recovery:{command_id}:{role}")


def projection_publication_entity_id(run_id: UUID, role: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:projection-publication:v1:{run_id}:{role}")


def projection_successor_intent_id(command_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:projection-successor:{command_id}:intent")


__all__ = [
    "projection_publication_entity_id",
    "projection_recovery_entity_id",
    "projection_successor_intent_id",
]
