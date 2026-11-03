"""Provider-neutral coordination of bounded email synchronization pages."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
import hashlib
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


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
    effect_key: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    effect_reference: UUID

    @model_validator(mode="after")
    def validate_reference(self) -> EmailSyncEffect:
        if self.effect_reference.int == 0:
            raise ValueError("effect reference must be non-zero")
        return self


class EmailSyncEffectReceipt(DefinitionModel):
    """Receipt returned only after the covered canonical effect is durable."""

    effect_key: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    receipt_reference: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    durable: bool


class EmailSyncPage(DefinitionModel):
    """One provider page with opaque cursors and stable effect identities."""

    workspace_id: UUID
    environment_id: UUID
    page_id: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    requested_checkpoint: str | None = Field(default=None, max_length=4096)
    next_checkpoint: str | None = Field(default=None, max_length=4096)
    terminal: bool = False
    effects: tuple[EmailSyncEffect, ...]

    @model_validator(mode="after")
    def validate_effects(self) -> EmailSyncPage:
        keys = tuple(effect.effect_key for effect in self.effects)
        if len(keys) != len(set(keys)):
            raise ValueError("page effect keys must be unique")
        if self.requested_checkpoint == self.next_checkpoint and not self.terminal:
            raise ValueError("a nonterminal page must advance its opaque checkpoint")
        return self


class EmailSyncPageState(DefinitionModel):
    workspace_id: UUID
    environment_id: UUID
    page_id: str = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    page_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_checkpoint: str | None = Field(default=None, max_length=4096)
    # The provider's next cursor is deliberately absent until all effects have
    # durable receipts and the coordinator is ready to publish the page.
    next_checkpoint: str | None = Field(default=None, max_length=4096)
    terminal: bool
    effects: tuple[EmailSyncEffect, ...]
    effect_receipts: tuple[EmailSyncEffectReceipt, ...] = ()
    committed: bool = False


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


class EmailSyncConcurrentUpdateError(RuntimeError):
    """A stale worker attempted to mutate a newer durable attempt revision."""

    def __init__(self) -> None:
        super().__init__("email sync attempt changed concurrently")


class EmailSyncAdmissionPort(Protocol):
    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState: ...


class EmailSyncEffectPort(Protocol):
    """Idempotent effect boundary keyed by ``EmailSyncEffect.effect_key``."""

    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt: ...


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
        self, scope: MailboxReadScope, *, attempt_key: str
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
        admission = EmailSyncAdmission.from_scope(scope)
        attempt_id = uuid5(
            NAMESPACE_URL,
            f"email-sync:{admission.model_dump_json()}:{attempt_key}",
        )
        existing = await self._repository.get(
            attempt_id, admission.workspace_id, admission.environment_id
        )
        if existing is not None:
            if existing.admission != admission or existing.attempt_key != attempt_key:
                raise ValueError("attempt identity conflicts with its binding")
            return existing
        now = self._now()
        return await self._repository.save(
            EmailSyncAttempt(
                attempt_id=attempt_id,
                attempt_key=attempt_key,
                admission=admission,
                status=EmailSyncStatus.PENDING,
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

        admission_state = await self._check_admission(attempt)
        if admission_state is not EmailSyncAdmissionState.ALLOWED:
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
                attempt.model_copy(
                    update={"status": EmailSyncStatus.INCOMPLETE}
                )
            )

        if known is None:
            if page.workspace_id != attempt.admission.workspace_id or page.environment_id != attempt.admission.environment_id:
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
                page_id=page.page_id,
                page_digest=self._page_digest(page),
                requested_checkpoint=page.requested_checkpoint,
                terminal=page.terminal,
                effects=page.effects,
            )
            attempt = await self._save(
                attempt.model_copy(
                    update={
                        "status": EmailSyncStatus.RUNNING,
                        "pages": (*attempt.pages, known),
                    }
                )
            )

        for effect in known.effects:
            receipt = self._receipt_for(known, effect.effect_key)
            if receipt is not None and receipt.durable:
                continue
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
        if admission_state is not EmailSyncAdmissionState.ALLOWED:
            return await self._stop_for_admission(attempt, admission_state)

        known = known.model_copy(
            update={"next_checkpoint": page.next_checkpoint, "committed": True}
        )
        attempt = attempt.model_copy(
            update={
                "committed_checkpoint": known.next_checkpoint,
                "status": (
                    EmailSyncStatus.COMPLETE
                    if known.terminal
                    else EmailSyncStatus.RUNNING
                ),
            }
        )
        return await self._replace_page(attempt, known)

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
                update={"status": EmailSyncStatus.SUSPENDED, "failure_code": "cancelled"}
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

        if (
            not failure_code
            or len(failure_code) > 64
            or not all(char.islower() or char.isdigit() or char == "_" for char in failure_code)
        ):
            raise ValueError("failure code must be a bounded non-empty value")
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
        pages = tuple(page if item.page_id == page.page_id else item for item in attempt.pages)
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
    def _page(attempt: EmailSyncAttempt, page_id: str) -> EmailSyncPageState | None:
        return next((page for page in attempt.pages if page.page_id == page_id), None)

    @staticmethod
    def _matches(stored: EmailSyncPageState, supplied: EmailSyncPage) -> bool:
        return stored.page_digest == EmailSyncCoordinator._page_digest(supplied)

    @staticmethod
    def _page_digest(page: EmailSyncPage) -> str:
        return hashlib.sha256(page.model_dump_json().encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt_for(
        page: EmailSyncPageState, effect_key: str
    ) -> EmailSyncEffectReceipt | None:
        return next(
            (receipt for receipt in page.effect_receipts if receipt.effect_key == effect_key),
            None,
        )

    @staticmethod
    def _with_receipt(
        page: EmailSyncPageState, receipt: EmailSyncEffectReceipt
    ) -> tuple[EmailSyncEffectReceipt, ...]:
        return tuple(
            current for current in page.effect_receipts if current.effect_key != receipt.effect_key
        ) + (receipt,)


__all__ = [
    "EmailSyncAdmission",
    "EmailSyncAdmissionPort",
    "EmailSyncAdmissionState",
    "EmailSyncAttempt",
    "EmailSyncCoordinator",
    "EmailSyncConcurrentUpdateError",
    "EmailSyncEffect",
    "EmailSyncEffectPort",
    "EmailSyncEffectReceipt",
    "EmailSyncPage",
    "EmailSyncPageState",
    "EmailSyncRepository",
    "EmailSyncStatus",
    "InMemoryEmailSyncRepository",
]
