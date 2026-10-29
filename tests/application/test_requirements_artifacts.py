from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope, PersistenceOperation, PersistencePurpose,
    TrustedContextProvenance, TrustedPersistenceContext,
)
from spine.application.persistence.errors import (
    InvalidPersistenceContextError,
    OptimisticConflictError,
)
from spine.application.requirements_artifacts import PersistedRequirementsArtifactService
from spine.application.workflow_runs import (
    AnswerClarificationCommandV1,
    WorkflowArtifactRef,
    WorkflowCommand,
)
from spine.domain.common import ActorKind, ActorRef, ContextOrigin
from spine.domain.capabilities.extract_requirements import (
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
)
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
        self.waits = 0

    async def get_run(self, **kwargs: object) -> WorkflowRun | None:
        return self.run if kwargs["run_id"] == self.run.run_id else None

    async def wait_for_clarification(self, artifact) -> WorkflowRun:
        if self.run.state is not WorkflowRunState.WAITING:
            self.run = self.run.model_copy(
                update={"state": WorkflowRunState.WAITING, "run_version": self.run.run_version + 1}
            )
            self.waits += 1
        return self.run


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

    async def get_for_run(self, *, workspace_id, environment_id, run_id):
        matches = [item for item in self.records.values()
                   if item.workspace_id == workspace_id and item.environment_id == environment_id
                   and item.run_id == run_id]
        return max(matches, key=lambda item: item.artifact_version) if matches else None


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
    uow, factory = _factory(artifacts, _run("Help"))
    service = PersistedRequirementsArtifactService(factory)

    artifact = await service.produce_first_candidate(context=_context(), run_id=UUID(int=3))

    assert artifact is not None
    assert artifact.state is RequirementState.PARTIAL
    assert artifact.requirements[0].missing_markers == ("scope", "success_criteria")
    assert len(artifact.clarifications) == 1
    clarification = artifact.clarifications[0]
    assert clarification.run_id == artifact.run_id
    assert clarification.requirement_id == artifact.requirements[0].requirement_id
    assert clarification.originating_artifact_version == artifact.artifact_version
    assert clarification.answer is None
    assert "Help" not in clarification.question
    assert uow.workflow_runs.run.state is WorkflowRunState.WAITING
    assert uow.workflow_runs.run.run_version == 2
    assert uow.workflow_runs.waits == 1


@pytest.mark.asyncio
async def test_clarification_answer_prepares_one_immutable_affected_successor() -> None:
    artifacts = _Artifacts()
    _, factory = _factory(artifacts, _run("Help"))
    class Revalidator:
        def __init__(self) -> None:
            self.calls = []

        async def revalidate(self, *, previous, requirement, clarification, answer):
            self.calls.append((requirement.requirement_id, clarification.clarification_id, answer.text))
            return RequirementsExtractionOutput(
                workspace_id=previous.workspace_id,
                environment_id=previous.environment_id,
                workflow_run_id=previous.run_id,
                work_item_id=previous.work_item_id,
                outcome=RequirementsExtractionOutcome.SUCCESS,
                requirements=(requirement.model_copy(update={
                    "statement": answer.text,
                    "missing_markers": (),
                    "ambiguous_markers": (),
                    "uncertainty": None,
                    "state": RequirementState.CANDIDATE,
                }),),
            )

    revalidator = Revalidator()
    service = PersistedRequirementsArtifactService(
        factory, clarification_revalidator=revalidator
    )
    first = await service.produce_first_candidate(context=_context(), run_id=UUID(int=3))
    assert first is not None
    clarification = first.clarifications[0]
    command = WorkflowCommand(
        command_id=UUID(int=643),
        idempotency_key="clarification-643",
        run_id=first.run_id,
        workspace_id=first.workspace_id,
        environment_id=first.environment_id,
        actor=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        command_type="answer_clarification",
        command_schema_version=1,
        expected_run_version=2,
        payload=AnswerClarificationCommandV1(
            clarification_id=clarification.clarification_id,
            requirement_id=clarification.requirement_id,
            answer="The scope is a customer portal delivered by December 20.",
        ),
        artifact_refs=(WorkflowArtifactRef(
            artifact_id=first.artifact_id,
            schema_name="requirements",
            schema_version=first.schema_version,
            artifact_version=first.artifact_version,
        ),),
        trace_id=UUID(int=20),
        correlation_id=UUID(int=644),
        causation_id=UUID(int=645),
    )

    successor = await service.prepare_clarification_answer(
        context=_context(), command=command
    )

    assert first.clarifications[0].answer is None
    assert successor.artifact_version == 2
    assert successor.predecessor_version == 1
    assert successor.clarifications[0].answer is not None
    assert successor.clarifications[0].answer.text.startswith("The scope")
    assert successor.requirements[0].statement == command.payload.answer
    assert successor.requirements[0].missing_markers == ()
    assert successor.requirements[0].ambiguous_markers == ()
    assert revalidator.calls == [(
        clarification.requirement_id,
        clarification.clarification_id,
        command.payload.answer,
    )]

    await artifacts.persist_candidate(successor)
    conflicting = command.model_copy(update={
        "command_id": UUID(int=646),
        "idempotency_key": "clarification-643-stale",
        "payload": command.payload.model_copy(update={"answer": "A different deadline."}),
    })
    with pytest.raises(OptimisticConflictError, match="artifact version is stale"):
        await service.prepare_clarification_answer(
            context=_context(), command=conflicting
        )


@pytest.mark.asyncio
async def test_clarifications_are_admitted_sequentially_on_the_same_run() -> None:
    artifacts = _Artifacts()
    _, factory = _factory(artifacts)
    service = PersistedRequirementsArtifactService(factory)
    extraction = RequirementsExtractionOutput(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        workflow_run_id=UUID(int=3),
        work_item_id=UUID(int=5),
        outcome=RequirementsExtractionOutcome.PARTIAL,
        requirements=(
            Requirement(
                requirement_id=UUID(int=31),
                category="functional",
                statement="Portal scope",
                missing_markers=("scope",),
                state=RequirementState.PARTIAL,
            ),
            Requirement(
                requirement_id=UUID(int=32),
                category="constraint",
                statement="Delivery date",
                ambiguous_markers=("date",),
                state=RequirementState.PARTIAL,
            ),
        ),
    )
    first = await service.persist_extracted_candidate(
        context=_context(), run_id=UUID(int=3), extraction=extraction
    )
    assert first is not None
    assert len(first.clarifications) == 1
    current = first.clarifications[0]
    command = WorkflowCommand(
        command_id=UUID(int=650),
        idempotency_key="clarification-sequential",
        run_id=first.run_id,
        workspace_id=first.workspace_id,
        environment_id=first.environment_id,
        actor=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        command_type="answer_clarification",
        command_schema_version=1,
        expected_run_version=2,
        payload=AnswerClarificationCommandV1(
            clarification_id=current.clarification_id,
            requirement_id=current.requirement_id,
            answer="The portal covers account management.",
        ),
        artifact_refs=(WorkflowArtifactRef(
            artifact_id=first.artifact_id,
            schema_name="requirements",
            schema_version=first.schema_version,
            artifact_version=first.artifact_version,
        ),),
        trace_id=UUID(int=20),
        correlation_id=UUID(int=651),
        causation_id=UUID(int=652),
    )

    successor = await service.prepare_clarification_answer(
        context=_context(), command=command
    )

    open_items = tuple(item for item in successor.clarifications if item.answer is None)
    assert len(open_items) == 1
    assert open_items[0].requirement_id == UUID(int=32)
    assert open_items[0].originating_artifact_version == 2
    assert successor.requirements[0].constraints[-1] == command.payload.answer

    await artifacts.persist_candidate(successor)
    next_item = open_items[0]
    next_command = command.model_copy(update={
        "command_id": UUID(int=653),
        "idempotency_key": "clarification-sequential-next",
        "expected_run_version": 4,
        "payload": AnswerClarificationCommandV1(
            clarification_id=next_item.clarification_id,
            requirement_id=next_item.requirement_id,
            answer="The delivery date is December 20.",
        ),
        "artifact_refs": (WorkflowArtifactRef(
            artifact_id=successor.artifact_id,
            schema_name="requirements",
            schema_version=successor.schema_version,
            artifact_version=successor.artifact_version,
        ),),
    })

    final = await service.prepare_clarification_answer(
        context=_context(), command=next_command
    )

    assert final.artifact_version == 3
    assert all(item.answer is not None for item in final.clarifications)
    assert final.clarifications[0].resulting_artifact_version == 2
    assert final.clarifications[1].resulting_artifact_version == 3
