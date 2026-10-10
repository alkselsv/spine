from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spine.application.diagnostics.audit import (
    AccessDecisionAuditPayload,
    AuditOutcome,
    AuditObjectReference,
    CommandAuditPayload,
    OutboxDeliveryAuditPayload,
    RequiredAuditCoordinator,
    UnsupportedAuditEventError,
)
from spine.application.persistence.context import ContextOrigin
from spine.application.persistence import OpaqueObjectReference
from spine.application.persistence.errors import (
    AuditConflictError,
    InvalidPersistenceContextError,
    PersistenceUnavailableError,
)
from spine.auth.errors import AuthorizationDeniedError
from spine.domain.workspaces import Workspace

from .adapter import AuditPersistenceAdapter
from .ids import synthetic_uuid
from .outbox_events import WorkspaceCreatedPayload


@pytest.mark.asyncio
async def test_audit_event_becomes_visible_only_after_explicit_commit(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2201)
    context = persistence_adapter.workspace_context(workspace_id)
    event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.create"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.COMPLETED,
        reason="workspace_created",
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="audit-contract",
                display_name="Audit Contract",
            )
        )
        audit_event_id = await uow.audit.append(event)
        assert await persistence_adapter.read_audit_event(
            context, audit_event_id
        ) is None
        await uow.commit()

    stored = await persistence_adapter.read_audit_event(context, audit_event_id)

    assert stored is not None
    assert stored.audit_event_id == audit_event_id
    assert stored.appended_at is not None
    assert stored.appended_at.tzinfo is not None
    assert stored.payload_json() == {"command_type": "workspace.create"}
    assert await persistence_adapter.read_audit_event(context, audit_event_id) == stored


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_mutation_and_outbox_intent(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2210)
    outbox_event_id = synthetic_uuid(2211)
    context = persistence_adapter.workspace_context(workspace_id)
    audit_event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.create"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.COMPLETED,
        reason="workspace_created",
    )
    outbox_intent = persistence_adapter.outbox_events.build_intent(
        event_id=outbox_event_id,
        workspace_id=workspace_id,
        event_type="workspace.created",
        schema_version=1,
        payload=WorkspaceCreatedPayload(
            workspace=OpaqueObjectReference(
                object_type="workspace",
                object_id=workspace_id,
                schema_version=1,
            ),
            lifecycle_state="active",
        ),
        trace_id=context.trace_id,
    )
    expected_workspace = Workspace(
        id=workspace_id,
        slug="audit-failure",
        display_name="Audit Failure",
    )
    persistence_adapter.fail_next_audit_append()

    with pytest.raises(PersistenceUnavailableError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.workspaces.add(expected_workspace)
            await uow.outbox.append(outbox_intent)
            await uow.audit.append(audit_event)

    async with persistence_adapter.uow_factory(context) as retry:
        assert await retry.workspaces.resolve(workspace_id) is None
        await retry.workspaces.add(expected_workspace)
        assert await retry.outbox.append(outbox_intent) == outbox_event_id
        await retry.rollback()


@pytest.mark.asyncio
async def test_required_audit_commits_before_releasing_effect(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2220)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="required-audit",
                display_name="Required Audit",
            )
        )
        await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="protected.release"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ACCEPTED,
        reason="release_allowed",
    )
    coordinator = RequiredAuditCoordinator(persistence_adapter.uow_factory)

    async def release(audit_event_id):
        assert await persistence_adapter.read_audit_event(
            context, audit_event_id
        ) is not None
        return "released"

    result = await coordinator.release_after_audit(
        context=context,
        event=event,
        release=release,
    )

    assert result == "released"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    (
        {"workspace_id": synthetic_uuid(2231)},
        {"environment_id": synthetic_uuid(2232)},
        {"acting_subject_id": synthetic_uuid(2233)},
        {"service_principal_id": synthetic_uuid(2234)},
        {"trace_id": synthetic_uuid(2235)},
        {
            "origin": ContextOrigin.WORKER,
            "acting_subject_id": None,
            "service_principal_id": synthetic_uuid(2236),
        },
    ),
)
async def test_audit_context_substitution_is_rejected_before_append(
    persistence_adapter: AuditPersistenceAdapter,
    substitution: dict[str, object],
) -> None:
    workspace_id = synthetic_uuid(2230)
    context = persistence_adapter.workspace_context(workspace_id)
    event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.create"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ACCEPTED,
        reason="command_valid",
    ).model_copy(update=substitution)

    with pytest.raises(InvalidPersistenceContextError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.audit.append(event)


@pytest.mark.asyncio
async def test_mutated_content_bearing_payload_is_rejected_with_safe_error(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2240)
    context = persistence_adapter.workspace_context(workspace_id)
    event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.create"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ACCEPTED,
        reason="command_valid",
    ).model_copy(
        update={
            "payload": CommandAuditPayload.model_construct(
                command_type="Bearer protected-token"
            )
        }
    )

    with pytest.raises(UnsupportedAuditEventError) as captured:
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.audit.append(event)

    assert "protected-token" not in str(captured.value)


@pytest.mark.asyncio
async def test_duplicate_producer_identity_returns_original_audit_event(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2250)
    original_event_id = synthetic_uuid(2251)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="audit-replay",
                display_name="Audit Replay",
            )
        )
        await setup.commit()

    def event(audit_event_id):
        return persistence_adapter.audit_events.build_event(
            audit_event_id=audit_event_id,
            workspace_id=workspace_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="workspace.create"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.COMPLETED,
            reason="workspace_created",
            producer_deduplication_id="workspace.create:command-2250",
        )

    async with persistence_adapter.uow_factory(context) as first:
        assert await first.audit.append(event(original_event_id)) == original_event_id
        await first.commit()

    async with persistence_adapter.uow_factory(context) as replay:
        assert await replay.audit.append(event(synthetic_uuid(2252))) == original_event_id
        await replay.commit()

    assert await persistence_adapter.read_audit_event(
        context, original_event_id
    ) is not None
    assert await persistence_adapter.read_audit_event(
        context, synthetic_uuid(2252)
    ) is None


@pytest.mark.asyncio
async def test_conflicting_producer_identity_raises_stable_audit_conflict(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2255)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="audit-producer-conflict",
                display_name="Audit Producer Conflict",
            )
        )
        await setup.commit()

    def event(*, audit_event_id, reason):
        return persistence_adapter.audit_events.build_event(
            audit_event_id=audit_event_id,
            workspace_id=workspace_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="workspace.inspect"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.COMPLETED,
            reason=reason,
            producer_deduplication_id="workspace.inspect:command-2255",
        )

    async with persistence_adapter.uow_factory(context) as first:
        await first.audit.append(
            event(
                audit_event_id=synthetic_uuid(2256),
                reason="inspection_completed",
            )
        )
        await first.commit()

    with pytest.raises(
        AuditConflictError,
        match="Audit producer identity conflicts with an event",
    ):
        async with persistence_adapter.uow_factory(context) as conflicting:
            await conflicting.audit.append(
                event(
                    audit_event_id=synthetic_uuid(2257),
                    reason="inspection_rejected",
                )
            )


@pytest.mark.asyncio
async def test_audit_reader_does_not_reveal_event_to_another_workspace(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2260)
    foreign_workspace_id = synthetic_uuid(2261)
    audit_event_id = synthetic_uuid(2262)
    context = persistence_adapter.workspace_context(workspace_id)
    foreign_context = persistence_adapter.workspace_context(foreign_workspace_id)
    for current_context, slug in (
        (context, "audit-owner"),
        (foreign_context, "audit-foreign"),
    ):
        async with persistence_adapter.uow_factory(current_context) as setup:
            await setup.workspaces.add(
                Workspace(
                    id=current_context.scope.workspace_id,
                    slug=slug,
                    display_name="Audit Isolation",
                )
            )
            await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.inspect"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.REJECTED,
        reason="access_denied",
        target=AuditObjectReference(
            object_type="workspace",
            object_id=workspace_id,
            schema_version=1,
        ),
    )
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.audit.append(event)
        await uow.commit()

    assert await persistence_adapter.read_audit_event(
        context, audit_event_id
    ) is not None
    assert await persistence_adapter.read_audit_event(
        foreign_context, audit_event_id
    ) is None


@pytest.mark.asyncio
async def test_required_audit_failure_never_releases_effect(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2270)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="required-audit-failure",
                display_name="Required Audit Failure",
            )
        )
        await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="protected.release"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ACCEPTED,
        reason="release_allowed",
    )
    released = False

    async def release(_audit_event_id):
        nonlocal released
        released = True
        return "protected result"

    persistence_adapter.fail_next_audit_append()
    coordinator = RequiredAuditCoordinator(persistence_adapter.uow_factory)

    with pytest.raises(PersistenceUnavailableError):
        await coordinator.release_after_audit(
            context=context,
            event=event,
            release=release,
        )

    assert released is False


@pytest.mark.asyncio
async def test_concurrent_duplicate_commit_keeps_one_logical_audit_event(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2280)
    first_event_id = synthetic_uuid(2281)
    second_event_id = synthetic_uuid(2282)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="audit-concurrent-replay",
                display_name="Audit Concurrent Replay",
            )
        )
        await setup.commit()

    def event(audit_event_id):
        return persistence_adapter.audit_events.build_event(
            audit_event_id=audit_event_id,
            workspace_id=workspace_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type="workspace.inspect"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
            outcome=AuditOutcome.COMPLETED,
            reason="inspection_completed",
            producer_deduplication_id="workspace.inspect:command-2280",
        )

    async with persistence_adapter.uow_factory(context) as first:
        async with persistence_adapter.uow_factory(context) as second:
            assert await first.audit.append(event(first_event_id)) == first_event_id
            assert await second.audit.append(event(second_event_id)) == second_event_id
            await first.commit()
            with pytest.raises(AuditConflictError):
                await second.commit()

    async with persistence_adapter.uow_factory(context) as retry:
        assert await retry.audit.append(event(second_event_id)) == first_event_id
        await retry.commit()

    assert await persistence_adapter.read_audit_event(
        context, first_event_id
    ) is not None
    assert await persistence_adapter.read_audit_event(
        context, second_event_id
    ) is None


@pytest.mark.asyncio
async def test_distinct_retry_attempt_identities_create_distinct_audit_events(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2290)
    interactive_context = persistence_adapter.workspace_context(workspace_id)
    worker_context = persistence_adapter.worker_workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(interactive_context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="audit-delivery-attempts",
                display_name="Audit Delivery Attempts",
            )
        )
        await setup.commit()

    event_ids = (synthetic_uuid(2291), synthetic_uuid(2292))
    for attempt_number, audit_event_id in enumerate(event_ids, start=1):
        event = persistence_adapter.audit_events.build_event(
            audit_event_id=audit_event_id,
            workspace_id=workspace_id,
            event_type="outbox.delivery",
            schema_version=1,
            payload=OutboxDeliveryAuditPayload(
                outbox_event_id=synthetic_uuid(2293),
                attempt_id=synthetic_uuid(2293 + attempt_number),
                attempt_number=attempt_number,
            ),
            origin=worker_context.origin,
            acting_subject_id=worker_context.acting_subject_id,
            service_principal_id=worker_context.service_principal_id,
            trace_id=worker_context.trace_id,
            occurred_at=datetime(2026, 2, 3, 4, attempt_number, tzinfo=timezone.utc),
            outcome=AuditOutcome.RETRY_SCHEDULED,
            reason="transient_failure",
            producer_deduplication_id=f"outbox.delivery:attempt-{attempt_number}",
        )
        async with persistence_adapter.uow_factory(worker_context) as uow:
            await uow.audit.append(event)
            await uow.commit()

    assert all(
        [
            await persistence_adapter.read_audit_event(worker_context, event_id)
            is not None
            for event_id in event_ids
        ]
    )


@pytest.mark.asyncio
async def test_exit_without_commit_discards_audit_event(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2300)
    audit_event_id = synthetic_uuid(2301)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="audit-implicit-rollback",
                display_name="Audit Implicit Rollback",
            )
        )
        await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="workspace.inspect"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.COMPLETED,
        reason="inspection_completed",
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.audit.append(event)

    assert await persistence_adapter.read_audit_event(
        context, audit_event_id
    ) is None


@pytest.mark.asyncio
async def test_denied_required_audit_never_releases_protected_effect(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2310)
    audit_event_id = synthetic_uuid(2311)
    protected_object_id = synthetic_uuid(2312)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="required-audit-denied",
                display_name="Required Audit Denied",
            )
        )
        await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        event_type="access.decision",
        schema_version=1,
        payload=AccessDecisionAuditPayload(
            purpose="answer_question",
            operation="read_content",
        ),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.DENIED,
        reason="authorization_denied",
        target=AuditObjectReference(
            object_type="document",
            object_id=protected_object_id,
            schema_version=1,
        ),
    )
    released = False

    async def release(_audit_event_id):
        nonlocal released
        released = True
        return "protected result"

    with pytest.raises(AuthorizationDeniedError) as captured:
        await RequiredAuditCoordinator(
            persistence_adapter.uow_factory
        ).release_after_audit(
            context=context,
            event=event,
            release=release,
        )

    assert released is False
    assert str(protected_object_id) not in str(captured.value)
    assert await persistence_adapter.read_audit_event(
        context, audit_event_id
    ) is not None


@pytest.mark.asyncio
async def test_denied_required_audit_failure_still_never_releases_effect(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2350)
    protected_object_id = synthetic_uuid(2351)
    context = persistence_adapter.workspace_context(workspace_id)
    event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="access.decision",
        schema_version=1,
        payload=AccessDecisionAuditPayload(
            purpose="answer_question",
            operation="read_content",
        ),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.DENIED,
        reason="authorization_denied",
        target=AuditObjectReference(
            object_type="document",
            object_id=protected_object_id,
            schema_version=1,
        ),
    )
    released = False

    async def release(_audit_event_id):
        nonlocal released
        released = True

    persistence_adapter.fail_next_audit_append()
    with pytest.raises(PersistenceUnavailableError) as captured:
        await RequiredAuditCoordinator(
            persistence_adapter.uow_factory
        ).release_after_audit(
            context=context,
            event=event,
            release=release,
        )

    assert released is False
    assert str(protected_object_id) not in str(captured.value)


@pytest.mark.asyncio
async def test_rejected_required_audit_never_releases_protected_effect(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2330)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="required-audit-rejected",
                display_name="Required Audit Rejected",
            )
        )
        await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        workspace_id=workspace_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="protected.release"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.REJECTED,
        reason="policy_rejected",
    )
    released = False

    async def release(_audit_event_id):
        nonlocal released
        released = True

    with pytest.raises(AuthorizationDeniedError):
        await RequiredAuditCoordinator(
            persistence_adapter.uow_factory
        ).release_after_audit(
            context=context,
            event=event,
            release=release,
        )

    assert released is False


@pytest.mark.asyncio
async def test_allowed_required_audit_releases_without_leaking_target_metadata(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2340)
    audit_event_id = synthetic_uuid(2341)
    protected_object_id = synthetic_uuid(2342)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="required-audit-allowed",
                display_name="Required Audit Allowed",
            )
        )
        await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        event_type="access.decision",
        schema_version=1,
        payload=AccessDecisionAuditPayload(
            purpose="answer_question",
            operation="read_content",
        ),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ALLOWED,
        reason="authorization_allowed",
        target=AuditObjectReference(
            object_type="document",
            object_id=protected_object_id,
            schema_version=1,
        ),
    )

    async def release(committed_audit_event_id):
        assert committed_audit_event_id == audit_event_id
        return "released"

    result = await RequiredAuditCoordinator(
        persistence_adapter.uow_factory
    ).release_after_audit(
        context=context,
        event=event,
        release=release,
    )

    assert result == "released"
    assert str(protected_object_id) not in result


@pytest.mark.asyncio
async def test_historical_audit_event_cannot_authorize_a_new_unit_of_work(
    persistence_adapter: AuditPersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2320)
    audit_event_id = synthetic_uuid(2321)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as setup:
        await setup.workspaces.add(
            Workspace(
                id=workspace_id,
                slug="historical-audit-not-authority",
                display_name="Historical Audit Is Not Authority",
            )
        )
        await setup.commit()
    event = persistence_adapter.audit_events.build_event(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        event_type="access.decision",
        schema_version=1,
        payload=AccessDecisionAuditPayload(
            purpose="answer_question",
            operation="read_content",
        ),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=datetime(2026, 2, 3, 4, 4, tzinfo=timezone.utc),
        outcome=AuditOutcome.ALLOWED,
        reason="authorization_allowed",
    )
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.audit.append(event)
        await uow.commit()
    historical_event = await persistence_adapter.read_audit_event(
        context, audit_event_id
    )
    assert historical_event is not None

    with pytest.raises(InvalidPersistenceContextError):
        persistence_adapter.uow_factory(historical_event)  # type: ignore[arg-type]
