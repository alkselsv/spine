from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from spine.infrastructure.cognee_provider_usage_meter import (
    CogneeProviderUsageMeter,
    CogneeProviderUsageSnapshot,
)
from spine.infrastructure.local_cognee_provider_guards import (
    register_litellm_provider_usage_meter,
)

from tests.memory.cognee.worker_profile_fixtures import cognee_worker_profile_values


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def _limits(**overrides):
    from spine.memory.cognee.worker_profile import (
        CogneeWorkerLimits,
    )

    values = cognee_worker_profile_values()["limits"] | overrides
    return CogneeWorkerLimits.model_validate(values)


@pytest.mark.asyncio
async def test_successful_provider_callbacks_report_independent_request_token_and_cost_totals() -> None:
    meter = CogneeProviderUsageMeter(limits=_limits())

    async with meter.track_run("projection-run-1"):
        await meter.observe_provider_request()
        await meter.async_log_pre_api_call("openai/gpt-5.4-nano", [], {})
        await meter.async_log_success_event(
            {"response_cost": 0.000123},
            {"usage": {"prompt_tokens": 120, "completion_tokens": 30}},
            NOW,
            NOW,
        )
        await meter.observe_provider_request()
        await meter.async_log_pre_api_call("openai/text-embedding-3-small", [], {})
        await meter.async_log_success_event(
            {"call_type": "aembedding", "response_cost": 0.00005},
            {"usage": {"prompt_tokens": 80, "total_tokens": 80}},
            NOW,
            NOW,
        )

    assert await meter.snapshot("projection-run-1") == CogneeProviderUsageSnapshot(
        schema_version="cognee-provider-usage-snapshot:v1",
        request_count=2,
        call_count=2,
        successful_call_count=2,
        failed_call_count=0,
        input_tokens=200,
        output_tokens=30,
        cost_amount=Decimal("0.000173"),
        cost_currency="USD",
        usage_complete=True,
        limit_breaches=(),
    )


@pytest.mark.asyncio
async def test_concurrent_run_scopes_keep_provider_usage_isolated() -> None:
    meter = CogneeProviderUsageMeter(limits=_limits())

    async with meter.track_run("projection-run-1"):
        await meter.observe_provider_request()
        await meter.async_log_pre_api_call("openai/gpt-5.4-nano", [], {})
        async with meter.track_run("projection-run-2"):
            await meter.observe_provider_request()
            await meter.async_log_pre_api_call("openai/gpt-5.4-nano", [], {})
            await meter.async_log_failure_event({}, None, NOW, NOW)
        await meter.async_log_success_event(
            {"response_cost": 0.000001},
            {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
            NOW,
            NOW,
        )

    first = await meter.snapshot("projection-run-1")
    second = await meter.snapshot("projection-run-2")

    assert (first.request_count, first.successful_call_count, first.failed_call_count) == (
        1,
        1,
        0,
    )
    assert (second.request_count, second.successful_call_count, second.failed_call_count) == (
        1,
        0,
        1,
    )


@pytest.mark.asyncio
async def test_litellm_contract_meters_each_synthetic_http_attempt_including_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    provider_requests: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request.url.path)
        if len(provider_requests) == 1:
            return httpx.Response(
                503,
                request=request,
                json={"error": {"message": "retry", "type": "server_error"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-5.4-nano",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(litellm, "aclient_session", client)
    monkeypatch.setattr(litellm, "callbacks", [])
    monkeypatch.setattr(litellm, "input_callback", [])
    monkeypatch.setattr(litellm, "_async_input_callback", [])
    meter = CogneeProviderUsageMeter(limits=_limits())
    register_litellm_provider_usage_meter(meter)
    try:
        async with meter.track_run("projection-run-contract"):
            await litellm.acompletion(
                model="openai/gpt-5.4-nano",
                api_base="https://provider.test/v1",
                api_key="synthetic-provider-key",
                messages=[{"role": "user", "content": "synthetic prompt"}],
                max_retries=1,
            )
            assert await meter.wait_until_settled(
                "projection-run-contract", timeout_seconds=1
            )
    finally:
        await meter.close()
        await client.aclose()

    usage = await meter.snapshot("projection-run-contract")

    assert provider_requests == ["/v1/chat/completions", "/v1/chat/completions"]
    assert (usage.request_count, usage.input_tokens, usage.output_tokens) == (2, 2, 3)
    assert usage.cost_amount == Decimal("0.000004150")
    assert usage.usage_complete is True
    assert client.event_hooks["request"] == []
    assert litellm.callbacks == []
    assert litellm.input_callback == []


@pytest.mark.parametrize(
    ("limit_overrides", "expected_breach"),
    (
        (
            {"max_input_tokens": 199},
            "provider_input_token_limit_exceeded",
        ),
        (
            {"max_output_tokens": 29},
            "provider_output_token_limit_exceeded",
        ),
        (
            {"max_budget_amount": "0.499999"},
            "provider_cost_limit_exceeded",
        ),
    ),
)
@pytest.mark.asyncio
async def test_provider_usage_over_one_post_response_limit_reports_exact_breach(
    limit_overrides: dict[str, object],
    expected_breach: str,
) -> None:
    meter = CogneeProviderUsageMeter(limits=_limits(**limit_overrides))

    async with meter.track_run("projection-run-over-limit"):
        for _ in range(2):
            await meter.observe_provider_request()
            await meter.async_log_pre_api_call("openai/gpt-5.4-nano", [], {})
            await meter.async_log_success_event(
                {"response_cost": 0.25},
                {"usage": {"prompt_tokens": 100, "completion_tokens": 15}},
                NOW,
                NOW,
            )

    usage = await meter.snapshot("projection-run-over-limit")

    assert usage.limit_breaches == (expected_breach,)


@pytest.mark.asyncio
async def test_success_without_provider_cost_remains_explicitly_incomplete() -> None:
    meter = CogneeProviderUsageMeter(limits=_limits())

    async with meter.track_run("projection-run-missing-cost"):
        await meter.observe_provider_request()
        await meter.async_log_pre_api_call("openai/gpt-5.4-nano", [], {})
        await meter.async_log_success_event(
            {},
            {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
            NOW,
            NOW,
        )

    usage = await meter.snapshot("projection-run-missing-cost")

    assert usage.cost_amount == Decimal("0")
    assert usage.usage_complete is False


@pytest.mark.asyncio
async def test_explicit_zero_provider_cost_is_complete_usage() -> None:
    meter = CogneeProviderUsageMeter(limits=_limits())

    async with meter.track_run("projection-run-zero-cost"):
        await meter.observe_provider_request()
        await meter.async_log_pre_api_call("openai/text-embedding-3-small", [], {})
        await meter.async_log_success_event(
            {"call_type": "aembedding", "response_cost": 0.0},
            {"usage": {"prompt_tokens": 2, "total_tokens": 2}},
            NOW,
            NOW,
        )

    usage = await meter.snapshot("projection-run-zero-cost")

    assert usage.cost_amount == Decimal("0")
    assert usage.usage_complete is True
