"""Durable, qualification-only projection publication fault injection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.diagnostics import AuditEventRegistry
from spine.application.projection_publication_faults import (
    ProjectionPublicationFaultBoundary,
    ProjectionPublicationFaultInjected,
    ProjectionPublicationFaultReason,
    ProjectionPublicationFaultRejected,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.audit import AuditOutcome, CommandAuditPayload
from spine.memory.contracts import ProjectionCommand


_OPERATION = PersistenceOperation("projection_qualification_fault")
_OPERATION_SCHEMA_VERSION = 1
_RESULT_TYPE = "projection_qualification_fault"
_AUDIT_EVENTS = AuditEventRegistry.with_default_families()
_REASONS_BY_BOUNDARY = {
    ProjectionPublicationFaultBoundary.BEFORE_PHYSICAL_VALIDATION: (
        ProjectionPublicationFaultReason.BEFORE_PHYSICAL_VALIDATION
    ),
    ProjectionPublicationFaultBoundary.AFTER_VALIDATION_BEFORE_ACTIVATION: (
        ProjectionPublicationFaultReason.AFTER_VALIDATION_BEFORE_ACTIVATION
    ),
}


class ProjectionPublicationFaultRule(DefinitionModel):
    boundary: ProjectionPublicationFaultBoundary
    reason_code: ProjectionPublicationFaultReason

    @model_validator(mode="after")
    def validate_reason(self) -> "ProjectionPublicationFaultRule":
        if _REASONS_BY_BOUNDARY[self.boundary] is not self.reason_code:
            raise ValueError("projection fault reason does not match boundary")
        return self


class ProjectionPublicationFaultInjectionProfile(DefinitionModel):
    """One immutable fault set bound to an exact isolated successor command."""

    schema_version: Literal["spine-projection-publication-fault-profile:v1"] = (
        "spine-projection-publication-fault-profile:v1"
    )
    profile_id: str = Field(
        pattern=r"^qualification-[a-z0-9][a-z0-9_.:-]{0,63}$"
    )
    profile_version: VersionIdentifier
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    workspace_id: UUID
    environment_id: UUID
    projection_kind: VersionIdentifier
    successor_command_identity: str = Field(
        pattern=r"^successor-[a-f0-9]{24,54}$"
    )
    rules: tuple[ProjectionPublicationFaultRule, ...] = Field(
        min_length=1, max_length=2
    )

    @model_validator(mode="after")
    def validate_scope_and_rules(self) -> "ProjectionPublicationFaultInjectionProfile":
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("projection fault scope must be non-zero")
        boundaries = tuple(rule.boundary for rule in self.rules)
        if len(boundaries) != len(set(boundaries)):
            raise ValueError("projection fault boundaries must be unique")
        return self


class ProjectionPublicationFaultInjectionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        case_sensitive=False,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    profile_json: str | None = Field(
        default=None,
        repr=False,
        validation_alias="SPINE_PROJECTION_PUBLICATION_FAULT_PROFILE_JSON",
    )

    def load_profile(self) -> ProjectionPublicationFaultInjectionProfile | None:
        if not self.profile_json:
            return None
        try:
            return ProjectionPublicationFaultInjectionProfile.model_validate_json(
                self.profile_json
            )
        except (ValidationError, ValueError):
            raise ValueError("PROJECTION_FAULT_PROFILE_INVALID") from None


class ProjectionPublicationFaultController:
    """Guard and durably consume one injected failure per configured boundary."""

    def __init__(
        self,
        profile: ProjectionPublicationFaultInjectionProfile | None,
        *,
        uow_factory: UnitOfWorkFactory | None = None,
        context_factory: Callable[
            [UUID, UUID, str, UUID], TrustedPersistenceContext
        ]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._profile = profile
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._profile is not None

    def assert_runtime_binding(
        self,
        *,
        commit_sha: str,
        deployment_fingerprint: str,
        provider_profile_digest: str,
        isolated_development: bool,
    ) -> None:
        profile = self._profile
        if profile is None:
            return
        if (
            not isolated_development
            or profile.commit_sha != commit_sha
            or profile.deployment_fingerprint != deployment_fingerprint
            or profile.provider_profile_digest != provider_profile_digest
        ):
            raise ValueError("PROJECTION_FAULT_PROFILE_MISMATCH")

    def assert_command_allowed(self, command: ProjectionCommand) -> None:
        profile = self._profile
        if profile is None:
            return
        if (
            command.workspace_id != profile.workspace_id
            or command.environment_id != profile.environment_id
            or command.physical_namespace.projection_kind
            != profile.projection_kind
            or command.command_identity != profile.successor_command_identity
        ):
            raise ProjectionPublicationFaultRejected()

    async def inject_if_bound(
        self,
        command: ProjectionCommand,
        boundary: ProjectionPublicationFaultBoundary,
    ) -> None:
        profile = self._profile
        if profile is None:
            return
        self.assert_command_allowed(command)
        rule = next(
            (item for item in profile.rules if item.boundary is boundary), None
        )
        if rule is None:
            return
        if (
            self._uow_factory is None
            or self._context_factory is None
            or self._clock is None
        ):
            raise ValueError("PROJECTION_FAULT_PERSISTENCE_UNAVAILABLE")

        payload = {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "commit_sha": profile.commit_sha,
            "deployment_fingerprint": profile.deployment_fingerprint,
            "provider_profile_digest": profile.provider_profile_digest,
            "workspace_id": command.workspace_id,
            "environment_id": command.environment_id,
            "projection_kind": command.physical_namespace.projection_kind,
            "successor_command_identity": command.command_identity,
            "projection_run_id": command.projection_run_id,
            "boundary": boundary,
            "reason_code": rule.reason_code,
        }
        digest = digest_command(
            operation=_OPERATION,
            operation_schema_version=_OPERATION_SCHEMA_VERSION,
            payload=payload,
        )
        key = IdempotencyKey(
            f"projection-fault:{profile.profile_id}:{command.projection_run_id.hex}:{boundary.value}"
        )
        result = OpaqueResultReference(
            result_type=_RESULT_TYPE,
            result_id=uuid5(
                NAMESPACE_URL,
                f"spine:projection-fault:v1:{digest.value}",
            ),
            schema_version=1,
        )
        audit_event_id = uuid5(
            NAMESPACE_URL,
            f"spine:projection-fault-audit:v1:{digest.value}",
        )
        context = self._context_factory(
            command.workspace_id,
            command.environment_id,
            _OPERATION.value,
            command.projection_run_id,
        )
        event = _AUDIT_EVENTS.build_event(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(
                command_type="projection_qualification_fault"
            ),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=self._clock(),
            outcome=AuditOutcome.COMPLETED,
            reason=rule.reason_code.value,
            producer_deduplication_id=f"projection-fault:{digest.value[:32]}",
            audit_event_id=audit_event_id,
        )
        async with self._uow_factory(context) as uow:
            claim = await uow.idempotency.claim(
                operation_schema_version=_OPERATION_SCHEMA_VERSION,
                key=key,
                digest=digest,
            )
            if isinstance(claim, IdempotencyReplay):
                if claim.result != result:
                    raise ValueError("PROJECTION_FAULT_RECEIPT_MISMATCH")
                await uow.commit()
                return
            await uow.audit.append(event)
            await uow.idempotency.complete(claim, result)
            await uow.commit()
        raise ProjectionPublicationFaultInjected(
            rule.reason_code,
            audit_event_id=audit_event_id,
        )


__all__ = [
    "ProjectionPublicationFaultController",
    "ProjectionPublicationFaultInjectionProfile",
    "ProjectionPublicationFaultInjectionSettings",
    "ProjectionPublicationFaultRule",
]
