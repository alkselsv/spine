"""One-shot application boundary for qualification authority lifecycle.

The service is intentionally not an API dependency and is not wired into
startup, workers, dispatchers, or readiness.  A trusted operator channel must
call it explicitly.  All external values crossing this boundary are opaque
references; authority is granted only after an independent authorizer accepts
the operation and the canonical store commits the binding, generation, and
safe audit transition together.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import field_validator

from spine.auth.records import (
    AuthorizationRecordProvenance,
    AuthorizationRecordStatus,
    QualificationAuthorityBinding,
)
from spine.domain.qualification import (
    QualificationAuditRecord,
    QualificationOpaqueReference,
    QualificationOperation,
    QualificationOperationReceipt,
    QualificationOperationState,
    QualificationProfile,
)
from spine.domain.common import ValidatedDefinitionModel


class QualificationProvisioningError(Exception):
    """Disclosure-safe base error for the bounded operator seam."""


class QualificationInvocationRejected(QualificationProvisioningError):
    """The request did not arrive through the explicit operator channel."""


class QualificationOperatorDenied(QualificationProvisioningError):
    """Independent operator or change approval validation failed."""


class QualificationProfileRejected(QualificationProvisioningError):
    """The server-approved profile or its policy references are invalid."""


class QualificationOperationConflict(QualificationProvisioningError):
    """An operation identity was reused with different authority inputs."""


class QualificationOperationUnavailable(QualificationProvisioningError):
    """The operation can be retried without exposing active partial authority."""

    def __init__(self, receipt: QualificationOperationReceipt) -> None:
        super().__init__("Qualification operation is incomplete and can be retried safely.")
        self.receipt = receipt


class QualificationAuditUnavailable(QualificationProvisioningError):
    """Required activation or revocation audit could not be committed."""

    def __init__(self, receipt: QualificationOperationReceipt) -> None:
        super().__init__("Qualification authority remains inactive; audit is unavailable.")
        self.receipt = receipt


class QualificationInvocationSource(str, Enum):
    """Only ``OPERATOR_COMMAND`` is allowed to invoke this service."""

    OPERATOR_COMMAND = "operator_command"
    API_REQUEST = "api_request"
    API_STARTUP = "api_startup"
    WORKER_STARTUP = "worker_startup"
    DISPATCHER = "dispatcher"
    MIGRATION = "migration"
    READINESS = "readiness"


class QualificationOperationRequest(ValidatedDefinitionModel):
    """Safe command submitted by an already selected operator channel."""

    operation_id: UUID
    operation: QualificationOperation
    profile: QualificationProfile
    operator_reference: QualificationOpaqueReference
    approval_reference: QualificationOpaqueReference

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("operation_id must be a non-zero UUID")
        return value

    @classmethod
    def _canonical_payload(cls, values: Mapping[str, object]) -> dict[str, object]:
        def convert(value: object) -> object:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, UUID):
                return str(value)
            if isinstance(value, QualificationProfile):
                return value.profile_digest
            if isinstance(value, QualificationOpaqueReference):
                return value.value
            if isinstance(value, Mapping):
                return {str(key): convert(item) for key, item in value.items()}
            if isinstance(value, (tuple, list, frozenset)):
                return [convert(item) for item in value]
            return value

        return {str(key): convert(value) for key, value in values.items()}

    @property
    def request_digest(self) -> str:
        payload = self._canonical_payload(
            {
                "operation_id": self.operation_id,
                "operation": self.operation,
                "profile": self.profile,
                "operator_reference": self.operator_reference,
                "approval_reference": self.approval_reference,
            }
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class QualificationOperatorAuthorizationPort(Protocol):
    """Independent authority source for operator and change approval."""

    async def validate(
        self,
        *,
        operation: QualificationOperation,
        profile: QualificationProfile,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
    ) -> bool: ...


class QualificationPolicyReferencePort(Protocol):
    """Validate approved AccessPolicy references without mutating grants."""

    async def validate(self, profile: QualificationProfile) -> bool: ...


class QualificationSecretPort(Protocol):
    """External secret lifecycle port; values never enter the request."""

    async def bind(self, *, operation_id: UUID, profile: QualificationProfile) -> None: ...

    async def revoke(self, *, operation_id: UUID, profile: QualificationProfile) -> None: ...

    async def cleanup(self, *, operation_id: UUID, profile: QualificationProfile) -> None: ...


class QualificationProvisioningStore(Protocol):
    async def get_operation(self, operation_id: UUID) -> QualificationOperationReceipt | None: ...

    async def begin(
        self, request: QualificationOperationRequest
    ) -> tuple[QualificationOperationReceipt, bool]: ...

    async def mark_validated(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt: ...

    async def record_inactive(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt: ...

    async def activate(
        self,
        request: QualificationOperationRequest,
        *,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
    ) -> QualificationOperationReceipt: ...

    async def revoke(
        self,
        request: QualificationOperationRequest,
        *,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
    ) -> QualificationOperationReceipt: ...

    async def mark_incomplete(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt: ...

    async def mark_cleanup_pending(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt: ...

    async def mark_cleaned(
        self,
        request: QualificationOperationRequest,
        *,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
    ) -> QualificationOperationReceipt: ...


class RejectAllQualificationOperators:
    """Fail-closed default until a run-specific operator channel is supplied."""

    async def validate(self, **_: object) -> bool:
        return False


class RejectAllQualificationPolicies:
    """Fail-closed default; policy grants remain owned by AccessPolicy."""

    async def validate(self, profile: QualificationProfile) -> bool:
        del profile
        return False


class QualificationProvisioningService:
    """Execute one explicitly operator-invoked qualification lifecycle action."""

    def __init__(
        self,
        *,
        store: QualificationProvisioningStore,
        operator_authorization: QualificationOperatorAuthorizationPort,
        policy_references: QualificationPolicyReferencePort,
        secrets: QualificationSecretPort,
    ) -> None:
        self._store = store
        self._operator_authorization = operator_authorization
        self._policy_references = policy_references
        self._secrets = secrets

    async def execute(
        self,
        request: QualificationOperationRequest,
        *,
        invocation_source: QualificationInvocationSource,
    ) -> QualificationOperationReceipt:
        if type(request) is not QualificationOperationRequest:
            raise QualificationProfileRejected("Qualification request contract is invalid.")
        if invocation_source is not QualificationInvocationSource.OPERATOR_COMMAND:
            raise QualificationInvocationRejected(
                "Qualification operations require an explicit operator command."
            )
        if request.operation not in request.profile.allowed_operations:
            raise QualificationProfileRejected("Operation is not allowed by the approved profile.")
        if not await self._safe_policy_validation(request.profile):
            raise QualificationProfileRejected("Approved policy references are unavailable.")
        if not await self._safe_operator_validation(request):
            raise QualificationOperatorDenied("Operator or change approval is not valid.")

        receipt, created = await self._store.begin(request)
        if not created:
            if receipt.request_digest != request.request_digest:
                raise QualificationOperationConflict(
                    "Operation identity conflicts with its original authority inputs."
                )
            if receipt.state in {
                QualificationOperationState.ACTIVE,
                QualificationOperationState.REVOKED,
                QualificationOperationState.CLEANED,
            }:
                return receipt.model_copy(update={"replay": True})

        await self._store.mark_validated(request)
        if request.operation is QualificationOperation.PROVISION:
            return await self._provision(request)
        if request.operation is QualificationOperation.REVOKE:
            return await self._revoke(request)
        return await self._cleanup(request)

    async def _safe_policy_validation(self, profile: QualificationProfile) -> bool:
        try:
            return await self._policy_references.validate(profile)
        except Exception:
            return False

    async def _safe_operator_validation(self, request: QualificationOperationRequest) -> bool:
        try:
            return await self._operator_authorization.validate(
                operation=request.operation,
                profile=request.profile,
                operator_reference=request.operator_reference,
                approval_reference=request.approval_reference,
            )
        except Exception:
            return False

    async def _provision(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt:
        await self._store.record_inactive(request)
        try:
            await self._secrets.bind(operation_id=request.operation_id, profile=request.profile)
        except Exception:
            receipt = await self._store.mark_incomplete(request)
            raise QualificationOperationUnavailable(receipt) from None
        try:
            return await self._store.activate(
                request,
                operator_reference=request.operator_reference,
                approval_reference=request.approval_reference,
            )
        except QualificationAuditUnavailable:
            receipt = await self._store.mark_incomplete(request)
            raise QualificationAuditUnavailable(receipt) from None

    async def _revoke(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt:
        try:
            receipt = await self._store.revoke(
                request,
                operator_reference=request.operator_reference,
                approval_reference=request.approval_reference,
            )
        except QualificationAuditUnavailable:
            receipt = await self._store.mark_incomplete(request)
            raise QualificationAuditUnavailable(receipt) from None
        try:
            await self._secrets.revoke(operation_id=request.operation_id, profile=request.profile)
        except Exception:
            return await self._store.mark_cleanup_pending(request)
        return receipt

    async def _cleanup(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt:
        try:
            await self._secrets.cleanup(operation_id=request.operation_id, profile=request.profile)
        except Exception:
            return await self._store.mark_cleanup_pending(request)
        try:
            return await self._store.mark_cleaned(
                request,
                operator_reference=request.operator_reference,
                approval_reference=request.approval_reference,
            )
        except QualificationAuditUnavailable:
            return await self._store.mark_cleanup_pending(request)


@dataclass(frozen=True, slots=True)
class _StoredState:
    operations: dict[UUID, QualificationOperationReceipt]
    histories: dict[UUID, list[QualificationAuthorityBinding]]
    generations: dict[tuple[UUID, UUID], int]
    audits: list[QualificationAuditRecord]
    target_references: dict[UUID, str]


class InMemoryQualificationProvisioningStore:
    """Deterministic adapter for the bounded operator contract and tests."""

    def __init__(
        self,
        *,
        initial_generations: Mapping[tuple[UUID, UUID], int] | None = None,
        audit_available: bool = True,
    ) -> None:
        self._state = _StoredState(
            operations={},
            histories={},
            generations=dict(initial_generations or {}),
            audits=[],
            target_references={},
        )
        self._audit_available = audit_available

    @property
    def operations(self) -> Mapping[UUID, QualificationOperationReceipt]:
        return self._state.operations

    @property
    def histories(self) -> Mapping[UUID, tuple[QualificationAuthorityBinding, ...]]:
        return {key: tuple(value) for key, value in self._state.histories.items()}

    @property
    def generations(self) -> Mapping[tuple[UUID, UUID], int]:
        return self._state.generations

    @property
    def audits(self) -> tuple[QualificationAuditRecord, ...]:
        return tuple(self._state.audits)

    @property
    def audit_available(self) -> bool:
        return self._audit_available

    @audit_available.setter
    def audit_available(self, value: bool) -> None:
        self._audit_available = value

    async def get_operation(self, operation_id: UUID) -> QualificationOperationReceipt | None:
        return self._state.operations.get(operation_id)

    async def begin(
        self, request: QualificationOperationRequest
    ) -> tuple[QualificationOperationReceipt, bool]:
        existing = self._state.operations.get(request.operation_id)
        if existing is not None:
            return existing, False
        receipt = _receipt_for(request, state=QualificationOperationState.REQUESTED)
        self._state.operations[request.operation_id] = receipt
        return receipt, True

    async def mark_validated(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt:
        return self._update(request, state=QualificationOperationState.VALIDATED)

    async def record_inactive(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt:
        binding = request.profile.binding
        requested_key = _binding_scope_key(binding)
        for binding_id, history in self._state.histories.items():
            if binding_id == binding.binding_id or not history:
                continue
            existing = history[-1]
            if _binding_scope_key(existing) == requested_key:
                raise QualificationOperationConflict(
                    "Qualification principal scope already has an approved binding."
                )
        history = self._state.histories.setdefault(binding.binding_id, [])
        if not history:
            history.append(binding)
            self._state.target_references[binding.binding_id] = (
                request.profile.target_reference.value
            )
        elif self._state.target_references.get(binding.binding_id) != (
            request.profile.target_reference.value
        ):
            raise QualificationOperationConflict(
                "Qualification binding cannot be reused for another target."
            )
        elif history[-1].status is AuthorizationRecordStatus.ACTIVE:
            return self._update(request, state=QualificationOperationState.ACTIVE)
        return self._update(request, state=QualificationOperationState.INACTIVE)

    async def activate(
        self,
        request: QualificationOperationRequest,
        *,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
    ) -> QualificationOperationReceipt:
        current = self._current_binding(request)
        if current.status is AuthorizationRecordStatus.ACTIVE:
            return self._update(request, state=QualificationOperationState.ACTIVE)
        if current.status is not AuthorizationRecordStatus.DISABLED:
            raise QualificationProvisioningError("Qualification binding cannot be activated.")
        generation_before = self._generation(request)
        generation_after = generation_before + 1
        audit = self._audit(
            request,
            transition=QualificationOperationState.ACTIVE,
            operator_reference=operator_reference,
            approval_reference=approval_reference,
            generation_before=generation_before,
            generation_after=generation_after,
            reason_code="qualification_activated",
        )
        successor = _successor(
            current,
            status=AuthorizationRecordStatus.ACTIVE,
            operation_id=request.operation_id,
            suffix="active",
        )
        self._state.histories[current.binding_id].append(successor)
        self._state.generations[_scope(request)] = generation_after
        self._state.audits.append(audit)
        return self._update(
            request,
            state=QualificationOperationState.ACTIVE,
            generation_before=generation_before,
            generation_after=generation_after,
            audit_reference=_audit_reference(audit.event_id),
        )

    async def revoke(
        self,
        request: QualificationOperationRequest,
        *,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
    ) -> QualificationOperationReceipt:
        current = self._current_binding(request)
        if current.status is AuthorizationRecordStatus.REVOKED:
            return self._update(request, state=QualificationOperationState.REVOKED)
        generation_before = self._generation(request)
        generation_after = generation_before + 1
        audit = self._audit(
            request,
            transition=QualificationOperationState.REVOKED,
            operator_reference=operator_reference,
            approval_reference=approval_reference,
            generation_before=generation_before,
            generation_after=generation_after,
            reason_code="qualification_revoked",
        )
        successor = _successor(
            current,
            status=AuthorizationRecordStatus.REVOKED,
            operation_id=request.operation_id,
            suffix="revoked",
        )
        self._state.histories[current.binding_id].append(successor)
        self._state.generations[_scope(request)] = generation_after
        self._state.audits.append(audit)
        return self._update(
            request,
            state=QualificationOperationState.REVOKED,
            generation_before=generation_before,
            generation_after=generation_after,
            audit_reference=_audit_reference(audit.event_id),
        )

    async def mark_incomplete(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt:
        return self._update(request, state=QualificationOperationState.INCOMPLETE)

    async def mark_cleanup_pending(
        self, request: QualificationOperationRequest
    ) -> QualificationOperationReceipt:
        return self._update(
            request,
            state=QualificationOperationState.CLEANUP_PENDING,
            cleanup_pending=True,
        )

    async def mark_cleaned(
        self,
        request: QualificationOperationRequest,
        *,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
    ) -> QualificationOperationReceipt:
        audit = self._audit(
            request,
            transition=QualificationOperationState.CLEANED,
            operator_reference=operator_reference,
            approval_reference=approval_reference,
            generation_before=self._state.operations[request.operation_id].generation_before,
            generation_after=self._state.operations[request.operation_id].generation_after,
            reason_code="qualification_cleaned",
        )
        self._state.audits.append(audit)
        return self._update(
            request,
            state=QualificationOperationState.CLEANED,
            audit_reference=_audit_reference(audit.event_id),
        )

    def _current_binding(
        self, request: QualificationOperationRequest
    ) -> QualificationAuthorityBinding:
        history = self._state.histories.get(request.profile.binding.binding_id)
        if not history:
            raise QualificationProvisioningError("Qualification binding is unavailable.")
        return history[-1]

    def _generation(self, request: QualificationOperationRequest) -> int:
        return self._state.generations.setdefault(_scope(request), 1)

    def _audit(
        self,
        request: QualificationOperationRequest,
        *,
        transition: QualificationOperationState,
        operator_reference: QualificationOpaqueReference,
        approval_reference: QualificationOpaqueReference,
        generation_before: int | None,
        generation_after: int | None,
        reason_code: str,
    ) -> QualificationAuditRecord:
        if not self._audit_available:
            raise QualificationAuditUnavailable(
                _receipt_for(request, state=QualificationOperationState.INCOMPLETE)
            )
        return QualificationAuditRecord(
            event_id=uuid5(NAMESPACE_URL, f"qualification-audit:{request.operation_id}:{transition.value}"),
            operation_id=request.operation_id,
            operation=request.operation,
            transition=transition,
            profile_id=request.profile.profile_id,
            profile_version=request.profile.profile_version,
            profile_digest=request.profile.profile_digest,
            target_reference=request.profile.target_reference,
            operator_reference=operator_reference,
            approval_reference=approval_reference,
            binding_id=request.profile.binding.binding_id,
            generation_before=generation_before,
            generation_after=generation_after,
            reason_code=reason_code,
        )

    def _update(
        self,
        request: QualificationOperationRequest,
        *,
        state: QualificationOperationState,
        generation_before: int | None = None,
        generation_after: int | None = None,
        audit_reference: QualificationOpaqueReference | None = None,
        cleanup_pending: bool = False,
    ) -> QualificationOperationReceipt:
        current = self._state.operations.get(request.operation_id)
        if current is None:
            current = _receipt_for(request, state=state)
        updated = current.model_copy(
            update={
                "state": state,
                "generation_before": (
                    generation_before
                    if generation_before is not None
                    else current.generation_before
                ),
                "generation_after": (
                    generation_after if generation_after is not None else current.generation_after
                ),
                "audit_reference": audit_reference or current.audit_reference,
                "cleanup_pending": cleanup_pending,
                "replay": False,
            }
        )
        self._state.operations[request.operation_id] = updated
        return updated


def _scope(request: QualificationOperationRequest) -> tuple[UUID, UUID]:
    return request.profile.workspace_id, request.profile.environment_id


def _binding_scope_key(
    binding: QualificationAuthorityBinding,
) -> tuple[UUID, UUID, object, UUID, str | None, str | None]:
    return (
        binding.workspace_id,
        binding.environment_id,
        binding.principal_kind,
        binding.principal_id,
        binding.purpose,
        binding.operation,
    )


def _receipt_for(
    request: QualificationOperationRequest,
    *,
    state: QualificationOperationState,
) -> QualificationOperationReceipt:
    return QualificationOperationReceipt(
        operation_id=request.operation_id,
        request_digest=request.request_digest,
        operation=request.operation,
        state=state,
        profile_id=request.profile.profile_id,
        profile_version=request.profile.profile_version,
        profile_digest=request.profile.profile_digest,
        target_reference=request.profile.target_reference,
        binding_id=request.profile.binding.binding_id,
    )


def _successor(
    current: QualificationAuthorityBinding,
    *,
    status: AuthorizationRecordStatus,
    operation_id: UUID,
    suffix: str,
) -> QualificationAuthorityBinding:
    return current.model_copy(
        update={
            "version": current.version + 1,
            "status": status,
            "provenance": AuthorizationRecordProvenance(
                change_id=uuid5(NAMESPACE_URL, f"qualification-change:{operation_id}:{suffix}"),
                recorded_by="qualification-operator",
            ),
        }
    )


def _audit_reference(event_id: UUID) -> QualificationOpaqueReference:
    return QualificationOpaqueReference(value=f"audit:{event_id.hex}")


__all__ = [
    "InMemoryQualificationProvisioningStore",
    "QualificationAuditUnavailable",
    "QualificationInvocationRejected",
    "QualificationInvocationSource",
    "QualificationOperationConflict",
    "QualificationOperationRequest",
    "QualificationOperationUnavailable",
    "QualificationOperatorAuthorizationPort",
    "QualificationOperatorDenied",
    "QualificationPolicyReferencePort",
    "QualificationProfileRejected",
    "QualificationProvisioningError",
    "QualificationProvisioningService",
    "QualificationProvisioningStore",
    "QualificationSecretPort",
    "RejectAllQualificationOperators",
    "RejectAllQualificationPolicies",
]
