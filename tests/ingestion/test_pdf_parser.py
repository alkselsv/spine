from __future__ import annotations

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from spine.domain.artifacts import ExtractedPageText, PageTextLocator, ParseCoverage
from spine.ingestion.parsers import PdfParser, parse_document
from tests.domain.artifacts.test_parsed_documents import revision


def pdf_bytes(*pages: tuple[str, ...], password: str | None = None) -> bytes:
    target = BytesIO()
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    font_reference = writer._add_object(font)
    for lines in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        stream = DecodedStreamObject()
        commands: list[bytes] = []
        for index, line in enumerate(lines):
            try:
                encoded_line = line.encode("latin-1")
            except UnicodeEncodeError:
                encoded = (b"\xfe\xff" + line.encode("utf-16-be")).hex().upper()
                operand = f"<{encoded}>".encode("ascii")
            else:
                escaped = (
                    encoded_line.replace(b"\\", b"\\\\")
                    .replace(b"(", b"\\(")
                    .replace(b")", b"\\)")
                )
                operand = b"(" + escaped + b")"
            commands.append(
                f"BT /F1 12 Tf 72 {720 - index * 16} Td ".encode("ascii")
                + operand
                + b" Tj ET"
            )
        stream.set_data(b" ".join(commands))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(password)
    writer.write(target)
    return target.getvalue()


def test_pdf_pages_and_repeated_text_have_exact_distinct_ranges() -> None:
    content = pdf_bytes(("repeat", "repeat"), ("other",))

    document = parse_document(
        revision(content, media_type="application/pdf"),
        content,
    )

    assert document.coverage is ParseCoverage.COMPLETE
    assert [segment.text for segment in document.segments] == ["repeat", "repeat", "other"]
    assert [segment.locator for segment in document.segments] == [
        PageTextLocator(page_number=1, start_codepoint=0, end_codepoint=6),
        PageTextLocator(page_number=1, start_codepoint=7, end_codepoint=13),
        PageTextLocator(page_number=2, start_codepoint=0, end_codepoint=5),
    ]
    assert document.segments[0].segment_id != document.segments[1].segment_id
    assert document.parser_name == "pdf"
    assert document.locator_contract_version == "pdf-page-extracted-codepoint:v1"


def test_pdf_blank_page_is_a_typed_partial_exclusion() -> None:
    content = pdf_bytes(("visible",), ())

    document = parse_document(
        revision(content, media_type="application/pdf"),
        content,
    )

    assert document.coverage is ParseCoverage.PARTIAL
    assert [segment.text for segment in document.segments] == ["visible"]
    assert [(item.code, item.count, item.locator) for item in document.exclusions] == [
        ("blank_page", 1, PageTextLocator(page_number=2, start_codepoint=0, end_codepoint=0)),
    ]


def test_pdf_with_only_blank_pages_has_failed_coverage() -> None:
    content = pdf_bytes(())

    document = parse_document(revision(content, media_type="application/pdf"), content)

    assert document.coverage is ParseCoverage.FAILED
    assert document.segments == ()
    assert [item.code for item in document.exclusions] == ["blank_page"]


def test_pdf_without_pages_has_malformed_failed_coverage() -> None:
    content = pdf_bytes()

    document = parse_document(revision(content, media_type="application/pdf"), content)

    assert document.coverage is ParseCoverage.FAILED
    assert document.segments == ()
    assert [item.code for item in document.exclusions] == ["malformed_document"]


def test_malformed_pdf_has_content_free_failed_coverage() -> None:
    content = b"not a pdf and protected customer text"

    document = parse_document(revision(content, media_type="application/pdf"), content)

    assert document.coverage is ParseCoverage.FAILED
    assert document.segments == ()
    assert [(item.code, item.count, item.locator) for item in document.exclusions] == [
        ("malformed_document", 1, None),
    ]
    assert b"protected customer text" not in document.model_dump_json().encode()


def test_pdf_extraction_failure_has_content_free_failed_coverage() -> None:
    content = pdf_bytes(("protected customer text",), password="secret")

    document = parse_document(revision(content, media_type="application/pdf"), content)

    assert document.coverage is ParseCoverage.FAILED
    assert document.segments == ()
    assert [(item.code, item.count, item.locator) for item in document.exclusions] == [
        ("extraction_failed", 1, None),
    ]
    assert b"protected customer text" not in document.model_dump_json().encode()


@pytest.mark.parametrize(
    ("limits", "expected_coverage"),
    [
        pytest.param({"max_bytes": 1}, ParseCoverage.FAILED, id="bytes"),
        pytest.param({"max_page_chars": 1}, ParseCoverage.FAILED, id="page-characters"),
        pytest.param({"max_pages": 1}, ParseCoverage.PARTIAL, id="page-count"),
        pytest.param({"max_total_chars": 1}, ParseCoverage.FAILED, id="total-characters"),
    ],
)
def test_pdf_resource_limit_has_bounded_content_free_exclusion(
    limits: dict[str, int],
    expected_coverage: ParseCoverage,
) -> None:
    content = pdf_bytes(("protected customer text",), ("second page",))

    document = PdfParser(**limits).parse(
        revision(content, media_type="application/pdf"),
        content,
    )

    assert document.coverage is expected_coverage
    assert {item.code for item in document.exclusions} == {"bounded_input"}
    assert all("protected customer text" not in item.model_dump_json() for item in document.exclusions)


def test_pdf_resource_limits_are_pinned_by_parser_configuration() -> None:
    assert PdfParser(max_bytes=1).config_reference != PdfParser(max_page_chars=1).config_reference


def test_pdf_locator_round_trip_reopens_the_exact_extracted_page_range() -> None:
    content = pdf_bytes(("repeat", "repeat"), ("other",))
    source = revision(content, media_type="application/pdf")
    parser = PdfParser()
    document = parser.parse(source, content)
    locator = document.segments[1].locator

    assert parser.resolve_locator(source, content, document, locator) == "repeat"
    with pytest.raises(ValueError, match="locator does not resolve"):
        document.resolve_locator(ExtractedPageText(page_number=2, text="repeat\nrepeat\n"), locator)
    with pytest.raises(ValueError, match="locator does not resolve"):
        parser.resolve_locator(
            source,
            content,
            document.model_copy(update={"parser_version": "pdf:v2"}),
            locator,
        )


def test_pdf_unicode_locators_use_codepoint_not_byte_offsets() -> None:
    content = pdf_bytes(("éé", "tail"))
    source = revision(content, media_type="application/pdf")
    parser = PdfParser()

    document = parser.parse(source, content)

    assert [segment.text for segment in document.segments] == ["éé", "tail"]
    assert document.segments[1].locator == PageTextLocator(
        page_number=1,
        start_codepoint=3,
        end_codepoint=7,
    )
    assert parser.resolve_locator(source, content, document, document.segments[0].locator) == "éé"


def test_pdf_replacement_character_is_typed_as_unlocatable_content() -> None:
    content = pdf_bytes(("cannot locate � safely",))

    document = parse_document(revision(content, media_type="application/pdf"), content)

    assert document.coverage is ParseCoverage.FAILED
    assert document.segments == ()
    assert [item.code for item in document.exclusions] == ["unlocatable_content"]


def test_public_pdf_adapter_rejects_a_tampered_revision_identity() -> None:
    content = pdf_bytes(("visible",))
    source = revision(content, media_type="application/pdf").model_copy(
        update={"revision_digest": "f" * 64}
    )

    with pytest.raises(ValueError):
        PdfParser().parse(source, content)
