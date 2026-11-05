from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.commit_fence import (
    CommitFenceBinding,
    canonical_commit_fence_lineage_digest,
)
from spine.application.commit_fence_provider_port import (
    CommitFenceFailureReason,
    CommitFenceOperation,
    CommitFenceOperationResult,
    CommitFenceOperationState,
    CommitFencePermit,
    ExternalCommitFencePort,
    RenewableExternalCommitFencePort,
)
from spine.application.private_asr_corpus import PrivateArtifactSourceLineage


NOW = datetime(2026, 11, 5, 14, tzinfo=timezone.utc)
CONTRACT_VERSION = "example-provider-contract:v1"


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
    if "workspace_id" in updates or "environment_id" in updates:
        values["lineage_digest"] = canonical_commit_fence_lineage_digest(
            workspace_id=values["workspace_id"],  # type: ignore[arg-type]
            environment_id=values["environment_id"],  # type: ignore[arg-type]
            source_lineage=lineage,
        )
    return CommitFenceBinding.model_validate(values)


def _result(
    operation: CommitFenceOperation,
    state: CommitFenceOperationState,
    **updates: object,
) -> CommitFenceOperationResult:
    binding = _binding()
    values: dict[str, object] = {
        "binding": binding,
        "provider_contract_version": CONTRACT_VERSION,
        "operation": operation,
        "state": state,
        "evaluated_at": NOW,
    }
    if state in {
        CommitFenceOperationState.ACQUIRED,
        CommitFenceOperationState.RENEWED,
        CommitFenceOperationState.VALID,
    }:
        values["permit"] = CommitFencePermit(
            binding=binding,
            provider_contract_version=CONTRACT_VERSION,
            permit_reference=_id(20),
            acquired_at=NOW,
            valid_until=NOW + timedelta(minutes=1),
        )
    elif state is CommitFenceOperationState.RELEASED:
        values["released_permit_reference"] = _id(20)
    if state in {
        CommitFenceOperationState.AMBIGUOUS,
        CommitFenceOperationState.INDETERMINATE,
    }:
        values["failure_reason"] = CommitFenceFailureReason.LOST_RESPONSE
    values.update(updates)
    return CommitFenceOperationResult.model_validate(values)


def test_provider_port_exposes_required_operations_and_optional_renewal() -> None:
    assert all(
        callable(getattr(ExternalCommitFencePort, name, None))
        for name in ("acquire", "validate_before_commit", "finish", "abort")
    )
    assert callable(getattr(RenewableExternalCommitFencePort, "renew", None))


@pytest.mark.parametrize(
    ("operation", "state"),
    [
        (CommitFenceOperation.ACQUIRE, CommitFenceOperationState.ACQUIRED),
        (CommitFenceOperation.RENEW, CommitFenceOperationState.RENEWED),
        (CommitFenceOperation.PRE_COMMIT, CommitFenceOperationState.VALID),
        (CommitFenceOperation.FINISH, CommitFenceOperationState.RELEASED),
        (CommitFenceOperation.ABORT, CommitFenceOperationState.RELEASED),
    ],
)
def test_provider_port_accepts_successful_operation_contract(
    operation: CommitFenceOperation, state: CommitFenceOperationState
) -> None:
    result = _result(operation, state)

    assert result.binding_digest == _binding().calculate_digest()
    assert result.provider_contract_version == CONTRACT_VERSION
    assert result.permits_commit is (operation is CommitFenceOperation.PRE_COMMIT)


@pytest.mark.parametrize(
    ("operation", "state"),
    [
        (CommitFenceOperation.ACQUIRE, CommitFenceOperationState.RENEWED),
        (CommitFenceOperation.RENEW, CommitFenceOperationState.ACQUIRED),
        (CommitFenceOperation.PRE_COMMIT, CommitFenceOperationState.RELEASED),
        (CommitFenceOperation.FINISH, CommitFenceOperationState.VALID),
        (CommitFenceOperation.ABORT, CommitFenceOperationState.ACQUIRED),
    ],
)
def test_provider_port_rejects_state_from_wrong_operation(
    operation: CommitFenceOperation, state: CommitFenceOperationState
) -> None:
    with pytest.raises(ValidationError):
        _result(operation, state)


@pytest.mark.parametrize(
    "state",
    [
        CommitFenceOperationState.DENIED,
        CommitFenceOperationState.STALE_HOLDER,
        CommitFenceOperationState.EXPIRED,
        CommitFenceOperationState.AMBIGUOUS,
        CommitFenceOperationState.INDETERMINATE,
        CommitFenceOperationState.UNSUPPORTED,
    ],
)
def test_provider_port_models_fail_closed_and_indeterminate_results(
    state: CommitFenceOperationState,
) -> None:
    result = _result(CommitFenceOperation.PRE_COMMIT, state)

    assert not result.permits_commit


def test_provider_port_rejects_permit_expiry_at_or_before_evaluation_time() -> None:
    with pytest.raises(ValidationError):
        _result(
            CommitFenceOperation.PRE_COMMIT,
            CommitFenceOperationState.VALID,
            permit_valid_until=NOW,
        )


def test_provider_port_rejects_permit_for_substituted_binding() -> None:
    requested_binding = _binding()
    wrong_binding = _binding(workspace_id=_id(99))
    permit = CommitFencePermit(
        binding=wrong_binding,
        provider_contract_version=CONTRACT_VERSION,
        permit_reference=_id(20),
        acquired_at=NOW,
        valid_until=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ValidationError, match="exact requested binding"):
        CommitFenceOperationResult(
            binding=requested_binding,
            provider_contract_version=CONTRACT_VERSION,
            operation=CommitFenceOperation.PRE_COMMIT,
            state=CommitFenceOperationState.VALID,
            evaluated_at=NOW,
            permit=permit,
        )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (CommitFenceOperationState.AMBIGUOUS, CommitFenceFailureReason.LOST_RESPONSE),
        (CommitFenceOperationState.INDETERMINATE, CommitFenceFailureReason.CLOCK_UNCERTAIN),
        (
            CommitFenceOperationState.INDETERMINATE,
            CommitFenceFailureReason.REVOCATION_ACKNOWLEDGEMENT_PENDING,
        ),
    ],
)
def test_provider_port_preserves_uncertainty_reason_without_granting_commit(
    state: CommitFenceOperationState,
    reason: CommitFenceFailureReason,
) -> None:
    result = _result(
        CommitFenceOperation.PRE_COMMIT,
        state,
        failure_reason=reason,
    )

    assert result.failure_reason is reason
    assert not result.permits_commit


def test_provider_port_requires_utc_evaluation_and_versioned_provider_contract() -> None:
    with pytest.raises(ValidationError):
        _result(
            CommitFenceOperation.ACQUIRE,
            CommitFenceOperationState.ACQUIRED,
            evaluated_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValidationError):
        _result(
            CommitFenceOperation.ACQUIRE,
            CommitFenceOperationState.ACQUIRED,
            provider_contract_version="",
        )


class DeterministicFencePort:
    """Scripted port responses for contract conformance; not a provider proof."""

    provider_contract_version = CONTRACT_VERSION

    def __init__(self, *responses: CommitFenceOperationResult) -> None:
        self._responses = deque(responses)

    def _respond(
        self,
        operation: CommitFenceOperation,
        binding: CommitFenceBinding,
    ) -> CommitFenceOperationResult:
        result = self._responses.popleft()
        assert result.operation is operation
        assert result.binding == binding
        return result

    async def acquire(self, binding: CommitFenceBinding) -> CommitFenceOperationResult:
        return self._respond(CommitFenceOperation.ACQUIRE, binding)

    async def renew(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult:
        del evaluated_at
        result = self._respond(CommitFenceOperation.RENEW, permit.binding)
        assert result.permit is not None
        assert result.permit.permit_reference == permit.permit_reference
        return result

    async def validate_before_commit(
        self, permit: CommitFencePermit, *, evaluated_at: datetime
    ) -> CommitFenceOperationResult:
        del evaluated_at
        result = self._respond(CommitFenceOperation.PRE_COMMIT, permit.binding)
        if result.permit is not None:
            assert result.permit.permit_reference == permit.permit_reference
        return result

    async def finish(self, permit: CommitFencePermit) -> CommitFenceOperationResult:
        result = self._respond(CommitFenceOperation.FINISH, permit.binding)
        assert result.released_permit_reference == permit.permit_reference
        return result

    async def abort(self, permit: CommitFencePermit) -> CommitFenceOperationResult:
        result = self._respond(CommitFenceOperation.ABORT, permit.binding)
        assert result.released_permit_reference == permit.permit_reference
        return result


@pytest.mark.asyncio
async def test_deterministic_port_conforms_for_acquire_renew_commit_and_release() -> None:
    binding = _binding()
    port = DeterministicFencePort(
        _result(CommitFenceOperation.ACQUIRE, CommitFenceOperationState.ACQUIRED),
        _result(CommitFenceOperation.RENEW, CommitFenceOperationState.RENEWED),
        _result(CommitFenceOperation.PRE_COMMIT, CommitFenceOperationState.VALID),
        _result(CommitFenceOperation.FINISH, CommitFenceOperationState.RELEASED),
        _result(CommitFenceOperation.ACQUIRE, CommitFenceOperationState.ACQUIRED),
        _result(CommitFenceOperation.ABORT, CommitFenceOperationState.RELEASED),
    )

    acquired = await port.acquire(binding)
    assert acquired.permit is not None
    renewed = await port.renew(acquired.permit, evaluated_at=NOW)
    assert renewed.state is CommitFenceOperationState.RENEWED
    assert renewed.permit is not None
    checked = await port.validate_before_commit(renewed.permit, evaluated_at=NOW)
    assert checked.permits_commit
    finished = await port.finish(checked.permit)
    assert finished.state is CommitFenceOperationState.RELEASED

    reacquired = await port.acquire(binding)
    assert reacquired.permit is not None
    aborted = await port.abort(reacquired.permit)
    assert aborted.state is CommitFenceOperationState.RELEASED


@pytest.mark.asyncio
async def test_deterministic_port_conformance_denies_expired_and_ambiguous_responses() -> None:
    binding = _binding()
    expired = _result(CommitFenceOperation.ACQUIRE, CommitFenceOperationState.EXPIRED)
    ambiguous = _result(
        CommitFenceOperation.PRE_COMMIT,
        CommitFenceOperationState.AMBIGUOUS,
        failure_reason=CommitFenceFailureReason.LOST_RESPONSE,
    )
    port = DeterministicFencePort(expired, ambiguous)

    denied = await port.acquire(binding)
    assert denied.state is CommitFenceOperationState.EXPIRED
    assert not denied.permits_commit

    permit = CommitFencePermit(
        binding=binding,
        provider_contract_version=CONTRACT_VERSION,
        permit_reference=_id(20),
        acquired_at=NOW,
        valid_until=NOW + timedelta(minutes=1),
    )
    uncertain = await port.validate_before_commit(permit, evaluated_at=NOW)
    assert uncertain.state is CommitFenceOperationState.AMBIGUOUS
    assert uncertain.failure_reason is CommitFenceFailureReason.LOST_RESPONSE
    assert not uncertain.permits_commit
