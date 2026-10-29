from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.requirements_review import RequirementsReviewService
from spine.application.requirements_review import RequirementsArtifactStaleError
from spine.application.workflow_runs import (
    EditRequirementCommandV1,
    RequestRequirementsReworkCommandV1,
    SaveRequirementsReviewCommandV1,
    WorkflowArtifactRef,
    WorkflowCommand,
)
from spine.domain.common import ActorKind, ActorRef, ContextOrigin
from spine.domain.workflows import (
    Requirement,
    RequirementReviewState,
    RequirementsArtifact,
    WorkflowCommandReceipt,
    WorkflowRunState,
)


def _context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(UUID(int=1), UUID(int=2)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow.run.command"),
        trace_id=UUID(int=30),
        acting_subject_id=UUID(int=21),
        service_principal_id=None,
        provenance=TrustedContextProvenance(UUID(int=31), b"trusted"),
    )


def _artifact() -> RequirementsArtifact:
    return RequirementsArtifact(
        artifact_id=UUID(int=40),
        artifact_version=1,
        production_identity="requirements-extraction-v1",
        production_digest="a" * 64,
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        work_item_id=UUID(int=5),
        run_id=UUID(int=3),
        requirements=(
            Requirement(
                requirement_id=UUID(int=41),
                category="functional",
                statement="Deliver on Friday",
            ),
            Requirement(
                requirement_id=UUID(int=42),
                category="constraint",
                statement="Use the approved template",
            ),
        ),
    )


def _command() -> WorkflowCommand:
    return WorkflowCommand(
        command_id=UUID(int=50),
        idempotency_key="edit-50",
        run_id=UUID(int=3),
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        actor=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        command_type="edit_requirement",
        command_schema_version=1,
        expected_run_version=1,
        payload=EditRequirementCommandV1(
            requirement_id=UUID(int=41),
            statement="Deliver on Friday by 12:00",
            reason="Manager clarified the deadline.",
        ),
        artifact_refs=(
            WorkflowArtifactRef(
                artifact_id=UUID(int=40),
                schema_name="requirements",
                schema_version="requirements:v1",
                artifact_version=1,
            ),
        ),
        trace_id=UUID(int=30),
        correlation_id=UUID(int=51),
        causation_id=UUID(int=52),
    )


class _Runs:
    def __init__(self) -> None:
        self.admission = "accepted"

    async def get_run(self, **kwargs):
        return object() if kwargs["run_id"] == UUID(int=3) else None

    async def dispatch(self, command):
        return WorkflowCommandReceipt(
            command_id=command.command_id,
            work_item_id=UUID(int=5),
            run_id=command.run_id,
            workflow_version_id=UUID(int=6),
            state=WorkflowRunState.WAITING,
            run_version=2,
            admission=self.admission,
        )


class _Artifacts:
    def __init__(self) -> None:
        self.items = [_artifact()]

    async def get(self, *, workspace_id, environment_id, artifact_id, artifact_version=None):
        return next(
            (
                item
                for item in self.items
                if item.workspace_id == workspace_id
                and item.environment_id == environment_id
                and item.artifact_id == artifact_id
                and (artifact_version is None or item.artifact_version == artifact_version)
            ),
            None,
        )

    async def versions(self, *, workspace_id, environment_id, artifact_id):
        return tuple(
            item
            for item in self.items
            if item.workspace_id == workspace_id
            and item.environment_id == environment_id
            and item.artifact_id == artifact_id
        )

    async def persist_candidate(self, artifact):
        self.items.append(artifact)
        return artifact


class _Uow:
    def __init__(self) -> None:
        self.workflow_runs = _Runs()
        self.requirements_artifacts = _Artifacts()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _Context:
    def __init__(self, uow) -> None:
        self.uow = uow

    async def __aenter__(self):
        return self.uow

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
async def test_exact_version_edit_creates_one_reviewed_successor_with_receipt_lineage() -> None:
    uow = _Uow()
    service = RequirementsReviewService(lambda _: _Context(uow))

    result = await service.dispatch(context=_context(), command=_command())

    assert result is not None
    assert result.action == "edit_requirement"
    assert result.actor == _command().actor
    assert result.reason == "Manager clarified the deadline."
    assert result.changed_requirement_id == UUID(int=41)
    assert result.base_artifact_ref.artifact_version == 1
    assert result.resulting_artifact_ref.artifact_version == 2
    assert len(uow.requirements_artifacts.items) == 2
    successor = uow.requirements_artifacts.items[-1]
    assert successor.predecessor_version == 1
    assert successor.requirements[0].statement == "Deliver on Friday by 12:00"
    assert successor.requirements[0].review_state is RequirementReviewState.REVIEWED
    assert successor.requirements[1] == _artifact().requirements[1]
    assert uow.commits == 1


def _review_command(*, command_type: str, payload, command_id: int = 60) -> WorkflowCommand:
    command = _command()
    return command.model_copy(
        update={
            "command_id": UUID(int=command_id),
            "idempotency_key": f"review-{command_id}",
            "command_type": command_type,
            "payload": payload,
        }
    )


@pytest.mark.asyncio
async def test_save_review_marks_every_requirement_reviewed_in_an_immutable_successor() -> None:
    uow = _Uow()
    service = RequirementsReviewService(lambda _: _Context(uow))
    command = _review_command(
        command_type="save_review",
        payload=SaveRequirementsReviewCommandV1(reason="Manager completed the review."),
    )

    result = await service.dispatch(context=_context(), command=command)

    assert result is not None
    assert result.action == "save_review"
    assert result.changed_requirement_id is None
    assert all(
        item.review_state is RequirementReviewState.REVIEWED
        for item in uow.requirements_artifacts.items[-1].requirements
    )


@pytest.mark.asyncio
async def test_rework_preserves_candidate_and_lineage_while_recording_typed_reason() -> None:
    uow = _Uow()
    base = uow.requirements_artifacts.items[0]
    uow.requirements_artifacts.items[0] = base.model_copy(
        update={"upstream_artifact_ids": (UUID(int=70),)}
    )
    service = RequirementsReviewService(lambda _: _Context(uow))
    command = _review_command(
        command_type="request_rework",
        payload=RequestRequirementsReworkCommandV1(
            reason="The delivery constraint needs source validation."
        ),
    )

    result = await service.dispatch(context=_context(), command=command)

    assert result is not None
    assert result.action == "request_rework"
    successor = uow.requirements_artifacts.items[-1]
    assert successor.upstream_artifact_ids == (UUID(int=70),)
    assert successor.requirements[0].statement == "Deliver on Friday"
    assert all(
        item.review_state is RequirementReviewState.CHANGES_REQUESTED
        for item in successor.requirements
    )


@pytest.mark.asyncio
async def test_stale_artifact_edit_returns_current_ref_without_appending_a_version() -> None:
    uow = _Uow()
    base = uow.requirements_artifacts.items[0]
    uow.requirements_artifacts.items.append(
        base.model_copy(
            update={
                "artifact_version": 2,
                "predecessor_version": 1,
                "production_identity": "other-review",
                "production_digest": "b" * 64,
            }
        )
    )
    service = RequirementsReviewService(lambda _: _Context(uow))

    with pytest.raises(RequirementsArtifactStaleError) as raised:
        await service.dispatch(context=_context(), command=_command())

    assert raised.value.current_ref.artifact_version == 2
    assert len(uow.requirements_artifacts.items) == 2
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_exact_replay_returns_original_receipt_without_duplicate_successor() -> None:
    uow = _Uow()
    service = RequirementsReviewService(lambda _: _Context(uow))

    first = await service.dispatch(context=_context(), command=_command())
    uow.workflow_runs.admission = "replayed"
    replay = await service.dispatch(context=_context(), command=_command())

    assert first is not None and replay is not None
    assert replay.command_receipt.admission == "replayed"
    assert replay.resulting_artifact_ref == first.resulting_artifact_ref
    assert len(uow.requirements_artifacts.items) == 2
