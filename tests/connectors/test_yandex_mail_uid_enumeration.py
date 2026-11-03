from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.connectors.yandex_mail_uid_enumeration import (
    YandexMailUidEnumerationPageRequest,
    YandexMailUidEnumerationPageTranscript,
    YandexMailUidMessageMetadata,
    YandexMailUidPageCode,
    normalize_yandex_mail_uid_page,
)
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderGenerationCode,
    YandexMailFolderGenerationObservation,
)
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope() -> MailboxReadScope:
    from spine.domain.connections.email_scope import EmailProviderOperation

    return MailboxReadScope(
        workspace_id=_id(1),
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
        allowed_operations=(EmailProviderOperation.ENUMERATE_FOLDER_UIDS,),
    )


def test_uid_page_reports_exact_coverage_and_explicit_continuation() -> None:
    scope = _scope()
    folder = _id(11)
    request = YandexMailUidEnumerationPageRequest(
        scope=scope,
        operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=folder,
        generation=_generation(scope, folder),
        start_uid=1,
        enumeration_max_items=10,
        enumeration_max_response_bytes=4096,
    )
    transcript = YandexMailUidEnumerationPageTranscript(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=folder,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=101,
        command_names=("UID SEARCH", "UID FETCH"),
        search_uids=(4, 7),
        fetched_messages=(
            YandexMailUidMessageMetadata(uid=4, flags=("\\Seen",), size_octets=120),
            YandexMailUidMessageMetadata(uid=7, flags=(), size_octets=240),
        ),
        response_bytes=512,
        search_complete=True,
        fetch_complete=True,
    )

    result = normalize_yandex_mail_uid_page(request=request, transcript=transcript)

    assert result.complete is True
    assert result.code is YandexMailUidPageCode.COMPLETE
    assert result.covered_uid_range == (1, 10)
    assert tuple(message.uid for message in result.messages) == (4, 7)
    assert result.continuation_uid == 11
    assert result.uid_validity == 101
    assert result.sequence_numbers_are_identity is False


def _generation(
    scope: MailboxReadScope, folder_reference: UUID
) -> YandexMailFolderGenerationObservation:
    return YandexMailFolderGenerationObservation(
        complete=True,
        code=YandexMailFolderGenerationCode.COMPLETE,
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=folder_reference,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=101,
        uid_next=21,
        selection_command="EXAMINE",
        read_only=True,
    )


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    (
        ({"search_uids": (4, 4, 7)}, YandexMailUidPageCode.MALFORMED_RESPONSE),
        (
            {"fetched_messages": (YandexMailUidMessageMetadata(uid=4, size_octets=120),)},
            YandexMailUidPageCode.MALFORMED_RESPONSE,
        ),
        ({"search_uids": (7, 4)}, YandexMailUidPageCode.MALFORMED_RESPONSE),
        ({"fetch_complete": False}, YandexMailUidPageCode.PARTIAL_BATCH),
        (
            {"command_names": ("UID SEARCH", "UID STORE")},
            YandexMailUidPageCode.UNSAFE_COMMANDS,
        ),
        ({"uid_validity": 102}, YandexMailUidPageCode.UIDVALIDITY_CHANGED),
        ({"response_bytes": 4097}, YandexMailUidPageCode.RESPONSE_LIMIT_EXCEEDED),
        ({"folder_reference": _id(12)}, YandexMailUidPageCode.WRONG_SCOPE),
    ),
)
def test_uid_page_uncertain_transcript_never_claims_complete_coverage(
    changes: dict[str, object], expected_code: YandexMailUidPageCode
) -> None:
    scope = _scope()
    folder = _id(11)
    request = YandexMailUidEnumerationPageRequest(
        scope=scope,
        operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=folder,
        generation=_generation(scope, folder),
        start_uid=1,
        enumeration_max_items=10,
        enumeration_max_response_bytes=4096,
    )
    transcript = YandexMailUidEnumerationPageTranscript(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=folder,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=101,
        command_names=("UID SEARCH", "UID FETCH"),
        search_uids=(4, 7),
        fetched_messages=(
            YandexMailUidMessageMetadata(uid=4, size_octets=120),
            YandexMailUidMessageMetadata(uid=7, size_octets=240),
        ),
        response_bytes=512,
        search_complete=True,
        fetch_complete=True,
    ).model_copy(update=changes)

    result = normalize_yandex_mail_uid_page(request=request, transcript=transcript)

    assert result.complete is False
    assert result.code is expected_code
    assert result.covered_uid_range is None
    assert result.continuation_uid is None


def test_uid_page_rejects_generation_from_another_folder_or_connection() -> None:
    scope = _scope()
    folder = _id(11)
    generation = _generation(scope, folder).model_copy(
        update={"connection_id": _id(99)}
    )

    with pytest.raises(ValidationError):
        YandexMailUidEnumerationPageRequest(
            scope=scope,
            operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=folder,
            generation=generation,
            start_uid=1,
            enumeration_max_items=10,
            enumeration_max_response_bytes=4096,
        )


def test_empty_uid_generation_has_complete_empty_coverage() -> None:
    scope = _scope()
    folder = _id(11)
    request = YandexMailUidEnumerationPageRequest(
        scope=scope,
        operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=folder,
        generation=_generation(scope, folder).model_copy(update={"uid_next": 1}),
        start_uid=1,
        enumeration_max_items=10,
        enumeration_max_response_bytes=4096,
    )
    transcript = YandexMailUidEnumerationPageTranscript(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=folder,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=101,
        command_names=(),
        search_uids=(),
        fetched_messages=(),
        response_bytes=0,
        search_complete=True,
        fetch_complete=True,
    )

    result = normalize_yandex_mail_uid_page(request=request, transcript=transcript)

    assert result.complete is True
    assert result.covered_uid_range is None
    assert result.continuation_uid is None
