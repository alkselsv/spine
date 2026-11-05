from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.gmail_message_state import (
    GmailLabelDisposition,
    GmailMessageEvidenceBinding,
    GmailMessageStateUnavailableError,
    GmailRelationshipAuthority,
    GmailRelationshipEvidence,
    GmailRelationshipKind,
    InMemoryGmailMessageStateRepository,
    gmail_relationship_observations,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryResult,
    GmailHistoryState,
)
from spine.connectors.gmail_message_state import (
    GmailMessageStateWriter,
    gmail_label_observation,
)
from spine.domain.sources.gmail import GmailMessageKey
from spine.domain.sources.models import EmailRevisionMetadata
from tests.contracts.connectors.gmail_message_state_repository_contract import (
    GmailMessageStateRepositoryScenario,
    assert_gmail_label_convergence_contract,
    assert_gmail_relationship_replay_contract,
    assert_gmail_stale_revision_contract,
    assert_gmail_tenant_denial_contract,
    assert_gmail_tombstone_resolution_contract,
)


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
CONNECTION_ID = UUID("30000000-0000-0000-0000-000000000003")
MAILBOX_ID = UUID("40000000-0000-0000-0000-000000000004")
SOURCE_ID = UUID("50000000-0000-0000-0000-000000000005")
REVISION_ID = UUID("60000000-0000-0000-0000-000000000006")
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def binding(
    *, workspace_id: UUID = WORKSPACE_ID, revision_id: UUID = REVISION_ID
) -> GmailMessageEvidenceBinding:
    return GmailMessageEvidenceBinding(
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT_ID,
        source_object_id=SOURCE_ID,
        source_revision_id=revision_id,
        message_key=GmailMessageKey(
            connection_id=CONNECTION_ID,
            mailbox_binding_id=MAILBOX_ID,
            message_id="gmail-message-840",
        ),
    )


def change(
    history_id: str,
    kind: GmailHistoryChangeKind,
    *,
    label_id: str = "INBOX",
) -> GmailHistoryChange:
    return GmailHistoryChange(
        history_id=history_id,
        kind=kind,
        message_id="gmail-message-840",
        label_id=label_id,
    )


def repository_scenario(
    *, denied: bool = False
) -> GmailMessageStateRepositoryScenario:
    target = binding()
    successor = binding(
        revision_id=UUID("60000000-0000-0000-0000-000000000008")
    )
    evidence = EmailRevisionMetadata(
        provider_kind="google",
        provider_message_id=target.message_key.message_id,
        internal_date=NOW,
        provider_thread_reference="gmail-thread-840",
        rfc_message_id_claims=("<message@example.test>",),
        references_claims=("<parent@example.test>",),
        in_reply_to_claims=("<parent@example.test>",),
    )
    repository = InMemoryGmailMessageStateRepository(
        () if denied else (target, successor),
        revision_evidence=(
            () if denied else ((target, evidence), (successor, evidence))
        ),
        current_bindings=() if denied else (target,),
    )

    async def advance_current() -> None:
        repository.set_current_binding(successor)

    async def tombstone_current() -> None:
        repository.clear_current_binding(target)

    async def restore_current() -> None:
        repository.set_current_binding(target)

    return GmailMessageStateRepositoryScenario(
        repository,
        target,
        evidence,
        advance_current,
        tombstone_current,
        restore_current,
    )


@pytest.mark.asyncio
async def test_in_memory_repository_satisfies_label_convergence_contract() -> None:
    await assert_gmail_label_convergence_contract(repository_scenario())


@pytest.mark.asyncio
async def test_in_memory_repository_satisfies_relationship_replay_contract() -> None:
    await assert_gmail_relationship_replay_contract(repository_scenario())


@pytest.mark.asyncio
async def test_in_memory_repository_satisfies_tenant_denial_contract() -> None:
    await assert_gmail_tenant_denial_contract(repository_scenario(denied=True))


@pytest.mark.asyncio
async def test_in_memory_repository_satisfies_stale_revision_contract() -> None:
    await assert_gmail_stale_revision_contract(repository_scenario())


@pytest.mark.asyncio
async def test_in_memory_repository_satisfies_tombstone_resolution_contract() -> None:
    await assert_gmail_tombstone_resolution_contract(repository_scenario())


@pytest.mark.asyncio
async def test_label_changes_bind_exact_source_without_content_effects() -> None:
    target = binding()
    repository = InMemoryGmailMessageStateRepository((target,))
    observation = gmail_label_observation(
        target,
        change("100", GmailHistoryChangeKind.LABEL_ADDED),
        received_at=NOW,
    )

    result = await repository.record_label(observation)

    assert result.disposition is GmailLabelDisposition.APPLIED
    assert result.current.observation.binding == target
    assert result.current.present is True
    assert result.current.observation.binding.source_revision_id == REVISION_ID


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_label_observations_converge() -> None:
    target = binding()
    repository = InMemoryGmailMessageStateRepository((target,))
    latest = gmail_label_observation(
        target,
        change("102", GmailHistoryChangeKind.LABEL_REMOVED),
        received_at=NOW,
    )
    older = gmail_label_observation(
        target,
        change("101", GmailHistoryChangeKind.LABEL_ADDED),
        received_at=NOW - timedelta(minutes=1),
    )

    applied = await repository.record_label(latest)
    replay = await repository.record_label(
        latest.model_copy(update={"received_at": NOW + timedelta(minutes=1)})
    )
    out_of_order = await repository.record_label(older)

    assert applied.disposition is GmailLabelDisposition.APPLIED
    assert replay.disposition is GmailLabelDisposition.DUPLICATE
    assert out_of_order.disposition is GmailLabelDisposition.OUT_OF_ORDER
    assert replay.current == out_of_order.current == applied.current
    assert out_of_order.current.present is False
    assert await repository.list_label_observations(target) == (older, latest)


@pytest.mark.asyncio
async def test_stale_revision_rejects_new_evidence_but_accepts_exact_replay() -> None:
    first = binding()
    successor = binding(
        revision_id=UUID("60000000-0000-0000-0000-000000000007")
    )
    repository = InMemoryGmailMessageStateRepository((first, successor))
    repository.set_current_binding(first)
    accepted = gmail_label_observation(
        first,
        change("100", GmailHistoryChangeKind.LABEL_ADDED),
        received_at=NOW,
    )
    await repository.record_label(accepted)
    repository.set_current_binding(successor)
    stale_new = gmail_label_observation(
        first,
        change("101", GmailHistoryChangeKind.LABEL_REMOVED),
        received_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(GmailMessageStateUnavailableError):
        await repository.record_label(stale_new)
    replay = await repository.record_label(
        accepted.model_copy(update={"received_at": NOW + timedelta(minutes=2)})
    )

    assert replay.disposition is GmailLabelDisposition.DUPLICATE
    assert replay.persisted == accepted


@pytest.mark.asyncio
async def test_tombstoned_message_resolves_no_state_target() -> None:
    target = binding()
    repository = InMemoryGmailMessageStateRepository((target,))
    repository.clear_current_binding(target)

    resolved = await repository.resolve_current_binding(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        message_key=target.message_key,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_thread_and_header_relationships_retain_distinct_authority() -> None:
    target = binding()
    revision_evidence = EmailRevisionMetadata(
        provider_kind="google",
        provider_message_id="gmail-message-840",
        internal_date=NOW,
        provider_thread_reference="gmail-thread-840",
        rfc_message_id_claims=("<message@example.test>",),
        references_claims=("<parent@example.test>",),
        in_reply_to_claims=("<parent@example.test>",),
    )
    repository = InMemoryGmailMessageStateRepository(
        (target,), revision_evidence=((target, revision_evidence),)
    )
    evidence = gmail_relationship_observations(
        target,
        revision_evidence=revision_evidence,
        received_at=NOW,
    )

    for relationship in evidence:
        await repository.record_relationship(relationship)

    stored = await repository.list_relationships(target)
    assert [(item.kind, item.authority) for item in stored] == [
        (GmailRelationshipKind.PROVIDER_THREAD, GmailRelationshipAuthority.PROVIDER),
        (GmailRelationshipKind.REFERENCES, GmailRelationshipAuthority.HEADER_INFERRED),
        (GmailRelationshipKind.IN_REPLY_TO, GmailRelationshipAuthority.HEADER_INFERRED),
    ]
    assert all(item.binding.source_revision_id == REVISION_ID for item in stored)
    assert not hasattr(stored[1], "business_identity")


@pytest.mark.asyncio
async def test_wrong_workspace_cannot_record_message_state() -> None:
    target = binding()
    repository = InMemoryGmailMessageStateRepository((target,))
    foreign = binding(
        workspace_id=UUID("10000000-0000-0000-0000-000000000099")
    )
    observation = gmail_label_observation(
        foreign,
        change("100", GmailHistoryChangeKind.LABEL_ADDED),
        received_at=NOW,
    )

    with pytest.raises(
        GmailMessageStateUnavailableError, match="unavailable in this tenant"
    ) as error:
        await repository.record_label(observation)

    assert "gmail-message-840" not in str(error.value)


@pytest.mark.asyncio
async def test_unavailable_history_target_diagnostic_discloses_no_provider_data() -> None:
    sensitive_values = (
        "refresh-token-value",
        "raw-provider-payload",
        "customer-subject",
        "private@example.test",
        "subject.pdf",
        "protected-content",
    )
    message_id = "|".join(sensitive_values)
    repository = InMemoryGmailMessageStateRepository()
    writer = GmailMessageStateWriter(
        repository=repository,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        mailbox_binding_id=MAILBOX_ID,
    )
    history = GmailHistoryResult(
        requested_start_history_id="99",
        admission_scope_digest="a" * 64,
        state=GmailHistoryState.COMPLETE,
        changes=(
            GmailHistoryChange(
                history_id="100",
                kind=GmailHistoryChangeKind.LABEL_ADDED,
                message_id=message_id,
                label_id="INBOX",
            ),
        ),
        terminal_history_id="100",
        pages_read=1,
        duplicate_count=0,
        malformed_count=0,
    )

    with pytest.raises(GmailMessageStateUnavailableError) as captured:
        await writer.record_history(history, received_at=NOW)

    diagnostic = f"{captured.value!s} {captured.value!r}"
    assert all(value not in diagnostic for value in sensitive_values)


@pytest.mark.asyncio
async def test_history_writer_persists_normalized_label_changes() -> None:
    target = binding()
    repository = InMemoryGmailMessageStateRepository((target,))
    writer = GmailMessageStateWriter(
        repository=repository,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        mailbox_binding_id=MAILBOX_ID,
    )
    history = GmailHistoryResult(
        requested_start_history_id="99",
        admission_scope_digest="a" * 64,
        state=GmailHistoryState.COMPLETE,
        changes=(
            change("100", GmailHistoryChangeKind.MESSAGE_ADDED, label_id="unused"),
            change("101", GmailHistoryChangeKind.LABEL_ADDED),
        ),
        terminal_history_id="102",
        pages_read=1,
        duplicate_count=0,
        malformed_count=0,
    )

    receipts = await writer.record_history(history, received_at=NOW)

    assert len(receipts) == 1
    assert receipts[0].current.present is True


@pytest.mark.asyncio
async def test_retry_after_durable_write_converges_to_duplicate() -> None:
    target = binding()
    durable = InMemoryGmailMessageStateRepository((target,))

    class FailAfterFirstDurableWrite:
        def __init__(self) -> None:
            self.failed = False

        async def record_label(self, observation):
            result = await durable.record_label(observation)
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic post-commit interruption")
            return result

        async def resolve_current_binding(self, **values):
            return await durable.resolve_current_binding(**values)

    writer = GmailMessageStateWriter(
        repository=FailAfterFirstDurableWrite(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        mailbox_binding_id=MAILBOX_ID,
    )
    history = GmailHistoryResult(
        requested_start_history_id="99",
        admission_scope_digest="a" * 64,
        state=GmailHistoryState.COMPLETE,
        changes=(change("101", GmailHistoryChangeKind.LABEL_ADDED),),
        terminal_history_id="102",
        pages_read=1,
        duplicate_count=0,
        malformed_count=0,
    )

    with pytest.raises(RuntimeError, match="post-commit interruption"):
        await writer.record_history(history, received_at=NOW)
    replay = await writer.record_history(history, received_at=NOW + timedelta(minutes=1))

    assert replay[0].disposition is GmailLabelDisposition.DUPLICATE
    assert len(await durable.list_label_observations(target)) == 1


def test_header_relationship_cannot_be_upgraded_to_provider_authority() -> None:
    with pytest.raises(ValidationError, match="cannot be upgraded"):
        GmailRelationshipEvidence.model_validate(
            {
                "evidence_id": "70000000-0000-0000-0000-000000000007",
                "binding": binding().model_dump(mode="json"),
                "kind": "references",
                "authority": "provider_reported_thread_id",
                "relationship_reference": "<parent@example.test>",
                "received_at": NOW,
            }
        )


def test_non_label_history_change_cannot_become_label_state() -> None:
    with pytest.raises(ValueError, match="label history changes"):
        gmail_label_observation(
            binding(),
            change("100", GmailHistoryChangeKind.MESSAGE_ADDED, label_id="INBOX"),
            received_at=NOW,
        )
