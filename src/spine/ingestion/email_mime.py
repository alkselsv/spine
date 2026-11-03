"""Bounded, network-free parsing of immutable raw MIME originals."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from email import policy
from email.feedparser import BytesFeedParser
from email.message import Message
import hashlib
import json
import re
from time import monotonic
from typing import Literal

from pydantic import Field, StrictFloat, StrictInt, ValidationError

from spine.domain.common import DefinitionModel
from spine.domain.artifacts.email_mime import (
    MimeArtifact,
    MimeDefect,
    MimeHeader,
    MimeParseDisposition,
    MimePart,
)

_POLL_BYTES = 4_096


@dataclass(frozen=True, slots=True)
class MimeAttachmentContent:
    """Decoded bytes for one attachment or inline MIME part, kept out of artifacts."""

    part_path: str
    media_type: str
    disposition: str
    filename: str | None
    content_id: str | None
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class MimeParseResult:
    artifact: MimeArtifact
    attachments: tuple[MimeAttachmentContent, ...]


class MimeParserProfile(DefinitionModel):
    """Caller-supplied operating limits; no production values are implied."""

    schema_version: Literal["mime-parser-profile:v1"] = "mime-parser-profile:v1"
    max_total_bytes: StrictInt = Field(gt=0)
    max_header_bytes: StrictInt = Field(gt=0)
    max_parts: StrictInt = Field(gt=0)
    max_depth: StrictInt = Field(gt=0)
    max_decoded_bytes: StrictInt = Field(gt=0)
    max_parse_seconds: StrictFloat = Field(gt=0)

    @property
    def reference(self) -> str:
        values = {
            "max_decoded_bytes": self.max_decoded_bytes,
            "max_depth": self.max_depth,
            "max_header_bytes": self.max_header_bytes,
            "max_parse_seconds": self.max_parse_seconds,
            "max_parts": self.max_parts,
            "max_total_bytes": self.max_total_bytes,
            "schema_version": self.schema_version,
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("ascii")
        return f"mime-parser:v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class MimeParser:
    profile: MimeParserProfile
    clock: Callable[[], float] = field(default=monotonic, compare=False, repr=False)

    def parse(self, raw: bytes) -> MimeArtifact:
        return self.parse_with_attachments(raw).artifact

    def parse_with_attachments(self, raw: bytes) -> MimeParseResult:
        if len(raw) > self.profile.max_total_bytes:
            return MimeParseResult(
                self._limit_result(raw, "total_bytes_exceeded"), ()
            )
        started_at = self.clock()
        budget = _ParseBudget(
            profile=self.profile,
            clock=self.clock,
            started_at=started_at,
        )
        parser = _BoundedBytesFeedParser(
            policy=_BoundedEmailPolicy(budget),
            budget=budget,
        )
        try:
            for offset in range(0, len(raw), _POLL_BYTES):
                budget.check_time("1")
                parser.feed(raw[offset : offset + _POLL_BYTES])
            message = parser.close()
            budget.check_time("1")
        except _LimitExceeded as exc:
            return MimeParseResult(
                self._limit_result(raw, exc.code, part_path=exc.part_path), ()
            )
        except RecursionError:
            return MimeParseResult(self._limit_result(raw, "depth_exceeded"), ())
        defects: list[MimeDefect] = []
        attachments: list[MimeAttachmentContent] = []
        try:
            root = _part(
                message,
                "1",
                budget=budget,
                defects=defects,
                attachments=attachments,
            )
        except _LimitExceeded as exc:
            return MimeParseResult(
                self._limit_result(raw, exc.code, part_path=exc.part_path), ()
            )
        headers = _headers(message, "1", defects)
        artifact = MimeArtifact(
            parser_profile=self.profile.reference,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_byte_length=len(raw),
            disposition=(
                MimeParseDisposition.MALFORMED
                if defects
                else MimeParseDisposition.COMPLETE
            ),
            headers=headers,
            root=root,
            defects=tuple(defects),
        )
        return MimeParseResult(
            artifact,
            tuple(attachments) if artifact.disposition is MimeParseDisposition.COMPLETE else (),
        )

    def _limit_result(
        self,
        raw: bytes,
        code: str,
        *,
        part_path: str = "1",
    ) -> MimeArtifact:
        return MimeArtifact(
            parser_profile=self.profile.reference,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_byte_length=len(raw),
            disposition=MimeParseDisposition.LIMIT_EXCEEDED,
            defects=(MimeDefect(part_path=part_path, code=code),),
        )


class _LimitExceeded(Exception):
    def __init__(self, code: str, part_path: str) -> None:
        super().__init__(code)
        self.code = code
        self.part_path = part_path


@dataclass(slots=True)
class _ParseBudget:
    profile: MimeParserProfile
    clock: Callable[[], float]
    started_at: float
    parts: int = 0
    header_bytes: int = 0
    decoded_bytes: int = 0

    def check_time(self, part_path: str) -> None:
        if self.clock() - self.started_at > self.profile.max_parse_seconds:
            raise _LimitExceeded("time_exceeded", part_path)

    def enter_part(self, depth: int) -> None:
        part_path = "1"
        self.check_time(part_path)
        if depth > self.profile.max_depth:
            raise _LimitExceeded("depth_exceeded", part_path)
        self.parts += 1
        if self.parts > self.profile.max_parts:
            raise _LimitExceeded("part_count_exceeded", part_path)

    def add_header_bytes(self, byte_length: int) -> None:
        self.check_time("1")
        self.header_bytes += byte_length
        if self.header_bytes > self.profile.max_header_bytes:
            raise _LimitExceeded("header_bytes_exceeded", "1")

    def reserve_decoded(self, byte_length: int, part_path: str) -> None:
        self.check_time(part_path)
        self.decoded_bytes += byte_length
        if self.decoded_bytes > self.profile.max_decoded_bytes:
            raise _LimitExceeded("decoded_bytes_exceeded", part_path)


class _BoundedEmailPolicy(policy.EmailPolicy):
    def __init__(self, budget: _ParseBudget) -> None:
        super().__init__()
        object.__setattr__(self, "_budget", budget)

    def header_source_parse(self, sourcelines: list[str]) -> tuple[str, str]:
        byte_length = sum(
            len(line.encode("ascii", "surrogateescape")) for line in sourcelines
        )
        self._budget.add_header_bytes(byte_length)
        return super().header_source_parse(sourcelines)


class _BoundedBytesFeedParser(BytesFeedParser):
    def __init__(
        self,
        *,
        policy: policy.Policy,
        budget: _ParseBudget,
    ) -> None:
        self._budget = budget
        super().__init__(policy=policy)

    def _new_message(self) -> None:
        self._budget.enter_part(len(self._msgstack) + 1)
        super()._new_message()


def _headers(
    message: Message,
    part_path: str,
    defects: list[MimeDefect],
) -> tuple[MimeHeader, ...]:
    headers: list[MimeHeader] = []
    for name, value in message.items():
        try:
            headers.append(MimeHeader(name=name.lower(), value=str(value)))
        except ValidationError:
            _append_defect(defects, part_path, "invalid_header")
    return tuple(headers)


def _part(
    message: Message,
    path: str,
    *,
    budget: _ParseBudget,
    defects: list[MimeDefect],
    attachments: list[MimeAttachmentContent],
) -> MimePart:
    budget.check_time(path)
    _record_message_defects(message, path, defects)
    children = (
        tuple(
            _part(
                child,
                f"{path}.{index}",
                budget=budget,
                defects=defects,
                attachments=attachments,
            )
            for index, child in enumerate(message.iter_parts(), start=1)
        )
        if message.is_multipart()
        else ()
    )
    estimated_decoded_bytes = (
        0
        if children
        else _decoded_size_upper_bound(message, budget=budget, part_path=path)
    )
    budget.reserve_decoded(estimated_decoded_bytes, path)
    payload = None if children else message.get_payload(decode=True)
    payload_bytes = payload if isinstance(payload, bytes) else b""
    budget.check_time(path)
    if len(payload_bytes) > estimated_decoded_bytes:
        budget.reserve_decoded(len(payload_bytes) - estimated_decoded_bytes, path)
    _record_message_defects(message, path, defects)
    transfer_encoding = str(message.get("Content-Transfer-Encoding") or "").strip().lower()
    if transfer_encoding and transfer_encoding not in {
        "7bit",
        "8bit",
        "binary",
        "base64",
        "quoted-printable",
    }:
        _append_defect(defects, path, "invalid_transfer_encoding")
    text: str | None = None
    invalid_transfer = any(
        defect.part_path == path and defect.code == "invalid_transfer_encoding"
        for defect in defects
    )
    if message.get_content_maintype() == "text" and not children and not invalid_transfer:
        try:
            text = payload_bytes.decode(message.get_content_charset() or "ascii")
        except (LookupError, UnicodeDecodeError):
            _append_defect(defects, path, "invalid_charset")
    disposition = message.get_content_disposition()
    filename = message.get_filename()
    content_id = message.get("Content-ID")
    if (
        not children
        and payload is not None
        and (
            disposition in {"inline", "attachment"}
            or filename is not None
            or content_id is not None
            or message.get_content_maintype() not in {"text", "multipart"}
        )
    ):
        attachments.append(MimeAttachmentContent(
            part_path=path,
            media_type=message.get_content_type().lower(),
            disposition=disposition or "attachment",
            filename=filename,
            content_id=str(content_id) if content_id is not None else None,
            content=payload_bytes,
            sha256=hashlib.sha256(payload_bytes).hexdigest(),
        ))
    return MimePart(
        part_path=path,
        media_type=message.get_content_type().lower(),
        headers=_headers(message, path, defects),
        content_disposition=disposition if disposition in {"inline", "attachment"} else None,
        content_id=message.get("Content-ID"),
        filename_present=message.get_filename() is not None,
        decoded_byte_length=len(payload_bytes),
        payload_sha256=hashlib.sha256(payload_bytes).hexdigest() if payload_bytes else None,
        text=text,
        children=children,
    )


_DEFECT_CODES = {
    "CloseBoundaryNotFoundDefect": "close_boundary_not_found",
    "FirstHeaderLineIsContinuationDefect": "invalid_header",
    "InvalidBase64CharactersDefect": "invalid_transfer_encoding",
    "InvalidBase64LengthDefect": "invalid_transfer_encoding",
    "InvalidBase64PaddingDefect": "invalid_transfer_encoding",
    "InvalidHeaderDefect": "invalid_header",
    "MalformedHeaderDefect": "invalid_header",
    "MisplacedEnvelopeHeaderDefect": "invalid_header",
    "MissingHeaderBodySeparatorDefect": "missing_header_body_separator",
    "MultipartInvariantViolationDefect": "multipart_invariant_violation",
    "NoBoundaryInMultipartDefect": "missing_boundary",
    "NonASCIILocalPartDefect": "invalid_header",
    "NonPrintableDefect": "invalid_header",
    "ObsoleteHeaderDefect": "invalid_header",
    "StartBoundaryNotFoundDefect": "start_boundary_not_found",
    "UndecodableBytesDefect": "invalid_header",
}


def _record_message_defects(
    message: Message,
    part_path: str,
    target: list[MimeDefect],
) -> None:
    for defect in message.defects:
        name = type(defect).__name__
        code = _DEFECT_CODES.get(name, _snake_case(name.removesuffix("Defect")))
        _append_defect(target, part_path, code)
    for value in message.values():
        for defect in getattr(value, "defects", ()):
            name = type(defect).__name__
            code = _DEFECT_CODES.get(name, "invalid_header")
            _append_defect(target, part_path, code)


def _append_defect(target: list[MimeDefect], part_path: str, code: str) -> None:
    value = MimeDefect(part_path=part_path, code=code)
    if value not in target:
        target.append(value)


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _decoded_size_upper_bound(
    message: Message,
    *,
    budget: _ParseBudget,
    part_path: str,
) -> int:
    payload = message.get_payload(decode=False)
    if not isinstance(payload, str):
        return 0
    # The email parser may replace undecodable 8-bit input with U+FFFD. UTF-8
    # therefore gives a conservative byte bound without re-exposing raw input.
    raw = payload.encode("utf-8", "surrogatepass")
    transfer_encoding = str(message.get("Content-Transfer-Encoding") or "").strip().lower()
    if transfer_encoding == "base64":
        alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        compact_length = 0
        content_length = 0
        padding = 0
        saw_padding = False
        invalid = False
        for index, value in enumerate(raw):
            if index % _POLL_BYTES == 0:
                budget.check_time(part_path)
            if value in b" \t\r\n":
                continue
            compact_length += 1
            if value == ord("="):
                saw_padding = True
                padding += 1
            elif value in alphabet and not saw_padding:
                content_length += 1
            else:
                invalid = True
        if (
            invalid
            or padding > 2
            or compact_length % 4 == 1
        ):
            return len(raw)
        return (content_length * 6) // 8
    if transfer_encoding == "quoted-printable":
        return _quoted_printable_size_upper_bound(
            raw,
            budget=budget,
            part_path=part_path,
        )
    return len(raw)


def _quoted_printable_size_upper_bound(
    raw: bytes,
    *,
    budget: _ParseBudget,
    part_path: str,
) -> int:
    size = 0
    index = 0
    hex_digits = b"0123456789abcdefABCDEF"
    while index < len(raw):
        if index % _POLL_BYTES == 0:
            budget.check_time(part_path)
        if raw[index : index + 3] == b"=\r\n":
            index += 3
        elif raw[index : index + 2] == b"=\n":
            index += 2
        elif (
            raw[index : index + 1] == b"="
            and index + 2 < len(raw)
            and raw[index + 1] in hex_digits
            and raw[index + 2] in hex_digits
        ):
            size += 1
            index += 3
        else:
            size += 1
            index += 1
    return size
