"""Application ports and deterministic fakes for resumable Q&A events.

The in-memory implementation models the persistence and delivery boundary used
by the future API/SSE adapter.  It intentionally does not claim that an
appended event was received by a client: append success is emission
persistence, while each replay returns a new delivery view subject to current
authorization.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ConfigDict, model_validator

from spine.domain.common import ValidatedDefinitionModel
from spine.domain.qa_events import (
    AnswerDeltaPayload,
    CancelledPayload,
    CitationAddedPayload,
    DeniedPayload,
    DisclosureReceipt,
    HeartbeatPayload,
    QAEvent,
    QAEventType,
)


class QAEventLogError(ValueError):
    """Base class for safe event-log and resume failures."""


class ResumeCursorError(QAEventLogError):
    """The cursor is malformed, stale, duplicated with different content, or bounded incorrectly."""


class CursorScopeError(ResumeCursorError):
    """A cursor belongs to a different workspace or event stream."""


class CandidateOutputStreamingError(QAEventLogError):
    """Candidate model output was offered before the disclosure boundary."""


class QAEventReplay(ValidatedDefinitionModel):
    """Safe delivery result for SSE and polling consumers."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    events: tuple[QAEvent, ...] = ()
    terminal: QAEvent | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> QAEventReplay:
        if self.terminal is not None and not self.terminal.is_terminal:
            raise ValueError("replay terminal must be a terminal event")
        if any(not isinstance(event, QAEvent) for event in self.events):
            raise ValueError("replay contains an invalid event")
        return self


class QAEventLogPort(Protocol):
    """Persistence contract for immutable Q&A emission records."""

    async def append(self, event: QAEvent) -> QAEvent: ...

    async def read(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        limit: int | None = None,
    ) -> tuple[QAEvent, ...]: ...

    async def read_after(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        last_event_id: UUID | str | None = None,
        last_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[QAEvent, ...]: ...


DisclosureAuthorizer = Callable[
    [DisclosureReceipt], bool | Awaitable[bool]
]
EventIdFactory = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class _StreamKey:
    workspace_id: UUID
    environment_id: UUID
    session_id: UUID
    message_id: UUID | None
    run_id: UUID | None


def _validate_uuid(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


def _parse_cursor(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ResumeCursorError("resume cursor is malformed") from exc


class InMemoryQAEventLog:
    """Deterministic append-only event log fake with scope-bound cursors."""

    def __init__(self, *, max_replay: int = 100) -> None:
        if max_replay <= 0:
            raise ValueError("max_replay must be positive")
        self.max_replay = max_replay
        self._streams: dict[_StreamKey, list[QAEvent]] = {}
        self._events_by_id: dict[UUID, QAEvent] = {}

    async def append(self, event: QAEvent) -> QAEvent:
        if type(event) is not QAEvent:
            raise TypeError("event log accepts only the canonical QAEvent envelope")
        key = _StreamKey(
            workspace_id=event.workspace_id,
            environment_id=event.environment_id,
            session_id=event.session_id,
            message_id=event.message_id,
            run_id=event.run_id,
        )
        existing_by_id = self._events_by_id.get(event.event_id)
        if existing_by_id is not None:
            if existing_by_id == event:
                return existing_by_id
            raise ResumeCursorError("event identity was reused with different content")

        events = self._streams.setdefault(key, [])
        existing_at_sequence = next(
            (item for item in events if item.sequence == event.sequence), None
        )
        if existing_at_sequence is not None:
            raise ResumeCursorError("event sequence was reused with different identity")
        if events and events[-1].is_terminal:
            raise ResumeCursorError("terminal event cannot be followed by another event")
        expected_sequence = events[-1].sequence + 1 if events else 1
        if event.sequence != expected_sequence:
            raise ResumeCursorError("event sequence must be monotonic without gaps")

        events.append(event)
        self._events_by_id[event.event_id] = event
        return event

    async def read(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        limit: int | None = None,
    ) -> tuple[QAEvent, ...]:
        return await self.read_after(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            limit=limit,
        )

    async def read_after(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        last_event_id: UUID | str | None = None,
        last_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[QAEvent, ...]:
        self._validate_limit(limit)
        cursor_id = _parse_cursor(last_event_id)
        if last_sequence is not None and (
            isinstance(last_sequence, bool)
            or not isinstance(last_sequence, int)
            or last_sequence < 0
        ):
            raise ResumeCursorError("resume sequence is malformed")

        key, events = self._resolve_stream(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            cursor_id=cursor_id,
        )
        del key
        if not events:
            if cursor_id is not None:
                raise ResumeCursorError("resume cursor is not available")
            if last_sequence not in (None, 0):
                raise ResumeCursorError("resume cursor is stale")
            return ()

        start_sequence = 0
        if cursor_id is not None:
            cursor_index = next(
                (index for index, item in enumerate(events) if item.event_id == cursor_id),
                None,
            )
            if cursor_index is None:
                raise ResumeCursorError("resume cursor is not available")
            cursor_event = events[cursor_index]
            if last_sequence is not None and last_sequence != cursor_event.sequence:
                raise ResumeCursorError("resume cursor sequence does not match event identity")
            start_sequence = cursor_event.sequence
        elif last_sequence is not None:
            if last_sequence > events[-1].sequence:
                raise ResumeCursorError("resume cursor is stale")
            start_sequence = last_sequence

        effective_limit = self._validate_limit(limit)
        return tuple(
            event
            for event in events
            if event.sequence > start_sequence
        )[:effective_limit]

    async def replay(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        last_event_id: UUID | str | None = None,
        last_sequence: int | None = None,
        limit: int | None = None,
    ) -> QAEventReplay:
        events = await self.read_after(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            last_event_id=last_event_id,
            last_sequence=last_sequence,
            limit=limit,
        )
        terminal = await self.terminal(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
        )
        return QAEventReplay(events=events, terminal=terminal)

    async def terminal(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
    ) -> QAEvent | None:
        _, events = self._resolve_stream(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
        )
        return next((event for event in reversed(events) if event.is_terminal), None)

    async def next_sequence(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
    ) -> int:
        _, events = self._resolve_stream(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
        )
        if events and events[-1].is_terminal:
            return events[-1].sequence
        return events[-1].sequence + 1 if events else 1

    def open_stream(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        trace_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        disclosure_authorizer: DisclosureAuthorizer | None = None,
        event_id_factory: EventIdFactory = uuid4,
    ) -> InMemoryQAEventStream:
        return InMemoryQAEventStream(
            self,
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            trace_id=trace_id,
            run_id=run_id,
            message_id=message_id,
            disclosure_authorizer=disclosure_authorizer,
            event_id_factory=event_id_factory,
        )

    def _resolve_stream(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None,
        message_id: UUID | None,
        cursor_id: UUID | None = None,
    ) -> tuple[_StreamKey, list[QAEvent]]:
        candidates = [
            (key, events)
            for key, events in self._streams.items()
            if key.workspace_id == workspace_id
            and key.environment_id == environment_id
            and key.session_id == session_id
            and (run_id is None or key.run_id == run_id)
            and (message_id is None or key.message_id == message_id)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            if cursor_id is not None:
                for key, events in candidates:
                    if any(item.event_id == cursor_id for item in events):
                        return key, events
            raise ResumeCursorError("resume stream identity is ambiguous")
        if cursor_id is not None and cursor_id in self._events_by_id:
            raise CursorScopeError("resume cursor belongs to another scope")
        fallback = _StreamKey(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
        )
        return fallback, self._streams.get(fallback, [])

    def _validate_limit(self, limit: int | None) -> int:
        if limit is None:
            return self.max_replay
        if isinstance(limit, bool) or limit <= 0 or limit > self.max_replay:
            raise ResumeCursorError("replay limit is outside the bounded range")
        return limit


class InMemoryQAEventStream:
    """Replay/poll fake that applies a fresh disclosure check per protected event."""

    def __init__(
        self,
        log: InMemoryQAEventLog,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        trace_id: UUID,
        run_id: UUID | None,
        message_id: UUID | None,
        disclosure_authorizer: DisclosureAuthorizer | None,
        event_id_factory: EventIdFactory,
    ) -> None:
        self.log = log
        self.workspace_id = _validate_uuid(workspace_id, "workspace_id")
        self.environment_id = _validate_uuid(environment_id, "environment_id")
        self.session_id = _validate_uuid(session_id, "session_id")
        self.trace_id = _validate_uuid(trace_id, "trace_id")
        self.run_id = run_id
        self.message_id = message_id
        self._authorize = disclosure_authorizer
        self._event_id_factory = event_id_factory
        self._virtual_denials: dict[UUID, QAEvent] = {}
        self._virtual_denials_by_sequence: dict[int, QAEvent] = {}
        self._heartbeat_events: dict[UUID, QAEvent] = {}
        self._default_heartbeat_id = uuid5(
            NAMESPACE_URL,
            f"spine:qa-heartbeat:{workspace_id}:{environment_id}:{session_id}:{run_id}:{message_id}",
        )

    async def replay(
        self,
        *,
        last_event_id: UUID | str | None = None,
        last_sequence: int | None = None,
        limit: int | None = None,
    ) -> QAEventReplay:
        cursor_id = _parse_cursor(last_event_id)
        virtual_terminal = (
            self._virtual_denials.get(cursor_id) if cursor_id is not None else None
        )
        if virtual_terminal is None and last_sequence is not None:
            virtual_terminal = self._virtual_denials_by_sequence.get(last_sequence)
        if virtual_terminal is not None:
            return QAEventReplay(terminal=virtual_terminal)

        persisted = await self.log.read_after(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            session_id=self.session_id,
            run_id=self.run_id,
            message_id=self.message_id,
            last_event_id=cursor_id,
            last_sequence=last_sequence,
            limit=limit,
        )
        safe_events: list[QAEvent] = []
        terminal: QAEvent | None = None
        for event in persisted:
            if event.is_protected and not await self._is_authorized(event):
                terminal = self._denial_for(event)
                safe_events.append(terminal)
                break
            safe_events.append(event)
            if event.is_terminal:
                terminal = event
                break

        if terminal is None:
            stored_terminal = await self.log.terminal(
                workspace_id=self.workspace_id,
                environment_id=self.environment_id,
                session_id=self.session_id,
                run_id=self.run_id,
                message_id=self.message_id,
            )
            if stored_terminal is not None:
                terminal = stored_terminal
        return QAEventReplay(events=tuple(safe_events), terminal=terminal)

    async def poll(
        self,
        *,
        last_event_id: UUID | str | None = None,
        last_sequence: int | None = None,
        limit: int | None = None,
    ) -> QAEventReplay:
        """Polling fallback with exactly the same cursor and disclosure rules."""

        return await self.replay(
            last_event_id=last_event_id,
            last_sequence=last_sequence,
            limit=limit,
        )

    async def rehydrate_terminal(self) -> QAEvent | None:
        return (await self.replay()).terminal

    async def heartbeat(self, *, emission_id: UUID | None = None) -> QAEvent:
        heartbeat_id = emission_id or self._default_heartbeat_id
        existing_heartbeat = self._heartbeat_events.get(heartbeat_id)
        if existing_heartbeat is not None:
            return existing_heartbeat
        terminal = await self._stored_terminal()
        if terminal is not None:
            return terminal
        emitted = await self._emit_generated(
            event_type=QAEventType.HEARTBEAT,
            payload=HeartbeatPayload(),
        )
        self._heartbeat_events[heartbeat_id] = emitted
        return emitted

    async def cancel(self) -> QAEvent:
        terminal = await self._stored_terminal()
        if terminal is not None:
            return terminal
        return await self._emit_generated(
            event_type=QAEventType.CANCELLED,
            payload=CancelledPayload(reason="request_cancelled"),
        )

    async def append(self, event: QAEvent) -> QAEvent:
        if (
            event.workspace_id != self.workspace_id
            or event.environment_id != self.environment_id
            or event.session_id != self.session_id
            or (self.run_id is not None and event.run_id != self.run_id)
            or (self.message_id is not None and event.message_id != self.message_id)
        ):
            raise CursorScopeError("event belongs to another stream scope")
        return await self._append(event)

    async def append_candidate(self, candidate: object) -> None:
        del candidate
        raise CandidateOutputStreamingError(
            "candidate output cannot cross the Q&A disclosure boundary"
        )

    async def iter_events(
        self,
        *,
        last_event_id: UUID | str | None = None,
        last_sequence: int | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[QAEvent]:
        replay = await self.replay(
            last_event_id=last_event_id,
            last_sequence=last_sequence,
            limit=limit,
        )
        for event in replay.events:
            yield event

    async def _append(self, event: QAEvent) -> QAEvent:
        return await self.log.append(event)

    async def _stored_terminal(self) -> QAEvent | None:
        return await self.log.terminal(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            session_id=self.session_id,
            run_id=self.run_id,
            message_id=self.message_id,
        )

    async def _emit_generated(self, *, event_type: QAEventType, payload: object) -> QAEvent:
        message_id = await self._effective_message_id()
        return await self._append(
            QAEvent(
                event_id=self._event_id_factory(),
                sequence=await self.log.next_sequence(
                    workspace_id=self.workspace_id,
                    environment_id=self.environment_id,
                    session_id=self.session_id,
                    run_id=self.run_id,
                    message_id=message_id,
                ),
                type=event_type,
                session_id=self.session_id,
                workspace_id=self.workspace_id,
                environment_id=self.environment_id,
                message_id=message_id,
                run_id=self.run_id,
                trace_id=self.trace_id,
                payload=payload,
            )
        )

    async def _effective_message_id(self) -> UUID | None:
        if self.message_id is not None:
            return self.message_id
        events = await self.log.read(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            session_id=self.session_id,
            run_id=self.run_id,
            limit=self.log.max_replay,
        )
        return events[0].message_id if events else None

    async def _is_authorized(self, event: QAEvent) -> bool:
        if self._authorize is None:
            return True
        payload = event.payload
        if isinstance(payload, (AnswerDeltaPayload, CitationAddedPayload)):
            receipt = payload.disclosure
        else:
            return False
        try:
            result = self._authorize(receipt)
            return bool(await result) if isinstance(result, Awaitable) else bool(result)
        except Exception:
            return False

    def _denial_for(self, event: QAEvent) -> QAEvent:
        existing = self._virtual_denials.get(event.event_id)
        if existing is not None:
            return existing
        denial = QAEvent(
            event_id=uuid5(
                NAMESPACE_URL,
                f"spine:qa-event:authorization-denied:{event.event_id}",
            ),
            sequence=event.sequence,
            type=QAEventType.DENIED,
            session_id=event.session_id,
            workspace_id=event.workspace_id,
            environment_id=event.environment_id,
            message_id=event.message_id,
            run_id=event.run_id,
            trace_id=event.trace_id,
            payload=DeniedPayload(reason="authorization_denied"),
        )
        self._virtual_denials[event.event_id] = denial
        self._virtual_denials[denial.event_id] = denial
        self._virtual_denials_by_sequence[denial.sequence] = denial
        return denial


QAEventLog = QAEventLogPort
QAEventStream = InMemoryQAEventStream

__all__ = [
    "CandidateOutputStreamingError",
    "CursorScopeError",
    "InMemoryQAEventLog",
    "InMemoryQAEventStream",
    "QAEventLog",
    "QAEventLogError",
    "QAEventLogPort",
    "QAEventReplay",
    "QAEventStream",
    "ResumeCursorError",
]
