"""Admitted Gmail initial-snapshot reads and canonical message acceptance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.application.connections.email_read import (
    MailboxReadAdmission,
    MailboxReadCommand,
    MailboxScopeRepository,
)
from spine.application.connections.provider import ProviderConnectionRepository
from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionOrchestrator,
)
from spine.application.email_sync import EmailSyncAttempt
from spine.application.gmail_initial_snapshot import (
    GmailHistoryBaseline,
    GmailHistoryBaselineState,
    GmailSnapshotMessageCommand,
    GmailSnapshotMessageReceipt,
    GmailSnapshotPage,
    GmailSnapshotPageRequest,
    gmail_snapshot_effect_key,
)
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.connectors.gmail import (
    GmailHistoryReadCommand,
    GmailMailboxReader,
    GmailMessageIdPage,
    GmailRawMessageResult,
    GmailReadClient,
)
from spine.connectors.gmail_checkpoints import (
    decode_gmail_history_checkpoint,
    decode_gmail_snapshot_page_checkpoint,
    encode_gmail_history_checkpoint,
    encode_gmail_snapshot_page_checkpoint,
)
from spine.connectors.gmail_history import (
    GmailHistoryChangeKind,
    GmailHistoryResult,
    gmail_history_scope_digest,
)
from spine.connectors.gmail_incremental import GmailIncrementalHistoryApplier
from spine.connectors.gmail_observation import GmailCanonicalObservationReader
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope
from spine.domain.connections.provider import ProviderConnection


class GmailProfileResponse(DefinitionModel):
    """Allowlisted users.getProfile response needed for a no-gap baseline."""

    history_id: str | None = Field(default=None, pattern=r"^[0-9]+$")


class GmailHistoryBaselineReadCommand(MailboxReadCommand):
    schema_version: Literal["gmail-history-baseline-read:v1"] = (
        "gmail-history-baseline-read:v1"
    )

    @model_validator(mode="after")
    def validate_operation(self) -> "GmailHistoryBaselineReadCommand":
        if self.operation is not EmailOperation.SYNCHRONIZE:
            raise ValueError("Gmail baseline capture requires synchronize operation")
        return self


class GmailSnapshotPageReadCommand(MailboxReadCommand):
    schema_version: Literal["gmail-snapshot-page-read:v1"] = (
        "gmail-snapshot-page-read:v1"
    )
    checkpoint: str | None = Field(default=None, max_length=4096, repr=False)
    provider_page_token: str | None = Field(default=None, max_length=4096, repr=False)
    max_results: int = Field(ge=1, le=500)

    @model_validator(mode="after")
    def validate_operation(self) -> "GmailSnapshotPageReadCommand":
        if self.operation is not EmailOperation.ENUMERATE_MAILBOX:
            raise ValueError("Gmail snapshot pages require enumeration operation")
        return self


class GmailInitialSnapshotProviderReader:
    """Translate admitted baseline/page commands into allowlisted Gmail reads."""

    def __init__(self, client: GmailReadClient[object]) -> None:
        self._client = client

    async def read(
        self,
        connection: ProviderConnection,
        command: MailboxReadCommand,
    ) -> GmailHistoryBaseline | GmailSnapshotPage:
        mailbox_reader = GmailMailboxReader(self._client)
        request = mailbox_reader.build_request(connection, command)
        if isinstance(command, GmailHistoryBaselineReadCommand):
            try:
                response = await self._client.read(
                    request.model_copy(update={"resource": "profile"})
                )
            except Exception:
                return self._unsafe_baseline(command.scope, "baseline_unavailable")
            if (
                not isinstance(response, GmailProfileResponse)
                or response.history_id is None
            ):
                return self._unsafe_baseline(command.scope, "baseline_malformed")
            return GmailHistoryBaseline(
                state=GmailHistoryBaselineState.CAPTURED,
                admission_scope_digest=gmail_history_scope_digest(command.scope),
                checkpoint=encode_gmail_history_checkpoint(
                    command.scope, response.history_id
                ),
            )
        if not isinstance(command, GmailSnapshotPageReadCommand):
            raise ValueError("Gmail initial snapshot command is invalid")
        try:
            response = await self._client.read(
                request.model_copy(
                    update={
                        "resource": "messages",
                        "page_token": command.provider_page_token,
                        "max_results": command.max_results,
                    }
                )
            )
        except Exception:
            return GmailSnapshotPage(
                requested_checkpoint=command.checkpoint,
                complete=False,
                error_code="provider_unavailable",
            )
        if (
            not isinstance(response, GmailMessageIdPage)
            or len(response.message_ids) > command.max_results
            or (not response.message_ids and response.next_page_token is not None)
        ):
            return GmailSnapshotPage(
                requested_checkpoint=command.checkpoint,
                complete=False,
                error_code="malformed_page",
            )
        return GmailSnapshotPage(
            requested_checkpoint=command.checkpoint,
            message_ids=response.message_ids,
            next_checkpoint=(
                None
                if response.next_page_token is None
                else encode_gmail_snapshot_page_checkpoint(
                    command.scope, response.next_page_token
                )
            ),
        )

    @staticmethod
    def _unsafe_baseline(
        scope: MailboxReadScope, error_code: str
    ) -> GmailHistoryBaseline:
        return GmailHistoryBaseline(
            state=GmailHistoryBaselineState.UNSAFE,
            admission_scope_digest=gmail_history_scope_digest(scope),
            error_code=error_code,
        )


@dataclass(frozen=True, slots=True)
class GmailInitialSnapshotAdapter:
    """Invoke baseline and single-page reads through exact mailbox admission."""

    admitted: MailboxReadAdmission[GmailHistoryBaseline | GmailSnapshotPage]
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]

    async def capture_history_baseline(
        self, scope: MailboxReadScope
    ) -> GmailHistoryBaseline:
        label_reference = self._label(scope)
        command = GmailHistoryBaselineReadCommand(
            scope=scope,
            operation=EmailOperation.SYNCHRONIZE,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=label_reference,
        )
        result = await self.admitted.read(
            command,
            self._context(scope, "gmail_snapshot_baseline", "baseline"),
        )
        if not isinstance(result, GmailHistoryBaseline):
            raise ValueError("Gmail baseline result is invalid")
        return result

    async def enumerate_snapshot_page(
        self, request: GmailSnapshotPageRequest
    ) -> GmailSnapshotPage:
        scope = request.scope
        provider_page_token = (
            None
            if request.checkpoint is None
            else decode_gmail_snapshot_page_checkpoint(scope, request.checkpoint)
        )
        command = GmailSnapshotPageReadCommand(
            scope=scope,
            operation=EmailOperation.ENUMERATE_MAILBOX,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            checkpoint=request.checkpoint,
            provider_page_token=provider_page_token,
            max_results=request.max_results,
        )
        result = await self.admitted.read(
            command,
            self._context(
                scope,
                "gmail_snapshot_page",
                request.checkpoint or "initial",
            ),
        )
        if not isinstance(result, GmailSnapshotPage):
            raise ValueError("Gmail snapshot page result is invalid")
        return result

    def _context(
        self, scope: MailboxReadScope, operation: str, identity: str
    ) -> TrustedPersistenceContext:
        trace_id = uuid5(
            NAMESPACE_URL,
            "spine:gmail-initial-snapshot:v1:"
            f"{scope.workspace_id}:{scope.environment_id}:{scope.connection_id}:"
            f"{scope.authorization_generation}:{scope.configuration_generation}:"
            f"{scope.scope_generation}:{identity}",
        )
        return self.context_factory(PersistenceOperation(operation), trace_id)

    @staticmethod
    def _label(scope: MailboxReadScope) -> UUID:
        labels = tuple(
            label
            for label in scope.allowed_label_references
            if label not in scope.excluded_label_references
        )
        if not labels:
            raise ValueError("Gmail snapshot requires an exact approved label")
        return labels[0]


@dataclass(frozen=True, slots=True)
class GmailInitialHistoryDrainAdapter:
    """Read admitted history, remove snapshot overlap, and stage durable effects."""

    reader: MailboxReadAdmission[GmailHistoryResult]
    applier: GmailIncrementalHistoryApplier
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]

    async def stage(
        self,
        attempt_id: UUID,
        scope: MailboxReadScope,
        *,
        baseline_checkpoint: str,
        max_results: int,
        max_pages: int,
        covered_snapshot_effect_keys: tuple[str, ...],
    ) -> EmailSyncAttempt:
        if scope != self.applier.scope:
            raise ValueError("Gmail history scope is outside the snapshot")
        start_history_id = decode_gmail_history_checkpoint(scope, baseline_checkpoint)
        label_reference = GmailInitialSnapshotAdapter._label(scope)
        command = GmailHistoryReadCommand(
            scope=scope,
            operation=EmailOperation.SYNCHRONIZE,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=label_reference,
            start_history_id=start_history_id,
            max_results=max_results,
            max_pages=max_pages,
        )
        result = await self.reader.read(
            command,
            self.context_factory(
                PersistenceOperation("gmail_snapshot_history_drain"),
                uuid5(
                    NAMESPACE_URL,
                    f"spine:gmail-initial-history:v1:{attempt_id}:{baseline_checkpoint}",
                ),
            ),
        )
        covered = set(covered_snapshot_effect_keys)
        filtered = result.model_copy(
            update={
                "changes": tuple(
                    change
                    for change in result.changes
                    if not (
                        change.kind is GmailHistoryChangeKind.MESSAGE_ADDED
                        and gmail_snapshot_effect_key(scope, change.message_id)
                        in covered
                    )
                )
            }
        )
        return await self.applier.stage(
            attempt_id,
            filtered,
            start_history_id=start_history_id,
        )


@dataclass(frozen=True, slots=True)
class GmailCanonicalSnapshotMessageAdapter:
    """Apply one snapshot message through RAW observation and canonical acceptance."""

    ingestion: ConnectorIngestionOrchestrator
    raw_messages: MailboxReadAdmission[GmailRawMessageResult]
    authorized_label_reference: UUID
    clock: Callable[[], datetime]

    async def apply(
        self, command: GmailSnapshotMessageCommand
    ) -> GmailSnapshotMessageReceipt:
        scope = command.scope
        if not scope.allows_location(self.authorized_label_reference):
            raise ValueError("Gmail snapshot label is outside the approved scope")
        expected_key = gmail_snapshot_effect_key(scope, command.message_id)
        if command.effect_key != expected_key:
            raise ValueError("Gmail snapshot effect identity is invalid")
        digest = hashlib.sha256(command.effect_key.encode("ascii")).hexdigest()
        ingestion_command = ConnectorIngestionCommand(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            command_identity=f"gmail.snapshot.{digest[:32]}",
            idempotency_key=command.effect_key,
            connection_id=scope.connection_id,
            resource_scope_reference=scope.resource_scope_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            resource_object_reference=command.message_id,
            access_policy_version_id=command.access_policy_version_id,
        )
        orchestrator = replace(
            self.ingestion,
            observation_reader=GmailCanonicalObservationReader(
                admission=self.raw_messages,
                scope=scope,
                authorized_label_reference=self.authorized_label_reference,
                clock=self.clock,
            ),
            read_operation=PersistenceOperation("mailbox.read_raw_message"),
        )
        result = await orchestrator.ingest(ingestion_command)
        acceptance = (
            None
            if result.canonical_acceptance is None
            else result.canonical_acceptance.acceptance
        )
        if acceptance is None:
            context = orchestrator.context_factory(
                PersistenceOperation("canonical_acceptance"),
                uuid5(
                    NAMESPACE_URL,
                    "spine:gmail-snapshot:current-acceptance:"
                    f"{scope.workspace_id}:{scope.environment_id}:"
                    f"{result.source.source_object_id}:{result.revision.revision_id}",
                ),
            )
            async with orchestrator.uow_factory(context) as uow:
                head = await uow.canonical.resolve_current(
                    result.source.source_object_id
                )
                acceptance = (
                    None
                    if head is None
                    else await uow.canonical.resolve_acceptance(head.acceptance_id)
                )
                await uow.rollback()
        if (
            acceptance is None
            or result.source.workspace_id != scope.workspace_id
            or result.source.environment_id != scope.environment_id
            or acceptance.workspace_id != scope.workspace_id
            or acceptance.environment_id != scope.environment_id
            or acceptance.source_object_id != result.source.source_object_id
            or acceptance.source_revision_id != result.revision.revision_id
        ):
            raise ValueError("canonical Gmail message receipt is outside its scope")
        acceptance_id = acceptance.canonical_acceptance_id
        return GmailSnapshotMessageReceipt(
            effect_key=command.effect_key,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            canonical_acceptance_id=acceptance_id,
            receipt_reference=str(acceptance_id),
        )


def compose_gmail_initial_snapshot_reader(
    *,
    scopes: MailboxScopeRepository,
    connections: ProviderConnectionRepository,
    client: GmailReadClient[object],
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
) -> GmailInitialSnapshotAdapter:
    return GmailInitialSnapshotAdapter(
        admitted=MailboxReadAdmission(
            scopes=scopes,
            connections=connections,
            provider=GmailInitialSnapshotProviderReader(client),
        ),
        context_factory=context_factory,
    )


__all__ = [
    "GmailCanonicalSnapshotMessageAdapter",
    "GmailHistoryBaselineReadCommand",
    "GmailInitialHistoryDrainAdapter",
    "GmailInitialSnapshotAdapter",
    "GmailInitialSnapshotProviderReader",
    "GmailProfileResponse",
    "GmailSnapshotPageReadCommand",
    "compose_gmail_initial_snapshot_reader",
]
