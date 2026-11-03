"""Bounded, read-only UID SEARCH/FETCH pages for approved Yandex folders."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from spine.application.connections.email_read import MailboxReadCommand
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderGenerationCode,
    YandexMailFolderGenerationObservation,
)
from spine.domain.connections.email_scope import EmailOperation
from spine.domain.common import DefinitionModel


_MAX_IMAP_NUMBER = 2**32 - 1
_ALLOWED_COMMANDS = {("UID SEARCH",), ("UID SEARCH", "UID FETCH")}


class YandexMailUidPageCode(str, Enum):
    COMPLETE = "complete"
    PARTIAL_BATCH = "partial_batch"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_LIMIT_EXCEEDED = "response_limit_exceeded"
    UNSAFE_COMMANDS = "unsafe_commands"
    WRONG_SCOPE = "wrong_scope"
    UIDVALIDITY_CHANGED = "uid_validity_changed"
    SERVER_REJECTED = "server_rejected"


class YandexMailUidEnumerationPageRequest(MailboxReadCommand):
    """One finite UID interval, pinned to an observed folder generation."""

    schema_version: Literal["yandex-mail-uid-enumeration-page:v1"] = (
        "yandex-mail-uid-enumeration-page:v1"
    )
    generation: YandexMailFolderGenerationObservation
    start_uid: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)

    @model_validator(mode="after")
    def validate_uid_page(self) -> "YandexMailUidEnumerationPageRequest":
        if (
            self.operation is not EmailOperation.ENUMERATE_FOLDER_UIDS
            or self.scope.provider_kind != "yandex_mail"
            or self.enumeration_max_items is None
            or self.enumeration_max_response_bytes is None
            or self.folder_or_label_reference is None
            or not self.scope.allows_location(self.folder_or_label_reference)
            or not self._generation_matches_scope()
            or self.start_uid > self.uid_next
        ):
            raise ValueError("Yandex Mail UID page request is outside its approved scope")
        return self

    def _generation_matches_scope(self) -> bool:
        generation = self.generation
        scope = self.scope
        return (
            generation.complete
            and generation.code is YandexMailFolderGenerationCode.COMPLETE
            and generation.uid_validity is not None
            and generation.uid_next is not None
            and generation.selection_command == "EXAMINE"
            and generation.read_only is True
            and generation.folder_reference == self.folder_or_label_reference
            and generation.workspace_id == scope.workspace_id
            and generation.environment_id == scope.environment_id
            and generation.connection_id == scope.connection_id
            and generation.connection_version_id == scope.connection_version_id
            and generation.mailbox_binding_id == scope.mailbox_binding_id
            and generation.authorization_generation == scope.authorization_generation
            and generation.configuration_generation == scope.configuration_generation
            and generation.scope_generation == scope.scope_generation
        )

    @property
    def folder_reference(self) -> UUID:
        assert self.folder_or_label_reference is not None
        return self.folder_or_label_reference

    @property
    def uid_validity(self) -> int:
        assert self.generation.uid_validity is not None
        return self.generation.uid_validity

    @property
    def uid_next(self) -> int:
        assert self.generation.uid_next is not None
        return self.generation.uid_next

    @property
    def end_uid(self) -> int:
        """Inclusive end UID; UIDNEXT itself is excluded from the snapshot."""

        assert self.enumeration_max_items is not None
        return min(
            self.start_uid + self.enumeration_max_items - 1,
            self.uid_next - 1,
        )

    @property
    def continuation_uid(self) -> int | None:
        next_uid = self.end_uid + 1
        return next_uid if next_uid < self.uid_next else None


class YandexMailUidMessageMetadata(DefinitionModel):
    """Content-free metadata fetched for one qualified UID."""

    schema_version: Literal["yandex-mail-uid-message-metadata:v1"] = (
        "yandex-mail-uid-message-metadata:v1"
    )
    uid: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)
    flags: tuple[Annotated[StrictStr, Field(max_length=128)], ...] = Field(
        default=(), max_length=64
    )
    size_octets: StrictInt = Field(ge=0)


class YandexMailUidEnumerationPageTranscript(DefinitionModel):
    """Synthetic/adapter transcript after bounded UID SEARCH and UID FETCH."""

    schema_version: Literal["yandex-mail-uid-enumeration-transcript:v1"] = (
        "yandex-mail-uid-enumeration-transcript:v1"
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
    uid_validity: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)
    command_names: tuple[StrictStr, ...] = Field(max_length=2)
    search_uids: tuple[StrictInt, ...] = Field(max_length=5000)
    fetched_messages: tuple[YandexMailUidMessageMetadata, ...] = Field(max_length=5000)
    response_bytes: StrictInt = Field(ge=0)
    search_complete: StrictBool
    fetch_complete: StrictBool


class YandexMailUidEnumerationPageResult(DefinitionModel):
    """Typed page evidence; continuation exists only after proven coverage."""

    schema_version: Literal["yandex-mail-uid-enumeration-result:v1"] = (
        "yandex-mail-uid-enumeration-result:v1"
    )
    complete: StrictBool
    code: YandexMailUidPageCode
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID
    authorization_generation: StrictInt
    configuration_generation: StrictInt
    scope_generation: StrictInt
    uid_validity: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)
    covered_uid_range: tuple[StrictInt, StrictInt] | None = None
    messages: tuple[YandexMailUidMessageMetadata, ...] = ()
    continuation_uid: StrictInt | None = Field(default=None, ge=1, le=_MAX_IMAP_NUMBER)
    sequence_numbers_are_identity: Literal[False] = False


def normalize_yandex_mail_uid_page(
    *,
    request: YandexMailUidEnumerationPageRequest,
    transcript: YandexMailUidEnumerationPageTranscript,
) -> YandexMailUidEnumerationPageResult:
    """Accept only an exact, ordered search/fetch batch for the requested range."""

    base = _result_base(request)
    if not _transcript_matches_request(request, transcript):
        return _result(request, YandexMailUidPageCode.WRONG_SCOPE, base)
    if transcript.uid_validity != request.uid_validity:
        return _result(request, YandexMailUidPageCode.UIDVALIDITY_CHANGED, base)
    start_uid, end_uid = request.start_uid, request.end_uid
    expected_commands = (
        ()
        if start_uid > end_uid
        else (
            ("UID SEARCH",)
            if not transcript.search_complete or not transcript.search_uids
            else ("UID SEARCH", "UID FETCH")
        )
    )
    if transcript.command_names not in _ALLOWED_COMMANDS and transcript.command_names != ():
        return _result(request, YandexMailUidPageCode.UNSAFE_COMMANDS, base)
    if transcript.command_names != expected_commands:
        return _result(request, YandexMailUidPageCode.UNSAFE_COMMANDS, base)
    if transcript.response_bytes > request.enumeration_max_response_bytes:
        return _result(request, YandexMailUidPageCode.RESPONSE_LIMIT_EXCEEDED, base)

    searched = transcript.search_uids
    fetched = transcript.fetched_messages
    sorted_uids = tuple(sorted(searched))
    fetched_uids = tuple(message.uid for message in fetched)
    ordered_fetch = tuple(sorted(fetched, key=lambda message: message.uid))
    expected = sorted_uids
    page_span = max(0, end_uid - start_uid + 1)
    malformed = (
        len(searched) > page_span
        or len(fetched) > page_span
        or any(uid < start_uid or uid > end_uid for uid in searched)
        or len(set(searched)) != len(searched)
        or searched != sorted_uids
        or len(set(fetched_uids)) != len(fetched_uids)
        or fetched_uids != tuple(sorted(fetched_uids))
        or fetched_uids != expected
    )
    if malformed:
        code = YandexMailUidPageCode.MALFORMED_RESPONSE
    elif not transcript.search_complete or not transcript.fetch_complete:
        code = YandexMailUidPageCode.PARTIAL_BATCH
    else:
        return YandexMailUidEnumerationPageResult(
            **base,
            complete=True,
            code=YandexMailUidPageCode.COMPLETE,
            uid_validity=request.uid_validity,
            covered_uid_range=(start_uid, end_uid) if start_uid <= end_uid else None,
            messages=ordered_fetch,
            continuation_uid=request.continuation_uid,
        )
    return _result(
        request,
        code,
        base,
        messages=ordered_fetch,
    )


def _transcript_matches_request(
    request: YandexMailUidEnumerationPageRequest,
    transcript: YandexMailUidEnumerationPageTranscript,
) -> bool:
    scope = request.scope
    return (
        transcript.workspace_id == scope.workspace_id
        and transcript.environment_id == scope.environment_id
        and transcript.connection_id == scope.connection_id
        and transcript.connection_version_id == scope.connection_version_id
        and transcript.mailbox_binding_id == scope.mailbox_binding_id
        and transcript.folder_reference == request.folder_reference
        and transcript.authorization_generation == scope.authorization_generation
        and transcript.configuration_generation == scope.configuration_generation
        and transcript.scope_generation == scope.scope_generation
    )


def _result_base(request: YandexMailUidEnumerationPageRequest) -> dict[str, object]:
    scope = request.scope
    return {
        "workspace_id": scope.workspace_id,
        "environment_id": scope.environment_id,
        "connection_id": scope.connection_id,
        "connection_version_id": scope.connection_version_id,
        "mailbox_binding_id": scope.mailbox_binding_id,
        "folder_reference": request.folder_reference,
        "authorization_generation": scope.authorization_generation,
        "configuration_generation": scope.configuration_generation,
        "scope_generation": scope.scope_generation,
    }


def _result(
    request: YandexMailUidEnumerationPageRequest,
    code: YandexMailUidPageCode,
    base: dict[str, object],
    *,
    messages: tuple[YandexMailUidMessageMetadata, ...] = (),
) -> YandexMailUidEnumerationPageResult:
    return YandexMailUidEnumerationPageResult(
        **base,
        complete=False,
        code=code,
        uid_validity=request.uid_validity,
        messages=messages,
        continuation_uid=None,
    )


def failed_yandex_mail_uid_page(
    *,
    request: YandexMailUidEnumerationPageRequest,
    code: YandexMailUidPageCode,
) -> YandexMailUidEnumerationPageResult:
    """Return a content-free incomplete page after a provider-side failure."""

    return _result(request, code, _result_base(request))


__all__ = [
    "YandexMailUidEnumerationPageRequest",
    "YandexMailUidEnumerationPageResult",
    "YandexMailUidEnumerationPageTranscript",
    "YandexMailUidMessageMetadata",
    "YandexMailUidPageCode",
    "failed_yandex_mail_uid_page",
    "normalize_yandex_mail_uid_page",
]
