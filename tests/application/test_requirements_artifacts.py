from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope, PersistenceOperation, PersistencePurpose,
    TrustedContextProvenance, TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.requirements_artifacts import PersistedRequirementsArtifactService
from spine.domain.common import ActorKind, ActorRef, ContextOrigin
from spine.domain.workflows import ClientRequestV1, Requirement, RequirementState, WorkflowRun
from spine.domain.workflows.runs import WorkflowRunState


def _context(workspace: int = 1, environment: int = 2) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(UUID(int=workspace), UUID(int=environment)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow_run_start"),
        trace_id=UUID(int=20),
        acting_subject_id=UUID(int=21), service_principal_id=None,
        provenance=TrustedContextProvenance(UUID(int=22), b"trusted"),
    )


def _run(message: str = "Deliver by Friday") -> WorkflowRun:
    return WorkflowRun(
        run_id=UUID(int=3), command_id=UUID(int=4), work_item_id=UUID(int=5),
        workspace_id=UUID(int=1), environment_id=UUID(int=2), workflow_key="proposal",
        workflow_version_id=UUID(int=6), workflow_version="1", state=WorkflowRunState.ADMITTED,
        run_version=1, started_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        client_request=ClientRequestV1(message=message), trace_id=UUID(int=7),
        correlation_id=UUID(int=8), causation_id=UUID(int=9),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class _Runs:
    def __init__(self, run: WorkflowRun | None = None) -> None:
        self.run = run or _run()

    async def get_run(self, **kwargs: object) -> WorkflowRun | None:
        return self.run if kwargs["run_id"] == self.run.run_id else None


class _Artifacts:
    def __init__(self) -> None:
        self.records = {}
        self.effects = 0

    async def persist_candidate(self, artifact):
        key = (artifact.workspace_id, artifact.environment_id, artifact.run_id,
               artifact.production_identity)
        current = self.records.get(key)
        if current is not None:
            if current.production_digest != artifact.production_digest:
                raise ValueError("conflicting replay")
            return current
        self.records[key] = artifact
        self.effects += 1
        return artifact

    async def versions(self, *, workspace_id, environment_id, artifact_id):
        return tuple(sorted((item for item in self.records.values()
                             if item.workspace_id == workspace_id and item.environment_id == environment_id
                             and item.artifact_id == artifact_id), key=lambda item: item.artifact_version))


class _Uow:
    def __init__(self, artifacts: _Artifacts, run: WorkflowRun | None = None) -> None:
        self.workflow_runs = _Runs(run)
        self.requirements_artifacts = artifacts
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def commit(self) -> None:
        self.commits += 1


def _factory(artifacts: _Artifacts, run: WorkflowRun | None = None):
    uow = _Uow(artifacts, run)
    return uow, lambda _: _Context(uow)


class _Context:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    async def __aenter__(self):
        return self.uow

    async def __aexit__(self, *args):
        return await self.uow.__aexit__(*args)


@pytest.mark.asyncio
async def test_replaying_candidate_production_returns_same_immutable_artifact() -> None:
    artifacts = _Artifacts()
    uow, factory = _factory(artifacts)
    service = PersistedRequirementsArtifactService(factory)

    first = await service.produce_first_candidate(context=_context(), run_id=UUID(int=3))
    replay = await service.produce_first_candidate(context=_context(), run_id=UUID(int=3))

    assert first == replay
    assert first is not None
    assert first.requirements[0].statement == "Deliver by Friday"
    assert first.requirements[0].state.value == "candidate"
    assert artifacts.effects == 1
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_candidate_production_rejects_run_outside_authorized_scope() -> None:
    artifacts = _Artifacts()
    _, factory = _factory(artifacts)
    service = PersistedRequirementsArtifactService(factory)

    with pytest.raises(InvalidPersistenceContextError):
        await service.produce_first_candidate(context=_context(77, 78), run_id=UUID(int=3))

    assert artifacts.effects == 0


@pytest.mark.asyncio
async def test_successor_preserves_prior_version_and_records_lineage() -> None:
    artifacts = _Artifacts()
    uow, factory = _factory(artifacts)
    service = PersistedRequirementsArtifactService(factory)
    first = await service.produce_first_candidate(context=_context(), run_id=UUID(int=3))
    assert first is not None

    successor = await service.persist_successor(
        context=_context(), artifact_id=first.artifact_id,
        production_identity="requirements-revision-v2",
        requirements=(Requirement(requirement_id=UUID(int=33), category="unresolved_information", statement="Friday at noon"),),
        state=RequirementState.PARTIAL,
        upstream_artifact_ids=(UUID(int=88),),
    )

    assert first.artifact_version == 1
    assert first.predecessor_version is None
    assert successor.artifact_version == 2
    assert successor.predecessor_version == 1
    assert successor.upstream_artifact_ids == (UUID(int=88),)
    assert len(await service.versions(context=_context(), artifact_id=first.artifact_id)) == 2
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_low_information_first_message_produces_partial_candidate() -> None:
    artifacts = _Artifacts()
    _, factory = _factory(artifacts, _run("Help"))
    service = PersistedRequirementsArtifactService(factory)

    artifact = await service.produce_first_candidate(context=_context(), run_id=UUID(int=3))

    assert artifact is not None
    assert artifact.state is RequirementState.PARTIAL
    assert artifact.requirements[0].missing_markers == ("scope", "success_criteria")
