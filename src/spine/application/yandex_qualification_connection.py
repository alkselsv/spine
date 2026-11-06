"""Canonical bootstrap use case for the isolated Yandex qualification authority."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

from spine.application.connections.management import (
    ProviderConnectionConfigurationRequest,
    ProviderConnectionConfigurationResult,
    ProviderConnectionManager,
    ProviderRuntimeObservation,
)
from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    AuditObjectReference,
    AuditOutcome,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.private_qualification_artifacts import (
    PrivateQualificationArtifactWriter,
)
from spine.application.yandex_qualification_credentials import YANDEX_QUALIFICATION_ROOT
from spine.domain.audit import (
    YandexQualificationAuthorityAuditPayload,
    YandexQualificationBinaryReadAuthorityAuditPayload,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnectionApproval,
    ProviderConnectionHealth,
    ProviderConnectionStatus,
    ProviderDiagnosticCode,
    ProviderKind,
)


def _fixed_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:yandex-qualification:678:{name}:v1")


YANDEX_QUALIFICATION_APPROVAL_ID = _fixed_id("approval")
YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID = _fixed_id("binary-read-approval")
YANDEX_QUALIFICATION_BINARY_READ_REFERENCE = _fixed_id("binary-read-qualification")
YANDEX_QUALIFICATION_CONNECTION_ID = _fixed_id("connection")
YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID = _fixed_id("service-principal")
YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE = _fixed_id("credential-reference")
YANDEX_QUALIFICATION_GRANT_REFERENCE = _fixed_id("grant-reference")
YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE = _fixed_id("resource-scope")
YANDEX_QUALIFICATION_REFERENCE = _fixed_id("qualification")
_IDEMPOTENCY_KEY = "issue-762-yandex-qualification-connection-v1"
_BINARY_READ_IDEMPOTENCY_KEY = "issue-789-yandex-binary-read-authority-v1"
_MANIFEST_DIGEST = "487360a49df37aa6fd3dac3b78750b0d991a8a7de44f5fc405a85f304befedc7"


class YandexQualificationBinaryReadConfig(DefinitionModel):
    """Exact human-approved successor authority for issue #789."""

    schema_version: Literal["yandex-qualification-binary-read-config:v1"] = (
        "yandex-qualification-binary-read-config:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    operator_identity_id: UUID
    approved_at: datetime
    expires_at: datetime
    manifest_digest: Literal[_MANIFEST_DIGEST]
    security_approval_reference: Literal["issue-789-comment-6687500665"]
    data_approval_reference: Literal["issue-789-comment-6687500665"]
    max_live_runs: Literal[10]
    download_redirect_host: Literal["mds.storage.yandex.net"]

    @field_validator("workspace_id", "environment_id", "operator_identity_id")
    @classmethod
    def validate_tenant_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        if value.int == 0:
            raise ValueError(f"{info.field_name} must be a non-nil UUID")
        return value

    @field_validator("approved_at", "expires_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info: ValidationInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError(f"{info.field_name} must use UTC")
        return value

    @model_validator(mode="after")
    def validate_campaign_window(self) -> "YandexQualificationBinaryReadConfig":
        lifetime = self.expires_at - self.approved_at
        if lifetime <= timedelta(0) or lifetime > timedelta(days=7):
            raise ValueError("binary-read authority must expire within seven days")
        return self


class YandexQualificationConnectionConfig(DefinitionModel):
    schema_version: Literal["yandex-qualification-connection-config:v1"] = (
        "yandex-qualification-connection-config:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    operator_identity_id: UUID
    approval_id: UUID
    connection_id: UUID
    service_principal_id: UUID
    credential_reference: UUID
    grant_reference: UUID
    resource_scope_reference: UUID
    qualification_reference: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    approved_at: datetime
    security_approval_reference: Literal["issue-701-comment-6539550358"]
    data_approval_reference: Literal["issue-702-comment-6539550684"]
    fixture_manifest_version: Literal["spine-yandex-qualification-fixtures-v1"]
    scope_version: Literal["spine-yandex-qualification-scope:v1"]
    approved_root: Literal[YANDEX_QUALIFICATION_ROOT]

    @field_validator("workspace_id", "environment_id", "operator_identity_id")
    @classmethod
    def validate_tenant_ids(cls, value: UUID, info: ValidationInfo) -> UUID:
        if value.int == 0:
            raise ValueError(f"{info.field_name} must be a non-nil UUID")
        return value

    @field_validator(
        "approval_id",
        "connection_id",
        "service_principal_id",
        "credential_reference",
        "grant_reference",
        "resource_scope_reference",
        "qualification_reference",
    )
    @classmethod
    def validate_fixed_authority(cls, value: UUID, info: ValidationInfo) -> UUID:
        expected = {
            "approval_id": YANDEX_QUALIFICATION_APPROVAL_ID,
            "connection_id": YANDEX_QUALIFICATION_CONNECTION_ID,
            "service_principal_id": YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
            "credential_reference": YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
            "grant_reference": YANDEX_QUALIFICATION_GRANT_REFERENCE,
            "resource_scope_reference": YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
            "qualification_reference": YANDEX_QUALIFICATION_REFERENCE,
        }[info.field_name]
        if value != expected:
            raise ValueError(f"{info.field_name} is not the approved #678 reference")
        return value

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        return value


@dataclass(frozen=True, slots=True)
class YandexQualificationConnectionPlan:
    approval: ProviderConnectionApproval
    request: ProviderConnectionConfigurationRequest
    observation: ProviderRuntimeObservation


class YandexQualificationConnectionReport(DefinitionModel):
    schema_version: Literal["yandex-qualification-connection-report:v1"] = (
        "yandex-qualification-connection-report:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    approval_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    service_principal_id: UUID
    resource_scope_reference: UUID
    qualification_reference: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    security_approval_reference: Literal["issue-701-comment-6539550358"]
    data_approval_reference: Literal["issue-702-comment-6539550684"]
    fixture_manifest_version: Literal["spine-yandex-qualification-fixtures-v1"]
    scope_version: Literal["spine-yandex-qualification-scope:v1"]
    approved_root_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    connection_replay: StrictBool
    provider_kind: Literal["yandex_disk"] = "yandex_disk"
    capability_kinds: tuple[
        Literal["enumeration", "full_reconciliation", "metadata_read"], ...
    ]
    credentials_recorded: Literal[False] = False
    provider_request_made: Literal[False] = False


class YandexQualificationExistingAuthority(Protocol):
    async def verify(self, config: YandexQualificationConnectionConfig) -> None: ...


class YandexQualificationBinaryReadAuthority(Protocol):
    async def verify(self, config: YandexQualificationBinaryReadConfig) -> None: ...


class YandexQualificationConnectionRepository(Protocol):
    async def bootstrap(
        self,
        *,
        plan: YandexQualificationConnectionPlan,
        result: ProviderConnectionConfigurationResult,
        authority_audit: AuditEvent,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult: ...


class YandexQualificationBinaryReadRepository(Protocol):
    async def get_current(
        self, connection_id: UUID, context: TrustedPersistenceContext
    ) -> ProviderConnection: ...

    async def provision(
        self,
        *,
        plan: YandexQualificationConnectionPlan,
        result: ProviderConnectionConfigurationResult,
        authority_audit: AuditEvent,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult: ...


class YandexQualificationConnectionBootstrap:
    def __init__(
        self,
        *,
        authority: YandexQualificationExistingAuthority,
        repository: YandexQualificationConnectionRepository,
        artifact_writer: PrivateQualificationArtifactWriter,
        context: TrustedPersistenceContext,
    ) -> None:
        self._authority = authority
        self._repository = repository
        self._artifact_writer = artifact_writer
        self._context = context

    async def run(
        self, config: YandexQualificationConnectionConfig, *, report_path: Path
    ) -> YandexQualificationConnectionReport:
        if self._context.acting_subject_id != config.operator_identity_id:
            raise ValueError(
                "Yandex qualification operator does not match the trusted context"
            )
        await self._authority.verify(config)
        plan = build_yandex_qualification_connection_plan(config)
        expected = ProviderConnectionManager.build_result(
            request=plan.request,
            approval=plan.approval,
            observation=plan.observation,
            context=self._context,
            replay=False,
        )
        persisted = await self._repository.bootstrap(
            plan=plan,
            result=expected,
            authority_audit=_authority_audit(config, self._context),
            context=self._context,
        )
        connection = persisted.inspection.connection
        report = YandexQualificationConnectionReport(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            approval_id=connection.approval_id,
            connection_id=connection.connection_id,
            connection_version_id=connection.connection_version_id,
            service_principal_id=connection.service_principal_id,
            resource_scope_reference=config.resource_scope_reference,
            qualification_reference=config.qualification_reference,
            authorization_generation=connection.authorization_generation,
            configuration_generation=connection.configuration_generation,
            security_approval_reference=config.security_approval_reference,
            data_approval_reference=config.data_approval_reference,
            fixture_manifest_version=config.fixture_manifest_version,
            scope_version=config.scope_version,
            approved_root_digest=_root_digest(),
            connection_replay=persisted.replay,
            capability_kinds=tuple(
                capability.kind.value for capability in connection.capabilities
            ),
        )
        self._artifact_writer.create(
            report_path, (report.model_dump_json(indent=2) + "\n").encode("utf-8")
        )
        return report


class YandexQualificationBinaryReadReport(DefinitionModel):
    schema_version: Literal["yandex-qualification-binary-read-report:v1"] = (
        "yandex-qualification-binary-read-report:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    approval_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    connection_version: StrictInt = Field(ge=2)
    predecessor_version_id: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    valid_until: datetime
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_kinds: tuple[str, ...]
    connection_replay: StrictBool
    credentials_recorded: Literal[False] = False
    provider_request_made: Literal[False] = False


class YandexQualificationBinaryReadProvisioner:
    def __init__(
        self,
        *,
        authority: YandexQualificationBinaryReadAuthority,
        repository: YandexQualificationBinaryReadRepository,
        artifact_writer: PrivateQualificationArtifactWriter,
        context: TrustedPersistenceContext,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._authority = authority
        self._repository = repository
        self._artifact_writer = artifact_writer
        self._context = context
        self._clock = clock

    async def run(
        self, config: YandexQualificationBinaryReadConfig, *, report_path: Path
    ) -> YandexQualificationBinaryReadReport:
        if self._context.acting_subject_id != config.operator_identity_id:
            raise ValueError(
                "Yandex qualification operator does not match the trusted context"
            )
        if (
            not isinstance(self._context.scope, EnvironmentScope)
            or self._context.scope.workspace_id != config.workspace_id
            or self._context.scope.environment_id != config.environment_id
        ):
            raise ValueError("Yandex qualification scope does not match trusted context")
        evaluated_at = self._clock()
        if (
            config.approved_at > evaluated_at
            or config.expires_at <= evaluated_at
            or config.expires_at > evaluated_at + timedelta(days=7)
        ):
            raise ValueError("Yandex binary-read authority window is not current")
        await self._authority.verify(config)
        predecessor = await self._repository.get_current(
            YANDEX_QUALIFICATION_CONNECTION_ID, self._context
        )
        plan = build_yandex_qualification_binary_read_plan(
            config, predecessor=predecessor
        )
        expected = ProviderConnectionManager.build_result(
            request=plan.request,
            approval=plan.approval,
            observation=plan.observation,
            context=self._context,
            replay=False,
        )
        persisted = await self._repository.provision(
            plan=plan,
            result=expected,
            authority_audit=_binary_read_authority_audit(config, self._context),
            context=self._context,
        )
        connection = persisted.inspection.connection
        if connection.predecessor_version_id is None or connection.valid_until is None:
            raise ValueError("binary-read successor is missing bounded authority lineage")
        report = YandexQualificationBinaryReadReport(
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            approval_id=connection.approval_id,
            connection_id=connection.connection_id,
            connection_version_id=connection.connection_version_id,
            connection_version=connection.version,
            predecessor_version_id=connection.predecessor_version_id,
            authorization_generation=connection.authorization_generation,
            configuration_generation=connection.configuration_generation,
            valid_until=connection.valid_until,
            manifest_digest=config.manifest_digest,
            capability_kinds=tuple(
                capability.kind.value for capability in connection.capabilities
            ),
            connection_replay=persisted.replay,
        )
        self._artifact_writer.create(
            report_path, (report.model_dump_json(indent=2) + "\n").encode("utf-8")
        )
        return report


def build_yandex_qualification_connection_plan(
    config: YandexQualificationConnectionConfig,
) -> YandexQualificationConnectionPlan:
    reference = OpaqueProviderReference(value=config.qualification_reference)
    capabilities = tuple(
        ProviderCapability(
            kind=kind,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=reference,
        )
        for kind in (
            ProviderCapabilityKind.ENUMERATION,
            ProviderCapabilityKind.FULL_RECONCILIATION,
            ProviderCapabilityKind.METADATA_READ,
        )
    )
    return YandexQualificationConnectionPlan(
        approval=ProviderConnectionApproval(
            approval_id=config.approval_id,
            workspace_id=config.workspace_id,
            environment_id=config.environment_id,
            service_principal_id=config.service_principal_id,
            provider_kind=ProviderKind.YANDEX_DISK,
            credential_reference=OpaqueProviderReference(
                value=config.credential_reference
            ),
            grant_reference=OpaqueProviderReference(value=config.grant_reference),
            approved_resource_scope_references=(
                OpaqueProviderReference(value=config.resource_scope_reference),
            ),
            authorization_generation=config.authorization_generation,
            configuration_generation=config.configuration_generation,
            status=ProviderConnectionStatus.ACTIVE,
        ),
        request=ProviderConnectionConfigurationRequest(
            approval_id=config.approval_id,
            connection_id=config.connection_id,
            expected_version=0,
            expected_predecessor_version_id=None,
            idempotency_key=_IDEMPOTENCY_KEY,
            occurred_at=config.approved_at,
        ),
        observation=ProviderRuntimeObservation(
            capabilities=capabilities,
            health=ProviderConnectionHealth.HEALTHY,
            diagnostic_codes=(ProviderDiagnosticCode.CONNECTION_READY,),
            observed_at=config.approved_at,
        ),
    )


def build_yandex_qualification_binary_read_plan(
    config: YandexQualificationBinaryReadConfig,
    *,
    predecessor: ProviderConnection,
) -> YandexQualificationConnectionPlan:
    """Build the one approved successor of the metadata-only authority."""

    if _is_exact_binary_read_successor(predecessor, config):
        assert predecessor.predecessor_version_id is not None
        approval = ProviderConnectionApproval(
            approval_id=predecessor.approval_id,
            workspace_id=predecessor.workspace_id,
            environment_id=predecessor.environment_id,
            service_principal_id=predecessor.service_principal_id,
            provider_kind=predecessor.provider_kind,
            credential_reference=predecessor.credential_reference,
            grant_reference=predecessor.grant_reference,
            approved_resource_scope_references=(
                predecessor.approved_resource_scope_references
            ),
            authorization_generation=predecessor.authorization_generation,
            configuration_generation=predecessor.configuration_generation,
            status=predecessor.status,
            valid_until=predecessor.valid_until,
            approved_manifest_digest=predecessor.approved_manifest_digest,
        )
        return YandexQualificationConnectionPlan(
            approval=approval,
            request=ProviderConnectionConfigurationRequest(
                approval_id=approval.approval_id,
                connection_id=predecessor.connection_id,
                expected_version=1,
                expected_predecessor_version_id=(
                    predecessor.predecessor_version_id
                ),
                idempotency_key=_BINARY_READ_IDEMPOTENCY_KEY,
                occurred_at=config.approved_at,
            ),
            observation=ProviderRuntimeObservation(
                capabilities=predecessor.capabilities,
                health=ProviderConnectionHealth.HEALTHY,
                diagnostic_codes=(ProviderDiagnosticCode.CONNECTION_READY,),
                observed_at=config.approved_at,
            ),
        )

    expected_capabilities = (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.FULL_RECONCILIATION,
        ProviderCapabilityKind.METADATA_READ,
    )
    if (
        predecessor.connection_id != YANDEX_QUALIFICATION_CONNECTION_ID
        or predecessor.approval_id != YANDEX_QUALIFICATION_APPROVAL_ID
        or predecessor.version != 1
        or predecessor.predecessor_version_id is not None
        or predecessor.workspace_id != config.workspace_id
        or predecessor.environment_id != config.environment_id
        or predecessor.service_principal_id
        != YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID
        or predecessor.provider_kind is not ProviderKind.YANDEX_DISK
        or predecessor.credential_reference.value
        != YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE
        or predecessor.grant_reference.value != YANDEX_QUALIFICATION_GRANT_REFERENCE
        or predecessor.approved_resource_scope_references
        != (
            OpaqueProviderReference(
                value=YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE
            ),
        )
        or predecessor.status is not ProviderConnectionStatus.ACTIVE
        or predecessor.valid_until is not None
        or tuple(item.kind for item in predecessor.capabilities)
        != expected_capabilities
        or any(
            item.status is not ProviderCapabilityStatus.SUPPORTED
            for item in predecessor.capabilities
        )
    ):
        raise ValueError("binary-read predecessor is not the approved metadata authority")

    qualification = OpaqueProviderReference(
        value=YANDEX_QUALIFICATION_BINARY_READ_REFERENCE
    )
    capabilities = predecessor.capabilities + (
        ProviderCapability(
            kind=ProviderCapabilityKind.BINARY_READ,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=qualification,
        ),
    )
    approval = ProviderConnectionApproval(
        approval_id=YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID,
        workspace_id=predecessor.workspace_id,
        environment_id=predecessor.environment_id,
        service_principal_id=predecessor.service_principal_id,
        provider_kind=predecessor.provider_kind,
        credential_reference=predecessor.credential_reference,
        grant_reference=predecessor.grant_reference,
        approved_resource_scope_references=(
            predecessor.approved_resource_scope_references
        ),
        authorization_generation=predecessor.authorization_generation,
        configuration_generation=predecessor.configuration_generation + 1,
        status=ProviderConnectionStatus.ACTIVE,
        valid_until=config.expires_at,
        approved_manifest_digest=config.manifest_digest,
    )
    return YandexQualificationConnectionPlan(
        approval=approval,
        request=ProviderConnectionConfigurationRequest(
            approval_id=approval.approval_id,
            connection_id=predecessor.connection_id,
            expected_version=predecessor.version,
            expected_predecessor_version_id=predecessor.connection_version_id,
            idempotency_key=_BINARY_READ_IDEMPOTENCY_KEY,
            occurred_at=config.approved_at,
        ),
        observation=ProviderRuntimeObservation(
            capabilities=capabilities,
            health=ProviderConnectionHealth.HEALTHY,
            diagnostic_codes=(ProviderDiagnosticCode.CONNECTION_READY,),
            observed_at=config.approved_at,
        ),
    )


def _is_exact_binary_read_successor(
    connection: ProviderConnection,
    config: YandexQualificationBinaryReadConfig,
) -> bool:
    expected_qualifications = {
        ProviderCapabilityKind.BINARY_READ: YANDEX_QUALIFICATION_BINARY_READ_REFERENCE,
        ProviderCapabilityKind.ENUMERATION: YANDEX_QUALIFICATION_REFERENCE,
        ProviderCapabilityKind.FULL_RECONCILIATION: YANDEX_QUALIFICATION_REFERENCE,
        ProviderCapabilityKind.METADATA_READ: YANDEX_QUALIFICATION_REFERENCE,
    }
    return (
        connection.connection_id == YANDEX_QUALIFICATION_CONNECTION_ID
        and connection.approval_id == YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID
        and connection.version == 2
        and connection.predecessor_version_id is not None
        and connection.workspace_id == config.workspace_id
        and connection.environment_id == config.environment_id
        and connection.service_principal_id
        == YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID
        and connection.provider_kind is ProviderKind.YANDEX_DISK
        and connection.credential_reference.value
        == YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE
        and connection.grant_reference.value == YANDEX_QUALIFICATION_GRANT_REFERENCE
        and connection.approved_resource_scope_references
        == (
            OpaqueProviderReference(
                value=YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE
            ),
        )
        and connection.status is ProviderConnectionStatus.ACTIVE
        and connection.valid_until == config.expires_at
        and connection.approved_manifest_digest == config.manifest_digest
        and set(item.kind for item in connection.capabilities)
        == set(expected_qualifications)
        and all(
            item.status is ProviderCapabilityStatus.SUPPORTED
            and item.qualification_reference is not None
            and item.qualification_reference.value
            == expected_qualifications[item.kind]
            for item in connection.capabilities
        )
    )


def _authority_audit(
    config: YandexQualificationConnectionConfig, context: TrustedPersistenceContext
) -> AuditEvent:
    return AuditEventRegistry.with_default_families().build_event(
        audit_event_id=uuid5(
            NAMESPACE_URL,
            f"spine:yandex-qualification-authority-audit:v1:{config.approval_id}",
        ),
        workspace_id=config.workspace_id,
        environment_id=config.environment_id,
        event_type="yandex.qualification_authority",
        schema_version=1,
        payload=YandexQualificationAuthorityAuditPayload(
            security_approval_reference=config.security_approval_reference,
            data_approval_reference=config.data_approval_reference,
            fixture_manifest_version=config.fixture_manifest_version,
            scope_version=config.scope_version,
            resource_scope_reference_id=config.resource_scope_reference,
            qualification_reference_id=config.qualification_reference,
            approved_root_digest=_root_digest(),
        ),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=config.approved_at,
        target=AuditObjectReference(
            object_type="provider_connection_approval",
            object_id=config.approval_id,
            schema_version=2,
        ),
        outcome=AuditOutcome.ACCEPTED,
        reason="qualification_authority_recorded",
        producer_deduplication_id=f"{_IDEMPOTENCY_KEY}:authority",
    )


def _binary_read_authority_audit(
    config: YandexQualificationBinaryReadConfig,
    context: TrustedPersistenceContext,
) -> AuditEvent:
    return AuditEventRegistry.with_default_families().build_event(
        audit_event_id=uuid5(
            NAMESPACE_URL,
            "spine:yandex-qualification-binary-read-authority:v1:"
            f"{YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID}",
        ),
        workspace_id=config.workspace_id,
        environment_id=config.environment_id,
        event_type="yandex.qualification_binary_read_authority",
        schema_version=1,
        payload=YandexQualificationBinaryReadAuthorityAuditPayload(
            security_approval_reference=config.security_approval_reference,
            data_approval_reference=config.data_approval_reference,
            manifest_digest=config.manifest_digest,
            resource_scope_reference_id=(
                YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE
            ),
            qualification_reference_id=YANDEX_QUALIFICATION_BINARY_READ_REFERENCE,
            valid_until=config.expires_at,
            max_live_runs=config.max_live_runs,
            redirect_host_digest=hashlib.sha256(
                config.download_redirect_host.encode("utf-8")
            ).hexdigest(),
        ),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=config.approved_at,
        target=AuditObjectReference(
            object_type="provider_connection_approval",
            object_id=YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID,
            schema_version=2,
        ),
        outcome=AuditOutcome.ACCEPTED,
        reason="qualification_binary_read_authority_recorded",
        producer_deduplication_id=(
            f"{_BINARY_READ_IDEMPOTENCY_KEY}:authority"
        ),
    )


def _root_digest() -> str:
    return hashlib.sha256(YANDEX_QUALIFICATION_ROOT.encode()).hexdigest()


__all__ = [
    "YANDEX_QUALIFICATION_APPROVAL_ID",
    "YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID",
    "YANDEX_QUALIFICATION_BINARY_READ_REFERENCE",
    "YANDEX_QUALIFICATION_CONNECTION_ID",
    "YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE",
    "YANDEX_QUALIFICATION_GRANT_REFERENCE",
    "YANDEX_QUALIFICATION_REFERENCE",
    "YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE",
    "YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID",
    "YandexQualificationConnectionBootstrap",
    "YandexQualificationBinaryReadConfig",
    "YandexQualificationBinaryReadProvisioner",
    "YandexQualificationBinaryReadReport",
    "YandexQualificationConnectionConfig",
    "YandexQualificationConnectionPlan",
    "YandexQualificationConnectionReport",
    "build_yandex_qualification_connection_plan",
    "build_yandex_qualification_binary_read_plan",
]
