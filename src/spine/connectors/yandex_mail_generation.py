"""Fail-closed, read-only evidence for one approved IMAP folder generation."""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBytes, StrictInt, StrictStr, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


_MAX_IMAP_NUMBER = 2**32 - 1
_UID_VALIDITY = re.compile(rb"\[UIDVALIDITY\s+([0-9]+)\]", re.IGNORECASE)
_UID_NEXT = re.compile(rb"\[UIDNEXT\s+([0-9]+)\]", re.IGNORECASE)
_COMPLETION = re.compile(rb"^(?!\*)[^ ]+\s+(OK|NO|BAD)(?:\s|$)", re.IGNORECASE)


class YandexMailFolderGenerationCode(str, Enum):
    COMPLETE = "complete"
    WRONG_SCOPE = "wrong_scope"
    WRONG_COMMAND = "wrong_command"
    SERVER_REJECTED = "server_rejected"
    UIDNOTSTICKY = "uid_not_sticky"
    UIDVALIDITY_MISSING = "uid_validity_missing"
    UIDVALIDITY_INVALID = "uid_validity_invalid"
    UIDVALIDITY_CHANGED = "uid_validity_changed"
    UIDNEXT_MISSING = "uid_next_missing"
    UIDNEXT_INVALID = "uid_next_invalid"
    INCONSISTENT_SERVER_STATE = "inconsistent_server_state"
    MALFORMED_RESPONSE = "malformed_response"
    READ_WRITE_SELECTION = "read_write_selection"


class YandexMailFolderExamineTranscript(DefinitionModel):
    """Synthetic or adapter-captured EXAMINE transcript; raw lines stay private."""

    schema_version: Literal["yandex-mail-folder-examine-transcript:v1"] = (
        "yandex-mail-folder-examine-transcript:v1"
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
    response_lines: tuple[StrictBytes, ...] = Field(
        repr=False, exclude=True, max_length=64
    )

    @model_validator(mode="after")
    def validate_transcript_size(self) -> "YandexMailFolderExamineTranscript":
        if sum(map(len, self.response_lines)) > 65_536:
            raise ValueError("IMAP folder response exceeds the observation limit")
        return self


class YandexMailFolderGenerationObservation(DefinitionModel):
    """Typed evidence from EXAMINE; UIDNEXT is a cursor hint, never identity."""

    schema_version: Literal["yandex-mail-folder-generation-observation:v1"] = (
        "yandex-mail-folder-generation-observation:v1"
    )
    complete: bool
    code: YandexMailFolderGenerationCode
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID | None = None
    authorization_generation: StrictInt
    configuration_generation: StrictInt
    scope_generation: StrictInt
    uid_validity: StrictInt | None = Field(default=None, ge=1, le=_MAX_IMAP_NUMBER)
    uid_next: StrictInt | None = Field(default=None, ge=1, le=_MAX_IMAP_NUMBER)
    selection_command: Literal["EXAMINE"] | None = None
    read_only: bool | None = None
    uid_next_is_identity: Literal[False] = False
    sequence_numbers_are_identity: Literal[False] = False
    absence_is_deletion_proof: Literal[False] = False


def observe_yandex_mail_folder_generation(
    *,
    scope: MailboxReadScope,
    folder_reference: UUID,
    transcript: YandexMailFolderExamineTranscript,
    expected_uid_validity: int | None = None,
) -> YandexMailFolderGenerationObservation:
    """Validate scope and parse an EXAMINE transcript without exposing server text."""

    base = dict(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
    )
    if (
        scope.provider_kind != "yandex_mail"
        or not scope.allows(EmailOperation.OBSERVE_FOLDER_GENERATION)
        or not scope.allows_location(folder_reference)
        or transcript.workspace_id != scope.workspace_id
        or transcript.environment_id != scope.environment_id
        or transcript.connection_id != scope.connection_id
        or transcript.connection_version_id != scope.connection_version_id
        or transcript.mailbox_binding_id != scope.mailbox_binding_id
        or transcript.folder_reference != folder_reference
        or transcript.authorization_generation != scope.authorization_generation
        or transcript.configuration_generation != scope.configuration_generation
        or transcript.scope_generation != scope.scope_generation
    ):
        return _failed(YandexMailFolderGenerationCode.WRONG_SCOPE, base)
    if transcript.command_names != ("EXAMINE",):
        return _failed(YandexMailFolderGenerationCode.WRONG_COMMAND, base)
    if expected_uid_validity is not None and (
        isinstance(expected_uid_validity, bool)
        or not isinstance(expected_uid_validity, int)
        or not 1 <= expected_uid_validity <= _MAX_IMAP_NUMBER
    ):
        return _failed(YandexMailFolderGenerationCode.UIDVALIDITY_INVALID, base)

    lines = transcript.response_lines
    completions = [
        match.group(1).upper()
        for line in lines
        if (match := _COMPLETION.match(line))
    ]
    if not completions or len(completions) != 1 or completions[0] != b"OK":
        return _failed(YandexMailFolderGenerationCode.SERVER_REJECTED, base)
    completion_line = next(line for line in lines if _COMPLETION.match(line))
    if b"[READ-WRITE]" in completion_line.upper():
        return _failed(
            YandexMailFolderGenerationCode.READ_WRITE_SELECTION,
            base,
            selection_command="EXAMINE",
            read_only=False,
        )
    if b"[READ-ONLY]" not in completion_line.upper():
        return _failed(
            YandexMailFolderGenerationCode.SERVER_REJECTED,
            base,
            selection_command="EXAMINE",
        )
    if any(b"UIDNOTSTICKY" in line.upper() for line in lines):
        return _failed(
            YandexMailFolderGenerationCode.UIDNOTSTICKY,
            base,
            selection_command="EXAMINE",
            read_only=True,
        )

    validity_values = _numbers(_UID_VALIDITY, lines)
    next_values = _numbers(_UID_NEXT, lines)
    if len(validity_values) > 1 or len(next_values) > 1:
        return _failed(
            YandexMailFolderGenerationCode.INCONSISTENT_SERVER_STATE,
            base,
            selection_command="EXAMINE",
            read_only=True,
        )
    if not validity_values:
        code = (
            YandexMailFolderGenerationCode.UIDVALIDITY_INVALID
            if any(b"UIDVALIDITY" in line.upper() for line in lines)
            else YandexMailFolderGenerationCode.UIDVALIDITY_MISSING
        )
        return _failed(code, base, selection_command="EXAMINE", read_only=True)
    uid_validity = validity_values[0]
    if not 1 <= uid_validity <= _MAX_IMAP_NUMBER:
        return _failed(
            YandexMailFolderGenerationCode.UIDVALIDITY_INVALID,
            base,
            selection_command="EXAMINE",
            read_only=True,
        )
    if expected_uid_validity is not None and uid_validity != expected_uid_validity:
        observed_uid_next = next_values[0] if len(next_values) == 1 else None
        if observed_uid_next is not None and not (
            1 <= observed_uid_next <= _MAX_IMAP_NUMBER
        ):
            observed_uid_next = None
        return YandexMailFolderGenerationObservation(
            **base,
            complete=False,
            code=YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED,
            folder_reference=folder_reference,
            uid_validity=uid_validity,
            uid_next=observed_uid_next,
            selection_command="EXAMINE",
            read_only=True,
        )
    if not next_values:
        code = (
            YandexMailFolderGenerationCode.UIDNEXT_INVALID
            if any(b"UIDNEXT" in line.upper() for line in lines)
            else YandexMailFolderGenerationCode.UIDNEXT_MISSING
        )
        return _failed(code, base, selection_command="EXAMINE", read_only=True)
    uid_next = next_values[0]
    if not 1 <= uid_next <= _MAX_IMAP_NUMBER:
        return _failed(
            YandexMailFolderGenerationCode.UIDNEXT_INVALID,
            base,
            selection_command="EXAMINE",
            read_only=True,
        )

    return YandexMailFolderGenerationObservation(
        **base,
        complete=True,
        code=YandexMailFolderGenerationCode.COMPLETE,
        folder_reference=folder_reference,
        uid_validity=uid_validity,
        uid_next=uid_next,
        selection_command="EXAMINE",
        read_only=True,
    )


def failed_yandex_mail_folder_generation_observation(
    *,
    scope: MailboxReadScope,
    code: YandexMailFolderGenerationCode,
) -> YandexMailFolderGenerationObservation:
    """Build a content-free typed failure for a failed admitted EXAMINE."""

    return _failed(
        code,
        {
            "workspace_id": scope.workspace_id,
            "environment_id": scope.environment_id,
            "connection_id": scope.connection_id,
            "connection_version_id": scope.connection_version_id,
            "mailbox_binding_id": scope.mailbox_binding_id,
            "authorization_generation": scope.authorization_generation,
            "configuration_generation": scope.configuration_generation,
            "scope_generation": scope.scope_generation,
        },
    )


def _numbers(pattern: re.Pattern[bytes], lines: tuple[bytes, ...]) -> list[int]:
    values: list[int] = []
    for line in lines:
        for match in pattern.finditer(line):
            try:
                values.append(int(match.group(1)))
            except (ValueError, OverflowError):
                values.append(0)
    return values


def _failed(
    code: YandexMailFolderGenerationCode,
    scope_values: dict[str, object],
    *,
    selection_command: Literal["EXAMINE"] | None = None,
    read_only: bool | None = None,
) -> YandexMailFolderGenerationObservation:
    return YandexMailFolderGenerationObservation(
        **scope_values,
        complete=False,
        code=code,
        selection_command=selection_command,
        read_only=read_only,
    )


__all__ = [
    "YandexMailFolderExamineTranscript",
    "YandexMailFolderGenerationCode",
    "YandexMailFolderGenerationObservation",
    "failed_yandex_mail_folder_generation_observation",
    "observe_yandex_mail_folder_generation",
]
