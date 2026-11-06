"""PostgreSQL operation state and current-authority fence for source disposition."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, delete, insert, select, update

from spine.application.disposition_authority import (
    DispositionAdmissionUnknownError,
    binding_from_tombstone_authorization,
    disposition_authority_snapshot_matches,
)
from spine.application.disposition_coordinator import (
    DispositionConflictError,
    DispositionOperationRepository,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import CanonicalEligibilityError
from spine.domain.sources.deletion_authorization import (
    TombstoneAuthorization,
    TombstoneAuthorizationDisposition,
)
from spine.domain.sources.disposition import (
    DispositionAuthorityBinding,
    DispositionAuthoritySnapshot,
    DispositionOperation,
    DispositionStatus,
    StoreEffectPermitStatus,
    disposition_authority_is_current,
)
from spine.infrastructure.persistence.postgresql_canonical_tombstone import (
    PostgreSQLCurrentTombstoneAuthorizationAuthority,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    current_source_heads,
    source_disposition_operations,
    source_disposition_outbox,
    tombstone_authorizations,
)
from spine.infrastructure.persistence.postgresql_source_locks import (
    lock_source_lifecycle_shared,
)


class PostgreSQLDispositionUnitOfWork(Protocol):
    async def execute_statement(self, statement: object, parameters: object | None = None): ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class PostgreSQLDispositionUnitOfWorkFactory(Protocol):
    def __call__(
        self, context: TrustedPersistenceContext
    ) -> AbstractAsyncContextManager[PostgreSQLDispositionUnitOfWork]: ...


DispositionContextFactory = Callable[[UUID, UUID], TrustedPersistenceContext]


class PostgreSQLDispositionRepository(DispositionOperationRepository):
    """Persist operation state, current authority and the wake-up intent atomically.

    A store-effect permit holds the current-pointer and operation-row locks for
    the duration of one handler call. It does not lock canonical content rows.
    """

    def __init__(
        self,
        *,
        uow_factory: PostgreSQLDispositionUnitOfWorkFactory,
        context_factory: DispositionContextFactory,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def admit_if_current(
        self,
        operation: DispositionOperation,
        binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None:
        if operation.authority_binding != binding or not _scope_matches(operation, binding):
            return None
        context = self._context(operation.workspace_id, operation.environment_id)
        try:
            async with self._uow_factory(context) as uow:
                await lock_source_lifecycle_shared(
                    uow.execute_statement,
                    workspace_id=operation.workspace_id,
                    environment_id=operation.environment_id,
                    source_object_id=binding.source_object_id,
                )
                snapshot = await self._authority_snapshot(uow, binding)
                now = _aware_now(self._clock)
                if snapshot is None or not disposition_authority_snapshot_matches(
                    binding, snapshot, evaluated_at=now
                ):
                    await uow.rollback()
                    return None
                existing = await self._get_in_uow(uow, operation.workspace_id, operation.environment_id, operation.operation_id, for_update=True)
                if existing is not None:
                    await uow.rollback()
                    if (
                        existing.request_digest == operation.request_digest
                        and existing.authority_binding == binding
                    ):
                        return existing
                    return None
                await uow.execute_statement(
                    insert(source_disposition_operations).values(
                        **_operation_values(operation, now)
                    )
                )
                await uow.execute_statement(
                    insert(source_disposition_outbox).values(
                        event_id=_outbox_event_id(operation),
                        workspace_id=operation.workspace_id,
                        environment_id=operation.environment_id,
                        operation_id=operation.operation_id,
                        event_type="source.disposition.requested.v1",
                        created_at=now,
                    )
                )
                await uow.commit()
            return operation
        except Exception:
            # Admission may have committed before the caller lost the response.
            try:
                persisted = await self.get(
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

    async def is_current(
        self,
        binding: DispositionAuthorityBinding,
        *,
        evaluated_at: datetime,
    ) -> bool:
        try:
            context = self._context(binding.workspace_id, binding.environment_id)
            async with self._uow_factory(context) as uow:
                await lock_source_lifecycle_shared(
                    uow.execute_statement,
                    workspace_id=binding.workspace_id,
                    environment_id=binding.environment_id,
                    source_object_id=binding.source_object_id,
                )
                snapshot = await self._authority_snapshot(uow, binding)
                return snapshot is not None and disposition_authority_snapshot_matches(
                    binding, snapshot, evaluated_at=evaluated_at
                )
        except Exception:
            return False

    async def store_effect_permit(
        self,
        operation: DispositionOperation,
        store_id: str,
    ) -> "PostgreSQLStoreEffectPermit":
        del store_id
        return PostgreSQLStoreEffectPermit(self, operation)

    async def rebind_if_current(
        self,
        operation: DispositionOperation,
        new_binding: DispositionAuthorityBinding,
    ) -> DispositionOperation | None:
        previous = operation.authority_binding
        if previous is None or not _fresh_same_scope(previous, new_binding):
            return None
        context = self._context(operation.workspace_id, operation.environment_id)
        async with self._uow_factory(context) as uow:
            await lock_source_lifecycle_shared(
                uow.execute_statement,
                workspace_id=operation.workspace_id,
                environment_id=operation.environment_id,
                source_object_id=new_binding.source_object_id,
            )
            row = await self._operation_row(uow, operation, for_update=True)
            current = _operation_from_row(row)
            if (
                current is None
                or current.state_version != operation.state_version
                or current.authority_binding != previous
                or not current.fence_active
                or current.status in {DispositionStatus.VERIFIED, DispositionStatus.CANCELLED}
            ):
                await uow.rollback()
                return None
            snapshot = await self._authority_snapshot(uow, new_binding)
            now = _aware_now(self._clock)
            if snapshot is None or not disposition_authority_snapshot_matches(
                new_binding, snapshot, evaluated_at=now
            ):
                await uow.rollback()
                return None
            rebound = current.model_copy(
                update={
                    "authority_binding": new_binding,
                    "state_version": current.state_version + 1,
                    "status": DispositionStatus.INCOMPLETE,
                    "safe_code": "AUTHORITY_REBOUND",
                }
            )
            await self._update_operation(uow, rebound, expected_state_version=current.state_version)
            await uow.commit()
        return rebound

    async def save(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation:
        context = self._context(operation.workspace_id, operation.environment_id)
        async with self._uow_factory(context) as uow:
            await self._update_operation(uow, operation, expected_state_version=expected_state_version)
            await uow.commit()
        return operation

    async def complete(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> DispositionOperation:
        completed = operation.model_copy(
            update={"state_version": expected_state_version + 1}
        )
        context = self._context(operation.workspace_id, operation.environment_id)
        async with self._uow_factory(context) as uow:
            await self._update_operation(
                uow, completed, expected_state_version=expected_state_version
            )
            await uow.commit()
        return completed

    async def forget_completed(
        self,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> None:
        context = self._context(operation.workspace_id, operation.environment_id)
        async with self._uow_factory(context) as uow:
            result = await uow.execute_statement(
                delete(source_disposition_operations).where(
                    source_disposition_operations.c.workspace_id == operation.workspace_id,
                    source_disposition_operations.c.environment_id == operation.environment_id,
                    source_disposition_operations.c.operation_id == operation.operation_id,
                    source_disposition_operations.c.state_version == expected_state_version,
                    source_disposition_operations.c.status == DispositionStatus.VERIFIED.value,
                )
            )
            if result.rowcount != 1:
                raise DispositionConflictError("disposition completion changed")
            await uow.commit()

    async def expire_completed(self, *, evaluated_at: datetime) -> int:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("disposition expiry time must be timezone-aware")
        # Expiry is performed per tenant by the scheduled T9 worker, which
        # supplies a trusted context. The repository does not use an unscoped
        # privileged delete here.
        return 0

    async def get(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
    ) -> DispositionOperation | None:
        context = self._context(workspace_id, environment_id)
        async with self._uow_factory(context) as uow:
            row = (
                await uow.execute_statement(
                    select(source_disposition_operations).where(
                        source_disposition_operations.c.workspace_id == workspace_id,
                        source_disposition_operations.c.environment_id == environment_id,
                        source_disposition_operations.c.operation_id == operation_id,
                    )
                )
            ).mappings().one_or_none()
            return _operation_from_row(row)

    async def get_operation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
    ) -> DispositionOperation | None:
        return await self.get(
            workspace_id=workspace_id,
            environment_id=environment_id,
            operation_id=operation_id,
        )

    def _context(self, workspace_id: UUID, environment_id: UUID) -> TrustedPersistenceContext:
        context = self._context_factory(workspace_id, environment_id)
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (workspace_id, environment_id):
            raise ValueError("disposition persistence context scope is invalid")
        return context

    async def _operation_row(
        self,
        uow: PostgreSQLDispositionUnitOfWork,
        operation: DispositionOperation,
        *,
        for_update: bool,
    ):
        query = select(source_disposition_operations).where(
            source_disposition_operations.c.workspace_id == operation.workspace_id,
            source_disposition_operations.c.environment_id == operation.environment_id,
            source_disposition_operations.c.operation_id == operation.operation_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await uow.execute_statement(query)).mappings().one_or_none()

    async def _get_in_uow(
        self,
        uow: PostgreSQLDispositionUnitOfWork,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: UUID,
        *,
        for_update: bool,
    ) -> DispositionOperation | None:
        query = select(source_disposition_operations).where(
            source_disposition_operations.c.workspace_id == workspace_id,
            source_disposition_operations.c.environment_id == environment_id,
            source_disposition_operations.c.operation_id == operation_id,
        )
        if for_update:
            query = query.with_for_update()
        row = (await uow.execute_statement(query)).mappings().one_or_none()
        return _operation_from_row(row)

    async def _update_operation(
        self,
        uow: PostgreSQLDispositionUnitOfWork,
        operation: DispositionOperation,
        *,
        expected_state_version: int,
    ) -> None:
        now = _aware_now(self._clock)
        result = await uow.execute_statement(
            update(source_disposition_operations)
            .where(
                source_disposition_operations.c.workspace_id == operation.workspace_id,
                source_disposition_operations.c.environment_id == operation.environment_id,
                source_disposition_operations.c.operation_id == operation.operation_id,
                source_disposition_operations.c.state_version == expected_state_version,
            )
            .values(
                request_digest=operation.request_digest,
                state_version=operation.state_version,
                status=operation.status.value,
                fence_active=operation.fence_active,
                operation_data=operation.model_dump(mode="json"),
                completion_ack_expires_at=operation.completion_ack_expires_at,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise DispositionConflictError("disposition state version changed")

    async def _authority_snapshot(
        self,
        uow: PostgreSQLDispositionUnitOfWork,
        binding: DispositionAuthorityBinding,
    ) -> DispositionAuthoritySnapshot | None:
        execute = uow.execute_statement
        source = (
            await execute(
                select(current_source_heads)
                .where(
                    current_source_heads.c.workspace_id == binding.workspace_id,
                    current_source_heads.c.environment_id == binding.environment_id,
                    current_source_heads.c.source_object_id == binding.source_object_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if source is None:
            return None
        authorization_record = (
            await execute(
                select(tombstone_authorizations.c.record).where(
                    tombstone_authorizations.c.workspace_id == binding.workspace_id,
                    tombstone_authorizations.c.environment_id == binding.environment_id,
                    tombstone_authorizations.c.authorization_id == binding.tombstone_authorization_id,
                    tombstone_authorizations.c.source_object_id == binding.source_object_id,
                    tombstone_authorizations.c.command_digest == binding.authorization_digest,
                )
            )
        ).scalar_one_or_none()
        authorization = _model(TombstoneAuthorization, authorization_record)
        if authorization is None or binding_from_tombstone_authorization(
            authorization, operation_id=binding.operation_id
        ) != binding:
            return None
        now = _aware_now(self._clock)
        authority = PostgreSQLCurrentTombstoneAuthorizationAuthority(clock=lambda: now)
        try:
            selector = await authority._lock_selector(execute, authorization, now)
            evidence = await authority._evidence(execute, authorization)
            policy = await authority._lock_policy(execute, authorization)
            issuer = await authority._lock_issuer(execute, authorization)
            decision = await authority._lock_decision(execute, authorization)
            connection_ok = await authority._lock_connection(execute, authorization)
            lifecycle_ok = await authority._lock_lifecycle(execute, authorization)
            retention_ok = await authority._lock_retention(execute, authorization, now)
            source_ok = await authority._source_identity(execute, authorization)
            if any(
                value is None
                for value in (selector, evidence, policy, issuer, decision)
            ):
                return None
            authorization.validate_bindings(
                evidence=evidence,
                approval=policy,
                issuer=issuer,
                evaluated_at=now,
            )
            if not (connection_ok and lifecycle_ok and retention_ok and source_ok):
                return None
        except (CanonicalEligibilityError, TypeError, ValueError):
            return None
        policy_active = (
            policy.approval_id == binding.deletion_policy_approval_id
            and policy.policy_version_id == binding.deletion_policy_version_id
            and policy.policy_digest == binding.deletion_policy_digest
            and policy.authority_generation == binding.deletion_policy_generation
            and policy.connection_id == authorization.connection_id
            and policy.source_contract_id == authorization.source_contract_id
            and policy.source_contract_version == authorization.source_contract_version
            and policy.resource_scope_reference == authorization.resource_scope_reference
            and policy.is_valid_at(now)
        )
        retention_active = (
            retention_ok
            and authorization.retention_eligibility_reference == binding.retention_eligibility_reference
            and authorization.retention_eligibility_generation == binding.retention_eligibility_generation
        )
        return DispositionAuthoritySnapshot(
            workspace_id=binding.workspace_id,
            environment_id=binding.environment_id,
            source_object_id=binding.source_object_id,
            current_revision_id=source.current_revision_id,
            current_acceptance_id=source.acceptance_id,
            current_generation=source.cas_version,
            tombstone_authorization_id=authorization.authorization_id,
            authorization_digest=authorization.command_digest,
            authorization_active=decision.disposition is TombstoneAuthorizationDisposition.ISSUED,
            deletion_policy_approval_id=policy.approval_id,
            deletion_policy_version_id=policy.policy_version_id,
            deletion_policy_digest=policy.policy_digest,
            deletion_policy_generation=policy.authority_generation,
            deletion_policy_active=policy_active,
            retention_eligibility_reference=authorization.retention_eligibility_reference,
            retention_eligibility_generation=authorization.retention_eligibility_generation,
            retention_eligibility_active=retention_active,
            issuer_authority_id=issuer.issuer_authority_id,
            issuer_generation=issuer.authority_generation,
            issuer_active=(
                issuer.issuer_authority_id == binding.issuer_authority_id
                and issuer.authority_generation == binding.issuer_generation
                and issuer.is_valid_at(now)
            ),
        )

    async def _permit_enter(
        self,
        operation: DispositionOperation,
        permit: "PostgreSQLStoreEffectPermit",
    ) -> StoreEffectPermitStatus:
        binding = operation.authority_binding
        if binding is None or not _scope_matches(operation, binding):
            return StoreEffectPermitStatus.AUTHORITY_NOT_CURRENT
        context = self._context(operation.workspace_id, operation.environment_id)
        permit.context_manager = self._uow_factory(context)
        try:
            uow = await permit.context_manager.__aenter__()
            permit.uow = uow
            await lock_source_lifecycle_shared(
                uow.execute_statement,
                workspace_id=operation.workspace_id,
                environment_id=operation.environment_id,
                source_object_id=binding.source_object_id,
            )
            current = await self._get_in_uow(
                uow,
                operation.workspace_id,
                operation.environment_id,
                operation.operation_id,
                for_update=True,
            )
            if (
                current is None
                or current.state_version != operation.state_version
                or current.status is DispositionStatus.CANCELLED
            ):
                return StoreEffectPermitStatus.OPERATION_NOT_CURRENT
            snapshot = await self._authority_snapshot(uow, binding)
            if snapshot is None or not disposition_authority_is_current(
                binding, snapshot, evaluated_at=_aware_now(self._clock)
            ):
                return StoreEffectPermitStatus.AUTHORITY_NOT_CURRENT
            return StoreEffectPermitStatus.ALLOWED
        except BaseException as error:
            await permit.context_manager.__aexit__(type(error), error, error.__traceback__)
            permit.context_manager = None
            permit.uow = None
            if isinstance(error, Exception):
                return StoreEffectPermitStatus.UNAVAILABLE
            raise

    async def _permit_exit(
        self,
        permit: "PostgreSQLStoreEffectPermit",
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        if permit.context_manager is not None:
            await permit.context_manager.__aexit__(exc_type, exc, traceback)
            permit.context_manager = None
            permit.uow = None


class PostgreSQLStoreEffectPermit:
    """Hold row/current-authority locks until one external store call finishes."""

    def __init__(self, repository: PostgreSQLDispositionRepository, operation: DispositionOperation) -> None:
        self.repository = repository
        self.operation = operation
        self.context_manager: Any = None
        self.uow: PostgreSQLDispositionUnitOfWork | None = None

    async def __aenter__(self) -> StoreEffectPermitStatus:
        return await self.repository._permit_enter(self.operation, self)

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.repository._permit_exit(self, exc_type, exc, traceback)


def _operation_values(operation: DispositionOperation, now: datetime) -> dict[str, object]:
    return {
        "workspace_id": operation.workspace_id,
        "environment_id": operation.environment_id,
        "operation_id": operation.operation_id,
        "request_digest": operation.request_digest,
        "state_version": operation.state_version,
        "status": operation.status.value,
        "fence_active": operation.fence_active,
        "operation_data": operation.model_dump(mode="json"),
        "completion_ack_expires_at": operation.completion_ack_expires_at,
        "created_at": now,
        "updated_at": now,
    }


def _operation_from_row(row: Any) -> DispositionOperation | None:
    if row is None:
        return None
    data = row["operation_data"]
    if isinstance(data, str):
        data = json.loads(data)
    return DispositionOperation.model_validate(data)


def _model(model: Any, record: Any):
    if record is None:
        return None
    if isinstance(record, str):
        record = json.loads(record)
    return model.model_validate(record)


def _scope_matches(
    operation: DispositionOperation,
    binding: DispositionAuthorityBinding,
) -> bool:
    return (
        operation.workspace_id,
        operation.environment_id,
        operation.operation_id,
    ) == (binding.workspace_id, binding.environment_id, binding.operation_id)


def _fresh_same_scope(
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


def _outbox_event_id(operation: DispositionOperation) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine.source-disposition-outbox.v1:"
        f"{operation.workspace_id}:{operation.environment_id}:{operation.operation_id}",
    )


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("disposition persistence clock must return an aware datetime")
    return now


__all__ = ["PostgreSQLDispositionRepository", "PostgreSQLStoreEffectPermit"]
