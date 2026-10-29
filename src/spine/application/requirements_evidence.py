"""Fail-closed validation and inspection for requirements evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from spine.application.content_authorization import (
    AuthorizationDecisionKind,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
)
from spine.application.parsed_content_inspection import (
    ParsedContentDecisionAudit,
    ParsedContentInspectionRequester,
)
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.parsed_artifacts import ParsedArtifactRepository
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.artifacts import Locator, ParsedSegment
from spine.domain.common import DefinitionModel
from spine.domain.sources import RevisionKind
from spine.domain.workflows import (
    Requirement,
    RequirementEvidenceRef,
    RequirementEvidenceStatus,
    RequirementEvidenceValidation,
    RequirementEvidenceValidationStatus,
    RequirementsArtifact,
)


class RequirementsEvidenceResolutionStatus(str, Enum):
    VERIFIED = "verified"
    MISSING = "missing"
    STALE = "stale"
    TOMBSTONED = "tombstoned"
    LOCATOR_MISMATCH = "locator_mismatch"
    FABRICATED = "fabricated"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class RequirementsEvidenceResolution:
    status: RequirementsEvidenceResolutionStatus
    segment: ParsedSegment | None = None


class RequirementsEvidenceResolver(Protocol):
    async def resolve(
        self,
        *,
        context: TrustedPersistenceContext,
        evidence: RequirementEvidenceRef,
    ) -> RequirementsEvidenceResolution: ...


class RequirementsEvidenceAuthorizer(Protocol):
    async def decide_read_content(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> ContentAuthorizationDecision: ...


class RequirementsEvidenceInspectionStatus(str, Enum):
    AVAILABLE = "available"
    DENIED = "denied"
    STALE = "stale"
    TOMBSTONED = "tombstoned"
    MISSING = "missing"
    LOCATOR_MISMATCH = "locator_mismatch"
    FABRICATED = "fabricated"
    DEGRADED = "degraded"


class RequirementsEvidenceInspectionEvidence(DefinitionModel):
    """Protected content released only after exact current checks pass."""

    schema_version: str = "requirements-evidence-inspection-evidence:v1"
    source_object_id: UUID
    source_revision_id: UUID
    segment_id: UUID
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_contract_version: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
    )
    locator: Locator
    text: str = Field(min_length=1, max_length=65_536)
    validation_reference: UUID
    authorization_decision_reference: UUID


class RequirementsEvidenceInspectionResult(DefinitionModel):
    """Safe result; unavailable results contain no source metadata or text."""

    schema_version: str = "requirements-evidence-inspection-result:v1"
    status: RequirementsEvidenceInspectionStatus
    evidence: RequirementsEvidenceInspectionEvidence | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "RequirementsEvidenceInspectionResult":
        if self.status is RequirementsEvidenceInspectionStatus.AVAILABLE:
            if self.evidence is None:
                raise ValueError("available evidence inspection requires evidence")
        elif self.evidence is not None:
            raise ValueError("unavailable evidence inspection cannot contain evidence")
        return self


@dataclass(frozen=True, slots=True)
class RequirementsEvidenceValidationService:
    """Validate candidate references and inspect them through current authority."""

    resolver: RequirementsEvidenceResolver
    authorizer: RequirementsEvidenceAuthorizer
    decision_audit: ParsedContentDecisionAudit | None = None
    validation_reference_factory: Callable[[], UUID] = uuid4
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def validate_artifact(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester | None,
        artifact: RequirementsArtifact,
    ) -> RequirementsArtifact:
        """Return a new artifact projection; never mutate source facts or input."""

        self._validate_scope(context, artifact)
        self._validate_requester(context, requester)
        requirements: list[Requirement] = []
        for requirement in artifact.requirements:
            requirements.append(
                await self._validate_requirement(
                    context=context,
                    requester=requester,
                    requirement=requirement,
                )
            )
        state = artifact.state
        return artifact.model_copy(
            update={"requirements": tuple(requirements), "state": state}
        )

    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        evidence: RequirementEvidenceRef,
    ) -> RequirementsEvidenceInspectionResult:
        """Authorize and return one exact segment, or a content-free safe status."""

        try:
            context.validate_shape()
            self._validate_requester(context, requester)
            self._validate_scope(context, evidence.source_ref)
            resolution = await self.resolver.resolve(context=context, evidence=evidence)
        except Exception:
            return self._safe(RequirementsEvidenceInspectionStatus.DEGRADED)
        status = _inspection_status(resolution.status)
        if resolution.status is not RequirementsEvidenceResolutionStatus.VERIFIED:
            return self._safe(status)
        if resolution.segment is None:
            return self._safe(RequirementsEvidenceInspectionStatus.DEGRADED)
        exact_status = _exact_segment_status(evidence, resolution.segment)
        if exact_status is not RequirementsEvidenceResolutionStatus.VERIFIED:
            return self._safe(_inspection_status(exact_status))

        try:
            decision = await self.authorizer.decide_read_content(
                context=context,
                requester=requester,
                source_object_id=evidence.source_ref.source_object_id,
                source_revision_id=evidence.source_ref.source_revision_id,
            )
            await self._record_decision(
                context=context,
                decision=decision,
                requested_source_revision_id=evidence.source_ref.source_revision_id,
            )
        except Exception:
            return self._safe(RequirementsEvidenceInspectionStatus.DEGRADED)
        if not _decision_matches(decision, context, evidence):
            return self._safe(_authorization_status(decision))

        validation_reference = self.validation_reference_factory()
        if validation_reference.int == 0:
            return self._safe(RequirementsEvidenceInspectionStatus.DEGRADED)
        return RequirementsEvidenceInspectionResult(
            status=RequirementsEvidenceInspectionStatus.AVAILABLE,
            evidence=RequirementsEvidenceInspectionEvidence(
                source_object_id=evidence.source_ref.source_object_id,
                source_revision_id=evidence.source_ref.source_revision_id,
                segment_id=resolution.segment.segment_id,
                segment_digest=resolution.segment.segment_digest,
                locator_contract_version=evidence.locator_contract_version,
                locator=resolution.segment.locator,
                text=resolution.segment.text,
                validation_reference=validation_reference,
                authorization_decision_reference=decision.decision_reference,
            ),
        )

    async def _record_decision(
        self,
        *,
        context: TrustedPersistenceContext,
        decision: ContentAuthorizationDecision,
        requested_source_revision_id: UUID,
    ) -> None:
        if self.decision_audit is None:
            raise RuntimeError("requirements evidence decision audit is unavailable")
        audit_reference = await self.decision_audit.record_decision(
            context=context,
            decision=decision,
            requested_source_revision_id=requested_source_revision_id,
        )
        if not isinstance(audit_reference, UUID) or audit_reference.int == 0:
            raise RuntimeError("requirements evidence decision audit is invalid")

    async def _validate_requirement(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        requirement: Requirement,
    ) -> Requirement:
        if not requirement.evidence_refs:
            return requirement
        validated: list[RequirementEvidenceRef] = []
        for evidence in requirement.evidence_refs:
            validation = await self._validate_reference(
                context=context,
                requester=requester,
                evidence=evidence,
            )
            validated.append(evidence.model_copy(update={"validation": validation}))
        status = (
            RequirementEvidenceStatus.REFERENCED
            if all(
                item.validation is not None
                and item.validation.status is RequirementEvidenceValidationStatus.VALID
                for item in validated
            )
            else RequirementEvidenceStatus.UNSUPPORTED
        )
        return requirement.model_copy(
            update={"evidence_refs": tuple(validated), "evidence_status": status}
        )

    async def _validate_reference(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester | None,
        evidence: RequirementEvidenceRef,
    ) -> RequirementEvidenceValidation:
        reference = self.validation_reference_factory()
        if requester is None:
            return RequirementEvidenceValidation(
                validation_reference=reference,
                status=RequirementEvidenceValidationStatus.DEGRADED,
                validated_at=self.clock(),
            )
        try:
            resolution = await self.resolver.resolve(context=context, evidence=evidence)
            status = _validation_status(resolution.status)
            decision_reference = None
            if resolution.status is RequirementsEvidenceResolutionStatus.VERIFIED:
                if resolution.segment is None:
                    status = RequirementEvidenceValidationStatus.DEGRADED
                else:
                    exact_status = _exact_segment_status(evidence, resolution.segment)
                    if exact_status is not RequirementsEvidenceResolutionStatus.VERIFIED:
                        status = _validation_status(exact_status)
                if status is not RequirementEvidenceValidationStatus.VALID:
                    return RequirementEvidenceValidation(
                        validation_reference=reference,
                        status=status,
                        validated_at=self.clock(),
                    )
                decision = await self.authorizer.decide_read_content(
                    context=context,
                    requester=requester,
                    source_object_id=evidence.source_ref.source_object_id,
                    source_revision_id=evidence.source_ref.source_revision_id,
                )
                await self._record_decision(
                    context=context,
                    decision=decision,
                    requested_source_revision_id=evidence.source_ref.source_revision_id,
                )
                if not _decision_matches(decision, context, evidence):
                    status = _validation_status_for_authorization(decision)
                else:
                    status = RequirementEvidenceValidationStatus.VALID
                decision_reference = decision.decision_reference
        except Exception:
            status = RequirementEvidenceValidationStatus.DEGRADED
        try:
            return RequirementEvidenceValidation(
                validation_reference=reference,
                status=status,
                authorization_decision_reference=decision_reference,
                validated_at=self.clock(),
            )
        except Exception:
            return RequirementEvidenceValidation(
                validation_reference=uuid4(),
                status=RequirementEvidenceValidationStatus.DEGRADED,
                validated_at=datetime.now(timezone.utc),
            )

    @staticmethod
    def _validate_scope(context: TrustedPersistenceContext, value: object) -> None:
        if not isinstance(context.scope, EnvironmentScope):
            raise ValueError("requirements evidence requires environment scope")
        if hasattr(value, "workspace_id") and hasattr(value, "environment_id"):
            if (
                value.workspace_id != context.scope.workspace_id
                or value.environment_id != context.scope.environment_id
            ):
                raise ValueError("requirements evidence scope is invalid")

    @staticmethod
    def _validate_requester(
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester | None,
    ) -> None:
        if requester is not None and context.acting_subject_id != requester.subject_id:
            raise ValueError("requirements evidence requester does not match context")

    @staticmethod
    def _safe(status: RequirementsEvidenceInspectionStatus) -> RequirementsEvidenceInspectionResult:
        return RequirementsEvidenceInspectionResult(status=status)


@dataclass(frozen=True, slots=True)
class PersistedRequirementsEvidenceResolver:
    """Resolve exact source/provenance/parsed lineage through canonical ports."""

    uow_factory: UnitOfWorkFactory
    parsed_artifacts: ParsedArtifactRepository

    async def resolve(
        self,
        *,
        context: TrustedPersistenceContext,
        evidence: RequirementEvidenceRef,
    ) -> RequirementsEvidenceResolution:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.DEGRADED)
        try:
            async with self.uow_factory(context) as uow:
                source = await uow.sources.resolve_source(evidence.source_ref.source_object_id)
                revision = await uow.sources.resolve_revision(evidence.source_ref.source_revision_id)
                if source is None or revision is None:
                    return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.MISSING)
                if (
                    source.workspace_id != scope.workspace_id
                    or source.environment_id != scope.environment_id
                    or revision.workspace_id != scope.workspace_id
                    or revision.environment_id != scope.environment_id
                    or revision.source_object_id != source.source_object_id
                ):
                    return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.FABRICATED)
                provenance = await uow.sources.list_provenance(revision.revision_id)
                if not any(
                    item.provenance_id == evidence.source_ref.provenance_id
                    and item.source_object_id == source.source_object_id
                    and item.revision_id == revision.revision_id
                    and item.workspace_id == scope.workspace_id
                    and item.environment_id == scope.environment_id
                    for item in provenance
                ):
                    return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.FABRICATED)
                head = await uow.canonical.resolve_current(source.source_object_id)
                if head is None:
                    return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.MISSING)
                if head.current_revision_kind is RevisionKind.TOMBSTONE:
                    return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.TOMBSTONED)
                if head.current_revision_id != revision.revision_id:
                    return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.STALE)
            document = await self.parsed_artifacts.get(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                artifact_id=evidence.parsed_artifact_ref.id,
            )
            if document is None:
                return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.MISSING)
            if (
                document.workspace_id != scope.workspace_id
                or document.environment_id != scope.environment_id
                or document.source_object_id != source.source_object_id
                or document.source_revision_id != revision.revision_id
                or document.source_revision_digest != revision.revision_digest
                or document.schema_version != "parsed-document:v1"
                or document.locator_contract_version
                != evidence.locator_contract_version
                or document.coverage.value != "complete"
            ):
                return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.FABRICATED)
            segment = next(
                (
                    item
                    for item in document.segments
                    if item.segment_id == evidence.segment_id
                ),
                None,
            )
            if segment is None:
                return RequirementsEvidenceResolution(
                    RequirementsEvidenceResolutionStatus.MISSING
                )
            if segment.segment_digest != evidence.segment_digest:
                return RequirementsEvidenceResolution(
                    RequirementsEvidenceResolutionStatus.FABRICATED
                )
            if segment.locator != evidence.locator:
                return RequirementsEvidenceResolution(
                    RequirementsEvidenceResolutionStatus.LOCATOR_MISMATCH
                )
            return RequirementsEvidenceResolution(
                RequirementsEvidenceResolutionStatus.VERIFIED,
                segment=segment,
            )
        except Exception:
            return RequirementsEvidenceResolution(RequirementsEvidenceResolutionStatus.DEGRADED)


def _decision_matches(
    decision: ContentAuthorizationDecision,
    context: TrustedPersistenceContext,
    evidence: RequirementEvidenceRef,
) -> bool:
    scope = context.scope
    return bool(
        isinstance(scope, EnvironmentScope)
        and decision.allowed
        and decision.reason_code is AuthorizationReasonCode.ALLOWED
        and decision.decision_kind is AuthorizationDecisionKind.HUMAN_DISCLOSURE
        and decision.workspace_id == scope.workspace_id
        and decision.environment_id == scope.environment_id
        and decision.purpose == context.purpose.value
        and decision.operation == context.operation.value
        and decision.source_object_ids == (evidence.source_ref.source_object_id,)
        and len(decision.policy_evidence) == 1
        and decision.policy_evidence[0].source_object_id
        == evidence.source_ref.source_object_id
        and decision.policy_evidence[0].source_revision_id == evidence.source_ref.source_revision_id
    )


def _exact_segment_status(
    evidence: RequirementEvidenceRef,
    segment: ParsedSegment,
) -> RequirementsEvidenceResolutionStatus:
    if (
        segment.segment_id != evidence.segment_id
        or segment.segment_digest != evidence.segment_digest
    ):
        return RequirementsEvidenceResolutionStatus.FABRICATED
    if segment.locator != evidence.locator:
        return RequirementsEvidenceResolutionStatus.LOCATOR_MISMATCH
    return RequirementsEvidenceResolutionStatus.VERIFIED


def _validation_status(
    status: RequirementsEvidenceResolutionStatus,
) -> RequirementEvidenceValidationStatus:
    if status is RequirementsEvidenceResolutionStatus.VERIFIED:
        return RequirementEvidenceValidationStatus.VALID
    return RequirementEvidenceValidationStatus(status.value)


def _inspection_status(status: RequirementsEvidenceResolutionStatus) -> RequirementsEvidenceInspectionStatus:
    if status is RequirementsEvidenceResolutionStatus.VERIFIED:
        return RequirementsEvidenceInspectionStatus.AVAILABLE
    return RequirementsEvidenceInspectionStatus(status.value)


def _authorization_status(decision: ContentAuthorizationDecision) -> RequirementsEvidenceInspectionStatus:
    if decision.reason_code is AuthorizationReasonCode.AUTHORITY_STALE:
        return RequirementsEvidenceInspectionStatus.DEGRADED
    if decision.reason_code in {
        AuthorizationReasonCode.DECISION_UNAVAILABLE,
        AuthorizationReasonCode.AUDIT_UNAVAILABLE,
    }:
        return RequirementsEvidenceInspectionStatus.DEGRADED
    return RequirementsEvidenceInspectionStatus.DENIED


def _validation_status_for_authorization(
    decision: ContentAuthorizationDecision,
) -> RequirementEvidenceValidationStatus:
    if decision.reason_code is AuthorizationReasonCode.AUTHORITY_STALE:
        return RequirementEvidenceValidationStatus.DEGRADED
    if decision.reason_code in {
        AuthorizationReasonCode.DECISION_UNAVAILABLE,
        AuthorizationReasonCode.AUDIT_UNAVAILABLE,
    }:
        return RequirementEvidenceValidationStatus.DEGRADED
    return RequirementEvidenceValidationStatus.DENIED


__all__ = [
    "PersistedRequirementsEvidenceResolver",
    "RequirementsEvidenceAuthorizer",
    "RequirementsEvidenceInspectionEvidence",
    "RequirementsEvidenceInspectionRequester",
    "RequirementsEvidenceInspectionResult",
    "RequirementsEvidenceInspectionStatus",
    "RequirementsEvidenceResolution",
    "RequirementsEvidenceResolutionStatus",
    "RequirementsEvidenceResolver",
    "RequirementsEvidenceValidationService",
]


RequirementsEvidenceInspectionRequester = ParsedContentInspectionRequester
