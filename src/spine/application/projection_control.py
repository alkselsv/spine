"""Application contracts for safe projection status and recovery requests.

The read model deliberately contains only PostgreSQL-owned publication facts
and bounded operational metadata.  Recovery requests append an outbox intent;
they never choose a snapshot, candidate, namespace or access policy.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    PersistenceUnavailableError,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.persistence.projection import (
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
    PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestedOutboxPayload,
    ProjectionSuccessorRequestedOutboxPayload,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.projection_identity import (
    projection_publication_entity_id,
    projection_recovery_entity_id,
    projection_successor_intent_id,
)
from spine.domain.projections import (
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionKind,
    PROJECTION_KIND_PATTERN,
    ProjectionReconciliationOutcome,
    ProjectionOperation,
    ProjectionRunState,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)


PROJECTION_RECOVERY_REQUEST_SCHEMA_VERSION = 1
_PROJECTION_RECOVERY_RESULT_TYPE = "projection_recovery_request"
_PROJECTION_SUCCESSOR_RESULT_TYPE = "projection_successor_request"


class InvalidProjectionControlRequest(ValueError):
    """A projection control request is malformed."""


class ProjectionControlServiceUnavailable(PersistenceUnavailableError):
    """The projection control adapter is not composed or temporarily unavailable."""


class ProjectionPublicationStatus(str, Enum):
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"
    UNAVAILABLE = "unavailable"


ProjectionOperationName = Literal[
    "build",
    "catch_up",
    "rebuild",
    "rollback",
    "publish",
    "reconciliation",
]
ProjectionOperationStatus = Literal[
    "accepted",
    "running",
    "completed",
    "failed",
    "unavailable",
]
ProjectionRecoveryState = Literal[
    "not_requested",
    "pending",
    "succeeded",
    "failed",
    "unavailable",
]


class ProjectionAsOfSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_boundary_id: UUID
    accepted_through_generation: int = Field(ge=0)
    boundary_time: datetime


class ProjectionReconciliationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: UUID
    outcome: ProjectionReconciliationOutcome
    checked_at: datetime


class ProjectionOperationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: ProjectionOperationName
    status: ProjectionOperationStatus
    completed_at: datetime | None = None
    retryable: bool = False


class ProjectionStatusSnapshot(BaseModel):
    """Safe, scope-bound projection publication read model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    workspace_id: UUID
    environment_id: UUID
    projection_kind: ProjectionKind
    publication_status: ProjectionPublicationStatus
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    snapshot_id: UUID | None = None
    activation_id: UUID | None = None
    canonical_boundary_id: UUID | None = None
    as_of: ProjectionAsOfSnapshot | None = None
    activation_generation: int | None = Field(default=None, ge=1)
    projection_config_version_id: UUID | None = None
    projection_config_version: int | None = Field(default=None, ge=1)
    freshness: ProjectionFreshness | None = None
    health: ProjectionHealth | None = None
    drift_count: int | None = Field(default=None, ge=0)
    reconciliation: ProjectionReconciliationSnapshot | None = None
    last_operation: ProjectionOperationSnapshot | None = None
    retryable: bool = False
    recovery_state: ProjectionRecoveryState = "not_requested"

    @model_validator(mode="after")
    def validate_publication_dimensions(self) -> "ProjectionStatusSnapshot":
        if self.publication_status == "published":
            required = (
                self.snapshot_id,
                self.activation_id,
                self.canonical_boundary_id,
                self.as_of,
                self.activation_generation,
                self.projection_config_version_id,
                self.projection_config_version,
                self.freshness,
                self.health,
                self.drift_count,
            )
            if any(value is None for value in required):
                raise ValueError("published status requires publication metadata")
        elif any(
            value is not None
            for value in (
                self.snapshot_id,
                self.activation_id,
                self.canonical_boundary_id,
                self.as_of,
                self.activation_generation,
                self.projection_config_version_id,
                self.projection_config_version,
                self.freshness,
                self.health,
                self.drift_count,
                self.reconciliation,
                self.last_operation,
            )
        ):
            raise ValueError("unpublished or unavailable status cannot carry publication metadata")
        if self.publication_status != "unavailable" and self.reason_code is not None:
            raise ValueError("only unavailable status may carry a reason code")
        return self

    @classmethod
    def unavailable(
        cls,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        projection_kind: ProjectionKind,
        reason_code: str = "dependency.unavailable",
    ) -> "ProjectionStatusSnapshot":
        return cls(
            workspace_id=workspace_id,
            environment_id=environment_id,
            projection_kind=projection_kind,
            publication_status="unavailable",
            reason_code=reason_code,
            recovery_state="unavailable",
        )


class ProjectionStatusService(Protocol):
    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
    ) -> ProjectionStatusSnapshot: ...


class ProjectionRecoveryCommandService(Protocol):
    async def request(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
        action: ProjectionRecoveryAction,
        idempotency_key: IdempotencyKey,
    ) -> "ProjectionRecoveryRequestResult": ...

    async def successor_status(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
        command_id: UUID,
    ) -> "ProjectionSuccessorStatusSnapshot": ...


class ProjectionRecoveryRequestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    projection_kind: ProjectionKind
    action: ProjectionRecoveryAction
    intent_id: UUID | None = None
    replay: bool = False


class ProjectionSuccessorStatusSnapshot(BaseModel):
    """Content-free correlation across one normal-successor lifecycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    intent_id: UUID
    projection_kind: ProjectionKind
    status: Literal["accepted", "running", "completed", "failed", "unavailable"]
    run_id: UUID | None = None
    candidate_id: UUID | None = None
    receipt_ids: tuple[UUID, ...] = ()
    snapshot_id: UUID | None = None
    activation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UnavailableProjectionStatusService:
    """Safe status fallback used when the persistence adapter is not composed."""

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
    ) -> ProjectionStatusSnapshot:
        _validate_projection_kind(projection_kind)
        workspace_id, environment_id = _scope_ids(context)
        return ProjectionStatusSnapshot.unavailable(
            workspace_id=workspace_id,
            environment_id=environment_id,
            projection_kind=projection_kind,
        )


@dataclass(frozen=True, slots=True)
class UnavailableProjectionRecoveryService:
    async def request(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
        action: ProjectionRecoveryAction,
        idempotency_key: IdempotencyKey,
    ) -> ProjectionRecoveryRequestResult:
        del context, projection_kind, action, idempotency_key
        raise ProjectionControlServiceUnavailable()

    async def successor_status(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
        command_id: UUID,
    ) -> ProjectionSuccessorStatusSnapshot:
        del context, projection_kind, command_id
        raise ProjectionControlServiceUnavailable()


@dataclass(frozen=True, slots=True)
class CanonicalProjectionStatusService:
    """Resolve active publication state through the canonical projection port."""

    uow_factory: UnitOfWorkFactory

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
    ) -> ProjectionStatusSnapshot:
        _validate_projection_kind(projection_kind)
        workspace_id, environment_id = _scope_ids(context)
        try:
            async with self.uow_factory(context) as uow:
                publication = await uow.projections.resolve_active_publication(
                    projection_kind
                )
                if (
                    publication.workspace_id != workspace_id
                    or publication.environment_id != environment_id
                    or publication.projection_kind != projection_kind
                ):
                    result = ProjectionStatusSnapshot.unavailable(
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                        projection_kind=projection_kind,
                    )
                elif isinstance(publication, UnpublishedActivePublication):
                    result = ProjectionStatusSnapshot(
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                        projection_kind=projection_kind,
                        publication_status="unpublished",
                    )
                elif isinstance(publication, UnavailableActivePublication):
                    result = ProjectionStatusSnapshot.unavailable(
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                        projection_kind=projection_kind,
                        reason_code=_safe_reason(publication.reason_code),
                    )
                elif isinstance(publication, PublishedActivePublication):
                    drifts = await uow.projections.list_drifts(
                        publication.projection_activation.projection_activation_id
                    )
                    result = _published_status(publication, drift_count=len(drifts))
                else:
                    raise TypeError("projection repository returned an unknown publication")
                await uow.commit()
                return result
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return ProjectionStatusSnapshot.unavailable(
                workspace_id=workspace_id,
                environment_id=environment_id,
                projection_kind=projection_kind,
            )


@dataclass(frozen=True, slots=True)
class ProjectionRecoveryRequestService:
    """Atomically claim a recovery command and append its worker intent."""

    uow_factory: UnitOfWorkFactory
    outbox_events: OutboxEventRegistry

    async def request(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
        action: ProjectionRecoveryAction,
        idempotency_key: IdempotencyKey,
    ) -> ProjectionRecoveryRequestResult:
        _scope_ids(context)
        _validate_projection_kind(projection_kind)
        if not isinstance(action, ProjectionRecoveryAction):
            raise InvalidProjectionControlRequest()
        successor = action is ProjectionRecoveryAction.SUCCESSOR
        result_type = (
            _PROJECTION_SUCCESSOR_RESULT_TYPE
            if successor
            else _PROJECTION_RECOVERY_RESULT_TYPE
        )
        digest = digest_command(
            operation=PersistenceOperation("request_projection_recovery"),
            operation_schema_version=PROJECTION_RECOVERY_REQUEST_SCHEMA_VERSION,
            payload={
                "projection_kind": projection_kind,
                "action": action,
            },
        )
        async with self.uow_factory(context) as uow:
            claim = await uow.idempotency.claim(
                operation_schema_version=PROJECTION_RECOVERY_REQUEST_SCHEMA_VERSION,
                key=idempotency_key,
                digest=digest,
            )
            if isinstance(claim, IdempotencyReplay):
                if claim.result.result_type != result_type:
                    raise IdempotencyConflictError(
                        "Projection recovery result reference is invalid."
                    )
                await uow.commit()
                return ProjectionRecoveryRequestResult(
                    command_id=claim.result.result_id,
                    projection_kind=projection_kind,
                    action=action,
                    intent_id=(
                        projection_successor_intent_id(claim.result.result_id)
                        if successor
                        else None
                    ),
                    replay=True,
                )

            if not isinstance(claim, OwnedIdempotencyClaim):
                raise IdempotencyConflictError("Projection recovery claim is invalid.")
            command_id = claim.receipt_id
            if successor:
                captured = await uow.boundaries.capture_for_claim(
                    projection_kind=projection_kind,
                    scope_version="r1",
                    claim=claim,
                )
                payload = ProjectionSuccessorRequestedOutboxPayload(
                    command_id=command_id,
                    projection_kind=projection_kind,
                    canonical_boundary_id=captured.boundary.boundary_id,
                    canonical_boundary_digest=captured.boundary.manifest_digest,
                    accepted_through_generation=(
                        captured.boundary.accepted_through_generation
                    ),
                    command_digest=digest.value,
                )
                event_type = PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE
                deduplication_id = f"projection-successor:{command_id}"
            else:
                payload = ProjectionRecoveryRequestedOutboxPayload(
                    command_id=command_id,
                    projection_kind=projection_kind,
                    action=action,
                    command_digest=digest.value,
                )
                event_type = PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE
                deduplication_id = f"projection-recovery:{command_id}"
            intent = self.outbox_events.build_intent(
                event_id=(
                    projection_successor_intent_id(command_id)
                    if successor
                    else None
                ),
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                event_type=event_type,
                schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
                payload=payload,
                aggregate=OpaqueObjectReference(
                    object_type=result_type,
                    object_id=command_id,
                    schema_version=1,
                ),
                producer_deduplication_id=deduplication_id,
                trace_id=context.trace_id,
            )
            await uow.outbox.append(intent)
            await uow.idempotency.complete(
                claim,
                OpaqueResultReference(
                    result_type=result_type,
                    result_id=command_id,
                    schema_version=1,
                ),
            )
            await uow.commit()
            return ProjectionRecoveryRequestResult(
                command_id=command_id,
                projection_kind=projection_kind,
                action=action,
                intent_id=(
                    projection_successor_intent_id(command_id)
                    if successor
                    else None
                ),
                replay=False,
            )

    async def successor_status(
        self,
        *,
        context: TrustedPersistenceContext,
        projection_kind: ProjectionKind,
        command_id: UUID,
    ) -> ProjectionSuccessorStatusSnapshot:
        _scope_ids(context)
        _validate_projection_kind(projection_kind)
        if not isinstance(command_id, UUID) or command_id.int == 0:
            raise InvalidProjectionControlRequest()
        intent_id = projection_successor_intent_id(command_id)
        run_id = projection_recovery_entity_id(command_id, "run")
        async with self.uow_factory(context) as uow:
            delivery_status = await uow.outbox.resolve_delivery_status(intent_id)
            run = await uow.projections.resolve_run(run_id)
            if run is None:
                await uow.commit()
                return ProjectionSuccessorStatusSnapshot(
                    command_id=command_id,
                    intent_id=intent_id,
                    projection_kind=projection_kind,
                    status=(
                        "failed"
                        if delivery_status == "quarantined"
                        else "accepted"
                        if delivery_status in {"pending", "in_flight", "retry_scheduled"}
                        else "unavailable"
                    ),
                )
            workspace_id, environment_id = _scope_ids(context)
            if (
                run.workspace_id != workspace_id
                or run.environment_id != environment_id
                or run.projection_kind != projection_kind
                or run.operation is not ProjectionOperation.CATCH_UP
                or run.command_identity != f"successor-{command_id.hex}"
            ):
                raise InvalidProjectionControlRequest()
            transitions = await uow.projections.list_transitions(run_id)
            receipts = await uow.projections.list_receipts(run_id)
            snapshot = await uow.projections.resolve_snapshot(
                projection_publication_entity_id(run_id, "snapshot")
            )
            activations = await uow.projections.list_activations(projection_kind)
            activation = next(
                (
                    value
                    for value in activations
                    if snapshot is not None
                    and value.projection_snapshot_id
                    == snapshot.projection_snapshot_id
                ),
                None,
            )
            states = {transition.state for transition in transitions}
            status: Literal[
                "accepted", "running", "completed", "failed", "unavailable"
            ]
            if (
                ProjectionRunState.FAILED in states
                or delivery_status == "quarantined"
            ):
                status = "failed"
            elif activation is not None and ProjectionRunState.COMPLETE in states:
                status = "completed"
            else:
                status = "running"
            await uow.commit()
            return ProjectionSuccessorStatusSnapshot(
                command_id=command_id,
                intent_id=intent_id,
                projection_kind=projection_kind,
                status=status,
                run_id=run.projection_run_id,
                candidate_id=run.projection_candidate_id,
                receipt_ids=tuple(
                    sorted(
                        (receipt.projection_receipt_id for receipt in receipts),
                        key=lambda value: value.hex,
                    )
                ),
                snapshot_id=(
                    snapshot.projection_snapshot_id if snapshot is not None else None
                ),
                activation_id=(
                    activation.projection_activation_id
                    if activation is not None
                    else None
                ),
            )


def _scope_ids(context: object) -> tuple[UUID, UUID]:
    scope = getattr(context, "scope", None)
    if not isinstance(scope, EnvironmentScope):
        raise ValueError("projection control requires an environment scope")
    return scope.workspace_id, scope.environment_id


def _validate_projection_kind(projection_kind: ProjectionKind) -> None:
    if not isinstance(projection_kind, str) or re.fullmatch(
        PROJECTION_KIND_PATTERN, projection_kind
    ) is None:
        raise InvalidProjectionControlRequest()


def _published_status(
    publication: PublishedActivePublication,
    *,
    drift_count: int,
) -> ProjectionStatusSnapshot:
    reconciliation = publication.effective_reconciliation
    recovery_state: ProjectionRecoveryState = "not_requested"
    if reconciliation is not None:
        recovery_state = {
            ProjectionReconciliationOutcome.HEALTHY: "succeeded",
            ProjectionReconciliationOutcome.DEGRADED: "pending",
            ProjectionReconciliationOutcome.FAILED: "failed",
            ProjectionReconciliationOutcome.INTERRUPTED: "pending",
        }[reconciliation.outcome]
    retryable = publication.freshness is ProjectionFreshness.STALE or (
        publication.health is ProjectionHealth.DEGRADED
    ) or recovery_state in {"pending", "failed"}
    last_operation = ProjectionOperationSnapshot(
        operation=(
            "reconciliation"
            if reconciliation is not None
            else publication.projection_activation.activation_kind.value
        ),
        status="completed",
        completed_at=(
            reconciliation.checked_at
            if reconciliation is not None
            else publication.projection_activation.activated_at
        ),
        retryable=retryable,
    )
    return ProjectionStatusSnapshot(
        workspace_id=publication.workspace_id,
        environment_id=publication.environment_id,
        projection_kind=publication.projection_kind,
        publication_status="published",
        snapshot_id=publication.projection_snapshot.projection_snapshot_id,
        activation_id=publication.projection_activation.projection_activation_id,
        canonical_boundary_id=publication.canonical_boundary_id,
        as_of=ProjectionAsOfSnapshot(
            canonical_boundary_id=publication.as_of.canonical_boundary_id,
            accepted_through_generation=publication.as_of.accepted_through_generation,
            boundary_time=publication.as_of.boundary_time,
        ),
        activation_generation=publication.activation_generation,
        projection_config_version_id=(
            publication.projection_config_version.projection_config_version_id
        ),
        projection_config_version=publication.projection_config_version.version,
        freshness=publication.freshness,
        health=publication.health,
        drift_count=drift_count,
        reconciliation=(
            ProjectionReconciliationSnapshot(
                result_id=reconciliation.reconciliation_result_id,
                outcome=reconciliation.outcome,
                checked_at=reconciliation.checked_at,
            )
            if reconciliation is not None
            else None
        ),
        last_operation=last_operation,
        retryable=retryable,
        recovery_state=recovery_state,
    )


def _safe_reason(reason_code: str) -> str:
    return (
        reason_code
        if reason_code in {"active_publication_integrity_conflict"}
        else "publication.unavailable"
    )


__all__ = [
    "CanonicalProjectionStatusService",
    "ProjectionAsOfSnapshot",
    "ProjectionOperationSnapshot",
    "ProjectionRecoveryAction",
    "ProjectionRecoveryCommandService",
    "ProjectionRecoveryRequestResult",
    "ProjectionRecoveryRequestService",
    "ProjectionRecoveryState",
    "ProjectionSuccessorStatusSnapshot",
    "ProjectionReconciliationSnapshot",
    "ProjectionStatusService",
    "ProjectionStatusSnapshot",
    "ProjectionPublicationStatus",
    "InvalidProjectionControlRequest",
    "ProjectionControlServiceUnavailable",
    "UnavailableProjectionRecoveryService",
    "UnavailableProjectionStatusService",
]
