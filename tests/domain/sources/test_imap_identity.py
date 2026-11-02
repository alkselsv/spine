from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.imap import (
    ImapMessageKey,
    ImapMessageSimilarityEvidence,
    ImapSourceIdentity,
    imap_external_generation,
    imap_source_identity,
)


WORKSPACE = UUID("11111111-1111-1111-1111-111111111111")
OTHER_WORKSPACE = UUID("22222222-2222-2222-2222-222222222222")
CONNECTION = UUID("33333333-3333-3333-3333-333333333333")
MAILBOX = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
FOLDER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_FOLDER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def message_key(**updates: object) -> ImapMessageKey:
    fields: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "connection_id": CONNECTION,
        "mailbox_binding_id": MAILBOX,
        "folder_binding_id": FOLDER,
        "uid_validity": 123456789,
        "uid": 42,
    }
    fields.update(updates)
    return ImapMessageKey(**fields)


def test_imap_identity_golden_vector_is_stable_and_bounded() -> None:
    identity = imap_source_identity(message_key())

    assert identity.external_namespace == "imap"
    assert identity.external_generation == "imap-uidvalidity-v1:123456789"
    assert identity.external_object_id == (
        "im1:2pharta5lmjumxr33kq2bhx36tee5iepayypgnc6qxokry7a63xa"
    )
    assert len(identity.external_object_id) <= 64


@pytest.mark.parametrize(
    "changes",
    (
        {"workspace_id": OTHER_WORKSPACE},
        {"folder_binding_id": OTHER_FOLDER},
        {"uid_validity": 123456790},
        {"uid": 43},
    ),
)
def test_imap_identity_isolated_by_tenant_folder_reset_and_uid(
    changes: dict[str, object],
) -> None:
    assert imap_source_identity(message_key()).external_object_id != (
        imap_source_identity(message_key(**changes)).external_object_id
    )


@pytest.mark.parametrize("field", ("sequence_number", "uid_next"))
def test_imap_message_key_rejects_non_identity_sequence_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        message_key(**{field: 42})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("uid", 0),
        ("uid", 2**32),
        ("uid_validity", 0),
        ("uid_validity", 2**32),
        ("uid", True),
        ("uid_validity", "123"),
    ),
)
def test_imap_message_key_rejects_zero_out_of_range_and_coerced_numbers(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        message_key(**{field: value})


@pytest.mark.parametrize(
    "field",
    ("workspace_id", "connection_id", "mailbox_binding_id", "folder_binding_id"),
)
def test_imap_message_key_rejects_zero_scope_identifiers(field: str) -> None:
    with pytest.raises(ValidationError):
        message_key(**{field: UUID(int=0)})


def test_imap_external_generation_rejects_boolean_uidvalidity() -> None:
    with pytest.raises(ValueError, match="UIDVALIDITY"):
        imap_external_generation(True)


@pytest.mark.parametrize("basis", ("message_id", "content_sha256"))
def test_matching_similarity_evidence_does_not_merge_message_identity(
    basis: str,
) -> None:
    first_key = message_key(uid=42)
    second_key = message_key(uid=42, folder_binding_id=OTHER_FOLDER)
    common = {
        "first_message_key": first_key,
        "second_message_key": second_key,
        "basis": basis,
        "value": "<same@invalid>"
        if basis == "message_id"
        else "a" * 64,
    }

    evidence = ImapMessageSimilarityEvidence(**common)

    assert evidence.first_message_key != evidence.second_message_key
    assert imap_source_identity(first_key).external_object_id != (
        imap_source_identity(second_key).external_object_id
    )
    assert not hasattr(evidence, "continuity")
    with pytest.raises(ValidationError):
        ImapMessageSimilarityEvidence(**common, continuity="same_message")

    if basis == "content_sha256":
        with pytest.raises(ValidationError, match="SHA-256 digest"):
            ImapMessageSimilarityEvidence(**{**common, "value": "z" * 64})


def test_imap_source_identity_rejects_a_mismatched_opaque_id() -> None:
    identity = imap_source_identity(message_key())
    colliding_key_identity = imap_source_identity(message_key(uid=43))

    with pytest.raises(ValidationError, match="does not match provider key"):
        ImapSourceIdentity(
            **identity.model_dump()
            | {"external_object_id": colliding_key_identity.external_object_id}
        )


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        (
            {},
            "im1:2pharta5lmjumxr33kq2bhx36tee5iepayypgnc6qxokry7a63xa",
        ),
        (
            {"workspace_id": OTHER_WORKSPACE},
            "im1:fqkjrcj6z2wab4nkc4nomlx2pusc3qwsrcjiw3usprhy4nh63uca",
        ),
        (
            {"folder_binding_id": OTHER_FOLDER},
            "im1:nbupdhxdlfx5tr3pqxwrzcawmda6btg2624tzlubeujkjsyvibgq",
        ),
        (
            {"uid_validity": 123456790},
            "im1:p4s3qklpziaipvxowinsixbnjqav65dof34qheyfc6x2ryq6rrcq",
        ),
        (
            {"uid": 43},
            "im1:vd6ppha65vg6r6jvz5qkv3wmyhjoj2afjvphfmuq56xdm3g6cyja",
        ),
    ),
)
def test_imap_identity_matches_versioned_scope_and_reset_vectors(
    changes: dict[str, object],
    expected: str,
) -> None:
    identity = imap_source_identity(message_key(**changes))

    assert identity.external_object_id == expected
    assert len(identity.external_object_id) <= 64
