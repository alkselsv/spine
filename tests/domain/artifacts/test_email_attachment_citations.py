from uuid import UUID

import pytest

from spine.domain.artifacts.email_attachment import (
    EmailAttachmentCitationLocator,
    decode_email_attachment_locator,
    encode_email_attachment_locator,
)


def _locator(**updates: object) -> EmailAttachmentCitationLocator:
    values = {
        "parent_source_object_id": UUID(int=1),
        "parent_source_revision_id": UUID(int=2),
        "parent_message_artifact_id": UUID(int=3),
        "parent_mime_part_path": "1.2",
        "parent_locator_digest": "a" * 64,
        "attachment_artifact_id": UUID(int=4),
        "attachment_artifact_digest": "b" * 64,
        "processing_receipt_id": UUID(int=5),
        "processing_receipt_digest": "c" * 64,
        "parsed_artifact_id": UUID(int=6),
        "child_locator_kind": "character_range",
        "child_locator_value": "line:2:4-12",
    }
    values.update(updates)
    return EmailAttachmentCitationLocator(**values)  # type: ignore[arg-type]


def test_email_attachment_locator_round_trip_preserves_exact_lineage() -> None:
    locator = _locator()

    reference = encode_email_attachment_locator(locator)

    assert reference.startswith("ea1|")
    assert len(reference) <= 512
    assert decode_email_attachment_locator(reference) == locator


@pytest.mark.parametrize(
    "reference",
    (
        "em1|wrong-contract",
        "ea1|not-enough-fields",
        encode_email_attachment_locator(_locator()).replace("|1.2|", "|01.2|"),
        encode_email_attachment_locator(_locator()) + "|extra",
    ),
)
def test_malformed_email_attachment_locator_fails_closed(reference: str) -> None:
    assert decode_email_attachment_locator(reference) is None


def test_email_attachment_locator_rejects_unbounded_child_reference() -> None:
    locator = _locator(child_locator_value="x" * 160)

    with pytest.raises(ValueError, match="stable locator limit"):
        encode_email_attachment_locator(locator)
