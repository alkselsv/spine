"""Provider-neutral coordination of bounded email synchronization pages."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, ValidationInfo, model_validator

from spine.application.diagnostics import DiagnosticContext
from spine.application.outbox import OutboxConsumerSuccess
from spine.application.persistence.outbox import OutboxIntent
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope
from spine.domain.sources.email_lifecycle import (
    EmailLifecycleCoverage,
    EmailLifecycleReceipt,
)

EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE = "email.sync.state_observed"
EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION = 1
EMAIL_SYNC_STATE_OBSERVED_CONSUMER_NAME = "email_sync_state_observer"
EMAIL_SYNC_PAGE_DIGEST_V1 = "email-sync-page-digest:v1"
EMAIL_SYNC_PAGE_DIGEST_V2 = "email-sync-page-digest:v2"


class EmailSyncStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INCOMPLETE = "incomplete"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SUSPENDED = "suspended"
    FAILED = "failed"
    COMPLETE = "complete"


class EmailSyncAdmissionState(str, Enum):
    ALLOWED = "allowed"
    REVOKED = "revoked"
    STALE_GENERATION = "stale_generation"
    DENIED = "denied"


class EmailSyncPageKind(str, Enum):
    """Stable semantic kind for durable provider coverage stages."""

    PROVIDER_PAGE = "provider_page"
    DISCOVERY = "discovery"
    FOLDER_GENERATION = "folder_generation"
    UID_PAGE = "uid_page"
    FOLDER_COMPLETE = "folder_complete"
    LIFECYCLE_RECONCILIATION = "lifecycle_reconciliation"
    HISTORY_BASELINE = "history_baseline"
    SNAPSHOT_PAGE = "snapshot_page"
    HISTORY_DRAIN = "history_drain"
    AGGREGATE_CHECKPOINT = "aggregate_checkpoint"


class EmailSyncEffectKind(str, Enum):
    """Whether a covered effect can create content or only records source state."""

    CONTENT = "content"
    STATE = "state"


class EmailSyncStateObservation(DefinitionModel):
    """Content-free provider state covered by one incremental checkpoint."""

    schema_version: Literal[
        "email-sync-state-observation:v1", "email-sync-state-observation:v2"
    ] = (
        "email-sync-state-observation:v2"
    )
    observation_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    mailbox_binding_id: UUID
    state_kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    provider_event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_reference_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_generation: str = Field(min_length=1, max_length=255)
    outbox_event_id: UUID


class EmailSyncStateObservedOutboxPayload(DefinitionModel):
    observation_id: UUID
    connection_id: UUID
    mailbox_binding_id: UUID
    state_kind: str
    provider_event_digest: str


class EmailSyncStateObservedConsumer:
    """Acknowledge publication after validating the durable observation reference."""

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        if (
            intent.event_type != EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE
            or intent.schema_version != EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION
            or not isinstance(intent.payload, EmailSyncStateObservedOutboxPayload)
            or intent.environment_id is None
        ):
            raise ValueError("email sync state event is invalid")
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class EmailSyncAdmission(DefinitionModel):
    """Exact tenant, connection, mailbox and generation binding for an attempt."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: UUID
    mailbox_subject_reference: UUID
    mailbox_binding_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    scope_generation: int = Field(ge=1)

    @classmethod
    def from_scope(cls, scope: MailboxReadScope) -> EmailSyncAdmission:
        return cls(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            resource_scope_reference=scope.resource_scope_reference,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            mailbox_binding_id=scope.mailbox_binding_id,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
        )


class EmailSyncEffect(DefinitionModel):
    """Opaque reference to one idempotent canonical effect covered by a page."""

    workspace_id: UUID
    environment_id: UUID
    effect_key: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    effect_reference: UUID
    kind: EmailSyncEffectKind = EmailSyncEffectKind.CONTENT
    state_observation: EmailSyncStateObservation | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> EmailSyncEffect:
        if self.effect_reference.int == 0:
            raise ValueError("effect reference must be non-zero")
        if self.kind is EmailSyncEffectKind.STATE:
            observation = self.state_observation
            if observation is None or (
                observation.observation_id != self.effect_reference
                or observation.workspace_id != self.workspace_id
                or observation.environment_id != self.environment_id
            ):
                raise ValueError("state effect must carry its exact observation")
        elif self.state_observation is not None:
            raise ValueError("content effect cannot carry a state observation")
        return self


class EmailSyncEffectReceipt(DefinitionModel):
    """Receipt returned only after the covered canonical effect is durable."""

    effect_key: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    receipt_reference: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    durable: bool


class EmailSyncPage(DefinitionModel):
    """One provider page with opaque cursors and stable effect identities."""

    workspace_id: UUID
    environment_id: UUID
    page_kind: EmailSyncPageKind = EmailSyncPageKind.PROVIDER_PAGE
    page_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    requested_checkpoint: str | None = Field(default=None, max_length=4096)
    next_checkpoint: str | None = Field(default=None, max_length=4096)
    terminal: bool = False
    publish_checkpoint: bool = True
    # Provider coverage may be terminal without completing the whole sync attempt.
    coverage_terminal: bool = False
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_cursor: str | None = Field(default=None, max_length=4096, repr=False)
    effects: tuple[EmailSyncEffect, ...]
    lifecycle_receipts: tuple[EmailLifecycleReceipt, ...] = ()

    @model_validator(mode="after")
    def validate_effects(self, info: ValidationInfo) -> EmailSyncPage:
        if (
            self.page_id.startswith("erased.")
            and not (info.context or {}).get("allow_erased_page_identity")
        ):
            raise ValueError(
                "page identities prefixed with 'erased.' are reserved for erased page lineage"
            )
        keys = tuple(effect.effect_key for effect in self.effects)
        if len(keys) != len(set(keys)):
            raise ValueError("page effect keys must be unique")
        _validate_lifecycle_receipts(
            self.page_kind,
            self.workspace_id,
            self.environment_id,
            self.lifecycle_receipts,
        )
        if not self.publish_checkpoint and (
            self.next_checkpoint is not None or self.terminal
        ):
            raise ValueError("a staged page cannot publish a checkpoint or terminate")
        if (
            self.publish_checkpoint
            and self.requested_checkpoint == self.next_checkpoint
            and not self.terminal
        ):
            raise ValueError("a nonterminal page must advance its opaque checkpoint")
        return self


class EmailSyncPageState(DefinitionModel):
    workspace_id: UUID
    environment_id: UUID
    page_kind: EmailSyncPageKind = EmailSyncPageKind.PROVIDER_PAGE
    page_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    page_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_schema_version: Literal["email-sync-page:v1"] = "email-sync-page:v1"
    page_digest_contract: Literal[
        "email-sync-page-digest:v1", "email-sync-page-digest:v2"
    ] = EMAIL_SYNC_PAGE_DIGEST_V1
    requested_checkpoint: str | None = Field(default=None, max_length=4096)
    # Durable retry input; it is not the attempt's published checkpoint until
    # every covered effect has a durable receipt.
    covered_checkpoint: str | None = Field(default=None, max_length=4096)
    next_checkpoint: str | None = Field(default=None, max_length=4096)
    terminal: bool
    publish_checkpoint: bool = True
    coverage_terminal: bool = False
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_cursor: str | None = Field(default=None, max_length=4096, repr=False)
    effects: tuple[EmailSyncEffect, ...]
    effect_receipts: tuple[EmailSyncEffectReceipt, ...] = ()
    lifecycle_receipts: tuple[EmailLifecycleReceipt, ...] = ()
    committed: bool = False

    @model_validator(mode="after")
    def validate_lifecycle_receipts(self) -> EmailSyncPageState:
        _validate_lifecycle_receipts(
            self.page_kind,
            self.workspace_id,
            self.environment_id,
            self.lifecycle_receipts,
        )
        return self


class EmailSyncAttempt(DefinitionModel):
    attempt_id: UUID
    attempt_key: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    admission: EmailSyncAdmission
    status: EmailSyncStatus
    # Optimistic concurrency token maintained by the durable repository.
    revision: int = Field(default=1, ge=1)
    committed_checkpoint: str | None = Field(default=None, max_length=4096)
    pages: tuple[EmailSyncPageState, ...] = ()
    failure_code: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z0-9_]+$"
    )
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> EmailSyncAttempt:
        for value in (self.created_at, self.updated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("sync attempt timestamps must be timezone-aware")
        return self


class EmailSyncRepository(Protocol):
    async def get(
        self, attempt_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> EmailSyncAttempt | None: ...

    async def save(self, attempt: EmailSyncAttempt) -> EmailSyncAttempt: ...

    async def fence_for_reconciliation(
        self, attempt: EmailSyncAttempt, *, failure_code: str
    ) -> EmailSyncAttempt: ...

    async def resolve_lifecycle_receipt(
        self, receipt_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> EmailLifecycleReceipt | None: ...


class EmailSyncConcurrentUpdateError(RuntimeError):
    """A stale worker attempted to mutate a newer durable attempt revision."""

    def __init__(self) -> None:
        super().__init__("email sync attempt changed concurrently")


class EmailSyncAdmissionPort(Protocol):
    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState: ...


class EmailSyncEffectPort(Protocol):
    """Idempotent effect boundary keyed by ``EmailSyncEffect.effect_key``."""

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt: ...


class EmailSyncStateObservationRepository(Protocol):
    async def record(self, observation: EmailSyncStateObservation) -> UUID: ...


class RoutedEmailSyncEffectPort:
    """Route content acceptance and content-free state to distinct stores."""

    def __init__(
        self,
        *,
        content: EmailSyncEffectPort,
        state: EmailSyncStateObservationRepository,
    ) -> None:
        self._content = content
        self._state = state

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        if effect.kind is EmailSyncEffectKind.CONTENT:
            return await self._content.apply(effect)
        assert effect.state_observation is not None
        reference = await self._state.record(effect.state_observation)
        return EmailSyncEffectReceipt(
            effect_key=effect.effect_key,
            receipt_reference=str(reference),
            durable=True,
        )


def register_email_sync_outbox_event(registry: object) -> None:
    """Register the content-free state event owned by email synchronization."""

    register = getattr(registry, "register", None)
    if not callable(register):
        raise TypeError("outbox registry must provide register")
    register(
        event_type=EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE,
        schema_version=EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION,
        payload_type=EmailSyncStateObservedOutboxPayload,
    )


def register_email_sync_delivery_route(registry: object) -> None:
    """Route state publication to its explicit validating consumer."""

    register = getattr(registry, "register", None)
    if not callable(register):
        raise TypeError("delivery route registry must provide register")
    register(
        event_type=EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE,
        schema_version=EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION,
        consumer_name=EMAIL_SYNC_STATE_OBSERVED_CONSUMER_NAME,
    )


class InMemoryEmailSyncRepository:
    """Small in-memory attempt store used by offline coordinator compositions."""

    def __init__(self) -> None:
        self._attempts: dict[UUID, EmailSyncAttempt] = {}

    async def get(
        self, attempt_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> EmailSyncAttempt | None:
        attempt = self._attempts.get(attempt_id)
        if attempt is None or (
            attempt.admission.workspace_id != workspace_id
            or attempt.admission.environment_id != environment_id
        ):
            return None
        return attempt

    async def save(self, attempt: EmailSyncAttempt) -> EmailSyncAttempt:
        self._attempts[attempt.attempt_id] = attempt
        return attempt

    async def fence_for_reconciliation(
        self, attempt: EmailSyncAttempt, *, failure_code: str
    ) -> EmailSyncAttempt:
        current = self._attempts.get(attempt.attempt_id)
        if current is None or current.admission != attempt.admission:
            raise KeyError("email sync attempt not found")
        if current.status in {EmailSyncStatus.SUSPENDED, EmailSyncStatus.FAILED}:
            return current
        fenced = current.model_copy(
            update={
                "status": EmailSyncStatus.RECONCILIATION_REQUIRED,
                "committed_checkpoint": attempt.committed_checkpoint,
                "failure_code": failure_code,
                "updated_at": attempt.updated_at,
            }
        )
        self._attempts[attempt.attempt_id] = fenced
        return fenced

    async def resolve_lifecycle_receipt(
        self, receipt_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> EmailLifecycleReceipt | None:
        for attempt in self._attempts.values():
            if (
                attempt.admission.workspace_id != workspace_id
                or attempt.admission.environment_id != environment_id
            ):
                continue
            for page in attempt.pages:
                if not page.committed:
                    continue
                for receipt in page.lifecycle_receipts:
                    if receipt.receipt_id == receipt_id:
                        return receipt
        return None


class EmailSyncCoordinator:
    """Coordinate effects and checkpoint publication without provider semantics."""

    def __init__(
        self,
        *,
        repository: EmailSyncRepository,
        admission: EmailSyncAdmissionPort,
        effects: EmailSyncEffectPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._admission = admission
        self._effects = effects
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def create_attempt(
        self,
        scope: MailboxReadScope,
        *,
        attempt_key: str,
        initial_checkpoint: str | None = None,
    ) -> EmailSyncAttempt:
        """Return the stable attempt for an exact authorized mailbox binding."""

        if (
            not attempt_key
            or len(attempt_key) > 255
            or not all(char.isalnum() or char in "_.:-" for char in attempt_key)
        ):
            raise ValueError("attempt key must be a bounded non-empty value")
        if not scope.allows(EmailOperation.SYNCHRONIZE):
            raise ValueError("mailbox scope does not authorize synchronization")
        if initial_checkpoint is not None and (
            not initial_checkpoint or len(initial_checkpoint) > 4096
        ):
            raise ValueError("initial checkpoint must be a bounded non-empty value")
        admission = EmailSyncAdmission.from_scope(scope)
        attempt_id = uuid5(
            NAMESPACE_URL,
            f"email-sync:{admission.model_dump_json()}:{attempt_key}",
        )
        existing = await self._repository.get(
            attempt_id, admission.workspace_id, admission.environment_id
        )
        if existing is not None:
            existing_initial_checkpoint = (
                existing.pages[0].requested_checkpoint
                if existing.pages
                else existing.committed_checkpoint
            )
            if (
                existing.admission != admission
                or existing.attempt_key != attempt_key
                or existing_initial_checkpoint != initial_checkpoint
            ):
                raise ValueError("attempt identity conflicts with its binding")
            return existing
        now = self._now()
        return await self._repository.save(
            EmailSyncAttempt(
                attempt_id=attempt_id,
                attempt_key=attempt_key,
                admission=admission,
                status=EmailSyncStatus.PENDING,
                committed_checkpoint=initial_checkpoint,
                created_at=now,
                updated_at=now,
            )
        )

    async def apply_page(
        self,
        attempt_id: UUID,
        page: EmailSyncPage,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAttempt:
        """Apply a page idempotently and publish its opaque cursor after receipts."""

        return await self._apply_page(
            attempt_id,
            page,
            workspace_id=workspace_id,
            environment_id=environment_id,
            supplied_receipts=(),
        )

    async def record_durable_page(
        self,
        attempt_id: UUID,
        page: EmailSyncPage,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        receipts: tuple[EmailSyncEffectReceipt, ...],
    ) -> EmailSyncAttempt:
        """Record already-durable canonical receipts before cursor publication.

        Provider-specific coordinators use this after their canonical ingestion
        port has returned durable acceptance receipts. PostgreSQL still verifies
        those acceptance references when a later aggregate checkpoint advances.
        """

        receipt_keys = tuple(receipt.effect_key for receipt in receipts)
        effect_keys = tuple(effect.effect_key for effect in page.effects)
        if (
            len(receipt_keys) != len(set(receipt_keys))
            or set(receipt_keys) != set(effect_keys)
            or any(not receipt.durable for receipt in receipts)
        ):
            raise ValueError(
                "durable page receipts must cover every effect exactly once"
            )
        return await self._apply_page(
            attempt_id,
            page,
            workspace_id=workspace_id,
            environment_id=environment_id,
            supplied_receipts=receipts,
        )

    async def _apply_page(
        self,
        attempt_id: UUID,
        page: EmailSyncPage,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        supplied_receipts: tuple[EmailSyncEffectReceipt, ...],
    ) -> EmailSyncAttempt:
        supplied_by_key = {receipt.effect_key: receipt for receipt in supplied_receipts}

        attempt = await self._require(attempt_id, workspace_id, environment_id)
        known = self._page(attempt, page.page_id)
        if known is not None and not self._matches(known, page):
            return await self._save(
                attempt.model_copy(
                    update={
                        "status": EmailSyncStatus.FAILED,
                        "failure_code": "page_identity_conflict",
                    }
                )
            )
        if attempt.status in {
            EmailSyncStatus.SUSPENDED,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            EmailSyncStatus.FAILED,
            EmailSyncStatus.COMPLETE,
        }:
            return attempt

        records_unusable_coverage = self._records_unusable_coverage(page)
        admission_state = await self._check_admission(attempt)
        if not self._lifecycle_coverage_matches_admission(page, admission_state):
            if admission_state is not EmailSyncAdmissionState.ALLOWED:
                return await self._stop_for_admission(attempt, admission_state)
            return await self._save(
                attempt.model_copy(
                    update={
                        "status": EmailSyncStatus.RECONCILIATION_REQUIRED,
                        "failure_code": "lifecycle_admission_conflict",
                    }
                )
            )
        if (
            admission_state is not EmailSyncAdmissionState.ALLOWED
            and not records_unusable_coverage
        ):
            return await self._stop_for_admission(attempt, admission_state)

        if known is None and page.requested_checkpoint != attempt.committed_checkpoint:
            return await self._save(
                attempt.model_copy(
                    update={"status": EmailSyncStatus.RECONCILIATION_REQUIRED}
                )
            )

        if known is None and any(not current.committed for current in attempt.pages):
            # A later page must never cover the cursor range of a failed page.
            return await self._save(
                attempt.model_copy(update={"status": EmailSyncStatus.INCOMPLETE})
            )

        if known is None:
            if (
                page.workspace_id != attempt.admission.workspace_id
                or page.environment_id != attempt.admission.environment_id
            ):
                return await self._save(
                    attempt.model_copy(
                        update={
                            "status": EmailSyncStatus.FAILED,
                            "failure_code": "page_scope_conflict",
                        }
                    )
                )
            if any(
                effect.workspace_id != attempt.admission.workspace_id
                or effect.environment_id != attempt.admission.environment_id
                for effect in page.effects
            ):
                return await self._save(
                    attempt.model_copy(
                        update={
                            "status": EmailSyncStatus.FAILED,
                            "failure_code": "effect_scope_conflict",
                        }
                    )
                )
            known = EmailSyncPageState(
                workspace_id=page.workspace_id,
                environment_id=page.environment_id,
                page_kind=page.page_kind,
                page_id=page.page_id,
                page_digest=self._page_digest(page),
                requested_checkpoint=page.requested_checkpoint,
                covered_checkpoint=page.next_checkpoint,
                terminal=page.terminal,
                publish_checkpoint=page.publish_checkpoint,
                coverage_terminal=page.coverage_terminal,
                evidence_digest=page.evidence_digest,
                evidence_cursor=page.evidence_cursor,
                effects=page.effects,
                lifecycle_receipts=page.lifecycle_receipts,
            )
            attempt = await self._save(
                attempt.model_copy(
                    update={
                        "status": EmailSyncStatus.RUNNING,
                        "failure_code": None,
                        "pages": (*attempt.pages, known),
                    }
                )
            )

        if attempt.status is EmailSyncStatus.INCOMPLETE:
            attempt = await self.resume(
                attempt_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
            )
            if attempt.status is not EmailSyncStatus.RUNNING:
                return attempt
            known = self._page(attempt, page.page_id)
            if known is None:
                raise EmailSyncConcurrentUpdateError()

        for effect in known.effects:
            current = await self.check_effect_admission(
                attempt_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
            )
            if current.status is not EmailSyncStatus.RUNNING:
                return current
            attempt = current
            known = self._page(attempt, page.page_id)
            if known is None:
                raise EmailSyncConcurrentUpdateError()
            receipt = self._receipt_for(known, effect.effect_key)
            if receipt is not None and receipt.durable:
                continue
            receipt = supplied_by_key.get(effect.effect_key)
            if receipt is None:
                try:
                    receipt = await self._effects.apply(effect)
                except Exception:
                    return await self._set_incomplete(attempt, known)
            if receipt.effect_key != effect.effect_key or not receipt.durable:
                return await self._set_incomplete(attempt, known)
            known = known.model_copy(
                update={"effect_receipts": self._with_receipt(known, receipt)}
            )
            attempt = await self._replace_page(attempt, known)

        # Recheck at the publication boundary. A generation change after effects
        # were accepted must hold the cursor for reconciliation.
        admission_state = await self._check_admission(attempt)
        if not self._lifecycle_coverage_matches_admission(page, admission_state):
            if admission_state is not EmailSyncAdmissionState.ALLOWED:
                return await self._stop_for_admission(attempt, admission_state)
            return await self._save(
                attempt.model_copy(
                    update={
                        "status": EmailSyncStatus.RECONCILIATION_REQUIRED,
                        "failure_code": "lifecycle_admission_conflict",
                    }
                )
            )
        if (
            admission_state is not EmailSyncAdmissionState.ALLOWED
            and not records_unusable_coverage
        ):
            return await self._stop_for_admission(attempt, admission_state)

        known = known.model_copy(
            update={"next_checkpoint": known.covered_checkpoint, "committed": True}
        )
        if not known.publish_checkpoint:
            return await self._replace_page(
                attempt.model_copy(
                    update={
                        "status": EmailSyncStatus.RUNNING,
                        "failure_code": None,
                    }
                ),
                known,
            )
        attempt = attempt.model_copy(
            update={
                "committed_checkpoint": known.next_checkpoint,
                "status": (
                    EmailSyncStatus.COMPLETE
                    if known.terminal
                    else EmailSyncStatus.RUNNING
                ),
                "failure_code": None,
            }
        )
        return await self._replace_page(attempt, known)

    async def resume_pending(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAttempt:
        """Finish the admitted range before accepting a wider provider drain."""

        attempt = await self._require(attempt_id, workspace_id, environment_id)
        pending = next((page for page in attempt.pages if not page.committed), None)
        if pending is None:
            return attempt
        return await self.apply_page(
            attempt_id,
            EmailSyncPage(
                workspace_id=pending.workspace_id,
                environment_id=pending.environment_id,
                page_kind=pending.page_kind,
                page_id=pending.page_id,
                requested_checkpoint=pending.requested_checkpoint,
                next_checkpoint=pending.covered_checkpoint,
                terminal=pending.terminal,
                publish_checkpoint=pending.publish_checkpoint,
                coverage_terminal=pending.coverage_terminal,
                evidence_digest=pending.evidence_digest,
                evidence_cursor=pending.evidence_cursor,
                effects=pending.effects,
                lifecycle_receipts=pending.lifecycle_receipts,
            ),
            workspace_id=workspace_id,
            environment_id=environment_id,
        )

    async def mark_incomplete(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        failure_code: str,
    ) -> EmailSyncAttempt:
        """Hold the aggregate checkpoint after incomplete provider coverage."""

        self._validate_failure_code(failure_code)
        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status in {
            EmailSyncStatus.COMPLETE,
            EmailSyncStatus.SUSPENDED,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            EmailSyncStatus.FAILED,
        }:
            return attempt
        return await self._save(
            attempt.model_copy(
                update={
                    "status": EmailSyncStatus.INCOMPLETE,
                    "failure_code": failure_code,
                }
            )
        )

    async def require_reconciliation(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        failure_code: str,
    ) -> EmailSyncAttempt:
        """Fence an attempt whose provider or authorization generation changed."""

        self._validate_failure_code(failure_code)
        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status in {
            EmailSyncStatus.COMPLETE,
            EmailSyncStatus.SUSPENDED,
            EmailSyncStatus.FAILED,
        }:
            return attempt
        return await self._repository.fence_for_reconciliation(
            attempt.model_copy(update={"updated_at": self._clock()}),
            failure_code=failure_code,
        )

    async def fence_for_reconciliation(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        expected_admission: EmailSyncAdmission,
        restore_committed_checkpoint: str | None,
        failure_code: str,
    ) -> EmailSyncAttempt:
        """Authorize and durably fence an obsolete provider cursor generation."""

        self._validate_failure_code(failure_code)
        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.admission != expected_admission:
            raise KeyError("email sync attempt not found")
        if attempt.status in {EmailSyncStatus.SUSPENDED, EmailSyncStatus.FAILED}:
            return attempt
        admission_state = await self._check_admission(attempt)
        if admission_state is not EmailSyncAdmissionState.ALLOWED:
            return await self._stop_for_admission(attempt, admission_state)
        if attempt.status is EmailSyncStatus.RECONCILIATION_REQUIRED:
            return attempt
        return await self._repository.fence_for_reconciliation(
            attempt.model_copy(
                update={
                    "committed_checkpoint": restore_committed_checkpoint,
                    "updated_at": self._clock(),
                }
            ),
            failure_code=failure_code,
        )

    async def check_effect_admission(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAttempt:
        """Recheck an attempt immediately before a canonical source effect."""

        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status is not EmailSyncStatus.RUNNING:
            return attempt
        admission_state = await self._check_admission(attempt)
        if admission_state is not EmailSyncAdmissionState.ALLOWED:
            return await self._stop_for_admission(attempt, admission_state)
        return attempt

    async def check_read_admission(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAttempt:
        """Recheck the exact immutable binding immediately before a provider read."""

        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status not in {
            EmailSyncStatus.PENDING,
            EmailSyncStatus.RUNNING,
            EmailSyncStatus.INCOMPLETE,
        }:
            return attempt
        admission_state = await self._check_admission(attempt)
        if admission_state is not EmailSyncAdmissionState.ALLOWED:
            return await self._stop_for_admission(attempt, admission_state)
        return attempt

    async def cancel(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAttempt:
        """Suspend an attempt without publishing any uncommitted page cursor."""

        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status in {EmailSyncStatus.COMPLETE, EmailSyncStatus.FAILED}:
            return attempt
        return await self._save(
            attempt.model_copy(
                update={
                    "status": EmailSyncStatus.SUSPENDED,
                    "failure_code": "cancelled",
                }
            )
        )

    async def get_attempt(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAttempt:
        """Load one tenant-scoped durable attempt for a provider coordinator."""

        return await self._require(attempt_id, workspace_id, environment_id)

    async def resolve_lifecycle_receipt(
        self,
        receipt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailLifecycleReceipt | None:
        """Resolve one durable tenant-scoped predecessor receipt."""

        return await self._repository.resolve_lifecycle_receipt(
            receipt_id, workspace_id, environment_id
        )

    async def admission_state(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAdmissionState:
        """Read the current exact-binding admission state through the safe port."""

        attempt = await self._require(attempt_id, workspace_id, environment_id)
        return await self._check_admission(attempt)

    async def hold(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        status: EmailSyncStatus,
        failure_code: str,
    ) -> EmailSyncAttempt:
        """Hold the checkpoint at a typed retry or reconciliation boundary."""

        if status not in {
            EmailSyncStatus.INCOMPLETE,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            EmailSyncStatus.SUSPENDED,
        }:
            raise ValueError("email sync hold status is invalid")
        self._validate_failure_code(failure_code)
        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status in {EmailSyncStatus.COMPLETE, EmailSyncStatus.FAILED}:
            return attempt
        if attempt.status in {
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            EmailSyncStatus.SUSPENDED,
        }:
            return attempt
        return await self._save(
            attempt.model_copy(update={"status": status, "failure_code": failure_code})
        )

    async def resume(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> EmailSyncAttempt:
        """Clear a retryable hold after rechecking the exact admission binding."""

        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status in {
            EmailSyncStatus.COMPLETE,
            EmailSyncStatus.FAILED,
            EmailSyncStatus.RECONCILIATION_REQUIRED,
            EmailSyncStatus.SUSPENDED,
        }:
            return attempt
        admission_state = await self._check_admission(attempt)
        if admission_state is not EmailSyncAdmissionState.ALLOWED:
            return await self._stop_for_admission(attempt, admission_state)
        return await self._save(
            attempt.model_copy(
                update={"status": EmailSyncStatus.RUNNING, "failure_code": None}
            )
        )

    async def fail(
        self,
        attempt_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        failure_code: str,
    ) -> EmailSyncAttempt:
        """Record a safe, caller-supplied failure code for the attempt."""

        self._validate_failure_code(failure_code)
        attempt = await self._require(attempt_id, workspace_id, environment_id)
        if attempt.status in {EmailSyncStatus.COMPLETE, EmailSyncStatus.SUSPENDED}:
            return attempt
        return await self._save(
            attempt.model_copy(
                update={"status": EmailSyncStatus.FAILED, "failure_code": failure_code}
            )
        )

    async def _set_incomplete(
        self, attempt: EmailSyncAttempt, page: EmailSyncPageState
    ) -> EmailSyncAttempt:
        return await self._replace_page(
            attempt.model_copy(
                update={
                    "status": EmailSyncStatus.INCOMPLETE,
                    "failure_code": "effect_incomplete",
                }
            ),
            page,
        )

    async def _check_admission(
        self, attempt: EmailSyncAttempt
    ) -> EmailSyncAdmissionState:
        try:
            return await self._admission.check(attempt.admission)
        except Exception:
            # Never surface provider or credential details from admission errors.
            return EmailSyncAdmissionState.DENIED

    async def _stop_for_admission(
        self,
        attempt: EmailSyncAttempt,
        state: EmailSyncAdmissionState,
    ) -> EmailSyncAttempt:
        status = (
            EmailSyncStatus.RECONCILIATION_REQUIRED
            if state is EmailSyncAdmissionState.STALE_GENERATION
            else EmailSyncStatus.SUSPENDED
        )
        return await self._save(attempt.model_copy(update={"status": status}))

    async def _replace_page(
        self, attempt: EmailSyncAttempt, page: EmailSyncPageState
    ) -> EmailSyncAttempt:
        pages = tuple(
            page if item.page_id == page.page_id else item for item in attempt.pages
        )
        return await self._save(attempt.model_copy(update={"pages": pages}))

    async def _save(self, attempt: EmailSyncAttempt) -> EmailSyncAttempt:
        return await self._repository.save(
            attempt.model_copy(update={"updated_at": self._now()})
        )

    async def _require(
        self, attempt_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> EmailSyncAttempt:
        attempt = await self._repository.get(attempt_id, workspace_id, environment_id)
        if attempt is None:
            raise KeyError("email sync attempt was not found")
        return attempt

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    @staticmethod
    def _validate_failure_code(failure_code: str) -> None:
        if (
            not failure_code
            or len(failure_code) > 64
            or not all(
                char.islower() or char.isdigit() or char == "_" for char in failure_code
            )
        ):
            raise ValueError("failure code must be a bounded non-empty value")

    @staticmethod
    def _page(attempt: EmailSyncAttempt, page_id: str) -> EmailSyncPageState | None:
        return next((page for page in attempt.pages if page.page_id == page_id), None)

    @staticmethod
    def _matches(stored: EmailSyncPageState, supplied: EmailSyncPage) -> bool:
        return stored.page_digest == EmailSyncCoordinator._page_digest(
            supplied, contract=stored.page_digest_contract
        )

    @staticmethod
    def _page_digest(
        page: EmailSyncPage,
        *,
        contract: str = EMAIL_SYNC_PAGE_DIGEST_V1,
    ) -> str:
        payload = page.model_dump(mode="json")
        if not page.coverage_terminal:
            # Preserve identities written before coverage-terminal was added.
            payload.pop("coverage_terminal")
        if page.page_kind is EmailSyncPageKind.PROVIDER_PAGE:
            # Preserve the digest of pages created before the semantic kind was
            # introduced; provider-specific kinds remain part of new identity.
            payload.pop("page_kind")
        if contract == EMAIL_SYNC_PAGE_DIGEST_V1:
            encoded = json.dumps(
                payload, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        elif contract == EMAIL_SYNC_PAGE_DIGEST_V2:
            # Mirrors spine.email_sync_page_json_canonical(jsonb): UTF-8
            # lexical object-key order, compact JSON and literal Unicode.
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        else:
            raise ValueError("email sync page digest contract is unsupported")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _page_from_state(state: EmailSyncPageState) -> EmailSyncPage:
        return EmailSyncPage.model_validate(
            state.model_dump(
                mode="python",
                exclude={
                    "page_digest",
                    "page_digest_contract",
                    "page_schema_version",
                    "covered_checkpoint",
                    "effect_receipts",
                    "committed",
                },
            ),
            context={"allow_erased_page_identity": True},
        )

    @staticmethod
    def _records_unusable_coverage(page: EmailSyncPage) -> bool:
        return (
            page.page_kind is EmailSyncPageKind.LIFECYCLE_RECONCILIATION
            and bool(page.lifecycle_receipts)
            and not page.effects
            and not page.publish_checkpoint
            and all(
                receipt.coverage is not EmailLifecycleCoverage.COMPLETE_AUTHORIZED
                for receipt in page.lifecycle_receipts
            )
        )

    @staticmethod
    def _lifecycle_coverage_matches_admission(
        page: EmailSyncPage,
        state: EmailSyncAdmissionState,
    ) -> bool:
        if (
            page.page_kind is not EmailSyncPageKind.LIFECYCLE_RECONCILIATION
            or not page.lifecycle_receipts
        ):
            return True
        coverages = {receipt.coverage for receipt in page.lifecycle_receipts}
        if coverages == {EmailLifecycleCoverage.PERMISSION_LOSS}:
            return state in {
                EmailSyncAdmissionState.REVOKED,
                EmailSyncAdmissionState.DENIED,
            }
        return state is EmailSyncAdmissionState.ALLOWED

    @staticmethod
    def _receipt_for(
        page: EmailSyncPageState, effect_key: str
    ) -> EmailSyncEffectReceipt | None:
        return next(
            (
                receipt
                for receipt in page.effect_receipts
                if receipt.effect_key == effect_key
            ),
            None,
        )

    @staticmethod
    def _with_receipt(
        page: EmailSyncPageState, receipt: EmailSyncEffectReceipt
    ) -> tuple[EmailSyncEffectReceipt, ...]:
        receipts = tuple(
            current
            for current in page.effect_receipts
            if current.effect_key != receipt.effect_key
        ) + (receipt,)
        return tuple(sorted(receipts, key=lambda current: current.effect_key))


def _validate_lifecycle_receipts(
    page_kind: EmailSyncPageKind,
    workspace_id: UUID,
    environment_id: UUID,
    receipts: tuple[EmailLifecycleReceipt, ...],
) -> None:
    receipt_ids = tuple(receipt.receipt_id for receipt in receipts)
    if len(receipt_ids) != len(set(receipt_ids)):
        raise ValueError("page lifecycle receipt IDs must be unique")
    if receipts and page_kind is not EmailSyncPageKind.LIFECYCLE_RECONCILIATION:
        raise ValueError("lifecycle receipts require a reconciliation page")
    if any(
        receipt.workspace_id != workspace_id
        or receipt.environment_id != environment_id
        for receipt in receipts
    ):
        raise ValueError("lifecycle receipt scope must match its page")


__all__ = [
    "EmailSyncAdmission",
    "EmailSyncAdmissionPort",
    "EmailSyncAdmissionState",
    "EmailSyncAttempt",
    "EmailSyncCoordinator",
    "EmailSyncConcurrentUpdateError",
    "EmailSyncEffect",
    "EmailSyncEffectKind",
    "EmailSyncEffectPort",
    "EmailSyncEffectReceipt",
    "EmailSyncPage",
    "EmailSyncPageKind",
    "EmailSyncPageState",
    "EmailSyncRepository",
    "EmailSyncStatus",
    "EmailSyncStateObservation",
    "EmailSyncStateObservationRepository",
    "EmailSyncStateObservedOutboxPayload",
    "EmailSyncStateObservedConsumer",
    "RoutedEmailSyncEffectPort",
    "register_email_sync_outbox_event",
    "register_email_sync_delivery_route",
    "InMemoryEmailSyncRepository",
]
