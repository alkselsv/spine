"""No-gap orchestration for a bounded Gmail initial snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.email_sync import (
    EmailSyncAttempt,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncPage,
    EmailSyncPageKind,
    EmailSyncStatus,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


class GmailHistoryBaselineState(str, Enum):
    CAPTURED = "captured"
    UNSAFE = "unsafe"


class GmailHistoryBaseline(DefinitionModel):
    """Content-free evidence captured before snapshot enumeration starts."""

    schema_version: Literal["gmail-history-baseline:v1"] = "gmail-history-baseline:v1"
    state: GmailHistoryBaselineState
    admission_scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint: str | None = Field(
        default=None, min_length=1, max_length=4096, repr=False
    )
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def validate_outcome(self) -> "GmailHistoryBaseline":
        if self.state is GmailHistoryBaselineState.CAPTURED:
            if self.checkpoint is None or self.error_code is not None:
                raise ValueError("captured Gmail baseline requires one checkpoint")
        elif self.checkpoint is not None or self.error_code is None:
            raise ValueError("unsafe Gmail baseline requires a safe error code")
        return self


class GmailSnapshotPageRequest(DefinitionModel):
    scope: MailboxReadScope
    checkpoint: str | None = Field(default=None, max_length=4096, repr=False)
    max_results: int = Field(ge=1, le=500)


class GmailSnapshotPage(DefinitionModel):
    """One bounded messages.list page, without protected message metadata."""

    schema_version: Literal["gmail-snapshot-page:v1"] = "gmail-snapshot-page:v1"
    requested_checkpoint: str | None = Field(default=None, max_length=4096, repr=False)
    message_ids: tuple[str, ...] = ()
    next_checkpoint: str | None = Field(default=None, max_length=4096, repr=False)
    complete: bool = True
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def validate_page(self) -> "GmailSnapshotPage":
        if len(self.message_ids) != len(set(self.message_ids)) or any(
            not value or len(value) > 512 or not value.isascii()
            for value in self.message_ids
        ):
            raise ValueError("Gmail snapshot message identities are invalid")
        if self.complete and self.error_code is not None:
            raise ValueError("complete Gmail page cannot carry an error")
        if not self.complete and self.error_code is None:
            raise ValueError("incomplete Gmail page requires a safe error code")
        return self


class GmailInitialSnapshotCommand(DefinitionModel):
    schema_version: Literal["gmail-initial-snapshot-command:v1"] = (
        "gmail-initial-snapshot-command:v1"
    )
    scope: MailboxReadScope
    attempt_key: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    snapshot_page_size: int = Field(ge=1, le=500)
    max_snapshot_pages: int = Field(ge=1, le=10_000)
    history_page_size: int = Field(ge=1, le=500)
    max_history_pages: int = Field(ge=1, le=10_000)
    max_message_bytes: int = Field(ge=1, le=268_435_456)
    access_policy_version_id: UUID

    @model_validator(mode="after")
    def validate_scope(self) -> "GmailInitialSnapshotCommand":
        required = (
            EmailOperation.ENUMERATE_MAILBOX,
            EmailOperation.READ_RAW_MESSAGE,
            EmailOperation.SYNCHRONIZE,
        )
        if (
            self.scope.provider_kind != "google"
            or any(not self.scope.allows(operation) for operation in required)
            or len(self.scope.allowed_label_references) != 1
            or self.scope.excluded_label_references
            or self.access_policy_version_id.int == 0
        ):
            raise ValueError(
                "snapshot scope requires approved Gmail operations and one exact label"
            )
        return self


class GmailInitialSnapshotReader(Protocol):
    async def capture_history_baseline(
        self, scope: MailboxReadScope
    ) -> GmailHistoryBaseline: ...

    async def enumerate_snapshot_page(
        self, request: GmailSnapshotPageRequest
    ) -> GmailSnapshotPage: ...


class GmailInitialHistoryDrainPort(Protocol):
    async def stage(
        self,
        attempt_id: UUID,
        scope: MailboxReadScope,
        *,
        baseline_checkpoint: str,
        max_results: int,
        max_pages: int,
        covered_snapshot_effect_keys: tuple[str, ...],
    ) -> EmailSyncAttempt: ...


class GmailSnapshotMessageCommand(DefinitionModel):
    schema_version: Literal["gmail-snapshot-message:v1"] = "gmail-snapshot-message:v1"
    scope: MailboxReadScope
    message_id: str = Field(min_length=1, max_length=512)
    max_message_bytes: int = Field(ge=1, le=268_435_456)
    access_policy_version_id: UUID
    effect_key: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")


class GmailSnapshotMessageReceipt(DefinitionModel):
    schema_version: Literal["gmail-snapshot-message-receipt:v1"] = (
        "gmail-snapshot-message-receipt:v1"
    )
    effect_key: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    workspace_id: UUID
    environment_id: UUID
    canonical_acceptance_id: UUID
    receipt_reference: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    durable: Literal[True] = True

    @model_validator(mode="after")
    def validate_reference(self) -> "GmailSnapshotMessageReceipt":
        if (
            self.workspace_id.int == 0
            or self.environment_id.int == 0
            or self.canonical_acceptance_id.int == 0
        ):
            raise ValueError("canonical acceptance reference must be non-zero")
        return self


class GmailSnapshotMessagePort(Protocol):
    async def apply(
        self, command: GmailSnapshotMessageCommand
    ) -> GmailSnapshotMessageReceipt: ...


@dataclass(frozen=True, slots=True)
class GmailInitialSnapshotResult:
    attempt: EmailSyncAttempt
    completed_snapshot_pages: int
    snapshot_messages: int
    history_effects: int


def gmail_snapshot_effect_key(scope: MailboxReadScope, message_id: str) -> str:
    """Return the shared canonical identity for snapshot/history overlap."""

    identity = f"{scope.connection_id}:{scope.mailbox_binding_id}:{message_id}"
    return f"gmail.snapshot.{hashlib.sha256(identity.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class GmailInitialSnapshotCoordinator:
    """Capture history, persist a full snapshot, drain, then commit once."""

    sync: EmailSyncCoordinator
    reader: GmailInitialSnapshotReader
    messages: GmailSnapshotMessagePort
    history: GmailInitialHistoryDrainPort

    async def run(
        self, command: GmailInitialSnapshotCommand
    ) -> GmailInitialSnapshotResult:
        scope = command.scope
        attempt = await self.sync.create_attempt(scope, attempt_key=command.attempt_key)
        if attempt.status in {
            EmailSyncStatus.COMPLETE,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            EmailSyncStatus.SUSPENDED,
            EmailSyncStatus.FAILED,
        }:
            return self._result(attempt)

        baseline, attempt = await self._baseline(scope, attempt)
        if baseline is None:
            return self._result(attempt)

        snapshot_checkpoint: str | None = None
        seen_checkpoints: set[str] = set()
        snapshot_digests: list[str] = []
        for ordinal in range(command.max_snapshot_pages):
            page_id = f"gmail.snapshot.{ordinal:08d}"
            known = self._known_page(attempt, page_id)
            if known is not None and known.committed:
                if known.evidence_digest is None:
                    return await self._reconcile(attempt, "snapshot_evidence_invalid")
                snapshot_digests.append(known.evidence_digest)
                snapshot_checkpoint = known.evidence_cursor
                if known.coverage_terminal:
                    break
                if snapshot_checkpoint is None:
                    return await self._reconcile(attempt, "snapshot_evidence_invalid")
                if snapshot_checkpoint in seen_checkpoints:
                    return await self._reconcile(attempt, "snapshot_pagination_cycle")
                seen_checkpoints.add(snapshot_checkpoint)
                continue

            attempt = await self.sync.check_read_admission(
                attempt.attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
            )
            if attempt.status not in {EmailSyncStatus.PENDING, EmailSyncStatus.RUNNING}:
                return self._result(attempt)
            request = GmailSnapshotPageRequest(
                scope=scope,
                checkpoint=snapshot_checkpoint,
                max_results=command.snapshot_page_size,
            )
            try:
                page = await self.reader.enumerate_snapshot_page(request)
            except Exception:
                return await self._reconcile(attempt, "snapshot_page_unavailable")
            if (
                not page.complete
                or page.requested_checkpoint != snapshot_checkpoint
                or len(page.message_ids) > command.snapshot_page_size
            ):
                return await self._reconcile(attempt, "snapshot_page_incomplete")

            effects: list[EmailSyncEffect] = []
            receipts: list[EmailSyncEffectReceipt] = []
            for message_id in page.message_ids:
                attempt = await self.sync.check_effect_admission(
                    attempt.attempt_id,
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                )
                if attempt.status is not EmailSyncStatus.RUNNING:
                    return self._result(attempt)
                effect_key = self._message_effect_key(scope, message_id)
                try:
                    receipt = await self.messages.apply(
                        GmailSnapshotMessageCommand(
                            scope=scope,
                            message_id=message_id,
                            max_message_bytes=command.max_message_bytes,
                            access_policy_version_id=command.access_policy_version_id,
                            effect_key=effect_key,
                        )
                    )
                except Exception:
                    return await self._reconcile(attempt, "snapshot_effect_incomplete")
                attempt = await self.sync.check_effect_admission(
                    attempt.attempt_id,
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                )
                if attempt.status is not EmailSyncStatus.RUNNING:
                    return self._result(attempt)
                if (
                    receipt.effect_key != effect_key
                    or receipt.workspace_id != scope.workspace_id
                    or receipt.environment_id != scope.environment_id
                    or not receipt.durable
                ):
                    return await self._reconcile(attempt, "snapshot_effect_incomplete")
                effects.append(
                    EmailSyncEffect(
                        workspace_id=scope.workspace_id,
                        environment_id=scope.environment_id,
                        effect_key=effect_key,
                        effect_reference=receipt.canonical_acceptance_id,
                    )
                )
                receipts.append(
                    EmailSyncEffectReceipt(
                        effect_key=effect_key,
                        receipt_reference=receipt.receipt_reference,
                        durable=True,
                    )
                )

            digest = self._snapshot_page_digest(page)
            staged = EmailSyncPage(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                page_kind=EmailSyncPageKind.SNAPSHOT_PAGE,
                page_id=page_id,
                requested_checkpoint=attempt.committed_checkpoint,
                publish_checkpoint=False,
                evidence_digest=digest,
                evidence_cursor=page.next_checkpoint,
                coverage_terminal=page.next_checkpoint is None,
                effects=tuple(effects),
            )
            attempt = await self.sync.record_durable_page(
                attempt.attempt_id,
                staged,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                receipts=tuple(receipts),
            )
            if attempt.status is not EmailSyncStatus.RUNNING:
                return self._result(attempt)
            snapshot_digests.append(digest)
            snapshot_checkpoint = page.next_checkpoint
            if snapshot_checkpoint is None:
                break
            if snapshot_checkpoint in seen_checkpoints:
                return await self._reconcile(attempt, "snapshot_pagination_cycle")
            seen_checkpoints.add(snapshot_checkpoint)
        else:
            return await self._reconcile(attempt, "snapshot_page_budget_exhausted")

        history_page = next(
            (
                page
                for page in attempt.pages
                if page.page_kind is EmailSyncPageKind.HISTORY_DRAIN
            ),
            None,
        )
        if history_page is None:
            attempt = await self.sync.check_read_admission(
                attempt.attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
            )
            if attempt.status is not EmailSyncStatus.RUNNING:
                return self._result(attempt)
            covered_effect_keys = tuple(
                sorted(
                    effect.effect_key
                    for page in attempt.pages
                    if page.page_kind is EmailSyncPageKind.SNAPSHOT_PAGE
                    for effect in page.effects
                )
            )
            try:
                attempt = await self.history.stage(
                    attempt.attempt_id,
                    scope,
                    baseline_checkpoint=baseline.checkpoint,
                    max_results=command.history_page_size,
                    max_pages=command.max_history_pages,
                    covered_snapshot_effect_keys=covered_effect_keys,
                )
            except Exception:
                return await self._reconcile(attempt, "history_drain_unavailable")
            if attempt.status is EmailSyncStatus.INCOMPLETE:
                return await self._reconcile(attempt, "history_drain_incomplete")
            if attempt.status is not EmailSyncStatus.RUNNING:
                return self._result(attempt)
            history_page = next(
                (
                    page
                    for page in attempt.pages
                    if page.page_kind is EmailSyncPageKind.HISTORY_DRAIN
                ),
                None,
            )
        terminal_checkpoint = (
            None if history_page is None else history_page.evidence_cursor
        )
        if history_page is None or history_page.evidence_digest is None:
            return await self._reconcile(attempt, "history_drain_evidence_invalid")
        if not history_page.committed or terminal_checkpoint is None:
            return await self._reconcile(attempt, "history_drain_evidence_invalid")
        aggregate_digest = self._digest(
            {
                "baseline": baseline.checkpoint,
                "snapshot_pages": snapshot_digests,
                "history_drain": history_page.evidence_digest,
                "terminal_checkpoint": terminal_checkpoint,
                "scope_generation": scope.scope_generation,
            }
        )
        attempt = await self.sync.record_durable_page(
            attempt.attempt_id,
            EmailSyncPage(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                page_kind=EmailSyncPageKind.AGGREGATE_CHECKPOINT,
                page_id=f"gmail.aggregate.{aggregate_digest[:32]}",
                requested_checkpoint=attempt.committed_checkpoint,
                next_checkpoint=terminal_checkpoint,
                terminal=True,
                evidence_digest=aggregate_digest,
                effects=(),
            ),
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            receipts=(),
        )
        return self._result(attempt)

    async def _baseline(
        self, scope: MailboxReadScope, attempt: EmailSyncAttempt
    ) -> tuple[GmailHistoryBaseline | None, EmailSyncAttempt]:
        known = self._known_page(attempt, "gmail.history.baseline")
        if known is not None and known.committed:
            checkpoint = known.evidence_cursor
            if checkpoint is None:
                return (
                    None,
                    (
                        await self._reconcile(attempt, "baseline_evidence_invalid")
                    ).attempt,
                )
            try:
                baseline = GmailHistoryBaseline(
                    state=GmailHistoryBaselineState.CAPTURED,
                    admission_scope_digest=self._scope_digest(scope),
                    checkpoint=checkpoint,
                )
            except ValueError:
                return (
                    None,
                    (
                        await self._reconcile(attempt, "baseline_evidence_invalid")
                    ).attempt,
                )
            expected_digest = self._digest(baseline.model_dump(mode="json"))
            if known.evidence_digest != expected_digest:
                return (
                    None,
                    (
                        await self._reconcile(attempt, "baseline_evidence_invalid")
                    ).attempt,
                )
            return baseline, attempt
        try:
            attempt = await self.sync.check_read_admission(
                attempt.attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
            )
            if attempt.status not in {EmailSyncStatus.PENDING, EmailSyncStatus.RUNNING}:
                return None, attempt
            baseline = await self.reader.capture_history_baseline(scope)
        except Exception:
            return (
                None,
                (await self._reconcile(attempt, "baseline_unavailable")).attempt,
            )
        if (
            baseline.state is not GmailHistoryBaselineState.CAPTURED
            or baseline.checkpoint is None
            or baseline.admission_scope_digest != self._scope_digest(scope)
        ):
            return None, (await self._reconcile(attempt, "baseline_unsafe")).attempt
        digest = self._digest(baseline.model_dump(mode="json"))
        attempt = await self.sync.record_durable_page(
            attempt.attempt_id,
            EmailSyncPage(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                page_kind=EmailSyncPageKind.HISTORY_BASELINE,
                page_id="gmail.history.baseline",
                requested_checkpoint=attempt.committed_checkpoint,
                publish_checkpoint=False,
                evidence_digest=digest,
                evidence_cursor=baseline.checkpoint,
                effects=(),
            ),
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            receipts=(),
        )
        if attempt.status is not EmailSyncStatus.RUNNING:
            return None, attempt
        return baseline, attempt

    async def _reconcile(
        self, attempt: EmailSyncAttempt, failure_code: str
    ) -> GmailInitialSnapshotResult:
        admission = attempt.admission
        attempt = await self.sync.require_reconciliation(
            attempt.attempt_id,
            workspace_id=admission.workspace_id,
            environment_id=admission.environment_id,
            failure_code=failure_code,
        )
        return self._result(attempt)

    @staticmethod
    def _known_page(attempt: EmailSyncAttempt, page_id: str):
        return next((page for page in attempt.pages if page.page_id == page_id), None)

    @staticmethod
    def _message_effect_key(scope: MailboxReadScope, message_id: str) -> str:
        return gmail_snapshot_effect_key(scope, message_id)

    @classmethod
    def _scope_digest(cls, scope: MailboxReadScope) -> str:
        return cls._digest(scope.model_dump(mode="json"))

    @classmethod
    def _snapshot_page_digest(cls, page: GmailSnapshotPage) -> str:
        return cls._digest(page.model_dump(mode="json"))

    @staticmethod
    def _digest(value: object) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _result(attempt: EmailSyncAttempt) -> GmailInitialSnapshotResult:
        snapshot_pages = tuple(
            page
            for page in attempt.pages
            if page.page_kind is EmailSyncPageKind.SNAPSHOT_PAGE
        )
        history_pages = tuple(
            page
            for page in attempt.pages
            if page.page_kind is EmailSyncPageKind.HISTORY_DRAIN
        )
        return GmailInitialSnapshotResult(
            attempt=attempt,
            completed_snapshot_pages=len(snapshot_pages),
            snapshot_messages=sum(len(page.effect_receipts) for page in snapshot_pages),
            history_effects=sum(len(page.effect_receipts) for page in history_pages),
        )


__all__ = [
    "GmailHistoryBaseline",
    "GmailHistoryBaselineState",
    "GmailInitialSnapshotCommand",
    "GmailInitialSnapshotCoordinator",
    "GmailInitialHistoryDrainPort",
    "GmailInitialSnapshotReader",
    "GmailInitialSnapshotResult",
    "GmailSnapshotMessageCommand",
    "GmailSnapshotMessagePort",
    "GmailSnapshotMessageReceipt",
    "GmailSnapshotPage",
    "GmailSnapshotPageRequest",
    "gmail_snapshot_effect_key",
]
