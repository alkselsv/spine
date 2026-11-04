"""Disclosure-safe read model for one exact transcription job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.transcription import TranscriptionRepository
from spine.domain.artifacts import TranscriptionJob
from spine.domain.common import DefinitionModel, VersionIdentifier


class TranscriptionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission_denied"


class TranscriptionStatusSnapshot(DefinitionModel):
    """Current processing state without audio handles or provider payloads."""

    schema_version: Literal["transcription-status:v1"] = "transcription-status:v1"
    status: TranscriptionStatus
    job_id: UUID | None = None
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    processing_profile_reference: VersionIdentifier | None = None
    adapter_reference: VersionIdentifier | None = None
    model_config_reference: VersionIdentifier | None = None
    terminal_code: str | None = Field(default=None, min_length=1, max_length=128)
    retryable: bool | None = None
    cancellation_requested: bool | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_disclosure(self) -> "TranscriptionStatusSnapshot":
        protected = (
            self.job_id,
            self.source_object_id,
            self.source_revision_id,
            self.processing_profile_reference,
            self.adapter_reference,
            self.model_config_reference,
            self.cancellation_requested,
            self.created_at,
        )
        unavailable = self.status in {
            TranscriptionStatus.UNAVAILABLE,
            TranscriptionStatus.PERMISSION_DENIED,
        }
        if unavailable:
            unavailable_metadata = (
                *protected,
                self.terminal_code,
                self.retryable,
                self.completed_at,
            )
            if any(value is not None for value in unavailable_metadata):
                raise ValueError(
                    "unavailable transcription status cannot disclose metadata"
                )
            return self
        if any(value is None for value in protected):
            raise ValueError("available transcription status requires pinned identity")
        if self.status is TranscriptionStatus.PENDING:
            if any(
                value is not None
                for value in (
                    self.terminal_code,
                    self.retryable,
                    self.completed_at,
                )
            ):
                raise ValueError("pending transcription cannot contain a terminal result")
            return self
        if self.completed_at is None or self.terminal_code is None:
            raise ValueError("terminal transcription status requires its outcome")
        return self


class TranscriptionStatusAuthorization(DefinitionModel):
    """Current disclosure decision; denial carries no protected lineage."""

    schema_version: Literal["transcription-status-authorization:v1"] = (
        "transcription-status-authorization:v1"
    )
    allowed: bool
    authorization_generation: int = Field(ge=1)
    reason_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{0,63}$",
    )
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    job_id: UUID | None = None
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    source_revision_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_lineage(self) -> "TranscriptionStatusAuthorization":
        lineage = (
            self.workspace_id,
            self.environment_id,
            self.job_id,
            self.source_object_id,
            self.source_revision_id,
            self.source_revision_digest,
        )
        if self.allowed:
            if any(value is None for value in lineage):
                raise ValueError("allowed transcription status requires exact lineage")
            if self.reason_code != "allowed":
                raise ValueError("allowed transcription status requires allowed reason")
        elif any(value is not None for value in lineage):
            raise ValueError("denied transcription status cannot disclose lineage")
        return self


class TranscriptionStatusAuthorizer(Protocol):
    async def recheck(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        job: TranscriptionJob,
    ) -> TranscriptionStatusAuthorization: ...


@dataclass(frozen=True, slots=True)
class TranscriptionStatusQuery:
    """Revalidate current disclosure before exposing processing metadata."""

    repository: TranscriptionRepository
    authorization: TranscriptionStatusAuthorizer

    async def get_status(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        workspace_id: UUID,
        environment_id: UUID,
        job_id: UUID,
    ) -> TranscriptionStatusSnapshot:
        try:
            context.validate_shape()
        except InvalidPersistenceContextError:
            return TranscriptionStatusSnapshot(
                status=TranscriptionStatus.PERMISSION_DENIED
            )
        if (
            not isinstance(context.scope, EnvironmentScope)
            or context.scope.workspace_id != workspace_id
            or context.scope.environment_id != environment_id
        ):
            return TranscriptionStatusSnapshot(
                status=TranscriptionStatus.PERMISSION_DENIED
            )
        job = await self.repository.get_job(
            workspace_id=workspace_id,
            environment_id=environment_id,
            job_id=job_id,
        )
        if job is None:
            return TranscriptionStatusSnapshot(status=TranscriptionStatus.UNAVAILABLE)
        authorization = await self.authorization.recheck(
            context=context,
            requester=requester,
            job=job,
        )
        if not authorization.allowed:
            return TranscriptionStatusSnapshot(
                status=TranscriptionStatus.UNAVAILABLE
            )
        if (
            authorization.workspace_id != job.workspace_id
            or authorization.environment_id != job.environment_id
            or authorization.job_id != job.job_id
            or authorization.source_object_id != job.source_object_id
            or authorization.source_revision_id != job.source_revision_id
            or authorization.source_revision_digest != job.source_revision_digest
        ):
            return TranscriptionStatusSnapshot(
                status=TranscriptionStatus.UNAVAILABLE
            )
        return TranscriptionStatusSnapshot(
            status=TranscriptionStatus(job.state.value),
            job_id=job.job_id,
            source_object_id=job.source_object_id,
            source_revision_id=job.source_revision_id,
            processing_profile_reference=job.processing_profile_reference,
            adapter_reference=job.adapter_reference,
            model_config_reference=job.model_config_reference,
            terminal_code=job.terminal_code,
            retryable=job.retryable,
            cancellation_requested=job.cancellation_requested_at is not None,
            created_at=job.created_at,
            completed_at=job.completed_at,
        )


__all__ = [
    "TranscriptionStatus",
    "TranscriptionStatusAuthorization",
    "TranscriptionStatusAuthorizer",
    "TranscriptionStatusQuery",
    "TranscriptionStatusSnapshot",
]
