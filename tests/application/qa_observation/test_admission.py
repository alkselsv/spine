from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.persistence.ask_admission import (
    AskAdmissionCommand,
    AskAdmissionResult,
)
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.qa_observation.admission import ObserverAskAdmission
from spine.application.qa_observation.channel import CaptureDisposition
from spine.application.qa_observation.contracts import CaptureSubscription
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000476")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000476")
SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000476")
SESSION_ID = UUID("40000000-0000-0000-0000-000000000476")
COMMAND_ID = UUID("50000000-0000-0000-0000-000000000476")
MESSAGE_ID = UUID("60000000-0000-0000-0000-000000000476")
RUN_ID = UUID("70000000-0000-0000-0000-000000000476")
EVENT_ID = UUID("80000000-0000-0000-0000-000000000476")
_CAPTURE_COMMAND = AskAdmissionCommand.create(
    workspace_id=WORKSPACE_ID,
    environment_id=ENVIRONMENT_ID,
    session_id=SESSION_ID,
    subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
    environment=EnvironmentKind.PRODUCTION,
    question="synthetic question",
    idempotency_key=IdempotencyKey("observer-476"),
)
CAPTURE_ID = uuid5(
    NAMESPACE_URL,
    "spine:qa-observer-capture:v1:"
    f"{WORKSPACE_ID}:{ENVIRONMENT_ID}:human:{SUBJECT_ID}:"
    f"observer-476:{_CAPTURE_COMMAND.digest.value}",
)


class Admission:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.count = 0

    async def admit(self, *, context, command):
        del context
        self.calls.append("admit")
        assert command.capture_id == CAPTURE_ID
        self.count += 1
        return AskAdmissionResult(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
            run_id=RUN_ID,
            dispatch_event_id=EVENT_ID,
            replay=self.count > 1,
        )


@pytest.mark.asyncio
async def test_failed_admission_cleans_pre_registered_subscription() -> None:
    calls: list[str] = []

    class FailedAdmission:
        async def admit(self, *, context, command):
            del context, command
            calls.append("admit")
            raise RuntimeError("admission failed")

    service = ObserverAskAdmission(
        admission=FailedAdmission(),
        channel=Channel(calls),
        capture_id_source=lambda _command: CAPTURE_ID,
    )
    command = AskAdmissionCommand.create(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
        environment=EnvironmentKind.PRODUCTION,
        question="synthetic question",
        idempotency_key=IdempotencyKey("observer-failure-476"),
    )

    with pytest.raises(RuntimeError, match="admission failed"):
        await service.admit(context=object(), command=command)

    assert calls == ["register", "admit", "cleanup"]


class Channel:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.identity = None

    async def register(self, *, capture_id, ttl):
        self.calls.append("register")
        assert capture_id == CAPTURE_ID
        assert ttl == timedelta(minutes=5)
        return CaptureSubscription(
            capture_id=capture_id,
            subscription_id=UUID("a0000000-0000-0000-0000-000000000476"),
            expires_at=datetime(2026, 10, 18, tzinfo=timezone.utc)
            + timedelta(minutes=5),
        )

    async def bind(self, *, subscription_id, identity):
        self.calls.append("bind")
        self.identity = identity
        assert subscription_id == UUID("a0000000-0000-0000-0000-000000000476")
        return CaptureDisposition.BOUND

    async def cleanup(self, *, subscription_id):
        del subscription_id
        self.calls.append("cleanup")
        return CaptureDisposition.CLEANED


@pytest.mark.asyncio
async def test_observer_registers_before_admission_and_binds_canonical_identity() -> None:
    calls: list[str] = []
    channel = Channel(calls)
    service = ObserverAskAdmission(
        admission=Admission(calls),
        channel=channel,
    )
    command = AskAdmissionCommand.create(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
        environment=EnvironmentKind.PRODUCTION,
        question="synthetic question",
        idempotency_key=IdempotencyKey("observer-476"),
    )

    result = await service.admit(context=object(), command=command)
    replay = await service.admit(context=object(), command=command)

    assert calls == ["register", "admit", "bind", "register", "admit", "bind"]
    assert result.admission.run_id == RUN_ID
    assert result.capture_id == CAPTURE_ID
    assert channel.identity.command_id == COMMAND_ID
    assert channel.identity.run_id == RUN_ID
    assert channel.identity.session_id == SESSION_ID
    assert channel.identity.message_id == MESSAGE_ID
    assert channel.identity.workspace_id == WORKSPACE_ID
    assert channel.identity.environment_id == ENVIRONMENT_ID
    assert channel.identity.requester.id == SUBJECT_ID
    assert channel.identity.attempt_id == uuid5(RUN_ID, "attempt:1")
    assert replay.admission.replay is True
    assert replay.capture_id == result.capture_id
    assert replay.subscription_id == result.subscription_id


async def test_conflicting_idempotency_request_preserves_first_observer_subscription() -> None:
    original = AskAdmissionCommand.create(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
        environment=EnvironmentKind.PRODUCTION,
        question="synthetic question",
        idempotency_key=IdempotencyKey("observer-conflict-476"),
    )
    conflicting = AskAdmissionCommand.create(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        session_id=SESSION_ID,
        subject=ActorRef(kind=ActorKind.HUMAN, id=SUBJECT_ID),
        environment=EnvironmentKind.PRODUCTION,
        question="different synthetic question",
        idempotency_key=IdempotencyKey("observer-conflict-476"),
    )

    class ConflictAwareAdmission:
        async def admit(self, *, context, command):
            del context
            if command.digest == original.digest:
                return AskAdmissionResult(
                    command_id=COMMAND_ID,
                    session_id=SESSION_ID,
                    message_id=MESSAGE_ID,
                    run_id=RUN_ID,
                    dispatch_event_id=EVENT_ID,
                    replay=False,
                )
            raise IdempotencyConflictError("conflict")

    class TrackingChannel:
        def __init__(self) -> None:
            self.active: set[UUID] = set()

        async def register(self, *, capture_id, ttl):
            del ttl
            subscription_id = uuid5(NAMESPACE_URL, str(capture_id))
            self.active.add(subscription_id)
            return CaptureSubscription(
                capture_id=capture_id,
                subscription_id=subscription_id,
                expires_at=datetime(2026, 10, 18, tzinfo=timezone.utc)
                + timedelta(minutes=5),
            )

        async def bind(self, *, subscription_id, identity):
            del identity
            assert subscription_id in self.active
            return CaptureDisposition.BOUND

        async def cleanup(self, *, subscription_id):
            self.active.discard(subscription_id)
            return CaptureDisposition.CLEANED

    channel = TrackingChannel()
    service = ObserverAskAdmission(admission=ConflictAwareAdmission(), channel=channel)
    original_result = await service.admit(context=object(), command=original)

    with pytest.raises(IdempotencyConflictError):
        await service.admit(context=object(), command=conflicting)

    assert original_result.subscription_id in channel.active
    assert len(channel.active) == 1
