"""Typed application ports for the Issue #86 operational ingestion ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    OpaqueResultReference,
)
from spine.domain.ingestion import (
    IngestionFailure,
    IngestionItemState,
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    IngestionWarning,
    ProcessingReceipt,
)

if TYPE_CHECKING:
    from spine.application.persistence.repositories import SourceObservationCommand


INGESTION_RUN_SCHEMA_VERSION = 1
INGESTION_BINDING_SCHEMA_VERSION = 1
PROCESSING_RECEIPT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class IngestionRunCommand:
    run: IngestionRun
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(cls, *, run: IngestionRun, idempotency_key: IdempotencyKey) -> "IngestionRunCommand":
        digest = digest_command(
            operation=PersistenceOperation("ingestion_run"),
            operation_schema_version=INGESTION_RUN_SCHEMA_VERSION,
            payload={
                "command_identity": run.command_identity,
                "trigger_kind": run.trigger_kind,
                "trigger_reference": run.trigger_reference,
                "requested_scope_schema": run.requested_scope_schema,
                "requested_scope_digest": run.requested_scope_digest,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            run=run.model_copy(update={"command_digest": digest.value}),
            idempotency_key=idempotency_key,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class IngestionBindingCommand:
    item: IngestionRunSource
    binding: IngestionRunSourceBinding
    observation: SourceObservationCommand
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls,
        *,
        item: IngestionRunSource,
        binding: IngestionRunSourceBinding,
        observation: SourceObservationCommand,
        idempotency_key: IdempotencyKey,
    ) -> "IngestionBindingCommand":
        digest = digest_command(
            operation=PersistenceOperation("ingestion_binding"),
            operation_schema_version=INGESTION_BINDING_SCHEMA_VERSION,
            payload={
                "item_id": item.ingestion_run_source_id,
                "source_object_id": binding.source_object_id,
                "source_revision_id": binding.source_revision_id,
                "original_receipt_id": binding.original_receipt_id,
                "original_receipt_digest": binding.original_receipt_digest,
                "observation_digest": observation.digest.value,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            item=item,
            binding=binding.model_copy(update={"binding_command_digest": digest.value}),
            observation=observation,
            idempotency_key=idempotency_key,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class ProcessingReceiptCommand:
    receipt: ProcessingReceipt
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls, *, receipt: ProcessingReceipt, idempotency_key: IdempotencyKey
    ) -> "ProcessingReceiptCommand":
        digest = digest_command(
            operation=PersistenceOperation("processing_receipt"),
            operation_schema_version=PROCESSING_RECEIPT_SCHEMA_VERSION,
            payload={
                "item_id": receipt.ingestion_run_source_id,
                "binding_id": receipt.binding_id,
                "source_revision_id": receipt.source_revision_id,
                "processor_kind": receipt.processor_kind,
                "parser_name": receipt.parser_name,
                "parser_version": receipt.parser_version,
                "parser_config_reference": receipt.parser_config_reference,
                "locator_contract_version": receipt.locator_contract_version,
                "original_sha256": receipt.original_sha256,
                "output_kind": receipt.output_kind,
                "output_reference": receipt.output_reference,
                "output_digest": receipt.output_digest,
                "coverage": receipt.coverage,
                "exclusions": [
                    item.model_dump(mode="json") for item in receipt.exclusions
                ],
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            receipt=receipt.model_copy(update={"command_digest": digest.value}),
            idempotency_key=idempotency_key,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class IngestionMutationResult:
    value: object
    replay: bool
    claim: IdempotencyClaimResult
    result_reference: OpaqueResultReference


@dataclass(frozen=True, slots=True)
class IngestionRunStateView:
    run: IngestionRun
    state: IngestionRunState
    items: tuple[IngestionRunSource, ...]
    item_states: tuple[IngestionItemState, ...]


class IngestionRepository(Protocol):
    async def create_run(self, command: IngestionRunCommand) -> IngestionMutationResult: ...

    async def create_item(self, item: IngestionRunSource) -> IngestionRunSource: ...

    async def bind_source(self, command: IngestionBindingCommand) -> IngestionMutationResult: ...

    async def append_transition(self, transition: IngestionTransition) -> IngestionTransition: ...

    async def append_warning(self, warning: IngestionWarning) -> IngestionWarning: ...

    async def append_failure(self, failure: IngestionFailure) -> IngestionFailure: ...

    async def record_processing_receipt(
        self, command: ProcessingReceiptCommand
    ) -> IngestionMutationResult: ...

    async def derive_run_state(self, run_id: UUID) -> IngestionRunStateView | None: ...

    async def list_runs(
        self,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> tuple[IngestionRunStateView, ...]: ...

    async def list_items(
        self,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> tuple[IngestionRunSource, ...]: ...

    async def resolve_run_head(self, run_id: UUID) -> IngestionTransition | None: ...

    async def resolve_item_head(self, item_id: UUID) -> IngestionTransition | None: ...

    async def resolve_processing_receipt(self, item_id: UUID) -> ProcessingReceipt | None: ...

    async def resolve_processing_receipt_by_id(
        self, processing_receipt_id: UUID
    ) -> ProcessingReceipt | None: ...

    async def resolve_warnings(self, item_id: UUID) -> tuple[IngestionWarning, ...]: ...

    async def resolve_failures(self, item_id: UUID) -> tuple[IngestionFailure, ...]: ...

    async def resolve_item(self, item_id: UUID) -> IngestionRunSource | None: ...

    async def resolve_binding(self, item_id: UUID) -> IngestionRunSourceBinding | None: ...

    async def list_bindings_for_source_revision(
        self, source_object_id: UUID, source_revision_id: UUID
    ) -> tuple[IngestionRunSourceBinding, ...]: ...


def deterministic_receipt_id(*, workspace_id: UUID, environment_id: UUID, operation: str, key: IdempotencyKey) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "|".join(("spine-idempotency-receipt-v1", str(workspace_id), str(environment_id), operation, "1", key.value)),
    )


RUN_TRANSITIONS: dict[IngestionRunState, frozenset[IngestionRunState]] = {
    IngestionRunState.REQUESTED: frozenset({IngestionRunState.RECEIVING, IngestionRunState.CANCELLED}),
    IngestionRunState.RECEIVING: frozenset({IngestionRunState.PROCESSING, IngestionRunState.CANCELLED}),
    IngestionRunState.PROCESSING: frozenset({IngestionRunState.PARTIALLY_SUCCEEDED, IngestionRunState.SUCCEEDED, IngestionRunState.FAILED, IngestionRunState.CANCELLED}),
    IngestionRunState.PARTIALLY_SUCCEEDED: frozenset(),
    IngestionRunState.SUCCEEDED: frozenset(),
    IngestionRunState.FAILED: frozenset(),
    IngestionRunState.CANCELLED: frozenset(),
}


ITEM_TRANSITIONS: dict[IngestionItemState, frozenset[IngestionItemState]] = {
    IngestionItemState.RECEIVED: frozenset({IngestionItemState.STORED, IngestionItemState.OBSERVED, IngestionItemState.PROCESSING_FAILED}),
    IngestionItemState.STORED: frozenset({IngestionItemState.OBSERVED, IngestionItemState.ADMISSION_PENDING, IngestionItemState.PROCESSING_FAILED}),
    IngestionItemState.OBSERVED: frozenset({IngestionItemState.ADMISSION_PENDING, IngestionItemState.QUARANTINED, IngestionItemState.REJECTED, IngestionItemState.ACCEPTED, IngestionItemState.PROCESSING_FAILED}),
    IngestionItemState.ADMISSION_PENDING: frozenset({IngestionItemState.QUARANTINED, IngestionItemState.REJECTED, IngestionItemState.ACCEPTED, IngestionItemState.PROCESSING_FAILED}),
    IngestionItemState.QUARANTINED: frozenset({IngestionItemState.RECEIVED}),
    IngestionItemState.REJECTED: frozenset(),
    IngestionItemState.ACCEPTED: frozenset({IngestionItemState.PROCESSING, IngestionItemState.PROCESSING_FAILED}),
    IngestionItemState.PROCESSING: frozenset({IngestionItemState.PROCESSED, IngestionItemState.PROCESSING_FAILED}),
    IngestionItemState.PROCESSED: frozenset({IngestionItemState.PROJECTION_REQUESTED}),
    IngestionItemState.PROCESSING_FAILED: frozenset({IngestionItemState.PROCESSING}),
    IngestionItemState.PROJECTION_REQUESTED: frozenset(),
}


def validate_transition(previous: IngestionTransition | None, current: IngestionTransition) -> None:
    if current.predecessor_transition_id != (previous.ingestion_transition_id if previous else None):
        raise ValueError("ingestion transition predecessor does not match the current head")
    if previous is None:
        if current.state not in {
            IngestionRunState.REQUESTED,
            IngestionItemState.RECEIVED,
        }:
            raise ValueError("ingestion transition must start at the requested/received state")
        return
    if previous.target_kind is not current.target_kind or previous.ingestion_run_id != current.ingestion_run_id or previous.ingestion_run_source_id != current.ingestion_run_source_id:
        raise ValueError("ingestion transition target does not match predecessor")
    allowed = RUN_TRANSITIONS if current.target_kind is IngestionTargetKind.RUN else ITEM_TRANSITIONS
    if current.state not in allowed[previous.state]:  # type: ignore[index]
        raise ValueError(f"illegal ingestion transition: {previous.state} -> {current.state}")


__all__ = [
    "INGESTION_BINDING_SCHEMA_VERSION",
    "INGESTION_RUN_SCHEMA_VERSION",
    "IngestionBindingCommand",
    "IngestionMutationResult",
    "IngestionRepository",
    "IngestionRunCommand",
    "IngestionRunStateView",
    "ITEM_TRANSITIONS",
    "PROCESSING_RECEIPT_SCHEMA_VERSION",
    "ProcessingReceiptCommand",
    "RUN_TRANSITIONS",
    "deterministic_receipt_id",
    "validate_transition",
]
