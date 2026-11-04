from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.artifacts.email_message import (
    EmailMessageCitationLocator,
    decode_email_message_locator,
    encode_email_message_locator,
)


def _locator(**updates: object) -> EmailMessageCitationLocator:
    values: dict[str, object] = {
        "source_revision_id": UUID(int=1),
        "source_revision_digest": "1" * 64,
        "artifact_id": UUID(int=2),
        "artifact_digest": "2" * 64,
        "parser_profile": f"mime-parser:v1:{'3' * 64}",
        "sanitizer_profile": f"email-html-sanitizer:v1:{'4' * 64}",
        "part_path": "1.2",
        "representation": "sanitized_html_text",
        "start_codepoint": 5,
        "end_codepoint": 12,
        "representation_digest": "5" * 64,
        "excerpt_digest": "6" * 64,
    }
    values.update(updates)
    return EmailMessageCitationLocator(**values)


def test_email_message_locator_has_a_stable_round_trip_vector() -> None:
    locator = _locator()

    reference = encode_email_message_locator(locator)

    assert reference == (
        "em1|00000000000000000000000000000001|"
        + "1" * 64
        + "|00000000000000000000000000000002|"
        + "2" * 64
        + "|"
        + "3" * 64
        + "|"
        + "4" * 64
        + "|1.2|h|5|12|"
        + "5" * 64
        + "|"
        + "6" * 64
    )
    assert len(reference) <= 512
    assert decode_email_message_locator(reference) == locator


@pytest.mark.parametrize(
    "updates",
    (
        {"source_revision_id": UUID(int=0)},
        {"artifact_id": UUID(int=0)},
        {"end_codepoint": 5},
        {"part_path": "attachment"},
    ),
)
def test_email_message_locator_rejects_incomplete_or_invalid_identity(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _locator(**updates)


@pytest.mark.parametrize(
    "reference",
    (
        "em2|fabricated",
        "em1|fabricated",
        "em1|00000000000000000000000000000001|protected text",
    ),
)
def test_email_message_locator_decoder_rejects_fabricated_references(
    reference: str,
) -> None:
    assert decode_email_message_locator(reference) is None
