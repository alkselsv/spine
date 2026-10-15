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
    InvocationOutputCheckpoint,
    InvocationReplayRecord,
)
from spine.agents.runtime.contracts import (
    AgentExecutionContext,
    AgentRequest,
    AgentResponse,
    agent_result_from_response,
)
from spine.agents.runtime.diagnostics import (
    AgentRetryDiagnosticState,
    AgentRunDiagnosticState,
    AgentRuntimeDiagnosticEmitter,
)
from spine.agents.runtime.idempotency import (
    AGENT_INVOCATION_OPERATION_SCHEMA_VERSION,
    AGENT_INVOCATION_RECORD_SCHEMA_VERSION,
    digest_agent_invocation,
)
from spine.agents.runtime.protocol import AgentHandler
from spine.agents.runtime.ports import CancellationController, RetryScheduler
from spine.application.diagnostics import (
    AuditEventRegistry,
    AuditWriter,
    DiagnosticEventRegistry,
    Retryability,
    StructuredError,
)
from spine.application.persistence import (
    IdempotencyConflictError,
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.domain.agents import (
    AgentInvocation,
    AgentResult,
    AgentRetrySafety,
    AgentRunStatus,
    AgentVersion,
)
from spine.domain.artifacts import Artifact, ArtifactRef, ArtifactState
from spine.domain.common import ActorKind, ActorRef


class _FailureCategory(str, Enum):
    CANCELLATION = "cancellation"
    IDEMPOTENCY = "idempotency"
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
        retry_scheduler: RetryScheduler | None = None,
        attempt_id_factory: Callable[[int], UUID] | None = None,
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
        if retry_scheduler is not None and not isinstance(retry_scheduler, RetryScheduler):
            raise TypeError("local retry scheduler is invalid")
        if attempt_id_factory is not None and not callable(attempt_id_factory):
            raise TypeError("local retry attempt identity factory is invalid")
        self._retry_scheduler = retry_scheduler
        self._attempt_id_factory = attempt_id_factory
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
        """Load, validate, execute and persist one bounded local invocation."""

        await self._events.observe_run(AgentRunDiagnosticState.STARTED)

        try:
            self._context.cancellation.raise_if_cancelled()
            self._validate_invocation(version, invocation)
            retry_policy = self._context.retry_policy
            if retry_policy.maximum_attempts > 1 and (
                self._retry_scheduler is None or self._attempt_id_factory is None
            ):
                raise ValueError("local retry dependencies are unavailable")
            if retry_policy.maximum_attempts > 1:
                assert self._attempt_id_factory is not None
                current_attempt_id = self._attempt_id_factory(1)
                if (
                    not isinstance(current_attempt_id, UUID)
                    or current_attempt_id.int == 0
                ):
                    raise ValueError("local retry attempt identity is invalid")
            else:
                current_attempt_id = self._context.attempt_id
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

        try:
            invocation_digest = digest_agent_invocation(
                context=self._context,
                invocation=invocation,
                validated_input=typed_input,
            )
            result_reference = OpaqueResultReference(
                result_type="agent_invocation",
                result_id=self._context.agent_run_id,
                schema_version=AGENT_INVOCATION_RECORD_SCHEMA_VERSION,
            )
            claim = await self._artifacts.claim_invocation(
                workspace_id=self._context.workspace_id,
                environment_id=self._context.environment_id,
                operation_schema_version=AGENT_INVOCATION_OPERATION_SCHEMA_VERSION,
                key=IdempotencyKey(invocation.idempotency_key),
                digest=invocation_digest,
            )
            if isinstance(claim, IdempotencyReplay):
                if claim.result != result_reference:
                    raise IdempotencyConflictError(
                        "Idempotency result cannot be resolved."
                    )
                replayed = await self._artifacts.load_invocation_result(
                    claim,
                    workspace_id=self._context.workspace_id,
                    environment_id=self._context.environment_id,
                )
                if not isinstance(replayed, InvocationReplayRecord):
                    raise IdempotencyConflictError(
                        "Idempotency result cannot be resolved."
                    )
                if replayed.state == "checkpoint":
                    if replayed.checkpoint is None:
                        raise IdempotencyConflictError(
                            "Idempotency result cannot be resolved."
                        )
                    return await self._persist_checkpoint(
                        replayed.checkpoint,
                        claim=claim,
                        result_reference=result_reference,
                        replayed=True,
                    )
                if replayed.result is None:
                    raise IdempotencyConflictError(
                        "Idempotency result cannot be resolved."
                    )
                self._cancellation.seal(self._cancellation_scope)
                await self._events.observe_replay()
                return replayed.result
            if not isinstance(claim, OwnedIdempotencyClaim):
                raise IdempotencyConflictError("Idempotency claim is invalid.")
        except IdempotencyConflictError:
            return await self._finish(
                self._safe_failure(_FailureCategory.IDEMPOTENCY, AgentRunStatus.FAILED)
            )
        except (TypeError, ValueError):
            return await self._finish(
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED)
            )
        except CancelledError:
            await self._events.observe_run(AgentRunDiagnosticState.CANCELLED)
            raise
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.PERSISTENCE, AgentRunStatus.FAILED)
            )

        attempt_number = 1
        local_attempt_ids = (
            {current_attempt_id} if current_attempt_id is not None else set()
        )
        while True:
            request = AgentRequest(
                capability=self._context.capability,
                schema_identity=self._context.schema_identity,
                input=typed_input,
                constraints=invocation.constraints,
                local_attempt_id=current_attempt_id,
            )
            try:
                self._context.cancellation.raise_if_cancelled()
                response = await self._handler.invoke(request, self._context)
                self._context.cancellation.raise_if_cancelled()
            except (CooperativeCancellationError, CancelledError):
                return await self._finish_claimed_cancelled(claim, result_reference)
            except Exception:
                return await self._finish_claimed(
                    claim,
                    result_reference,
                    self._safe_failure(_FailureCategory.INTERNAL, AgentRunStatus.FAILED),
                )

            if not isinstance(response, AgentResponse):
                return await self._finish_claimed(
                    claim,
                    result_reference,
                    self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED),
                )
            if response.schema_identity != self._context.schema_identity:
                return await self._finish_claimed(
                    claim,
                    result_reference,
                    self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED),
                )
            if response.proposed_action_ids:
                # Proposed actions need their own persisted, lineage-bearing boundary.
                # Until that contract exists, never expose handler-fabricated IDs.
                return await self._finish_claimed(
                    claim,
                    result_reference,
                    self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED),
                )
            try:
                self._schemas.check_metadata(response)
            except Exception:
                return await self._finish_claimed(
                    claim,
                    result_reference,
                    self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED),
                )
            retry_reason = (
                retry_policy.retry_reason(response.error)
                if invocation.retry_safety is AgentRetrySafety.SAFE_TO_REPEAT
                else None
            )
            if retry_reason is None or attempt_number >= retry_policy.maximum_attempts:
                if response.error is not None:
                    if retry_reason is not None:
                        await self._events.observe_retry(
                            state=AgentRetryDiagnosticState.EXHAUSTED,
                            attempt_number=attempt_number,
                            retry_reason_code=retry_reason,
                            attempt_id=current_attempt_id,
                        )
                    return await self._finish_claimed(
                        claim,
                        result_reference,
                        agent_result_from_response(
                            response,
                            trace_ref=self._trace_ref,
                        ),
                    )
                break

            next_attempt_number = attempt_number + 1
            try:
                if self._retry_scheduler is None or self._attempt_id_factory is None:
                    raise ValueError("local retry dependencies are unavailable")
                next_attempt_id = self._attempt_id_factory(next_attempt_number)
                if (
                    not isinstance(next_attempt_id, UUID)
                    or next_attempt_id.int == 0
                    or next_attempt_id in local_attempt_ids
                ):
                    raise ValueError("local retry attempt identity is invalid")
                local_attempt_ids.add(next_attempt_id)
                await self._events.observe_retry(
                    state=AgentRetryDiagnosticState.SCHEDULED,
                    attempt_number=next_attempt_number,
                    retry_reason_code=retry_reason,
                    attempt_id=next_attempt_id,
                )
                await self._retry_scheduler.wait(
                    retry_policy.delay_before(next_attempt_number)
                )
                self._context.cancellation.raise_if_cancelled()
            except (CooperativeCancellationError, CancelledError):
                return await self._finish_claimed_cancelled(claim, result_reference)
            except Exception:
                return await self._finish_claimed(
                    claim,
                    result_reference,
                    self._safe_failure(_FailureCategory.INTERNAL, AgentRunStatus.FAILED),
                )
            attempt_number = next_attempt_number
            current_attempt_id = next_attempt_id

        try:
            self._context.cancellation.raise_if_cancelled()
            output = self._schemas.load_output(response.output)
            self._schemas.check_handoff(typed_input, output, response)
        except CooperativeCancellationError:
            return await self._finish_claimed_cancelled(claim, result_reference)
        except Exception:
            return await self._finish_claimed(
                claim,
                result_reference,
                self._safe_failure(_FailureCategory.REQUEST, AgentRunStatus.FAILED),
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
        checkpoint = InvocationOutputCheckpoint(
            drafts=(draft,),
            evidence_refs=response.evidence_refs,
            confidence=response.confidence,
            structured_metrics=response.structured_metrics,
            trace_ref=self._trace_ref,
        )
        try:
            if not self._cancellation.seal(self._cancellation_scope):
                return await self._finish_claimed(
                    claim,
                    result_reference,
                    self._cancelled_result(),
                )
            persisted_checkpoint = await self._artifacts.checkpoint_outputs(
                checkpoint,
                claim=claim,
                result_reference=result_reference,
            )
            if persisted_checkpoint != checkpoint:
                raise ValueError("output checkpoint is invalid")
        except CooperativeCancellationError:
            return await self._finish_claimed(
                claim,
                result_reference,
                self._cancelled_result(),
            )
        except CancelledError:
            await self._events.observe_run(AgentRunDiagnosticState.CANCELLED)
            raise
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.PERSISTENCE, AgentRunStatus.FAILED)
            )
        return await self._persist_checkpoint(
            checkpoint,
            claim=claim,
            result_reference=result_reference,
            replayed=False,
        )

    async def cancel(
        self,
        step_run_id: UUID,
        *,
        scope: CancellationScope | None = None,
    ) -> None:
        """Request cancellation of this exact run through the existing surface."""

        resolved_scope = self._cancellation_scope if scope is None else scope
        if (
            step_run_id != resolved_scope.step_run_id
            or resolved_scope != self._cancellation_scope
        ):
            raise ValueError("cancellation scope is invalid")
        if self._cancellation.request_cancel(resolved_scope):
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
        elif category is _FailureCategory.IDEMPOTENCY:
            code = "spine.persistence.idempotency_conflict"
            message = "The idempotency key conflicts with an existing command."
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
        state = _diagnostic_state(result.status)
        await self._events.observe_run(state)
        return result

    async def _finish_claimed(
        self,
        claim: OwnedIdempotencyClaim,
        result_reference: OpaqueResultReference,
        result: AgentResult,
    ) -> AgentResult:
        """Commit a terminal result through the same canonical receipt."""

        state = _diagnostic_state(result.status)
        try:
            persisted = await self._artifacts.persist_result(
                result,
                claim=claim,
                result_reference=result_reference,
                audit_event=self._events.build_run_audit_event(state),
            )
        except CancelledError:
            await self._events.observe_run(AgentRunDiagnosticState.CANCELLED)
            raise
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.PERSISTENCE, AgentRunStatus.FAILED)
            )
        if not isinstance(persisted, AgentResult) or persisted != result:
            return await self._finish(
                self._safe_failure(_FailureCategory.PERSISTENCE, AgentRunStatus.FAILED)
            )
        await self._events.observe_run_diagnostic(state)
        return persisted

    async def _finish_claimed_cancelled(
        self,
        claim: OwnedIdempotencyClaim,
        result_reference: OpaqueResultReference,
    ) -> AgentResult:
        """Persist the canonical terminal cancellation result for an owned claim."""

        return await self._finish_claimed(
            claim,
            result_reference,
            self._cancelled_result(),
        )

    async def _persist_checkpoint(
        self,
        checkpoint: InvocationOutputCheckpoint,
        *,
        claim: OwnedIdempotencyClaim | IdempotencyReplay,
        result_reference: OpaqueResultReference,
        replayed: bool,
    ) -> AgentResult:
        try:
            result = await self._artifacts.persist_outputs(
                checkpoint,
                claim=claim,
                result_reference=result_reference,
                audit_event=self._events.build_run_audit_event(
                    AgentRunDiagnosticState.COMPLETED
                ),
                validate_references=self._validate_persisted_references,
            )
        except CancelledError:
            await self._events.observe_run(AgentRunDiagnosticState.CANCELLED)
            raise
        except Exception:
            return await self._finish(
                self._safe_failure(_FailureCategory.PERSISTENCE, AgentRunStatus.FAILED)
            )
        if not isinstance(result, AgentResult):
            return await self._finish(
                self._safe_failure(_FailureCategory.PERSISTENCE, AgentRunStatus.FAILED)
            )
        if replayed:
            await self._events.observe_replay()
        else:
            await self._events.observe_run_diagnostic(
                AgentRunDiagnosticState.COMPLETED
            )
        return result


def _diagnostic_state(status: AgentRunStatus) -> AgentRunDiagnosticState:
    return {
        AgentRunStatus.SUCCEEDED: AgentRunDiagnosticState.COMPLETED,
        AgentRunStatus.FAILED: AgentRunDiagnosticState.FAILED,
        AgentRunStatus.CANCELLED: AgentRunDiagnosticState.CANCELLED,
    }[status]


__all__ = ["LocalAgentRuntime"]
