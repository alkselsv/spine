"""Authorized acceptance of one exact immutable requirements artifact."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, field_validator

from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.workflow_runs import workflow_command_digest
from spine.application.requirements_review import (
    RequirementsArtifactStaleError,
    RequirementsReviewUnavailableError,
    looks_derived_from_protected_text,
)
from spine.application.workflow_runs import WorkflowArtifactRef, WorkflowCommand
from spine.application.transcript_inspection import (
    TranscriptInspectionResult,
    TranscriptInspectionStatus,
)
from spine.domain.common import ActorKind, ActorRef, SpineModel
from spine.domain.workflows import (
    RequirementEvidenceRelationship,
    RequirementEvidenceValidationStatus,
    RequirementEvidenceValidation,
    RequirementAcceptanceBasis,
    RequirementReviewState,
    RequirementState,
    RequirementsArtifact,
    WorkflowCommandReceipt,
)


REQUIREMENTS_ACCEPTANCE_POLICY_VERSION = "requirements-artifact-acceptance:v1"


class RequirementsAcceptanceDeniedError(PermissionError):
    """Current actor authority or evidence policy denies acceptance."""


class RequirementsAcceptanceAuthority(SpineModel):
    """Server-resolved current human authority for one acceptance decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    actor_id: UUID
    workspace_id: UUID
    environment_id: UUID
    permission: Literal["requirements_artifact.accept"]
    policy_version: Literal["requirements-artifact-acceptance:v1"]
    authentication_configuration_version: str = Field(min_length=1, max_length=255)
    authorization_generation: int = Field(ge=1, strict=True)
    allowed: bool

    @field_validator(
        "actor_id", "workspace_id", "environment_id"
    )
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("requirements acceptance authority identities must be non-zero")
        return value


class RequirementsAcceptanceReceipt(SpineModel):
    """Immutable human decision lineage for an accepted exact artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["requirements-acceptance-receipt:v1"] = (
        "requirements-acceptance-receipt:v1"
    )
    command_receipt: WorkflowCommandReceipt
    accepted_artifact_ref: WorkflowArtifactRef
    accepted_artifact_digest: str
    actor: ActorRef
    policy_version: Literal["requirements-artifact-acceptance:v1"]
    authentication_configuration_version: str = Field(min_length=1, max_length=255)
    authorization_generation: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=255)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_validation_references: tuple[UUID, ...]
    correlation_id: UUID
    causation_id: UUID
    assertion_count: int
    assumption_count: int


class RequirementsAcceptanceEvidenceValidator(Protocol):
    async def validate_artifact(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        artifact: RequirementsArtifact,
    ) -> RequirementsArtifact: ...


class TranscriptEvidenceInspector(Protocol):
    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        workspace_id: UUID,
        environment_id: UUID,
        locator: object,
    ) -> TranscriptInspectionResult: ...


class CurrentRequirementsAcceptanceEvidenceValidator:
    """Revalidate parsed and transcript evidence through their current read gates."""

    def __init__(
        self,
        *,
        parsed: RequirementsAcceptanceEvidenceValidator,
        transcripts: TranscriptEvidenceInspector,
    ) -> None:
        self._parsed = parsed
        self._transcripts = transcripts

    async def validate_artifact(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        artifact: RequirementsArtifact,
    ) -> RequirementsArtifact:
        validated = await self._parsed.validate_artifact(
            context=context, requester=requester, artifact=artifact
        )
        requirements = []
        for requirement in validated.requirements:
            evidence_refs = []
            for evidence in requirement.evidence_refs:
                if evidence.transcript_locator is None:
                    evidence_refs.append(evidence)
                    continue
                result = await self._transcripts.inspect(
                    context=context,
                    requester=requester,
                    workspace_id=artifact.workspace_id,
                    environment_id=artifact.environment_id,
                    locator=evidence.transcript_locator,
                )
                status = {
                    TranscriptInspectionStatus.CURRENT: RequirementEvidenceValidationStatus.VALID,
                    TranscriptInspectionStatus.PERMISSION_DENIED: RequirementEvidenceValidationStatus.DENIED,
                    TranscriptInspectionStatus.STALE: RequirementEvidenceValidationStatus.STALE,
                    TranscriptInspectionStatus.SUPERSEDED: RequirementEvidenceValidationStatus.STALE,
                }.get(result.status, RequirementEvidenceValidationStatus.DEGRADED)
                validation_reference = getattr(result, "audit_reference", None)
                decision_reference = getattr(
                    result, "authorization_decision_reference", None
                )
                evidence_refs.append(
                    evidence.model_copy(
                        update={
                            "validation": (
                                RequirementEvidenceValidation(
                                    validation_reference=validation_reference,
                                    status=status,
                                    authorization_decision_reference=(
                                        decision_reference
                                        if status
                                        is RequirementEvidenceValidationStatus.VALID
                                        else None
                                    ),
                                )
                                if validation_reference is not None
                                and (
                                    status is not RequirementEvidenceValidationStatus.VALID
                                    or decision_reference is not None
                                )
                                else None
                            )
                        }
                    )
                )
            requirements.append(
                requirement.model_copy(update={"evidence_refs": tuple(evidence_refs)})
            )
        return validated.model_copy(update={"requirements": tuple(requirements)})


class RequirementsAcceptanceService:
    """Recheck policy and atomically complete one requirements workflow."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        evidence_validator: RequirementsAcceptanceEvidenceValidator,
    ) -> None:
        self._uow_factory = uow_factory
        self._evidence_validator = evidence_validator

    async def dispatch(
        self,
        *,
        context: TrustedPersistenceContext,
        command: WorkflowCommand,
        authority: RequirementsAcceptanceAuthority,
        requester: ParsedContentInspectionRequester,
    ) -> RequirementsAcceptanceReceipt | None:
        context.validate_shape()
        if command.command_type != "accept_requirements":
            raise ValueError("requirements acceptance command is invalid")
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements acceptance scope is invalid.")
        if (
            command.workspace_id != scope.workspace_id
            or command.environment_id != scope.environment_id
            or command.actor.kind is not ActorKind.HUMAN
            or command.actor.id != context.acting_subject_id
            or requester.subject_id != context.acting_subject_id
            or authority.actor_id != command.actor.id
        ):
            raise RequirementsAcceptanceDeniedError("Requirements acceptance is denied.")
        base_ref = _single_requirements_ref(command)
        command_digest = workflow_command_digest(command).value

        async with self._uow_factory(context) as uow:
            stored = await uow.requirements_acceptances.get(command_id=command.command_id)
            if stored is not None:
                if (
                    stored.idempotency_key != command.idempotency_key
                    or stored.command_digest != command_digest
                ):
                    raise ConstraintConflictError(
                        "Requirements acceptance replay conflicts with prior outcome."
                    )
                await uow.commit()
                return stored.model_copy(
                    update={
                        "command_receipt": stored.command_receipt.model_copy(
                            update={"admission": "replayed"}
                        )
                    }
                )
            if (
                not authority.allowed
                or authority.policy_version != command.payload.policy_version
                or authority.workspace_id != scope.workspace_id
                or authority.environment_id != scope.environment_id
            ):
                raise RequirementsAcceptanceDeniedError(
                    "Requirements acceptance is denied."
                )
            run = await uow.workflow_runs.get_run(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                run_id=command.run_id,
            )
            if run is None:
                return None
            if run.workflow_key != "commercial.requirements" or run.work_item_id is None:
                raise RequirementsReviewUnavailableError(
                    "Requirements acceptance is unavailable for this workflow run."
                )
            versions = await uow.requirements_artifacts.versions(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                artifact_id=base_ref.artifact_id,
            )
            if not versions:
                raise RequirementsReviewUnavailableError(
                    "Requirements artifact is unavailable."
                )
            artifact = versions[-1]
            current_ref = _artifact_ref(artifact)
            if base_ref != current_ref or command.payload.artifact_digest != artifact.production_digest:
                raise RequirementsArtifactStaleError(current_ref)
            if artifact.run_id != run.run_id or artifact.work_item_id != run.work_item_id:
                raise RequirementsReviewUnavailableError(
                    "Requirements artifact is unavailable for this workflow run."
                )
            validated = await self._evidence_validator.validate_artifact(
                context=context,
                requester=requester,
                artifact=artifact,
            )
            predecessor = next(
                (
                    item
                    for item in versions
                    if artifact.predecessor_version is not None
                    and item.artifact_version == artifact.predecessor_version
                ),
                None,
            )
            evidence_references = _enforce_policy(
                validated, command, predecessor=predecessor
            )
            command_receipt = await uow.workflow_runs.dispatch(command)
            if command_receipt.admission == "replayed":
                stored = await uow.requirements_acceptances.get(
                    command_id=command.command_id
                )
                if stored is None:
                    raise RequirementsReviewUnavailableError(
                        "Requirements acceptance replay is unavailable."
                    )
                await uow.commit()
                return stored.model_copy(
                    update={
                        "command_receipt": stored.command_receipt.model_copy(
                            update={"admission": "replayed"}
                        )
                    }
                )
            receipt = RequirementsAcceptanceReceipt(
                command_receipt=command_receipt,
                accepted_artifact_ref=base_ref,
                accepted_artifact_digest=artifact.production_digest,
                actor=command.actor,
                policy_version=command.payload.policy_version,
                authentication_configuration_version=(
                    authority.authentication_configuration_version
                ),
                authorization_generation=authority.authorization_generation,
                idempotency_key=command.idempotency_key,
                command_digest=command_digest,
                evidence_validation_references=evidence_references,
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
                assertion_count=command.payload.acknowledged_assertion_count,
                assumption_count=command.payload.acknowledged_assumption_count,
            )
            stored = await uow.requirements_acceptances.create(receipt)
            await uow.commit()
            return stored


def _enforce_policy(
    artifact: RequirementsArtifact,
    command: WorkflowCommand,
    *,
    predecessor: RequirementsArtifact | None,
) -> tuple[UUID, ...]:
    if artifact.state is not RequirementState.HUMAN_REVIEWED or not artifact.requirements:
        raise RequirementsAcceptanceDeniedError("Requirements acceptance evidence is denied.")
    references: list[UUID] = []
    assertion_count = 0
    assumption_count = 0
    for requirement in artifact.requirements:
        basis = getattr(
            requirement,
            "acceptance_basis",
            RequirementAcceptanceBasis.SOURCE_VALIDATED,
        )
        if (
            requirement.state is not RequirementState.HUMAN_REVIEWED
            or requirement.review_state is not RequirementReviewState.REVIEWED
            or requirement.ambiguous_markers
            or requirement.missing_markers
        ):
            raise RequirementsAcceptanceDeniedError(
                "Requirements acceptance evidence is denied."
            )
        contradictions = tuple(
            evidence
            for evidence in requirement.evidence_refs
            if evidence.relationship is RequirementEvidenceRelationship.CONTRADICTS
        )
        conflict_rationale = getattr(requirement, "conflict_resolution_rationale", None)
        if contradictions and not conflict_rationale:
            raise RequirementsAcceptanceDeniedError(
                "Requirements acceptance evidence is denied."
            )
        if basis is RequirementAcceptanceBasis.SOURCE_VALIDATED:
            if not requirement.evidence_refs or any(
                evidence.relationship is not RequirementEvidenceRelationship.SUPPORTS
                or evidence.validation is None
                or evidence.validation.status is not RequirementEvidenceValidationStatus.VALID
                for evidence in requirement.evidence_refs
            ):
                raise RequirementsAcceptanceDeniedError(
                    "Requirements acceptance evidence is denied."
                )
        elif basis is RequirementAcceptanceBasis.MANAGER_ASSERTION:
            _validate_human_basis(requirement, artifact, command, predecessor)
            assertion_count += 1
        else:
            _validate_human_basis(requirement, artifact, command, predecessor)
            assumption_count += 1
        references.extend(
            evidence.validation.validation_reference
            for evidence in requirement.evidence_refs
            if evidence.validation is not None
        )
    if (
        command.payload.acknowledged_assertion_count != assertion_count
        or command.payload.acknowledged_assumption_count != assumption_count
    ):
        raise RequirementsAcceptanceDeniedError(
            "Requirements acceptance acknowledgement is invalid."
        )
    return tuple(references)


def _validate_human_basis(
    requirement: object,
    artifact: RequirementsArtifact,
    command: WorkflowCommand,
    predecessor: RequirementsArtifact | None,
) -> None:
    if (
        getattr(getattr(requirement, "attributed_by", None), "kind", None)
        is not ActorKind.HUMAN
        or getattr(requirement, "evidence_refs", ())
        or getattr(requirement, "source_refs", ())
    ):
        raise RequirementsAcceptanceDeniedError(
            "Requirements acceptance evidence is denied."
        )
    if predecessor is None:
        return
    prefix = "requirements-human-reclassify_requirement:v1:"
    if not artifact.production_identity.startswith(prefix):
        raise RequirementsAcceptanceDeniedError(
            "Requirements acceptance evidence is denied."
        )
    try:
        reclassification_command_id = UUID(
            artifact.production_identity.removeprefix(prefix)
        )
    except ValueError as error:
        raise RequirementsAcceptanceDeniedError(
            "Requirements acceptance evidence is denied."
        ) from error
    previous = next(
        (
            item
            for item in predecessor.requirements
            if uuid5(
                reclassification_command_id,
                f"spine.requirement-reclassification:v1:{item.requirement_id}",
            )
            == requirement.requirement_id
        ),
        None,
    )
    if (
        previous is None
        or artifact.predecessor_version != predecessor.artifact_version
        or predecessor.artifact_id not in requirement.upstream_artifact_ids
        or (
            requirement.acceptance_basis
            is RequirementAcceptanceBasis.MANAGER_ASSERTION
            and requirement.assertion_audit_reference
            != reclassification_command_id
        )
    ):
        raise RequirementsAcceptanceDeniedError(
            "Requirements acceptance evidence is denied."
        )
    if (
        any(
            evidence.relationship is RequirementEvidenceRelationship.CONTRADICTS
            for evidence in previous.evidence_refs
        )
        and not requirement.conflict_resolution_rationale
    ):
        raise RequirementsAcceptanceDeniedError(
            "Requirements acceptance evidence is denied."
        )
    if (
        (previous.source_refs or previous.evidence_refs)
        and looks_derived_from_protected_text(
            requirement.statement, previous.statement
        )
    ):
        raise RequirementsAcceptanceDeniedError(
            "Requirements acceptance evidence is denied."
        )


def _single_requirements_ref(command: WorkflowCommand) -> WorkflowArtifactRef:
    if len(command.artifact_refs) != 1:
        raise ValueError("requirements acceptance requires one exact artifact reference")
    return command.artifact_refs[0]


def _artifact_ref(artifact: RequirementsArtifact) -> WorkflowArtifactRef:
    return WorkflowArtifactRef(
        artifact_id=artifact.artifact_id,
        schema_name="requirements",
        schema_version=artifact.schema_version,
        artifact_version=artifact.artifact_version,
    )


__all__ = [
    "REQUIREMENTS_ACCEPTANCE_POLICY_VERSION",
    "RequirementsAcceptanceAuthority",
    "CurrentRequirementsAcceptanceEvidenceValidator",
    "RequirementsAcceptanceDeniedError",
    "RequirementsAcceptanceEvidenceValidator",
    "RequirementsAcceptanceReceipt",
    "RequirementsAcceptanceService",
]
