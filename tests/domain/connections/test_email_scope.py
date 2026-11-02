from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.connections.email_scope import (
    EmailOperation,
    EmailProviderOperation,
    MailboxReadScope,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope(**updates: object) -> MailboxReadScope:
    values: dict[str, object] = {
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "connection_id": _id(3),
        "connection_version_id": _id(4),
        "provider_kind": "google",
        "resource_scope_reference": _id(5),
        "mailbox_subject_reference": _id(6),
        "mailbox_binding_id": _id(7),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "scope_generation": 10,
        "allowed_label_references": (_id(11),),
        "excluded_label_references": (_id(12),),
        "allowed_operations": (EmailProviderOperation.READ_MESSAGE_METADATA,),
    }
    values.update(updates)
    return MailboxReadScope(**values)


def test_scope_is_frozen_versioned_and_contains_only_opaque_references() -> None:
    scope = _scope()

    assert scope.schema_version == "mailbox-read-scope:v1"
    assert scope.allows(EmailOperation.READ_MESSAGE_METADATA)
    assert not scope.allows(EmailOperation.READ_RAW_MESSAGE)
    assert not scope.allows(EmailOperation.PROCESS_CONTENT)
    assert not scope.allows(EmailOperation.READ_CONTENT)
    with pytest.raises(ValidationError):
        MailboxReadScope.model_validate(
            scope.model_dump() | {"mailbox_address": "private@example.test"}
        )
    with pytest.raises(ValidationError):
        scope.mailbox_binding_id = _id(99)


def test_scope_rejects_overlapping_or_provider_incompatible_locations() -> None:
    with pytest.raises(ValidationError):
        _scope(
            allowed_label_references=(_id(11),),
            excluded_label_references=(_id(11),),
        )
    with pytest.raises(ValidationError):
        _scope(allowed_folder_references=(_id(13),))


def test_scope_checks_exclusions_and_exact_location_membership() -> None:
    scope = _scope()

    assert scope.allows_location(_id(11))
    assert not scope.allows_location(_id(12))
    assert not scope.allows_location(_id(13))
    assert not scope.allows_location(None)
