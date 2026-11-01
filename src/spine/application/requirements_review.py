"""Atomic human review commands for immutable requirements artifacts."""

from __future__ import annotations

from hashlib import sha256
import json
import unicodedata
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.workflow_runs import validate_workflow_command_context
from spine.application.workflow_runs import (
    EditRequirementCommandV1,
    ReclassifyRequirementCommandV1,
    RequestRequirementsReworkCommandV1,
    SaveRequirementsReviewCommandV1,
    WorkflowArtifactRef,
    WorkflowCommand,
)
from spine.domain.common import ActorRef, SpineModel
from spine.domain.workflows import (
    RequirementEvidenceStatus,
    RequirementAcceptanceBasis,
    RequirementReviewState,
    RequirementState,
    RequirementV3,
    RequirementsArtifact,
    WorkflowCommandReceipt,
)


class RequirementsReviewUnavailableError(LookupError):
    """The exact artifact or requirement is unavailable in the authorized scope."""


class RequirementsArtifactStaleError(RuntimeError):
    """The command references an artifact version that is no longer active."""

    def __init__(self, current_ref: WorkflowArtifactRef) -> None:
        super().__init__("Requirements artifact version is stale.")
        self.current_ref = current_ref


class RequirementsReviewReceipt(SpineModel):
    """Stable human-review result reconstructed from canonical command/artifact state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    command_receipt: WorkflowCommandReceipt
    action: Literal[
        "edit_requirement", "reclassify_requirement", "save_review", "request_rework"
    ]
    actor: ActorRef
    reason: str = Field(min_length=1, max_length=2_000)
    changed_requirement_id: UUID | None = None
    base_artifact_ref: WorkflowArtifactRef
    resulting_artifact_ref: WorkflowArtifactRef


class RequirementsReviewService:
    """Admit a human command and append its artifact successor in one UoW."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def dispatch(
        self,
        *,
        context: TrustedPersistenceContext,
        command: WorkflowCommand,
    ) -> RequirementsReviewReceipt | None:
        validate_workflow_command_context(context, command)
        if command.command_type not in {
            "edit_requirement", "reclassify_requirement", "save_review", "request_rework"
        }:
            raise ValueError("requirements review command is invalid")
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements review scope is invalid.")
        base_ref = _single_requirements_ref(command)
        production_identity = (
            f"requirements-human-{command.command_type}:v1:{command.command_id}"
        )

        async with self._uow_factory(context) as uow:
            run = await uow.workflow_runs.get_run(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                run_id=command.run_id,
            )
            if run is None:
                return None
            if (
                run.workflow_key != "commercial.requirements"
                or run.run_id != command.run_id
            ):
                raise RequirementsReviewUnavailableError(
                    "Requirements review is unavailable for this workflow run."
                )
            command_receipt = await uow.workflow_runs.dispatch(command)
            versions = await uow.requirements_artifacts.versions(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                artifact_id=base_ref.artifact_id,
            )
            if command_receipt.admission == "replayed":
                successor = next(
                    (item for item in versions if item.production_identity == production_identity),
                    None,
                )
                if successor is None:
                    raise RequirementsReviewUnavailableError(
                        "Requirements review replay is unavailable."
                    )
            else:
                if not versions:
                    raise RequirementsReviewUnavailableError(
                        "Requirements artifact is unavailable."
                    )
                previous = versions[-1]
                if (
                    previous.run_id != run.run_id
                    or previous.work_item_id != run.work_item_id
                ):
                    raise RequirementsReviewUnavailableError(
                        "Requirements artifact is unavailable for this workflow run."
                    )
                current_ref = _artifact_ref(previous)
                if base_ref != current_ref:
                    raise RequirementsArtifactStaleError(current_ref)
                successor = _review_successor(
                    previous=previous,
                    payload=command.payload,
                    actor=command.actor,
                    command_id=command.command_id,
                    production_identity=production_identity,
                )
                successor = await uow.requirements_artifacts.persist_candidate(successor)
            await uow.commit()

        return RequirementsReviewReceipt(
            command_receipt=command_receipt,
            action=command.command_type,
            actor=command.actor,
            reason=(
                command.payload.rationale
                if isinstance(command.payload, ReclassifyRequirementCommandV1)
                else command.payload.reason
            ),
            changed_requirement_id=(
                _reclassified_requirement_id(
                    command.command_id, command.payload.requirement_id
                )
                if isinstance(command.payload, ReclassifyRequirementCommandV1)
                else command.payload.requirement_id
                if isinstance(command.payload, EditRequirementCommandV1)
                else None
            ),
            base_artifact_ref=base_ref,
            resulting_artifact_ref=_artifact_ref(successor),
        )


def _single_requirements_ref(command: WorkflowCommand) -> WorkflowArtifactRef:
    if len(command.artifact_refs) != 1:
        raise ValueError("requirements review requires one exact artifact reference")
    reference = command.artifact_refs[0]
    if reference.schema_name != "requirements" or not reference.schema_version.startswith(
        "requirements:v"
    ):
        raise ValueError("requirements review artifact reference is invalid")
    return reference


def _artifact_ref(artifact: RequirementsArtifact) -> WorkflowArtifactRef:
    return WorkflowArtifactRef(
        artifact_id=artifact.artifact_id,
        schema_name="requirements",
        schema_version=artifact.schema_version,
        artifact_version=artifact.artifact_version,
    )


def _review_successor(
    *,
    previous: RequirementsArtifact,
    payload: (
        EditRequirementCommandV1
        | ReclassifyRequirementCommandV1
        | SaveRequirementsReviewCommandV1
        | RequestRequirementsReworkCommandV1
    ),
    actor: ActorRef,
    command_id: UUID,
    production_identity: str,
) -> RequirementsArtifact:
    if isinstance(payload, EditRequirementCommandV1):
        found = False
        requirements = []
        for requirement in previous.requirements:
            if requirement.requirement_id == payload.requirement_id:
                found = True
                requirement = requirement.model_copy(
                    update={
                        "statement": payload.statement.strip(),
                        "review_state": RequirementReviewState.REVIEWED,
                        "evidence_refs": tuple(
                            reference.model_copy(update={"validation": None})
                            for reference in requirement.evidence_refs
                        ),
                        "evidence_status": RequirementEvidenceStatus.UNVERIFIED,
                    }
                )
            requirements.append(requirement)
        if not found:
            raise RequirementsReviewUnavailableError("Requirement is unavailable.")
        changed_requirement_id = str(payload.requirement_id)
        statement = payload.statement.strip()
    elif isinstance(payload, ReclassifyRequirementCommandV1):
        found = False
        requirements = []
        statement = payload.statement.strip()
        for requirement in previous.requirements:
            if requirement.requirement_id == payload.requirement_id:
                found = True
                if (
                    (requirement.source_refs or requirement.evidence_refs)
                    and looks_derived_from_protected_text(
                        statement, requirement.statement
                    )
                ):
                    raise RequirementsReviewUnavailableError(
                        "Reclassification requires independently supplied text."
                    )
                if (
                    any(
                        evidence.relationship.value == "contradicts"
                        for evidence in requirement.evidence_refs
                    )
                    and payload.conflict_resolution_rationale is None
                ):
                    raise RequirementsReviewUnavailableError(
                        "Conflicting evidence requires an explicit resolution."
                    )
                requirement = RequirementV3.model_validate(
                    {
                        **requirement.model_dump(),
                        "schema_version": "requirement:v3",
                        "requirement_id": _reclassified_requirement_id(
                            command_id, requirement.requirement_id
                        ),
                        "statement": statement,
                        "state": RequirementState.HUMAN_REVIEWED,
                        "review_state": RequirementReviewState.REVIEWED,
                        "source_refs": (),
                        "evidence_refs": (),
                        "evidence_status": RequirementEvidenceStatus.UNVERIFIED,
                        "missing_markers": (),
                        "ambiguous_markers": (),
                        "upstream_artifact_ids": tuple(
                            dict.fromkeys(
                                (*requirement.upstream_artifact_ids, previous.artifact_id)
                            )
                        ),
                        "acceptance_basis": payload.acceptance_basis,
                        "acceptance_rationale": payload.rationale.strip(),
                        "false_consequence": (
                            payload.false_consequence.strip()
                            if payload.false_consequence is not None
                            else None
                        ),
                        "attributed_by": actor,
                        "assertion_audit_reference": (
                            command_id
                            if payload.acceptance_basis == "manager_assertion"
                            else None
                        ),
                        "independent_human_assertion": (
                            payload.acceptance_basis == "manager_assertion"
                        ),
                        "conflict_resolution_rationale": (
                            payload.conflict_resolution_rationale.strip()
                            if payload.conflict_resolution_rationale is not None
                            else None
                        ),
                    }
                )
            requirements.append(requirement)
        if not found:
            raise RequirementsReviewUnavailableError("Requirement is unavailable.")
        changed_requirement_id = str(payload.requirement_id)
    else:
        review_state = (
            RequirementReviewState.REVIEWED
            if isinstance(payload, SaveRequirementsReviewCommandV1)
            else RequirementReviewState.CHANGES_REQUESTED
        )
        requirements = [
            requirement.model_copy(update={"review_state": review_state})
            for requirement in previous.requirements
        ]
        changed_requirement_id = None
        statement = None
    content = {
        "base_artifact_version": previous.artifact_version,
        "requirement_id": changed_requirement_id,
        "statement": statement,
        "reason": (
            payload.rationale.strip()
            if isinstance(payload, ReclassifyRequirementCommandV1)
            else payload.reason.strip()
        ),
        "acceptance_basis": getattr(payload, "acceptance_basis", None),
        "independence_attestation": getattr(
            payload, "independence_attestation", None
        ),
        "false_consequence": getattr(payload, "false_consequence", None),
        "conflict_resolution_rationale": getattr(
            payload, "conflict_resolution_rationale", None
        ),
    }
    digest = sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return previous.model_copy(
        update={
            "artifact_version": previous.artifact_version + 1,
            "predecessor_version": previous.artifact_version,
            "production_identity": production_identity,
            "production_digest": digest,
            "requirements": tuple(requirements),
            "state": (
                RequirementState.HUMAN_REVIEWED
                if isinstance(
                    payload,
                    (SaveRequirementsReviewCommandV1, ReclassifyRequirementCommandV1),
                )
                else previous.state
            ),
            "schema_version": (
                "requirements:v3"
                if isinstance(payload, ReclassifyRequirementCommandV1)
                else previous.schema_version
            ),
        }
    )


def _normalized_statement(value: str) -> str:
    return " ".join(
        "".join(
            character if not unicodedata.category(character).startswith("P") else " "
            for character in value.casefold()
        ).split()
    )


def looks_derived_from_protected_text(candidate: str, protected: str) -> bool:
    candidate_tokens = set(_normalized_statement(candidate).split())
    protected_tokens = set(_normalized_statement(protected).split())
    if not candidate_tokens or not protected_tokens:
        return True
    overlap = len(candidate_tokens & protected_tokens)
    return overlap / min(len(candidate_tokens), len(protected_tokens)) >= 0.6


def _reclassified_requirement_id(command_id: UUID, requirement_id: UUID) -> UUID:
    return uuid5(
        command_id,
        f"spine.requirement-reclassification:v1:{requirement_id}",
    )


__all__ = [
    "RequirementsArtifactStaleError",
    "RequirementsReviewReceipt",
    "RequirementsReviewService",
    "RequirementsReviewUnavailableError",
    "looks_derived_from_protected_text",
]
