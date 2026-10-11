"""Registered disclosure-safe observations for one local agent execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from spine.agents.runtime.contracts import AgentExecutionContext
from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    AuditObjectReference,
    AuditOutcome,
    AuditWriter,
    DiagnosticEventRegistry,
    DiagnosticSeverity,
    emit_diagnostic_safely,
)
from spine.domain.common import ContextOrigin

_AGENT_RUN_EVENT_TYPE = "agent.run"
_AGENT_RETRY_EVENT_TYPE = "agent.retry"
_AGENT_TOOL_EVENT_TYPE = "agent.tool"
_AGENT_USAGE_EVENT_TYPE = "agent.usage"
_AGENT_RUN_OBJECT_TYPE = "agent_run"


class AgentRuntimeAuditError(RuntimeError):
    """Disclosure-safe failure to append a required runtime audit event."""


class AgentRunDiagnosticState(str, Enum):
    """Safe lifecycle states observable outside the canonical result."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLED = "cancelled"


class AgentRetryDiagnosticState(str, Enum):
    """Safe bounded-retry observations."""

    SCHEDULED = "scheduled"
    EXHAUSTED = "exhausted"


class AgentRetryReason(str, Enum):
    """Closed retry categories; provider error text is never accepted."""

    PROVIDER_UNAVAILABLE = "provider.unavailable"
    RATE_LIMITED = "provider.rate_limited"
    TOOL_UNAVAILABLE = "tool.unavailable"
    TRANSIENT_INTERNAL = "internal.transient"


class AgentToolDiagnosticState(str, Enum):
    """Safe tool-call outcomes without arguments or provider data."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentUsageKind(str, Enum):
    """Allowlisted resource counters emitted by local adapters."""

    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    TOOL_CALLS = "tool_calls"


class AgentExecutionObservationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_item_id: UUID
    workflow_run_id: UUID
    step_run_id: UUID
    agent_run_id: UUID
    attempt_id: UUID | None
    agent_version_id: UUID
    capability_id: UUID


class AgentRunDiagnosticPayload(AgentExecutionObservationIdentity):
    """One safe local-run lifecycle observation."""

    run_state: AgentRunDiagnosticState


class AgentRetryDiagnosticPayload(AgentExecutionObservationIdentity):
    """One safe bounded-retry observation."""

    retry_state: AgentRetryDiagnosticState
    attempt_number: int = Field(ge=1)
    retry_reason_code: AgentRetryReason


class AgentToolDiagnosticPayload(AgentExecutionObservationIdentity):
    """One safe Tool Gateway observation."""

    tool_call_id: UUID
    tool_state: AgentToolDiagnosticState


class AgentUsageDiagnosticPayload(AgentExecutionObservationIdentity):
    """One safe resource counter without provider payloads."""

    usage_kind: AgentUsageKind
    quantity: int = Field(ge=0)


class AgentRunAuditPayload(AgentExecutionObservationIdentity):
    """Registered audit payload for a local agent-run transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_state: AgentRunDiagnosticState


def build_agent_runtime_diagnostic_registry() -> DiagnosticEventRegistry:
    """Build the sealed Issue #4 registry used by the local runtime."""

    registry = DiagnosticEventRegistry()
    for event_type, payload_type in (
        (_AGENT_RUN_EVENT_TYPE, AgentRunDiagnosticPayload),
        (_AGENT_RETRY_EVENT_TYPE, AgentRetryDiagnosticPayload),
        (_AGENT_TOOL_EVENT_TYPE, AgentToolDiagnosticPayload),
        (_AGENT_USAGE_EVENT_TYPE, AgentUsageDiagnosticPayload),
    ):
        registry.register(
            event_type=event_type,
            schema_version=1,
            payload_type=payload_type,
        )
    registry.seal()
    return registry


def build_agent_runtime_audit_registry() -> AuditEventRegistry:
    """Build the sealed Issue #4 audit registry used by the local runtime."""

    registry = AuditEventRegistry()
    registry.register(
        event_type=_AGENT_RUN_EVENT_TYPE,
        schema_version=1,
        payload_type=AgentRunAuditPayload,
        allowed_outcomes=frozenset(
            {
                AuditOutcome.ACCEPTED,
                AuditOutcome.COMPLETED,
                AuditOutcome.REJECTED,
            }
        ),
        requires_target=True,
    )
    registry.seal()
    return registry


class AgentRuntimeDiagnosticEmitter:
    """Translate local observations to the shared diagnostic and audit ports."""

    def __init__(
        self,
        *,
        context: AgentExecutionContext,
        diagnostic_registry: DiagnosticEventRegistry,
        audit_registry: AuditEventRegistry,
        audit_writer: AuditWriter,
        clock: Callable[[], datetime],
    ) -> None:
        self._context = context
        self._diagnostic_registry = diagnostic_registry
        self._audit_registry = audit_registry
        self._audit_writer = audit_writer
        self._clock = clock

    async def observe_run(self, state: AgentRunDiagnosticState) -> None:
        await self.observe_run_diagnostic(state)
        if state is not AgentRunDiagnosticState.CANCELLATION_REQUESTED:
            await self._append_run_audit(state)

    async def observe_run_diagnostic(self, state: AgentRunDiagnosticState) -> None:
        """Emit only the non-canonical observation for a run transition."""

        payload = AgentRunDiagnosticPayload(
            **self._diagnostic_identity(),
            run_state=state,
        )
        await self._emit_diagnostic(
            event_type=_AGENT_RUN_EVENT_TYPE,
            payload=payload,
            severity=(
                DiagnosticSeverity.ERROR
                if state is AgentRunDiagnosticState.FAILED
                else DiagnosticSeverity.INFO
            ),
        )

    async def observe_retry(
        self,
        *,
        state: AgentRetryDiagnosticState,
        attempt_number: int,
        retry_reason_code: AgentRetryReason,
    ) -> None:
        await self._emit_diagnostic(
            event_type=_AGENT_RETRY_EVENT_TYPE,
            payload=AgentRetryDiagnosticPayload(
                **self._diagnostic_identity(),
                retry_state=state,
                attempt_number=attempt_number,
                retry_reason_code=retry_reason_code,
            ),
            severity=DiagnosticSeverity.WARNING,
        )

    async def observe_tool(
        self,
        *,
        tool_call_id: UUID,
        state: AgentToolDiagnosticState,
    ) -> None:
        await self._emit_diagnostic(
            event_type=_AGENT_TOOL_EVENT_TYPE,
            payload=AgentToolDiagnosticPayload(
                **self._diagnostic_identity(),
                tool_call_id=tool_call_id,
                tool_state=state,
            ),
            severity=(
                DiagnosticSeverity.ERROR
                if state is AgentToolDiagnosticState.FAILED
                else DiagnosticSeverity.INFO
            ),
        )

    async def observe_usage(self, *, kind: AgentUsageKind, quantity: int) -> None:
        await self._emit_diagnostic(
            event_type=_AGENT_USAGE_EVENT_TYPE,
            payload=AgentUsageDiagnosticPayload(
                **self._diagnostic_identity(),
                usage_kind=kind,
                quantity=quantity,
            ),
            severity=DiagnosticSeverity.INFO,
        )

    def _diagnostic_identity(self) -> dict[str, object]:
        context = self._context
        return {
            "work_item_id": context.work_item_id,
            "workflow_run_id": context.workflow_run_id,
            "step_run_id": context.step_run_id,
            "agent_run_id": context.agent_run_id,
            "attempt_id": context.attempt_id,
            "agent_version_id": context.agent_version.id,
            "capability_id": context.capability.id,
        }

    async def _emit_diagnostic(
        self,
        *,
        event_type: str,
        payload: BaseModel,
        severity: DiagnosticSeverity,
    ) -> None:
        context = self._context
        event = self._diagnostic_registry.build_event(
            event_type=event_type,
            schema_version=1,
            severity=severity,
            occurred_at=self._clock(),
            diagnostic_context=context.diagnostic_context,
            workspace_id=context.workspace_id,
            environment_id=context.environment_id,
            payload=payload,
        )
        await emit_diagnostic_safely(context.event_sink, event)

    def build_run_audit_event(self, state: AgentRunDiagnosticState) -> AuditEvent:
        """Build the required event for an owning atomic persistence boundary."""

        context = self._context
        outcome = {
            AgentRunDiagnosticState.STARTED: AuditOutcome.ACCEPTED,
            AgentRunDiagnosticState.COMPLETED: AuditOutcome.COMPLETED,
            AgentRunDiagnosticState.FAILED: AuditOutcome.REJECTED,
            AgentRunDiagnosticState.CANCELLED: AuditOutcome.COMPLETED,
        }[state]
        return self._audit_registry.build_event(
            workspace_id=context.workspace_id,
            environment_id=context.environment_id,
            event_type=_AGENT_RUN_EVENT_TYPE,
            schema_version=1,
            payload=AgentRunAuditPayload(
                **self._diagnostic_identity(),
                run_state=state,
            ),
            origin=(
                ContextOrigin.INTERACTIVE
                if context.trusted_identity.acting_subject_id is not None
                else ContextOrigin.WORKER
            ),
            acting_subject_id=context.trusted_identity.acting_subject_id,
            service_principal_id=context.trusted_identity.service_principal_id,
            trace_id=context.diagnostic_context.trace_id,
            correlation_id=context.diagnostic_context.correlation_id,
            causation_id=context.diagnostic_context.causation_id,
            occurred_at=self._clock(),
            target=AuditObjectReference(
                object_type=_AGENT_RUN_OBJECT_TYPE,
                object_id=context.agent_run_id,
                schema_version=1,
            ),
            outcome=outcome,
            reason=f"agent_runtime.{state.value}",
            producer_deduplication_id=(
                f"agent_run.{context.agent_run_id.hex}."
                f"attempt.{context.attempt_id.hex if context.attempt_id else 'none'}."
                f"{state.value}"
            ),
        )

    async def _append_run_audit(self, state: AgentRunDiagnosticState) -> None:
        event = self.build_run_audit_event(state)
        try:
            await self._audit_writer.append(event)
        except Exception:
            raise AgentRuntimeAuditError("Agent runtime audit is unavailable.") from None


__all__ = [
    "AgentRetryDiagnosticPayload",
    "AgentRetryReason",
    "AgentRetryDiagnosticState",
    "AgentExecutionObservationIdentity",
    "AgentRunAuditPayload",
    "AgentRunDiagnosticPayload",
    "AgentRunDiagnosticState",
    "AgentRuntimeAuditError",
    "AgentRuntimeDiagnosticEmitter",
    "AgentToolDiagnosticPayload",
    "AgentToolDiagnosticState",
    "AgentUsageDiagnosticPayload",
    "AgentUsageKind",
    "build_agent_runtime_audit_registry",
    "build_agent_runtime_diagnostic_registry",
]
