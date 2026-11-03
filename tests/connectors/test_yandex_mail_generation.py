from __future__ import annotations

from uuid import UUID

import pytest

from spine.connectors.yandex_mail_generation import (
    YandexMailFolderExamineTranscript,
    YandexMailFolderGenerationCode,
    observe_yandex_mail_folder_generation,
)
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope() -> MailboxReadScope:
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
        allowed_operations=(EmailProviderOperation.OBSERVE_FOLDER_GENERATION,),
    )


def _transcript(
    *,
    lines: tuple[bytes, ...] = (
        b"* OK [UIDVALIDITY 101] stable UID generation",
        b"* OK [UIDNEXT 42] next available UID",
        b"A001 OK [READ-ONLY] EXAMINE completed",
    ),
    commands: tuple[str, ...] = ("EXAMINE",),
    **changes: object,
) -> YandexMailFolderExamineTranscript:
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
        "command_names": commands,
        "response_lines": lines,
    }
    values.update(changes)
    return YandexMailFolderExamineTranscript(**values)


def test_examine_records_exact_folder_generation_without_identity_or_deletion_claims() -> None:
    scope = _scope()

    result = observe_yandex_mail_folder_generation(
        scope=scope,
        folder_reference=_id(11),
        transcript=_transcript(),
    )

    assert result.complete is True
    assert result.code is YandexMailFolderGenerationCode.COMPLETE
    assert result.folder_reference == _id(11)
    assert result.connection_id == scope.connection_id
    assert result.authorization_generation == scope.authorization_generation
    assert result.configuration_generation == scope.configuration_generation
    assert result.uid_validity == 101
    assert result.uid_next == 42
    assert result.scope_generation == scope.scope_generation
    assert result.selection_command == "EXAMINE"
    assert result.read_only is True
    assert result.uid_next_is_identity is False
    assert result.sequence_numbers_are_identity is False
    assert result.absence_is_deletion_proof is False


@pytest.mark.parametrize(
    ("lines", "expected"),
    (
        ((b"* OK [UIDNEXT 42]", b"A001 OK [READ-ONLY] EXAMINE"), YandexMailFolderGenerationCode.UIDVALIDITY_MISSING),
        ((b"* OK [UIDVALIDITY 0]", b"* OK [UIDNEXT 42]", b"A001 OK [READ-ONLY]"), YandexMailFolderGenerationCode.UIDVALIDITY_INVALID),
        ((b"* OK [UIDVALIDITY 101]", b"A001 OK [READ-ONLY]"), YandexMailFolderGenerationCode.UIDNEXT_MISSING),
        ((b"* OK [UIDVALIDITY 101]", b"* OK [UIDNEXT 0]", b"A001 OK [READ-ONLY]"), YandexMailFolderGenerationCode.UIDNEXT_INVALID),
        ((b"* OK [UIDVALIDITY 101]", b"* OK [UIDVALIDITY 102]", b"* OK [UIDNEXT 42]", b"A001 OK [READ-ONLY]"), YandexMailFolderGenerationCode.INCONSISTENT_SERVER_STATE),
        ((b"* OK [UIDVALIDITY 101]", b"* OK [UIDNEXT 42]", b"* OK [UIDNOTSTICKY]", b"A001 OK [READ-ONLY]"), YandexMailFolderGenerationCode.UIDNOTSTICKY),
        ((b"A001 NO [AUTHENTICATIONFAILED] private@example.test",), YandexMailFolderGenerationCode.SERVER_REJECTED),
    ),
)
def test_invalid_examine_transcript_fails_closed_with_typed_code(
    lines: tuple[bytes, ...], expected: YandexMailFolderGenerationCode
) -> None:
    result = observe_yandex_mail_folder_generation(
        scope=_scope(),
        folder_reference=_id(11),
        transcript=_transcript(lines=lines),
    )

    assert result.complete is False
    assert result.code is expected
    assert result.uid_validity is None
    assert result.uid_next is None


def test_changed_uidvalidity_requires_reconciliation() -> None:
    result = observe_yandex_mail_folder_generation(
        scope=_scope(),
        folder_reference=_id(11),
        transcript=_transcript(),
        expected_uid_validity=100,
    )

    assert result.complete is False
    assert result.code is YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED
    assert result.uid_validity == 101
    assert result.uid_next == 42


@pytest.mark.parametrize(
    ("folder_reference", "transcript_changes"),
    (
        (_id(99), {}),
        (_id(11), {"scope_generation": 99}),
        (_id(11), {"authorization_generation": 99}),
        (_id(11), {"configuration_generation": 99}),
        (_id(11), {"connection_id": _id(99)}),
        (_id(11), {"folder_reference": _id(99)}),
        (_id(11), {"connection_version_id": _id(99)}),
        (_id(11), {"mailbox_binding_id": _id(99)}),
    ),
)
def test_wrong_folder_or_generation_is_content_free_denial(
    folder_reference: UUID, transcript_changes: dict[str, object]
) -> None:
    result = observe_yandex_mail_folder_generation(
        scope=_scope(),
        folder_reference=folder_reference,
        transcript=_transcript(**transcript_changes),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderGenerationCode.WRONG_SCOPE


@pytest.mark.parametrize(
    "commands", (("SELECT",), ("EXAMINE", "STORE"), ("EXAMINE", "EXPUNGE"))
)
def test_only_examine_command_is_accepted(commands: tuple[str, ...]) -> None:
    result = observe_yandex_mail_folder_generation(
        scope=_scope(),
        folder_reference=_id(11),
        transcript=_transcript(commands=commands),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderGenerationCode.WRONG_COMMAND


def test_read_write_selection_is_rejected() -> None:
    result = observe_yandex_mail_folder_generation(
        scope=_scope(),
        folder_reference=_id(11),
        transcript=_transcript(
            lines=(
                b"* OK [UIDVALIDITY 101]",
                b"* OK [UIDNEXT 42]",
                b"A001 OK [READ-WRITE] SELECT complete",
            )
        ),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderGenerationCode.READ_WRITE_SELECTION


def test_raw_server_diagnostics_are_safe() -> None:
    failed = observe_yandex_mail_folder_generation(
        scope=_scope(),
        folder_reference=_id(11),
        transcript=_transcript(
            lines=(b"A001 NO private subject address@example.test provider payload",)
        ),
    )

    assert failed.code is YandexMailFolderGenerationCode.SERVER_REJECTED
    assert "private subject" not in repr(failed)
    assert "address@example.test" not in repr(failed)
    assert "provider payload" not in repr(failed)
    assert "private subject" not in repr(_transcript())
    assert "private subject" not in str(_transcript().model_dump())
