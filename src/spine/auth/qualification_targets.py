"""Provider-neutral SOURCE/RESTORE qualification binding contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Protocol
from uuid import UUID

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator

from spine.domain.common import DefinitionModel, ValidatedDefinitionModel
from spine.auth.records import (
    AuthorizationRecordStatus,
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
)


_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$")


def _non_nil_uuid(value: UUID, info: ValidationInfo) -> UUID:
    if value.int == 0:
        raise ValueError(f"{info.field_name} must be a non-nil UUID")
    return value


def _safe_reference(value: str, info: ValidationInfo) -> str:
    if _REFERENCE.fullmatch(value) is None:
        raise ValueError(f"{info.field_name} must be an approved opaque reference")
    return value


class QualificationTarget(str, Enum):
    SOURCE = "source"
    RESTORE = "restore"


class QualificationTargetRole(str, Enum):
    SOURCE_OPERATOR = "source_operator"
    RESTORE_OPERATOR = "restore_operator"
    VERIFIER = "verifier"


class QualificationDatabaseAccess(str, Enum):
    WRITE = "write"
    READ_ONLY = "read_only"


class QualificationSecretStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ExternalSecretReference(DefinitionModel):
    """References-only input; secret values are intentionally unrepresentable."""

    provider_reference: str = Field(min_length=1, max_length=255)
    secret_reference: str = Field(min_length=1, max_length=255)

    @field_validator("provider_reference", "secret_reference")
    @classmethod
    def _validate_references(cls, value: str, info: ValidationInfo) -> str:
        return _safe_reference(value, info)


class QualificationTargetBinding(ValidatedDefinitionModel):
    """Approved target-specific references for one canonical authority binding."""

    binding_id: UUID
    binding_series_id: UUID
    binding_version: int = Field(ge=1)
    predecessor_binding_id: UUID | None = None
    authority_binding_id: UUID
    authority_binding_version: int = Field(ge=1)
    qualification_id: UUID
    workspace_id: UUID
    environment_id: UUID
    target: QualificationTarget
    target_reference: str = Field(min_length=1, max_length=255)
    target_scope_reference: str = Field(min_length=1, max_length=255)
    principal_id: UUID
    principal_kind: QualificationPrincipalKind
    purpose: str | None = Field(default=None, min_length=1, max_length=64)
    operation: str | None = Field(default=None, min_length=1, max_length=64)
    alias_reference: str = Field(min_length=1, max_length=255)
    secret: ExternalSecretReference
    database_role_reference: str = Field(min_length=1, max_length=255)
    target_role: QualificationTargetRole
    database_access: QualificationDatabaseAccess
    access_policy_references: tuple[UUID, ...] = Field(min_length=1)

    @field_validator(
        "binding_id",
        "binding_series_id",
        "authority_binding_id",
        "qualification_id",
        "workspace_id",
        "environment_id",
        "principal_id",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)

    @field_validator(
        "target_reference",
        "target_scope_reference",
        "alias_reference",
        "database_role_reference",
    )
    @classmethod
    def _validate_reference(cls, value: str, info: ValidationInfo) -> str:
        return _safe_reference(value, info)

    @field_validator("access_policy_references")
    @classmethod
    def _validate_policy_references(cls, values: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(values)) != len(values):
            raise ValueError("access policy references must be unique")
        if any(value.int == 0 for value in values):
            raise ValueError("access policy references must be non-nil UUIDs")
        return tuple(sorted(values, key=str))

    @model_validator(mode="after")
    def _validate_role_scope(self) -> "QualificationTargetBinding":
        if (self.predecessor_binding_id is None) != (self.binding_version == 1):
            raise ValueError("only target binding version 1 may omit a predecessor")
        if self.predecessor_binding_id is not None and self.predecessor_binding_id.int == 0:
            raise ValueError("predecessor_binding_id must be a non-nil UUID")
        if self.binding_version == 1 and self.binding_id != self.binding_series_id:
            raise ValueError("target binding version 1 must start its binding series")
        if self.principal_kind is QualificationPrincipalKind.HUMAN:
            if self.purpose is not None or self.operation is not None:
                raise ValueError("human target binding cannot carry service scope")
        elif self.purpose is None or self.operation is None:
            raise ValueError("service target binding requires purpose and operation")
        if self.target_role is QualificationTargetRole.VERIFIER and self.principal_kind is not QualificationPrincipalKind.SERVICE:
            raise ValueError("verifier must use an independent service principal")
        valid = (
            self.target is QualificationTarget.SOURCE
            and self.target_role is QualificationTargetRole.SOURCE_OPERATOR
            and self.database_access is QualificationDatabaseAccess.WRITE
        ) or (
            self.target is QualificationTarget.RESTORE
            and self.target_role is QualificationTargetRole.RESTORE_OPERATOR
            and self.database_access is QualificationDatabaseAccess.WRITE
        ) or (
            self.target is QualificationTarget.RESTORE
            and self.target_role is QualificationTargetRole.VERIFIER
            and self.database_access is QualificationDatabaseAccess.READ_ONLY
        )
        if not valid:
            raise ValueError("verifier must be restore-only and read-only; operator role must match target")
        return self


class QualificationTargetBindingRequest(ValidatedDefinitionModel):
    """Exact trusted configuration expected by a target process."""

    binding_id: UUID
    binding_series_id: UUID
    binding_version: int = Field(ge=1)
    predecessor_binding_id: UUID | None = None
    authority_binding_id: UUID
    authority_binding_version: int = Field(ge=1)
    qualification_id: UUID
    workspace_id: UUID
    environment_id: UUID
    target: QualificationTarget
    target_reference: str = Field(min_length=1, max_length=255)
    target_scope_reference: str = Field(min_length=1, max_length=255)
    principal_id: UUID
    principal_kind: QualificationPrincipalKind
    purpose: str | None = Field(default=None, min_length=1, max_length=64)
    operation: str | None = Field(default=None, min_length=1, max_length=64)
    alias_reference: str = Field(min_length=1, max_length=255)
    secret: ExternalSecretReference
    database_role_reference: str = Field(min_length=1, max_length=255)
    target_role: QualificationTargetRole
    required_database_access: QualificationDatabaseAccess
    access_policy_references: tuple[UUID, ...] = Field(min_length=1)

    @field_validator(
        "binding_id",
        "binding_series_id",
        "authority_binding_id",
        "qualification_id",
        "workspace_id",
        "environment_id",
        "principal_id",
    )
    @classmethod
    def _validate_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        return _non_nil_uuid(value, info)

    @field_validator(
        "target_reference",
        "target_scope_reference",
        "alias_reference",
        "database_role_reference",
    )
    @classmethod
    def _validate_reference(cls, value: str, info: ValidationInfo) -> str:
        return _safe_reference(value, info)

    @field_validator("access_policy_references")
    @classmethod
    def _validate_policy_references(cls, values: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(values)) != len(values):
            raise ValueError("access policy references must be unique")
        if any(value.int == 0 for value in values):
            raise ValueError("access policy references must be non-nil UUIDs")
        return tuple(sorted(values, key=str))

    @model_validator(mode="after")
    def _validate_history(self) -> "QualificationTargetBindingRequest":
        if (self.predecessor_binding_id is None) != (self.binding_version == 1):
            raise ValueError("only target binding version 1 may omit a predecessor")
        if self.predecessor_binding_id is not None and self.predecessor_binding_id.int == 0:
            raise ValueError("predecessor_binding_id must be a non-nil UUID")
        if self.binding_version == 1 and self.binding_id != self.binding_series_id:
            raise ValueError("target binding version 1 must start its binding series")
        return self


class QualificationBindingDeniedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("qualification target binding denied")


class QualificationSecretDeniedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("qualification secret reference denied")


class QualificationTargetBindingResolver(Protocol):
    async def resolve(
        self, request: QualificationTargetBindingRequest
    ) -> QualificationTargetBinding: ...


class QualificationSecretResolver(Protocol):
    async def resolve(
        self,
        reference: ExternalSecretReference,
        *,
        target: QualificationTarget,
        target_reference: str,
        database_role_reference: str,
    ) -> SecretStr: ...


class InMemoryQualificationTargetBindingResolver:
    """Deterministic adapter that permits only one exact approved binding."""

    def __init__(
        self,
        bindings: tuple[QualificationTargetBinding, ...],
        *,
        authority_bindings: tuple[QualificationAuthorityBinding, ...],
        approved_access_policy_references: frozenset[UUID],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._bindings = tuple(bindings)
        self._authority_bindings = tuple(authority_bindings)
        self._approved_access_policy_references = frozenset(
            approved_access_policy_references
        )
        self._clock = clock

    async def resolve(
        self, request: QualificationTargetBindingRequest
    ) -> QualificationTargetBinding:
        if _has_disallowed_reference_reuse(self._bindings) or _has_invalid_history(
            self._bindings
        ):
            raise QualificationBindingDeniedError()
        matches = tuple(
            binding
            for binding in self._bindings
            if _binding_matches_request(binding, request)
        )
        if len(matches) != 1:
            raise QualificationBindingDeniedError()
        binding = matches[0]
        if not _canonical_authority_is_current(
            binding,
            self._authority_bindings,
            now=self._clock(),
        ):
            raise QualificationBindingDeniedError()
        if not set(binding.access_policy_references).issubset(
            self._approved_access_policy_references
        ):
            raise QualificationBindingDeniedError()
        return binding


class QualificationSecretRecord(DefinitionModel):
    """Synthetic provider result used by the deterministic secret adapter."""

    reference: ExternalSecretReference
    target: QualificationTarget
    target_reference: str = Field(min_length=1, max_length=255)
    database_role_reference: str = Field(min_length=1, max_length=255)
    status: QualificationSecretStatus
    expires_at: datetime
    value: SecretStr

    @field_validator("target_reference", "database_role_reference")
    @classmethod
    def _validate_reference(cls, value: str, info: ValidationInfo) -> str:
        return _safe_reference(value, info)

    @field_validator("expires_at")
    @classmethod
    def _validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("secret expiry must be timezone-aware")
        return value


class InMemoryQualificationSecretResolver:
    """Fake approved provider path with fail-closed reference validation."""

    def __init__(
        self,
        records: tuple[QualificationSecretRecord, ...],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._records = tuple(records)
        self._clock = clock

    async def resolve(
        self,
        reference: ExternalSecretReference,
        *,
        target: QualificationTarget,
        target_reference: str,
        database_role_reference: str,
    ) -> SecretStr:
        matches = tuple(record for record in self._records if record.reference == reference)
        if len(matches) != 1:
            raise QualificationSecretDeniedError()
        record = matches[0]
        if (
            record.status is not QualificationSecretStatus.ACTIVE
            or record.target is not target
            or record.target_reference != target_reference
            or record.database_role_reference != database_role_reference
            or self._clock() >= record.expires_at
        ):
            raise QualificationSecretDeniedError()
        return SecretStr(record.value.get_secret_value())


def _binding_matches_request(
    binding: QualificationTargetBinding,
    request: QualificationTargetBindingRequest,
) -> bool:
    return bool(
        binding.authority_binding_id == request.authority_binding_id
        and binding.binding_id == request.binding_id
        and binding.binding_series_id == request.binding_series_id
        and binding.binding_version == request.binding_version
        and binding.predecessor_binding_id == request.predecessor_binding_id
        and binding.authority_binding_version == request.authority_binding_version
        and binding.qualification_id == request.qualification_id
        and binding.workspace_id == request.workspace_id
        and binding.environment_id == request.environment_id
        and binding.target is request.target
        and binding.target_reference == request.target_reference
        and binding.target_scope_reference == request.target_scope_reference
        and binding.principal_id == request.principal_id
        and binding.principal_kind is request.principal_kind
        and binding.purpose == request.purpose
        and binding.operation == request.operation
        and binding.alias_reference == request.alias_reference
        and binding.secret == request.secret
        and binding.database_role_reference == request.database_role_reference
        and binding.target_role is request.target_role
        and binding.database_access is request.required_database_access
        and binding.access_policy_references == request.access_policy_references
    )


def qualification_target_binding_digest(
    binding: QualificationTargetBinding | QualificationTargetBindingRequest,
) -> str:
    """Canonical digest used for exact, non-discovering adapter resolution."""

    payload = {
        "binding_id": str(binding.binding_id),
        "binding_series_id": str(binding.binding_series_id),
        "binding_version": binding.binding_version,
        "predecessor_binding_id": (
            None
            if binding.predecessor_binding_id is None
            else str(binding.predecessor_binding_id)
        ),
        "authority_binding_id": str(binding.authority_binding_id),
        "authority_binding_version": binding.authority_binding_version,
        "qualification_id": str(binding.qualification_id),
        "workspace_id": str(binding.workspace_id),
        "environment_id": str(binding.environment_id),
        "target": binding.target.value,
        "target_reference": binding.target_reference,
        "target_scope_reference": binding.target_scope_reference,
        "principal_kind": binding.principal_kind.value,
        "principal_id": str(binding.principal_id),
        "purpose": binding.purpose,
        "operation": binding.operation,
        "alias_reference": binding.alias_reference,
        "secret_provider_reference": binding.secret.provider_reference,
        "secret_reference": binding.secret.secret_reference,
        "database_role_reference": binding.database_role_reference,
        "target_role": binding.target_role.value,
        "database_access": (
            binding.database_access.value
            if isinstance(binding, QualificationTargetBinding)
            else binding.required_database_access.value
        ),
        "access_policy_references": sorted(
            str(reference) for reference in binding.access_policy_references
        ),
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_authority_is_current(
    binding: QualificationTargetBinding,
    authorities: tuple[QualificationAuthorityBinding, ...],
    *,
    now: datetime,
) -> bool:
    history = tuple(
        authority
        for authority in authorities
        if authority.binding_id == binding.authority_binding_id
    )
    if not history:
        return False
    current_version = max(authority.version for authority in history)
    matches = tuple(
        authority
        for authority in history
        if authority.version == binding.authority_binding_version
        and authority.version == current_version
        and authority.qualification_id == binding.qualification_id
        and authority.workspace_id == binding.workspace_id
        and authority.environment_id == binding.environment_id
        and authority.principal_kind is binding.principal_kind
        and authority.principal_id == binding.principal_id
        and authority.purpose == binding.purpose
        and authority.operation == binding.operation
    )
    return bool(
        len(matches) == 1
        and matches[0].status is AuthorizationRecordStatus.ACTIVE
        and now < matches[0].expires_at
    )


def _has_disallowed_reference_reuse(
    bindings: tuple[QualificationTargetBinding, ...],
) -> bool:
    for index, left in enumerate(bindings):
        for right in bindings[index + 1 :]:
            shared_private_reference = any(
                left_value == right_value
                for left_value, right_value in (
                    (left.principal_id, right.principal_id),
                    (left.alias_reference, right.alias_reference),
                    (left.secret, right.secret),
                    (left.database_role_reference, right.database_role_reference),
                    (left.target_scope_reference, right.target_scope_reference),
                )
            )
            if left.target is not right.target and (
                shared_private_reference
                or left.target_reference == right.target_reference
            ):
                return True
            if (
                left.target is right.target
                and (
                    left.target_role is not right.target_role
                    or left.binding_series_id != right.binding_series_id
                )
                and shared_private_reference
            ):
                return True
    return False


def _has_invalid_history(bindings: tuple[QualificationTargetBinding, ...]) -> bool:
    identities = {binding.binding_id: binding for binding in bindings}
    if len(identities) != len(bindings):
        return True
    for binding in bindings:
        if binding.binding_version == 1:
            continue
        predecessor = identities.get(binding.predecessor_binding_id)
        if (
            predecessor is None
            or predecessor.binding_series_id != binding.binding_series_id
            or predecessor.binding_version + 1 != binding.binding_version
            or predecessor.target is not binding.target
            or predecessor.target_role is not binding.target_role
            or predecessor.authority_binding_id != binding.authority_binding_id
            or predecessor.authority_binding_version
            >= binding.authority_binding_version
        ):
            return True
    return False


__all__ = [
    "ExternalSecretReference",
    "InMemoryQualificationSecretResolver",
    "InMemoryQualificationTargetBindingResolver",
    "QualificationBindingDeniedError",
    "QualificationDatabaseAccess",
    "QualificationSecretDeniedError",
    "QualificationSecretRecord",
    "QualificationSecretResolver",
    "QualificationSecretStatus",
    "QualificationTarget",
    "QualificationTargetBinding",
    "QualificationTargetBindingRequest",
    "QualificationTargetBindingResolver",
    "QualificationTargetRole",
    "qualification_target_binding_digest",
]
