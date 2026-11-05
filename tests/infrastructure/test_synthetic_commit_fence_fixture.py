from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.commit_fence import (
    CommitFenceBinding,
    canonical_commit_fence_lineage_digest,
)
from spine.application.commit_fence_provider_port import (
    CommitFenceFailureReason,
    CommitFenceOperation,
    CommitFenceOperationState,
)
from spine.application.private_asr_corpus import PrivateArtifactSourceLineage

from tests.infrastructure.fixtures.synthetic_commit_fence import (
    DeterministicBarrier,
    ManualCommitFenceClock,
    SyntheticCommitFenceAuthority,
    SyntheticCommitFenceBarriers,
)


NOW = datetime(2026, 11, 5, 14, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _binding(**updates: object) -> CommitFenceBinding:
    lineage = (
        PrivateArtifactSourceLineage(
            source_object_id=_id(10),
            source_revision_id=_id(11),
            source_revision_digest="a" * 64,
        ),
    )
    values: dict[str, object] = {
        "issuer_id": _id(1),
        "approval_version": "approval:v1",
        "approval_digest": "b" * 64,
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "purpose": "private_report_publication",
        "action": "commit_report",
        "subject_id": _id(4),
        "state_digest": "c" * 64,
        "source_lineage": lineage,
        "lineage_digest": canonical_commit_fence_lineage_digest(
            workspace_id=_id(2), environment_id=_id(3), source_lineage=lineage
        ),
        "generation": 1,
        "revocation_epoch": 1,
        "operation_id": _id(5),
        "idempotency_key": "commit-fence-operation-1",
        "valid_from": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(minutes=5),
    }
    values.update(updates)
    return CommitFenceBinding.model_validate(values)


@pytest.mark.asyncio
async def test_synthetic_authority_orders_commit_before_revoke() -> None:
    authority = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))
    binding = _binding()

    assert all(
        callable(getattr(authority, name))
        for name in ("acquire", "validate_before_commit", "finish", "abort")
    )
    acquired = await authority.acquire(binding)
    assert acquired.state is CommitFenceOperationState.ACQUIRED
    assert acquired.permit is not None
    checked = await authority.validate_before_commit(acquired.permit, evaluated_at=NOW)
    assert checked.permits_commit

    released = await authority.finish(acquired.permit)
    assert released.state is CommitFenceOperationState.RELEASED
    acknowledgement = await authority.revoke(binding)
    assert acknowledgement.revoked_through_epoch == binding.revocation_epoch
    assert acknowledgement.acknowledged_at == NOW


@pytest.mark.asyncio
async def test_revocation_acknowledgement_waits_for_active_permit_release() -> None:
    authority = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))
    binding = _binding()
    acquired = await authority.acquire(binding)
    assert acquired.permit is not None
    entered_ack_barrier = DeterministicBarrier(blocked=True)
    authority.barriers.before_revocation_acknowledgement = entered_ack_barrier

    revocation = asyncio.create_task(authority.revoke(binding))
    await authority.wait_for_revocation_request(binding)
    assert not revocation.done()

    checked = await authority.validate_before_commit(acquired.permit, evaluated_at=NOW)
    assert checked.permits_commit
    await authority.finish(acquired.permit)
    await entered_ack_barrier.wait_until_reached()
    assert not revocation.done()

    entered_ack_barrier.release()
    acknowledgement = await revocation
    assert acknowledgement.acknowledged_at == NOW


@pytest.mark.asyncio
async def test_revocation_that_precedes_acquire_denies_old_epoch() -> None:
    authority = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))
    binding = _binding()

    await authority.revoke(binding)
    result = await authority.acquire(binding)

    assert result.operation is CommitFenceOperation.ACQUIRE
    assert result.state is CommitFenceOperationState.STALE_HOLDER
    assert result.permit is None


@pytest.mark.asyncio
async def test_retry_after_revocation_requires_the_next_epoch() -> None:
    authority = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))
    original = _binding()
    await authority.revoke(original)

    old_retry = await authority.acquire(original)
    fresh_retry = await authority.acquire(
        _binding(revocation_epoch=original.revocation_epoch + 1)
    )

    assert old_retry.state is CommitFenceOperationState.STALE_HOLDER
    assert fresh_retry.state is CommitFenceOperationState.ACQUIRED


@pytest.mark.asyncio
async def test_expired_permit_cannot_validate_commit_or_be_renewed() -> None:
    clock = ManualCommitFenceClock(NOW)
    authority = SyntheticCommitFenceAuthority(clock=clock, permit_ttl=timedelta(seconds=5))
    acquired = await authority.acquire(_binding())
    assert acquired.permit is not None
    clock.advance(timedelta(seconds=5))

    checked = await authority.validate_before_commit(acquired.permit, evaluated_at=clock())
    renewed = await authority.renew(acquired.permit, evaluated_at=clock())

    assert checked.state is CommitFenceOperationState.EXPIRED
    assert not checked.permits_commit
    assert renewed.state is CommitFenceOperationState.EXPIRED


@pytest.mark.asyncio
async def test_renewal_extends_permit_only_within_binding_expiry() -> None:
    clock = ManualCommitFenceClock(NOW)
    authority = SyntheticCommitFenceAuthority(clock=clock, permit_ttl=timedelta(seconds=10))
    acquired = await authority.acquire(_binding(valid_until=NOW + timedelta(seconds=30)))
    assert acquired.permit is not None
    clock.advance(timedelta(seconds=8))

    renewed = await authority.renew(acquired.permit, evaluated_at=clock())

    assert renewed.state is CommitFenceOperationState.RENEWED
    assert renewed.permit is not None
    assert renewed.permit.permit_reference == acquired.permit.permit_reference
    assert renewed.permit.valid_until == NOW + timedelta(seconds=18)


@pytest.mark.asyncio
async def test_cancelled_work_can_release_through_abort() -> None:
    authority = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))
    release_results = []
    work_started = asyncio.Event()

    async def work() -> None:
        acquired = await authority.acquire(_binding())
        assert acquired.permit is not None
        try:
            work_started.set()
            await asyncio.Event().wait()
        finally:
            release_results.append(await authority.abort(acquired.permit))

    task = asyncio.create_task(work())
    await work_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert release_results[0].state is CommitFenceOperationState.RELEASED
    assert release_results[0].operation is CommitFenceOperation.ABORT
    assert (await authority.acquire(_binding())).state is CommitFenceOperationState.ACQUIRED


@pytest.mark.asyncio
async def test_ambiguous_release_stays_indeterminate_and_revocation_waits_for_expiry() -> None:
    clock = ManualCommitFenceClock(NOW)
    authority = SyntheticCommitFenceAuthority(clock=clock, permit_ttl=timedelta(seconds=5))
    acquired = await authority.acquire(_binding())
    assert acquired.permit is not None
    authority.make_release_ambiguous(acquired.permit)

    result = await authority.abort(acquired.permit)
    assert result.state is CommitFenceOperationState.INDETERMINATE
    assert result.failure_reason is CommitFenceFailureReason.LOST_RESPONSE

    revocation = asyncio.create_task(authority.revoke(acquired.permit.binding))
    await authority.wait_for_revocation_request(acquired.permit.binding)
    assert not revocation.done()

    clock.advance(timedelta(seconds=5))
    await authority.notify_clock_advanced()
    acknowledgement = await revocation
    assert acknowledgement.acknowledged_at == clock()


@pytest.mark.asyncio
async def test_acquire_barrier_makes_revocation_order_reproducible() -> None:
    clock = ManualCommitFenceClock(NOW)
    barrier = DeterministicBarrier(blocked=True)
    authority = SyntheticCommitFenceAuthority(
        clock=clock,
        barriers=SyntheticCommitFenceBarriers(before_acquire=barrier),
    )
    binding = _binding()
    acquire = asyncio.create_task(authority.acquire(binding))
    await barrier.wait_until_reached()

    revocation = asyncio.create_task(authority.revoke(binding))
    await authority.wait_for_revocation_request(binding)
    barrier.release()

    result, acknowledgement = await asyncio.gather(acquire, revocation)
    assert result.state is CommitFenceOperationState.STALE_HOLDER
    assert acknowledgement.revoked_through_epoch == binding.revocation_epoch


def test_synthetic_fixture_is_excluded_from_production_package() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    fixture_source = Path(__file__).resolve().parent / "fixtures" / "synthetic_commit_fence.py"
    production_source = repository_root / "src" / "spine"
    package_config = (repository_root / "pyproject.toml").read_text(encoding="utf-8")

    assert fixture_source.is_relative_to(repository_root / "tests")
    assert 'packages = ["src/spine"]' in package_config
    assert not any(
        "synthetic_commit_fence" in path.read_text(encoding="utf-8")
        for path in production_source.rglob("*.py")
    )

