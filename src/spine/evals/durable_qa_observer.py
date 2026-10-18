"""Gold-blind observer adapter for the configured durable Q&A composition.

The adapter submits through the same admission owned by the API composition,
waits on canonical Ask status, and reads the protected values only from the
same-execution CaptureChannel envelope. It never invokes retrieval or the
capability directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import time
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from spine.application.ask_terminal_waiter import TerminalAskWaiter
from spine.application.persistence.ask_admission import AskAdmissionCommand
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.qa_observation.channel import (
    CaptureChannel,
    CaptureDisposition,
    CaptureReceiveState,
)
from spine.application.qa_observation.admission import ObserverAskAdmission
from spine.application.qa_sessions import QASessionService
from spine.application.qa_observation.contracts import AdmittedExecutionIdentity, ExecutionWaitState
from spine.application.qa_sessions import QASessionRequester
from spine.auth import AuthorizationDeniedError
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.evals.production_contract import (
    ProductionContractApplicationFlow,
    ProductionContractAuthorizationDenied,
    ProductionContractCaseRequest,
    ProductionContractObservation,
    ProductionContractObserverFailure,
    ProductionContractPartialFailure,
    ProductionContractUnavailable,
)


@dataclass(frozen=True, slots=True)
class ObserverAuthorization:
    """Fresh authorization and persistence context for one observer request."""

    context: TrustedPersistenceContext
    environment: EnvironmentKind
    authorization_generation: int


class ObserverAuthorizationPort(Protocol):
    """Resolve current trusted authority through the configured auth boundary."""

    async def authorize(
        self, *, request: ProductionContractCaseRequest
    ) -> ObserverAuthorization: ...


class DurableAskObservationComposition(Protocol):
    """Application ports exposed by the configured durable API composition."""

    sessions: QASessionService | None
    observer_admission: ObserverAskAdmission | None
    capture_channel: CaptureChannel | None


@dataclass(frozen=True, slots=True)
class DurableProductionQAAFlow(ProductionContractApplicationFlow):
    """Execute one case through the shared API/Temporal Q&A composition."""

    composition: DurableAskObservationComposition
    authorization: ObserverAuthorizationPort
    waiter: TerminalAskWaiter
    invocation_id_source: Callable[[], UUID] = uuid4
    monotonic: Callable[[], float] = time.monotonic
    wait_timeout: timedelta = timedelta(seconds=30)
    capture_timeout: timedelta = timedelta(seconds=5)
    worker_timing_sink: Callable[[UUID, int], None] | None = None

    def __post_init__(self) -> None:
        if self.wait_timeout <= timedelta(0) or self.wait_timeout > timedelta(seconds=30):
            raise ValueError("durable observer wait timeout must be within thirty seconds")
        if self.capture_timeout <= timedelta(0) or self.capture_timeout > timedelta(seconds=30):
            raise ValueError("durable observer capture timeout must be within thirty seconds")

    async def execute(
        self, request: ProductionContractCaseRequest
    ) -> ProductionContractObservation:
        composition = self.composition
        sessions = composition.sessions
        observer_admission = composition.observer_admission
        channel = composition.capture_channel
        if sessions is None or observer_admission is None or channel is None:
            raise ProductionContractUnavailable()

        try:
            authorized = await self.authorization.authorize(request=request)
        except AuthorizationDeniedError:
            raise ProductionContractAuthorizationDenied() from None
        except ProductionContractObserverFailure:
            raise
        except Exception:
            raise ProductionContractUnavailable() from None

        context = authorized.context
        scope = context.scope
        subject_id = context.acting_subject_id
        if (
            not isinstance(scope, EnvironmentScope)
            or scope.workspace_id != request.workspace_id
            or scope.environment_id != request.environment_id
            or subject_id != request.principal_id
            or authorized.authorization_generation < 1
        ):
            raise ProductionContractAuthorizationDenied()

        requester = QASessionRequester(
            subject_id=request.principal_id,
            authorization_generation=authorized.authorization_generation,
        )
        invocation_id = self.invocation_id_source()
        if invocation_id.int == 0:
            raise ProductionContractUnavailable()
        key_suffix = hashlib.sha256(
            f"{request.case_id}:{invocation_id}".encode("utf-8")
        ).hexdigest()
        try:
            session = await sessions.create_session(
                context=context,
                requester=requester,
                idempotency_key=IdempotencyKey(f"r1-observer-session:{key_suffix}"),
            )
            command = AskAdmissionCommand.create(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                session_id=session.value.session_id,
                subject=ActorRef(kind=ActorKind.HUMAN, id=request.principal_id),
                environment=authorized.environment,
                question=request.question,
                idempotency_key=IdempotencyKey(f"r1-observer-ask:{key_suffix}"),
            )
        except Exception:
            raise ProductionContractUnavailable() from None

        try:
            admitted = await observer_admission.admit(
                context=context,
                command=command,
            )
        except AuthorizationDeniedError:
            raise ProductionContractAuthorizationDenied() from None
        except Exception:
            raise ProductionContractUnavailable() from None

        identity = AdmittedExecutionIdentity(
            command_id=admitted.admission.command_id,
            run_id=admitted.admission.run_id,
            session_id=admitted.admission.session_id,
            message_id=admitted.admission.message_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            requester=ActorRef(kind=ActorKind.HUMAN, id=request.principal_id),
            attempt_id=uuid5(admitted.admission.run_id, "attempt:1"),
            attempt_number=1,
        )
        correlation_id = identity.run_id
        subscription_id = admitted.subscription_id
        try:
            deadline = self.monotonic() + self.wait_timeout.total_seconds()
            terminal = await self.waiter.wait(
                identity=identity,
                context=context,
                admission_result=admitted.admission,
                requester=requester,
                deadline=deadline,
            )
            if (
                terminal.wait.state is not ExecutionWaitState.TERMINAL
                or terminal.wait.identity != identity
                or terminal.message is None
            ):
                if terminal.wait.unavailable_reason is not None and (
                    terminal.wait.unavailable_reason.value == "authorization_changed"
                ):
                    raise ProductionContractAuthorizationDenied(
                        correlation_id=correlation_id,
                    )
                raise ProductionContractUnavailable(correlation_id=correlation_id)

            receive = await channel.receive(
                subscription_id=subscription_id,
                identity=identity,
                timeout=self.capture_timeout,
            )
            if receive.state is not CaptureReceiveState.CAPTURED or receive.capture is None:
                raise _partial_failure(identity)
            envelope = receive.capture
            if (
                envelope.capture_id != admitted.capture_id
                or envelope.subscription_id != subscription_id
                or not envelope.identity.matches(identity)
            ):
                raise _partial_failure(identity)
            capture = envelope.capture
            if terminal.wait.outcome is not capture.answer.outcome:
                raise _partial_failure(identity)
            self._validate_terminal_message(
                request=request,
                identity=identity,
                message=terminal.message,
                capture_answer=capture.answer,
            )
            if envelope.worker_execution_elapsed_ns is not None and self.worker_timing_sink:
                try:
                    self.worker_timing_sink(
                        identity.attempt_id,
                        envelope.worker_execution_elapsed_ns,
                    )
                except Exception:
                    pass
            observation = ProductionContractObservation(
                case_id=request.case_id,
                question=capture.question,
                context_response=capture.context_response,
                context_bundle=capture.context_bundle,
                projection_retrieval=capture.projection_retrieval,
                retrieval=capture.retrieval,
                answer=capture.answer,
                disclosure=capture.disclosure,
            )
            observation.validate()
            ack = await channel.acknowledge(
                subscription_id=subscription_id,
                capture_id=admitted.capture_id,
                identity=identity,
            )
            if ack not in {CaptureDisposition.ACKNOWLEDGED, CaptureDisposition.DUPLICATE}:
                raise _partial_failure(identity)
            return observation
        except ProductionContractObserverFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _partial_failure(identity) from None
        finally:
            # Cleanup is best-effort and never turns a safe outcome into a
            # content-bearing error. No cancellation is sent to Temporal.
            try:
                await channel.cleanup(subscription_id=subscription_id)
            except Exception:
                pass

    @staticmethod
    def _validate_terminal_message(
        *, request, identity: AdmittedExecutionIdentity, message, capture_answer
    ) -> None:
        if (
            message.message_id != identity.message_id
            or message.session_id != identity.session_id
            or message.run_id != identity.run_id
            or message.workspace_id != identity.workspace_id
            or message.environment_id != identity.environment_id
            or message.question != request.question
        ):
            raise _partial_failure(identity)
        if capture_answer.outcome.value == "answered":
            presentation = message.accepted_presentation
            accepted = capture_answer.answer
            if (
                message.answer is None
                or presentation is None
                or message.disclosure_eligibility.value != "eligible"
                or accepted is None
                or hashlib.sha256(message.answer.encode("utf-8")).hexdigest()
                != hashlib.sha256(accepted.text.encode("utf-8")).hexdigest()
                or tuple(presentation.citation_ids)
                != tuple(citation.citation_id for citation in accepted.citations)
            ):
                raise _partial_failure(identity)
        elif message.answer is not None or message.accepted_presentation is not None:
            raise _partial_failure(identity)


def _partial_failure(identity: AdmittedExecutionIdentity) -> ProductionContractPartialFailure:
    return ProductionContractPartialFailure(
        correlation_id=identity.run_id,
        completed_attempt_id=identity.attempt_id,
        completed_attempt_number=identity.attempt_number,
    )


__all__ = [
    "DurableAskObservationComposition",
    "DurableProductionQAAFlow",
    "ObserverAuthorization",
    "ObserverAuthorizationPort",
]
