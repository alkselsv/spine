from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from spine.application.gmail_message_state import (
    GmailLabelDisposition,
    GmailMessageEvidenceBinding,
    GmailMessageStateRepository,
    GmailMessageStateUnavailableError,
    gmail_relationship_observations,
)
from spine.connectors.gmail_history import GmailHistoryChange, GmailHistoryChangeKind
from spine.connectors.gmail_message_state import gmail_label_observation
from spine.domain.sources.models import EmailRevisionMetadata


RECEIVED_AT = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class GmailMessageStateRepositoryScenario:
    repository: GmailMessageStateRepository
    binding: GmailMessageEvidenceBinding
    revision_evidence: EmailRevisionMetadata
    advance_current: Callable[[], Awaitable[None]] | None = None
    tombstone_current: Callable[[], Awaitable[None]] | None = None
    restore_current: Callable[[], Awaitable[None]] | None = None


def _change(history_id: str, kind: GmailHistoryChangeKind) -> GmailHistoryChange:
    return GmailHistoryChange(
        history_id=history_id,
        kind=kind,
        message_id="gmail-message-840",
        label_id="INBOX",
    )


async def assert_gmail_label_convergence_contract(
    scenario: GmailMessageStateRepositoryScenario,
) -> None:
    latest = gmail_label_observation(
        scenario.binding,
        _change("102", GmailHistoryChangeKind.LABEL_REMOVED),
        received_at=RECEIVED_AT,
    )
    older = gmail_label_observation(
        scenario.binding,
        _change("101", GmailHistoryChangeKind.LABEL_ADDED),
        received_at=RECEIVED_AT - timedelta(seconds=1),
    )

    applied = await scenario.repository.record_label(latest)
    duplicate = await scenario.repository.record_label(
        latest.model_copy(update={"received_at": RECEIVED_AT + timedelta(seconds=1)})
    )
    out_of_order = await scenario.repository.record_label(older)

    assert applied.disposition is GmailLabelDisposition.APPLIED
    assert duplicate.disposition is GmailLabelDisposition.DUPLICATE
    assert out_of_order.disposition is GmailLabelDisposition.OUT_OF_ORDER
    assert applied.current.present is False
    assert duplicate.current == out_of_order.current == applied.current
    assert await scenario.repository.list_label_observations(scenario.binding) == (
        older,
        latest,
    )


async def assert_gmail_relationship_replay_contract(
    scenario: GmailMessageStateRepositoryScenario,
) -> None:
    relationships = gmail_relationship_observations(
        scenario.binding,
        revision_evidence=scenario.revision_evidence,
        received_at=RECEIVED_AT,
    )

    first = tuple(
        [await scenario.repository.record_relationship(item) for item in relationships]
    )
    replayed = tuple(
        [
            await scenario.repository.record_relationship(
                item.model_copy(
                    update={"received_at": RECEIVED_AT + timedelta(seconds=1)}
                )
            )
            for item in relationships
        ]
    )

    assert replayed == first == relationships
    stored = await scenario.repository.list_relationships(scenario.binding)
    assert stored == relationships


async def assert_gmail_tenant_denial_contract(
    scenario: GmailMessageStateRepositoryScenario,
) -> None:
    observation = gmail_label_observation(
        scenario.binding,
        _change("103", GmailHistoryChangeKind.LABEL_ADDED),
        received_at=RECEIVED_AT,
    )

    with pytest.raises(
        GmailMessageStateUnavailableError, match="unavailable in this tenant"
    ) as error:
        await scenario.repository.record_label(observation)

    assert scenario.binding.message_key.message_id not in str(error.value)


async def assert_gmail_stale_revision_contract(
    scenario: GmailMessageStateRepositoryScenario,
) -> None:
    assert scenario.advance_current is not None
    accepted = gmail_label_observation(
        scenario.binding,
        _change("104", GmailHistoryChangeKind.LABEL_ADDED),
        received_at=RECEIVED_AT,
    )
    await scenario.repository.record_label(accepted)
    await scenario.advance_current()
    try:
        stale = gmail_label_observation(
            scenario.binding,
            _change("105", GmailHistoryChangeKind.LABEL_REMOVED),
            received_at=RECEIVED_AT + timedelta(seconds=1),
        )

        with pytest.raises(GmailMessageStateUnavailableError):
            await scenario.repository.record_label(stale)
        replay = await scenario.repository.record_label(
            accepted.model_copy(
                update={"received_at": RECEIVED_AT + timedelta(seconds=2)}
            )
        )

        assert replay.disposition is GmailLabelDisposition.DUPLICATE
        assert replay.persisted == accepted
    finally:
        if scenario.restore_current is not None:
            await scenario.restore_current()


async def assert_gmail_tombstone_resolution_contract(
    scenario: GmailMessageStateRepositoryScenario,
) -> None:
    assert scenario.tombstone_current is not None
    await scenario.tombstone_current()
    try:
        resolved = await scenario.repository.resolve_current_binding(
            workspace_id=scenario.binding.workspace_id,
            environment_id=scenario.binding.environment_id,
            message_key=scenario.binding.message_key,
        )

        assert resolved is None
    finally:
        if scenario.restore_current is not None:
            await scenario.restore_current()


__all__ = [
    "GmailMessageStateRepositoryScenario",
    "RECEIVED_AT",
    "assert_gmail_label_convergence_contract",
    "assert_gmail_relationship_replay_contract",
    "assert_gmail_stale_revision_contract",
    "assert_gmail_tenant_denial_contract",
    "assert_gmail_tombstone_resolution_contract",
]
