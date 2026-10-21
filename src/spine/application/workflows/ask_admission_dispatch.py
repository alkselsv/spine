"""Durable dispatch contracts for an admitted Q&A run.

This module owns the framework-independent boundary between the transactional
outbox and a Temporal adapter.  It intentionally carries only opaque persisted
identities into workflow start.  Protected command content is loaded later by
an authorization-aware activity boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from enum import Enum
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.outbox.dispatch import (
    OutboxConsumerSuccess,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.persistence.ask_admission import (
    AskCancellationDispatchPayload,
    AskAdmissionDispatchPayload,
    AskCaptureSubscription,
    QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION,
    QA_ASK_CANCELLATION_EVENT_TYPE,
    QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION,
    QA_ASK_ADMISSION_EVENT_TYPE,
    ask_admission_metadata_schema_version,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.application.diagnostics.context import DiagnosticContext
from spine.application.authorized_ask import (
    AskExecutionCommand,
    AskRequestContext,
    AuthorizedAskResult,
    AuthorizedAskService,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.worker_execution_observation import (
    WorkerExecutionElapsedCapturePort,
    WorkerExecutionElapsedSample,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind

if TYPE_CHECKING:
    from spine.application.qa_observation.contracts import CaptureEnvelope

ASK_ADMISSION_WORKFLOW_TYPE = "spine.qa.ask-admission:v1"


def _require_id(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def workflow_id_for_run(run_id: UUID) -> str:
    """Return the one stable Temporal identity for a canonical Q&A run."""

    _require_id(run_id, "run_id")
    return str(run_id)


class AskAdmissionWorkflowInput(BaseModel):
    """Opaque workflow input assembled from a committed admission intent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(default=1, gt=0)
    command_id: UUID
    workspace_id: UUID
    environment_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    capture_id: UUID | None = None
    capture_subscription: AskCaptureSubscription | None = None

    def model_post_init(self, __context: object) -> None:
        del __context
        for field_name in (
            "command_id",
            "workspace_id",
            "environment_id",
            "session_id",
            "message_id",
            "run_id",
            "trace_id",
        ):
            _require_id(getattr(self, field_name), field_name)
        for field_name in (
            "correlation_id",
            "causation_id",
            "capture_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_id(value, field_name)
        if self.schema_version != ask_admission_metadata_schema_version(
            self.capture_id, self.capture_subscription
        ):
            raise ValueError("Ask workflow input version is invalid")
        if self.capture_subscription is not None and (
            self.capture_subscription.capture_id != self.capture_id
        ):
            raise ValueError("capture subscription does not match capture_id")

    @classmethod
    def from_intent(cls, intent: OutboxIntent) -> "AskAdmissionWorkflowInput":
        if (
            type(intent) is not OutboxIntent
            or intent.event_type != QA_ASK_ADMISSION_EVENT_TYPE
            or intent.schema_version != QA_ASK_ADMISSION_EVENT_SCHEMA_VERSION
            or intent.environment_id is None
            or type(intent.payload) is not AskAdmissionDispatchPayload
            or intent.payload.schema_version not in {1, 2, 3}
            or intent.aggregate is None
            or intent.aggregate.object_type != "answer_question_run"
        ):
            raise InvalidAskAdmissionIntent("Ask admission intent is invalid.")

        payload = intent.payload
        if (
            payload.run_id != intent.aggregate.object_id
            or payload.command_id.int == 0
            or payload.session_id.int == 0
            or payload.message_id.int == 0
            or payload.run_id.int == 0
        ):
            raise InvalidAskAdmissionIntent("Ask admission intent lineage is invalid.")
        return cls(
            command_id=payload.command_id,
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            session_id=payload.session_id,
            message_id=payload.message_id,
            run_id=payload.run_id,
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.event_id,
            capture_id=payload.capture_id,
            capture_subscription=payload.capture_subscription,
            schema_version=payload.schema_version,
        )


class WorkerCapturePublisher(Protocol):
    """Worker-side CaptureChannel seam used only after terminal commit."""

    async def publish(self, envelope: "CaptureEnvelope") -> object: ...


class AskAdmissionCommandData(BaseModel):
    """Protected command data released only by a governed activity loader."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: UUID
    workspace_id: UUID
    environment_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    question: str = Field(min_length=1, max_length=32768)
    subject: ActorRef | None = None
    environment: EnvironmentKind | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)

    def matches(self, workflow_input: AskAdmissionWorkflowInput) -> bool:
        return all(
            getattr(self, field_name) == getattr(workflow_input, field_name)
            for field_name in (
                "command_id",
                "workspace_id",
                "environment_id",
                "session_id",
                "message_id",
                "run_id",
            )
        )


class AskAdmissionCommandLoader(Protocol):
    """Load the exact admitted command under current authorization."""

    async def load(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AskAdmissionCommandData: ...


class AskAdmissionCommandSource(Protocol):
    """Persistence read port for one exact, scope-bound admitted command."""

    async def load_exact(
        self,
        workflow_input: AskAdmissionWorkflowInput,
    ) -> AskAdmissionCommandData | None: ...


class AskAdmissionCommandAuthorizer(Protocol):
    """Current authorization check for protected command content."""

    async def authorize(
        self,
        *,
        workflow_input: AskAdmissionWorkflowInput,
        command: AskAdmissionCommandData,
    ) -> bool: ...


class AskAdmissionCommandUnavailable(LookupError):
    """A command is missing, mismatched, stale, or no longer authorized."""


class ScopedAskAdmissionCommandLoader:
    """Load protected command data only after exact lineage and auth checks."""

    def __init__(
        self,
        source: AskAdmissionCommandSource,
        authorizer: AskAdmissionCommandAuthorizer,
    ) -> None:
        self._source = source
        self._authorizer = authorizer

    async def load(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AskAdmissionCommandData:
        command = await self._source.load_exact(workflow_input)
        if command is None or not command.matches(workflow_input):
            raise AskAdmissionCommandUnavailable("Admitted Ask command is unavailable.")
        try:
            allowed = await self._authorizer.authorize(
                workflow_input=workflow_input,
                command=command,
            )
        except Exception:
            allowed = False
        if not allowed:
            raise AskAdmissionCommandUnavailable("Admitted Ask command is unavailable.")
        return command


class AskAdmissionWorkflowHandle(BaseModel):
    """Safe identity returned by a Temporal client after start or lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_id: str = Field(min_length=1, max_length=255)
    run_id: UUID

    @model_validator(mode="after")
    def validate_identity(self) -> "AskAdmissionWorkflowHandle":
        expected = workflow_id_for_run(self.run_id)
        if self.workflow_id != expected:
            raise ValueError("Temporal workflow handle identity is invalid")
        return self


class TemporalWorkflowAlreadyStarted(Exception):
    """The requested workflow identity already exists in Temporal."""

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(workflow_id)


class AskAdmissionWorkflowClient(Protocol):
    async def start_workflow(
        self,
        *,
        workflow_type: str,
        workflow_id: str,
        input: AskAdmissionWorkflowInput,
    ) -> AskAdmissionWorkflowHandle: ...

    async def get_workflow(self, *, workflow_id: str) -> AskAdmissionWorkflowHandle: ...

    async def request_cancellation(self, *, workflow_id: str) -> None: ...


class AskAdmissionWorkflowLauncher:
    """Start once, then resolve duplicate delivery to the existing workflow."""

    def __init__(self, client: AskAdmissionWorkflowClient) -> None:
        self._client = client

    async def get_existing(
        self, *, workflow_id: str
    ) -> AskAdmissionWorkflowHandle | None:
        try:
            return await self._client.get_workflow(workflow_id=workflow_id)
        except LookupError:
            return None

    async def start_or_resume(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AskAdmissionWorkflowHandle:
        workflow_id = workflow_id_for_run(workflow_input.run_id)
        try:
            handle = await self._client.start_workflow(
                workflow_type=ASK_ADMISSION_WORKFLOW_TYPE,
                workflow_id=workflow_id,
                input=workflow_input,
            )
        except TemporalWorkflowAlreadyStarted as error:
            if error.workflow_id != workflow_id:
                raise InvalidAskAdmissionIntent(
                    "Temporal duplicate-start identity is invalid."
                ) from None
            handle = await self._client.get_workflow(workflow_id=workflow_id)
        if handle.workflow_id != workflow_id or handle.run_id != workflow_input.run_id:
            raise InvalidAskAdmissionIntent("Temporal workflow identity is invalid.")
        return handle


class AskAdmissionDispatchConsumer:
    """Outbox consumer that acknowledges only after Temporal accepts the start."""

    def __init__(self, launcher: AskAdmissionWorkflowLauncher) -> None:
        self._launcher = launcher

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        try:
            workflow_input = AskAdmissionWorkflowInput.from_intent(intent)
            expected_context = DiagnosticContext(
                trace_id=workflow_input.trace_id,
                correlation_id=intent.correlation_id,
                causation_id=intent.event_id,
            )
            if diagnostic_context != expected_context:
                raise InvalidAskAdmissionIntent("Dispatch context is invalid.")
            await self._launcher.start_or_resume(workflow_input)
        except (InvalidAskAdmissionIntent, ValueError):
            raise TerminalOutboxConsumerFailure("qa.ask.invalid_intent") from None
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "qa.ask.temporal_unavailable"
            ) from None
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class AskCancellationDispatchConsumer:
    """Deliver a committed cancellation intent to the canonical Temporal run."""

    def __init__(self, client: AskAdmissionWorkflowClient) -> None:
        self._client = client

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        payload = intent.payload
        if (
            intent.event_type != QA_ASK_CANCELLATION_EVENT_TYPE
            or intent.schema_version != QA_ASK_CANCELLATION_EVENT_SCHEMA_VERSION
            or type(payload) is not AskCancellationDispatchPayload
            or intent.environment_id is None
            or intent.aggregate is None
            or intent.aggregate.object_type != "answer_question_run"
            or intent.aggregate.object_id != payload.run_id
        ):
            raise TerminalOutboxConsumerFailure("qa.ask.invalid_cancellation_intent")
        expected_context = DiagnosticContext(
            trace_id=intent.trace_id,
            correlation_id=intent.correlation_id,
            causation_id=intent.event_id,
        )
        if diagnostic_context != expected_context:
            raise TerminalOutboxConsumerFailure("qa.ask.invalid_cancellation_intent")
        try:
            await self._client.request_cancellation(
                workflow_id=workflow_id_for_run(payload.run_id)
            )
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "qa.ask.temporal_unavailable"
            ) from None
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class AskAdmissionWorkflowState(str, Enum):
    WAITING_FOR_EXECUTION = "waiting_for_execution"
    CANCELLATION_REQUESTED = "cancellation_requested"


class AskAdmissionWorkflow:
    """Deterministic cancellation-aware execution for one admitted Ask run."""

    def __init__(self, executor: "AskAdmissionWorkflowExecutor | None" = None) -> None:
        self._state = AskAdmissionWorkflowState.WAITING_FOR_EXECUTION
        self._executor = executor

    @property
    def state(self) -> AskAdmissionWorkflowState:
        return self._state

    def request_cancellation(self) -> None:
        self._state = AskAdmissionWorkflowState.CANCELLATION_REQUESTED

    @property
    def cancellation_requested(self) -> bool:
        return self._state is AskAdmissionWorkflowState.CANCELLATION_REQUESTED

    async def execute(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AuthorizedAskResult:
        """Run the admitted capability exactly once through the activity seam."""

        if self._executor is None:
            raise RuntimeError("Ask workflow execution is not composed")
        if self.cancellation_requested:
            return await self._executor.cancel(workflow_input)
        return await self._executor.execute(workflow_input)


class AskAdmissionWorkflowExecutor:
    """Activity-side execution of one opaque Temporal workflow input."""

    def __init__(
        self,
        *,
        loader: AskAdmissionCommandLoader,
        service: AuthorizedAskService,
        persistence_context_factory: Callable[
            [AskAdmissionWorkflowInput], TrustedPersistenceContext
        ],
        run_starter: Callable[[AskAdmissionWorkflowInput], Awaitable[object]] | None = None,
        authorization_generation: int = 1,
        authorization_generation_resolver: Callable[
            [AskAdmissionWorkflowInput, AskAdmissionCommandData], Awaitable[int]
        ]
        | None = None,
        monotonic_ns: Callable[[], int] | None = None,
        execution_elapsed_port: WorkerExecutionElapsedCapturePort | None = None,
        capture_publisher: WorkerCapturePublisher | None = None,
    ) -> None:
        if authorization_generation < 1:
            raise ValueError("authorization generation must be positive")
        self._loader = loader
        self._service = service
        self._persistence_context_factory = persistence_context_factory
        self._run_starter = run_starter
        self._authorization_generation = authorization_generation
        self._authorization_generation_resolver = authorization_generation_resolver
        self._monotonic_ns = monotonic_ns
        self._execution_elapsed_port = execution_elapsed_port
        self._capture_publisher = capture_publisher

    async def execute(self, workflow_input: AskAdmissionWorkflowInput) -> AuthorizedAskResult:
        command = await self._loader.load(workflow_input)
        if (
            command.subject is None
            or command.environment is None
            or command.idempotency_key is None
        ):
            raise AskAdmissionCommandUnavailable(
                "Admitted Ask command lacks execution identity."
            )
        if self._run_starter is not None:
            await self._run_starter(workflow_input)
        context = self._persistence_context_factory(workflow_input)
        authorization_generation = await self._resolve_authorization_generation(
            workflow_input, command
        )
        started_at = self._monotonic_ns() if self._monotonic_ns is not None else None
        result = await self._service.execute_admitted(
            AskExecutionCommand(
                request_context=AskRequestContext(
                    subject_id=command.subject.id,
                    workspace_id=workflow_input.workspace_id,
                    environment_id=workflow_input.environment_id,
                    trace_id=workflow_input.trace_id,
                    authorization_generation=authorization_generation,
                    correlation_id=workflow_input.correlation_id,
                    causation_id=workflow_input.causation_id,
                ),
                persistence_context=context,
                command_id=workflow_input.command_id,
                session_id=workflow_input.session_id,
                message_id=workflow_input.message_id,
                run_id=workflow_input.run_id,
                question=command.question,
                idempotency_key=IdempotencyKey(command.idempotency_key),
                logical_execution_key=f"durable-ask:{workflow_input.command_id}",
                capture_subscription=workflow_input.capture_subscription,
            )
        )
        finished_at = self._monotonic_ns() if started_at is not None else None
        elapsed_ns: int | None = None
        if started_at is not None and finished_at is not None:
            elapsed_ns = max(0, finished_at - started_at)
        if (
            result.command_id != workflow_input.command_id
            or result.session_id != workflow_input.session_id
            or result.message_id != workflow_input.message_id
            or result.run_id != workflow_input.run_id
        ):
            raise InvalidAskAdmissionIntent("Ask execution returned mismatched identity.")
        if (
            getattr(result, "same_execution_capture", None) is not None
            and getattr(result, "capture_subscription", None) is not None
            and getattr(result, "attempt_number", None) is not None
            and elapsed_ns is not None
            and self._capture_publisher is not None
        ):
            try:
                from spine.application.qa_observation.contracts import (
                    AdmittedExecutionIdentity,
                    CaptureEnvelope,
                )

                if (
                    workflow_input.capture_subscription
                    != getattr(result, "capture_subscription")
                    or workflow_input.capture_id
                    != getattr(result, "capture_subscription").capture_id
                ):
                    raise ValueError("observer capture route changed during execution")
                identity = AdmittedExecutionIdentity(
                    command_id=result.command_id,
                    run_id=result.run_id,
                    session_id=result.session_id,
                    message_id=result.message_id,
                    workspace_id=workflow_input.workspace_id,
                    environment_id=workflow_input.environment_id,
                    requester=ActorRef(kind=ActorKind.HUMAN, id=command.subject.id),
                    attempt_id=result.attempt_id,
                    attempt_number=getattr(result, "attempt_number"),
                )
                envelope = CaptureEnvelope(
                    capture_id=getattr(result, "capture_subscription").capture_id,
                    subscription_id=getattr(result, "capture_subscription").subscription_id,
                    identity=identity,
                    capture=getattr(result, "same_execution_capture"),
                    worker_execution_elapsed_ns=elapsed_ns,
                )
                publish = getattr(self._capture_publisher, "publish", None)
                if callable(publish):
                    await publish(envelope)
            except Exception:
                # Capture delivery happens after canonical terminal commit and
                # cannot change the persisted Q&A outcome.
                pass
        if (
            elapsed_ns is not None
            and self._execution_elapsed_port is not None
        ):
            try:
                await self._execution_elapsed_port.record_worker_execution_elapsed(
                    WorkerExecutionElapsedSample(
                        run_id=workflow_input.run_id,
                        attempt_id=result.attempt_id,
                        capture_id=workflow_input.capture_id,
                        worker_execution_elapsed_ns=elapsed_ns,
                    )
                )
            except Exception:
                # Observability must not change the canonical terminal result.
                pass
        return result

    async def cancel(
        self, workflow_input: AskAdmissionWorkflowInput
    ) -> AuthorizedAskResult:
        """Observe durable cancellation under the workflow's current authorization."""

        command = await self._loader.load(workflow_input)
        if command.subject is None or command.idempotency_key is None:
            raise AskAdmissionCommandUnavailable(
                "Admitted Ask command lacks cancellation identity."
            )
        if self._run_starter is not None:
            await self._run_starter(workflow_input)
        context = self._persistence_context_factory(workflow_input)
        authorization_generation = await self._resolve_authorization_generation(
            workflow_input, command
        )
        result = await self._service.execute_admitted(
            AskExecutionCommand(
                request_context=AskRequestContext(
                    subject_id=command.subject.id,
                    workspace_id=workflow_input.workspace_id,
                    environment_id=workflow_input.environment_id,
                    trace_id=workflow_input.trace_id,
                    authorization_generation=authorization_generation,
                    correlation_id=workflow_input.correlation_id,
                    causation_id=workflow_input.causation_id,
                ),
                persistence_context=context,
                command_id=workflow_input.command_id,
                session_id=workflow_input.session_id,
                message_id=workflow_input.message_id,
                run_id=workflow_input.run_id,
                question=command.question,
                idempotency_key=IdempotencyKey(command.idempotency_key),
                logical_execution_key=f"durable-ask:{workflow_input.command_id}",
                cancelled=True,
            )
        )
        if (
            result.command_id != workflow_input.command_id
            or result.session_id != workflow_input.session_id
            or result.message_id != workflow_input.message_id
            or result.run_id != workflow_input.run_id
        ):
            raise InvalidAskAdmissionIntent("Ask cancellation returned mismatched identity.")
        return result

    async def _resolve_authorization_generation(
        self,
        workflow_input: AskAdmissionWorkflowInput,
        command: AskAdmissionCommandData,
    ) -> int:
        generation = self._authorization_generation
        if self._authorization_generation_resolver is not None:
            generation = await self._authorization_generation_resolver(
                workflow_input, command
            )
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise AskAdmissionCommandUnavailable(
                "Current authorization generation is unavailable."
            )
        return generation


class AskAdmissionReconciliationSource(Protocol):
    async def list_unstarted_admissions(
        self, *, limit: int
    ) -> Sequence[AskAdmissionWorkflowInput]: ...


class AskAdmissionReconciliationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    inspected: int
    started: int
    resumed: int
    failed: int


async def reconcile_ask_admissions(
    source: AskAdmissionReconciliationSource,
    launcher: AskAdmissionWorkflowLauncher,
    *,
    limit: int = 100,
) -> AskAdmissionReconciliationReport:
    """Retry committed admitted runs without creating a second workflow."""

    if isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("reconciliation limit must be between 1 and 1000")
    inputs = tuple(await source.list_unstarted_admissions(limit=limit))
    started = resumed = failed = 0
    for workflow_input in inputs:
        try:
            before = await launcher.get_existing(
                workflow_id=workflow_id_for_run(workflow_input.run_id)
            )
        except Exception:
            before = None
        try:
            await launcher.start_or_resume(workflow_input)
        except Exception:
            failed += 1
        else:
            if before is None:
                started += 1
            else:
                resumed += 1
    return AskAdmissionReconciliationReport(
        inspected=len(inputs), started=started, resumed=resumed, failed=failed
    )


class InvalidAskAdmissionIntent(ValueError):
    """The committed intent cannot be safely mapped to one Q&A workflow."""


__all__ = [
    "ASK_ADMISSION_WORKFLOW_TYPE",
    "AskAdmissionCommandData",
    "AskAdmissionCommandAuthorizer",
    "AskAdmissionCommandLoader",
    "AskAdmissionCommandSource",
    "AskAdmissionCommandUnavailable",
    "AskAdmissionDispatchConsumer",
    "AskCancellationDispatchConsumer",
    "AskAdmissionReconciliationReport",
    "AskAdmissionReconciliationSource",
    "AskAdmissionWorkflow",
    "AskAdmissionWorkflowExecutor",
    "AskAdmissionWorkflowClient",
    "AskAdmissionWorkflowHandle",
    "AskAdmissionWorkflowInput",
    "AskAdmissionWorkflowLauncher",
    "AskAdmissionWorkflowState",
    "InvalidAskAdmissionIntent",
    "TemporalWorkflowAlreadyStarted",
    "reconcile_ask_admissions",
    "workflow_id_for_run",
]
