"""One bounded local Agent execution over typed Artifact handoffs."""

from __future__ import annotations

from collections.abc import Callable
from asyncio import CancelledError
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from spine.agents.runtime.cancellation import (
    CancellationScope,
    CooperativeCancellationError,
)
from spine.agents.runtime.artifacts import (
    ArtifactDraft,
    ArtifactPersistence,
    ArtifactReadError,
    CapabilityArtifactSchema,
)
from spine.agents.runtime.contracts import (
    AgentExecutionContext,
    AgentRequest,
    AgentResponse,
    agent_result_from_response,
)
from spine.agents.runtime.diagnostics import (
    AgentRunDiagnosticState,
    AgentRuntimeDiagnosticEmitter,
)
from spine.agents.runtime.protocol import AgentHandler
from spine.agents.runtime.ports import CancellationController
from spine.application.diagnostics import (
    AuditEventRegistry,
    AuditWriter,
    DiagnosticEventRegistry,
    Retryability,
    StructuredError,
)
from spine.domain.agents import AgentInvocation, AgentResult, AgentRunStatus, AgentVersion
from spine.domain.artifacts import Artifact, ArtifactRef, ArtifactState
from spine.domain.common import ActorKind, ActorRef


class _FailureCategory(str, Enum):
    CANCELLATION = "cancellation"
    REQUEST = "request"
    INTERNAL = "internal"
    PERSISTENCE = "persistence"


class LocalAgentRuntime:
    """Execute one already-pinned and admitted local handler.

    Binding resolution and trusted context construction are deliberately
    outside this object. Production composition supplies the guarded handler
    created by that boundary; deterministic contract tests may use a structural
    fake with an equivalent context.
    """

    def __init__(
        self,
        *,
        context: AgentExecutionContext,
        handler: AgentHandler[Any, Any],
        artifacts: ArtifactPersistence,
        schemas: CapabilityArtifactSchema,
        diagnostic_registry: DiagnosticEventRegistry,
        audit_registry: AuditEventRegistry,
        audit_writer: AuditWriter,
        clock: Callable[[], datetime],
        cancellation_controller: CancellationController,
    ) -> None:
        if not isinstance(context, AgentExecutionContext):
            raise TypeError("local runtime context is invalid")
        if not isinstance(handler, AgentHandler):
            raise TypeError("local runtime handler is invalid")
        if not isinstance(artifacts, ArtifactPersistence):
            raise TypeError("local runtime artifact persistence is invalid")
        if not isinstance(schemas, CapabilityArtifactSchema):
            raise TypeError("local runtime artifact schemas are invalid")
        if schemas.schema_identity != context.schema_identity:
            raise ValueError("local runtime artifact schema mapping is incompatible")
        if not isinstance(diagnostic_registry, DiagnosticEventRegistry):
            raise TypeError("local runtime diagnostic registry is invalid")
        if not isinstance(audit_registry, AuditEventRegistry):
            raise TypeError("local runtime audit registry is invalid")
        if not callable(clock):
            raise TypeError("local runtime clock is invalid")
        self._context = context
        self._handler = handler
        self._artifacts = artifacts
        self._schemas = schemas
        self._cancellation_scope = CancellationScope(
            workspace_id=context.workspace_id,
            environment_id=context.environment_id,
            step_run_id=context.step_run_id,
            agent_run_id=context.agent_run_id,
        )
        if not isinstance(cancellation_controller, CancellationController):
            raise TypeError("local runtime cancellation control is unavailable")
        if cancellation_controller.scope != self._cancellation_scope:
            raise ValueError("local runtime cancellation scope is incompatible")
        if cancellation_controller.token is not context.cancellation:
            raise ValueError("local runtime cancellation token is incompatible")
        self._cancellation = cancellation_controller
        self._events = AgentRuntimeDiagnosticEmitter(
            context=context,
            diagnostic_registry=diagnostic_registry,
            audit_registry=audit_registry,
            audit_writer=audit_writer,
            clock=clock,
        )

    async def invoke(
        self,
        version: AgentVersion,
        invocation: AgentInvocation,
    ) -> AgentResult:
        """Load, validate, execute and persist exactly one technical attempt."""

        await self._events.observe_run(AgentRunDiagnosticState.STARTED)

        try:
            self._context.cancellation.raise_if_cancelled()
            self._validate_invocation(version, invocation)
            inputs = await self._load_inputs(invocation.input_artifact_ids)
            typed_input = self._schemas.load_input(inputs)
        except CooperativeCancellationError:
            return await self._finish(self._cancelled_result())
        except CancelledError:
            await self._events.observe_run(AgentRunDiagnosticState.CANCELLED)
            raise
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED)
            )

        request = AgentRequest(
            capability=self._context.capability,
            schema_identity=self._context.schema_identity,
            input=typed_input,
            constraints=invocation.constraints,
        )
        try:
            self._context.cancellation.raise_if_cancelled()
            response = await self._handler.invoke(request, self._context)
            self._context.cancellation.raise_if_cancelled()
        except CooperativeCancellationError:
            return await self._finish(self._cancelled_result())
        except CancelledError:
            return await self._finish(self._cancelled_result())
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.INTERNAL, AgentRunStatus.FAILED)
            )

        if not isinstance(response, AgentResponse):
            return await self._finish(
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED)
            )
        if response.schema_identity != self._context.schema_identity:
            return await self._finish(
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED)
            )
        if response.proposed_action_ids:
            # Proposed actions need their own persisted, lineage-bearing boundary.
            # Until that contract exists, never expose handler-fabricated IDs.
            return await self._finish(
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED)
            )
        try:
            self._schemas.check_metadata(response)
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED)
            )
        if response.error is not None:
            return await self._finish(
                agent_result_from_response(
                    response,
                    trace_ref=self._trace_ref,
                )
            )

        try:
            self._context.cancellation.raise_if_cancelled()
            output = self._schemas.load_output(response.output)
        except CooperativeCancellationError:
            return await self._finish(self._cancelled_result())
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED)
            )

        draft = ArtifactDraft(
            workspace_id=self._context.workspace_id,
            environment_id=self._context.environment_id,
            work_item_id=self._context.work_item_id,
            produced_by=ActorRef(
                kind=ActorKind.AGENT,
                id=self._context.agent_version.agent_id,
            ),
            produced_by_agent_version_id=self._context.agent_version.id,
            artifact_schema=self._context.capability.output_schema,
            content=self._schemas.dump(output),
            evidence_refs=response.evidence_refs,
            upstream_artifact_ids=invocation.input_artifact_ids,
        )
        try:
            if not self._cancellation.seal(self._cancellation_scope):
                return await self._finish(self._cancelled_result())
            completion_audit = self._events.build_run_audit_event(
                AgentRunDiagnosticState.COMPLETED
            )
            references = await self._artifacts.persist_outputs(
                (draft,),
                idempotency_key=invocation.idempotency_key,
                audit_event=completion_audit,
                validate_references=self._validate_persisted_references,
            )
        except CooperativeCancellationError:
            return await self._finish(self._cancelled_result())
        except CancelledError:
            await self._events.observe_run(AgentRunDiagnosticState.CANCELLED)
            raise
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.PERSISTENCE, AgentRunStatus.FAILED)
            )

        result = agent_result_from_response(
            response,
            output_artifact_ids=tuple(reference.id for reference in references),
            trace_ref=self._trace_ref,
        )
        await self._events.observe_run_diagnostic(
            AgentRunDiagnosticState.COMPLETED
        )
        return result

    async def cancel(
        self,
        step_run_id: UUID,
        *,
        scope: CancellationScope,
    ) -> None:
        """Request cancellation of this exact run through the existing surface."""

        if step_run_id != scope.step_run_id or scope != self._cancellation_scope:
            raise ValueError("cancellation scope is invalid")
        if self._cancellation.request_cancel(scope):
            await self._events.observe_run(
                AgentRunDiagnosticState.CANCELLATION_REQUESTED
            )

    @property
    def _trace_ref(self) -> str:
        return str(self._context.diagnostic_context.trace_id)

    def _validate_invocation(
        self,
        version: AgentVersion,
        invocation: AgentInvocation,
    ) -> None:
        context = self._context
        if version != context.agent_version:
            raise ValueError("agent version is not pinned")
        if invocation.capability != context.capability.key:
            raise ValueError("capability scope is invalid")
        if invocation.work_item_id != context.work_item_id:
            raise ValueError("work item scope is invalid")
        if invocation.step_run_id != context.step_run_id:
            raise ValueError("step scope is invalid")
        if invocation.idempotency_key != context.idempotency_key:
            raise ValueError("idempotency scope is invalid")
        if invocation.input_artifact_ids != context.input_artifact_ids:
            raise ValueError("artifact lineage is invalid")
        if len(set(invocation.input_artifact_ids)) != len(invocation.input_artifact_ids):
            raise ValueError("artifact lineage is invalid")

    async def _load_inputs(self, artifact_ids: tuple[UUID, ...]) -> tuple[Artifact, ...]:
        loaded: list[Artifact] = []
        for artifact_id in artifact_ids:
            try:
                artifact = await self._artifacts.load(
                    artifact_id,
                    workspace_id=self._context.workspace_id,
                    environment_id=self._context.environment_id,
                    work_item_id=self._context.work_item_id,
                )
            except CancelledError:
                raise
            except Exception as error:
                raise ArtifactReadError("input artifact is unavailable") from error
            if artifact is None:
                raise ArtifactReadError("input artifact is unavailable")
            self._validate_input_artifact(artifact, artifact_id)
            try:
                await self._artifacts.verify_lineage(
                    artifact,
                    workspace_id=self._context.workspace_id,
                    environment_id=self._context.environment_id,
                    work_item_id=self._context.work_item_id,
                )
            except CancelledError:
                raise
            except Exception as error:
                raise ArtifactReadError("input artifact is unavailable") from error
            loaded.append(artifact)
        return tuple(loaded)

    def _validate_input_artifact(self, artifact: Artifact, expected_id: UUID) -> None:
        context = self._context
        if artifact.id != expected_id:
            raise ArtifactReadError("input artifact is unavailable")
        if artifact.workspace_id != context.workspace_id:
            raise ArtifactReadError("input artifact is unavailable")
        if artifact.environment_id != context.environment_id:
            raise ArtifactReadError("input artifact is unavailable")
        if artifact.work_item_id != context.work_item_id:
            raise ArtifactReadError("input artifact is unavailable")
        if artifact.state is not ArtifactState.ACTIVE:
            raise ArtifactReadError("input artifact is unavailable")
        if artifact.artifact_schema != context.capability.input_schema:
            raise ArtifactReadError("input artifact is unavailable")
        if artifact.id in artifact.upstream_artifact_ids:
            raise ArtifactReadError("input artifact is unavailable")

    def _validate_persisted_references(
        self,
        references: tuple[ArtifactRef, ...],
    ) -> None:
        if not isinstance(references, tuple) or len(references) != 1:
            raise ValueError("output persistence receipt is invalid")
        reference = references[0]
        schema = self._context.capability.output_schema
        if not isinstance(reference, ArtifactRef):
            raise ValueError("output persistence receipt is invalid")
        if reference.id.int == 0:
            raise ValueError("output persistence receipt is invalid")
        if (
            reference.schema_name != schema.name
            or reference.schema_version != schema.version
        ):
            raise ValueError("output persistence receipt is invalid")

    def _safe_failure(
        self,
        category: _FailureCategory,
        status: AgentRunStatus,
    ) -> AgentResult:
        if category is _FailureCategory.CANCELLATION:
            code = "spine.execution.cancelled"
            message = "Execution was cancelled."
        elif category is _FailureCategory.PERSISTENCE:
            code = "spine.persistence.failure"
            message = "Persistence operation failed."
        elif category is _FailureCategory.INTERNAL:
            code = "spine.internal.unexpected"
            message = "An unexpected internal error occurred."
        else:
            code = "spine.request.rejected"
            message = "The request was rejected."
        response: AgentResponse[Any] = AgentResponse(
            schema_identity=self._context.schema_identity,
            error=StructuredError(
                code=code,
                safe_message=message,
                retryability=Retryability.NEVER,
                trace_id=self._context.diagnostic_context.trace_id,
            ),
        )
        return agent_result_from_response(
            response,
            status=status,
            trace_ref=self._trace_ref,
        )

    def _cancelled_result(self) -> AgentResult:
        return self._safe_failure(
            _FailureCategory.CANCELLATION,
            AgentRunStatus.CANCELLED,
        )

    async def _finish(self, result: AgentResult) -> AgentResult:
        self._cancellation.seal(self._cancellation_scope)
        state = {
            AgentRunStatus.SUCCEEDED: AgentRunDiagnosticState.COMPLETED,
            AgentRunStatus.FAILED: AgentRunDiagnosticState.FAILED,
            AgentRunStatus.CANCELLED: AgentRunDiagnosticState.CANCELLED,
        }[result.status]
        await self._events.observe_run(state)
        return result


__all__ = ["LocalAgentRuntime"]
