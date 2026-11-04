"""Generation-fenced, bounded polling of new Yandex Mail IMAP UIDs."""

from __future__ import annotations

import base64
import binascii
import hashlib
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, StrictInt, ValidationError, model_validator

from spine.application.connections.email_read import MailboxReadCommand
from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAttempt,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncPage,
    EmailSyncStatus,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.yandex_mail_uidvalidity_reset import (
    YandexMailUidValidityResetCommand,
    YandexMailUidValidityResetPort,
    YandexMailUidValidityResetReceipt,
    YandexMailUidValidityResetState,
)
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderGenerationCode,
    YandexMailFolderGenerationObservation,
)
from spine.connectors.yandex_mail_uid_enumeration import (
    YandexMailUidEnumerationPageRequest,
    YandexMailUidEnumerationPageResult,
    YandexMailUidPageCode,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


_CHECKPOINT_PREFIX = "yandex-imap-uid-checkpoint:v1:"
_MAX_IMAP_NUMBER = 2**32 - 1


class YandexMailIncrementalPollState(str, Enum):
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    INCOMPLETE = "incomplete"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SUSPENDED = "suspended"


class YandexMailIncrementalBaseline(DefinitionModel):
    """Snapshot/reconciliation handoff from which new UID polling may start."""

    schema_version: Literal["yandex-mail-incremental-baseline:v1"] = (
        "yandex-mail-incremental-baseline:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    scope_generation: StrictInt = Field(ge=1)
    uid_validity: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)
    next_uid: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)
    coverage_kind: Literal["snapshot", "reconciliation"]
    coverage_receipt_reference: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    complete: Literal[True] = True
    periodic_reconciliation_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_references(self) -> "YandexMailIncrementalBaseline":
        references = (
            self.workspace_id,
            self.environment_id,
            self.connection_id,
            self.connection_version_id,
            self.mailbox_binding_id,
            self.folder_reference,
        )
        if any(reference.int == 0 for reference in references):
            raise ValueError("incremental baseline references must be non-zero")
        return self

    @classmethod
    def from_scope(
        cls,
        scope: MailboxReadScope,
        *,
        folder_reference: UUID,
        uid_validity: int,
        next_uid: int,
        coverage_kind: Literal["snapshot", "reconciliation"],
        coverage_receipt_reference: str,
    ) -> "YandexMailIncrementalBaseline":
        return cls(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=folder_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=uid_validity,
            next_uid=next_uid,
            coverage_kind=coverage_kind,
            coverage_receipt_reference=coverage_receipt_reference,
        )

    def matches_scope(
        self, scope: MailboxReadScope, folder_reference: UUID
    ) -> bool:
        return (
            self.workspace_id == scope.workspace_id
            and self.environment_id == scope.environment_id
            and self.connection_id == scope.connection_id
            and self.connection_version_id == scope.connection_version_id
            and self.mailbox_binding_id == scope.mailbox_binding_id
            and self.folder_reference == folder_reference
            and self.authorization_generation == scope.authorization_generation
            and self.configuration_generation == scope.configuration_generation
            and self.scope_generation == scope.scope_generation
        )


class YandexMailIncrementalCheckpoint(DefinitionModel):
    """Opaque adapter cursor bound to one exact folder and authority generation."""

    schema_version: Literal["yandex-mail-incremental-checkpoint:v1"] = (
        "yandex-mail-incremental-checkpoint:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    scope_generation: StrictInt = Field(ge=1)
    uid_validity: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)
    next_uid: StrictInt = Field(ge=1, le=_MAX_IMAP_NUMBER)
    baseline_kind: Literal["snapshot", "reconciliation"]
    baseline_receipt_reference: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    periodic_reconciliation_required: Literal[True] = True
    covers_flag_changes: Literal[False] = False
    covers_folder_changes: Literal[False] = False
    proves_deletions: Literal[False] = False

    @model_validator(mode="after")
    def validate_references(self) -> "YandexMailIncrementalCheckpoint":
        references = (
            self.workspace_id,
            self.environment_id,
            self.connection_id,
            self.connection_version_id,
            self.mailbox_binding_id,
            self.folder_reference,
        )
        if any(reference.int == 0 for reference in references):
            raise ValueError("incremental checkpoint references must be non-zero")
        return self

    @classmethod
    def from_scope(
        cls,
        scope: MailboxReadScope,
        *,
        folder_reference: UUID,
        uid_validity: int,
        next_uid: int,
        baseline_kind: Literal["snapshot", "reconciliation"],
        baseline_receipt_reference: str,
    ) -> "YandexMailIncrementalCheckpoint":
        return cls(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=folder_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=uid_validity,
            next_uid=next_uid,
            baseline_kind=baseline_kind,
            baseline_receipt_reference=baseline_receipt_reference,
        )

    def matches_scope(
        self, scope: MailboxReadScope, folder_reference: UUID
    ) -> bool:
        return (
            self.workspace_id == scope.workspace_id
            and self.environment_id == scope.environment_id
            and self.connection_id == scope.connection_id
            and self.connection_version_id == scope.connection_version_id
            and self.mailbox_binding_id == scope.mailbox_binding_id
            and self.folder_reference == folder_reference
            and self.authorization_generation == scope.authorization_generation
            and self.configuration_generation == scope.configuration_generation
            and self.scope_generation == scope.scope_generation
        )


class YandexMailIncrementalPollResult(DefinitionModel):
    """Disclosure-safe polling outcome with no mutable-message-state claims."""

    schema_version: Literal["yandex-mail-incremental-poll-result:v1"] = (
        "yandex-mail-incremental-poll-result:v1"
    )
    state: YandexMailIncrementalPollState
    attempt: EmailSyncAttempt
    covered_uid_range: tuple[StrictInt, StrictInt] | None = None
    discovered_uids: tuple[StrictInt, ...] = ()
    reset_receipt: YandexMailUidValidityResetReceipt | None = None
    reset_state: YandexMailUidValidityResetState | None = None
    periodic_reconciliation_required: Literal[True] = True
    covers_flag_changes: Literal[False] = False
    covers_folder_changes: Literal[False] = False
    proves_deletions: Literal[False] = False


class YandexMailIncrementalReadPort(Protocol):
    async def read(
        self,
        command: MailboxReadCommand | YandexMailUidEnumerationPageRequest,
        context: TrustedPersistenceContext,
    ) -> object: ...


class YandexMailIncrementalEffectFactory(Protocol):
    """Describe the canonical acceptance an idempotent effect will commit."""

    async def effect_for_uid(
        self,
        *,
        scope: MailboxReadScope,
        folder_reference: UUID,
        uid_validity: int,
        uid: int,
    ) -> EmailSyncEffect: ...


class YandexMailIncrementalBaselineVerifier(Protocol):
    """Verify that a baseline is a durable complete snapshot/reconciliation receipt."""

    async def verify(
        self,
        baseline: YandexMailIncrementalBaseline,
        context: TrustedPersistenceContext,
    ) -> bool: ...


def encode_yandex_mail_incremental_checkpoint(
    checkpoint: YandexMailIncrementalCheckpoint,
) -> str:
    payload = checkpoint.model_dump_json().encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_CHECKPOINT_PREFIX}{encoded}"


def decode_yandex_mail_incremental_checkpoint(
    value: str,
) -> YandexMailIncrementalCheckpoint:
    """Decode an internal cursor without retaining malformed provider text."""

    if not value.startswith(_CHECKPOINT_PREFIX):
        raise ValueError("invalid Yandex Mail incremental checkpoint")
    encoded = value[len(_CHECKPOINT_PREFIX) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.b64decode(
            encoded + padding, altchars=b"-_", validate=True
        )
        return YandexMailIncrementalCheckpoint.model_validate_json(payload)
    except (binascii.Error, UnicodeDecodeError, ValidationError, ValueError):
        raise ValueError("invalid Yandex Mail incremental checkpoint") from None


def yandex_mail_uid_effect_key(
    *,
    scope: MailboxReadScope,
    folder_reference: UUID,
    uid_validity: int,
    uid: int,
) -> str:
    """Return the stable effect key for one exact qualified IMAP UID."""

    binding = (
        f"{scope.workspace_id}:{scope.environment_id}:{scope.connection_id}:"
        f"{scope.connection_version_id}:{scope.mailbox_binding_id}:"
        f"{folder_reference}:{uid_validity}"
    )
    digest = hashlib.sha256(binding.encode("ascii")).hexdigest()[:32]
    return f"imapuid.{digest}.{uid_validity}.{uid}"


class YandexMailIncrementalPoller:
    """Poll one proven UID interval and publish only its durable successor cursor."""

    def __init__(
        self,
        *,
        reader: YandexMailIncrementalReadPort,
        coordinator: EmailSyncCoordinator,
        effect_factory: YandexMailIncrementalEffectFactory,
        baseline_verifier: YandexMailIncrementalBaselineVerifier,
        uidvalidity_reset: YandexMailUidValidityResetPort,
    ) -> None:
        self._reader = reader
        self._coordinator = coordinator
        self._effect_factory = effect_factory
        self._baseline_verifier = baseline_verifier
        self._uidvalidity_reset = uidvalidity_reset

    async def poll(
        self,
        attempt_id: UUID,
        *,
        scope: MailboxReadScope,
        folder_reference: UUID,
        context: TrustedPersistenceContext,
        max_items: int,
        max_response_bytes: int,
        baseline: YandexMailIncrementalBaseline | None = None,
    ) -> YandexMailIncrementalPollResult:
        attempt = await self._coordinator.get_attempt(
            attempt_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        if (
            attempt.admission != EmailSyncAdmission.from_scope(scope)
            or scope.provider_kind != "yandex_mail"
            or not scope.allows_location(folder_reference)
            or not all(
                scope.allows(operation)
                for operation in (
                    EmailOperation.SYNCHRONIZE,
                    EmailOperation.OBSERVE_FOLDER_GENERATION,
                    EmailOperation.ENUMERATE_FOLDER_UIDS,
                )
            )
        ):
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.SUSPENDED,
                "scope_not_authorized",
            )
            return self._result(held)
        if attempt.status in {
            EmailSyncStatus.COMPLETE,
            EmailSyncStatus.FAILED,
            EmailSyncStatus.SUSPENDED,
        }:
            return self._result(attempt)
        if (
            attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED
            and attempt.failure_code
            not in {"uidvalidity_changed", "uidvalidity_reset_incomplete"}
        ):
            return self._result(attempt)

        checkpoint = await self._checkpoint_or_hold(
            attempt_id=attempt_id,
            attempt=attempt,
            scope=scope,
            folder_reference=folder_reference,
            baseline=baseline,
            context=context,
        )
        if isinstance(checkpoint, YandexMailIncrementalPollResult):
            return checkpoint

        generation_command = MailboxReadCommand(
            scope=scope,
            operation=EmailOperation.OBSERVE_FOLDER_GENERATION,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=folder_reference,
            expected_uid_validity=checkpoint.uid_validity,
        )
        try:
            generation = await self._reader.read(generation_command, context)
        except Exception:
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.INCOMPLETE,
                "provider_unavailable",
            )
            return self._result(held)
        if not isinstance(generation, YandexMailFolderGenerationObservation):
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.INCOMPLETE,
                "generation_incomplete",
            )
            return self._result(held)
        generation_failure = self._generation_failure(generation, checkpoint)
        if generation_failure is not None:
            status, code = generation_failure
            reset = await self._recover_changed_uidvalidity(
                attempt_id=attempt_id,
                scope=scope,
                folder_reference=folder_reference,
                stale_uid_validity=checkpoint.uid_validity,
                restore_committed_checkpoint=attempt.committed_checkpoint,
                failure_code=code,
                observed_uid_validity=generation.uid_validity,
            )
            if reset is not None:
                return reset
            held = await self._hold(attempt_id, scope, status, code)
            return self._result(held)

        assert generation.uid_next is not None
        if generation.uid_next < checkpoint.next_uid:
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.RECONCILIATION_REQUIRED,
                "uidnext_regressed",
            )
            return self._result(held)
        if generation.uid_next == checkpoint.next_uid:
            if attempt.committed_checkpoint is None:
                attempt = await self._commit_empty_baseline(
                    attempt=attempt,
                    checkpoint=checkpoint,
                    scope=scope,
                )
            else:
                attempt = await self._coordinator.resume(
                    attempt_id,
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                )
            if attempt.status is EmailSyncStatus.RUNNING:
                return self._result(
                    attempt, state=YandexMailIncrementalPollState.NO_CHANGE
                )
            return self._result(attempt)

        request = YandexMailUidEnumerationPageRequest(
            scope=scope,
            operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=folder_reference,
            generation=generation,
            start_uid=checkpoint.next_uid,
            enumeration_max_items=max_items,
            enumeration_max_response_bytes=max_response_bytes,
        )
        try:
            page_result = await self._reader.read(request, context)
        except Exception:
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.INCOMPLETE,
                "provider_unavailable",
            )
            return self._result(held)
        if not isinstance(page_result, YandexMailUidEnumerationPageResult):
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.INCOMPLETE,
                "uid_page_incomplete",
            )
            return self._result(held)
        page_failure = self._page_failure(page_result, request)
        if page_failure is not None:
            status, code = page_failure
            reset = await self._recover_changed_uidvalidity(
                attempt_id=attempt_id,
                scope=scope,
                folder_reference=folder_reference,
                stale_uid_validity=checkpoint.uid_validity,
                restore_committed_checkpoint=attempt.committed_checkpoint,
                failure_code=code,
                observed_uid_validity=page_result.uid_validity,
            )
            if reset is not None:
                return reset
            held = await self._hold(attempt_id, scope, status, code)
            return self._result(held)

        assert page_result.covered_uid_range is not None
        start_uid, end_uid = page_result.covered_uid_range
        effects: list[EmailSyncEffect] = []
        for message in page_result.messages:
            try:
                effect = await self._effect_factory.effect_for_uid(
                    scope=scope,
                    folder_reference=folder_reference,
                    uid_validity=checkpoint.uid_validity,
                    uid=message.uid,
                )
            except Exception:
                held = await self._hold(
                    attempt_id,
                    scope,
                    EmailSyncStatus.INCOMPLETE,
                    "effect_description_incomplete",
                )
                return self._result(held)
            expected_key = yandex_mail_uid_effect_key(
                scope=scope,
                folder_reference=folder_reference,
                uid_validity=checkpoint.uid_validity,
                uid=message.uid,
            )
            if (
                effect.workspace_id != scope.workspace_id
                or effect.environment_id != scope.environment_id
                or effect.effect_key != expected_key
            ):
                held = await self._hold(
                    attempt_id,
                    scope,
                    EmailSyncStatus.INCOMPLETE,
                    "effect_description_invalid",
                )
                return self._result(held)
            effects.append(effect)

        successor = checkpoint.model_copy(update={"next_uid": end_uid + 1})
        page = EmailSyncPage(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            page_id=self._page_id(checkpoint, start_uid, end_uid),
            requested_checkpoint=attempt.committed_checkpoint,
            next_checkpoint=encode_yandex_mail_incremental_checkpoint(successor),
            terminal=False,
            effects=tuple(effects),
        )
        applied = await self._coordinator.apply_page(
            attempt_id,
            page,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )
        return self._result(
            applied,
            covered_uid_range=page_result.covered_uid_range,
            discovered_uids=tuple(message.uid for message in page_result.messages),
        )

    async def _checkpoint_or_hold(
        self,
        *,
        attempt_id: UUID,
        attempt: EmailSyncAttempt,
        scope: MailboxReadScope,
        folder_reference: UUID,
        baseline: YandexMailIncrementalBaseline | None,
        context: TrustedPersistenceContext,
    ) -> YandexMailIncrementalCheckpoint | YandexMailIncrementalPollResult:
        if attempt.committed_checkpoint is None:
            if baseline is None:
                held = await self._hold(
                    attempt_id,
                    scope,
                    EmailSyncStatus.RECONCILIATION_REQUIRED,
                    "baseline_required",
                )
                return self._result(held)
            if not baseline.matches_scope(scope, folder_reference):
                held = await self._hold(
                    attempt_id,
                    scope,
                    EmailSyncStatus.RECONCILIATION_REQUIRED,
                    "baseline_scope_stale",
                )
                return self._result(held)
            try:
                verified = await self._baseline_verifier.verify(baseline, context)
            except Exception:
                verified = False
            if not verified:
                held = await self._hold(
                    attempt_id,
                    scope,
                    EmailSyncStatus.RECONCILIATION_REQUIRED,
                    "baseline_unverified",
                )
                return self._result(held)
            return YandexMailIncrementalCheckpoint.from_scope(
                scope,
                folder_reference=folder_reference,
                uid_validity=baseline.uid_validity,
                next_uid=baseline.next_uid,
                baseline_kind=baseline.coverage_kind,
                baseline_receipt_reference=baseline.coverage_receipt_reference,
            )
        try:
            checkpoint = decode_yandex_mail_incremental_checkpoint(
                attempt.committed_checkpoint
            )
        except ValueError:
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.RECONCILIATION_REQUIRED,
                "checkpoint_invalid",
            )
            return self._result(held)
        if not checkpoint.matches_scope(scope, folder_reference):
            held = await self._hold(
                attempt_id,
                scope,
                EmailSyncStatus.RECONCILIATION_REQUIRED,
                "checkpoint_scope_stale",
            )
            return self._result(held)
        return checkpoint

    async def _commit_empty_baseline(
        self,
        *,
        attempt: EmailSyncAttempt,
        checkpoint: YandexMailIncrementalCheckpoint,
        scope: MailboxReadScope,
    ) -> EmailSyncAttempt:
        encoded = encode_yandex_mail_incremental_checkpoint(checkpoint)
        page = EmailSyncPage(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            page_id=self._baseline_page_id(checkpoint),
            requested_checkpoint=None,
            next_checkpoint=encoded,
            terminal=False,
            effects=(),
        )
        return await self._coordinator.apply_page(
            attempt.attempt_id,
            page,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )

    async def _hold(
        self,
        attempt_id: UUID,
        scope: MailboxReadScope,
        status: EmailSyncStatus,
        failure_code: str,
    ) -> EmailSyncAttempt:
        return await self._coordinator.hold(
            attempt_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            status=status,
            failure_code=failure_code,
        )

    async def _recover_uidvalidity_reset(
        self,
        *,
        attempt_id: UUID,
        scope: MailboxReadScope,
        folder_reference: UUID,
        stale_uid_validity: int,
        observed_uid_validity: int,
        restore_committed_checkpoint: str | None,
    ) -> YandexMailIncrementalPollResult:
        fenced = await self._coordinator.fence_for_reconciliation(
            attempt_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            expected_admission=EmailSyncAdmission.from_scope(scope),
            restore_committed_checkpoint=restore_committed_checkpoint,
            failure_code="uidvalidity_changed",
        )
        if fenced.status is not EmailSyncStatus.RECONCILIATION_REQUIRED:
            return self._result(fenced)
        try:
            receipt = await self._uidvalidity_reset.reconcile(
                YandexMailUidValidityResetCommand(
                    scope=scope,
                    stale_attempt_id=attempt_id,
                    folder_reference=folder_reference,
                    stale_uid_validity=stale_uid_validity,
                    observed_uid_validity=observed_uid_validity,
                )
            )
        except Exception:
            fenced = await self._coordinator.require_reconciliation(
                attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                failure_code="uidvalidity_reset_incomplete",
            )
            return self._result(
                fenced,
                reset_state=YandexMailUidValidityResetState.INCOMPLETE,
            )
        if receipt.state is YandexMailUidValidityResetState.INCOMPLETE:
            fenced = await self._coordinator.require_reconciliation(
                attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                failure_code="uidvalidity_reset_incomplete",
            )
        elif receipt.state is YandexMailUidValidityResetState.SUSPENDED:
            fenced = await self._coordinator.get_attempt(
                attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
            )
        else:
            fenced = await self._coordinator.require_reconciliation(
                attempt_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                failure_code="uidvalidity_reset_started",
            )
        return self._result(
            fenced,
            reset_receipt=receipt,
            reset_state=receipt.state,
        )

    async def _recover_changed_uidvalidity(
        self,
        *,
        attempt_id: UUID,
        scope: MailboxReadScope,
        folder_reference: UUID,
        stale_uid_validity: int,
        restore_committed_checkpoint: str | None,
        failure_code: str,
        observed_uid_validity: int | None,
    ) -> YandexMailIncrementalPollResult | None:
        if (
            failure_code != "uidvalidity_changed"
            or observed_uid_validity is None
            or observed_uid_validity == stale_uid_validity
        ):
            return None
        return await self._recover_uidvalidity_reset(
            attempt_id=attempt_id,
            scope=scope,
            folder_reference=folder_reference,
            stale_uid_validity=stale_uid_validity,
            observed_uid_validity=observed_uid_validity,
            restore_committed_checkpoint=restore_committed_checkpoint,
        )

    @staticmethod
    def _generation_failure(
        generation: YandexMailFolderGenerationObservation,
        checkpoint: YandexMailIncrementalCheckpoint,
    ) -> tuple[EmailSyncStatus, str] | None:
        if generation.code is YandexMailFolderGenerationCode.WRONG_SCOPE:
            return EmailSyncStatus.SUSPENDED, "scope_not_authorized"
        if (
            generation.workspace_id != checkpoint.workspace_id
            or generation.environment_id != checkpoint.environment_id
            or generation.connection_id != checkpoint.connection_id
            or generation.connection_version_id != checkpoint.connection_version_id
            or generation.mailbox_binding_id != checkpoint.mailbox_binding_id
            or generation.folder_reference != checkpoint.folder_reference
            or generation.authorization_generation
            != checkpoint.authorization_generation
            or generation.configuration_generation
            != checkpoint.configuration_generation
            or generation.scope_generation != checkpoint.scope_generation
        ):
            return EmailSyncStatus.RECONCILIATION_REQUIRED, "generation_stale"
        if (
            generation.code is YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED
            or generation.uid_validity != checkpoint.uid_validity
        ):
            return EmailSyncStatus.RECONCILIATION_REQUIRED, "uidvalidity_changed"
        if (
            not generation.complete
            or generation.code is not YandexMailFolderGenerationCode.COMPLETE
            or generation.uid_next is None
            or generation.selection_command != "EXAMINE"
            or generation.read_only is not True
        ):
            return EmailSyncStatus.INCOMPLETE, "generation_incomplete"
        return None

    @staticmethod
    def _page_failure(
        result: YandexMailUidEnumerationPageResult,
        request: YandexMailUidEnumerationPageRequest,
    ) -> tuple[EmailSyncStatus, str] | None:
        if result.code is YandexMailUidPageCode.WRONG_SCOPE:
            return EmailSyncStatus.SUSPENDED, "scope_not_authorized"
        if (
            result.workspace_id != request.scope.workspace_id
            or result.environment_id != request.scope.environment_id
            or result.connection_id != request.scope.connection_id
            or result.connection_version_id != request.scope.connection_version_id
            or result.mailbox_binding_id != request.scope.mailbox_binding_id
            or result.folder_reference != request.folder_reference
            or result.authorization_generation
            != request.scope.authorization_generation
            or result.configuration_generation
            != request.scope.configuration_generation
            or result.scope_generation != request.scope.scope_generation
        ):
            return EmailSyncStatus.INCOMPLETE, "uid_page_incomplete"
        if result.code is YandexMailUidPageCode.UIDVALIDITY_CHANGED:
            return EmailSyncStatus.RECONCILIATION_REQUIRED, "uidvalidity_changed"
        if (
            not result.complete
            or result.covered_uid_range
            != (request.start_uid, request.end_uid)
            or result.uid_validity != request.uid_validity
        ):
            return EmailSyncStatus.INCOMPLETE, "uid_page_incomplete"
        uids = tuple(message.uid for message in result.messages)
        if (
            uids != tuple(sorted(uids))
            or len(uids) != len(set(uids))
            or any(uid < request.start_uid or uid > request.end_uid for uid in uids)
        ):
            return EmailSyncStatus.INCOMPLETE, "uid_page_incomplete"
        return None

    @staticmethod
    def _page_id(
        checkpoint: YandexMailIncrementalCheckpoint,
        start_uid: int,
        end_uid: int,
    ) -> str:
        binding = (
            f"{checkpoint.connection_version_id}:{checkpoint.mailbox_binding_id}:"
            f"{checkpoint.folder_reference}:{checkpoint.authorization_generation}:"
            f"{checkpoint.configuration_generation}:{checkpoint.scope_generation}:"
            f"{checkpoint.uid_validity}"
        )
        digest = hashlib.sha256(binding.encode("ascii")).hexdigest()[:32]
        return f"imapuid.{digest}.{start_uid}.{end_uid}"

    @staticmethod
    def _baseline_page_id(checkpoint: YandexMailIncrementalCheckpoint) -> str:
        binding = (
            f"{checkpoint.connection_version_id}:{checkpoint.mailbox_binding_id}:"
            f"{checkpoint.folder_reference}:{checkpoint.authorization_generation}:"
            f"{checkpoint.configuration_generation}:{checkpoint.scope_generation}:"
            f"{checkpoint.uid_validity}:{checkpoint.baseline_kind}:"
            f"{checkpoint.baseline_receipt_reference}"
        )
        digest = hashlib.sha256(binding.encode("ascii")).hexdigest()[:32]
        return f"imapuid.{digest}.baseline.{checkpoint.next_uid}"

    @staticmethod
    def _result(
        attempt: EmailSyncAttempt,
        *,
        state: YandexMailIncrementalPollState | None = None,
        covered_uid_range: tuple[int, int] | None = None,
        discovered_uids: tuple[int, ...] = (),
        reset_receipt: YandexMailUidValidityResetReceipt | None = None,
        reset_state: YandexMailUidValidityResetState | None = None,
    ) -> YandexMailIncrementalPollResult:
        if state is None:
            state = {
                EmailSyncStatus.RUNNING: YandexMailIncrementalPollState.APPLIED,
                EmailSyncStatus.INCOMPLETE: YandexMailIncrementalPollState.INCOMPLETE,
                EmailSyncStatus.RECONCILIATION_REQUIRED: (
                    YandexMailIncrementalPollState.RECONCILIATION_REQUIRED
                ),
                EmailSyncStatus.SUSPENDED: YandexMailIncrementalPollState.SUSPENDED,
            }.get(attempt.status, YandexMailIncrementalPollState.INCOMPLETE)
        return YandexMailIncrementalPollResult(
            state=state,
            attempt=attempt,
            covered_uid_range=covered_uid_range,
            discovered_uids=discovered_uids,
            reset_receipt=reset_receipt,
            reset_state=reset_state,
        )


__all__ = [
    "YandexMailIncrementalBaseline",
    "YandexMailIncrementalBaselineVerifier",
    "YandexMailIncrementalCheckpoint",
    "YandexMailIncrementalEffectFactory",
    "YandexMailIncrementalPollResult",
    "YandexMailIncrementalPollState",
    "YandexMailIncrementalPoller",
    "YandexMailIncrementalReadPort",
    "decode_yandex_mail_incremental_checkpoint",
    "encode_yandex_mail_incremental_checkpoint",
    "yandex_mail_uid_effect_key",
]
