from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.gmail import (
    GmailHeaderRelationshipEvidence,
    GmailMessageKey,
    GmailThreadEvidence,
    GmailThreadKey,
    RfcMessageIdEvidence,
    gmail_source_identity,
)


CONNECTION = UUID("11111111-1111-1111-1111-111111111111")
MAILBOX = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_MAILBOX = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def test_gmail_source_identity_is_stable_and_qualified_by_mailbox() -> None:
    key = GmailMessageKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        message_id="18f4a7d9c2b6e031",
    )

    identity = gmail_source_identity(key)
    same_identity = gmail_source_identity(key.model_copy())
    other_mailbox_identity = gmail_source_identity(
        key.model_copy(update={"mailbox_binding_id": OTHER_MAILBOX})
    )

    assert identity == same_identity
    assert identity.provider_key == key
    assert identity.external_namespace == "gmail"
    assert identity.external_generation == "gmail-source-key:v1"
    assert identity.external_object_id.startswith("gm1:")
    assert identity.external_object_id != other_mailbox_identity.external_object_id


def test_gmail_source_identity_matches_its_versioned_golden_vector() -> None:
    key = GmailMessageKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        message_id="18f4a7d9c2b6e031",
    )

    assert gmail_source_identity(key).external_object_id == (
        "gm1:ccjms2mlougmcl6n7kv7qtfvewqosybohifeewabzjrknd732cgq"
    )


def test_gmail_source_identity_isolated_by_connection_and_message_id() -> None:
    key = GmailMessageKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        message_id="same-provider-id",
    )
    other_connection = key.model_copy(
        update={"connection_id": UUID("22222222-2222-2222-2222-222222222222")}
    )
    other_message = key.model_copy(update={"message_id": "other-provider-id"})

    assert gmail_source_identity(key).external_object_id != (
        gmail_source_identity(other_connection).external_object_id
    )
    assert gmail_source_identity(key).external_object_id != (
        gmail_source_identity(other_message).external_object_id
    )


@pytest.mark.parametrize(
    "message_id",
    ("", "has whitespace", "has\nnewline", "\x00"),
)
def test_gmail_message_key_rejects_invalid_provider_identifiers(
    message_id: str,
) -> None:
    with pytest.raises(ValidationError):
        GmailMessageKey(
            connection_id=CONNECTION,
            mailbox_binding_id=MAILBOX,
            message_id=message_id,
        )


def test_rfc_message_id_is_optional_untrusted_and_may_repeat_or_be_malformed() -> None:
    key = GmailMessageKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        message_id="provider-message-1",
    )

    absent = RfcMessageIdEvidence(message_key=key)
    malformed = RfcMessageIdEvidence(message_key=key, value="not a valid message id")
    duplicate = RfcMessageIdEvidence(message_key=key, value="not a valid message id")

    assert absent.value is None
    assert malformed.value == "not a valid message id"
    assert (malformed, duplicate)[0].value == (malformed, duplicate)[1].value
    assert gmail_source_identity(key).external_object_id == gmail_source_identity(
        key.model_copy()
    ).external_object_id


def test_duplicate_rfc_message_id_does_not_merge_provider_messages() -> None:
    first = GmailMessageKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        message_id="provider-message-1",
    )
    second = first.model_copy(update={"message_id": "provider-message-2"})
    first_header = RfcMessageIdEvidence(message_key=first, value="<duplicate@invalid>")
    second_header = RfcMessageIdEvidence(message_key=second, value="<duplicate@invalid>")

    assert first_header.value == second_header.value
    assert gmail_source_identity(first).external_object_id != (
        gmail_source_identity(second).external_object_id
    )
    assert set(gmail_source_identity(first).model_dump()) == {
        "schema_version",
        "external_namespace",
        "external_generation",
        "external_object_id",
        "provider_key",
    }


def test_gmail_thread_and_header_relationships_keep_distinct_authority() -> None:
    message_key = GmailMessageKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        message_id="message-1",
    )
    thread_key = GmailThreadKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        thread_id="thread-1",
    )
    provider_evidence = GmailThreadEvidence(
        message_key=message_key,
        thread_key=thread_key,
    )
    references = GmailHeaderRelationshipEvidence(
        message_key=message_key,
        relation="references",
        header_value="<prior@example.invalid>",
    )
    in_reply_to = GmailHeaderRelationshipEvidence(
        message_key=message_key,
        relation="in_reply_to",
        header_value="malformed header value",
    )

    assert provider_evidence.evidence_kind == "provider_reported_thread_id"
    assert references.evidence_kind == in_reply_to.evidence_kind == "header_inferred"
    assert references.relation == "references"
    assert in_reply_to.relation == "in_reply_to"
    assert in_reply_to.header_value == "malformed header value"


def test_gmail_thread_evidence_rejects_a_different_mailbox_scope() -> None:
    message_key = GmailMessageKey(
        connection_id=CONNECTION,
        mailbox_binding_id=MAILBOX,
        message_id="message-1",
    )
    thread_key = GmailThreadKey(
        connection_id=CONNECTION,
        mailbox_binding_id=OTHER_MAILBOX,
        thread_id="thread-1",
    )

    with pytest.raises(ValidationError, match="share the message mailbox scope"):
        GmailThreadEvidence(message_key=message_key, thread_key=thread_key)
