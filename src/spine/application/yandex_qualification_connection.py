"""Canonical bootstrap use case for the isolated Yandex qualification authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StrictBool, StrictInt, ValidationInfo, field_validator

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
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.private_qualification_artifacts import (
    PrivateQualificationArtifactWriter,
)
from spine.application.yandex_qualification_credentials import YANDEX_QUALIFICATION_ROOT
from spine.domain.audit import YandexQualificationAuthorityAuditPayload
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
YANDEX_QUALIFICATION_CONNECTION_ID = _fixed_id("connection")
YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID = _fixed_id("service-principal")
YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE = _fixed_id("credential-reference")
YANDEX_QUALIFICATION_GRANT_REFERENCE = _fixed_id("grant-reference")
YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE = _fixed_id("resource-scope")
YANDEX_QUALIFICATION_REFERENCE = _fixed_id("qualification")
_IDEMPOTENCY_KEY = "issue-762-yandex-qualification-connection-v1"


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


class YandexQualificationConnectionRepository(Protocol):
    async def bootstrap(
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


def _root_digest() -> str:
    return hashlib.sha256(YANDEX_QUALIFICATION_ROOT.encode()).hexdigest()


__all__ = [
    "YANDEX_QUALIFICATION_APPROVAL_ID",
    "YANDEX_QUALIFICATION_CONNECTION_ID",
    "YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE",
    "YANDEX_QUALIFICATION_GRANT_REFERENCE",
    "YANDEX_QUALIFICATION_REFERENCE",
    "YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE",
    "YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID",
    "YandexQualificationConnectionBootstrap",
    "YandexQualificationConnectionConfig",
    "YandexQualificationConnectionPlan",
    "YandexQualificationConnectionReport",
    "build_yandex_qualification_connection_plan",
]
