from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import httpx
from pydantic import ValidationError

from spine.connectors.google_api import GoogleApiRequest, HttpxGoogleApiTransport
from spine.connectors.google_execution_envelope import (
    GoogleBoundedResponseTransport,
    GoogleExecutionBudget,
    GoogleExecutionDeadlineExceeded,
    GoogleExecutionEnvelope,
    GoogleExecutionLimitExceeded,
)


def _envelope(**overrides: object) -> GoogleExecutionEnvelope:
    values: dict[str, object] = {
        "max_run_duration_seconds": 5.0,
        "max_response_body_bytes": 4,
        "max_drive_content_bytes": 3,
        "max_aggregate_response_bytes": 8,
        "max_drive_file_targets": 2,
        "max_exports": 2,
        "max_spreadsheets": 2,
        "max_numeric_sheets": 3,
        "max_aggregate_rows": 4,
        "max_api_pages": 2,
        "max_configured_ranges": 2,
    }
    values.update(overrides)
    return GoogleExecutionEnvelope(**values)  # type: ignore[arg-type]


class StreamingFakeTransport:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.returned = False
        self.calls = 0

    async def request(self, request: GoogleApiRequest) -> SimpleNamespace:
        self.calls += 1
        body = bytearray()
        for chunk in self.chunks:
            if request.body_observer is not None:
                request.body_observer(chunk)
            body.extend(chunk)
        self.returned = True
        return SimpleNamespace(content=bytes(body), stream_observed=True)


def _request(url: str = "https://google.test/v1/list") -> GoogleApiRequest:
    return GoogleApiRequest(
        method="GET",
        url=url,
        headers={},
        query={},
        form=None,
        timeout_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_google_execution_envelope_stops_oversized_stream_before_response_returns() -> None:
    inner = StreamingFakeTransport((b"ab", b"cd", b"ef"))
    bounded = GoogleBoundedResponseTransport(
        inner, GoogleExecutionBudget(_envelope()), deadline=5.0, monotonic=lambda: 0.0
    )

    with pytest.raises(GoogleExecutionLimitExceeded):
        await bounded.request(_request())

    assert inner.returned is False


@pytest.mark.asyncio
async def test_google_httpx_transport_stops_stream_before_buffering_over_limit_body() -> None:
    class ChunkStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.read_chunks = 0

        async def __aiter__(self):
            for chunk in (b"a", b"b", b"c", b"d", b"e", b"f"):
                self.read_chunks += 1
                yield chunk

        async def aclose(self) -> None:
            return None

    stream = ChunkStream()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        bounded = GoogleBoundedResponseTransport(
            HttpxGoogleApiTransport(client),
            GoogleExecutionBudget(_envelope()),
            deadline=5.0,
            monotonic=lambda: 0.0,
        )
        with pytest.raises(GoogleExecutionLimitExceeded):
            await bounded.request(_request())
    finally:
        await client.aclose()

    assert stream.read_chunks == 5


@pytest.mark.asyncio
async def test_google_execution_envelope_charges_each_physical_retry_response() -> None:
    inner = StreamingFakeTransport((b"abc",))
    budget = GoogleExecutionBudget(_envelope(max_aggregate_response_bytes=5))

    class FakeRequestControlLayer:
        """#753 seam double: each physical attempt delegates through #755."""

        def __init__(self, downstream: GoogleBoundedResponseTransport) -> None:
            self.downstream = downstream

        async def perform_two_attempts(self, request: GoogleApiRequest) -> None:
            await self.downstream.request(request)
            await self.downstream.request(request)

    bounded = GoogleBoundedResponseTransport(inner, budget, deadline=5.0, monotonic=lambda: 0.0)
    request_control = FakeRequestControlLayer(bounded)

    with pytest.raises(GoogleExecutionLimitExceeded):
        await request_control.perform_two_attempts(_request())

    assert inner.calls == 2


@pytest.mark.asyncio
async def test_google_execution_envelope_applies_additional_drive_content_cap() -> None:
    inner = StreamingFakeTransport((b"abcd",))
    bounded = GoogleBoundedResponseTransport(
        inner, GoogleExecutionBudget(_envelope()), deadline=5.0, monotonic=lambda: 0.0
    )

    with pytest.raises(GoogleExecutionLimitExceeded):
        await bounded.request(_request("https://google.test/files/a?alt=media"))

    assert inner.returned is False


@pytest.mark.asyncio
async def test_google_execution_envelope_deadline_refuses_new_transport_attempt() -> None:
    inner = StreamingFakeTransport((b"a",))
    bounded = GoogleBoundedResponseTransport(
        inner, GoogleExecutionBudget(_envelope()), deadline=1.0, monotonic=lambda: 1.0
    )

    with pytest.raises(GoogleExecutionDeadlineExceeded):
        await bounded.request(_request())

    assert inner.calls == 0


@pytest.mark.asyncio
async def test_google_execution_envelope_cancellation_stops_inflight_request() -> None:
    class BlockingTransport:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def request(self, request: GoogleApiRequest) -> SimpleNamespace:
            del request
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
            raise AssertionError("unreachable")

    inner = BlockingTransport()
    bounded = GoogleBoundedResponseTransport(
        inner, GoogleExecutionBudget(_envelope()), deadline=5.0, monotonic=lambda: 0.0
    )
    request_task = asyncio.create_task(bounded.request(_request()))
    await inner.started.wait()

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert inner.cancelled.is_set()


@pytest.mark.asyncio
async def test_google_execution_envelope_deadline_cancels_inflight_request() -> None:
    class BlockingTransport:
        def __init__(self) -> None:
            self.cancelled = asyncio.Event()

        async def request(self, request: GoogleApiRequest) -> SimpleNamespace:
            del request
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
            raise AssertionError("unreachable")

    inner = BlockingTransport()

    async def expire(awaitable, timeout: float):
        del timeout
        request_task = asyncio.ensure_future(awaitable)
        await asyncio.sleep(0)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        raise asyncio.TimeoutError

    bounded = GoogleBoundedResponseTransport(
        inner,
        GoogleExecutionBudget(_envelope()),
        deadline=5.0,
        monotonic=lambda: 0.0,
        wait_for=expire,
    )

    with pytest.raises(GoogleExecutionDeadlineExceeded):
        await bounded.request(_request())

    assert inner.cancelled.is_set()


@pytest.mark.asyncio
async def test_google_execution_envelope_preserves_per_request_timeout_outcome() -> None:
    class TimedOutTransport:
        async def request(self, request: GoogleApiRequest) -> SimpleNamespace:
            del request
            raise TimeoutError("one request timed out")

    bounded = GoogleBoundedResponseTransport(
        TimedOutTransport(),
        GoogleExecutionBudget(_envelope()),
        deadline=5.0,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(TimeoutError, match="one request timed out"):
        await bounded.request(_request())


def test_google_execution_envelope_page_and_row_reservations_fail_closed() -> None:
    budget = GoogleExecutionBudget(_envelope(max_api_pages=1, max_aggregate_rows=2))
    budget.reserve_page()
    budget.add_rows(2)

    with pytest.raises(GoogleExecutionLimitExceeded):
        budget.reserve_page()
    with pytest.raises(GoogleExecutionLimitExceeded):
        budget.add_rows(1)


@pytest.mark.asyncio
async def test_google_execution_envelope_concurrent_page_reservations_stay_within_cap() -> None:
    budget = GoogleExecutionBudget(_envelope(max_api_pages=1))

    async def reserve_page() -> bool:
        try:
            budget.reserve_page()
        except GoogleExecutionLimitExceeded:
            return False
        return True

    results = await asyncio.gather(*(reserve_page() for _ in range(20)))

    assert sum(results) == 1
    assert budget.pages == 1


@pytest.mark.asyncio
async def test_google_execution_envelope_concurrent_row_reservations_stay_within_cap() -> None:
    budget = GoogleExecutionBudget(_envelope(max_aggregate_rows=4))

    async def reserve_row() -> bool:
        try:
            budget.add_rows(1)
        except GoogleExecutionLimitExceeded:
            return False
        return True

    results = await asyncio.gather(*(reserve_row() for _ in range(20)))

    assert sum(results) == 4
    assert budget.rows == 4


@pytest.mark.asyncio
async def test_google_execution_envelope_concurrent_byte_reservations_stay_within_cap() -> None:
    budget = GoogleExecutionBudget(_envelope(max_aggregate_response_bytes=5))

    async def reserve_byte() -> bool:
        try:
            budget.charge_body(1, is_drive_content=False, response_total=1)
        except GoogleExecutionLimitExceeded:
            return False
        return True

    results = await asyncio.gather(*(reserve_byte() for _ in range(20)))

    assert sum(results) == 5
    assert budget.response_bytes == 5


def test_google_execution_envelope_requires_every_bound() -> None:
    payload = _envelope().model_dump()
    del payload["max_exports"]

    with pytest.raises(ValidationError):
        GoogleExecutionEnvelope.model_validate(payload)

