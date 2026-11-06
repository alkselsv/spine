"""Commit bounded Gmail history only after every covered effect is durable."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.email_sync import (
    EmailSyncAttempt,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectKind,
    EmailSyncPage,
    EmailSyncPageKind,
    EmailSyncStateObservation,
    EmailSyncStatus,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryId,
    GmailHistoryResult,
    GmailHistoryState,
    gmail_history_scope_digest,
)
from spine.connectors.gmail_checkpoints import encode_gmail_history_checkpoint
from spine.domain.connections.email_scope import MailboxReadScope
from spine.domain.sources.gmail import GmailMessageKey, gmail_source_identity


class GmailHistoryEffectFactory(Protocol):
    """Plan one change against its deterministic canonical acceptance identity."""

    async def prepare(
        self,
        scope: MailboxReadScope,
        change: GmailHistoryChange,
    ) -> EmailSyncEffect: ...


@dataclass(frozen=True, slots=True)
class GmailIncrementalHistoryApplier:
    """Bridge normalized Gmail history into the shared durable sync coordinator."""

    coordinator: EmailSyncCoordinator
    scope: MailboxReadScope
    effect_factory: GmailHistoryEffectFactory

    async def start(
        self, *, attempt_key: str, start_history_id: GmailHistoryId
    ) -> EmailSyncAttempt:
        """Create an idempotent run pinned to its previously committed cursor."""

        _require_history_id(start_history_id)
        return await self.coordinator.create_attempt(
            self.scope,
            attempt_key=attempt_key,
            initial_checkpoint=start_history_id,
        )

    async def apply(
        self,
        attempt_id: UUID,
        result: GmailHistoryResult,
        *,
        start_history_id: GmailHistoryId,
    ) -> EmailSyncAttempt:
        """Accept a complete bounded drain or durably hold its start checkpoint."""

        _require_history_id(start_history_id)
        resumed = await self.coordinator.resume_pending(
            attempt_id,
            workspace_id=self.scope.workspace_id,
            environment_id=self.scope.environment_id,
        )
        if resumed.status is EmailSyncStatus.COMPLETE or any(
            not page.committed for page in resumed.pages
        ):
            return resumed
        invalid = await self._hold_invalid_result(
            attempt_id,
            result,
            start_history_id=start_history_id,
        )
        if invalid is not None:
            return invalid
        effects = await self._prepare_effects(attempt_id, result)
        if effects is None:
            return await self.coordinator.get_attempt(
                attempt_id,
                workspace_id=self.scope.workspace_id,
                environment_id=self.scope.environment_id,
            )
        assert result.terminal_history_id is not None
        page = EmailSyncPage(
            workspace_id=self.scope.workspace_id,
            environment_id=self.scope.environment_id,
            page_id=_page_id(self.scope, start_history_id, result),
            requested_checkpoint=start_history_id,
            next_checkpoint=result.terminal_history_id,
            terminal=True,
            effects=effects,
        )
        return await self.coordinator.apply_page(
            attempt_id,
            page,
            workspace_id=self.scope.workspace_id,
            environment_id=self.scope.environment_id,
        )

    async def stage(
        self,
        attempt_id: UUID,
        result: GmailHistoryResult,
        *,
        start_history_id: GmailHistoryId,
    ) -> EmailSyncAttempt:
        """Durably apply a complete drain without publishing its terminal cursor.

        Initial snapshots use this after every captured snapshot page is durable.
        The caller may then publish one aggregate terminal checkpoint that proves
        the baseline, snapshot and intervening history were all covered.
        """

        _require_history_id(start_history_id)
        resumed = await self.coordinator.resume_pending(
            attempt_id,
            workspace_id=self.scope.workspace_id,
            environment_id=self.scope.environment_id,
        )
        if resumed.status is EmailSyncStatus.COMPLETE or any(
            not page.committed for page in resumed.pages
        ):
            return resumed
        invalid = await self._hold_invalid_result(
            attempt_id,
            result,
            start_history_id=start_history_id,
        )
        if invalid is not None:
            return invalid
        effects = await self._prepare_effects(attempt_id, result)
        if effects is None:
            return await self.coordinator.get_attempt(
                attempt_id,
                workspace_id=self.scope.workspace_id,
                environment_id=self.scope.environment_id,
            )
        assert result.terminal_history_id is not None
        identity = _page_id(self.scope, start_history_id, result)
        evidence_digest = hashlib.sha256(
            result.model_dump_json().encode("utf-8")
        ).hexdigest()
        return await self.coordinator.apply_page(
            attempt_id,
            EmailSyncPage(
                workspace_id=self.scope.workspace_id,
                environment_id=self.scope.environment_id,
                page_kind=EmailSyncPageKind.HISTORY_DRAIN,
                page_id=f"gmail.history.drain.{identity.rsplit('.', 1)[-1]}",
                requested_checkpoint=resumed.committed_checkpoint,
                publish_checkpoint=False,
                evidence_digest=evidence_digest,
                evidence_cursor=encode_gmail_history_checkpoint(
                    self.scope, result.terminal_history_id
                ),
                effects=effects,
            ),
            workspace_id=self.scope.workspace_id,
            environment_id=self.scope.environment_id,
        )

    async def _hold_invalid_result(
        self,
        attempt_id: UUID,
        result: GmailHistoryResult,
        *,
        start_history_id: GmailHistoryId,
    ) -> EmailSyncAttempt | None:
        if (
            result.state is GmailHistoryState.COMPLETE
            and result.terminal_history_id is not None
            and result.pages_read >= 1
            and _covers_requested_range(self.scope, start_history_id, result)
        ):
            return None
        status = (
            EmailSyncStatus.RECONCILIATION_REQUIRED
            if result.state is GmailHistoryState.RECONCILIATION_REQUIRED
            else EmailSyncStatus.INCOMPLETE
        )
        failure_code = (
            _failure_code(result)
            if result.state is not GmailHistoryState.COMPLETE
            or result.terminal_history_id is None
            or result.pages_read < 1
            else "gmail_history_range_invalid"
        )
        return await self.coordinator.hold(
            attempt_id,
            workspace_id=self.scope.workspace_id,
            environment_id=self.scope.environment_id,
            status=status,
            failure_code=failure_code,
        )

    async def _prepare_effects(
        self,
        attempt_id: UUID,
        result: GmailHistoryResult,
    ) -> tuple[EmailSyncEffect, ...] | None:
        try:
            return tuple(
                [
                    self._validate_effect(
                        change,
                        await self.effect_factory.prepare(self.scope, change),
                    )
                    for change in result.changes
                ]
            )
        except Exception:
            await self.coordinator.hold(
                attempt_id,
                workspace_id=self.scope.workspace_id,
                environment_id=self.scope.environment_id,
                status=EmailSyncStatus.INCOMPLETE,
                failure_code="gmail_effect_preparation_failed",
            )
            return None

    def _validate_effect(
        self,
        change: GmailHistoryChange,
        effect: EmailSyncEffect,
    ) -> EmailSyncEffect:
        if (
            effect.workspace_id != self.scope.workspace_id
            or effect.environment_id != self.scope.environment_id
        ):
            raise ValueError("Gmail history effect is outside the admitted scope")
        if change.kind is GmailHistoryChangeKind.MESSAGE_ADDED:
            return effect.model_copy(update={"kind": EmailSyncEffectKind.CONTENT})
        observation_id = uuid5(
            NAMESPACE_URL,
            "spine:gmail-state:v1:"
            f"{self.scope.workspace_id}:{self.scope.environment_id}:"
            f"{self.scope.connection_id}:{self.scope.mailbox_binding_id}:"
            f"{change.event_id}",
        )
        outbox_event_id = uuid5(
            NAMESPACE_URL,
            f"spine:gmail-state-outbox:v1:{observation_id}",
        )
        observation = EmailSyncStateObservation(
            observation_id=observation_id,
            workspace_id=self.scope.workspace_id,
            environment_id=self.scope.environment_id,
            connection_id=self.scope.connection_id,
            mailbox_binding_id=self.scope.mailbox_binding_id,
            state_kind=change.kind.value,
            provider_event_digest=hashlib.sha256(
                change.event_id.encode("utf-8")
            ).hexdigest(),
            subject_reference_digest=hashlib.sha256(
                gmail_source_identity(
                    GmailMessageKey(
                        connection_id=self.scope.connection_id,
                        mailbox_binding_id=self.scope.mailbox_binding_id,
                        message_id=change.message_id,
                    )
                ).external_object_id.encode("utf-8")
            ).hexdigest(),
            provider_generation=change.history_id,
            outbox_event_id=outbox_event_id,
        )
        return EmailSyncEffect(
            workspace_id=effect.workspace_id,
            environment_id=effect.environment_id,
            effect_key=effect.effect_key,
            effect_reference=observation_id,
            kind=EmailSyncEffectKind.STATE,
            state_observation=observation,
        )


def _page_id(
    scope: MailboxReadScope,
    start_history_id: GmailHistoryId,
    result: GmailHistoryResult,
) -> str:
    effect_ids = "\n".join(change.event_id for change in result.changes)
    identity = ":".join(
        (
            "spine:gmail-history-page:v1",
            str(scope.connection_id),
            str(scope.mailbox_binding_id),
            start_history_id,
            result.terminal_history_id or "missing",
            str(result.pages_read),
            effect_ids,
        )
    )
    return f"gmail.history.page.{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _failure_code(result: GmailHistoryResult) -> str:
    if result.state is GmailHistoryState.RECONCILIATION_REQUIRED:
        return "gmail_history_cursor_stale"
    if result.error_code is None:
        return "gmail_history_incomplete"
    candidate = f"gmail_{result.error_code}"
    if len(candidate) <= 64 and all(
        char.islower() or char.isdigit() or char == "_" for char in candidate
    ):
        return candidate
    return "gmail_history_incomplete"


def _require_history_id(value: str) -> None:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("Gmail history ID must be a decimal string")


def _covers_requested_range(
    scope: MailboxReadScope,
    start_history_id: GmailHistoryId,
    result: GmailHistoryResult,
) -> bool:
    terminal = result.terminal_history_id
    if (
        result.requested_start_history_id != start_history_id
        or result.admission_scope_digest != gmail_history_scope_digest(scope)
        or terminal is None
        or int(terminal) < int(start_history_id)
    ):
        return False
    return all(int(change.history_id) <= int(terminal) for change in result.changes)


__all__ = ["GmailHistoryEffectFactory", "GmailIncrementalHistoryApplier"]
