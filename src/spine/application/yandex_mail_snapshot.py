"""Durable bounded initial snapshots across approved Yandex Mail folders."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

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
from spine.domain.connections.email_scope import (
    EmailOperation,
    MailboxReadScope,
)


class YandexMailSnapshotFolderCode(str, Enum):
    COMPLETE = "complete"
    PARTIAL_RESPONSE = "partial_response"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_LIMIT_EXCEEDED = "response_limit_exceeded"
    APPROVED_FOLDER_MISSING = "approved_folder_missing"
    APPROVED_FOLDER_AMBIGUOUS = "approved_folder_ambiguous"
    DISCOVERY_UNAVAILABLE = "discovery_unavailable"


class YandexMailSnapshotGenerationCode(str, Enum):
    COMPLETE = "complete"
    WRONG_SCOPE = "wrong_scope"
    WRONG_COMMAND = "wrong_command"
    SERVER_REJECTED = "server_rejected"
    UIDNOTSTICKY = "uid_not_sticky"
    UIDVALIDITY_MISSING = "uid_validity_missing"
    UIDVALIDITY_INVALID = "uid_validity_invalid"
    UIDVALIDITY_CHANGED = "uid_validity_changed"
    UIDNEXT_MISSING = "uid_next_missing"
    UIDNEXT_INVALID = "uid_next_invalid"
    INCONSISTENT_SERVER_STATE = "inconsistent_server_state"
    MALFORMED_RESPONSE = "malformed_response"
    READ_WRITE_SELECTION = "read_write_selection"


class YandexMailSnapshotUidPageCode(str, Enum):
    COMPLETE = "complete"
    PARTIAL_BATCH = "partial_batch"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_LIMIT_EXCEEDED = "response_limit_exceeded"
    UNSAFE_COMMANDS = "unsafe_commands"
    WRONG_SCOPE = "wrong_scope"
    UIDVALIDITY_CHANGED = "uid_validity_changed"
    SERVER_REJECTED = "server_rejected"


class YandexMailSnapshotFolderDiscovery(DefinitionModel):
    workspace_id: UUID
    environment_id: UUID
    complete: bool
    code: YandexMailSnapshotFolderCode
    discovered_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    folders: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_folders(self) -> "YandexMailSnapshotFolderDiscovery":
        if len(self.folders) != len(set(self.folders)):
            raise ValueError("snapshot folder references must be unique")
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("snapshot discovery scope must be non-zero")
        return self


class YandexMailSnapshotFolderGeneration(DefinitionModel):
    complete: bool
    code: YandexMailSnapshotGenerationCode
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID | None = None
    authorization_generation: int
    configuration_generation: int
    scope_generation: int
    uid_validity: int | None = Field(default=None, ge=1, le=2**32 - 1)
    uid_next: int | None = Field(default=None, ge=1, le=2**32 - 1)
    read_only: bool | None = None

    def matches(self, scope: MailboxReadScope, folder_reference: UUID) -> bool:
        return (
            self.complete
            and self.code is YandexMailSnapshotGenerationCode.COMPLETE
            and self.folder_reference == folder_reference
            and self.workspace_id == scope.workspace_id
            and self.environment_id == scope.environment_id
            and self.connection_id == scope.connection_id
            and self.connection_version_id == scope.connection_version_id
            and self.mailbox_binding_id == scope.mailbox_binding_id
            and self.authorization_generation == scope.authorization_generation
            and self.configuration_generation == scope.configuration_generation
            and self.scope_generation == scope.scope_generation
            and self.uid_validity is not None
            and self.uid_next is not None
            and self.read_only is True
        )

    def evidence_cursor(self) -> str:
        if self.uid_validity is None or self.uid_next is None:
            raise ValueError("complete generation evidence is required")
        return f"imap-generation:v1:{self.uid_validity}:{self.uid_next}"

    @classmethod
    def from_evidence_cursor(
        cls,
        scope: MailboxReadScope,
        folder_reference: UUID,
        cursor: str | None,
    ) -> "YandexMailSnapshotFolderGeneration | None":
        try:
            prefix, version, uid_validity, uid_next = (cursor or "").split(":")
            if prefix != "imap-generation" or version != "v1":
                return None
            return cls(
                complete=True,
                code=YandexMailSnapshotGenerationCode.COMPLETE,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                connection_id=scope.connection_id,
                connection_version_id=scope.connection_version_id,
                mailbox_binding_id=scope.mailbox_binding_id,
                folder_reference=folder_reference,
                authorization_generation=scope.authorization_generation,
                configuration_generation=scope.configuration_generation,
                scope_generation=scope.scope_generation,
                uid_validity=int(uid_validity),
                uid_next=int(uid_next),
                read_only=True,
            )
        except (TypeError, ValueError):
            return None


class YandexMailSnapshotUidPageRequest(DefinitionModel):
    scope: MailboxReadScope
    folder_reference: UUID
    generation: YandexMailSnapshotFolderGeneration
    start_uid: int = Field(ge=1, le=2**32 - 1)
    max_items: int = Field(ge=1, le=5000)
    max_response_bytes: int = Field(ge=1024, le=8_388_608)

    @model_validator(mode="after")
    def validate_generation(self) -> "YandexMailSnapshotUidPageRequest":
        if (
            not self.generation.matches(self.scope, self.folder_reference)
            or self.start_uid > self.uid_next
        ):
            raise ValueError("UID page request is outside its approved generation")
        return self

    @property
    def uid_validity(self) -> int:
        assert self.generation.uid_validity is not None
        return self.generation.uid_validity

    @property
    def uid_next(self) -> int:
        assert self.generation.uid_next is not None
        return self.generation.uid_next

    @property
    def end_uid(self) -> int:
        return min(self.start_uid + self.max_items - 1, self.uid_next - 1)

    @property
    def continuation_uid(self) -> int | None:
        next_uid = self.end_uid + 1
        return next_uid if next_uid < self.uid_next else None


class YandexMailSnapshotMessageMetadata(DefinitionModel):
    uid: int = Field(ge=1, le=2**32 - 1)
    size_octets: int = Field(ge=0)


class YandexMailSnapshotUidPage(DefinitionModel):
    complete: bool
    code: YandexMailSnapshotUidPageCode
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    folder_reference: UUID
    authorization_generation: int
    configuration_generation: int
    scope_generation: int
    uid_validity: int = Field(ge=1, le=2**32 - 1)
    covered_uid_range: tuple[int, int] | None = None
    messages: tuple[YandexMailSnapshotMessageMetadata, ...] = ()
    continuation_uid: int | None = Field(default=None, ge=1, le=2**32 - 1)

    def matches(self, request: YandexMailSnapshotUidPageRequest) -> bool:
        scope = request.scope
        return (
            self.workspace_id == scope.workspace_id
            and self.environment_id == scope.environment_id
            and self.connection_id == scope.connection_id
            and self.connection_version_id == scope.connection_version_id
            and self.mailbox_binding_id == scope.mailbox_binding_id
            and self.folder_reference == request.folder_reference
            and self.authorization_generation == scope.authorization_generation
            and self.configuration_generation == scope.configuration_generation
            and self.scope_generation == scope.scope_generation
        )


class YandexMailRequiredFolderGeneration(DefinitionModel):
    """Exact folder generation that complete discovery must confirm."""

    folder_reference: UUID
    uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)

    @model_validator(mode="after")
    def validate_reference(self) -> "YandexMailRequiredFolderGeneration":
        if self.folder_reference.int == 0:
            raise ValueError("required folder reference must be non-zero")
        return self


class YandexMailInitialSnapshotCommand(DefinitionModel):
    """One finite initial attempt under an exact immutable mailbox scope."""

    schema_version: Literal["yandex-mail-initial-snapshot-command:v1"] = (
        "yandex-mail-initial-snapshot-command:v1"
    )
    scope: MailboxReadScope
    attempt_key: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    uid_page_size: int = Field(ge=1, le=5000)
    uid_page_response_bytes: int = Field(ge=1024, le=8_388_608)
    max_uid_pages_per_folder: int = Field(ge=1, le=10_000)
    max_message_bytes: int = Field(ge=1, le=268_435_456)
    access_policy_version_id: UUID
    required_folder_generations: tuple[YandexMailRequiredFolderGeneration, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> "YandexMailInitialSnapshotCommand":
        required = (
            EmailOperation.ENUMERATE_MAILBOX,
            EmailOperation.OBSERVE_FOLDER_GENERATION,
            EmailOperation.ENUMERATE_FOLDER_UIDS,
            EmailOperation.READ_RAW_MESSAGE,
            EmailOperation.SYNCHRONIZE,
        )
        if self.scope.provider_kind != "yandex_mail" or any(
            not self.scope.allows(operation) for operation in required
        ) or self.access_policy_version_id.int == 0:
            raise ValueError("snapshot scope lacks an approved Yandex Mail operation")
        required_folders = tuple(
            requirement.folder_reference
            for requirement in self.required_folder_generations
        )
        if (
            len(required_folders) != len(set(required_folders))
            or any(
                not self.scope.allows_location(requirement.folder_reference)
                for requirement in self.required_folder_generations
            )
        ):
            raise ValueError("required folder generation is outside the approved scope")
        return self

    def required_uid_validity(self, folder_reference: UUID) -> int | None:
        return next(
            (
                requirement.uid_validity
                for requirement in self.required_folder_generations
                if requirement.folder_reference == folder_reference
            ),
            None,
        )


class YandexMailInitialSnapshotReader(Protocol):
    """Admitted provider reads; implementations keep IMAP details private."""

    async def discover_folders(
        self, scope: MailboxReadScope
    ) -> YandexMailSnapshotFolderDiscovery: ...

    async def observe_folder_generation(
        self, scope: MailboxReadScope, folder_reference: UUID
    ) -> YandexMailSnapshotFolderGeneration: ...

    async def enumerate_uid_page(
        self, request: YandexMailSnapshotUidPageRequest
    ) -> YandexMailSnapshotUidPage: ...


class YandexMailSnapshotMessageCommand(DefinitionModel):
    """Canonical message-effect request derived from qualified UID evidence."""

    schema_version: Literal["yandex-mail-snapshot-message:v1"] = (
        "yandex-mail-snapshot-message:v1"
    )
    scope: MailboxReadScope
    folder_reference: UUID
    uid_validity: int = Field(ge=1, le=2**32 - 1)
    uid: int = Field(ge=1, le=2**32 - 1)
    size_octets: int = Field(ge=0)
    max_message_bytes: int = Field(ge=1, le=268_435_456)
    access_policy_version_id: UUID
    effect_key: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )

    @model_validator(mode="after")
    def validate_scope(self) -> "YandexMailSnapshotMessageCommand":
        if (
            not self.scope.allows(EmailOperation.READ_RAW_MESSAGE)
            or not self.scope.allows_location(self.folder_reference)
            or self.access_policy_version_id.int == 0
        ):
            raise ValueError("snapshot message is outside its approved scope")
        return self


class YandexMailSnapshotMessageReceipt(DefinitionModel):
    """Canonical acceptance receipt safe to add to the durable sync ledger."""

    schema_version: Literal["yandex-mail-snapshot-message-receipt:v1"] = (
        "yandex-mail-snapshot-message-receipt:v1"
    )
    effect_key: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    workspace_id: UUID
    environment_id: UUID
    canonical_acceptance_id: UUID
    receipt_reference: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    durable: Literal[True] = True

    @model_validator(mode="after")
    def validate_reference(self) -> "YandexMailSnapshotMessageReceipt":
        if (
            self.workspace_id.int == 0
            or self.environment_id.int == 0
            or self.canonical_acceptance_id.int == 0
        ):
            raise ValueError("canonical acceptance reference must be non-zero")
        return self


class YandexMailSnapshotMessagePort(Protocol):
    async def apply(
        self, command: YandexMailSnapshotMessageCommand
    ) -> YandexMailSnapshotMessageReceipt: ...


@dataclass(frozen=True, slots=True)
class YandexMailInitialSnapshotResult:
    attempt: EmailSyncAttempt
    completed_folders: int
    completed_uid_pages: int
    durable_message_effects: int


@dataclass(frozen=True, slots=True)
class YandexMailInitialSnapshotCoordinator:
    """Persist folder/page/message coverage before one aggregate checkpoint."""

    sync: EmailSyncCoordinator
    reader: YandexMailInitialSnapshotReader
    messages: YandexMailSnapshotMessagePort

    async def run(
        self, command: YandexMailInitialSnapshotCommand
    ) -> YandexMailInitialSnapshotResult:
        scope = command.scope
        attempt = await self.sync.create_attempt(scope, attempt_key=command.attempt_key)
        if attempt.status in {
            EmailSyncStatus.COMPLETE,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            EmailSyncStatus.SUSPENDED,
            EmailSyncStatus.FAILED,
        }:
            return self._result(attempt)

        try:
            discovery = await self.reader.discover_folders(scope)
        except Exception:
            return await self._incomplete(attempt, "folder_discovery_unavailable")
        if not discovery.complete:
            return await self._incomplete(
                attempt, f"folder_discovery_{discovery.code.value}"
            )
        expected_folders = set(scope.allowed_folder_references) - set(
            scope.excluded_folder_references
        )
        if (
            discovery.workspace_id != scope.workspace_id
            or discovery.environment_id != scope.environment_id
            or discovery.approved_count != len(discovery.folders)
            or set(discovery.folders) != expected_folders
        ):
            return await self._incomplete(attempt, "folder_coverage_inconsistent")

        ordered_folders = tuple(sorted(discovery.folders, key=lambda value: value.int))
        discovery_digest = self._digest(
            {
                "folders": [str(folder) for folder in ordered_folders],
                "scope_generation": scope.scope_generation,
            }
        )
        discovery_page = EmailSyncPage(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            page_kind=EmailSyncPageKind.DISCOVERY,
            page_id="imap.discovery",
            requested_checkpoint=attempt.committed_checkpoint,
            publish_checkpoint=False,
            evidence_digest=discovery_digest,
            effects=(),
        )
        known_discovery = self._known_page(attempt, discovery_page.page_id)
        if (
            known_discovery is not None
            and known_discovery.evidence_digest != discovery_digest
        ):
            return await self._reconcile(attempt, "folder_coverage_changed")
        attempt = await self.sync.record_durable_page(
            attempt.attempt_id,
            discovery_page,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            receipts=(),
        )
        if attempt.status is not EmailSyncStatus.RUNNING:
            return self._result(attempt)

        aggregate_evidence: list[dict[str, object]] = []
        completed_folders = 0
        completed_uid_pages = 0
        durable_message_effects = 0
        for folder_reference in ordered_folders:
            generation, attempt = await self._generation(
                command, attempt, folder_reference
            )
            if generation is None:
                return self._result(attempt)

            start_uid = 1
            folder_pages: list[str] = []
            for _page_ordinal in range(command.max_uid_pages_per_folder):
                request = YandexMailSnapshotUidPageRequest(
                    scope=scope,
                    folder_reference=folder_reference,
                    max_items=command.uid_page_size,
                    max_response_bytes=command.uid_page_response_bytes,
                    generation=generation,
                    start_uid=start_uid,
                )
                try:
                    uid_page = await self.reader.enumerate_uid_page(request)
                except Exception:
                    return await self._incomplete(attempt, "uid_page_unavailable")
                if not uid_page.matches(request):
                    return await self._incomplete(attempt, "uid_page_inconsistent")
                if not uid_page.complete:
                    if (
                        uid_page.code
                        is YandexMailSnapshotUidPageCode.UIDVALIDITY_CHANGED
                    ):
                        return await self._reconcile(
                            attempt, "folder_generation_changed"
                        )
                    return await self._incomplete(
                        attempt, f"uid_page_{uid_page.code.value}"
                    )
                if uid_page.uid_validity != request.uid_validity:
                    return await self._incomplete(attempt, "uid_page_inconsistent")

                effects: list[EmailSyncEffect] = []
                receipts: list[EmailSyncEffectReceipt] = []
                for message in uid_page.messages:
                    attempt = await self.sync.check_effect_admission(
                        attempt.attempt_id,
                        workspace_id=scope.workspace_id,
                        environment_id=scope.environment_id,
                    )
                    if attempt.status is not EmailSyncStatus.RUNNING:
                        return self._result(attempt)
                    effect_key = self._message_effect_key(
                        folder_reference, generation.uid_validity, message.uid
                    )
                    try:
                        receipt = await self.messages.apply(
                            YandexMailSnapshotMessageCommand(
                                scope=scope,
                                folder_reference=folder_reference,
                                uid_validity=generation.uid_validity,
                                uid=message.uid,
                                size_octets=message.size_octets,
                                max_message_bytes=command.max_message_bytes,
                                access_policy_version_id=(
                                    command.access_policy_version_id
                                ),
                                effect_key=effect_key,
                            )
                        )
                    except Exception:
                        return await self._incomplete(
                            attempt, "message_effect_incomplete"
                        )
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
                        return await self._incomplete(
                            attempt, "message_effect_incomplete"
                        )
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

                page_digest = self._uid_page_digest(uid_page)
                staged_page = EmailSyncPage(
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    page_kind=EmailSyncPageKind.UID_PAGE,
                    page_id=self._uid_page_id(folder_reference, start_uid),
                    requested_checkpoint=attempt.committed_checkpoint,
                    publish_checkpoint=False,
                    evidence_digest=page_digest,
                    evidence_cursor=(
                        str(uid_page.continuation_uid)
                        if uid_page.continuation_uid is not None
                        else "terminal"
                    ),
                    effects=tuple(effects),
                )
                known = self._known_page(attempt, staged_page.page_id)
                if known is not None and known.evidence_digest != page_digest:
                    return await self._reconcile(attempt, "uid_page_replay_changed")
                attempt = await self.sync.record_durable_page(
                    attempt.attempt_id,
                    staged_page,
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    receipts=tuple(receipts),
                )
                if attempt.status is not EmailSyncStatus.RUNNING:
                    return self._result(attempt)
                completed_uid_pages += 1
                durable_message_effects += len(effects)
                folder_pages.append(page_digest)
                if uid_page.continuation_uid is None:
                    break
                if uid_page.continuation_uid <= start_uid:
                    return await self._incomplete(
                        attempt, "uid_page_continuation_invalid"
                    )
                start_uid = uid_page.continuation_uid
            else:
                return await self._incomplete(attempt, "uid_page_budget_exhausted")

            folder_evidence = {
                "folder_reference": str(folder_reference),
                "uid_validity": generation.uid_validity,
                "uid_next": generation.uid_next,
                "pages": folder_pages,
            }
            folder_digest = self._digest(folder_evidence)
            folder_complete = EmailSyncPage(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                page_kind=EmailSyncPageKind.FOLDER_COMPLETE,
                page_id=f"imap.folder.{folder_reference.hex}.complete",
                requested_checkpoint=attempt.committed_checkpoint,
                publish_checkpoint=False,
                evidence_digest=folder_digest,
                effects=(),
            )
            known = self._known_page(attempt, folder_complete.page_id)
            if known is not None and known.evidence_digest != folder_digest:
                return await self._reconcile(attempt, "folder_coverage_changed")
            attempt = await self.sync.record_durable_page(
                attempt.attempt_id,
                folder_complete,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                receipts=(),
            )
            if attempt.status is not EmailSyncStatus.RUNNING:
                return self._result(attempt)
            completed_folders += 1
            aggregate_evidence.append(folder_evidence)

        aggregate_digest = self._digest(aggregate_evidence)
        terminal_page = EmailSyncPage(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            page_kind=EmailSyncPageKind.AGGREGATE_CHECKPOINT,
            page_id=f"imap.aggregate.{aggregate_digest[:32]}",
            requested_checkpoint=attempt.committed_checkpoint,
            next_checkpoint=f"imap-snapshot:v1:{aggregate_digest}",
            terminal=True,
            evidence_digest=aggregate_digest,
            effects=(),
        )
        attempt = await self.sync.record_durable_page(
            attempt.attempt_id,
            terminal_page,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            receipts=(),
        )
        return YandexMailInitialSnapshotResult(
            attempt=attempt,
            completed_folders=completed_folders,
            completed_uid_pages=completed_uid_pages,
            durable_message_effects=durable_message_effects,
        )

    async def _generation(
        self,
        command: YandexMailInitialSnapshotCommand,
        attempt: EmailSyncAttempt,
        folder_reference: UUID,
    ) -> tuple[YandexMailSnapshotFolderGeneration | None, EmailSyncAttempt]:
        scope = command.scope
        page_id = self._generation_page_id(folder_reference)
        known = self._known_page(attempt, page_id)
        if known is not None and known.committed:
            generation = YandexMailSnapshotFolderGeneration.from_evidence_cursor(
                scope, folder_reference, known.evidence_cursor
            )
            if generation is None:
                attempt = await self.sync.require_reconciliation(
                    attempt.attempt_id,
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    failure_code="folder_generation_evidence_invalid",
                )
                return None, attempt
            return generation, attempt

        try:
            generation = await self.reader.observe_folder_generation(
                scope, folder_reference
            )
        except Exception:
            result = await self._incomplete(attempt, "folder_generation_unavailable")
            return None, result.attempt
        if not generation.matches(scope, folder_reference):
            if (
                generation.code
                is YandexMailSnapshotGenerationCode.UIDVALIDITY_CHANGED
            ):
                result = await self._reconcile(attempt, "folder_generation_changed")
            else:
                result = await self._incomplete(
                    attempt, f"folder_generation_{generation.code.value}"
                )
            return None, result.attempt

        required_uid_validity = command.required_uid_validity(folder_reference)
        if (
            required_uid_validity is not None
            and generation.uid_validity != required_uid_validity
        ):
            result = await self._reconcile(attempt, "folder_generation_changed")
            return None, result.attempt

        evidence_cursor = generation.evidence_cursor()
        evidence_digest = self._digest(evidence_cursor)
        attempt = await self.sync.record_durable_page(
            attempt.attempt_id,
            EmailSyncPage(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                page_kind=EmailSyncPageKind.FOLDER_GENERATION,
                page_id=page_id,
                requested_checkpoint=attempt.committed_checkpoint,
                publish_checkpoint=False,
                evidence_digest=evidence_digest,
                evidence_cursor=evidence_cursor,
                effects=(),
            ),
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            receipts=(),
        )
        if attempt.status is not EmailSyncStatus.RUNNING:
            return None, attempt
        return generation, attempt

    async def _incomplete(
        self, attempt: EmailSyncAttempt, failure_code: str
    ) -> YandexMailInitialSnapshotResult:
        admission = attempt.admission
        attempt = await self.sync.mark_incomplete(
            attempt.attempt_id,
            workspace_id=admission.workspace_id,
            environment_id=admission.environment_id,
            failure_code=failure_code[:64],
        )
        return self._result(attempt)

    async def _reconcile(
        self, attempt: EmailSyncAttempt, failure_code: str
    ) -> YandexMailInitialSnapshotResult:
        admission = attempt.admission
        attempt = await self.sync.require_reconciliation(
            attempt.attempt_id,
            workspace_id=admission.workspace_id,
            environment_id=admission.environment_id,
            failure_code=failure_code,
        )
        return self._result(attempt)

    @staticmethod
    def _result(attempt: EmailSyncAttempt) -> YandexMailInitialSnapshotResult:
        message_effects = sum(len(page.effect_receipts) for page in attempt.pages)
        uid_pages = sum(
            page.page_kind is EmailSyncPageKind.UID_PAGE for page in attempt.pages
        )
        folders = sum(
            page.page_kind is EmailSyncPageKind.FOLDER_COMPLETE
            for page in attempt.pages
        )
        return YandexMailInitialSnapshotResult(
            attempt=attempt,
            completed_folders=folders,
            completed_uid_pages=uid_pages,
            durable_message_effects=message_effects,
        )

    @staticmethod
    def _known_page(attempt: EmailSyncAttempt, page_id: str):
        return next((page for page in attempt.pages if page.page_id == page_id), None)

    @staticmethod
    def _generation_page_id(folder_reference: UUID) -> str:
        return f"imap.folder.{folder_reference.hex}.generation"

    @staticmethod
    def _uid_page_id(folder_reference: UUID, start_uid: int) -> str:
        return f"imap.folder.{folder_reference.hex}.uids.{start_uid}"

    @staticmethod
    def _message_effect_key(
        folder_reference: UUID, uid_validity: int, uid: int
    ) -> str:
        return f"imap.message.{folder_reference.hex}.{uid_validity}.{uid}"

    @classmethod
    def _uid_page_digest(cls, page: YandexMailSnapshotUidPage) -> str:
        return cls._digest(
            {
                "folder_reference": str(page.folder_reference),
                "uid_validity": page.uid_validity,
                "covered_uid_range": page.covered_uid_range,
                "messages": [
                    {"uid": message.uid, "size_octets": message.size_octets}
                    for message in page.messages
                ],
                "continuation_uid": page.continuation_uid,
            }
        )

    @staticmethod
    def _digest(value: object) -> str:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


__all__ = [
    "YandexMailInitialSnapshotCommand",
    "YandexMailInitialSnapshotCoordinator",
    "YandexMailInitialSnapshotReader",
    "YandexMailInitialSnapshotResult",
    "YandexMailRequiredFolderGeneration",
    "YandexMailSnapshotFolderCode",
    "YandexMailSnapshotFolderDiscovery",
    "YandexMailSnapshotFolderGeneration",
    "YandexMailSnapshotGenerationCode",
    "YandexMailSnapshotMessageCommand",
    "YandexMailSnapshotMessageMetadata",
    "YandexMailSnapshotMessagePort",
    "YandexMailSnapshotMessageReceipt",
    "YandexMailSnapshotUidPage",
    "YandexMailSnapshotUidPageCode",
    "YandexMailSnapshotUidPageRequest",
]
