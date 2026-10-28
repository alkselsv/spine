"""One-shot qualification of an application rebuilt from verified restore evidence.

This module owns only the fail-closed ordering.  Restore verification, target
preparation, projection publication and the authorized application read remain
behind their existing seams.  The target is always re-sealed after preparation,
and the result contains metadata only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.domain.common import ValidatedDefinitionModel


class RecoveredApplicationQualificationRequest(ValidatedDefinitionModel):
    """Immutable operator-approved scope for one isolated qualification run."""

    schema_version: Literal["recovered-application-qualification-request:v1"] = (
        "recovered-application-qualification-request:v1"
    )
    run_reference: str = Field(pattern=r"^run:[a-z0-9_.:-]{1,123}$")
    authorization_reference: str = Field(pattern=r"^approval:[a-z0-9_.:-]{1,118}$")
    target_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_reference: str = Field(pattern=r"^target:[a-z0-9_.:-]{1,120}$")
    recovery_set_id: str = Field(pattern=r"^recovery:[a-z0-9_.:-]{1,118}$")
    restore_evidence_reference: str = Field(pattern=r"^restore:[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    source_revision_id: UUID
    projection_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    expires_at: datetime
    isolated_activation: Literal[True] = True
    synthetic_data_only: Literal[True] = True
    external_serving_allowed: Literal[False] = False
    cutover_allowed: Literal[False] = False

    @field_validator("workspace_id", "environment_id", "source_revision_id")
    @classmethod
    def _validate_ids(cls, value: UUID, info: object) -> UUID:
        if value.int == 0:
            raise ValueError(f"{getattr(info, 'field_name', 'identifier')} must be non-zero")
        return value

    @field_validator("expires_at")
    @classmethod
    def _validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return value


class ProjectionRecoveryObservation(ValidatedDefinitionModel):
    """Content-free result from recovery and normal publication in the target."""

    verified: bool
    projection_snapshot_reference: str | None = Field(
        default=None, pattern=r"^snapshot:[a-z0-9_.:-]{1,116}$"
    )
    activation_generation: int | None = Field(default=None, ge=1)
    evidence_reference: str | None = Field(
        default=None, pattern=r"^evidence:[a-z0-9_.:-]{1,116}$"
    )

    @model_validator(mode="after")
    def _validate_verified_observation(self) -> "ProjectionRecoveryObservation":
        complete = (
            self.projection_snapshot_reference is not None
            and self.activation_generation is not None
            and self.evidence_reference is not None
        )
        if self.verified != complete:
            raise ValueError("verified projection observation must be complete")
        return self


class ApplicationCanaryObservation(ValidatedDefinitionModel):
    """Content-free outcome of one authorized read through the application path."""

    verified: bool
    source_revision_id: UUID | None = None
    evidence_count: int = Field(ge=0)
    diagnostic_reference: str | None = Field(
        default=None, pattern=r"^diagnostic:[a-z0-9_.:-]{1,114}$"
    )

    @model_validator(mode="after")
    def _validate_verified_observation(self) -> "ApplicationCanaryObservation":
        complete = (
            self.source_revision_id is not None
            and self.evidence_count > 0
            and self.diagnostic_reference is not None
        )
        if self.verified != complete:
            raise ValueError("verified canary observation must be complete")
        return self


class RecoveredApplicationQualificationResult(ValidatedDefinitionModel):
    """Disclosure-safe result suitable for immutable qualification evidence."""

    schema_version: Literal["recovered-application-qualification-result:v1"] = (
        "recovered-application-qualification-result:v1"
    )
    run_reference: str
    authorization_reference: str
    target_reference: str
    status: Literal["verified", "not_verified", "blocked", "indeterminate"]
    safe_failure_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$"
    )
    projection_snapshot_reference: str | None = None
    activation_generation: int | None = Field(default=None, ge=1)
    projection_evidence_reference: str | None = None
    canary_diagnostic_reference: str | None = None
    canary_evidence_count: int = Field(default=0, ge=0)
    target_sealed: bool


class VerifiedRestoreEvidencePort(Protocol):
    async def verify(self, qualification: RecoveredApplicationQualificationRequest) -> bool: ...


class RecoveredApplicationTargetPort(Protocol):
    async def prepare(self, qualification: RecoveredApplicationQualificationRequest) -> None: ...

    async def seal(self, qualification: RecoveredApplicationQualificationRequest) -> None: ...


class ProjectionRecoveryQualificationPort(Protocol):
    async def recover_and_activate(
        self, qualification: RecoveredApplicationQualificationRequest
    ) -> ProjectionRecoveryObservation: ...


class ApplicationCanaryPort(Protocol):
    async def read_permitted_source(
        self,
        qualification: RecoveredApplicationQualificationRequest,
        projection: ProjectionRecoveryObservation,
    ) -> ApplicationCanaryObservation: ...


class RecoveredApplicationQualificationService:
    """Qualify one restored target and fail closed on every partial outcome."""

    def __init__(
        self,
        *,
        restore_evidence: VerifiedRestoreEvidencePort,
        target: RecoveredApplicationTargetPort,
        projection: ProjectionRecoveryQualificationPort,
        canary: ApplicationCanaryPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._restore_evidence = restore_evidence
        self._target = target
        self._projection = projection
        self._canary = canary
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def qualify(
        self, request: RecoveredApplicationQualificationRequest
    ) -> RecoveredApplicationQualificationResult:
        if self._clock() >= request.expires_at:
            return self._result(request, status="blocked", failure="authorization_expired")
        try:
            restore_verified = await self._restore_evidence.verify(request)
        except Exception:
            restore_verified = False
        if not restore_verified:
            return self._result(
                request, status="blocked", failure="restore_evidence_unverified"
            )

        prepared = False
        seal_failed = False
        result = self._result(request, status="not_verified", failure="target_prepare_failed")
        try:
            prepared = True
            await self._target.prepare(request)
            projection = await self._projection.recover_and_activate(request)
            if not projection.verified:
                result = self._result(
                    request, status="not_verified", failure="projection_recovery_failed"
                )
            else:
                canary = await self._canary.read_permitted_source(request, projection)
                if not canary.verified or canary.source_revision_id != request.source_revision_id:
                    result = self._result(
                        request,
                        status="not_verified",
                        failure="application_canary_failed",
                        projection=projection,
                    )
                else:
                    result = self._result(
                        request,
                        status="verified",
                        projection=projection,
                        canary=canary,
                    )
        except Exception:
            result = self._result(
                request, status="not_verified", failure="qualification_step_failed"
            )
        finally:
            if prepared:
                try:
                    await self._target.seal(request)
                except Exception:
                    seal_failed = True
        if seal_failed:
            return self._result(
                request,
                status="indeterminate",
                failure="target_seal_failed",
                target_sealed=False,
            )
        return result

    @staticmethod
    def _result(
        request: RecoveredApplicationQualificationRequest,
        *,
        status: Literal["verified", "not_verified", "blocked", "indeterminate"],
        failure: str | None = None,
        projection: ProjectionRecoveryObservation | None = None,
        canary: ApplicationCanaryObservation | None = None,
        target_sealed: bool = True,
    ) -> RecoveredApplicationQualificationResult:
        return RecoveredApplicationQualificationResult(
            run_reference=request.run_reference,
            authorization_reference=request.authorization_reference,
            target_reference=request.target_reference,
            status=status,
            safe_failure_code=failure,
            projection_snapshot_reference=(
                projection.projection_snapshot_reference if projection else None
            ),
            activation_generation=(projection.activation_generation if projection else None),
            projection_evidence_reference=(projection.evidence_reference if projection else None),
            canary_diagnostic_reference=(canary.diagnostic_reference if canary else None),
            canary_evidence_count=(canary.evidence_count if canary else 0),
            target_sealed=target_sealed,
        )


__all__ = [
    "ApplicationCanaryObservation",
    "ApplicationCanaryPort",
    "ProjectionRecoveryObservation",
    "ProjectionRecoveryQualificationPort",
    "RecoveredApplicationQualificationRequest",
    "RecoveredApplicationQualificationResult",
    "RecoveredApplicationQualificationService",
    "RecoveredApplicationTargetPort",
    "VerifiedRestoreEvidencePort",
]
