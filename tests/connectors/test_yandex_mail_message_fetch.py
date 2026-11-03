from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchCommand,
    YandexMailRawMessageFetchEnd,
    YandexMailRawMessageFetchEvent,
    YandexMailRawMessageFetchStart,
    YandexMailRawMessageLiteralChunk,
    fetch_yandex_mail_raw_message,
)
from spine.domain.connections.email_scope import (
    EmailOperation,
    EmailProviderOperation,
    MailboxReadScope,
)


_RAW_MESSAGE = b"Subject: Synthetic\r\n\r\nhello\x00world\r\n"


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope(*, workspace_id: UUID = _id(1)) -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=workspace_id,
        environment_id=_id(2),
        connection_id=_id(3),
        connection_version_id=_id(4),
        provider_kind="yandex_mail",
        resource_scope_reference=_id(5),
        mailbox_subject_reference=_id(6),
        mailbox_binding_id=_id(7),
        authorization_generation=8,
        configuration_generation=9,
        scope_generation=10,
        allowed_folder_references=(_id(11),),
        allowed_operations=(EmailProviderOperation.READ_RAW_MESSAGE,),
    )


def _start(**changes: object) -> YandexMailRawMessageFetchStart:
    scope = _scope()
    values: dict[str, object] = {
        "workspace_id": scope.workspace_id,
        "environment_id": scope.environment_id,
        "connection_id": scope.connection_id,
        "connection_version_id": scope.connection_version_id,
        "mailbox_binding_id": scope.mailbox_binding_id,
        "folder_reference": _id(11),
        "authorization_generation": scope.authorization_generation,
        "configuration_generation": scope.configuration_generation,
        "scope_generation": scope.scope_generation,
        "command_names": ("EXAMINE", "UID FETCH"),
        "fetch_data_item": "BODY.PEEK[]",
        "selected_uid_validity": 101,
        "fetched_uid": 42,
        "declared_octets": len(_RAW_MESSAGE),
        "envelope": b"synthetic private envelope",
        "internal_date": datetime(2026, 11, 3, 9, 15, tzinfo=timezone.utc),
        "flags_before": ("\\Flagged",),
        "message_found": True,
    }
    values.update(changes)
    return YandexMailRawMessageFetchStart(**values)


class _ScriptedTranscript:
    def __init__(self, *events: YandexMailRawMessageFetchEvent) -> None:
        assert events and isinstance(events[0], YandexMailRawMessageFetchStart)
        self._start = events[0]
        self._events = events[1:]
        self._index = 0
        self.yielded = 0
        self.max_bytes_requests: list[int] = []

    def __repr__(self) -> str:
        return "_ScriptedTranscript(private_events=True)"

    async def read_start(self):
        self.yielded += 1
        return self._start

    async def read_next(self, *, max_bytes: int):
        self.max_bytes_requests.append(max_bytes)
        if self._index == len(self._events):
            return None
        event = self._events[self._index]
        self._index += 1
        self.yielded += 1
        return event


def _transcript(
    *,
    start: YandexMailRawMessageFetchStart | None = None,
    chunks: tuple[bytes, ...] = (_RAW_MESSAGE[:7], _RAW_MESSAGE[7:23], _RAW_MESSAGE[23:]),
    end: YandexMailRawMessageFetchEnd | None = None,
) -> _ScriptedTranscript:
    return _ScriptedTranscript(
        start or _start(),
        *(YandexMailRawMessageLiteralChunk(value=chunk) for chunk in chunks),
        end
        or YandexMailRawMessageFetchEnd(
            command_names=("EXAMINE", "UID FETCH"),
            flags_after=("\\Flagged",),
            complete=True,
        ),
    )


async def _fetch(
    transcript: _ScriptedTranscript,
    *,
    scope: MailboxReadScope | None = None,
    max_message_bytes: int = 1024,
):
    return await fetch_yandex_mail_raw_message(
        scope=scope or _scope(),
        folder_reference=_id(11),
        uid_validity=101,
        uid=42,
        max_message_bytes=max_message_bytes,
        transcript=transcript,
    )


@pytest.mark.asyncio
async def test_qualified_body_peek_fetch_retains_exact_bounded_content_evidence() -> None:
    result = await _fetch(_transcript())

    assert result.code is YandexMailRawMessageFetchCode.COMPLETE
    assert result.complete is True
    assert result.raw_bytes == _RAW_MESSAGE
    assert result.content_length == 35
    assert result.content_sha256 == (
        "fbf50165ae80e122c23c1dbabb75a27127849919278b4cf4fa521d4711586b72"
    )
    assert result.declared_octets == 35
    assert result.envelope == b"synthetic private envelope"
    assert result.internal_date == datetime(2026, 11, 3, 9, 15, tzinfo=timezone.utc)
    assert result.fetch_data_item == "BODY.PEEK[]"
    assert result.seen_preserved is True
    assert result.mutation_commands == ()
    assert result.mutation_evidence_complete is True


def test_raw_fetch_command_rejects_provider_message_id_that_differs_from_uid() -> None:
    scope = _scope()

    with pytest.raises(ValidationError, match="exact UID"):
        YandexMailRawMessageFetchCommand(
            scope=scope,
            operation=EmailOperation.READ_RAW_MESSAGE,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=_id(11),
            message_id="43",
            uid_validity=101,
            uid=42,
            max_message_bytes=1024,
        )


@pytest.mark.asyncio
async def test_truncated_literal_returns_typed_safe_incomplete_outcome() -> None:
    transcript = _transcript(
        start=_start(declared_octets=len(_RAW_MESSAGE) + 5),
        end=YandexMailRawMessageFetchEnd(
            command_names=("EXAMINE", "UID FETCH"),
            flags_after=("\\Flagged",),
            complete=False,
        ),
    )

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.TRUNCATED
    assert result.complete is False
    assert result.raw_bytes is None
    assert result.content_length == len(_RAW_MESSAGE)
    assert result.declared_octets == len(_RAW_MESSAGE) + 5
    assert result.envelope is None
    assert "Subject" not in repr(result)


@pytest.mark.asyncio
async def test_declared_oversize_stops_before_first_literal_chunk() -> None:
    transcript = _transcript(
        start=_start(declared_octets=len(_RAW_MESSAGE) + 1024),
        chunks=(_RAW_MESSAGE, b"x" * 1024),
    )

    result = await _fetch(transcript, max_message_bytes=64)

    assert result.code is YandexMailRawMessageFetchCode.MESSAGE_TOO_LARGE
    assert result.raw_bytes is None
    assert result.content_length == 0
    assert result.declared_octets == len(_RAW_MESSAGE) + 1024
    assert result.envelope is None
    assert transcript.yielded == 1
    assert transcript.max_bytes_requests == []


@pytest.mark.asyncio
async def test_undeclared_oversize_is_rejected_at_requested_remaining_bound() -> None:
    transcript = _transcript(
        start=_start(declared_octets=60),
        chunks=(b"a" * 40, b"b" * 30),
    )

    result = await _fetch(transcript, max_message_bytes=64)

    assert result.code is YandexMailRawMessageFetchCode.MESSAGE_TOO_LARGE
    assert result.raw_bytes is None
    assert result.content_length == 40
    assert transcript.max_bytes_requests == [64, 24]


@pytest.mark.asyncio
async def test_cross_workspace_start_is_rejected_before_literal_disclosure() -> None:
    transcript = _transcript()

    result = await _fetch(transcript, scope=_scope(workspace_id=_id(99)))

    assert result.code is YandexMailRawMessageFetchCode.WRONG_SCOPE
    assert result.raw_bytes is None
    assert result.content_sha256 is None
    assert transcript.yielded == 1
    assert "Subject" not in repr(result)


@pytest.mark.asyncio
async def test_wrong_command_transcript_is_rejected_before_literal_read() -> None:
    transcript = _transcript(start=_start(command_names=("SELECT", "UID FETCH")))

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.WRONG_COMMAND
    assert transcript.yielded == 1


@pytest.mark.asyncio
async def test_non_peek_body_fetch_is_classified_as_mutation() -> None:
    transcript = _transcript(start=_start(fetch_data_item="BODY[]"))

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.MUTATION_DETECTED
    assert result.seen_preserved is False
    assert transcript.yielded == 1


@pytest.mark.asyncio
async def test_uidvalidity_reset_race_is_rejected_before_literal_read() -> None:
    transcript = _transcript(start=_start(selected_uid_validity=102))

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.RESET_RACE
    assert transcript.yielded == 1


@pytest.mark.asyncio
async def test_wrong_fetched_uid_is_rejected_before_literal_read() -> None:
    transcript = _transcript(start=_start(fetched_uid=43))

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.UID_MISMATCH
    assert transcript.yielded == 1


@pytest.mark.asyncio
async def test_missing_uid_returns_typed_outcome_without_literal_read() -> None:
    transcript = _transcript(
        start=_start(message_found=False, fetched_uid=None, declared_octets=0),
        chunks=(),
    )

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.MESSAGE_MISSING
    assert result.raw_bytes is None
    assert transcript.yielded == 1


@pytest.mark.asyncio
async def test_seen_flag_change_withholds_fetched_bytes() -> None:
    transcript = _transcript(
        end=YandexMailRawMessageFetchEnd(
            command_names=("EXAMINE", "UID FETCH"),
            flags_after=("\\Flagged", "\\Seen"),
            complete=True,
        )
    )

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.MUTATION_DETECTED
    assert result.raw_bytes is None
    assert result.seen_preserved is False
    assert result.mutation_evidence_complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ("STORE", "MOVE", "COPY", "EXPUNGE", "DELETE"))
async def test_mutation_command_in_completed_transcript_withholds_bytes(
    command: str,
) -> None:
    transcript = _transcript(
        end=YandexMailRawMessageFetchEnd(
            command_names=("EXAMINE", "UID FETCH"),
            mutation_commands=(command,),
            flags_after=("\\Flagged",),
            complete=True,
        )
    )

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.MUTATION_DETECTED
    assert result.raw_bytes is None
    assert result.mutation_commands == (command,)
    assert result.mutation_evidence_complete is True


@pytest.mark.asyncio
async def test_malformed_envelope_evidence_never_reads_message_bytes() -> None:
    transcript = _transcript(start=_start(envelope=None))

    result = await _fetch(transcript)

    assert result.code is YandexMailRawMessageFetchCode.MALFORMED_RESPONSE
    assert result.raw_bytes is None
    assert transcript.yielded == 1


@pytest.mark.asyncio
async def test_transcript_and_result_serialization_exclude_private_payload_fields() -> None:
    start = _start()
    chunk = YandexMailRawMessageLiteralChunk(value=_RAW_MESSAGE)
    transcript = _transcript(start=start, chunks=(_RAW_MESSAGE,))
    result = await _fetch(transcript)

    assert "envelope" not in start.model_dump()
    assert "flags_before" not in start.model_dump()
    assert "value" not in chunk.model_dump()
    assert "raw_bytes" not in result.model_dump()
    assert "envelope" not in result.model_dump()
    assert "Subject" not in repr(transcript)
    assert "private envelope" not in repr(start)
