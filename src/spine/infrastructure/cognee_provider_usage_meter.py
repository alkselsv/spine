"""Content-free independent provider usage metering for Cognee qualifications."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import math
from threading import Lock
from typing import Any, Awaitable, Callable, Literal

import httpx
from pydantic import ConfigDict, Field

from spine.domain.common import DefinitionModel
from spine.memory.cognee.worker_profile import CogneeWorkerLimits


_COST_QUANTUM = Decimal("0.000000001")


class CogneeProviderUsageSnapshot(DefinitionModel):
    """Content-free aggregate observed independently of Cognee usage tables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cognee-provider-usage-snapshot:v1"]
    request_count: int = Field(ge=0)
    call_count: int = Field(ge=0)
    successful_call_count: int = Field(ge=0)
    failed_call_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_amount: Decimal = Field(ge=Decimal("0"))
    cost_currency: str = Field(pattern=r"^[A-Z]{3}$")
    usage_complete: bool
    limit_breaches: tuple[str, ...]


class CogneeProviderUsageLimitError(RuntimeError):
    """A content-free refusal raised before an over-limit HTTP attempt."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class CogneeProviderUsageMeter:
    """Aggregate LiteLLM provider callbacks without retaining protected content."""

    def __init__(self, *, limits: CogneeWorkerLimits) -> None:
        self._limits = limits
        self._lock = Lock()
        self._active_run_id: ContextVar[str | None] = ContextVar(
            f"cognee_provider_usage_run_{id(self)}", default=None
        )
        self._runs: dict[str, _MutableRunUsage] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._request_hook: Callable[[httpx.Request], Awaitable[None]] | None = None
        self._provider_callback: object | None = None
        self._owns_http_client = False

    @asynccontextmanager
    async def track_run(
        self,
        run_id: str,
        *,
        budget_amount: Decimal | None = None,
        budget_currency: str | None = None,
    ):
        """Scope callback observations to one projection run."""

        if not run_id:
            raise ValueError("provider usage run ID must be nonblank")
        effective_budget_amount = budget_amount or self._limits.max_budget_amount
        effective_budget_currency = budget_currency or self._limits.budget_currency
        token = self._active_run_id.set(run_id)
        with self._lock:
            usage_state = self._runs.setdefault(
                run_id,
                _MutableRunUsage(
                    budget_amount=effective_budget_amount,
                    budget_currency=effective_budget_currency,
                ),
            )
            if (
                usage_state.budget_amount != effective_budget_amount
                or usage_state.budget_currency != effective_budget_currency
            ):
                self._active_run_id.reset(token)
                raise ValueError("provider usage run budget changed")
        try:
            yield
        finally:
            self._active_run_id.reset(token)

    async def async_log_pre_api_call(
        self,
        model: str,
        messages: list[object],
        kwargs: dict[str, Any],
    ) -> None:
        self.log_pre_api_call(model, messages, kwargs)

    def log_pre_api_call(
        self,
        model: str,
        messages: list[object],
        kwargs: dict[str, Any],
    ) -> None:
        del model, messages, kwargs
        run_id = self._active_run_id.get()
        if run_id is None:
            return
        with self._lock:
            usage_state = self._runs[run_id]
            usage_state.call_count += 1
            usage_state.settled.clear()

    async def observe_provider_request(self) -> None:
        """Count one actual outbound HTTP attempt in the active run scope."""

        run_id = self._active_run_id.get()
        if run_id is None:
            return
        with self._lock:
            usage_state = self._runs[run_id]
            if usage_state.request_count >= self._limits.max_requests_per_run:
                raise CogneeProviderUsageLimitError(
                    "provider_request_limit_exceeded"
                )
            usage_state.request_count += 1

    def bind_http_client(
        self,
        client: httpx.AsyncClient,
        request_hook: Callable[[httpx.Request], Awaitable[None]],
        *,
        owned: bool,
    ) -> None:
        """Bind the instrumented LiteLLM client for deterministic cleanup."""

        self._http_client = client
        self._request_hook = request_hook
        self._owns_http_client = owned

    def bind_provider_callback(self, callback: object) -> None:
        """Retain the registered callback identity for deterministic cleanup."""

        self._provider_callback = callback

    async def close(self) -> None:
        """Remove the request hook and close a client created by this meter."""

        client = self._http_client
        request_hook = self._request_hook
        callback = self._provider_callback
        if client is not None and request_hook is not None:
            hooks = client.event_hooks.get("request", [])
            if request_hook in hooks:
                hooks.remove(request_hook)
        if client is not None and self._owns_http_client and not client.is_closed:
            await client.aclose()
        if callback is not None or (client is not None and self._owns_http_client):
            import litellm

            for callbacks in (litellm.input_callback, litellm.callbacks):
                if callback in callbacks:
                    callbacks.remove(callback)
            if client is not None and self._owns_http_client and litellm.aclient_session is client:
                litellm.aclient_session = None
        self._http_client = None
        self._request_hook = None
        self._provider_callback = None
        self._owns_http_client = False

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: object,
        start_time: object,
        end_time: object,
    ) -> None:
        del start_time, end_time
        usage = _value(response_obj, "usage")
        input_tokens = _nonnegative_int(
            _value(usage, "prompt_tokens", "input_tokens")
        )
        output_tokens = _nonnegative_int(
            _value(usage, "completion_tokens", "output_tokens")
        )
        call_type = _value(kwargs, "call_type")
        if call_type is None:
            call_type = _value(
                _value(kwargs, "standard_logging_object"), "call_type"
            )
        output_tokens_required = call_type not in ("embedding", "aembedding")
        raw_cost = _value(kwargs, "response_cost")
        if raw_cost is None:
            raw_cost = _value(
                _value(kwargs, "standard_logging_object"), "response_cost"
            )
        cost = _nonnegative_decimal(raw_cost)
        run_id = self._active_run_id.get()
        if run_id is None:
            return
        with self._lock:
            usage_state = self._runs[run_id]
            usage_state.successful_call_count += 1
            if input_tokens is None or (
                output_tokens is None and output_tokens_required
            ):
                usage_state.missing_usage_count += 1
            else:
                usage_state.input_tokens += input_tokens
                usage_state.output_tokens += output_tokens or 0
            if cost is None:
                usage_state.missing_cost_count += 1
            else:
                usage_state.cost_amount += cost
            if usage_state.outcome_count == usage_state.call_count:
                usage_state.settled.set()

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: object,
        start_time: object,
        end_time: object,
    ) -> None:
        del kwargs, response_obj, start_time, end_time
        run_id = self._active_run_id.get()
        if run_id is None:
            return
        with self._lock:
            usage_state = self._runs[run_id]
            usage_state.failed_call_count += 1
            if usage_state.outcome_count == usage_state.call_count:
                usage_state.settled.set()

    async def wait_until_settled(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Wait until every logical call has a success or failure callback."""

        if timeout_seconds <= 0:
            raise ValueError("provider usage settlement timeout must be positive")
        with self._lock:
            usage_state = self._runs.get(run_id)
            if usage_state is None or usage_state.outcome_count == usage_state.call_count:
                return True
            settled = usage_state.settled
        try:
            await asyncio.wait_for(settled.wait(), timeout=timeout_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return False
        with self._lock:
            return usage_state.outcome_count == usage_state.call_count

    async def snapshot(self, run_id: str) -> CogneeProviderUsageSnapshot:
        """Return one immutable, content-free view of the observed usage."""

        with self._lock:
            usage_state = self._runs.get(
                run_id,
                _MutableRunUsage(
                    budget_amount=self._limits.max_budget_amount,
                    budget_currency=self._limits.budget_currency,
                ),
            )
            request_count = usage_state.request_count
            call_count = usage_state.call_count
            successful_call_count = usage_state.successful_call_count
            failed_call_count = usage_state.failed_call_count
            input_tokens = usage_state.input_tokens
            output_tokens = usage_state.output_tokens
            cost_amount = usage_state.cost_amount
            usage_complete = (
                successful_call_count + failed_call_count == call_count
                and usage_state.missing_usage_count == 0
                and usage_state.missing_cost_count == 0
            )
        breaches: list[str] = []
        if request_count > self._limits.max_requests_per_run:
            breaches.append("provider_request_limit_exceeded")
        if input_tokens > self._limits.max_input_tokens:
            breaches.append("provider_input_token_limit_exceeded")
        if output_tokens > self._limits.max_output_tokens:
            breaches.append("provider_output_token_limit_exceeded")
        if usage_state.budget_currency != "USD":
            breaches.append("provider_cost_currency_mismatch")
        elif cost_amount > usage_state.budget_amount:
            breaches.append("provider_cost_limit_exceeded")
        return CogneeProviderUsageSnapshot(
            schema_version="cognee-provider-usage-snapshot:v1",
            request_count=request_count,
            call_count=call_count,
            successful_call_count=successful_call_count,
            failed_call_count=failed_call_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_amount=cost_amount,
            cost_currency="USD",
            usage_complete=usage_complete,
            limit_breaches=tuple(breaches),
        )


@dataclass(slots=True)
class _MutableRunUsage:
    budget_amount: Decimal
    budget_currency: str
    request_count: int = 0
    call_count: int = 0
    successful_call_count: int = 0
    failed_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_amount: Decimal = Decimal("0")
    missing_usage_count: int = 0
    missing_cost_count: int = 0
    settled: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def outcome_count(self) -> int:
        return self.successful_call_count + self.failed_call_count


def _value(source: object, *names: str) -> object | None:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _nonnegative_int(value: object | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonnegative_decimal(value: object | None) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0:
        return None
    try:
        return result.quantize(_COST_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        return None


__all__ = [
    "CogneeProviderUsageLimitError",
    "CogneeProviderUsageMeter",
    "CogneeProviderUsageSnapshot",
]
