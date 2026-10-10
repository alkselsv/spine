"""Deterministic transaction-aware in-memory persistence adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from types import TracebackType
from typing import NoReturn, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from spine.application.persistence.bootstrap import (
    InitialWorkspaceBootstrapAuthority,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistenceScope,
    TrustedPersistenceContext,
    TrustedContextVerifier,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    InvalidBootstrapAuthorityError,
    InvalidPersistenceContextError,
    OutboxConflictError,
    PersistenceError,
    UnexpectedPersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
    idempotency_conflict,
)
from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.outbox import (
    OutboxEventRegistry,
    OutboxIntent,
    validate_outbox_intent_for_context,
)
from spine.domain.workspaces import Environment, Workspace


_ResultT = TypeVar("_ResultT")
_UNEXPECTED_FAILURE_MESSAGE = "Persistence operation failed."


@dataclass(slots=True)
class _StoreState:
    workspaces: dict[UUID, Workspace]
    environments: dict[UUID, Environment]
    idempotency_receipts: dict["_ReceiptKey", "_IdempotencyReceipt"]
    outbox_intents: dict[UUID, OutboxIntent]


@dataclass(frozen=True, slots=True)
class _ReceiptKey:
    workspace_id: UUID
    environment_id: UUID | None
    operation_name: str
    operation_schema_version: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _IdempotencyReceipt:
    receipt_id: UUID
    key: _ReceiptKey
    digest: CommandDigest
    result: OpaqueResultReference | None


class _Store:
    def __init__(self, *, transaction_lock: asyncio.Lock | None = None) -> None:
        self.state = _StoreState(
            workspaces={},
            environments={},
            idempotency_receipts={},
            outbox_intents={},
        )
        self.lock = transaction_lock or asyncio.Lock()
        self.initialized = False
        self.bootstrap_sealed = False

    @property
    def workspaces(self) -> dict[UUID, Workspace]:
        return self.state.workspaces

    @property
    def environments(self) -> dict[UUID, Environment]:
        return self.state.environments

    @property
    def idempotency_receipts(self) -> dict[_ReceiptKey, _IdempotencyReceipt]:
        return self.state.idempotency_receipts

    @property
    def outbox_intents(self) -> dict[UUID, OutboxIntent]:
        return self.state.outbox_intents


def _workspace_copy(value: Workspace) -> Workspace:
    """Rebuild a canonical Workspace without invoking overridable copy hooks."""
    if type(value) is not Workspace:
        raise TypeError("Unsupported Workspace implementation.")
    return Workspace(id=value.id, slug=value.slug, display_name=value.display_name)


def _environment_copy(value: Environment) -> Environment:
    """Rebuild a canonical Environment without invoking overridable copy hooks."""
    if type(value) is not Environment:
        raise TypeError("Unsupported Environment implementation.")
    return Environment(
        id=value.id,
        workspace_id=value.workspace_id,
        kind=value.kind,
        display_name=value.display_name,
    )


class _Lifecycle(Enum):
    NEW = auto()
    ENTERING = auto()
    ACTIVE = auto()
    COMMITTED = auto()
    ROLLED_BACK = auto()
    CLOSED = auto()


class _WorkspaceRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def add(self, workspace: Workspace) -> None:
        self._uow._guard_active()

        def operation() -> None:
            scope = self._uow.scope
            if not isinstance(scope, WorkspaceScope) or workspace.id != scope.workspace_id:
                self._uow._fail(
                    InvalidPersistenceContextError("Persistence context is invalid.")
                )
            if not self._uow._initialized:
                self._uow._fail(
                    InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
                )
            if self._uow._resolve_workspace(workspace.id) is not None:
                self._uow._fail(
                    ConstraintConflictError("Workspace identity already exists.")
                )
            self._uow._pending_workspaces[workspace.id] = _workspace_copy(workspace)

        self._uow._repository_call(operation)

    async def resolve(self, workspace_id: UUID) -> Workspace | None:
        self._uow._guard_active()

        def operation() -> Workspace | None:
            if workspace_id != self._uow.scope.workspace_id:
                return None
            workspace = self._uow._resolve_workspace(workspace_id)
            return _workspace_copy(workspace) if workspace is not None else None

        return self._uow._repository_call(operation)


class _EnvironmentRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def add(self, environment: Environment) -> None:
        self._uow._guard_active()

        def operation() -> None:
            scope = self._uow.scope
            if not isinstance(scope, WorkspaceScope) or environment.workspace_id != scope.workspace_id:
                self._uow._fail(
                    InvalidPersistenceContextError("Persistence context is invalid.")
                )
            if self._uow._resolve_workspace(scope.workspace_id) is None:
                self._uow._fail(ConstraintConflictError("Owning Workspace does not exist."))
            if self._uow._resolve_environment(environment.id) is not None:
                self._uow._fail(
                    ConstraintConflictError("Environment identity already exists.")
                )
            self._uow._pending_environments[environment.id] = _environment_copy(environment)

        self._uow._repository_call(operation)

    async def resolve(self, environment_id: UUID) -> Environment | None:
        self._uow._guard_active()

        def operation() -> Environment | None:
            environment = self._uow._resolve_environment(environment_id)
            if environment is None:
                return None
            scope = self._uow.scope
            if environment.workspace_id != scope.workspace_id:
                return None
            if isinstance(scope, EnvironmentScope) and environment.id != scope.environment_id:
                return None
            return _environment_copy(environment)

        return self._uow._repository_call(operation)


class _IdempotencyRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def claim(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> IdempotencyClaimResult:
        self._uow._guard_active()

        def repository_operation() -> IdempotencyClaimResult:
            operation = self._uow._operation_snapshot()
            if (
                digest.operation != operation
                or digest.operation_schema_version != operation_schema_version
            ):
                self._uow._fail(
                    IdempotencyConflictError(
                        "Idempotency digest does not match the trusted operation."
                    )
                )
            receipt_key = self._uow._receipt_key(
                operation=operation,
                operation_schema_version=operation_schema_version,
                key=key,
            )
            existing = self._uow._resolve_receipt(receipt_key)
            if existing is not None:
                if existing.digest != digest:
                    self._uow._fail(idempotency_conflict())
                if existing.result is None:
                    self._uow._fail(
                        IdempotencyConflictError("Idempotency key is already claimed.")
                    )
                return IdempotencyReplay(
                    kind="replay",
                    receipt_id=existing.receipt_id,
                    operation=operation,
                    operation_schema_version=operation_schema_version,
                    key=key,
                    result=existing.result,
                )
            receipt_id = _receipt_id(receipt_key)
            claim = OwnedIdempotencyClaim(
                kind="owned",
                receipt_id=receipt_id,
                operation=operation,
                operation_schema_version=operation_schema_version,
                key=key,
                digest=digest,
            )
            self._uow._pending_receipts[receipt_key] = _IdempotencyReceipt(
                receipt_id=receipt_id,
                key=receipt_key,
                digest=digest,
                result=None,
            )
            return claim

        return self._uow._repository_call(repository_operation)

    async def complete(
        self,
        claim: OwnedIdempotencyClaim,
        result: OpaqueResultReference,
    ) -> None:
        self._uow._guard_active()

        def repository_operation() -> None:
            if not isinstance(claim, OwnedIdempotencyClaim):
                self._uow._fail(
                    IdempotencyConflictError("Only an owned idempotency claim can complete.")
                )
            if claim.operation != self._uow._operation_snapshot():
                self._uow._fail(
                    IdempotencyConflictError("Idempotency claim cannot be completed.")
                )
            if (
                claim.digest.operation != claim.operation
                or claim.digest.operation_schema_version != claim.operation_schema_version
            ):
                self._uow._fail(
                    IdempotencyConflictError("Idempotency claim cannot be completed.")
                )
            receipt_key = self._uow._receipt_key(
                operation=claim.operation,
                operation_schema_version=claim.operation_schema_version,
                key=claim.key,
            )
            pending = self._uow._pending_receipts.get(receipt_key)
            if (
                pending is None
                or pending.receipt_id != claim.receipt_id
                or pending.digest != claim.digest
                or pending.result is not None
            ):
                self._uow._fail(
                    IdempotencyConflictError("Idempotency claim cannot be completed.")
                )
            self._uow._pending_receipts[receipt_key] = _IdempotencyReceipt(
                receipt_id=pending.receipt_id,
                key=pending.key,
                digest=pending.digest,
                result=result,
            )

        self._uow._repository_call(repository_operation)


class _OutboxWriter:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def append(self, intent: OutboxIntent) -> UUID:
        self._uow._guard_active()

        def repository_operation() -> UUID:
            canonical = validate_outbox_intent_for_context(
                intent,
                registry=self._uow._outbox_events,
                scope=self._uow.scope,
                trace_id=self._uow._trace_id_snapshot(),
            )
            event_id = canonical.event_id or self._uow._event_id_factory()
            if self._uow._resolve_outbox_intent(event_id) is not None:
                self._uow._fail(
                    OutboxConflictError("Outbox event identity already exists.")
                )
            if self._uow._has_producer_identity(canonical):
                self._uow._fail(
                    OutboxConflictError(
                        "Outbox producer identity already exists."
                    )
                )
            copied = canonical.model_copy(deep=True, update={"event_id": event_id})
            self._uow._pending_outbox_intents[event_id] = copied
            return event_id

        return self._uow._repository_call(repository_operation)


class InMemoryUnitOfWork:
    def __init__(
        self,
        store: _Store,
        source_context: TrustedPersistenceContext,
        context_snapshot: TrustedPersistenceContext,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        event_id_factory: Callable[[], UUID],
    ) -> None:
        if not isinstance(source_context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        self._store = store
        self._source_context = source_context
        self._context_snapshot = context_snapshot
        self._context_verifier = context_verifier
        self._outbox_events = outbox_events
        self._event_id_factory = event_id_factory
        self._scope: PersistenceScope | None = None
        self._operation: PersistenceOperation | None = None
        self._trace_id: UUID | None = None
        self._initialized = False
        self._lifecycle = _Lifecycle.NEW
        self._owner: asyncio.Task[object] | None = None
        self._base_workspaces: dict[UUID, Workspace] = {}
        self._base_environments: dict[UUID, Environment] = {}
        self._base_receipts: dict[_ReceiptKey, _IdempotencyReceipt] = {}
        self._base_outbox_intents: dict[UUID, OutboxIntent] = {}
        self._pending_workspaces: dict[UUID, Workspace] = {}
        self._pending_environments: dict[UUID, Environment] = {}
        self._pending_receipts: dict[_ReceiptKey, _IdempotencyReceipt] = {}
        self._pending_outbox_intents: dict[UUID, OutboxIntent] = {}
        self._workspaces = _WorkspaceRepository(self)
        self._environments = _EnvironmentRepository(self)
        self._idempotency = _IdempotencyRepository(self)
        self._outbox = _OutboxWriter(self)

    @property
    def workspaces(self) -> _WorkspaceRepository:
        self._guard_active()
        return self._workspaces

    @property
    def environments(self) -> _EnvironmentRepository:
        self._guard_active()
        return self._environments

    @property
    def idempotency(self) -> _IdempotencyRepository:
        self._guard_active()
        return self._idempotency

    @property
    def outbox(self) -> _OutboxWriter:
        self._guard_active()
        return self._outbox

    def _operation_snapshot(self) -> PersistenceOperation:
        self._guard_active()
        if self._operation is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._operation

    def _trace_id_snapshot(self) -> UUID:
        self._guard_active()
        if self._trace_id is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._trace_id

    @property
    def scope(self) -> PersistenceScope:
        self._guard_active()
        if self._scope is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._scope

    async def __aenter__(self) -> "InMemoryUnitOfWork":
        if self._lifecycle is not _Lifecycle.NEW:
            raise UnitOfWorkLifecycleError("Unit of Work cannot be entered more than once.")
        self._lifecycle = _Lifecycle.ENTERING
        self._owner = asyncio.current_task()
        try:
            async with self._store.lock:
                context_snapshot = self._context_verifier.verify(self._source_context)
                if context_snapshot != self._context_snapshot:
                    raise InvalidPersistenceContextError(
                        "Persistence context is invalid."
                    )
                self._base_workspaces = {
                    key: _workspace_copy(value) for key, value in self._store.workspaces.items()
                }
                self._base_environments = {
                    key: _environment_copy(value)
                    for key, value in self._store.environments.items()
                }
                self._base_receipts = {
                    key: _receipt_copy(value)
                    for key, value in self._store.idempotency_receipts.items()
                }
                self._base_outbox_intents = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.outbox_intents.items()
                }
                self._initialized = self._store.initialized
                if not self._initialized:
                    raise InvalidBootstrapAuthorityError(
                        "Initial bootstrap is not authorized."
                    )
                self._validate_context(context_snapshot)
        except BaseException as error:
            self._raise_terminal(error)
        self._lifecycle = _Lifecycle.ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._lifecycle is _Lifecycle.CLOSED:
            return
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ACTIVE:
            await self.rollback()
        self._lifecycle = _Lifecycle.CLOSED
        self._clear_transaction()

    async def commit(self) -> None:
        self._guard_active()
        try:
            async with self._store.lock:
                self._validate_commit()
                prepared_workspaces = {
                    key: _workspace_copy(value)
                    for key, value in self._store.workspaces.items()
                }
                prepared_workspaces.update(
                    {
                        key: _workspace_copy(value)
                        for key, value in self._pending_workspaces.items()
                    }
                )
                prepared_environments = {
                    key: _environment_copy(value)
                    for key, value in self._store.environments.items()
                }
                prepared_environments.update(
                    {
                        key: _environment_copy(value)
                        for key, value in self._pending_environments.items()
                    }
                )
                prepared_receipts = {
                    key: _receipt_copy(value)
                    for key, value in self._store.idempotency_receipts.items()
                }
                prepared_receipts.update(
                    {
                        key: _receipt_copy(value)
                        for key, value in self._pending_receipts.items()
                    }
                )
                prepared_outbox_intents = {
                    key: value.model_copy(deep=True)
                    for key, value in self._store.outbox_intents.items()
                }
                prepared_outbox_intents.update(
                    {
                        key: value.model_copy(deep=True)
                        for key, value in self._pending_outbox_intents.items()
                    }
                )
                self._store.state = _StoreState(
                    workspaces=prepared_workspaces,
                    environments=prepared_environments,
                    idempotency_receipts=prepared_receipts,
                    outbox_intents=prepared_outbox_intents,
                )
        except BaseException as error:
            self._raise_terminal(error)
        self._lifecycle = _Lifecycle.COMMITTED
        self._clear_transaction()

    async def rollback(self) -> None:
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ROLLED_BACK:
            return
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        self._pending_workspaces.clear()
        self._pending_environments.clear()
        self._pending_receipts.clear()
        self._pending_outbox_intents.clear()
        self._lifecycle = _Lifecycle.ROLLED_BACK

    def _guard_owner(self) -> None:
        if self._owner is None or asyncio.current_task() is not self._owner:
            raise UnitOfWorkLifecycleError("Unit of Work may only be used by its owning task.")

    def _guard_active(self) -> None:
        self._guard_owner()
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")

    def _repository_call(self, operation: Callable[[], _ResultT]) -> _ResultT:
        try:
            return operation()
        except BaseException as error:
            self._raise_terminal(error)

    def _raise_terminal(self, error: BaseException) -> NoReturn:
        self._lifecycle = _Lifecycle.CLOSED
        self._clear_transaction()
        if isinstance(error, asyncio.CancelledError):
            raise error
        if isinstance(error, PersistenceError):
            raise error
        if isinstance(error, Exception):
            raise UnexpectedPersistenceError(_UNEXPECTED_FAILURE_MESSAGE) from None
        raise error

    def _validate_context(self, context_snapshot: TrustedPersistenceContext) -> None:
        scope = context_snapshot.scope
        if isinstance(scope, WorkspaceScope):
            self._scope = WorkspaceScope(workspace_id=scope.workspace_id)
            self._operation = PersistenceOperation(context_snapshot.operation.value)
            self._trace_id = context_snapshot.trace_id
            return
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        environment = self._base_environments.get(scope.environment_id)
        if environment is None or environment.workspace_id != scope.workspace_id:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        self._scope = EnvironmentScope(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        self._operation = PersistenceOperation(context_snapshot.operation.value)
        self._trace_id = context_snapshot.trace_id

    def _validate_commit(self) -> None:
        if self._pending_workspaces and not self._store.initialized:
            raise InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
        if self._pending_workspaces.keys() & self._store.workspaces.keys():
            raise ConstraintConflictError("Workspace identity already exists.")
        if self._pending_environments.keys() & self._store.environments.keys():
            raise ConstraintConflictError("Environment identity already exists.")
        if self._pending_receipts.keys() & self._store.idempotency_receipts.keys():
            raise IdempotencyConflictError("Idempotency key conflicts with existing command.")
        if self._pending_outbox_intents.keys() & self._store.outbox_intents.keys():
            raise OutboxConflictError("Outbox event identity already exists.")
        for intent in self._pending_outbox_intents.values():
            if self._store_has_producer_identity(intent):
                raise OutboxConflictError("Outbox producer identity already exists.")
        incomplete = [
            receipt for receipt in self._pending_receipts.values() if receipt.result is None
        ]
        if incomplete:
            raise UnitOfWorkLifecycleError("Owned idempotency claims must be completed before commit.")
        if any(
            receipt.key.operation_name != receipt.digest.operation.value
            or receipt.key.operation_schema_version != receipt.digest.operation_schema_version
            for receipt in self._pending_receipts.values()
        ):
            raise IdempotencyConflictError(
                "Idempotency digest does not match the trusted operation."
            )
        available_workspaces = self._store.workspaces.keys() | self._pending_workspaces.keys()
        if any(
            intent.workspace_id not in available_workspaces
            for intent in self._pending_outbox_intents.values()
        ):
            raise ConstraintConflictError("Owning Workspace does not exist.")
        if any(
            environment.workspace_id not in available_workspaces
            for environment in self._pending_environments.values()
        ):
            raise ConstraintConflictError("Owning Workspace does not exist.")

    def _resolve_workspace(self, workspace_id: UUID) -> Workspace | None:
        return self._pending_workspaces.get(workspace_id) or self._base_workspaces.get(workspace_id)

    def _resolve_environment(self, environment_id: UUID) -> Environment | None:
        return self._pending_environments.get(environment_id) or self._base_environments.get(
            environment_id
        )

    def _resolve_receipt(self, key: _ReceiptKey) -> _IdempotencyReceipt | None:
        return self._pending_receipts.get(key) or self._base_receipts.get(key)

    def _resolve_outbox_intent(self, event_id: UUID) -> OutboxIntent | None:
        return self._pending_outbox_intents.get(event_id) or self._base_outbox_intents.get(
            event_id
        )

    @staticmethod
    def _same_producer_identity(left: OutboxIntent, right: OutboxIntent) -> bool:
        return (
            left.producer_deduplication_id is not None
            and left.workspace_id == right.workspace_id
            and left.environment_id == right.environment_id
            and left.event_type == right.event_type
            and left.producer_deduplication_id == right.producer_deduplication_id
        )

    def _has_producer_identity(self, intent: OutboxIntent) -> bool:
        return any(
            self._same_producer_identity(existing, intent)
            for existing in (
                *self._base_outbox_intents.values(),
                *self._pending_outbox_intents.values(),
            )
        )

    def _store_has_producer_identity(self, intent: OutboxIntent) -> bool:
        return any(
            self._same_producer_identity(existing, intent)
            for existing in self._store.outbox_intents.values()
        )

    def _receipt_key(
        self,
        *,
        operation: PersistenceOperation,
        operation_schema_version: int,
        key: IdempotencyKey,
    ) -> _ReceiptKey:
        scope = self.scope
        environment_id = scope.environment_id if isinstance(scope, EnvironmentScope) else None
        return _ReceiptKey(
            workspace_id=scope.workspace_id,
            environment_id=environment_id,
            operation_name=operation.value,
            operation_schema_version=operation_schema_version,
            idempotency_key=key.value,
        )

    def _clear_transaction(self) -> None:
        self._base_workspaces.clear()
        self._base_environments.clear()
        self._base_receipts.clear()
        self._base_outbox_intents.clear()
        self._pending_workspaces.clear()
        self._pending_environments.clear()
        self._pending_receipts.clear()
        self._pending_outbox_intents.clear()
        self._scope = None
        self._operation = None
        self._trace_id = None

    def _fail(self, error: Exception) -> NoReturn:
        self._raise_terminal(error)


def _receipt_id(key: _ReceiptKey) -> UUID:
    environment_id = str(key.environment_id) if key.environment_id is not None else "-"
    return uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "spine-idempotency-receipt-v1",
                str(key.workspace_id),
                environment_id,
                key.operation_name,
                str(key.operation_schema_version),
                key.idempotency_key,
            )
        ),
    )


def _receipt_copy(value: _IdempotencyReceipt) -> _IdempotencyReceipt:
    if type(value) is not _IdempotencyReceipt:
        raise TypeError("Unsupported idempotency receipt implementation.")
    return _IdempotencyReceipt(
        receipt_id=value.receipt_id,
        key=value.key,
        digest=value.digest,
        result=value.result,
    )


class _InMemoryUnitOfWorkFactory:
    def __init__(
        self,
        store: _Store,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        event_id_factory: Callable[[], UUID],
    ) -> None:
        self._store = store
        self._context_verifier = context_verifier
        self._outbox_events = outbox_events
        self._event_id_factory = event_id_factory

    def __call__(self, context: TrustedPersistenceContext) -> InMemoryUnitOfWork:
        if not isinstance(context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        context_snapshot = self._context_verifier.verify(context)
        return InMemoryUnitOfWork(
            self._store,
            context,
            context_snapshot,
            self._context_verifier,
            self._outbox_events,
            self._event_id_factory,
        )


class _InMemoryInitialWorkspaceBootstrap:
    def __init__(
        self,
        store: _Store,
        expected_authority: InitialWorkspaceBootstrapAuthority | None,
    ) -> None:
        self._store = store
        self._expected_authority = expected_authority

    async def create_initial_workspace(
        self,
        authority: InitialWorkspaceBootstrapAuthority,
        workspace: Workspace,
    ) -> None:
        if self._expected_authority is None or authority is not self._expected_authority:
            raise InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
        async with self._store.lock:
            if self._store.bootstrap_sealed or self._store.workspaces:
                raise ConstraintConflictError("Initial Workspace bootstrap is sealed.")
            try:
                copied_workspace = _workspace_copy(workspace)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise UnexpectedPersistenceError(_UNEXPECTED_FAILURE_MESSAGE) from None
            self._store.workspaces[workspace.id] = copied_workspace
            self._store.initialized = True
            self._store.bootstrap_sealed = True


class InMemoryPersistence:
    """Process-local deterministic adapter and its explicitly scoped entry points."""

    def __init__(
        self,
        *,
        context_verifier: TrustedContextVerifier,
        outbox_events: OutboxEventRegistry,
        bootstrap_authority: InitialWorkspaceBootstrapAuthority | None = None,
        transaction_lock: asyncio.Lock | None = None,
        event_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        store = _Store(transaction_lock=transaction_lock)
        self.uow_factory = _InMemoryUnitOfWorkFactory(
            store,
            context_verifier,
            outbox_events,
            event_id_factory,
        )
        self.initial_workspace_bootstrap = _InMemoryInitialWorkspaceBootstrap(
            store, bootstrap_authority
        )
