"""Port-driven coordinator for tenant-scoped source disposition work.

The coordinator persists the fence before calling any store. PostgreSQL-backed
atomicity, current deletion authority and publication/read composition are
implemented at their respective adapter boundaries by dependent issues.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.sources.disposition import (
    DispositionAuthorityBinding,
    DispositionOperation,
    DispositionStatus,
    DispositionStoreState,
    StoreEffectPermitStatus,
    StoreDispositionStatus,
)


DispositionStoreId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$"),
]


class DispositionConflictError(RuntimeError):
    """An operation identity was replayed with a different request digest."""


class DispositionAdmission(Protocol):
    """Atomically verifies current deletion authority and persists the fence."""

    async def fence_or_get(
        self,
        operation: DispositionOperation,
    ) -> DispositionOperation | None: ...

    async def is_current(self, operation: DispositionOperation) -> bool: ...

    async def store_effect_permit(
        self,
        operation: DispositionOperation,
        store_id: str,
    ) -> "DispositionStoreEffectPermit": ...

    async def rebind_authority(
        self,
        operation: DispositionOperation,
        new_binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None: ...


class DispositionStoreEffectPermit(Protocol):
    """Lease serializing one store effect against authority revocation."""

    async def __aenter__(self) -> StoreEffectPermitStatus: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class DispositionStore(Protocol):
    async def dispose(self, operation: DispositionOperation) -> "StoreOutcome": ...

    async def reconcile(self, operation: DispositionOperation) -> "StoreOutcome": ...


class DispositionOperationRepository(Protocol):
    """Durable implementations atomically create the operation and its fence."""

    async def save(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation: ...

    async def complete(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation:
        """CAS and write the minimal completion audit, retaining open retry state."""

    async def forget_completed(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> None:
        """Purge operation identity only after completion was delivered/acknowledged."""

    async def expire_completed(self, *, evaluated_at: datetime) -> int:
        """Purge expired completion identities, including later topology downgrades."""

    async def get(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
    ) -> DispositionOperation | None: ...


class StoreOutcome(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DispositionRequest(DefinitionModel):
    """Content-free operation scope and declared complete store coverage."""

    workspace_id: UUID
    environment_id: UUID
    operation_id: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    store_ids: tuple[DispositionStoreId, ...] = Field(min_length=1)
    authority_binding: DispositionAuthorityBinding | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "DispositionRequest":
        if any(value.int == 0 for value in (self.workspace_id, self.environment_id, self.operation_id)):
            raise ValueError("disposition request identifiers must be non-zero")
        if len(set(self.store_ids)) != len(self.store_ids):
            raise ValueError("disposition request store IDs must be unique")
        if self.authority_binding is not None and (
            self.authority_binding.workspace_id != self.workspace_id
            or self.authority_binding.environment_id != self.environment_id
            or self.authority_binding.operation_id != self.operation_id
        ):
            raise ValueError("disposition authority binding scope does not match request")
        return self


class DispositionCoordinator:
    completion_ack_window = timedelta(hours=24)

    def __init__(
        self,
        *,
        repository: DispositionOperationRepository,
        admission: DispositionAdmission,
        stores: Mapping[str, DispositionStore],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._admission = admission
        self._stores = dict(stores)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(self, request: DispositionRequest) -> DispositionOperation:
        await self.expire_completed_operations()
        store_ids = tuple(sorted(set(self._stores) | set(request.store_ids)))
        operation = DispositionOperation(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            operation_id=request.operation_id,
            request_digest=request.request_digest,
            declared_store_ids=request.store_ids,
            authority_binding=request.authority_binding,
            stores=tuple(DispositionStoreState(store_id=store_id) for store_id in store_ids),
        )
        current = await self._repository.get(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            operation_id=request.operation_id,
        )
        if current is not None:
            if current.request_digest != request.request_digest:
                raise DispositionConflictError("request digest changed")
            if set(current.declared_store_ids) != set(request.store_ids):
                raise DispositionConflictError("declared store coverage changed")
            if current.authority_binding != request.authority_binding:
                raise DispositionConflictError("disposition authority binding changed")
            if current.status is DispositionStatus.CANCELLED:
                return current
            if current.status is DispositionStatus.VERIFIED:
                if not self._has_exact_store_coverage(request, current):
                    return await self._save_coverage_mismatch(request, current)
                return current
            if not await self._admission.is_current(current):
                return await self._save(
                    current.model_copy(
                        update={
                            "status": DispositionStatus.INCOMPLETE,
                            "safe_code": "AUTHORITY_NOT_CURRENT",
                        }
                    )
                )
            operation = current
        else:
            current = await self._admission.fence_or_get(operation)
            if current is None:
                return operation.model_copy(
                    update={
                        "status": DispositionStatus.BLOCKED,
                        "fence_active": False,
                        "safe_code": "AUTHORITY_NOT_CURRENT",
                    }
                )
            operation = current
        if not self._has_exact_store_coverage(request, operation):
            return await self._save_coverage_mismatch(request, operation)
        return await self._run(operation)

    async def reconcile(self, request: DispositionRequest) -> DispositionOperation:
        await self.expire_completed_operations()
        operation = await self._repository.get(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            operation_id=request.operation_id,
        )
        if operation is None or operation.request_digest != request.request_digest:
            raise DispositionConflictError("disposition operation identity is unavailable")
        if set(operation.declared_store_ids) != set(request.store_ids):
            raise DispositionConflictError("declared store coverage changed")
        if operation.authority_binding != request.authority_binding:
            raise DispositionConflictError("disposition authority binding changed")
        if not self._has_exact_store_coverage(request, operation):
            return await self._save_coverage_mismatch(request, operation)
        if not any(
            store.status
            in {StoreDispositionStatus.UNKNOWN, StoreDispositionStatus.IN_PROGRESS}
            for store in operation.stores
        ):
            return operation
        return await self._run(operation)

    async def cancel(self, operation: DispositionOperation) -> DispositionOperation:
        await self.expire_completed_operations()
        current = await self._repository.get(
            workspace_id=operation.workspace_id,
            environment_id=operation.environment_id,
            operation_id=operation.operation_id,
        )
        if current is None or current.request_digest != operation.request_digest:
            raise DispositionConflictError("disposition operation identity is unavailable")
        if current.status in {DispositionStatus.UNKNOWN, DispositionStatus.VERIFIED}:
            return current
        return await self._save(
            current.model_copy(
                update={"status": DispositionStatus.CANCELLED, "safe_code": "CANCELLED"}
            )
        )

    async def resume_with_current_authority(
        self,
        request: DispositionRequest,
    ) -> DispositionOperation:
        """Resume the same fenced operation under a fresh exact authority binding."""

        await self.expire_completed_operations()
        current = await self._repository.get(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            operation_id=request.operation_id,
        )
        if (
            current is None
            or current.request_digest != request.request_digest
            or set(current.declared_store_ids) != set(request.store_ids)
            or request.authority_binding is None
            or not current.fence_active
            or current.status in {DispositionStatus.VERIFIED, DispositionStatus.CANCELLED}
        ):
            raise DispositionConflictError("disposition operation identity is unavailable")
        rebound = await self._admission.rebind_authority(
            current,
            request.authority_binding,
        )
        if rebound is None:
            return current.model_copy(
                update={"status": DispositionStatus.INCOMPLETE, "safe_code": "AUTHORITY_NOT_CURRENT"}
            )
        return await self.execute(request)

    async def _run(self, operation: DispositionOperation) -> DispositionOperation:
        if operation.status in {DispositionStatus.VERIFIED, DispositionStatus.CANCELLED}:
            return operation
        for index, store_state in enumerate(operation.stores):
            current = await self._repository.get(
                workspace_id=operation.workspace_id,
                environment_id=operation.environment_id,
                operation_id=operation.operation_id,
            )
            if current is None:
                return operation.model_copy(
                    update={"status": DispositionStatus.UNKNOWN, "safe_code": "STATE_UNAVAILABLE"}
                )
            if current.status is DispositionStatus.CANCELLED:
                return current
            if current.state_version != operation.state_version:
                return current
            if store_state.status is StoreDispositionStatus.VERIFIED:
                continue
            if not await self._admission.is_current(operation):
                return await self._save(
                    operation.model_copy(
                        update={
                            "status": DispositionStatus.INCOMPLETE,
                            "safe_code": "AUTHORITY_NOT_CURRENT",
                        }
                    )
                )
            handler = self._stores.get(store_state.store_id)
            if handler is None:
                operation = _replace_store(
                    operation,
                    index,
                    status=StoreDispositionStatus.UNKNOWN,
                    result_code="UNKNOWN_STORE",
                )
                operation = operation.model_copy(
                    update={"status": DispositionStatus.UNKNOWN, "safe_code": "UNKNOWN_STORE"}
                )
                return await self._save(operation)
            operation = await self._save(
                _start_store_attempt(operation, index)
            )
            if operation.status is DispositionStatus.CANCELLED:
                return operation
            permit_status = StoreEffectPermitStatus.UNAVAILABLE
            try:
                permit = await self._admission.store_effect_permit(
                    operation,
                    store_state.store_id,
                )
                async with permit as result:
                    permit_status = result
                    if result is StoreEffectPermitStatus.ALLOWED:
                        outcome = (
                            await handler.reconcile(operation)
                            if store_state.status
                            in {StoreDispositionStatus.UNKNOWN, StoreDispositionStatus.IN_PROGRESS}
                            else await handler.dispose(operation)
                        )
            except Exception:
                # A lost response may follow a committed deletion; reconcile
                # under the same operation identity before any replay.
                outcome = StoreOutcome.UNKNOWN
            if permit_status is not StoreEffectPermitStatus.ALLOWED:
                # No store call was allowed. Restore the previous commit state
                # so UNKNOWN/IN_PROGRESS is reconciled before any fresh delete.
                operation = _replace_store(
                    operation,
                    index,
                    status=store_state.status,
                    result_code=store_state.result_code,
                    attempts=store_state.attempts,
                )
                result_code = (
                    "AUTHORITY_NOT_CURRENT"
                    if permit_status is StoreEffectPermitStatus.AUTHORITY_NOT_CURRENT
                    else "STORE_PERMIT_UNAVAILABLE"
                    if permit_status is StoreEffectPermitStatus.UNAVAILABLE
                    else "OPERATION_NOT_CURRENT"
                )
                return await self._save(
                    operation.model_copy(
                        update={
                            "status": DispositionStatus.INCOMPLETE,
                            "safe_code": result_code,
                        }
                    )
                )
            current = await self._repository.get(
                workspace_id=operation.workspace_id,
                environment_id=operation.environment_id,
                operation_id=operation.operation_id,
            )
            if current is None:
                return operation.model_copy(
                    update={"status": DispositionStatus.UNKNOWN, "safe_code": "STATE_UNAVAILABLE"}
                )
            if current.status is DispositionStatus.CANCELLED:
                return current
            if current.state_version != operation.state_version:
                return current
            status = {
                StoreOutcome.VERIFIED: StoreDispositionStatus.VERIFIED,
                StoreOutcome.FAILED: StoreDispositionStatus.FAILED,
                StoreOutcome.UNKNOWN: StoreDispositionStatus.UNKNOWN,
            }.get(outcome)
            if status is None:
                status = StoreDispositionStatus.UNKNOWN
                result_code = "INVALID_STORE_RESULT"
            else:
                result_code = {
                    StoreDispositionStatus.VERIFIED: "STORE_VERIFIED",
                    StoreDispositionStatus.FAILED: "STORE_FAILED",
                    StoreDispositionStatus.UNKNOWN: "COMMIT_UNKNOWN",
                }[status]
            operation = _replace_store(operation, index, status=status, result_code=result_code)
            if status is StoreDispositionStatus.UNKNOWN:
                return await self._save(
                    operation.model_copy(
                        update={"status": DispositionStatus.UNKNOWN, "safe_code": "COMMIT_UNKNOWN"}
                    )
                )
            operation = await self._save(
                operation.model_copy(
                    update={
                        "status": DispositionStatus.INCOMPLETE,
                        "safe_code": (
                            "STORE_FAILED"
                            if status is StoreDispositionStatus.FAILED
                            else "DISPOSITION_PENDING"
                        ),
                    }
                )
            )
            if operation.status is DispositionStatus.CANCELLED:
                return operation
        if all(store.status is StoreDispositionStatus.VERIFIED for store in operation.stores):
            verified = operation.model_copy(
                update={
                    "status": DispositionStatus.VERIFIED,
                    "fence_active": True,
                    "safe_code": "DISPOSITION_VERIFIED",
                    "completion_ack_expires_at": (
                        self._aware_now() + self.completion_ack_window
                    ),
                }
            )
            try:
                return await self._repository.complete(
                    verified,
                    expected_state_version=operation.state_version,
                )
            except Exception:
                current = await self._repository.get(
                    workspace_id=operation.workspace_id,
                    environment_id=operation.environment_id,
                    operation_id=operation.operation_id,
                )
                if current is not None and current.status is DispositionStatus.VERIFIED:
                    return current
                if current is not None and current.status is DispositionStatus.CANCELLED:
                    return current
                return (current or operation).model_copy(
                    update={"status": DispositionStatus.UNKNOWN, "safe_code": "COMPLETION_UNKNOWN"}
                )
        return operation

    async def acknowledge_completion(
        self,
        operation: DispositionOperation,
    ) -> None:
        await self.expire_completed_operations()
        current = await self._repository.get(
            workspace_id=operation.workspace_id,
            environment_id=operation.environment_id,
            operation_id=operation.operation_id,
        )
        if current is None or current.request_digest != operation.request_digest:
            raise DispositionConflictError("disposition operation identity is unavailable")
        if current.status is not DispositionStatus.VERIFIED:
            raise DispositionConflictError("disposition completion has not been verified")
        request = DispositionRequest(
            workspace_id=current.workspace_id,
            environment_id=current.environment_id,
            operation_id=current.operation_id,
            request_digest=current.request_digest,
            store_ids=current.declared_store_ids,
            authority_binding=current.authority_binding,
        )
        if not self._has_exact_store_coverage(request, current):
            await self._save_coverage_mismatch(request, current)
            raise DispositionConflictError("store coverage changed before completion acknowledgement")
        await self._repository.forget_completed(
            current,
            expected_state_version=current.state_version,
        )

    async def expire_completed_operations(self) -> int:
        """Enforce the bounded response-replay window without keeping joins."""

        evaluated_at = self._aware_now()
        return await self._repository.expire_completed(evaluated_at=evaluated_at)

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("disposition coordinator clock must return an aware datetime")
        return now

    async def _save(self, operation: DispositionOperation) -> DispositionOperation:
        next_version = operation.state_version + 1
        try:
            return await self._repository.save(
                operation.model_copy(update={"state_version": next_version}),
                expected_state_version=operation.state_version,
            )
        except DispositionConflictError:
            current = await self._repository.get(
                workspace_id=operation.workspace_id,
                environment_id=operation.environment_id,
                operation_id=operation.operation_id,
            )
            if current is not None:
                return current
            raise

    async def _save_coverage_mismatch(
        self,
        request: DispositionRequest,
        operation: DispositionOperation,
    ) -> DispositionOperation:
        unknown_store_ids = set(self._stores) ^ set(request.store_ids)
        prior_stores = {store.store_id: store for store in operation.stores}
        all_store_ids = sorted(set(prior_stores) | set(self._stores) | set(request.store_ids))
        updated_stores = tuple(
            DispositionStoreState(
                store_id=store_id,
                status=(
                    StoreDispositionStatus.UNKNOWN
                    if store_id in unknown_store_ids
                    else prior_stores[store_id].status
                ),
                attempts=prior_stores[store_id].attempts if store_id in prior_stores else 0,
                result_code=(
                    "UNKNOWN_STORE"
                    if store_id in unknown_store_ids
                    else prior_stores[store_id].result_code
                ),
            )
            for store_id in all_store_ids
        )
        return await self._save(
            operation.model_copy(
                update={
                    "stores": updated_stores,
                    "status": DispositionStatus.UNKNOWN,
                    "safe_code": "UNKNOWN_STORE",
                }
            )
        )

    def _has_exact_store_coverage(
        self,
        request: DispositionRequest,
        operation: DispositionOperation,
    ) -> bool:
        requested = set(request.store_ids)
        configured = set(self._stores)
        return requested == configured and {store.store_id for store in operation.stores} == configured


def _replace_store(
    operation: DispositionOperation,
    index: int,
    *,
    status: StoreDispositionStatus,
    result_code: str | None,
    attempts: int | None = None,
) -> DispositionOperation:
    stores = list(operation.stores)
    prior = stores[index]
    stores[index] = DispositionStoreState(
        store_id=prior.store_id,
        status=status,
        attempts=prior.attempts if attempts is None else attempts,
        result_code=result_code,
    )
    return operation.model_copy(update={"stores": tuple(stores)})


def _start_store_attempt(
    operation: DispositionOperation,
    index: int,
) -> DispositionOperation:
    stores = list(operation.stores)
    prior = stores[index]
    stores[index] = DispositionStoreState(
        store_id=prior.store_id,
        status=StoreDispositionStatus.IN_PROGRESS,
        attempts=prior.attempts + 1,
    )
    return operation.model_copy(
        update={
            "stores": tuple(stores),
            "status": DispositionStatus.DELETION_PENDING,
            "safe_code": "DISPOSITION_PENDING",
        }
    )
