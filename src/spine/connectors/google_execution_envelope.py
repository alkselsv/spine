"""Fail-closed per-run resource limits for Google qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
from typing import Literal, Protocol, TYPE_CHECKING

from pydantic import ConfigDict, Field, StrictFloat, StrictInt

from spine.domain.common import DefinitionModel

if TYPE_CHECKING:
    from spine.connectors.google_api import GoogleApiRequest, GoogleApiResponse, GoogleApiTransport

__all__ = [
    "GoogleExecutionApprovalVerifier",
    "GoogleExecutionBudget",
    "GoogleExecutionDeadlineExceeded",
    "GoogleExecutionEnvelope",
    "GoogleExecutionLimitExceeded",
]


class GoogleExecutionEnvelope(DefinitionModel):
    """Explicit resource limits; deliberately has no production defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["google-execution-envelope:v1"] = "google-execution-envelope:v1"
    max_run_duration_seconds: StrictFloat = Field(gt=0)
    max_response_body_bytes: StrictInt = Field(gt=0)
    max_drive_content_bytes: StrictInt = Field(gt=0)
    max_aggregate_response_bytes: StrictInt = Field(gt=0)
    max_drive_file_targets: StrictInt = Field(gt=0)
    max_exports: StrictInt = Field(gt=0)
    max_spreadsheets: StrictInt = Field(gt=0)
    max_numeric_sheets: StrictInt = Field(gt=0)
    max_aggregate_rows: StrictInt = Field(gt=0)
    max_api_pages: StrictInt = Field(gt=0)
    max_configured_ranges: StrictInt = Field(gt=0)

    @property
    def fingerprint(self) -> str:
        material = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(material).hexdigest()


class GoogleExecutionApprovalVerifier(Protocol):
    def is_approved(self, envelope_fingerprint: str) -> bool: ...


class GoogleExecutionDeadlineExceeded(RuntimeError):
    """The injected monotonic deadline elapsed."""


class GoogleExecutionLimitExceeded(RuntimeError):
    """Sanitized limit error; never carries provider payload or source content."""


class GoogleExecutionBudget:
    """Synchronous reservations are atomic between awaits on the event loop."""

    def __init__(self, envelope: GoogleExecutionEnvelope) -> None:
        self.envelope = envelope
        self.response_bytes = 0
        self.rows = 0
        self.pages = 0

    def reserve_page(self) -> None:
        if self.pages >= self.envelope.max_api_pages:
            raise GoogleExecutionLimitExceeded("Google qualification limit exceeded.")
        self.pages += 1

    def add_rows(self, count: int) -> None:
        if count < 0 or self.rows + count > self.envelope.max_aggregate_rows:
            raise GoogleExecutionLimitExceeded("Google qualification limit exceeded.")
        self.rows += count

    def charge_body(
        self, count: int, *, is_drive_content: bool, response_total: int
    ) -> None:
        cap = self.envelope.max_response_body_bytes
        if is_drive_content:
            cap = min(cap, self.envelope.max_drive_content_bytes)
        if (
            response_total > cap
            or self.response_bytes + count
            > self.envelope.max_aggregate_response_bytes
        ):
            raise GoogleExecutionLimitExceeded("Google qualification limit exceeded.")
        self.response_bytes += count


class GoogleBoundedResponseTransport:
    """Decorates each physical attempt, charging bytes before HTTP buffers them."""

    def __init__(
        self,
        inner: "GoogleApiTransport",
        budget: GoogleExecutionBudget,
        *,
        deadline: float,
        monotonic: Callable[[], float],
        wait_for: Callable[
            [Awaitable["GoogleApiResponse"], float], Awaitable["GoogleApiResponse"]
        ] = asyncio.wait_for,
    ) -> None:
        self._inner = inner
        self._budget = budget
        self._deadline = deadline
        self._monotonic = monotonic
        self._wait_for = wait_for

    async def request(self, request: "GoogleApiRequest") -> "GoogleApiResponse":
        from urllib.parse import urlsplit, parse_qs

        if self._monotonic() >= self._deadline:
            raise GoogleExecutionDeadlineExceeded(
                "Google qualification deadline exceeded."
            )
        timeout = self._deadline - self._monotonic()
        is_content = (
            "/export" in urlsplit(request.url).path
            or parse_qs(urlsplit(request.url).query).get("alt") == ["media"]
            or request.query.get("alt") == "media"
        )

        def observe(chunk: bytes) -> None:
            nonlocal response_bytes
            response_bytes += len(chunk)
            self._budget.charge_body(
                len(chunk), is_drive_content=is_content, response_total=response_bytes
            )

        response_bytes = 0

        bounded_request = request.with_body_observer(observe, timeout)
        request_task = asyncio.create_task(self._inner.request(bounded_request))
        try:
            response = await self._wait_for(request_task, timeout)
        except asyncio.TimeoutError:
            if request_task.cancelled():
                raise GoogleExecutionDeadlineExceeded(
                    "Google qualification deadline exceeded."
                ) from None
            # Preserve per-request timeouts so the existing request policy can
            # classify or retry them without misreporting a run deadline.
            raise
        # A fake or third-party transport may not support streaming observation.
        # Its already materialized body is still checked and charged defensively.
        if getattr(response, "stream_observed", False) is not True:
            self._budget.charge_body(
                len(response.content),
                is_drive_content=is_content,
                response_total=len(response.content),
            )
        return response
