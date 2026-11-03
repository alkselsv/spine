"""Map normalized Gmail history events into canonical state evidence."""

from __future__ import annotations

from datetime import datetime

from spine.application.gmail_message_state import (
    GmailLabelObservation,
    GmailLabelState,
    GmailMessageEvidenceBinding,
)
from spine.connectors.gmail_history import GmailHistoryChange, GmailHistoryChangeKind


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


__all__ = ["gmail_label_observation"]
