"""Immutable contracts for governing email-derived source artifacts."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.sources.retention import RetentionArtifactClass


EMAIL_RETENTION_ARTIFACT_CLASSES = frozenset(
    {
        RetentionArtifactClass.EMAIL_RAW_MIME,
        RetentionArtifactClass.EMAIL_PARSED_BODY,
        RetentionArtifactClass.EMAIL_ATTACHMENT_ORIGINAL,
        RetentionArtifactClass.EMAIL_ATTACHMENT_ARTIFACT,
        RetentionArtifactClass.EMAIL_PROJECTION,
    }
)
EMAIL_ATTACHMENT_RETENTION_CLASSES = frozenset(
    {
        RetentionArtifactClass.EMAIL_ATTACHMENT_ORIGINAL,
        RetentionArtifactClass.EMAIL_ATTACHMENT_ARTIFACT,
    }
)


class EmailGovernanceOperation(str, Enum):
    PROCESS = "process"
    RETRIEVE = "retrieve"


class EmailLineageRole(str, Enum):
    MESSAGE = "message"
    ATTACHMENT = "attachment"
    CONTRIBUTOR = "contributor"


class EmailGovernanceReason(str, Enum):
    ALLOWED = "allowed"
    UNAVAILABLE = "unavailable"


class EmailConnectionAuthorityStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"


class EmailProviderPresence(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    DELETED = "deleted"


class EmailSourceLifecycle(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    TOMBSTONED = "tombstoned"


class EmailCurrentSourceAuthority(DefinitionModel):
    """Content-free current source state resolved independently of content."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connection_status: EmailConnectionAuthorityStatus
    provider_presence: EmailProviderPresence
    lifecycle: EmailSourceLifecycle
    authority_generation: int = Field(ge=1)
    authority_reference: UUID

    @model_validator(mode="after")
    def validate_reference(self) -> "EmailCurrentSourceAuthority":
        if self.authority_reference.int == 0:
            raise ValueError("email source authority reference must be non-zero")
        return self

    @property
    def allows_current_content(self) -> bool:
        return (
            self.connection_status is EmailConnectionAuthorityStatus.ACTIVE
            and self.provider_presence is EmailProviderPresence.PRESENT
            and self.lifecycle is EmailSourceLifecycle.CURRENT
        )


class EmailGovernedArtifact(DefinitionModel):
    """Opaque identity of one immutable email source-bearing artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    environment_id: UUID
    artifact_class: RetentionArtifactClass
    artifact_id: UUID
    artifact_version: int = Field(ge=1)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> "EmailGovernedArtifact":
        if self.artifact_class not in EMAIL_RETENTION_ARTIFACT_CLASSES:
            raise ValueError("artifact class is not governed email content")
        if any(
            value.int == 0
            for value in (self.workspace_id, self.environment_id, self.artifact_id)
        ):
            raise ValueError("email artifact identifiers must be non-zero")
        return self


class EmailDerivativeProcessingPlan(DefinitionModel):
    """Source and derivative classes that must be admitted before parsing."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    artifact_classes: tuple[RetentionArtifactClass, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_plan(self) -> "EmailDerivativeProcessingPlan":
        if any(
            value.int == 0
            for value in (
                self.workspace_id,
                self.environment_id,
                self.source_object_id,
                self.source_revision_id,
            )
        ):
            raise ValueError("email processing plan identifiers must be non-zero")
        if len(set(self.artifact_classes)) != len(self.artifact_classes):
            raise ValueError("email processing plan artifact classes must be unique")
        if any(
            item not in EMAIL_RETENTION_ARTIFACT_CLASSES
            for item in self.artifact_classes
        ):
            raise ValueError("email processing plan contains a non-email class")
        return self


class EmailSourceLineage(DefinitionModel):
    """Exact current source revision required by an email artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    role: EmailLineageRole
    source_object_id: UUID
    source_revision_id: UUID

    @model_validator(mode="after")
    def validate_identity(self) -> "EmailSourceLineage":
        if self.source_object_id.int == 0 or self.source_revision_id.int == 0:
            raise ValueError("email lineage identifiers must be non-zero")
        return self


class EmailResolvedLineage(DefinitionModel):
    """Repository-attested parent/child source closure for one artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact: EmailGovernedArtifact
    sources: tuple[EmailSourceLineage, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_closure(self) -> "EmailResolvedLineage":
        identities = tuple(
            (item.source_object_id, item.source_revision_id) for item in self.sources
        )
        if len(set(identities)) != len(identities):
            raise ValueError("email source lineage must be unique")
        roles = tuple(item.role for item in self.sources)
        if roles.count(EmailLineageRole.MESSAGE) != 1:
            raise ValueError("email lineage requires one message parent")
        if self.artifact.artifact_class in EMAIL_ATTACHMENT_RETENTION_CLASSES:
            if roles.count(EmailLineageRole.ATTACHMENT) != 1:
                raise ValueError("attachment lineage requires one attachment child")
        elif EmailLineageRole.ATTACHMENT in roles:
            raise ValueError("non-attachment lineage cannot carry an attachment child")
        return self


class EmailGovernanceSourceEvidence(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_object_id: UUID
    source_revision_id: UUID
    retention_applicability_id: UUID
    retention_applicability_generation: int = Field(ge=1)


class EmailGovernanceEvidence(DefinitionModel):
    """Content-free authority evidence available only on an allowed decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    sources: tuple[EmailGovernanceSourceEvidence, ...] = Field(min_length=1)
    policy_decision_reference: UUID


class EmailGovernanceDecision(DefinitionModel):
    """Public-safe decision: every rejection has the same content-free shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed: bool
    reason: EmailGovernanceReason
    evidence: EmailGovernanceEvidence | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "EmailGovernanceDecision":
        if self.allowed != (self.reason is EmailGovernanceReason.ALLOWED):
            raise ValueError("email governance outcome is inconsistent")
        if self.allowed != (self.evidence is not None):
            raise ValueError("only allowed email governance decisions carry evidence")
        return self


__all__ = [
    "EMAIL_ATTACHMENT_RETENTION_CLASSES",
    "EMAIL_RETENTION_ARTIFACT_CLASSES",
    "EmailConnectionAuthorityStatus",
    "EmailCurrentSourceAuthority",
    "EmailDerivativeProcessingPlan",
    "EmailGovernanceDecision",
    "EmailGovernanceEvidence",
    "EmailGovernanceSourceEvidence",
    "EmailGovernanceOperation",
    "EmailGovernanceReason",
    "EmailGovernedArtifact",
    "EmailLineageRole",
    "EmailProviderPresence",
    "EmailResolvedLineage",
    "EmailSourceLineage",
    "EmailSourceLifecycle",
]
