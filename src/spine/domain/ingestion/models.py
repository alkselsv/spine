"""Framework-independent, immutable ingestion operational ledger contracts."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictInt, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.sources.original_reference import ObjectReference


_DIGEST = r"[0-9a-f]{64}"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"
BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=64, pattern=_IDENTIFIER)]
DigestHex = Annotated[str, Field(pattern=_DIGEST)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
SafeCounter = Annotated[StrictInt, Field(ge=0, le=9_999_999_999)]
SafeAttempt = Annotated[StrictInt, Field(gt=0, le=9_999_999_999)]


class IngestionRunState(str, Enum):
    REQUESTED = "requested"
    RECEIVING = "receiving"
    PROCESSING = "processing"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IngestionItemState(str, Enum):
    RECEIVED = "received"
    STORED = "stored"
    OBSERVED = "observed"
    ADMISSION_PENDING = "admission_pending"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    PROCESSED = "processed"
    PROCESSING_FAILED = "processing_failed"
    PROJECTION_REQUESTED = "projection_requested"


class IngestionTargetKind(str, Enum):
    RUN = "run"
    ITEM = "item"


class ProcessingCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class WarningStage(str, Enum):
    RECEIVING = "receiving"
    VALIDATING = "validating"
    STORING = "storing"
    OBSERVING = "observing"
    PARSING = "parsing"
    LOCATING = "locating"


class FailureStage(str, Enum):
    RECEIVING = "receiving"
    VALIDATING = "validating"
    STORING = "storing"
    OBSERVING = "observing"
    PARSING = "parsing"
    LOCATING = "locating"
    PERSISTING = "persisting"


class _FrozenRecord(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SafeDiagnosticDetail(_FrozenRecord):
    """Allowlisted, content-free diagnostic values.

    Parser/provider exception text, filenames, titles, paths and excerpts have
    no field in this contract. An opaque reference can point to a separately
    protected diagnostic store without making that store part of the ledger.
    """

    schema_version: Literal["ingestion-safe-detail:v1"]
    attempt: SafeAttempt | None = None
    item_count: SafeCounter | None = None
    byte_count: SafeCounter | None = None
    duration_ms: SafeCounter | None = None
    retryable: bool | None = None
    opaque_detail_ref: UUID | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> "SafeDiagnosticDetail":
        if self.opaque_detail_ref is not None and self.opaque_detail_ref.int == 0:
            raise ValueError("opaque_detail_ref must be a non-zero UUID")
        return self


class ProcessingExclusion(_FrozenRecord):
    schema_version: Literal["ingestion-exclusion:v1"]
    code: Literal["unsupported_format", "invalid_locator", "unreadable_content", "policy_excluded"]
    count: NonNegativeInt
    opaque_detail_ref: UUID | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> "ProcessingExclusion":
        if self.opaque_detail_ref is not None and self.opaque_detail_ref.int == 0:
            raise ValueError("opaque_detail_ref must be a non-zero UUID")
        return self


class IngestionRun(_FrozenRecord):
    ingestion_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    command_identity: BoundedIdentifier
    command_digest: DigestHex
    trigger_kind: Literal["upload", "connector", "reprocess", "retry"]
    trigger_reference: BoundedIdentifier | None = None
    requested_scope_schema: Literal["ingestion-scope:v1"]
    requested_scope_digest: DigestHex
    requested_at: datetime
    idempotency_receipt_id: UUID | None = None

    @model_validator(mode="after")
    def validate_ids_and_time(self) -> "IngestionRun":
        _require_ids(self, "ingestion_run_id", "workspace_id", "environment_id")
        _require_optional_id(self.idempotency_receipt_id, "idempotency_receipt_id")
        _require_time(self.requested_at)
        return self


class IngestionRunSource(_FrozenRecord):
    ingestion_run_source_id: UUID
    ingestion_run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    input_ordinal: PositiveInt
    input_identity: BoundedIdentifier
    input_digest: DigestHex
    command_identity: BoundedIdentifier
    command_digest: DigestHex
    created_at: datetime

    @model_validator(mode="after")
    def validate_ids_and_time(self) -> "IngestionRunSource":
        _require_ids(self, "ingestion_run_source_id", "ingestion_run_id", "workspace_id", "environment_id")
        _require_time(self.created_at)
        return self


class IngestionRunSourceBinding(_FrozenRecord):
    binding_id: UUID
    ingestion_run_source_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    binding_command_identity: BoundedIdentifier
    binding_command_digest: DigestHex
    original_reference: ObjectReference | None = None
    original_receipt_kind: Literal["issue6.write_receipt"] | None = None
    original_receipt_id: UUID | None = None
    original_receipt_digest: DigestHex | None = None
    bound_at: datetime

    idempotency_receipt_id: UUID | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "IngestionRunSourceBinding":
        _require_ids(
            self,
            "binding_id",
            "ingestion_run_source_id",
            "source_object_id",
            "source_revision_id",
            "workspace_id",
            "environment_id",
        )
        _require_optional_id(self.idempotency_receipt_id, "idempotency_receipt_id")
        _require_time(self.bound_at)
        if self.original_receipt_id is None:
            if any(
                value is not None
                for value in (self.original_reference, self.original_receipt_kind, self.original_receipt_digest)
            ):
                raise ValueError("original receipt fields must be supplied together")
        else:
            if self.original_reference is None or self.original_receipt_kind is None or self.original_receipt_digest is None:
                raise ValueError("content binding requires a complete finalized receipt reference")
            if self.original_receipt_id.int == 0:
                raise ValueError("original_receipt_id must be a non-zero UUID")
            if self.original_receipt_digest != self.original_reference.digest_hex:
                raise ValueError("original receipt digest must match the original reference")
        return self


class IngestionTransition(_FrozenRecord):
    ingestion_transition_id: UUID
    workspace_id: UUID
    environment_id: UUID
    target_kind: IngestionTargetKind
    ingestion_run_id: UUID
    ingestion_run_source_id: UUID | None = None
    predecessor_transition_id: UUID | None = None
    state: IngestionRunState | IngestionItemState
    attempt: PositiveInt
    safe_metrics: SafeDiagnosticDetail | None = None
    actor_or_service: BoundedIdentifier
    trace_id: UUID
    occurred_at: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_state_for_target(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        target = values.get("target_kind")
        state = values.get("state")
        if isinstance(target, str):
            target = IngestionTargetKind(target)
        if isinstance(state, str):
            values = dict(values)
            values["state"] = (
                IngestionRunState(state)
                if target is IngestionTargetKind.RUN
                else IngestionItemState(state)
            )
        return values

    @model_validator(mode="after")
    def validate_target(self) -> "IngestionTransition":
        _require_ids(self, "ingestion_transition_id", "workspace_id", "environment_id", "ingestion_run_id", "trace_id")
        _require_time(self.occurred_at)
        if self.target_kind is IngestionTargetKind.RUN:
            if self.ingestion_run_source_id is not None or not isinstance(self.state, IngestionRunState):
                raise ValueError("run transitions require a run state and no item")
        elif self.ingestion_run_source_id is None or not isinstance(self.state, IngestionItemState):
            raise ValueError("item transitions require an item state")
        return self


class IngestionWarning(_FrozenRecord):
    ingestion_warning_id: UUID
    workspace_id: UUID
    environment_id: UUID
    ingestion_run_source_id: UUID
    attempt: PositiveInt
    stage: WarningStage
    code: Literal["metadata_normalized", "partial_coverage", "source_order_unknown", "orphan_candidate"]
    safe_detail: SafeDiagnosticDetail
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_record(self) -> "IngestionWarning":
        _require_ids(self, "ingestion_warning_id", "workspace_id", "environment_id", "ingestion_run_source_id")
        _require_time(self.occurred_at)
        return self


class IngestionFailure(_FrozenRecord):
    ingestion_failure_id: UUID
    workspace_id: UUID
    environment_id: UUID
    ingestion_run_source_id: UUID
    attempt: PositiveInt
    stage: FailureStage
    code: Literal["invalid_input", "storage_unavailable", "storage_integrity_conflict", "observation_conflict", "parser_failed", "locator_failed", "persistence_failed", "cancelled"]
    retryable: bool
    safe_detail: SafeDiagnosticDetail
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_record(self) -> "IngestionFailure":
        _require_ids(self, "ingestion_failure_id", "workspace_id", "environment_id", "ingestion_run_source_id")
        _require_time(self.occurred_at)
        return self


class ProcessingReceipt(_FrozenRecord):
    processing_receipt_id: UUID
    workspace_id: UUID
    environment_id: UUID
    ingestion_run_source_id: UUID
    binding_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    processor_kind: BoundedIdentifier
    parser_name: BoundedIdentifier
    parser_version: BoundedIdentifier
    parser_config_reference: BoundedIdentifier
    locator_contract_version: BoundedIdentifier
    original_sha256: DigestHex
    output_kind: BoundedIdentifier | None = None
    output_reference: UUID | None = None
    output_digest: DigestHex | None = None
    coverage: ProcessingCoverage
    exclusions: tuple[ProcessingExclusion, ...] = ()
    warning_ids: tuple[UUID, ...] = ()
    failure_ids: tuple[UUID, ...] = ()
    command_identity: BoundedIdentifier
    command_digest: DigestHex
    completed_at: datetime
    idempotency_receipt_id: UUID | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> "ProcessingReceipt":
        _require_ids(
            self,
            "processing_receipt_id",
            "workspace_id",
            "environment_id",
            "ingestion_run_source_id",
            "binding_id",
            "source_object_id",
            "source_revision_id",
        )
        _require_optional_id(self.idempotency_receipt_id, "idempotency_receipt_id")
        _require_time(self.completed_at)
        if self.output_reference is not None and self.output_reference.int == 0:
            raise ValueError("output_reference must be a non-zero UUID")
        if any(identifier.int == 0 for identifier in (*self.warning_ids, *self.failure_ids)):
            raise ValueError("diagnostic references must be non-zero UUIDs")
        if self.coverage is ProcessingCoverage.COMPLETE and self.exclusions:
            raise ValueError("complete processing cannot contain exclusions")
        if self.coverage is ProcessingCoverage.COMPLETE and self.failure_ids:
            raise ValueError("complete processing cannot contain failures")
        if self.coverage is ProcessingCoverage.FAILED and self.output_digest is not None:
            raise ValueError("failed processing cannot contain an output digest")
        if self.coverage is not ProcessingCoverage.FAILED and self.output_digest is None:
            raise ValueError("successful or partial processing requires an output digest")
        if self.coverage is not ProcessingCoverage.FAILED and (self.output_kind is None or self.output_reference is None):
            raise ValueError("successful or partial processing requires an output reference")
        if self.coverage is ProcessingCoverage.COMPLETE and self.locator_contract_version == "":
            raise ValueError("complete processing requires a locator contract version")
        return self


def _require_ids(model: object, *names: str) -> None:
    for name in names:
        value = getattr(model, name)
        if not isinstance(value, UUID) or value.int == 0:
            raise ValueError(f"{name} must be a non-zero UUID")


def _require_optional_id(value: UUID | None, name: str) -> None:
    if value is not None and (not isinstance(value, UUID) or value.int == 0):
        raise ValueError(f"{name} must be a non-zero UUID")


def _require_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")


__all__ = [
    "FailureStage",
    "IngestionFailure",
    "IngestionItemState",
    "IngestionRun",
    "IngestionRunSource",
    "IngestionRunSourceBinding",
    "IngestionRunState",
    "IngestionTargetKind",
    "IngestionTransition",
    "IngestionWarning",
    "ProcessingCoverage",
    "ProcessingExclusion",
    "ProcessingReceipt",
    "SafeDiagnosticDetail",
    "WarningStage",
]
