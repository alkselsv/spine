"""Map normalized Gmail history events into canonical state evidence."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from uuid import UUID

from spine.application.gmail_message_state import (
    GmailLabelObservation,
    GmailLabelObservationResult,
    GmailLabelState,
    GmailMessageEvidenceBinding,
    GmailMessageStateRepository,
    GmailMessageStateUnavailableError,
    GmailRelationshipEvidence,
    gmail_relationship_observations,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryResult,
)
from spine.domain.connections.email_scope import MailboxReadScope
from spine.domain.sources.gmail import GmailMessageKey
from spine.domain.sources.models import EmailRevisionMetadata


@dataclass(frozen=True, slots=True)
class GmailMessageStateWriter:
    """Persist normalized history and raw-revision relationship evidence."""

    repository: GmailMessageStateRepository
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    mailbox_binding_id: UUID

    def require_scope(self, scope: MailboxReadScope) -> None:
        """Fail closed when composition and admitted mailbox authority differ."""

        if (
            scope.workspace_id != self.workspace_id
            or scope.environment_id != self.environment_id
            or scope.connection_id != self.connection_id
            or scope.mailbox_binding_id != self.mailbox_binding_id
        ):
            raise GmailMessageStateUnavailableError()

    async def record_history(
        self, result: GmailHistoryResult, *, received_at: datetime
    ) -> tuple[GmailLabelObservationResult, ...]:
        receipts: list[GmailLabelObservationResult] = []
        for change in result.changes:
            if change.kind not in {
                GmailHistoryChangeKind.LABEL_ADDED,
                GmailHistoryChangeKind.LABEL_REMOVED,
            }:
                continue
            message_key = GmailMessageKey(
                connection_id=self.connection_id,
                mailbox_binding_id=self.mailbox_binding_id,
                message_id=change.message_id,
            )
            binding = await self.repository.resolve_current_binding(
                workspace_id=self.workspace_id,
                environment_id=self.environment_id,
                message_key=message_key,
            )
            if binding is None:
                raise GmailMessageStateUnavailableError()
            receipts.append(
                await self.repository.record_label(
                    gmail_label_observation(
                        binding, change, received_at=received_at
                    )
                )
            )
        return tuple(receipts)

    async def record_relationships(
        self,
        binding: GmailMessageEvidenceBinding,
        revision_evidence: EmailRevisionMetadata,
        *,
        received_at: datetime,
    ) -> tuple[GmailRelationshipEvidence, ...]:
        if (
            binding.workspace_id != self.workspace_id
            or binding.environment_id != self.environment_id
            or binding.message_key.connection_id != self.connection_id
            or binding.message_key.mailbox_binding_id != self.mailbox_binding_id
        ):
            raise GmailMessageStateUnavailableError()
        relationships = gmail_relationship_observations(
            binding,
            revision_evidence=revision_evidence,
            received_at=received_at,
        )
        persisted: list[GmailRelationshipEvidence] = []
        for relationship in relationships:
            persisted.append(await self.repository.record_relationship(relationship))
        return tuple(persisted)


def gmail_label_observation(
    binding: GmailMessageEvidenceBinding,
    change: GmailHistoryChange,
    *,
    received_at: datetime,
) -> GmailLabelObservation:
    """Map one normalized Gmail label event without creating content state."""

    if change.kind not in {
        GmailHistoryChangeKind.LABEL_ADDED,
        GmailHistoryChangeKind.LABEL_REMOVED,
    } or change.label_id is None:
        raise ValueError("only Gmail label history changes can create label state")
    if change.message_id != binding.message_key.message_id:
        raise ValueError("Gmail history change does not match the bound message")
    state = (
        GmailLabelState.PRESENT
        if change.kind is GmailHistoryChangeKind.LABEL_ADDED
        else GmailLabelState.ABSENT
    )
    return GmailLabelObservation.create(
        binding=binding,
        history_id=change.history_id,
        event_identity=change.event_id,
        label_id=change.label_id,
        state=state,
        received_at=received_at,
    )


__all__ = ["GmailMessageStateWriter", "gmail_label_observation"]
