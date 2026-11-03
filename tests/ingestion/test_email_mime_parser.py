from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

import pytest

from spine.domain.artifacts.email_mime import MimeParseDisposition
from spine.ingestion.email_mime import MimeParser, MimeParserProfile


FIXTURES = Path(__file__).parents[1] / "fixtures" / "email_mime"


def _parser(**overrides: object) -> MimeParser:
    values: dict[str, object] = {
        "max_total_bytes": 25_000_000,
        "max_header_bytes": 256_000,
        "max_parts": 1_000,
        "max_depth": 32,
        "max_decoded_bytes": 50_000_000,
        "max_parse_seconds": 10.0,
    }
    clock = overrides.pop("clock", None)
    values.update(overrides)
    profile = MimeParserProfile(**values)  # type: ignore[arg-type]
    return MimeParser(profile=profile, **({"clock": clock} if clock is not None else {}))


def test_encoded_headers_and_multipart_alternative_produce_stable_part_tree() -> None:
    raw = (
        b"From: =?utf-8?b?0KLQtdGB0YI=?= <sender@example.test>\r\n"
        b"Subject: =?utf-8?b?0J/RgNC40LLQtdGC?=\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="alternative"\r\n'
        b"\r\n"
        b"--alternative\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"0KLQtdC70L4=\r\n"
        b"--alternative\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"<p>=D0=A2=D0=B5=D0=BB=D0=BE</p>\r\n"
        b"--alternative--\r\n"
    )

    first = _parser().parse(raw)
    second = _parser().parse(raw)

    assert first == second
    assert first.disposition is MimeParseDisposition.COMPLETE
    assert first.raw_sha256 == "adb8aa0ff0df61917bf6b786eb2898687d435ee1c9a750d45d30cc0138a21d1f"
    assert [(header.name, header.value) for header in first.headers] == [
        ("from", "Тест <sender@example.test>"),
        ("subject", "Привет"),
        ("mime-version", "1.0"),
        ("content-type", 'multipart/alternative; boundary="alternative"'),
    ]
    assert first.root is not None
    assert first.root.part_path == "1"
    assert first.root.media_type == "multipart/alternative"
    assert [part.part_path for part in first.root.children] == ["1.1", "1.2"]
    assert [part.text for part in first.root.children] == ["Тело", "<p>Тело</p>"]
    assert first.defects == ()


def test_mixed_inline_nested_and_embedded_message_have_stable_paths() -> None:
    raw = (
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="mixed"\r\n\r\n'
        b"--mixed\r\n"
        b'Content-Type: multipart/alternative; boundary="alternative"\r\n\r\n'
        b"--alternative\r\nContent-Type: text/plain\r\n\r\nplain\r\n"
        b"--alternative\r\nContent-Type: text/html\r\n\r\n"
        b'<img src="https://remote.invalid/pixel">\r\n'
        b"--alternative--\r\n"
        b"--mixed\r\n"
        b"Content-Type: image/png\r\n"
        b'Content-Disposition: inline; filename="../../provider-name.png"\r\n'
        b"Content-ID: <image-1>\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"c3ludGhldGljLWltYWdl\r\n"
        b"--mixed\r\nContent-Type: message/rfc822\r\n\r\n"
        b"Subject: embedded\r\nContent-Type: text/plain\r\n\r\nembedded body\r\n"
        b"--mixed--\r\n"
    )

    artifact = _parser().parse(raw)

    assert artifact.disposition is MimeParseDisposition.COMPLETE
    assert artifact.root is not None
    alternative, inline, embedded = artifact.root.children
    assert alternative.part_path == "1.1"
    assert [part.part_path for part in alternative.children] == ["1.1.1", "1.1.2"]
    assert alternative.children[1].text == '<img src="https://remote.invalid/pixel">'
    assert inline.part_path == "1.2"
    assert inline.content_disposition == "inline"
    assert inline.content_id == "<image-1>"
    assert inline.filename_present is True
    assert inline.text is None
    assert embedded.part_path == "1.3"
    assert embedded.media_type == "message/rfc822"
    assert [part.part_path for part in embedded.children] == ["1.3.1"]
    assert embedded.children[0].text == "embedded body"


def test_attachment_extraction_returns_exact_decoded_bytes_and_part_identity() -> None:
    raw = (FIXTURES / "golden_mixed.eml").read_bytes()

    result = _parser().parse_with_attachments(raw)

    assert result.artifact.disposition is MimeParseDisposition.COMPLETE
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.part_path == "1.2"
    assert attachment.media_type == "application/octet-stream"
    assert attachment.filename == "synthetic.txt"
    assert attachment.content == b"synthetic"
    assert attachment.sha256 == "b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2"


def test_inline_parts_are_extracted_separately_from_body_parts() -> None:
    raw = (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--b\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\n"
        b"Content-ID: <i>\r\nContent-Transfer-Encoding: base64\r\n\r\ncG5n\r\n--b--\r\n"
    )

    result = _parser().parse_with_attachments(raw)

    assert [(part.part_path, part.content, part.disposition) for part in result.attachments] == [
        ("1.2", b"png", "inline")
    ]


def test_total_byte_limit_returns_content_free_typed_disposition() -> None:
    raw = b"Subject: protected subject\r\n\r\nprotected message body"

    artifact = _parser(max_total_bytes=len(raw) - 1).parse(raw)

    assert artifact.disposition is MimeParseDisposition.LIMIT_EXCEEDED
    assert artifact.root is None
    assert artifact.headers == ()
    assert [(defect.part_path, defect.code) for defect in artifact.defects] == [
        ("1", "total_bytes_exceeded")
    ]
    serialized = artifact.model_dump_json()
    assert "protected" not in serialized


def _two_part_message() -> bytes:
    return (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\none\r\n"
        b"--b\r\nContent-Type: text/plain\r\n\r\ntwo\r\n--b--\r\n"
    )


def _nested_message() -> bytes:
    return (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="a"\r\n\r\n'
        b'--a\r\nContent-Type: multipart/alternative; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\ndeep\r\n--b--\r\n--a--\r\n"
    )


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


@pytest.mark.parametrize(
    ("raw", "parser_factory", "expected_code"),
    [
        pytest.param(
            b"Subject: protected subject\r\n\r\nbody",
            lambda: _parser(max_header_bytes=10),
            "header_bytes_exceeded",
            id="header-bytes",
        ),
        pytest.param(
            _two_part_message(),
            lambda: _parser(max_parts=2),
            "part_count_exceeded",
            id="part-count",
        ),
        pytest.param(
            _nested_message(),
            lambda: _parser(max_depth=2),
            "depth_exceeded",
            id="depth",
        ),
        pytest.param(
            b"Content-Type: text/plain\r\n\r\nprotected body",
            lambda: _parser(max_decoded_bytes=5),
            "decoded_bytes_exceeded",
            id="decoded-bytes",
        ),
        pytest.param(
            b"Content-Type: text/plain\r\n\r\nbody",
            lambda: _parser(max_parse_seconds=1, clock=_Clock(0, 2)),
            "time_exceeded",
            id="elapsed-time",
        ),
    ],
)
def test_injected_resource_limit_returns_typed_content_free_disposition(
    raw: bytes,
    parser_factory: Callable[[], MimeParser],
    expected_code: str,
) -> None:
    artifact = parser_factory().parse(raw)

    assert artifact.disposition is MimeParseDisposition.LIMIT_EXCEEDED
    assert artifact.root is None
    assert artifact.headers == ()
    assert [defect.code for defect in artifact.defects] == [expected_code]
    assert "protected" not in artifact.model_dump_json()


def test_malformed_unpadded_base64_cannot_bypass_decoded_byte_limit() -> None:
    raw = b"Content-Transfer-Encoding: base64\r\n\r\nYWI"

    artifact = _parser(max_decoded_bytes=1).parse(raw)

    assert artifact.disposition is MimeParseDisposition.LIMIT_EXCEEDED
    assert [defect.code for defect in artifact.defects] == ["decoded_bytes_exceeded"]


def test_raw_8bit_body_returns_typed_charset_defect() -> None:
    raw = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n\xff"
    )

    artifact = _parser().parse(raw)

    assert artifact.disposition is MimeParseDisposition.MALFORMED
    assert [defect.code for defect in artifact.defects] == ["invalid_charset"]


@pytest.mark.parametrize(
    ("raw", "expected_codes"),
    [
        pytest.param(
            b"Content-Type: text/plain; charset=private-invalid-charset\r\n\r\nsecret body",
            ["invalid_charset"],
            id="charset",
        ),
        pytest.param(
            b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            b"%%%private-invalid-transfer%%%",
            ["invalid_transfer_encoding"],
            id="transfer-encoding",
        ),
        pytest.param(
            b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="private"\r\n'
            b"\r\nsecret body without boundary",
            ["start_boundary_not_found", "multipart_invariant_violation"],
            id="boundary",
        ),
        pytest.param(
            b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nsecret truncated body",
            ["close_boundary_not_found"],
            id="truncation",
        ),
    ],
)
def test_malformed_input_preserves_content_free_deterministic_defects(
    raw: bytes,
    expected_codes: list[str],
) -> None:
    first = _parser().parse(raw)
    second = _parser().parse(raw)

    assert first == second
    assert first.disposition is MimeParseDisposition.MALFORMED
    assert first.root is not None
    assert [defect.code for defect in first.defects] == expected_codes
    assert all(defect.part_path == "1" for defect in first.defects)
    serialized_defects = "".join(defect.model_dump_json() for defect in first.defects)
    assert "private" not in serialized_defects
    assert "secret" not in serialized_defects


def test_missing_optional_message_metadata_defaults_to_plain_text() -> None:
    artifact = _parser().parse(b"\r\nsynthetic body")

    assert artifact.disposition is MimeParseDisposition.COMPLETE
    assert artifact.headers == ()
    assert artifact.root is not None
    assert artifact.root.media_type == "text/plain"
    assert artifact.root.text == "synthetic body"


def test_pathological_header_returns_typed_defect_without_raising_raw_error() -> None:
    raw = b"X" * 999 + b": protected header value\r\n\r\nsynthetic body"

    artifact = _parser().parse(raw)

    assert artifact.disposition is MimeParseDisposition.MALFORMED
    assert [defect.code for defect in artifact.defects] == ["invalid_header"]
    assert "protected" not in artifact.defects[0].model_dump_json()


def test_golden_mime_artifact_replays_exactly() -> None:
    raw = (FIXTURES / "golden_mixed.eml").read_bytes()
    expected = json.loads((FIXTURES / "golden_mixed.json").read_text(encoding="utf-8"))

    artifact = _parser().parse(raw)

    assert artifact.model_dump(mode="json") == expected


def test_parser_profile_pins_limits_but_not_injected_clock() -> None:
    baseline = _parser()

    assert baseline.profile.reference != _parser(max_parts=1).profile.reference
    assert baseline.profile.reference == _parser(clock=_Clock(0)).profile.reference


@pytest.mark.parametrize(
    "parser_factory",
    [
        pytest.param(lambda: _parser(max_total_bytes=0), id="total-bytes"),
        pytest.param(lambda: _parser(max_header_bytes=0), id="header-bytes"),
        pytest.param(lambda: _parser(max_parts=0), id="parts"),
        pytest.param(lambda: _parser(max_depth=0), id="depth"),
        pytest.param(lambda: _parser(max_decoded_bytes=0), id="decoded-bytes"),
        pytest.param(lambda: _parser(max_parse_seconds=0), id="time"),
    ],
)
def test_non_positive_parser_limit_is_rejected(
    parser_factory: Callable[[], MimeParser],
) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        parser_factory()
