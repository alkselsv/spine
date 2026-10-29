"""Use case boundary for immutable conversational requirements artifacts."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Protocol
from uuid import UUID, uuid5, NAMESPACE_URL

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.workflows import (
    Requirement,
    RequirementCategory,
    RequirementState,
    WorkflowRun,
)
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


class PersistedRequirementsArtifactService:
    """Create a deterministic synthetic first candidate and serve scoped readback."""

    _production_identity = "requirements-extraction-v1"

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

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
            await uow.commit()
            return persisted

    async def persist_extracted_candidate(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        extraction: RequirementsExtractionOutput,
        upstream_artifact_ids: tuple[UUID, ...] = (),
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
            persisted = await uow.requirements_artifacts.persist_candidate(artifact)
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
    return RequirementsArtifact(
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
