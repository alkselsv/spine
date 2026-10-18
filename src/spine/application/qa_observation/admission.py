"""Trusted observer entry for registering capture before canonical admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.persistence.ask_admission import (
    AskAdmissionApplication,
    AskAdmissionCommand,
    AskAdmissionResult,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.qa_observation.channel import CaptureChannel, CaptureDisposition
from spine.application.qa_observation.contracts import AdmittedExecutionIdentity
from spine.domain.common import ActorKind, ActorRef


class ObserverAskAdmissionUnavailable(RuntimeError):
    """Observer admission or its ephemeral capture route could not be established."""


@dataclass(frozen=True, slots=True)
class ObserverAskAdmissionResult:
    admission: AskAdmissionResult
    capture_id: UUID
    subscription_id: UUID


class ObserverAskAdmission:
    """Register, admit and bind one trusted observer capture correlation.

    This class is intended for the server-owned observer composition. It takes
    a trusted ``AskAdmissionCommand`` directly; the public API has no parameter
    for ``capture_id``.
    """

    def __init__(
        self,
        *,
        admission: AskAdmissionApplication,
        channel: CaptureChannel,
        capture_id_source: Callable[[AskAdmissionCommand], UUID] | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("capture subscription TTL must be positive")
        self._admission = admission
        self._channel = channel
        self._capture_id_source = capture_id_source or self._capture_id_for_command
        self._ttl = ttl

    async def admit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: AskAdmissionCommand,
    ) -> ObserverAskAdmissionResult:
        capture_id = self._capture_id_source(command)
        if capture_id.int == 0:
            raise ValueError("capture id source returned a zero UUID")
        subscription = await self._channel.register(
            capture_id=capture_id,
            ttl=self._ttl,
        )
        try:
            admitted = await self._admission.admit(
                context=context,
                command=AskAdmissionCommand.create(
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    session_id=command.session_id,
                    subject=command.subject,
                    environment=command.environment,
                    question=command.question,
                    idempotency_key=command.idempotency_key,
                    clarification_of_message_id=command.clarification_of_message_id,
                    capture_id=capture_id,
                ),
            )
            identity = AdmittedExecutionIdentity(
                command_id=admitted.command_id,
                run_id=admitted.run_id,
                session_id=admitted.session_id,
                message_id=admitted.message_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                requester=ActorRef(kind=ActorKind.HUMAN, id=command.subject.id),
                attempt_id=uuid5(admitted.run_id, "attempt:1"),
                attempt_number=1,
            )
            disposition = await self._channel.bind(
                subscription_id=subscription.subscription_id,
                identity=identity,
            )
            if disposition not in {
                CaptureDisposition.BOUND,
                CaptureDisposition.ALREADY_BOUND,
            }:
                raise ObserverAskAdmissionUnavailable()
            return ObserverAskAdmissionResult(
                admission=admitted,
                capture_id=capture_id,
                subscription_id=subscription.subscription_id,
            )
        except Exception:
            await self._channel.cleanup(
                subscription_id=subscription.subscription_id,
            )
            raise

    @staticmethod
    def _capture_id_for_command(command: AskAdmissionCommand) -> UUID:
        """Derive the server-owned routing ID so admission retries reuse it."""

        return uuid5(
            NAMESPACE_URL,
            "spine:qa-observer-capture:v1:"
            f"{command.workspace_id}:{command.environment_id}:"
            f"{command.subject.kind.value}:{command.subject.id}:"
            f"{command.idempotency_key.value}:{command.digest.value}",
        )


__all__ = [
    "ObserverAskAdmission",
    "ObserverAskAdmissionResult",
    "ObserverAskAdmissionUnavailable",
]
