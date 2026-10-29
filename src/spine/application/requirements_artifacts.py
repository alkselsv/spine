"""Use case boundary for immutable conversational requirements artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Callable, Protocol
from uuid import UUID, uuid5, NAMESPACE_URL

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidPersistenceContextError,
    OptimisticConflictError,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.requirements_evidence import RequirementsEvidenceValidationService
from spine.application.workflow_runs import AnswerClarificationCommandV1, WorkflowCommand
from spine.domain.workflows import (
    Clarification,
    ClarificationAnswer,
    ClarificationReason,
    Requirement,
    RequirementCategory,
    RequirementState,
    WorkflowRun,
)
from spine.domain.common import ActorKind, ActorRef
from spine.domain.capabilities.extract_requirements import (
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
)
from spine.domain.workflows.requirements import RequirementsArtifact


class RequirementsArtifactRepository(Protocol):
    async def persist_candidate(self, artifact: RequirementsArtifact) -> RequirementsArtifact: ...

    async def get(self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID,
                  artifact_version: int | None = None) -> RequirementsArtifact | None: ...

    async def versions(self, *, workspace_id: UUID, environment_id: UUID,
                       artifact_id: UUID) -> tuple[RequirementsArtifact, ...]: ...

    async def get_for_run(self, *, workspace_id: UUID, environment_id: UUID,
                          run_id: UUID) -> RequirementsArtifact | None: ...


class RequirementsClarificationRevalidator(Protocol):
    """Re-run the affected #641 extraction result against one typed answer."""

    async def revalidate(
        self,
        *,
        previous: RequirementsArtifact,
        requirement: Requirement,
        clarification: Clarification,
        answer: ClarificationAnswer,
    ) -> RequirementsExtractionOutput: ...


class DeterministicRequirementsClarificationRevalidator:
    """Offline extraction revalidator used until the configured agent is composed here."""

    async def revalidate(
        self,
        *,
        previous: RequirementsArtifact,
        requirement: Requirement,
        clarification: Clarification,
        answer: ClarificationAnswer,
    ) -> RequirementsExtractionOutput:
        del clarification
        resolution = answer.text.strip()
        resolved = requirement.model_copy(
            update={
                "statement": (
                    resolution
                    if requirement.category is RequirementCategory.UNRESOLVED_INFORMATION
                    else requirement.statement
                ),
                "constraints": (
                    requirement.constraints
                    if requirement.category is RequirementCategory.UNRESOLVED_INFORMATION
                    else (*requirement.constraints, resolution)
                ),
                "missing_markers": (),
                "ambiguous_markers": (),
                "uncertainty": None,
                "state": RequirementState.CANDIDATE,
            }
        )
        return RequirementsExtractionOutput(
            workspace_id=previous.workspace_id,
            environment_id=previous.environment_id,
            workflow_run_id=previous.run_id,
            work_item_id=previous.work_item_id,
            outcome=RequirementsExtractionOutcome.SUCCESS,
            requirements=(resolved,),
        )


class PersistedRequirementsArtifactService:
    """Create a deterministic synthetic first candidate and serve scoped readback."""

    _production_identity = "requirements-extraction-v1"

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        evidence_validation_service: RequirementsEvidenceValidationService | None = None,
        clarification_revalidator: RequirementsClarificationRevalidator | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._evidence_validation_service = evidence_validation_service
        self._clarification_revalidator = (
            clarification_revalidator
            or DeterministicRequirementsClarificationRevalidator()
        )
        self._clock = clock

    async def produce_first_candidate(
        self, *, context: TrustedPersistenceContext, run_id: UUID
    ) -> RequirementsArtifact | None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        async with self._uow_factory(context) as uow:
            run = await uow.workflow_runs.get_run(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                run_id=run_id,
            )
            if run is None:
                return None
            if (run.workspace_id, run.environment_id) != (
                context.scope.workspace_id, context.scope.environment_id
            ):
                raise InvalidPersistenceContextError("Workflow run scope is invalid.")
            artifact = _deterministic_candidate(run)
            persisted = await uow.requirements_artifacts.persist_candidate(artifact)
            await _enter_clarification_wait(uow, persisted)
            await uow.commit()
            return persisted

    async def persist_extracted_candidate(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        extraction: RequirementsExtractionOutput,
        upstream_artifact_ids: tuple[UUID, ...] = (),
        requester: ParsedContentInspectionRequester | None = None,
    ) -> RequirementsArtifact | None:
        """Persist one runtime extraction through the #640 version boundary.

        Success and partial results become the first immutable candidate version.
        Abstentions and typed failures deliberately do not manufacture an
        artifact; their runtime result remains the workflow's failure/awaiting
        state.  Replay and conflict behavior stays owned by the existing
        ``RequirementsArtifactRepository``.
        """

        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        if type(extraction) is not RequirementsExtractionOutput:
            raise ValueError("requirements extraction output is invalid")
        if extraction.outcome not in {
            RequirementsExtractionOutcome.SUCCESS,
            RequirementsExtractionOutcome.PARTIAL,
        }:
            return None
        async with self._uow_factory(context) as uow:
            run = await uow.workflow_runs.get_run(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                run_id=run_id,
            )
            if run is None:
                return None
            if (
                run.workspace_id != context.scope.workspace_id
                or run.environment_id != context.scope.environment_id
                or extraction.workspace_id != run.workspace_id
                or extraction.environment_id != run.environment_id
                or extraction.workflow_run_id != run.run_id
                or extraction.work_item_id != run.work_item_id
            ):
                raise InvalidPersistenceContextError("Requirements extraction scope is invalid.")
            lineage = tuple(dict.fromkeys(upstream_artifact_ids))
            content = {
                "extraction": extraction.model_dump(mode="json"),
                "upstream_artifact_ids": [str(item) for item in lineage],
            }
            digest = sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            artifact = RequirementsArtifact(
                artifact_id=uuid5(NAMESPACE_URL, f"spine:requirements:{run.run_id}"),
                artifact_version=1,
                production_identity=self._production_identity,
                production_digest=digest,
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                work_item_id=run.work_item_id,
                run_id=run.run_id,
                requirements=extraction.requirements,
                state=(
                    RequirementState.PARTIAL
                    if extraction.outcome is RequirementsExtractionOutcome.PARTIAL
                    else RequirementState.CANDIDATE
                ),
                upstream_artifact_ids=lineage,
            )
            artifact = _with_open_clarifications(artifact, run)
            if self._evidence_validation_service is not None:
                artifact = await self._evidence_validation_service.validate_artifact(
                    context=context,
                    requester=requester,
                    artifact=artifact,
                )
            persisted = await uow.requirements_artifacts.persist_candidate(artifact)
            await _enter_clarification_wait(uow, persisted)
            await uow.commit()
            return persisted

    async def get(self, *, context: TrustedPersistenceContext, artifact_id: UUID,
                  artifact_version: int | None = None) -> RequirementsArtifact | None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        async with self._uow_factory(context) as uow:
            return await uow.requirements_artifacts.get(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                artifact_id=artifact_id,
                artifact_version=artifact_version,
            )

    async def prepare_clarification_answer(
        self,
        *,
        context: TrustedPersistenceContext,
        command: WorkflowCommand,
        requester: ParsedContentInspectionRequester | None = None,
    ) -> RequirementsArtifact:
        """Revalidate the affected requirement and prepare one exact successor."""

        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        if (
            command.command_type != "answer_clarification"
            or type(command.payload) is not AnswerClarificationCommandV1
            or len(command.artifact_refs) != 1
        ):
            raise ConstraintConflictError("Workflow clarification command is invalid.")
        artifact_ref = command.artifact_refs[0]
        async with self._uow_factory(context) as uow:
            versions = await uow.requirements_artifacts.versions(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                artifact_id=artifact_ref.artifact_id,
            )
        if not versions:
            raise ConstraintConflictError("Requirements artifact is unavailable.")
        previous = versions[-1]
        if (
            previous.run_id != command.run_id
            or previous.schema_version != artifact_ref.schema_version
            or previous.artifact_version != artifact_ref.artifact_version
        ):
            raise OptimisticConflictError("Requirements artifact version is stale.")
        clarification = next(
            (
                item
                for item in previous.clarifications
                if item.clarification_id == command.payload.clarification_id
            ),
            None,
        )
        if (
            clarification is None
            or clarification.answer is not None
            or clarification.requirement_id != command.payload.requirement_id
            or clarification.originating_artifact_version != previous.artifact_version
        ):
            raise ConstraintConflictError("Workflow clarification is unavailable.")
        affected = next(
            (
                item
                for item in previous.requirements
                if item.requirement_id == command.payload.requirement_id
            ),
            None,
        )
        if affected is None:
            raise ConstraintConflictError("Workflow clarification is unavailable.")

        answered_event_id = uuid5(
            NAMESPACE_URL, f"spine:clarification-answered:v1:{command.command_id}"
        )
        answered = clarification.model_copy(
            update={
                "answer": ClarificationAnswer(
                    text=command.payload.answer,
                    answered_by=command.actor,
                    answered_event_id=answered_event_id,
                    answered_at=self._clock(),
                ),
                "resulting_artifact_version": previous.artifact_version + 1,
            }
        )
        revalidated_extraction = await self._clarification_revalidator.revalidate(
            previous=previous,
            requirement=affected,
            clarification=clarification,
            answer=answered.answer,
        )
        if (
            revalidated_extraction.workspace_id != previous.workspace_id
            or revalidated_extraction.environment_id != previous.environment_id
            or revalidated_extraction.workflow_run_id != previous.run_id
            or revalidated_extraction.work_item_id != previous.work_item_id
            or revalidated_extraction.outcome
            not in {
                RequirementsExtractionOutcome.SUCCESS,
                RequirementsExtractionOutcome.PARTIAL,
            }
            or len(revalidated_extraction.requirements) != 1
            or revalidated_extraction.requirements[0].requirement_id
            != affected.requirement_id
        ):
            raise ConstraintConflictError(
                "Workflow clarification extraction revalidation is invalid."
            )
        updated_requirement = revalidated_extraction.requirements[0]
        requirements = tuple(
            updated_requirement if item.requirement_id == affected.requirement_id else item
            for item in previous.requirements
        )
        state = (
            RequirementState.PARTIAL
            if any(
                item.state is RequirementState.PARTIAL
                or item.missing_markers
                or item.ambiguous_markers
                for item in requirements
            )
            else RequirementState.CANDIDATE
        )
        clarifications = tuple(
            answered if item.clarification_id == answered.clarification_id else item
            for item in previous.clarifications
        )
        successor = RequirementsArtifact.model_validate(
            {
                **previous.model_dump(mode="python"),
                "artifact_version": previous.artifact_version + 1,
                "predecessor_version": previous.artifact_version,
                "production_identity": f"clarification-answer:v1:{command.command_id}",
                "production_digest": "0" * 64,
                "requirements": requirements,
                "clarifications": clarifications,
                "state": state,
            }
        )
        if self._evidence_validation_service is not None:
            affected_projection = successor.model_copy(
                update={
                    "requirements": (updated_requirement,),
                    "clarifications": (answered,),
                }
            )
            validated = await self._evidence_validation_service.validate_artifact(
                context=context,
                requester=requester,
                artifact=affected_projection,
            )
            validated_requirement = validated.requirements[0]
            requirements = tuple(
                validated_requirement if item.requirement_id == affected.requirement_id else item
                for item in requirements
            )
            validation_references = tuple(
                evidence.validation.validation_reference
                for evidence in validated_requirement.evidence_refs
                if evidence.validation is not None
            )
            answered = answered.model_copy(
                update={"affected_validation_references": validation_references}
            )
            clarifications = tuple(
                answered if item.clarification_id == answered.clarification_id else item
                for item in clarifications
            )
        next_clarification = _next_open_clarification(
            artifact=successor.model_copy(
                update={
                    "requirements": requirements,
                    "clarifications": clarifications,
                }
            ),
            asked_at=answered.answer.answered_at,
            correlation_id=command.correlation_id,
            excluded_requirement_ids=(affected.requirement_id,),
        )
        if next_clarification is not None:
            clarifications = (*clarifications, next_clarification)
        digest = _successor_digest(
            requirements=requirements,
            clarifications=clarifications,
            state=state,
            command_id=command.command_id,
        )
        return RequirementsArtifact.model_validate(
            {
                **successor.model_dump(mode="python"),
                "production_digest": digest,
                "requirements": requirements,
                "clarifications": clarifications,
            }
        )

    async def get_for_run(self, *, context: TrustedPersistenceContext,
                          run_id: UUID) -> RequirementsArtifact | None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        async with self._uow_factory(context) as uow:
            return await uow.requirements_artifacts.get_for_run(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                run_id=run_id,
            )

    async def persist_successor(
        self, *, context: TrustedPersistenceContext, artifact_id: UUID,
        production_identity: str, requirements: tuple[Requirement, ...],
        state: RequirementState, upstream_artifact_ids: tuple[UUID, ...] = (),
    ) -> RequirementsArtifact:
        """Append an immutable successor while retaining exact predecessor lineage."""
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        async with self._uow_factory(context) as uow:
            versions = await uow.requirements_artifacts.versions(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                artifact_id=artifact_id,
            )
            if not versions:
                raise InvalidPersistenceContextError("Requirements artifact is unavailable.")
            previous = versions[-1]
            if previous.artifact_id != artifact_id:
                raise InvalidPersistenceContextError("Requirements artifact identity is invalid.")
            lineage = tuple(dict.fromkeys((*previous.upstream_artifact_ids, *upstream_artifact_ids)))
            content = {
                "requirements": [item.model_dump(mode="json") for item in requirements],
                "state": state.value,
                "upstream_artifact_ids": [str(item) for item in lineage],
            }
            digest = sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            successor = previous.model_copy(update={
                "artifact_version": previous.artifact_version + 1,
                "predecessor_version": previous.artifact_version,
                "production_identity": production_identity,
                "production_digest": digest,
                "requirements": requirements,
                "state": state,
                "upstream_artifact_ids": lineage,
            })
            result = await uow.requirements_artifacts.persist_candidate(successor)
            await uow.commit()
            return result

    async def versions(self, *, context: TrustedPersistenceContext,
                       artifact_id: UUID) -> tuple[RequirementsArtifact, ...]:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Requirements artifact scope is invalid.")
        async with self._uow_factory(context) as uow:
            return await uow.requirements_artifacts.versions(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                artifact_id=artifact_id,
            )


def _deterministic_candidate(run: WorkflowRun) -> RequirementsArtifact:
    """Map the accepted first message to a minimal synthetic, unaccepted requirement."""
    request = run.client_request
    source_text = request.message.strip()
    partial = len(source_text.split()) < 3
    serialized = json.dumps(
        {"message": request.message, "accepted_sources": [ref.model_dump(mode="json") for ref in request.accepted_sources]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = sha256(serialized.encode("utf-8")).hexdigest()
    artifact = RequirementsArtifact(
        artifact_id=uuid5(NAMESPACE_URL, f"spine:requirements:{run.run_id}"),
        artifact_version=1,
        production_identity=PersistedRequirementsArtifactService._production_identity,
        production_digest=digest,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        work_item_id=run.work_item_id,
        run_id=run.run_id,
        requirements=(Requirement(
            requirement_id=uuid5(NAMESPACE_URL, f"spine:requirements:{run.run_id}:1"),
            category=(
                RequirementCategory.UNRESOLVED_INFORMATION
                if partial else RequirementCategory.FUNCTIONAL
            ),
            statement=source_text or "The request needs clarification.",
            state=RequirementState.PARTIAL if partial else RequirementState.CANDIDATE,
            uncertainty="The initial request needs more detail." if partial else None,
            ambiguous_markers=("request_intent",) if partial else (),
            missing_markers=("scope", "success_criteria") if partial else (),
            source_refs=request.accepted_sources,
        ),),
        state=RequirementState.PARTIAL if partial else RequirementState.CANDIDATE,
    )
    return _with_open_clarifications(artifact, run)


def _with_open_clarifications(
    artifact: RequirementsArtifact,
    run: WorkflowRun,
) -> RequirementsArtifact:
    """Attach stable, content-safe questions for unresolved candidate fields."""

    clarifications: list[Clarification] = []
    question_actor = ActorRef(
        kind=ActorKind.AGENT,
        id=uuid5(NAMESPACE_URL, "spine:agent:extract-requirements:v1"),
    )
    for requirement in artifact.requirements:
        if not requirement.missing_markers and not requirement.ambiguous_markers:
            continue
        reason = (
            ClarificationReason.MISSING_INFORMATION
            if requirement.missing_markers
            else ClarificationReason.AMBIGUOUS_INFORMATION
        )
        clarification_id = uuid5(
            NAMESPACE_URL,
            (
                "spine:clarification:v1:"
                f"{artifact.artifact_id}:{artifact.artifact_version}:{requirement.requirement_id}"
            ),
        )
        clarifications.append(
            Clarification(
                clarification_id=clarification_id,
                run_id=artifact.run_id,
                requirement_id=requirement.requirement_id,
                originating_artifact_id=artifact.artifact_id,
                originating_artifact_version=artifact.artifact_version,
                question=(
                    "Please provide the missing information required to continue."
                    if reason is ClarificationReason.MISSING_INFORMATION
                    else "Please resolve the ambiguous information required to continue."
                ),
                reason=reason,
                asked_by=question_actor,
                asked_event_id=uuid5(
                    NAMESPACE_URL, f"spine:clarification-asked:v1:{clarification_id}"
                ),
                asked_at=run.created_at,
                correlation_id=run.correlation_id,
            )
        )
        break
    if not clarifications:
        return artifact
    return artifact.model_copy(update={"clarifications": tuple(clarifications)})


def _next_open_clarification(
    *,
    artifact: RequirementsArtifact,
    asked_at: datetime,
    correlation_id: UUID,
    excluded_requirement_ids: tuple[UUID, ...] = (),
) -> Clarification | None:
    """Create the next sequential question against the exact successor version."""

    question_actor = ActorRef(
        kind=ActorKind.AGENT,
        id=uuid5(NAMESPACE_URL, "spine:agent:extract-requirements:v1"),
    )
    for requirement in artifact.requirements:
        if requirement.requirement_id in excluded_requirement_ids:
            continue
        if not requirement.missing_markers and not requirement.ambiguous_markers:
            continue
        reason = (
            ClarificationReason.MISSING_INFORMATION
            if requirement.missing_markers
            else ClarificationReason.AMBIGUOUS_INFORMATION
        )
        clarification_id = uuid5(
            NAMESPACE_URL,
            (
                "spine:clarification:v1:"
                f"{artifact.artifact_id}:{artifact.artifact_version}:{requirement.requirement_id}"
            ),
        )
        return Clarification(
            clarification_id=clarification_id,
            run_id=artifact.run_id,
            requirement_id=requirement.requirement_id,
            originating_artifact_id=artifact.artifact_id,
            originating_artifact_version=artifact.artifact_version,
            question=(
                "Please provide the missing information required to continue."
                if reason is ClarificationReason.MISSING_INFORMATION
                else "Please resolve the ambiguous information required to continue."
            ),
            reason=reason,
            asked_by=question_actor,
            asked_event_id=uuid5(
                NAMESPACE_URL, f"spine:clarification-asked:v1:{clarification_id}"
            ),
            asked_at=asked_at,
            correlation_id=correlation_id,
        )
    return None


async def _enter_clarification_wait(uow: object, artifact: RequirementsArtifact) -> None:
    if not any(item.answer is None for item in artifact.clarifications):
        return
    artifacts = getattr(uow, "requirements_artifacts")
    latest = await artifacts.get_for_run(
        workspace_id=artifact.workspace_id,
        environment_id=artifact.environment_id,
        run_id=artifact.run_id,
    )
    if latest is None or (
        latest.artifact_id,
        latest.artifact_version,
    ) != (artifact.artifact_id, artifact.artifact_version):
        return
    await getattr(uow, "workflow_runs").wait_for_clarification(artifact)


def _successor_digest(
    *,
    requirements: tuple[Requirement, ...],
    clarifications: tuple[Clarification, ...],
    state: RequirementState,
    command_id: UUID,
) -> str:
    payload = {
        "command_id": str(command_id),
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "clarifications": [item.model_dump(mode="json") for item in clarifications],
        "state": state.value,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
