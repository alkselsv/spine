"""Fail-closed adapter seam for current deletion authority and source fences.

This module consumes the existing one-transition ``TombstoneAuthorization``
contract. It adds no positive read/processing policy; those authorities are
checked independently by their existing callers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from spine.domain.sources.deletion_authorization import TombstoneAuthorization
from spine.domain.sources.disposition import (
    DispositionAuthorityBinding,
    DispositionAuthoritySnapshot,
    DispositionOperation,
    StoreEffectPermitStatus,
    disposition_authority_is_current,
)


class DispositionAuthorityRepository(Protocol):
    """Serialization boundary for current pointers, revocation and the fence."""

    async def admit_if_current(
        self,
        operation: DispositionOperation,
        binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None:
        """Atomically validate current authority/expiry and insert DELETION_PENDING."""

    async def is_current(
        self,
        binding: DispositionAuthorityBinding,
        *,
        evaluated_at: datetime,
    ) -> bool:
        """Re-read deletion-only authority and source generation before work."""

    async def get_operation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
    ) -> DispositionOperation | None: ...

    async def store_effect_permit(
        self,
        operation: DispositionOperation,
        store_id: str,
    ) -> "DispositionStoreEffectPermit":
        """Serialize state, cancellation and authority; evaluate expiry at acquisition."""
        ...

    async def rebind_if_current(
        self,
        operation: DispositionOperation,
        new_binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None: ...


class DispositionStoreEffectPermit(Protocol):
    async def __aenter__(self) -> StoreEffectPermitStatus: ...

    async def __aexit__(self, exc_type, exc, traceback) -> None: ...


class DispositionAdmissionUnknownError(RuntimeError):
    """Fence admission may have committed but its result could not be read."""

    def __init__(self) -> None:
        super().__init__("Disposition admission state is unavailable.")


class CurrentDispositionAdmission:
    """Implement T3 admission using current deletion-policy state only."""

    def __init__(
        self,
        repository: DispositionAuthorityRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def fence_or_get(
        self,
        operation: DispositionOperation,
    ) -> DispositionOperation | None:
        binding = operation.authority_binding
        if binding is None or not _operation_matches_binding(operation, binding):
            return None
        try:
            return await self._repository.admit_if_current(
                operation,
                binding,
            )
        except Exception:
            try:
                persisted = await self._repository.get_operation(
                    workspace_id=operation.workspace_id,
                    environment_id=operation.environment_id,
                    operation_id=operation.operation_id,
                )
            except Exception:
                raise DispositionAdmissionUnknownError() from None
            if (
                persisted is not None
                and persisted.request_digest == operation.request_digest
                and persisted.authority_binding == binding
            ):
                return persisted
            raise DispositionAdmissionUnknownError() from None

    async def is_current(self, operation: DispositionOperation) -> bool:
        binding = operation.authority_binding
        if binding is None or not _operation_matches_binding(operation, binding):
            return False
        try:
            return await self._repository.is_current(
                binding,
                evaluated_at=_aware_now(self._clock),
            )
        except Exception:
            return False

    async def store_effect_permit(
        self,
        operation: DispositionOperation,
        store_id: str,
    ) -> DispositionStoreEffectPermit:
        binding = operation.authority_binding
        if binding is None or not _operation_matches_binding(operation, binding):
            return _DeniedStoreEffectPermit(StoreEffectPermitStatus.AUTHORITY_NOT_CURRENT)
        try:
            return await self._repository.store_effect_permit(
                operation,
                store_id,
            )
        except Exception:
            return _DeniedStoreEffectPermit(StoreEffectPermitStatus.UNAVAILABLE)

    async def rebind_authority(
        self,
        operation: DispositionOperation,
        new_binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None:
        old_binding = operation.authority_binding
        if old_binding is None or not _is_fresh_same_scope(old_binding, new_binding):
            return None
        try:
            return await self._repository.rebind_if_current(
                operation,
                new_binding,
            )
        except Exception:
            return None


def binding_from_tombstone_authorization(
    authorization: TombstoneAuthorization,
    *,
    operation_id: UUID,
) -> DispositionAuthorityBinding:
    """Project only the exact deletion authority fields needed during cleanup."""

    return DispositionAuthorityBinding(
        workspace_id=authorization.workspace_id,
        environment_id=authorization.environment_id,
        operation_id=operation_id,
        source_object_id=authorization.source_object_id,
        expected_revision_id=authorization.expected_current_revision_id,
        expected_acceptance_id=authorization.expected_current_acceptance_id,
        expected_generation=authorization.expected_current_cas_version,
        tombstone_authorization_id=authorization.authorization_id,
        authorization_digest=authorization.command_digest,
        deletion_policy_approval_id=authorization.deletion_policy_approval_id,
        deletion_policy_version_id=authorization.deletion_policy_version_id,
        deletion_policy_digest=authorization.deletion_policy_digest,
        deletion_policy_generation=authorization.deletion_policy_authority_generation,
        retention_eligibility_reference=authorization.retention_eligibility_reference,
        retention_eligibility_generation=authorization.retention_eligibility_generation,
        issuer_authority_id=authorization.issuer_authority_id,
        issuer_generation=authorization.issuer_authority_generation,
        expires_at=authorization.expires_at,
    )


def disposition_authority_snapshot_matches(
    binding: DispositionAuthorityBinding,
    snapshot: DispositionAuthoritySnapshot,
    *,
    evaluated_at: datetime,
) -> bool:
    """Disclosure-safe exact-current predicate for adapter and fake contracts."""

    return disposition_authority_is_current(
        binding,
        snapshot,
        evaluated_at=evaluated_at,
    )


def _operation_matches_binding(
    operation: DispositionOperation,
    binding: DispositionAuthorityBinding,
) -> bool:
    return (
        operation.workspace_id,
        operation.environment_id,
        operation.operation_id,
    ) == (
        binding.workspace_id,
        binding.environment_id,
        binding.operation_id,
    )


def _is_fresh_same_scope(
    prior: DispositionAuthorityBinding,
    replacement: DispositionAuthorityBinding,
) -> bool:
    return (
        (
            prior.workspace_id,
            prior.environment_id,
            prior.operation_id,
            prior.source_object_id,
            prior.expected_revision_id,
            prior.expected_acceptance_id,
            prior.expected_generation,
        )
        == (
            replacement.workspace_id,
            replacement.environment_id,
            replacement.operation_id,
            replacement.source_object_id,
            replacement.expected_revision_id,
            replacement.expected_acceptance_id,
            replacement.expected_generation,
        )
        and prior.tombstone_authorization_id != replacement.tombstone_authorization_id
        and replacement.deletion_policy_generation > prior.deletion_policy_generation
    )


class _DeniedStoreEffectPermit:
    def __init__(self, status: StoreEffectPermitStatus) -> None:
        self._status = status

    async def __aenter__(self) -> StoreEffectPermitStatus:
        return self._status

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    evaluated_at = clock()
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("disposition authority clock must return an aware datetime")
    return evaluated_at
