"""Conservative lifecycle classification for complete Yandex Mail scans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncAttempt,
    EmailSyncCoordinator,
    EmailSyncPage,
    EmailSyncPageKind,
    EmailSyncStatus,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope
from spine.domain.sources.email_lifecycle import (
    EmailLifecycleCoverage,
    EmailLifecycleLineage,
    EmailLifecycleOutcome,
    EmailLifecycleReceipt,
)
from spine.domain.sources.imap import ImapMessageKey, imap_source_identity


class YandexMailReconciliationMessage(DefinitionModel):
    """Content-minimized canonical lineage used to compare two IMAP scans."""

    schema_version: Literal["yandex-mail-reconciliation-message:v1"] = (
        "yandex-mail-reconciliation-message:v1"
    )
    key: ImapMessageKey
    source_object_id: UUID
    source_revision_id: UUID
    message_id_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    last_absence_receipt_id: UUID | None = None

    @model_validator(mode="after")
    def validate_lineage(self) -> "YandexMailReconciliationMessage":
        if self.source_object_id.int == 0 or self.source_revision_id.int == 0:
            raise ValueError("reconciliation source lineage must be non-zero")
        if (
            self.last_absence_receipt_id is not None
            and self.last_absence_receipt_id.int == 0
        ):
            raise ValueError("absence lineage receipt must be non-zero")
        return self

    @property
    def provider_identity(self) -> str:
        return imap_source_identity(self.key).external_object_id


class YandexMailReconciliationFolderEvidence(DefinitionModel):
    """Committed generation and full-folder pages proving scan coverage."""

    schema_version: Literal["yandex-mail-reconciliation-folder-evidence:v1"] = (
        "yandex-mail-reconciliation-folder-evidence:v1"
    )
    folder_reference: UUID
    generation_page_id: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    folder_complete_page_id: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )

    @model_validator(mode="after")
    def validate_page_identity(self) -> "YandexMailReconciliationFolderEvidence":
        if self.folder_reference.int == 0:
            raise ValueError("folder coverage identity must be non-zero")
        prefix = f"imap.folder.{self.folder_reference.hex}"
        if self.generation_page_id != f"{prefix}.generation" or (
            self.folder_complete_page_id != f"{prefix}.complete"
        ):
            raise ValueError("folder coverage pages must identify the exact folder")
        return self


class YandexMailReconciliationCommand(DefinitionModel):
    schema_version: Literal["yandex-mail-reconciliation-command:v1"] = (
        "yandex-mail-reconciliation-command:v1"
    )
    scope: MailboxReadScope
    attempt_id: UUID
    scan_id: UUID
    scan_completed_at: datetime
    coverage: EmailLifecycleCoverage
    folder_evidence: tuple[YandexMailReconciliationFolderEvidence, ...] = ()
    previous: tuple[YandexMailReconciliationMessage, ...] = ()
    current: tuple[YandexMailReconciliationMessage, ...] = ()

    @model_validator(mode="after")
    def validate_scan(self) -> "YandexMailReconciliationCommand":
        if (
            self.scan_completed_at.tzinfo is None
            or self.scan_completed_at.utcoffset() is None
        ):
            raise ValueError("scan completion timestamp must be timezone-aware")
        if self.attempt_id.int == 0 or self.scan_id.int == 0:
            raise ValueError("reconciliation identities must be non-zero")
        if self.scope.provider_kind != "yandex_mail" or not self.scope.allows(
            EmailOperation.SYNCHRONIZE
        ):
            raise ValueError("reconciliation requires an approved Yandex Mail scope")
        evidence_folders = tuple(
            evidence.folder_reference for evidence in self.folder_evidence
        )
        if len(evidence_folders) != len(set(evidence_folders)):
            raise ValueError("folder coverage evidence must be unique")
        expected_folders = set(self.scope.allowed_folder_references) - set(
            self.scope.excluded_folder_references
        )
        if self.coverage is EmailLifecycleCoverage.COMPLETE_AUTHORIZED and (
            set(evidence_folders) != expected_folders
        ):
            raise ValueError("complete coverage requires every authorized folder")
        if self.coverage is not EmailLifecycleCoverage.COMPLETE_AUTHORIZED and (
            self.folder_evidence
        ):
            raise ValueError("unusable coverage cannot claim completed folders")
        for collection_name, messages in (
            ("previous", self.previous),
            ("current", self.current),
        ):
            identities = tuple(message.provider_identity for message in messages)
            if len(identities) != len(set(identities)):
                raise ValueError(f"{collection_name} message identities must be unique")
            if any(not self._matches_scope(message) for message in messages):
                raise ValueError(
                    "reconciliation message is outside the exact mailbox scope"
                )
        return self

    def _matches_scope(self, message: YandexMailReconciliationMessage) -> bool:
        key = message.key
        return (
            key.workspace_id == self.scope.workspace_id
            and key.connection_id == self.scope.connection_id
            and key.mailbox_binding_id == self.scope.mailbox_binding_id
            and self.scope.allows_location(key.folder_binding_id)
        )


@dataclass(frozen=True, slots=True)
class YandexMailReconciliationResult:
    attempt: EmailSyncAttempt
    receipts: tuple[EmailLifecycleReceipt, ...]


class EmailLifecyclePersistenceError(RuntimeError):
    """Lifecycle evidence did not cross its durable tenant-scoped boundary."""

    def __init__(self) -> None:
        super().__init__("email lifecycle evidence was not durably recorded")


@dataclass(frozen=True, slots=True)
class YandexMailReconciliationCoordinator:
    """Persist content-free evidence without making a tombstone decision."""

    sync: EmailSyncCoordinator

    async def reconcile(
        self, command: YandexMailReconciliationCommand
    ) -> YandexMailReconciliationResult:
        attempt = await self.sync.get_attempt(
            command.attempt_id,
            workspace_id=command.scope.workspace_id,
            environment_id=command.scope.environment_id,
        )
        if attempt.admission != EmailSyncAdmission.from_scope(command.scope):
            raise ValueError("reconciliation attempt does not match its mailbox scope")
        command_evidence = _command_evidence(command)
        replay = self._committed_replay(command_evidence, command, attempt)
        if replay is not None:
            return replay
        admission_state = await self.sync.admission_state(
            attempt.attempt_id,
            workspace_id=command.scope.workspace_id,
            environment_id=command.scope.environment_id,
        )
        if admission_state is EmailSyncAdmissionState.STALE_GENERATION:
            attempt = await self.sync.hold(
                attempt.attempt_id,
                workspace_id=command.scope.workspace_id,
                environment_id=command.scope.environment_id,
                status=EmailSyncStatus.RECONCILIATION_REQUIRED,
                failure_code="stale_generation",
            )
            return YandexMailReconciliationResult(attempt=attempt, receipts=())
        if admission_state in {
            EmailSyncAdmissionState.REVOKED,
            EmailSyncAdmissionState.DENIED,
        }:
            command = command.model_copy(
                update={
                    "coverage": EmailLifecycleCoverage.PERMISSION_LOSS,
                    "folder_evidence": (),
                }
            )
        elif command.coverage is EmailLifecycleCoverage.PERMISSION_LOSS:
            raise ValueError("permission loss requires denied current admission")
        if command.coverage is EmailLifecycleCoverage.COMPLETE_AUTHORIZED:
            self._require_complete_coverage(command, attempt)
        predecessors = await self._verified_predecessors(command)
        receipts = self._classify(command, predecessors)
        digest = hashlib.sha256(
            json.dumps(
                [receipt.model_dump(mode="json") for receipt in receipts],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        page = EmailSyncPage(
            workspace_id=command.scope.workspace_id,
            environment_id=command.scope.environment_id,
            page_kind=EmailSyncPageKind.LIFECYCLE_RECONCILIATION,
            page_id=f"imap.lifecycle.{command.scan_id.hex}",
            requested_checkpoint=attempt.committed_checkpoint,
            publish_checkpoint=False,
            evidence_digest=digest,
            evidence_cursor=command_evidence,
            effects=(),
            lifecycle_receipts=receipts,
        )
        attempt = await self.sync.record_durable_page(
            attempt.attempt_id,
            page,
            workspace_id=command.scope.workspace_id,
            environment_id=command.scope.environment_id,
            receipts=(),
        )
        persisted_page = next(
            (state for state in attempt.pages if state.page_id == page.page_id),
            None,
        )
        if (
            persisted_page is None
            or not persisted_page.committed
            or persisted_page.lifecycle_receipts != receipts
        ):
            raise EmailLifecyclePersistenceError()
        if command.coverage is not EmailLifecycleCoverage.COMPLETE_AUTHORIZED:
            status, failure_code = {
                EmailLifecycleCoverage.PERMISSION_LOSS: (
                    EmailSyncStatus.SUSPENDED,
                    "permission_loss",
                ),
                EmailLifecycleCoverage.PROVIDER_OUTAGE: (
                    EmailSyncStatus.INCOMPLETE,
                    "provider_outage",
                ),
                EmailLifecycleCoverage.INCOMPLETE_SCAN: (
                    EmailSyncStatus.INCOMPLETE,
                    "incomplete_scan",
                ),
            }[command.coverage]
            attempt = await self.sync.hold(
                attempt.attempt_id,
                workspace_id=command.scope.workspace_id,
                environment_id=command.scope.environment_id,
                status=status,
                failure_code=failure_code,
            )
        return YandexMailReconciliationResult(
            attempt=attempt,
            receipts=persisted_page.lifecycle_receipts,
        )

    @staticmethod
    def _committed_replay(
        command_evidence: str,
        command: YandexMailReconciliationCommand,
        attempt: EmailSyncAttempt,
    ) -> YandexMailReconciliationResult | None:
        page_id = f"imap.lifecycle.{command.scan_id.hex}"
        persisted = next(
            (page for page in attempt.pages if page.page_id == page_id),
            None,
        )
        if persisted is None or not persisted.committed:
            return None
        if (
            persisted.page_kind is not EmailSyncPageKind.LIFECYCLE_RECONCILIATION
            or persisted.evidence_cursor != command_evidence
        ):
            raise ValueError("reconciliation scan identity conflicts with durable evidence")
        return YandexMailReconciliationResult(
            attempt=attempt,
            receipts=persisted.lifecycle_receipts,
        )

    def _require_complete_coverage(
        self,
        command: YandexMailReconciliationCommand,
        attempt: EmailSyncAttempt,
    ) -> None:
        uid_validity_by_folder: dict[UUID, int] = {}
        pages = {page.page_id: page for page in attempt.pages}
        for evidence in command.folder_evidence:
            generation = pages.get(evidence.generation_page_id)
            complete = pages.get(evidence.folder_complete_page_id)
            if (
                generation is None
                or not generation.committed
                or generation.page_kind is not EmailSyncPageKind.FOLDER_GENERATION
                or complete is None
                or not complete.committed
                or complete.page_kind is not EmailSyncPageKind.FOLDER_COMPLETE
            ):
                raise ValueError("complete coverage lacks durable folder evidence")
            uid_validity_by_folder[evidence.folder_reference] = (
                _uid_validity(generation.evidence_cursor)
            )
        for message in (*command.previous, *command.current):
            if (
                uid_validity_by_folder.get(message.key.folder_binding_id)
                != message.key.uid_validity
            ):
                raise ValueError("UIDVALIDITY change requires separate reconciliation")

    async def _verified_predecessors(
        self,
        command: YandexMailReconciliationCommand,
    ) -> dict[str, EmailLifecycleReceipt]:
        if command.coverage is not EmailLifecycleCoverage.COMPLETE_AUTHORIZED:
            return {}
        current = {message.provider_identity: message for message in command.current}
        predecessors: dict[str, EmailLifecycleReceipt] = {}
        for prior in command.previous:
            if (
                prior.provider_identity not in current
                or prior.last_absence_receipt_id is None
            ):
                continue
            receipt = await self.sync.resolve_lifecycle_receipt(
                prior.last_absence_receipt_id,
                workspace_id=command.scope.workspace_id,
                environment_id=command.scope.environment_id,
            )
            if (
                receipt is None
                or receipt.outcome is not EmailLifecycleOutcome.ABSENCE_EVIDENCE
                or not receipt.authoritative_absence
                or receipt.provider_identity != prior.provider_identity
                or receipt.source_object_id != prior.source_object_id
                or receipt.source_revision_id != prior.source_revision_id
            ):
                raise ValueError("reappearance predecessor evidence is unavailable")
            predecessors[prior.provider_identity] = receipt
        return predecessors

    @staticmethod
    def _classify(
        command: YandexMailReconciliationCommand,
        predecessors: dict[str, EmailLifecycleReceipt],
    ) -> tuple[EmailLifecycleReceipt, ...]:
        failure_outcomes = {
            EmailLifecycleCoverage.INCOMPLETE_SCAN: EmailLifecycleOutcome.INCOMPLETE_SCAN,
            EmailLifecycleCoverage.PERMISSION_LOSS: EmailLifecycleOutcome.PERMISSION_LOSS,
            EmailLifecycleCoverage.PROVIDER_OUTAGE: EmailLifecycleOutcome.PROVIDER_OUTAGE,
        }
        failure = failure_outcomes.get(command.coverage)
        if failure is not None:
            return (_receipt(command, outcome=failure),)

        previous = {message.provider_identity: message for message in command.previous}
        current = {message.provider_identity: message for message in command.current}
        receipts: list[EmailLifecycleReceipt] = []
        absence_by_identity: dict[str, EmailLifecycleReceipt] = {}

        for identity in sorted(set(previous) - set(current)):
            prior = previous[identity]
            receipt = _receipt(
                command,
                outcome=EmailLifecycleOutcome.ABSENCE_EVIDENCE,
                message=prior,
                authoritative_absence=True,
            )
            absence_by_identity[identity] = receipt
            receipts.append(receipt)

        for identity in sorted(current):
            observed = current[identity]
            prior = previous.get(identity)
            if (
                prior is not None
                and prior.source_object_id != observed.source_object_id
            ):
                raise ValueError(
                    "exact provider identity cannot be rebound during reconciliation"
                )
            predecessor = predecessors.get(identity)
            if predecessor is not None:
                receipts.append(
                    _receipt(
                        command,
                        outcome=EmailLifecycleOutcome.REAPPEARED,
                        message=observed,
                        lineage=EmailLifecycleLineage.PRESERVED_IDENTITY,
                        predecessor_receipt_id=predecessor.receipt_id,
                        identity_continuity_proven=True,
                    )
                )
            else:
                receipts.append(
                    _receipt(
                        command,
                        outcome=EmailLifecycleOutcome.OBSERVED,
                        message=observed,
                    )
                )

        new_messages = tuple(
            current[identity] for identity in sorted(set(current) - set(previous))
        )
        for observed in new_messages:
            for prior in command.previous:
                match = _similarity(observed, prior)
                if match is None:
                    continue
                basis, similarity_digest = match
                receipts.append(
                    _receipt(
                        command,
                        outcome=EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE,
                        message=observed,
                        candidate=prior,
                        lineage=EmailLifecycleLineage.DISTINCT_IDENTITY_CANDIDATE,
                        predecessor_receipt_id=(
                            absence_by_identity[prior.provider_identity].receipt_id
                            if prior.provider_identity in absence_by_identity
                            else None
                        ),
                        similarity_basis=basis,
                        similarity_digest=similarity_digest,
                    )
                )

        for index, observed in enumerate(new_messages):
            for candidate in new_messages[index + 1 :]:
                match = _similarity(observed, candidate)
                if match is None:
                    continue
                basis, similarity_digest = match
                receipts.append(
                    _receipt(
                        command,
                        outcome=EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE,
                        message=observed,
                        candidate=candidate,
                        lineage=EmailLifecycleLineage.DISTINCT_IDENTITY_CANDIDATE,
                        similarity_basis=basis,
                        similarity_digest=similarity_digest,
                    )
                )
        return tuple(receipts)


def _similarity(
    first: YandexMailReconciliationMessage,
    second: YandexMailReconciliationMessage,
) -> tuple[Literal["message_id", "content_sha256"], str] | None:
    if (
        first.message_id_digest is not None
        and first.message_id_digest == second.message_id_digest
    ):
        return "message_id", first.message_id_digest
    if (
        first.content_sha256 is not None
        and first.content_sha256 == second.content_sha256
    ):
        return "content_sha256", first.content_sha256
    return None


def _uid_validity(cursor: str | None) -> int:
    try:
        prefix, version, uid_validity, uid_next = (cursor or "").split(":")
        value = int(uid_validity)
        next_value = int(uid_next)
    except (TypeError, ValueError):
        raise ValueError("folder generation evidence is invalid") from None
    if (
        prefix != "imap-generation"
        or version != "v1"
        or not 1 <= value <= 2**32 - 1
        or not 1 <= next_value <= 2**32 - 1
    ):
        raise ValueError("folder generation evidence is invalid")
    return value


def _command_evidence(command: YandexMailReconciliationCommand) -> str:
    payload = command.model_dump_json(exclude_none=False).encode("utf-8")
    return f"email-lifecycle-command:v1:{hashlib.sha256(payload).hexdigest()}"


def _receipt(
    command: YandexMailReconciliationCommand,
    *,
    outcome: EmailLifecycleOutcome,
    message: YandexMailReconciliationMessage | None = None,
    candidate: YandexMailReconciliationMessage | None = None,
    lineage: EmailLifecycleLineage = EmailLifecycleLineage.NONE,
    predecessor_receipt_id: UUID | None = None,
    similarity_basis: Literal["message_id", "content_sha256"] | None = None,
    similarity_digest: str | None = None,
    identity_continuity_proven: bool = False,
    authoritative_absence: bool = False,
) -> EmailLifecycleReceipt:
    identity = message.provider_identity if message is not None else "scan"
    candidate_identity = (
        candidate.provider_identity if candidate is not None else "none"
    )
    receipt_id = uuid5(
        NAMESPACE_URL,
        ":".join(
            (
                "spine:email-lifecycle-receipt:v1",
                str(command.scope.workspace_id),
                str(command.scan_id),
                outcome.value,
                identity,
                candidate_identity,
            )
        ),
    )
    return EmailLifecycleReceipt(
        receipt_id=receipt_id,
        workspace_id=command.scope.workspace_id,
        environment_id=command.scope.environment_id,
        scan_id=command.scan_id,
        outcome=outcome,
        coverage=command.coverage,
        lineage=lineage,
        provider_identity=message.provider_identity if message is not None else None,
        candidate_provider_identity=(
            candidate.provider_identity if candidate is not None else None
        ),
        source_object_id=message.source_object_id if message is not None else None,
        source_revision_id=message.source_revision_id if message is not None else None,
        candidate_source_object_id=(
            candidate.source_object_id if candidate is not None else None
        ),
        candidate_source_revision_id=(
            candidate.source_revision_id if candidate is not None else None
        ),
        predecessor_receipt_id=predecessor_receipt_id,
        similarity_basis=similarity_basis,
        similarity_digest=similarity_digest,
        identity_continuity_proven=identity_continuity_proven,
        authoritative_absence=authoritative_absence,
        tombstone_authorized=False,
        observed_at=command.scan_completed_at,
    )


__all__ = [
    "EmailLifecyclePersistenceError",
    "EmailLifecycleCoverage",
    "EmailLifecycleLineage",
    "EmailLifecycleOutcome",
    "YandexMailReconciliationCommand",
    "YandexMailReconciliationCoordinator",
    "YandexMailReconciliationFolderEvidence",
    "YandexMailReconciliationMessage",
    "YandexMailReconciliationResult",
]
