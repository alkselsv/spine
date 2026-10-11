from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from spine.application.storage.authorized_read import (
    AdmissionDisposition,
    AuthorizedOriginalReadGateway,
    OriginalReadDenied,
    OriginalReadDecision,
    OriginalReadLifecycle,
    OriginalReadRetention,
    OriginalReadTarget,
)
from spine.domain.sources import SourceRevisionReceiptAssociation, content_revision
from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    ConsumedOriginalReadLease,
    ObjectReference,
)


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _target(
    *,
    lifecycle: OriginalReadLifecycle = OriginalReadLifecycle.CURRENT,
    disposition: AdmissionDisposition = AdmissionDisposition.ADMITTED,
    retention: OriginalReadRetention = OriginalReadRetention.AVAILABLE,
    lifecycle_version: str = "source-v1",
) -> OriginalReadTarget:
    workspace_id = UUID("10000000-0000-4000-8000-000000000001")
    environment_id = UUID("20000000-0000-4000-8000-000000000001")
    source_object_id = UUID("30000000-0000-4000-8000-000000000001")
    revision_id = UUID("40000000-0000-4000-8000-000000000001")
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID("50000000-0000-4000-8000-000000000001"),
        storage_generation=UUID("60000000-0000-4000-8000-000000000001"),
        digest_algorithm="sha256",
        digest_hex="a" * 64,
        byte_length=4,
    )
    revision = content_revision(
        revision_id=revision_id,
        source_object_id=source_object_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata={"embedded_title": "Title"},
        original_reference=reference,
        original_sha256=reference.digest_hex,
        byte_length=reference.byte_length,
        media_type="text/plain",
        observed_at=NOW,
    )
    association = SourceRevisionReceiptAssociation(
        association_id=uuid4(),
        source_object_id=source_object_id,
        revision_id=revision_id,
        provenance_id=uuid4(),
        workspace_id=workspace_id,
        environment_id=environment_id,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=uuid4(),
        original_command_result_id=uuid4(),
        original_command_result_schema_version=1,
        original_upload_id=uuid4(),
        original_reference=reference,
        original_sha256=reference.digest_hex,
        byte_length=reference.byte_length,
        finalized_at=NOW,
        associated_at=NOW,
    )
    return OriginalReadTarget(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
        source_revision_id=revision_id,
        revision=revision,
        association=association,
        current_revision_id=revision_id if lifecycle is OriginalReadLifecycle.CURRENT else uuid4(),
        lifecycle=lifecycle,
        lifecycle_version=lifecycle_version,
        disposition=disposition,
        retention=retention,
    )


class _Resolver:
    def __init__(self, *targets: OriginalReadTarget) -> None:
        self.targets = list(targets)
        self.calls = 0

    async def resolve_original_read_target(self, **kwargs: object) -> OriginalReadTarget | None:
        target = self.targets[min(self.calls, len(self.targets) - 1)]
        self.calls += 1
        return target


class _Authorizer:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str]] = []

    async def decide_original_read(
        self, *, target: OriginalReadTarget, purpose: str, now: datetime
    ) -> OriginalReadDecision:
        self.calls.append((str(target.source_revision_id), purpose))
        return OriginalReadDecision(
            allowed=self.allowed,
            policy_version="policy-v1",
            lifecycle_version=target.lifecycle_version,
            expires_at=now + timedelta(minutes=1),
        )


class _ChangingAuthorizer(_Authorizer):
    def __init__(self) -> None:
        super().__init__()
        self._allowed = iter((True, False))

    async def decide_original_read(
        self, *, target: OriginalReadTarget, purpose: str, now: datetime
    ) -> OriginalReadDecision:
        self.calls.append((str(target.source_revision_id), purpose))
        return OriginalReadDecision(
            allowed=next(self._allowed),
            policy_version="policy-v1",
            lifecycle_version=target.lifecycle_version,
            expires_at=now + timedelta(minutes=1),
        )


class _Store:
    def __init__(self, registry: AuthorizedReadGrantRegistry) -> None:
        self.registry = registry
        self.leases: list[ConsumedOriginalReadLease] = []

    def open_bounded_read(
        self, lease: ConsumedOriginalReadLease, *, max_bytes: int
    ) -> AsyncIterator[bytes]:
        self.leases.append(lease)

        async def stream() -> AsyncIterator[bytes]:
            self.registry.consume_read_lease(lease, now=NOW)
            yield b"safe"

        return stream()


async def _read_all(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([part async for part in stream])


@pytest.mark.parametrize(
    "blocked_field",
    ["lifecycle", "disposition", "retention"],
)
async def test_gateway_denies_non_disclosable_target_before_storage(
    blocked_field: str,
) -> None:
    values: dict[str, object] = {
        "lifecycle": OriginalReadLifecycle.SUPERSEDED,
        "disposition": AdmissionDisposition.QUARANTINED,
        "retention": OriginalReadRetention.EXPIRED,
    }
    target = _target(**{blocked_field: values[blocked_field]})
    registry = AuthorizedReadGrantRegistry()
    store = _Store(registry)
    gateway = AuthorizedOriginalReadGateway(
        resolver=_Resolver(target),
        authorizer=_Authorizer(),
        object_store=store,
        grant_registry=registry,
        clock=lambda: NOW,
    )

    with pytest.raises(OriginalReadDenied):
        await _read_all(
            gateway.read_original(
                workspace_id=target.workspace_id,
                environment_id=target.environment_id,
                source_revision_id=target.source_revision_id,
                purpose="document.admission",
                max_bytes=16,
            )
        )
    assert store.leases == []


async def test_gateway_revalidates_before_issuing_grant_and_streaming() -> None:
    first = _target()
    changed = _target(lifecycle_version="source-v2")
    registry = AuthorizedReadGrantRegistry()
    store = _Store(registry)
    authorizer = _Authorizer()
    gateway = AuthorizedOriginalReadGateway(
        resolver=_Resolver(first, changed),
        authorizer=authorizer,
        object_store=store,
        grant_registry=registry,
        clock=lambda: NOW,
    )

    with pytest.raises(OriginalReadDenied):
        await _read_all(
            gateway.read_original(
                workspace_id=first.workspace_id,
                environment_id=first.environment_id,
                source_revision_id=first.source_revision_id,
                purpose="document.admission",
                max_bytes=16,
            )
        )
    assert store.leases == []
    assert len(authorizer.calls) == 1


async def test_gateway_denies_policy_revocation_between_checks() -> None:
    target = _target()
    registry = AuthorizedReadGrantRegistry()
    store = _Store(registry)
    gateway = AuthorizedOriginalReadGateway(
        resolver=_Resolver(target, target),
        authorizer=_ChangingAuthorizer(),
        object_store=store,
        grant_registry=registry,
        clock=lambda: NOW,
    )

    with pytest.raises(OriginalReadDenied):
        await _read_all(
            gateway.read_original(
                workspace_id=target.workspace_id,
                environment_id=target.environment_id,
                source_revision_id=target.source_revision_id,
                purpose="document.admission",
                max_bytes=16,
            )
        )
    assert store.leases == []


async def test_gateway_allows_only_after_two_policy_checks_and_consumes_grant_once() -> None:
    target = _target()
    registry = AuthorizedReadGrantRegistry()
    store = _Store(registry)
    authorizer = _Authorizer()
    gateway = AuthorizedOriginalReadGateway(
        resolver=_Resolver(target, target),
        authorizer=authorizer,
        object_store=store,
        grant_registry=registry,
        clock=lambda: NOW,
    )

    stream = gateway.read_original(
        workspace_id=target.workspace_id,
        environment_id=target.environment_id,
        source_revision_id=target.source_revision_id,
        purpose="document.admission",
        max_bytes=16,
    )
    assert await _read_all(stream) == b"safe"
    assert len(authorizer.calls) == 2
    assert len(store.leases) == 1


async def test_gateway_does_not_accept_caller_supplied_object_reference() -> None:
    target = _target()
    gateway = AuthorizedOriginalReadGateway(
        resolver=_Resolver(target),
        authorizer=_Authorizer(),
        object_store=_Store(AuthorizedReadGrantRegistry()),
        grant_registry=AuthorizedReadGrantRegistry(),
        clock=lambda: NOW,
    )

    with pytest.raises(TypeError):
        gateway.read_original(  # type: ignore[call-arg]
            workspace_id=target.workspace_id,
            environment_id=target.environment_id,
            source_revision_id=target.source_revision_id,
            object_reference=target.association.original_reference,
            purpose="document.admission",
            max_bytes=16,
        )
