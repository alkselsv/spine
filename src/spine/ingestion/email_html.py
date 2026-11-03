"""Deterministic, network-free sanitization of parsed email HTML."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
import json
import re

import nh3

from spine.domain.artifacts.email_html import (
    EmailHtmlDisposition,
    EmailHtmlSanitizerProfile,
    SanitizedEmailHtml,
)
from spine.domain.artifacts.email_mime import MimeArtifact, MimePart


_ALLOWED_TAGS = frozenset(
    {
        "a", "b", "blockquote", "br", "caption", "code", "dd", "div", "dl",
        "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li",
        "ol", "p", "pre", "s", "strong", "sub", "sup", "table", "tbody",
        "td", "th", "thead", "tr", "u", "ul",
    }
)
_CLEAN_CONTENT_TAGS = frozenset(
    {"iframe", "math", "noscript", "object", "script", "style", "svg", "template"}
)
_ALLOWED_ATTRIBUTES = {"a": frozenset({"href", "title"})}
_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})
_BLOCK_TAGS = frozenset(
    {
        "blockquote", "caption", "dd", "div", "dl", "dt", "h1", "h2", "h3",
        "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "table", "tbody",
        "td", "th", "thead", "tr", "ul",
    }
)


@dataclass(frozen=True, slots=True)
class EmailHtmlSanitizer:
    profile: EmailHtmlSanitizerProfile

    @property
    def reference(self) -> str:
        material = {
            "allowed_attributes": {
                tag: sorted(attributes)
                for tag, attributes in sorted(_ALLOWED_ATTRIBUTES.items())
            },
            "allowed_tags": sorted(_ALLOWED_TAGS),
            "allowed_url_schemes": sorted(_ALLOWED_URL_SCHEMES),
            "clean_content_tags": sorted(_CLEAN_CONTENT_TAGS),
            "library": "nh3",
            "library_version": nh3.__version__,
            "max_depth": self.profile.max_depth,
            "max_parts": self.profile.max_parts,
            "max_total_input_bytes": self.profile.max_total_input_bytes,
            "max_total_output_bytes": self.profile.max_total_output_bytes,
            "schema_version": self.profile.schema_version,
            "url_relative": "deny",
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
        return f"email-html-sanitizer:v1:{hashlib.sha256(encoded).hexdigest()}"

    def sanitize(self, source: MimeArtifact) -> SanitizedEmailHtml:
        """Return the first eligible inline HTML body, or a content-free result."""

        return self.sanitize_all(source)[0]

    def sanitize_all(self, source: MimeArtifact) -> tuple[SanitizedEmailHtml, ...]:
        """Sanitize eligible HTML body parts without mutating raw MIME evidence."""

        if source.root is None:
            return (
                self._result(
                    source,
                    part_path="1",
                    disposition=EmailHtmlDisposition.SOURCE_UNAVAILABLE,
                ),
            )

        candidates, traversal_limit = self._candidate_parts(source.root)
        if traversal_limit is not None:
            return (
                self._result(
                    source,
                    part_path=traversal_limit,
                    disposition=EmailHtmlDisposition.TRAVERSAL_LIMIT_EXCEEDED,
                ),
            )

        results: list[SanitizedEmailHtml] = []
        total_input_bytes = 0
        total_output_bytes = 0
        for part in candidates:
            assert part.text is not None
            source_bytes = part.text.encode("utf-8")
            total_input_bytes += len(source_bytes)
            if total_input_bytes > self.profile.max_total_input_bytes:
                results.append(
                    self._result(
                        source,
                        part_path=part.part_path,
                        disposition=EmailHtmlDisposition.INPUT_LIMIT_EXCEEDED,
                    )
                )
                break
            sanitized = _clean_html(part.text)
            plain_text = _plain_text(sanitized)
            output_bytes = len(sanitized.encode("utf-8")) + len(plain_text.encode("utf-8"))
            total_output_bytes += output_bytes
            if total_output_bytes > self.profile.max_total_output_bytes:
                results.append(
                    self._result(
                        source,
                        part_path=part.part_path,
                        disposition=EmailHtmlDisposition.OUTPUT_LIMIT_EXCEEDED,
                    )
                )
                break
            results.append(
                self._result(
                    source,
                    part_path=part.part_path,
                    disposition=EmailHtmlDisposition.COMPLETE,
                    sanitized_html=sanitized,
                    plain_text=plain_text,
                )
            )
        if not results:
            results.append(
                self._result(
                    source,
                    part_path="1",
                    disposition=EmailHtmlDisposition.SOURCE_UNAVAILABLE,
                )
            )
        return tuple(results)

    def _candidate_parts(
        self,
        root: MimePart,
    ) -> tuple[list[MimePart], str | None]:
        candidates: list[MimePart] = []
        stack: list[tuple[MimePart, int]] = [(root, 1)]
        visited = 0
        while stack:
            part, depth = stack.pop()
            visited += 1
            if visited > self.profile.max_parts or depth > self.profile.max_depth:
                return [], part.part_path
            if part.content_disposition == "attachment" or part.media_type == "message/rfc822":
                continue
            if part.media_type == "text/html" and part.text is not None:
                candidates.append(part)
                continue
            if part.media_type.startswith("multipart/"):
                stack.extend((child, depth + 1) for child in reversed(part.children))
        return candidates, None

    def _result(
        self,
        source: MimeArtifact,
        *,
        part_path: str,
        disposition: EmailHtmlDisposition,
        sanitized_html: str | None = None,
        plain_text: str | None = None,
    ) -> SanitizedEmailHtml:
        return SanitizedEmailHtml(
            source_sha256=source.raw_sha256,
            source_part_path=part_path,
            sanitizer_profile=self.reference,
            disposition=disposition,
            sanitized_html=sanitized_html,
            plain_text=plain_text,
        )


def _clean_html(source: str) -> str:
    return nh3.clean(
        source,
        tags=_ALLOWED_TAGS,
        clean_content_tags=_CLEAN_CONTENT_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attribute,
        strip_comments=True,
        link_rel=None,
        url_schemes=_ALLOWED_URL_SCHEMES,
        url_relative="deny",
    )


def _filter_attribute(tag: str, attribute: str, value: str) -> str | None:
    if attribute in _ALLOWED_ATTRIBUTES.get(tag, ()):
        return value
    return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag == "br" or tag in _BLOCK_TAGS:
            self.fragments.append("\n")
        elif tag == "a":
            self.fragments.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.fragments.append("\n")
        elif tag == "a":
            self.fragments.append(" ")

    def handle_data(self, data: str) -> None:
        self.fragments.append(data)


_HORIZONTAL_WHITESPACE = re.compile(r"[\t\f\v ]+")


def _plain_text(safe_html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(safe_html)
    extractor.close()
    lines = [
        _HORIZONTAL_WHITESPACE.sub(" ", line).strip()
        for line in "".join(extractor.fragments).splitlines()
    ]
    return "\n".join(line for line in lines if line)
