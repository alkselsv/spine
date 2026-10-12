"""Trusted orchestration for immutable, idempotent original uploads."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable
from uuid import UUID

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    InvalidPersistenceContextError,
    PersistenceError,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.infrastructure.object_storage.contracts import (
    Issue8ResultReference,
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    OriginalUploadCommandPayload,
    OriginalUploadCommandResult,
    PreWriteRequestDigest,
    UploadCommandState,
    UploadCommandStateName,
    WriteReceipt,
)
from spine.infrastructure.object_storage.errors import (
    IntegrityMismatch,
    ObjectProtected,
    ReconciliationRequired,
    StorageError,
    StorageErrorCategory,
    UploadConflict,
    UploadNotFound,
)
from spine.infrastructure.object_storage.port import OriginalObjectStore

from .contracts import (
    OriginalUploadAbortOutcome,
    OriginalUploadOutcome,
    OriginalUploadRecord,
    OriginalUploadReconciliationOutcome,
    OriginalUploadUnitOfWork,
    OriginalUploadUnitOfWorkFactory,
    UploadCleanupStatus,
)
from .diagnostics import (
    NullStorageOutcomeRecorder,
    StorageOutcome,
    StorageOutcomeKind,
    StorageOutcomeRecorder,
    StorageOutcomeStatus,
    record_storage_outcome_safely,
)


_RESULT_TYPE = "original_upload"
_RESULT_SCHEMA_VERSION = 1
_RECONCILIATION_CATEGORIES = frozenset(
    {
        StorageErrorCategory.RECONCILIATION_REQUIRED,
        StorageErrorCategory.STORAGE_INTEGRITY_INDETERMINATE,
        StorageErrorCategory.RECOVERY_EVIDENCE_CHAIN_CONFLICT,
        StorageErrorCategory.RECOVERY_EVIDENCE_INDETERMINATE,
    }
)


class OriginalUploadOrchestrator:
    """Coordinate persistence and physical storage without conflating authority."""

    def __init__(
        self,
        *,
        uow_factory: OriginalUploadUnitOfWorkFactory,
        object_store: OriginalObjectStore,
        identity_factory: Callable[[], UUID],
        outcome_recorder: StorageOutcomeRecorder | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._object_store = object_store
        self._identity_factory = identity_factory
        self._outcome_recorder = outcome_recorder or NullStorageOutcomeRecorder()

    async def upload(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
        chunks: AsyncIterable[bytes],
    ) -> OriginalUploadOutcome:
        """Finalize one upload or verify an exact same-key byte replay."""

        self._validate_context_binding(context, command)
        record, replay = await self._resolve_or_create(context, command)
        self._raise_if_upload_blocked(record)

        try:
            storage_state = await self._object_store.begin_or_resume_upload(
                record.command,
                record.state,
            )
        except StorageError as error:
            await self._record_storage_failure(context, record, error)
            raise

        if record.state.state is UploadCommandStateName.FINALIZED:
            replayed = await self._replay_finalized(
                record,
                chunks,
                storage_state=storage_state,
            )
            await self._record_outcome(
                context,
                operation=StorageOutcomeKind.UPLOAD,
                status=StorageOutcomeStatus.SUCCEEDED,
                subject_id=record.result.upload_id,
                suffix="replay",
            )
            return replayed
        if storage_state.state is UploadCommandStateName.FINALIZED:
            await self._mark_reconciliation_required(context, record.result.upload_id)
            raise ReconciliationRequired()
        if storage_state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            await self._mark_reconciliation_required(context, record.result.upload_id)
            raise ReconciliationRequired()
        if storage_state.state is UploadCommandStateName.ABORTED:
            await self._replace_state(
                context,
                record.result.upload_id,
                target=UploadCommandStateName.ABORTED,
                cleanup_status=UploadCleanupStatus.PENDING,
            )
            raise ObjectProtected()
        if storage_state.state is UploadCommandStateName.INTEGRITY_CONFLICT:
            await self._replace_state(
                context,
                record.result.upload_id,
                target=UploadCommandStateName.INTEGRITY_CONFLICT,
            )
            raise IntegrityMismatch()
        if storage_state.state is not record.state.state:
            record = await self._replace_state(
                context,
                record.result.upload_id,
                target=storage_state.state,
            )

        observed = record.observed_content
        if observed is None:
            try:
                observed = await self._object_store.write_chunks(
                    record.result.upload_id,
                    chunks,
                )
            except asyncio.CancelledError:
                await self._mark_interrupted(context, record.result.upload_id)
                raise
            except StorageError as error:
                await self._record_storage_failure(
                    context, record, error, mark_interrupted=True
                )
                raise
            try:
                record = await self._store_observed_content(
                    context,
                    record.result.upload_id,
                    observed,
                )
            except PersistenceError:
                await self._mark_reconciliation_required(
                    context, record.result.upload_id
                )
                raise ReconciliationRequired() from None

        try:
            receipt = await self._object_store.finalize_upload(
                record.state,
                observed,
            )
        except StorageError as error:
            await self._record_storage_failure(context, record, error)
            raise

        try:
            finalized = await self._finalize_record(
                context,
                record.result.upload_id,
                receipt,
            )
        except PersistenceError:
            await self._mark_reconciliation_required(context, record.result.upload_id)
            raise ReconciliationRequired() from None
        durable_receipt = finalized.state.provisional_receipt
        assert durable_receipt is not None
        await self._record_outcome(
            context,
            operation=StorageOutcomeKind.UPLOAD,
            status=StorageOutcomeStatus.SUCCEEDED,
            subject_id=finalized.result.upload_id,
            suffix="replay" if replay else "accepted",
        )
        return OriginalUploadOutcome(
            result=finalized.result,
            write_receipt=durable_receipt,
            replay=replay,
        )

    async def abort(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
    ) -> OriginalUploadAbortOutcome:
        """Persist terminal abort; physical cleanup is a separate operation."""

        self._validate_context_binding(context, command)
        record, replay = await self._resolve_or_create(context, command)
        if record.state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        if record.state.state is UploadCommandStateName.ABORTED:
            await self._record_outcome(
                context,
                operation=StorageOutcomeKind.UPLOAD,
                status=StorageOutcomeStatus.ABORTED,
                subject_id=record.result.upload_id,
                suffix="aborted-replay",
            )
            return self._abort_outcome(record, replay=True)
        if record.state.state in {
            UploadCommandStateName.FINALIZED,
            UploadCommandStateName.INTEGRITY_CONFLICT,
        }:
            raise ObjectProtected()
        try:
            physical_state = await self._object_store.begin_or_resume_upload(
                record.command,
                record.state,
            )
        except StorageError as error:
            await self._record_storage_failure(context, record, error)
            raise
        if physical_state.state is UploadCommandStateName.FINALIZED:
            await self._mark_reconciliation_required(context, record.result.upload_id)
            raise ReconciliationRequired()
        if physical_state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            await self._mark_reconciliation_required(context, record.result.upload_id)
            raise ReconciliationRequired()
        if physical_state.state is UploadCommandStateName.INTEGRITY_CONFLICT:
            await self._replace_state(
                context,
                record.result.upload_id,
                target=UploadCommandStateName.INTEGRITY_CONFLICT,
            )
            raise ObjectProtected()
        if record.state.state is not UploadCommandStateName.ABORTED:
            record = await self._replace_state(
                context,
                record.result.upload_id,
                target=UploadCommandStateName.ABORTED,
                cleanup_status=UploadCleanupStatus.PENDING,
            )
        await self._record_outcome(
            context,
            operation=StorageOutcomeKind.UPLOAD,
            status=StorageOutcomeStatus.ABORTED,
            subject_id=record.result.upload_id,
            suffix="aborted",
        )
        return self._abort_outcome(record, replay=replay)

    async def cleanup_aborted(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
    ) -> OriginalUploadAbortOutcome:
        """Best-effort physical cleanup that cannot reopen logical abort."""

        self._validate_context_binding(context, command)
        record, replay = await self._resolve_or_create(
            context, command, create=False
        )
        if record.state.state is not UploadCommandStateName.ABORTED:
            raise ObjectProtected()
        if record.cleanup_status is UploadCleanupStatus.COMPLETED:
            await self._record_outcome(
                context,
                operation=StorageOutcomeKind.DELETION,
                status=StorageOutcomeStatus.SUCCEEDED,
                subject_id=record.result.object_id,
                suffix="completed-replay",
            )
            return self._abort_outcome(record, replay=True)

        cleanup_state = UploadCommandState.model_validate(
            record.state.model_dump()
            | {
                "state": UploadCommandStateName.INTERRUPTED,
                "provisional_receipt": None,
            }
        )
        error_category: StorageErrorCategory | None = None
        try:
            await self._object_store.abort_upload(cleanup_state)
            cleanup_status = UploadCleanupStatus.COMPLETED
        except UploadNotFound:
            cleanup_status = UploadCleanupStatus.COMPLETED
        except StorageError as error:
            cleanup_status = UploadCleanupStatus.FAILED
            error_category = error.category

        record = await self._replace_cleanup_status(
            context,
            record.result.upload_id,
            cleanup_status,
            error_category=error_category,
        )
        await self._record_outcome(
            context,
            operation=StorageOutcomeKind.DELETION,
            status=(
                StorageOutcomeStatus.SUCCEEDED
                if cleanup_status is UploadCleanupStatus.COMPLETED
                else StorageOutcomeStatus.UNAVAILABLE
            ),
            subject_id=record.result.object_id,
            error_category=(error_category.value if error_category else None),
            suffix=cleanup_status.value,
            attempt_number=record.record_version,
        )
        return OriginalUploadAbortOutcome(
            result=record.result,
            state=record.state.state,
            cleanup_status=record.cleanup_status,
            cleanup_error_category=record.cleanup_error_category,
            replay=replay,
        )

    async def reconcile(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
    ) -> OriginalUploadReconciliationOutcome:
        """Resolve an indeterminate upload only from durable adapter evidence."""

        self._validate_context_binding(context, command)
        record, _ = await self._resolve_or_create(context, command, create=False)
        if record.state.state is not UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()

        try:
            physical_state = await self._object_store.reconcile_upload(
                record.command,
                record.state,
            )
        except ReconciliationRequired:
            await self._record_outcome(
                context,
                operation=StorageOutcomeKind.RECONCILIATION,
                status=StorageOutcomeStatus.INDETERMINATE,
                subject_id=record.result.upload_id,
                suffix="indeterminate",
            )
            return OriginalUploadReconciliationOutcome(
                result=record.result,
                state=record.state.state,
                resolved=False,
                changed=False,
            )

        receipt = physical_state.provisional_receipt
        if physical_state.state is UploadCommandStateName.FINALIZED:
            if receipt is None:
                raise ReconciliationRequired()
            updated = await self._replace_reconciliation_state(
                context,
                record.result.upload_id,
                physical_state,
                observed_content=receipt.observed_content,
            )
        else:
            updated = await self._replace_reconciliation_state(
                context,
                record.result.upload_id,
                physical_state,
                cleanup_status=(
                    UploadCleanupStatus.PENDING
                    if physical_state.state is UploadCommandStateName.ABORTED
                    else UploadCleanupStatus.NOT_APPLICABLE
                ),
            )
        await self._record_outcome(
            context,
            operation=StorageOutcomeKind.RECONCILIATION,
            status=(
                StorageOutcomeStatus.SUCCEEDED
                if updated.state.state is UploadCommandStateName.FINALIZED
                else StorageOutcomeStatus.ABORTED
            ),
            subject_id=updated.result.upload_id,
            suffix=updated.state.state.value,
        )
        return OriginalUploadReconciliationOutcome(
            result=updated.result,
            state=updated.state.state,
            write_receipt=updated.state.provisional_receipt,
            resolved=True,
            changed=updated != record,
        )

    async def _resolve_or_create(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
        *,
        create: bool = True,
    ) -> tuple[OriginalUploadRecord, bool]:
        digest = self._request_digest(command)
        key = IdempotencyKey(command.idempotency_key)
        async with self._uow_factory(context) as uow:
            claim = await uow.idempotency.claim(
                operation_schema_version=command.operation_schema_version,
                key=key,
                digest=digest,
            )
            if isinstance(claim, IdempotencyReplay):
                self._validate_result_reference(claim.result)
                record = await uow.original_uploads.resolve(claim.result.result_id)
                if record is None:
                    raise IdempotencyConflictError(
                        "Idempotency result cannot be resolved."
                    )
                await uow.commit()
                return record, True

            assert isinstance(claim, OwnedIdempotencyClaim)
            if not create:
                raise IdempotencyConflictError(
                    "Upload cleanup requires an existing aborted command."
                )
            upload_id = self._new_identity()
            object_id = self._new_identity()
            request_digest = PreWriteRequestDigest(
                algorithm="sha256",
                algorithm_version=digest.algorithm_version,
                digest_hex=digest.value,
            )
            result = OriginalUploadCommandResult(
                request_digest=request_digest,
                upload_id=upload_id,
                object_id=object_id,
                issue8_result_reference=Issue8ResultReference(
                    receipt_id=claim.receipt_id
                ),
            )
            state = UploadCommandState(
                issue8_receipt_id=claim.receipt_id,
                request_digest=request_digest,
                upload_id=upload_id,
                object_id=object_id,
                state=UploadCommandStateName.STAGING,
            )
            record = OriginalUploadRecord(
                record_version=1,
                command=command,
                result=result,
                state=state,
            )
            await uow.original_uploads.add(record)
            await uow.idempotency.complete(
                claim,
                OpaqueResultReference(
                    result_type=_RESULT_TYPE,
                    result_id=upload_id,
                    schema_version=_RESULT_SCHEMA_VERSION,
                ),
            )
            await uow.commit()
            return record, False

    async def _replay_finalized(
        self,
        record: OriginalUploadRecord,
        chunks: AsyncIterable[bytes],
        *,
        storage_state: UploadCommandState,
    ) -> OriginalUploadOutcome:
        if storage_state.state is not UploadCommandStateName.FINALIZED:
            raise ReconciliationRequired()
        observed = await self._object_store.write_chunks(record.result.upload_id, chunks)
        if observed != record.observed_content:
            raise UploadConflict()
        receipt = await self._object_store.finalize_upload(storage_state, observed)
        if receipt != record.state.provisional_receipt:
            raise UploadConflict()
        return OriginalUploadOutcome(
            result=record.result,
            write_receipt=receipt,
            replay=True,
        )

    async def _store_observed_content(
        self,
        context: TrustedPersistenceContext,
        upload_id: UUID,
        observed: ObservedContentIdentity,
    ) -> OriginalUploadRecord:
        async with self._uow_factory(context) as uow:
            current = await self._resolve_record(uow, upload_id)
            if current.observed_content is not None:
                if current.observed_content != observed:
                    raise UploadConflict()
                await uow.commit()
                return current
            updated = self._updated_record(current, observed_content=observed)
            await uow.original_uploads.replace(
                updated,
                expected_version=current.record_version,
            )
            await uow.commit()
            return updated

    async def _finalize_record(
        self,
        context: TrustedPersistenceContext,
        upload_id: UUID,
        receipt: WriteReceipt,
    ) -> OriginalUploadRecord:
        async with self._uow_factory(context) as uow:
            current = await self._resolve_record(uow, upload_id)
            if current.state.state is UploadCommandStateName.FINALIZED:
                if current.state.provisional_receipt != receipt:
                    raise UploadConflict()
                await uow.commit()
                return current
            finalized_state = current.state.transitioned(
                UploadCommandStateName.FINALIZED,
                provisional_receipt=receipt,
            )
            updated = self._updated_record(current, state=finalized_state)
            await uow.original_uploads.replace(
                updated,
                expected_version=current.record_version,
            )
            await uow.commit()
            return updated

    async def _replace_state(
        self,
        context: TrustedPersistenceContext,
        upload_id: UUID,
        *,
        target: UploadCommandStateName,
        cleanup_status: UploadCleanupStatus = UploadCleanupStatus.NOT_APPLICABLE,
    ) -> OriginalUploadRecord:
        async with self._uow_factory(context) as uow:
            current = await self._resolve_record(uow, upload_id)
            if current.state.state is target:
                await uow.commit()
                return current
            state = current.state.transitioned(target)
            updated = self._updated_record(
                current,
                state=state,
                cleanup_status=cleanup_status,
            )
            await uow.original_uploads.replace(
                updated,
                expected_version=current.record_version,
            )
            await uow.commit()
            return updated

    async def _replace_cleanup_status(
        self,
        context: TrustedPersistenceContext,
        upload_id: UUID,
        status: UploadCleanupStatus,
        *,
        error_category: StorageErrorCategory | None,
    ) -> OriginalUploadRecord:
        async with self._uow_factory(context) as uow:
            current = await self._resolve_record(uow, upload_id)
            if current.state.state is not UploadCommandStateName.ABORTED:
                raise ObjectProtected()
            if current.cleanup_status is status:
                await uow.commit()
                return current
            updated = self._updated_record(
                current,
                cleanup_status=status,
                cleanup_error_category=error_category,
            )
            await uow.original_uploads.replace(
                updated,
                expected_version=current.record_version,
            )
            await uow.commit()
        return updated

    async def _replace_reconciliation_state(
        self,
        context: TrustedPersistenceContext,
        upload_id: UUID,
        state: UploadCommandState,
        *,
        observed_content: ObservedContentIdentity | None = None,
        cleanup_status: UploadCleanupStatus = UploadCleanupStatus.NOT_APPLICABLE,
    ) -> OriginalUploadRecord:
        async with self._uow_factory(context) as uow:
            current = await self._resolve_record(uow, upload_id)
            if current.state.state is not UploadCommandStateName.RECONCILIATION_REQUIRED:
                raise ReconciliationRequired()
            if observed_content is None:
                observed_content = current.observed_content
            updated = self._updated_record(
                current,
                state=state,
                observed_content=observed_content,
                cleanup_status=cleanup_status,
            )
            await uow.original_uploads.replace(
                updated,
                expected_version=current.record_version,
            )
            await uow.commit()
            return updated

    async def _mark_interrupted(
        self,
        context: TrustedPersistenceContext,
        upload_id: UUID,
    ) -> None:
        try:
            await self._replace_state(
                context,
                upload_id,
                target=UploadCommandStateName.INTERRUPTED,
            )
        except PersistenceError:
            return

    async def _mark_reconciliation_required(
        self,
        context: TrustedPersistenceContext,
        upload_id: UUID,
    ) -> None:
        try:
            await self._replace_state(
                context,
                upload_id,
                target=UploadCommandStateName.RECONCILIATION_REQUIRED,
            )
        except PersistenceError:
            return

    async def _record_storage_failure(
        self,
        context: TrustedPersistenceContext,
        record: OriginalUploadRecord,
        error: StorageError,
        *,
        mark_interrupted: bool = False,
    ) -> None:
        await self._record_outcome(
            context,
            operation=(
                StorageOutcomeKind.INTEGRITY
                if error.category is StorageErrorCategory.INTEGRITY_MISMATCH
                else StorageOutcomeKind.RECONCILIATION
                if error.category in _RECONCILIATION_CATEGORIES
                else StorageOutcomeKind.RETRY
            ),
            status=(
                StorageOutcomeStatus.RETRY_SCHEDULED
                if error.retryable
                else StorageOutcomeStatus.INDETERMINATE
            ),
            subject_id=record.result.upload_id,
            error_category=error.category.value,
            suffix=error.category.value,
            attempt_number=record.record_version,
        )
        if record.state.state in {
            UploadCommandStateName.FINALIZED,
            UploadCommandStateName.ABORTED,
            UploadCommandStateName.INTEGRITY_CONFLICT,
        }:
            return
        if error.category in _RECONCILIATION_CATEGORIES:
            await self._mark_reconciliation_required(context, record.result.upload_id)
            return
        if error.category is StorageErrorCategory.INTEGRITY_MISMATCH:
            await self._replace_state(
                context,
                record.result.upload_id,
                target=UploadCommandStateName.INTEGRITY_CONFLICT,
            )
            return
        if error.retryable:
            if mark_interrupted and record.observed_content is None:
                await self._mark_interrupted(context, record.result.upload_id)
            return
        if record.state.state is not UploadCommandStateName.RECONCILIATION_REQUIRED:
            await self._mark_reconciliation_required(context, record.result.upload_id)

    async def _record_outcome(
        self,
        context: TrustedPersistenceContext,
        *,
        operation: StorageOutcomeKind,
        status: StorageOutcomeStatus,
        subject_id: UUID,
        suffix: str,
        error_category: str | None = None,
        attempt_number: int = 1,
    ) -> None:
        outcome = StorageOutcome(
            workspace_id=context.scope.workspace_id,
            environment_id=(
                context.scope.environment_id
                if isinstance(context.scope, EnvironmentScope)
                else None
            ),
            trace_id=context.trace_id,
            operation=operation,
            status=status,
            attempt_number=attempt_number,
            subject_id=subject_id,
            error_code=error_category,
            producer_deduplication_id=(
                f"storage:{operation.value}:{subject_id}:{suffix}:attempt-{attempt_number}"
            ),
        )
        await record_storage_outcome_safely(self._outcome_recorder, outcome)

    @staticmethod
    async def _resolve_record(
        uow: OriginalUploadUnitOfWork,
        upload_id: UUID,
    ) -> OriginalUploadRecord:
        record = await uow.original_uploads.resolve(upload_id)
        if record is None:
            raise IdempotencyConflictError("Upload result cannot be resolved.")
        return record

    @staticmethod
    def _updated_record(
        current: OriginalUploadRecord,
        **updates: object,
    ) -> OriginalUploadRecord:
        return OriginalUploadRecord.model_validate(
            current.model_dump()
            | updates
            | {"record_version": current.record_version + 1}
        )

    @staticmethod
    def _request_digest(command: OriginalUploadCommandInput) -> CommandDigest:
        payload = OriginalUploadCommandPayload(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            purpose=command.purpose,
            expected_sha256=command.expected_sha256,
            expected_byte_length=command.expected_byte_length,
        )
        return digest_command(
            operation=PersistenceOperation(command.operation_name),
            operation_schema_version=command.operation_schema_version,
            payload=payload.model_dump(),
        )

    @staticmethod
    def _validate_context_binding(
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
    ) -> None:
        try:
            context.validate_shape()
            scope = context.scope
            environment_id = (
                scope.environment_id if isinstance(scope, EnvironmentScope) else None
            )
            if not isinstance(scope, (WorkspaceScope, EnvironmentScope)):
                raise InvalidPersistenceContextError("Persistence context is invalid.")
            if (
                scope.workspace_id != command.workspace_id
                or environment_id != command.environment_id
                or context.operation.value != command.operation_name
                or context.purpose.value != command.purpose
            ):
                raise InvalidPersistenceContextError("Persistence context is invalid.")
        except (AttributeError, TypeError, ValueError):
            raise InvalidPersistenceContextError("Persistence context is invalid.") from None

    @staticmethod
    def _validate_result_reference(reference: OpaqueResultReference) -> None:
        if (
            reference.result_type != _RESULT_TYPE
            or reference.schema_version != _RESULT_SCHEMA_VERSION
        ):
            raise IdempotencyConflictError("Idempotency result type is invalid.")

    def _new_identity(self) -> UUID:
        identity = self._identity_factory()
        if not isinstance(identity, UUID) or identity.int == 0:
            raise ValueError("identity factory must return a non-zero UUID")
        return identity

    @staticmethod
    def _raise_if_upload_blocked(record: OriginalUploadRecord) -> None:
        if record.state.state is UploadCommandStateName.RECONCILIATION_REQUIRED:
            raise ReconciliationRequired()
        if record.state.state in {
            UploadCommandStateName.ABORTED,
            UploadCommandStateName.INTEGRITY_CONFLICT,
        }:
            raise ObjectProtected()

    @staticmethod
    def _abort_outcome(
        record: OriginalUploadRecord,
        *,
        replay: bool,
    ) -> OriginalUploadAbortOutcome:
        return OriginalUploadAbortOutcome(
            result=record.result,
            state=record.state.state,
            cleanup_status=record.cleanup_status,
            cleanup_error_category=record.cleanup_error_category,
            replay=replay,
        )


__all__ = ["OriginalUploadOrchestrator"]
