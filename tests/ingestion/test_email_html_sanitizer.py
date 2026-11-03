from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from spine.domain.artifacts.email_mime import MimeParseDisposition
from spine.ingestion.email_html import EmailHtmlSanitizer, EmailHtmlSanitizerProfile
from spine.ingestion.email_mime import MimeParser, MimeParserProfile

FIXTURES = Path(__file__).parents[1] / "fixtures" / "email_html"


def _mime_parser(**overrides: object) -> MimeParser:
    values: dict[str, object] = {
        "max_total_bytes": 1_000_000,
        "max_header_bytes": 10_000,
        "max_parts": 100,
        "max_depth": 10,
        "max_decoded_bytes": 1_000_000,
        "max_parse_seconds": 5.0,
    }
    values.update(overrides)
    return MimeParser(profile=MimeParserProfile(**values))  # type: ignore[arg-type]


def _html_artifact(html: str):
    encoded = html.encode("utf-8")
    raw = (
        b"MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        + encoded
    )
    return _mime_parser().parse(raw), raw


def _sanitizer(**overrides: object) -> EmailHtmlSanitizer:
    profile_values: dict[str, object] = {
        "max_total_input_bytes": 100_000,
        "max_total_output_bytes": 200_000,
        "max_parts": 100,
        "max_depth": 20,
    }
    profile_values.update(overrides)
    return EmailHtmlSanitizer(profile=EmailHtmlSanitizerProfile(**profile_values))  # type: ignore[arg-type]


def test_sanitizer_removes_active_content_and_remote_resources_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (FIXTURES / "adversarial_input.html").read_text()
    golden = json.loads((FIXTURES / "adversarial_golden.json").read_text())
    parsed, raw = _html_artifact(html)
    attempted_connections: list[object] = []

    def refuse_network(*args: object, **kwargs: object) -> None:
        attempted_connections.append((args, kwargs))
        raise AssertionError("sanitization attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    first = _sanitizer().sanitize(parsed)
    second = _sanitizer().sanitize(parsed)

    assert first == second
    assert parsed.root is not None
    assert parsed.root.text == html
    assert parsed.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert first.source_sha256 == parsed.raw_sha256
    assert first.source_part_path == "1"
    assert first.disposition.value == "complete"
    assert first.sanitized_html == golden["sanitized_html"]
    assert first.plain_text == golden["plain_text"]
    assert "tracker.invalid" not in (first.sanitized_html or "")
    assert "javascript:" not in (first.sanitized_html or "")
    assert attempted_connections == []


@pytest.mark.parametrize(
    ("html", "expected_html", "expected_text"),
    [
        (
            "<p>One &amp; two</p><p>Three<br>four</p>",
            "<p>One &amp; two</p><p>Three<br>four</p>",
            "One & two\nThree\nfour",
        ),
        (
            "<table><tr><td>A</td><td>B</td></tr></table>",
            "<table><tbody><tr><td>A</td><td>B</td></tr></tbody></table>",
            "A\nB",
        ),
        (
            "<p title='x' style='color:red'>caf&eacute;</p><!--hidden-->",
            "<p>café</p>",
            "café",
        ),
        (
            "<p>broken <b>formatting",
            "<p>broken <b>formatting</b></p>",
            "broken formatting",
        ),
    ],
)
def test_sanitizer_emits_stable_html_and_plain_text(
    html: str,
    expected_html: str,
    expected_text: str,
) -> None:
    parsed, _ = _html_artifact(html)

    result = _sanitizer().sanitize(parsed)

    assert result.sanitized_html == expected_html
    assert result.plain_text == expected_text


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java&#x73;cript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//tracker.invalid/pixel",
        "/relative/path",
    ],
)
def test_sanitizer_removes_dangerous_or_relative_url_schemes(url: str) -> None:
    parsed, _ = _html_artifact(f'<a href="{url}">label</a>')

    result = _sanitizer().sanitize(parsed)

    assert result.sanitized_html == "<a>label</a>"
    assert url not in (result.sanitized_html or "")


def test_sanitizer_decodes_declared_legacy_charset_before_cleaning() -> None:
    raw = (
        b"MIME-Version: 1.0\r\nContent-Type: text/html; charset=iso-8859-1\r\n\r\n"
        b"<p>caf\xe9</p>"
    )
    parsed = _mime_parser().parse(raw)

    result = _sanitizer().sanitize(parsed)

    assert result.sanitized_html == "<p>café</p>"
    assert result.plain_text == "café"
    assert result.source_sha256 == hashlib.sha256(raw).hexdigest()


def test_sanitizer_returns_typed_limit_result_without_source_content() -> None:
    secret = "private subject and body"
    parsed, _ = _html_artifact(f"<p>{secret}</p>")

    result = _sanitizer(max_total_input_bytes=4).sanitize(parsed)

    assert result.disposition.value == "input_limit_exceeded"
    assert result.sanitized_html is None
    assert result.plain_text is None
    assert secret not in str(result)


def test_sanitizer_rejects_output_that_exceeds_configured_bound() -> None:
    parsed, _ = _html_artifact("<p>long body</p>")

    result = _sanitizer(max_total_output_bytes=3).sanitize(parsed)

    assert result.disposition.value == "output_limit_exceeded"
    assert result.sanitized_html is None
    assert result.plain_text is None


def test_sanitizer_skips_attachments_and_embedded_messages() -> None:
    raw = (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/html\r\n\r\n<p>body</p>\r\n"
        b"--b\r\nContent-Type: text/html\r\n"
        b'Content-Disposition: attachment; filename="ignore.html"\r\n\r\n'
        b"<script>attachment content</script>\r\n"
        b"--b\r\nContent-Type: message/rfc822\r\n\r\n"
        b"Content-Type: text/html\r\n\r\n<p>embedded message</p>\r\n"
        b"--b--\r\n"
    )
    parsed = _mime_parser().parse(raw)

    results = _sanitizer().sanitize_all(parsed)

    assert parsed.disposition is MimeParseDisposition.COMPLETE
    assert len(results) == 1
    assert results[0].source_part_path == "1.1"
    assert results[0].sanitized_html == "<p>body</p>"


def test_sanitizer_returns_typed_result_for_mime_parser_limit() -> None:
    parsed = _mime_parser(max_total_bytes=1).parse(b"Content-Type: text/html\r\n\r\n<p>x</p>")

    results = _sanitizer().sanitize_all(parsed)

    assert len(results) == 1
    assert results[0].disposition.value == "source_unavailable"
    assert results[0].sanitized_html is None
