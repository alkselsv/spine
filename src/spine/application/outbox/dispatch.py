"""Bounded at-least-once dispatch over durable outbox delivery state."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.diagnostics.context import DiagnosticContext
from spine.application.diagnostics.events import (
    DiagnosticEventRegistry,
    DiagnosticSeverity,
    DiagnosticSink,
    OutboxDeliveryDiagnosticPayload,
    OutboxDeliveryDiagnosticState,
    emit_diagnostic_safely,
)
from spine.application.persistence.delivery import (
    ClaimedOutboxDelivery,
    OutboxDelivery,
    OutboxDeliveryStatus,
    UnresolvedOutboxIntent,
    validate_consumer_name,
)
from spine.application.persistence.errors import (
    PersistenceError,
    UnexpectedPersistenceError,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationOutcome,
    OperationalOperation,
    OperationalStage,
    SafeOperationalFailureCode,
)
from spine.domain.audit import AuditEvent, AuditOutcome, OutboxDeliveryAuditPayload
from spine.domain.common import ContextOrigin


_LOGGER = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")


class DuplicateOutboxConsumerError(ValueError):
    """A consumer name has more than one explicit implementation."""


class RetryableOutboxConsumerFailure(Exception):
    """A registered safe failure category eligible for delivery retry."""

    def __init__(self, code: str) -> None:
        if _IDENTIFIER.fullmatch(code) is None:
            raise ValueError("failure code must be a bounded safe identifier.")
        self.code = code
        super().__init__(code)


class TerminalOutboxConsumerFailure(Exception):
    """A registered safe failure category that must not be retried."""

    def __init__(self, code: str) -> None:
        if _IDENTIFIER.fullmatch(code) is None:
            raise ValueError("failure code must be a bounded safe identifier.")
        self.code = code
        super().__init__(code)


class DispatchOutcomeCode(str, Enum):
    """Disclosure-safe outcomes returned by one bounded dispatch call."""

    DELIVERED = "delivery.delivered"
    RETRY_SCHEDULED = "delivery.retry_scheduled"
    QUARANTINED = "delivery.quarantined"


class OutboxConsumerSuccess(BaseModel):
    """Typed success carrying the exact context observed by a consumer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    diagnostic_context: DiagnosticContext


class OutboxConsumer(Protocol):
    """One explicit handler for a validated versioned outbox intent."""

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess: ...


class OutboxConsumerRegistry:
    """Explicit consumer-name registry with no import-path discovery."""

    def __init__(self) -> None:
        self._consumers: dict[str, OutboxConsumer] = {}

    def register(self, consumer_name: str, consumer: OutboxConsumer) -> None:
        validate_consumer_name(consumer_name)
        if consumer_name in self._consumers:
            raise DuplicateOutboxConsumerError(
                "Outbox consumer is already registered."
            )
        self._consumers[consumer_name] = consumer

    def resolve_optional(self, consumer_name: str) -> OutboxConsumer | None:
        return self._consumers.get(consumer_name)


class OutboxDeliveryFinalization(BaseModel):
    """One atomic delivery-state and Audit Event outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: OutboxDeliveryStatus
    occurred_at: datetime
    next_attempt_at: datetime | None = None
    error_code: str | None = None

    @field_validator("occurred_at", "next_attempt_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("delivery timestamps must be timezone-aware.")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("error_code must be a bounded safe identifier.")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "OutboxDeliveryFinalization":
        if self.status is OutboxDeliveryStatus.DELIVERED:
            if self.next_attempt_at is not None or self.error_code is not None:
                raise ValueError("delivered finalization contains retry state.")
            return self
        if self.status is OutboxDeliveryStatus.RETRY_SCHEDULED:
            if (
                self.next_attempt_at is None
                or self.next_attempt_at <= self.occurred_at
                or self.error_code is None
            ):
                raise ValueError("retry finalization is incomplete.")
            return self
        if self.status is OutboxDeliveryStatus.QUARANTINED:
            if self.next_attempt_at is not None or self.error_code is None:
                raise ValueError("quarantine finalization is incomplete.")
            return self
        raise ValueError("delivery finalization status is not supported.")


class OutboxDispatchRepository(Protocol):
    """Claim and atomically persist one delivery outcome with its audit."""

    async def claim_due(
        self,
        *,
        limit: int,
        worker_identity: str,
        claimed_at: datetime,
        lease_duration: timedelta,
    ) -> tuple[ClaimedOutboxDelivery, ...]: ...

    async def finalize(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        finalization: OutboxDeliveryFinalization,
        audit_event: AuditEvent,
    ) -> OutboxDelivery: ...


class DispatchReport(BaseModel):
    """Safe aggregate result of one bounded dispatch call."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    claimed: int
    delivered: int
    retry_scheduled: int
    quarantined: int
    outcome_codes: tuple[DispatchOutcomeCode, ...]


def validate_outbox_delivery_audit(
    claim: ClaimedOutboxDelivery,
    finalization: OutboxDeliveryFinalization,
    event: AuditEvent,
    *,
    registry: AuditEventRegistry,
) -> AuditEvent:
    """Validate exact delivery lineage before an atomic adapter transition."""

    canonical = registry.validate(event)
    intent = claim.intent
    event_id = intent.event_id
    expected_outcomes = {
        OutboxDeliveryStatus.DELIVERED: AuditOutcome.SUCCEEDED,
        OutboxDeliveryStatus.RETRY_SCHEDULED: AuditOutcome.RETRY_SCHEDULED,
        OutboxDeliveryStatus.QUARANTINED: AuditOutcome.QUARANTINED,
    }
    expected_reasons = {
        OutboxDeliveryStatus.DELIVERED: "delivery.succeeded",
        OutboxDeliveryStatus.RETRY_SCHEDULED: finalization.error_code,
        OutboxDeliveryStatus.QUARANTINED: finalization.error_code,
    }
    if (
        event_id is None
        or canonical.audit_event_id is None
        or canonical.event_type != "outbox.delivery"
        or canonical.schema_version != 1
        or canonical.workspace_id != intent.workspace_id
        or canonical.environment_id != intent.environment_id
        or canonical.origin is not ContextOrigin.WORKER
        or canonical.acting_subject_id is not None
        or canonical.trace_id != intent.trace_id
        or canonical.correlation_id != intent.correlation_id
        or canonical.causation_id != event_id
        or canonical.occurred_at != finalization.occurred_at
        or canonical.outcome is not expected_outcomes[finalization.status]
        or canonical.reason != expected_reasons[finalization.status]
        or type(canonical.payload) is not OutboxDeliveryAuditPayload
        or canonical.payload.outbox_event_id != event_id
        or canonical.payload.attempt_id != claim.lease_token
        or canonical.payload.attempt_number != claim.attempt_number
    ):
        raise ValueError("Outbox delivery Audit Event is invalid.")
    return canonical


class OutboxDispatcher:
    """Deep dispatch module exposing one bounded application operation."""

    def __init__(
        self,
        *,
        repository: OutboxDispatchRepository,
        consumers: OutboxConsumerRegistry,
        audit_events: AuditEventRegistry,
        diagnostic_events: DiagnosticEventRegistry,
        diagnostic_sink: DiagnosticSink,
        service_principal_id: UUID,
        clock: Callable[[], datetime],
        audit_event_id_source: Callable[[], UUID],
        retry_delay: Callable[[int], timedelta],
        maximum_retry_delay: timedelta,
        lease_duration: timedelta,
        handler_timeout: timedelta,
        maximum_attempts: int,
        operational_observer: OperationalObservationEmitter | None = None,
    ) -> None:
        if service_principal_id.int == 0:
            raise ValueError("service_principal_id must be a non-zero UUID.")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        if handler_timeout <= timedelta(0) or handler_timeout >= lease_duration:
            raise ValueError("handler_timeout must be positive and shorter than lease.")
        if maximum_retry_delay <= timedelta(0):
            raise ValueError("maximum_retry_delay must be positive.")
        if isinstance(maximum_attempts, bool) or maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive.")
        self._repository = repository
        self._consumers = consumers
        self._audit_events = audit_events
        self._diagnostic_events = diagnostic_events
        self._diagnostic_sink = diagnostic_sink
        self._service_principal_id = service_principal_id
        self._clock = clock
        self._audit_event_id_source = audit_event_id_source
        self._retry_delay = retry_delay
        self._maximum_retry_delay = maximum_retry_delay
        self._lease_duration = lease_duration
        self._handler_timeout = handler_timeout
        self._maximum_attempts = maximum_attempts
        self._operational_observer = operational_observer

    async def dispatch_batch(
        self,
        *,
        limit: int,
        worker_identity: str,
    ) -> DispatchReport:
        try:
            claims = await self._repository.claim_due(
                limit=limit,
                worker_identity=worker_identity,
                claimed_at=self._clock(),
                lease_duration=self._lease_duration,
            )
        except PersistenceError:
            raise
        except Exception:
            raise UnexpectedPersistenceError(
                "Outbox dispatch persistence failed."
            ) from None
        outcomes = await asyncio.gather(
            *(self._dispatch_claim(claim) for claim in claims)
        )
        return DispatchReport(
            claimed=len(claims),
            delivered=outcomes.count(DispatchOutcomeCode.DELIVERED),
            retry_scheduled=outcomes.count(DispatchOutcomeCode.RETRY_SCHEDULED),
            quarantined=outcomes.count(DispatchOutcomeCode.QUARANTINED),
            outcome_codes=tuple(outcomes),
        )

    async def _dispatch_claim(
        self,
        claim: ClaimedOutboxDelivery,
    ) -> DispatchOutcomeCode:
        intent = claim.intent
        event_id = intent.event_id
        if event_id is None:
            raise RuntimeError("claimed delivery has no event identity")
        diagnostic_context = DiagnosticContext(
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=event_id,
        )
        if self._operational_observer is not None and intent.environment_id is not None:
            await self._operational_observer.emit(
                workspace_id=intent.workspace_id,
                environment_id=intent.environment_id,
                diagnostic_context=diagnostic_context,
                component=OperationalComponent.OUTBOX,
                stage=OperationalStage.DISPATCH,
                operation=OperationalOperation.OUTBOX_DELIVERY,
                outcome=OperationalObservationOutcome.STARTED,
                attempt_number=claim.attempt_number,
            )
        if type(intent) is UnresolvedOutboxIntent:
            finalization = OutboxDeliveryFinalization(
                status=OutboxDeliveryStatus.QUARANTINED,
                occurred_at=self._clock(),
                error_code="delivery.unknown_event",
            )
        elif claim.consumer_name is None:
            finalization = OutboxDeliveryFinalization(
                status=OutboxDeliveryStatus.QUARANTINED,
                occurred_at=self._clock(),
                error_code="delivery.unknown_route",
            )
        else:
            consumer = self._consumers.resolve_optional(claim.consumer_name)
            if consumer is None:
                finalization = OutboxDeliveryFinalization(
                    status=OutboxDeliveryStatus.QUARANTINED,
                    occurred_at=self._clock(),
                    error_code="delivery.unknown_consumer",
                )
            else:
                try:
                    result = await asyncio.wait_for(
                        consumer.consume(intent, diagnostic_context),
                        timeout=self._handler_timeout.total_seconds(),
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    finalization = self._retry_or_exhaust(
                        claim,
                        failed_at=self._clock(),
                        error_code="delivery.handler_timeout",
                    )
                except RetryableOutboxConsumerFailure as failure:
                    finalization = self._retry_or_exhaust(
                        claim,
                        failed_at=self._clock(),
                        error_code=failure.code,
                    )
                except TerminalOutboxConsumerFailure as failure:
                    finalization = OutboxDeliveryFinalization(
                        status=OutboxDeliveryStatus.QUARANTINED,
                        occurred_at=self._clock(),
                        error_code=failure.code,
                    )
                except Exception:
                    finalization = OutboxDeliveryFinalization(
                        status=OutboxDeliveryStatus.QUARANTINED,
                        occurred_at=self._clock(),
                        error_code="delivery.handler_unexpected",
                    )
                else:
                    if (
                        type(result) is not OutboxConsumerSuccess
                        or result.diagnostic_context != diagnostic_context
                    ):
                        finalization = OutboxDeliveryFinalization(
                            status=OutboxDeliveryStatus.QUARANTINED,
                            occurred_at=self._clock(),
                            error_code="delivery.context_mismatch",
                        )
                    else:
                        finalization = OutboxDeliveryFinalization(
                            status=OutboxDeliveryStatus.DELIVERED,
                            occurred_at=self._clock(),
                        )
        return await self._commit_outcome(
            claim,
            diagnostic_context=diagnostic_context,
            finalization=finalization,
        )

    def _retry_or_exhaust(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        failed_at: datetime,
        error_code: str,
    ) -> OutboxDeliveryFinalization:
        if claim.attempt_number >= self._maximum_attempts:
            _LOGGER.warning(
                "Outbox delivery exhausted attempts after safe failure code: %s",
                error_code,
            )
            return OutboxDeliveryFinalization(
                status=OutboxDeliveryStatus.QUARANTINED,
                occurred_at=failed_at,
                error_code="delivery.attempts_exhausted",
            )
        retry_delay = self._retry_delay(claim.attempt_number)
        if retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be positive.")
        retry_delay = min(retry_delay, self._maximum_retry_delay)
        return OutboxDeliveryFinalization(
            status=OutboxDeliveryStatus.RETRY_SCHEDULED,
            occurred_at=failed_at,
            next_attempt_at=failed_at + retry_delay,
            error_code=error_code,
        )

    async def _commit_outcome(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        diagnostic_context: DiagnosticContext,
        finalization: OutboxDeliveryFinalization,
    ) -> DispatchOutcomeCode:
        intent = claim.intent
        event_id = intent.event_id
        if event_id is None:
            raise RuntimeError("claimed delivery has no event identity")
        if finalization.status is OutboxDeliveryStatus.DELIVERED:
            audit_outcome = AuditOutcome.SUCCEEDED
            reason = "delivery.succeeded"
            diagnostic_state = OutboxDeliveryDiagnosticState.DELIVERED
            severity = DiagnosticSeverity.INFO
            report_code = DispatchOutcomeCode.DELIVERED
            retry_delay = None
        elif finalization.status is OutboxDeliveryStatus.RETRY_SCHEDULED:
            audit_outcome = AuditOutcome.RETRY_SCHEDULED
            assert finalization.error_code is not None
            assert finalization.next_attempt_at is not None
            reason = finalization.error_code
            diagnostic_state = OutboxDeliveryDiagnosticState.RETRY_SCHEDULED
            severity = DiagnosticSeverity.WARNING
            report_code = DispatchOutcomeCode.RETRY_SCHEDULED
            retry_delay = finalization.next_attempt_at - finalization.occurred_at
        else:
            audit_outcome = AuditOutcome.QUARANTINED
            assert finalization.error_code is not None
            reason = finalization.error_code
            diagnostic_state = OutboxDeliveryDiagnosticState.QUARANTINED
            severity = DiagnosticSeverity.ERROR
            report_code = DispatchOutcomeCode.QUARANTINED
            retry_delay = None
        audit_event = self._audit_events.build_event(
            audit_event_id=self._audit_event_id_source(),
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            event_type="outbox.delivery",
            schema_version=1,
            payload=OutboxDeliveryAuditPayload(
                outbox_event_id=event_id,
                attempt_id=claim.lease_token,
                attempt_number=claim.attempt_number,
            ),
            origin=ContextOrigin.WORKER,
            acting_subject_id=None,
            service_principal_id=self._service_principal_id,
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=event_id,
            occurred_at=finalization.occurred_at,
            outcome=audit_outcome,
            reason=reason,
            producer_deduplication_id=(
                f"delivery.{event_id.hex}.{claim.attempt_number}."
                f"{audit_outcome.value}"
            ),
        )
        try:
            await self._repository.finalize(
                claim,
                finalization=finalization,
                audit_event=audit_event,
            )
        except PersistenceError:
            await self._observe_persistence_failure(
                claim, diagnostic_context=diagnostic_context
            )
            raise
        except Exception:
            await self._observe_persistence_failure(
                claim, diagnostic_context=diagnostic_context
            )
            raise UnexpectedPersistenceError(
                "Outbox dispatch persistence failed."
            ) from None
        diagnostic = self._diagnostic_events.build_event(
            event_type="outbox.delivery",
            schema_version=1,
            severity=severity,
            occurred_at=finalization.occurred_at,
            diagnostic_context=diagnostic_context,
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            payload=OutboxDeliveryDiagnosticPayload(
                delivery_state=diagnostic_state,
                attempt_number=claim.attempt_number,
                retry_delay=retry_delay,
            ),
        )
        await emit_diagnostic_safely(self._diagnostic_sink, diagnostic)
        if self._operational_observer is not None and intent.environment_id is not None:
            operational_outcome = {
                OutboxDeliveryStatus.DELIVERED: OperationalObservationOutcome.SUCCEEDED,
                OutboxDeliveryStatus.RETRY_SCHEDULED: OperationalObservationOutcome.RETRY_SCHEDULED,
                OutboxDeliveryStatus.QUARANTINED: OperationalObservationOutcome.QUARANTINED,
            }[finalization.status]
            failure_code = None
            if finalization.error_code is not None:
                failure_code = _operational_failure_code(finalization.error_code)
            await self._operational_observer.emit(
                workspace_id=intent.workspace_id,
                environment_id=intent.environment_id,
                diagnostic_context=diagnostic_context,
                component=OperationalComponent.WORKER,
                stage=OperationalStage.DISPATCH,
                operation=OperationalOperation.OUTBOX_DELIVERY,
                outcome=operational_outcome,
                safe_failure_code=failure_code,
                attempt_number=claim.attempt_number,
            )
        return report_code

    async def _observe_persistence_failure(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        intent = claim.intent
        if self._operational_observer is None or intent.environment_id is None:
            return
        await self._operational_observer.emit(
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            diagnostic_context=diagnostic_context,
            component=OperationalComponent.WORKER,
            stage=OperationalStage.DISPATCH,
            operation=OperationalOperation.OUTBOX_DELIVERY,
            outcome=OperationalObservationOutcome.FAILED,
            safe_failure_code=SafeOperationalFailureCode.PERSISTENCE_UNAVAILABLE,
            attempt_number=claim.attempt_number,
        )


def _operational_failure_code(error_code: str) -> SafeOperationalFailureCode:
    if error_code == "delivery.handler_timeout":
        return SafeOperationalFailureCode.TIMEOUT
    if error_code == "delivery.attempts_exhausted":
        return SafeOperationalFailureCode.RETRY_EXHAUSTED
    if error_code == "delivery.context_mismatch":
        return SafeOperationalFailureCode.CONTEXT_MISMATCH
    if error_code in {
        "delivery.unknown_event",
        "delivery.unknown_route",
        "delivery.unknown_consumer",
    }:
        return SafeOperationalFailureCode.CONTEXT_INVALID
    return SafeOperationalFailureCode.INTERNAL_FAILURE


__all__ = [
    "DispatchOutcomeCode",
    "DispatchReport",
    "DuplicateOutboxConsumerError",
    "OutboxConsumer",
    "OutboxConsumerRegistry",
    "OutboxConsumerSuccess",
    "OutboxDeliveryFinalization",
    "OutboxDispatchRepository",
    "OutboxDispatcher",
    "RetryableOutboxConsumerFailure",
    "TerminalOutboxConsumerFailure",
    "validate_outbox_delivery_audit",
]
