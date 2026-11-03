"""Bounded, non-mutating raw-message reads for Yandex Mail IMAP."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import Field, StrictBytes, StrictInt, StrictStr, model_validator

from spine.application.connections.email_read import MailboxReadCommand
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


class YandexMailRawMessageFetchCode(str, Enum):
    COMPLETE = "complete"
    WRONG_SCOPE = "wrong_scope"
    WRONG_COMMAND = "wrong_command"
    MUTATION_DETECTED = "mutation_detected"
    RESET_RACE = "reset_race"
    UID_MISMATCH = "uid_mismatch"
    MESSAGE_MISSING = "message_missing"
    TRUNCATED = "truncated"
    MESSAGE_TOO_LARGE = "message_too_large"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    AUTHORIZATION_LOST = "authorization_lost"


class YandexMailRawMessageFetchCommand(MailboxReadCommand):
    """One exact folder-generation/UID fetch with a caller-owned byte ceiling."""

    schema_version: Literal["yandex-mail-raw-message-fetch-command:v1"] = (
        "yandex-mail-raw-message-fetch-command:v1"
    )
    uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    uid: StrictInt = Field(ge=1, le=2**32 - 1)
    max_message_bytes: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_raw_message_operation(self) -> "YandexMailRawMessageFetchCommand":
        if (
            self.operation is not EmailOperation.READ_RAW_MESSAGE
            or self.folder_or_label_reference is None
            or self.message_id != str(self.uid)
        ):
            raise ValueError(
                "Yandex Mail raw fetch requires one approved folder and exact UID"
            )
        return self


class YandexMailRawMessageFetchStart(DefinitionModel):
    """Private metadata received before the first RFC822 literal byte."""

    schema_version: Literal["yandex-mail-raw-message-fetch-start:v1"] = (
        "yandex-mail-raw-message-fetch-start:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    scope_generation: StrictInt = Field(ge=1)
    command_names: tuple[StrictStr, ...]
    fetch_data_item: StrictStr
    selected_uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    fetched_uid: StrictInt | None = Field(default=None, ge=1, le=2**32 - 1)
    declared_octets: StrictInt = Field(ge=0)
    envelope: StrictBytes | None = Field(default=None, repr=False, exclude=True)
    internal_date: datetime | None = None
    flags_before: tuple[StrictStr, ...] = Field(repr=False, exclude=True)
    message_found: bool = True


class YandexMailRawMessageLiteralChunk(DefinitionModel):
    """One binary-safe chunk yielded only after start evidence is admitted."""

    schema_version: Literal["yandex-mail-raw-message-literal-chunk:v1"] = (
        "yandex-mail-raw-message-literal-chunk:v1"
    )
    value: StrictBytes = Field(repr=False, exclude=True)


class YandexMailRawMessageFetchEnd(DefinitionModel):
    """Completion and flag evidence observed after the RFC822 literal."""

    schema_version: Literal["yandex-mail-raw-message-fetch-end:v1"] = (
        "yandex-mail-raw-message-fetch-end:v1"
    )
    command_names: tuple[StrictStr, ...]
    mutation_commands: tuple[
        Literal["STORE", "MOVE", "COPY", "EXPUNGE", "DELETE"], ...
    ] = ()
    flags_after: tuple[StrictStr, ...] = Field(repr=False, exclude=True)
    complete: bool


YandexMailRawMessageFetchEvent = (
    YandexMailRawMessageFetchStart
    | YandexMailRawMessageLiteralChunk
    | YandexMailRawMessageFetchEnd
)


@runtime_checkable
class YandexMailRawMessageFetchTranscript(Protocol):
    """Pull-based IMAP transcript that must not pre-buffer RFC822 literals."""

    async def read_start(self) -> YandexMailRawMessageFetchStart: ...

    async def read_next(
        self,
        *,
        max_bytes: int,
    ) -> YandexMailRawMessageLiteralChunk | YandexMailRawMessageFetchEnd | None: ...


class YandexMailRawMessageFetchResult(DefinitionModel):
    """Exact bytes and disclosure-safe completeness evidence for one UID."""

    schema_version: Literal["yandex-mail-raw-message-fetch-result:v1"] = (
        "yandex-mail-raw-message-fetch-result:v1"
    )
    code: YandexMailRawMessageFetchCode
    complete: bool
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID
    authorization_generation: StrictInt
    configuration_generation: StrictInt
    scope_generation: StrictInt
    uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    uid: StrictInt = Field(ge=1, le=2**32 - 1)
    raw_bytes: StrictBytes | None = Field(default=None, repr=False, exclude=True)
    content_length: StrictInt = Field(ge=0)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    declared_octets: StrictInt | None = Field(default=None, ge=0)
    envelope: StrictBytes | None = Field(default=None, repr=False, exclude=True)
    internal_date: datetime | None = None
    fetch_data_item: Literal["BODY.PEEK[]"] | None = None
    seen_preserved: bool | None = None
    mutation_commands: tuple[
        Literal["STORE", "MOVE", "COPY", "EXPUNGE", "DELETE"], ...
    ] = ()
    mutation_evidence_complete: bool = False


async def fetch_yandex_mail_raw_message(
    *,
    scope: MailboxReadScope,
    folder_reference: UUID,
    uid_validity: int,
    uid: int,
    max_message_bytes: int,
    transcript: YandexMailRawMessageFetchTranscript,
) -> YandexMailRawMessageFetchResult:
    """Admit start evidence before consuming a bounded BODY.PEEK literal stream."""

    try:
        first = await transcript.read_start()
    except (AttributeError, StopAsyncIteration):
        return failed_yandex_mail_raw_message_fetch(
            scope=scope,
            folder_reference=folder_reference,
            uid_validity=uid_validity,
            uid=uid,
            code=YandexMailRawMessageFetchCode.MALFORMED_RESPONSE,
        )
    if not isinstance(first, YandexMailRawMessageFetchStart):
        return failed_yandex_mail_raw_message_fetch(
            scope=scope,
            folder_reference=folder_reference,
            uid_validity=uid_validity,
            uid=uid,
            code=YandexMailRawMessageFetchCode.MALFORMED_RESPONSE,
        )
    start = first
    base = dict(
        scope=scope,
        folder_reference=folder_reference,
        uid_validity=uid_validity,
        uid=uid,
        start=start,
    )
    if not _start_matches_scope(scope, folder_reference, start):
        return _failed_result(code=YandexMailRawMessageFetchCode.WRONG_SCOPE, **base)
    if start.command_names != ("EXAMINE", "UID FETCH"):
        return _failed_result(code=YandexMailRawMessageFetchCode.WRONG_COMMAND, **base)
    if start.fetch_data_item != "BODY.PEEK[]":
        return _failed_result(
            code=YandexMailRawMessageFetchCode.MUTATION_DETECTED,
            seen_preserved=False,
            **base,
        )
    if start.selected_uid_validity != uid_validity:
        return _failed_result(code=YandexMailRawMessageFetchCode.RESET_RACE, **base)
    if not start.message_found:
        return _failed_result(
            code=YandexMailRawMessageFetchCode.MESSAGE_MISSING,
            seen_preserved=None,
            **base,
        )
    if start.fetched_uid != uid:
        return _failed_result(code=YandexMailRawMessageFetchCode.UID_MISMATCH, **base)
    if (
        isinstance(max_message_bytes, bool)
        or not isinstance(max_message_bytes, int)
        or max_message_bytes < 1
        or start.envelope is None
        or start.internal_date is None
        or start.internal_date.utcoffset() is None
    ):
        return _failed_result(
            code=YandexMailRawMessageFetchCode.MALFORMED_RESPONSE,
            seen_preserved=None,
            **base,
        )
    if start.declared_octets > max_message_bytes:
        return _incomplete_result(
            code=YandexMailRawMessageFetchCode.MESSAGE_TOO_LARGE,
            content=b"",
            seen_preserved=None,
            **base,
        )

    content = bytearray()
    end: YandexMailRawMessageFetchEnd | None = None
    while True:
        event = await transcript.read_next(
            max_bytes=max_message_bytes - len(content)
        )
        if event is None:
            break
        if isinstance(event, YandexMailRawMessageLiteralChunk) and end is None:
            if len(content) + len(event.value) > max_message_bytes:
                return _incomplete_result(
                    code=YandexMailRawMessageFetchCode.MESSAGE_TOO_LARGE,
                    content=bytes(content),
                    seen_preserved=None,
                    **base,
                )
            content.extend(event.value)
        elif isinstance(event, YandexMailRawMessageFetchEnd) and end is None:
            end = event
        else:
            return _failed_result(
                code=YandexMailRawMessageFetchCode.MALFORMED_RESPONSE,
                seen_preserved=None,
                **base,
            )

    raw_bytes = bytes(content)
    if end is None or not end.complete or len(raw_bytes) != start.declared_octets:
        return _incomplete_result(
            code=YandexMailRawMessageFetchCode.TRUNCATED,
            content=raw_bytes,
            seen_preserved=None,
            **base,
        )
    if (
        end.command_names != ("EXAMINE", "UID FETCH")
        or end.mutation_commands
        or end.flags_after != start.flags_before
    ):
        return _incomplete_result(
            code=YandexMailRawMessageFetchCode.MUTATION_DETECTED,
            content=raw_bytes,
            seen_preserved=False,
            mutation_commands=end.mutation_commands,
            mutation_evidence_complete=True,
            **base,
        )

    return YandexMailRawMessageFetchResult(
        code=YandexMailRawMessageFetchCode.COMPLETE,
        complete=True,
        **_result_identity(scope, folder_reference, uid_validity, uid),
        raw_bytes=raw_bytes,
        content_length=len(raw_bytes),
        content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        declared_octets=start.declared_octets,
        envelope=start.envelope,
        internal_date=start.internal_date,
        fetch_data_item="BODY.PEEK[]",
        seen_preserved=True,
        mutation_commands=end.mutation_commands,
        mutation_evidence_complete=True,
    )


def _start_matches_scope(
    scope: MailboxReadScope,
    folder_reference: UUID,
    start: YandexMailRawMessageFetchStart,
) -> bool:
    return (
        scope.provider_kind == "yandex_mail"
        and scope.allows(EmailOperation.READ_RAW_MESSAGE)
        and scope.allows_location(folder_reference)
        and start.workspace_id == scope.workspace_id
        and start.environment_id == scope.environment_id
        and start.connection_id == scope.connection_id
        and start.connection_version_id == scope.connection_version_id
        and start.mailbox_binding_id == scope.mailbox_binding_id
        and start.folder_reference == folder_reference
        and start.authorization_generation == scope.authorization_generation
        and start.configuration_generation == scope.configuration_generation
        and start.scope_generation == scope.scope_generation
    )


def _result_identity(
    scope: MailboxReadScope,
    folder_reference: UUID,
    uid_validity: int,
    uid: int,
) -> dict[str, object]:
    return {
        "workspace_id": scope.workspace_id,
        "environment_id": scope.environment_id,
        "connection_id": scope.connection_id,
        "connection_version_id": scope.connection_version_id,
        "mailbox_binding_id": scope.mailbox_binding_id,
        "folder_reference": folder_reference,
        "authorization_generation": scope.authorization_generation,
        "configuration_generation": scope.configuration_generation,
        "scope_generation": scope.scope_generation,
        "uid_validity": uid_validity,
        "uid": uid,
    }


def _incomplete_result(
    *,
    code: YandexMailRawMessageFetchCode,
    scope: MailboxReadScope,
    folder_reference: UUID,
    uid_validity: int,
    uid: int,
    start: YandexMailRawMessageFetchStart,
    content: bytes,
    seen_preserved: bool | None,
    mutation_commands: tuple[
        Literal["STORE", "MOVE", "COPY", "EXPUNGE", "DELETE"], ...
    ] = (),
    mutation_evidence_complete: bool = False,
) -> YandexMailRawMessageFetchResult:
    return YandexMailRawMessageFetchResult(
        code=code,
        complete=False,
        **_result_identity(scope, folder_reference, uid_validity, uid),
        raw_bytes=None,
        content_length=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        declared_octets=start.declared_octets,
        envelope=None,
        internal_date=start.internal_date,
        fetch_data_item="BODY.PEEK[]",
        seen_preserved=seen_preserved,
        mutation_commands=mutation_commands,
        mutation_evidence_complete=mutation_evidence_complete,
    )


def _failed_result(
    *,
    code: YandexMailRawMessageFetchCode,
    scope: MailboxReadScope,
    folder_reference: UUID,
    uid_validity: int,
    uid: int,
    start: YandexMailRawMessageFetchStart,
    seen_preserved: bool | None = None,
) -> YandexMailRawMessageFetchResult:
    return YandexMailRawMessageFetchResult(
        code=code,
        complete=False,
        **_result_identity(scope, folder_reference, uid_validity, uid),
        raw_bytes=None,
        content_length=0,
        content_sha256=None,
        declared_octets=start.declared_octets,
        envelope=None,
        internal_date=None,
        fetch_data_item=("BODY.PEEK[]" if start.fetch_data_item == "BODY.PEEK[]" else None),
        seen_preserved=seen_preserved,
    )


def failed_yandex_mail_raw_message_fetch(
    *,
    scope: MailboxReadScope,
    folder_reference: UUID,
    uid_validity: int,
    uid: int,
    code: YandexMailRawMessageFetchCode,
) -> YandexMailRawMessageFetchResult:
    """Create a content-free provider failure without retaining exception text."""

    if code not in {
        YandexMailRawMessageFetchCode.PROVIDER_UNAVAILABLE,
        YandexMailRawMessageFetchCode.AUTHORIZATION_LOST,
        YandexMailRawMessageFetchCode.MALFORMED_RESPONSE,
    }:
        raise ValueError("unsupported raw-message provider failure code")
    return YandexMailRawMessageFetchResult(
        code=code,
        complete=False,
        **_result_identity(scope, folder_reference, uid_validity, uid),
        raw_bytes=None,
        content_length=0,
        content_sha256=None,
        declared_octets=None,
        envelope=None,
        internal_date=None,
        fetch_data_item=None,
        seen_preserved=None,
    )


__all__ = [
    "YandexMailRawMessageFetchCode",
    "YandexMailRawMessageFetchCommand",
    "YandexMailRawMessageFetchEnd",
    "YandexMailRawMessageFetchEvent",
    "YandexMailRawMessageFetchResult",
    "YandexMailRawMessageFetchStart",
    "YandexMailRawMessageFetchTranscript",
    "YandexMailRawMessageLiteralChunk",
    "failed_yandex_mail_raw_message_fetch",
    "fetch_yandex_mail_raw_message",
]
