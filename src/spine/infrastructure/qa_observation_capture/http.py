"""HTTP transport with an authenticated, bounded in-memory capture service.

The runtime owns the HTTP listener, TLS/workload identity, credential issuance,
and authorizer. This module contains no default credentials or endpoint values.
The service stores protected envelopes only in process memory and never logs
request bodies or payload details.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from spine.application.qa_observation.channel import (
    CaptureChannel,
    CaptureDisposition,
    CaptureReceiveResult,
    CaptureReceiveState,
)
from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureEnvelope,
    CaptureSubscription,
)
from spine.infrastructure.qa_observation_capture.volatile import VolatileCaptureStore

MAX_CAPTURE_PAYLOAD_BYTES = 1_048_576
MAX_SUBSCRIPTION_TTL = timedelta(minutes=10)
MAX_SUBSCRIPTIONS = 1024
MAX_BUFFERED_ENVELOPES = 128
MAX_RECEIVE_WAIT = timedelta(seconds=30)


class CaptureWorkloadRole(str, Enum):
    OBSERVER = "observer"
    WORKER = "worker"


class CaptureWorkload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workload_id: str = Field(min_length=1, max_length=200)
    role: CaptureWorkloadRole


class CaptureAuthorizer(Protocol):
    """Runtime policy for trusted scope, requester, and observer binding."""

    async def observer_may_bind(
        self, observer: CaptureWorkload, identity: AdmittedExecutionIdentity
    ) -> bool: ...

    async def worker_may_publish(
        self, worker: CaptureWorkload, identity: AdmittedExecutionIdentity
    ) -> bool: ...

    async def worker_may_deliver_to_observer(
        self,
        worker: CaptureWorkload,
        observer: CaptureWorkload,
        identity: AdmittedExecutionIdentity,
    ) -> bool: ...


Authenticator = Callable[[str | None], Awaitable[CaptureWorkload | None]]
HeadersProvider = Callable[[], Mapping[str, str]]


class _BindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: AdmittedExecutionIdentity


class _ReceiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: AdmittedExecutionIdentity
    timeout_ms: int


class _AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: UUID
    identity: AdmittedExecutionIdentity


class _RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: UUID
    ttl_ms: int


class _DispositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: CaptureDisposition


class _ReceiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: CaptureReceiveState
    capture: CaptureEnvelope | None = None

    @model_validator(mode="after")
    def validate_capture_state(self) -> "_ReceiveResponse":
        if (self.state is CaptureReceiveState.CAPTURED) != (self.capture is not None):
            raise ValueError("captured state requires exactly one envelope")
        return self


class _CaptureService:
    def __init__(
        self, *, authorizer: CaptureAuthorizer, clock: Callable[[], datetime]
    ) -> None:
        self._authorizer = authorizer
        self._channel = VolatileCaptureStore(
            clock=clock, max_buffered_envelopes=MAX_BUFFERED_ENVELOPES
        )
        self._owners: dict[UUID, tuple[CaptureWorkload, datetime]] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    async def register(
        self, workload: CaptureWorkload, capture_id: UUID, ttl: timedelta
    ) -> CaptureSubscription:
        async with self._lock:
            await self._prune_expired()
            try:
                subscription = await self._channel.register(
                    capture_id=capture_id, ttl=ttl
                )
            except ValueError:
                raise HTTPException(409, "capture subscription unavailable") from None
            existing_owner = self._owners.get(subscription.subscription_id)
            if existing_owner is not None:
                if existing_owner[0] != workload:
                    raise HTTPException(409, "capture subscription unavailable")
                return subscription
            if len(self._owners) >= MAX_SUBSCRIPTIONS:
                await self._channel.cleanup(
                    subscription_id=subscription.subscription_id
                )
                raise HTTPException(429, "capture channel unavailable")
            self._owners[subscription.subscription_id] = (
                workload,
                subscription.expires_at,
            )
            return subscription

    async def bind(
        self,
        workload: CaptureWorkload,
        subscription_id: UUID,
        identity: AdmittedExecutionIdentity,
    ) -> CaptureDisposition:
        await self._prune_expired()
        owner_entry = self._owners.get(subscription_id)
        owner = owner_entry[0] if owner_entry is not None else None
        if (
            not self._is_owner(owner, workload)
            or not await self._authorized_observer(workload, identity)
        ):
            return CaptureDisposition.NOT_FOUND
        return await self._channel.bind(
            subscription_id=subscription_id, identity=identity
        )

    async def receive(
        self,
        workload: CaptureWorkload,
        subscription_id: UUID,
        identity: AdmittedExecutionIdentity,
        timeout: timedelta,
        is_disconnected: Callable[[], Awaitable[bool]],
    ) -> CaptureReceiveResult:
        await self._prune_expired()
        owner_entry = self._owners.get(subscription_id)
        owner = owner_entry[0] if owner_entry is not None else None
        if not self._is_owner(owner, workload) or not await self._authorized_observer(
            workload, identity
        ):
            return CaptureReceiveResult(CaptureReceiveState.UNAVAILABLE)
        deadline = asyncio.get_running_loop().time() + timeout.total_seconds()
        while True:
            result = await self._channel.receive(
                subscription_id=subscription_id,
                identity=identity,
            )
            if result.state is not CaptureReceiveState.PENDING:
                return result
            if await is_disconnected():
                await self.cleanup(workload, subscription_id)
                return CaptureReceiveResult(CaptureReceiveState.UNAVAILABLE)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if timeout <= timedelta(0):
                    return result
                await self.cleanup(workload, subscription_id)
                return CaptureReceiveResult(CaptureReceiveState.UNAVAILABLE)
            await asyncio.sleep(min(0.01, remaining))

    async def publish(
        self, workload: CaptureWorkload, envelope: CaptureEnvelope
    ) -> CaptureDisposition:
        await self._prune_expired()
        owner_entry = self._owners.get(envelope.subscription_id)
        owner = owner_entry[0] if owner_entry is not None else None
        if (
            workload.role is not CaptureWorkloadRole.WORKER
            or owner is None
            or not await self._authorized_worker(workload, envelope.identity)
            or not await self._authorized_delivery(
                workload, owner, envelope.identity
            )
        ):
            return CaptureDisposition.NOT_FOUND
        return await self._channel.publish(envelope)

    async def acknowledge(
        self,
        workload: CaptureWorkload,
        subscription_id: UUID,
        capture_id: UUID,
        identity: AdmittedExecutionIdentity,
    ) -> CaptureDisposition:
        await self._prune_expired()
        owner_entry = self._owners.get(subscription_id)
        owner = owner_entry[0] if owner_entry is not None else None
        if (
            not self._is_owner(owner, workload)
            or not await self._authorized_observer(workload, identity)
        ):
            return CaptureDisposition.NOT_FOUND
        return await self._channel.acknowledge(
            subscription_id=subscription_id,
            capture_id=capture_id,
            identity=identity,
        )

    async def cleanup(
        self, workload: CaptureWorkload, subscription_id: UUID
    ) -> CaptureDisposition:
        await self._prune_expired()
        owner_entry = self._owners.get(subscription_id)
        owner = owner_entry[0] if owner_entry is not None else None
        if not self._is_owner(owner, workload):
            return CaptureDisposition.NOT_FOUND
        self._owners.pop(subscription_id, None)
        return await self._channel.cleanup(subscription_id=subscription_id)

    @staticmethod
    def _is_owner(owner: CaptureWorkload | None, workload: CaptureWorkload) -> bool:
        return (
            owner is not None
            and workload.role is CaptureWorkloadRole.OBSERVER
            and owner == workload
        )

    async def _authorized_observer(
        self, workload: CaptureWorkload, identity: AdmittedExecutionIdentity
    ) -> bool:
        try:
            return await self._authorizer.observer_may_bind(workload, identity)
        except Exception:
            return False

    async def _authorized_worker(
        self, workload: CaptureWorkload, identity: AdmittedExecutionIdentity
    ) -> bool:
        try:
            return await self._authorizer.worker_may_publish(workload, identity)
        except Exception:
            return False

    async def _authorized_delivery(
        self,
        worker: CaptureWorkload,
        observer: CaptureWorkload,
        identity: AdmittedExecutionIdentity,
    ) -> bool:
        try:
            return await self._authorizer.worker_may_deliver_to_observer(
                worker, observer, identity
            )
        except Exception:
            return False

    async def _prune_expired(self) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("capture channel clock must return aware UTC timestamps")
        now = now.astimezone(timezone.utc)
        expired = [
            subscription_id
            for subscription_id, (_, expires_at) in self._owners.items()
            if now >= expires_at
        ]
        for subscription_id in expired:
            self._owners.pop(subscription_id, None)
            await self._channel.cleanup(subscription_id=subscription_id)


def create_capture_channel_router(
    *,
    authenticator: Authenticator,
    authorizer: CaptureAuthorizer,
    clock: Callable[[], datetime],
) -> APIRouter:
    """Create service routes for a runtime-owned authenticated HTTP listener."""

    service = _CaptureService(authorizer=authorizer, clock=clock)
    router = APIRouter(prefix="/internal/qa-capture", include_in_schema=False)

    async def authenticate(request: Request, role: CaptureWorkloadRole) -> CaptureWorkload:
        authorization = request.headers.get("authorization")
        try:
            workload = await authenticator(authorization)
        except Exception:
            workload = None
        if workload is None or workload.role is not role:
            raise HTTPException(401, "capture channel unavailable")
        return workload

    async def parse_body(request: Request, schema: type[BaseModel]) -> BaseModel:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_CAPTURE_PAYLOAD_BYTES:
                raise HTTPException(413, "capture payload unavailable")
            chunks.append(chunk)
        try:
            return schema.model_validate_json(b"".join(chunks))
        except ValidationError:
            raise HTTPException(400, "capture request unavailable") from None

    @router.post("/subscriptions", response_model=CaptureSubscription)
    async def register(request: Request) -> CaptureSubscription:
        workload = await authenticate(request, CaptureWorkloadRole.OBSERVER)
        data = await parse_body(request, _RegisterRequest)
        assert isinstance(data, _RegisterRequest)
        ttl = timedelta(milliseconds=data.ttl_ms)
        if data.ttl_ms < 1 or ttl > MAX_SUBSCRIPTION_TTL:
            raise HTTPException(400, "capture request unavailable")
        return await service.register(workload, data.capture_id, ttl)

    @router.post(
        "/subscriptions/{subscription_id}/bind", response_model=_DispositionResponse
    )
    async def bind(
        subscription_id: UUID, request: Request
    ) -> _DispositionResponse:
        workload = await authenticate(request, CaptureWorkloadRole.OBSERVER)
        data = await parse_body(request, _BindRequest)
        assert isinstance(data, _BindRequest)
        disposition = await service.bind(workload, subscription_id, data.identity)
        return _DispositionResponse(disposition=disposition)

    @router.post(
        "/subscriptions/{subscription_id}/receive", response_model=_ReceiveResponse
    )
    async def receive(
        subscription_id: UUID, request: Request
    ) -> _ReceiveResponse:
        workload = await authenticate(request, CaptureWorkloadRole.OBSERVER)
        data = await parse_body(request, _ReceiveRequest)
        assert isinstance(data, _ReceiveRequest)
        if (
            data.timeout_ms < 0
            or data.timeout_ms > int(MAX_RECEIVE_WAIT.total_seconds() * 1000)
        ):
            raise HTTPException(400, "capture request unavailable")
        try:
            result = await service.receive(
                workload,
                subscription_id,
                data.identity,
                timedelta(milliseconds=data.timeout_ms),
                request.is_disconnected,
            )
        except asyncio.CancelledError:
            await service.cleanup(workload, subscription_id)
            raise
        return _ReceiveResponse(state=result.state, capture=result.capture)

    @router.post(
        "/subscriptions/{subscription_id}/acknowledge",
        response_model=_DispositionResponse,
    )
    async def acknowledge(
        subscription_id: UUID, request: Request
    ) -> _DispositionResponse:
        workload = await authenticate(request, CaptureWorkloadRole.OBSERVER)
        data = await parse_body(request, _AcknowledgeRequest)
        assert isinstance(data, _AcknowledgeRequest)
        disposition = await service.acknowledge(
            workload, subscription_id, data.capture_id, data.identity
        )
        return _DispositionResponse(disposition=disposition)

    @router.delete(
        "/subscriptions/{subscription_id}", response_model=_DispositionResponse
    )
    async def cleanup(
        subscription_id: UUID, request: Request
    ) -> _DispositionResponse:
        workload = await authenticate(request, CaptureWorkloadRole.OBSERVER)
        disposition = await service.cleanup(workload, subscription_id)
        return _DispositionResponse(disposition=disposition)

    @router.post("/captures", response_model=_DispositionResponse)
    async def publish(request: Request) -> _DispositionResponse:
        workload = await authenticate(request, CaptureWorkloadRole.WORKER)
        data = await parse_body(request, CaptureEnvelope)
        assert isinstance(data, CaptureEnvelope)
        disposition = await service.publish(workload, data)
        return _DispositionResponse(disposition=disposition)

    return router


class HttpCaptureChannel(CaptureChannel):
    """Client adapter for separately deployed worker and observer processes.

    Production callers supply an HTTPS endpoint and inject workload credentials
    through ``headers`` and TLS/workload identity through ``transport``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: HeadersProvider,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 35.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/internal/qa-capture",
            headers=dict(headers()),
            transport=transport,
            timeout=timeout,
        )
        self._headers = headers

    async def close(self) -> None:
        await self._client.aclose()

    async def register(self, *, capture_id: UUID, ttl: timedelta) -> CaptureSubscription:
        response = await self._request(
            "POST",
            "/subscriptions",
            json={
                "capture_id": str(capture_id),
                "ttl_ms": int(ttl.total_seconds() * 1000),
            },
        )
        return CaptureSubscription.model_validate(response.json())

    async def bind(
        self, *, subscription_id: UUID, identity: AdmittedExecutionIdentity
    ) -> CaptureDisposition:
        return await self._request_disposition(
            "POST",
            f"/subscriptions/{subscription_id}/bind",
            json={"identity": identity.model_dump(mode="json")},
        )

    async def receive(
        self,
        *,
        subscription_id: UUID,
        identity: AdmittedExecutionIdentity,
        timeout: timedelta,
    ) -> CaptureReceiveResult:
        try:
            response = await self._request(
                "POST",
                f"/subscriptions/{subscription_id}/receive",
                json={
                    "identity": identity.model_dump(mode="json"),
                    "timeout_ms": int(timeout.total_seconds() * 1000),
                },
                timeout=max(self._client.timeout.read or 35.0, timeout.total_seconds() + 1.0),
            )
            data = _ReceiveResponse.model_validate(response.json())
            return CaptureReceiveResult(state=data.state, capture=data.capture)
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                self.cleanup(subscription_id=subscription_id)
            )
            while True:
                try:
                    disposition = await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    if cleanup_task.done():
                        raise CaptureTransportUnavailable() from None
                except Exception:
                    raise CaptureTransportUnavailable() from None
            if disposition not in {
                CaptureDisposition.CLEANED,
                CaptureDisposition.NOT_FOUND,
            }:
                raise CaptureTransportUnavailable()
            raise
        except (
            CaptureTransportUnavailable,
            httpx.HTTPError,
            ValidationError,
            ValueError,
        ):
            return CaptureReceiveResult(CaptureReceiveState.UNAVAILABLE)

    async def publish(self, envelope: CaptureEnvelope) -> CaptureDisposition:
        return await self._request_disposition(
            "POST", "/captures", json=envelope.model_dump(mode="json")
        )

    async def acknowledge(
        self,
        *,
        subscription_id: UUID,
        capture_id: UUID,
        identity: AdmittedExecutionIdentity,
    ) -> CaptureDisposition:
        return await self._request_disposition(
            "POST",
            f"/subscriptions/{subscription_id}/acknowledge",
            json={
                "capture_id": str(capture_id),
                "identity": identity.model_dump(mode="json"),
            },
        )

    async def cleanup(self, *, subscription_id: UUID) -> CaptureDisposition:
        return await self._request_disposition(
            "DELETE", f"/subscriptions/{subscription_id}"
        )

    async def _request_disposition(
        self, method: str, path: str, **kwargs: object
    ) -> CaptureDisposition:
        try:
            response = await self._request(method, path, **kwargs)
            return _DispositionResponse.model_validate(response.json()).disposition
        except (CaptureTransportUnavailable, ValidationError, ValueError):
            return CaptureDisposition.UNAVAILABLE

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        headers = dict(self._headers())
        payload = kwargs.pop("json", None)
        if payload is not None:
            from json import dumps

            encoded_payload = dumps(payload, separators=(",", ":")).encode()
            if len(encoded_payload) > MAX_CAPTURE_PAYLOAD_BYTES:
                raise CaptureTransportUnavailable()
            kwargs["content"] = encoded_payload
            headers.setdefault("Content-Type", "application/json")
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except (httpx.HTTPError, RuntimeError):
            raise CaptureTransportUnavailable() from None
        if response.is_error:
            raise CaptureTransportUnavailable()
        if len(response.content) > MAX_CAPTURE_PAYLOAD_BYTES:
            raise CaptureTransportUnavailable()
        return response


class CaptureTransportUnavailable(RuntimeError):
    """Safe transport failure; deliberately contains no payload or credentials."""
