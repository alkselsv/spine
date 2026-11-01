from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.requirements_review import (
    RequirementsArtifactStaleError,
    RequirementsReviewService,
    RequirementsReviewUnavailableError,
)
from spine.application.workflow_runs import (
    EditRequirementCommandV1,
    ReclassifyRequirementCommandV1,
    RequestRequirementsReworkCommandV1,
    SaveRequirementsReviewCommandV1,
    WorkflowArtifactRef,
    WorkflowCommand,
)
from spine.domain.common import ActorKind, ActorRef, ContextOrigin
from spine.domain.artifacts import ArtifactRef, PhysicalLineLocator
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    Requirement,
    RequirementEvidenceRef,
    RequirementEvidenceStatus,
    RequirementEvidenceValidation,
    RequirementAcceptanceBasis,
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
        if (
            kwargs["workspace_id"] != UUID(int=1)
            or kwargs["environment_id"] != UUID(int=2)
            or kwargs["run_id"] != UUID(int=3)
        ):
            return None
        return SimpleNamespace(
            workflow_key="commercial.requirements",
            run_id=UUID(int=3),
            work_item_id=UUID(int=5),
        )

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
        self.fail_next_persist = False

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
        if self.fail_next_persist:
            self.fail_next_persist = False
            raise RuntimeError("simulated persistence failure")
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
    assert uow.requirements_artifacts.items[-1].state.value == "human_reviewed"


@pytest.mark.asyncio
async def test_edit_invalidates_prior_evidence_validation_for_the_new_statement() -> None:
    uow = _Uow()
    source_ref = AcceptedSourceRevisionRef(
        source_object_id=UUID(int=80),
        source_revision_id=UUID(int=81),
        provenance_id=UUID(int=82),
    )
    evidence = RequirementEvidenceRef(
        source_ref=source_ref,
        parsed_artifact_ref=ArtifactRef(
            id=UUID(int=83),
            schema_name="parsed-document",
            schema_version="parsed-document:v1",
        ),
        segment_id=UUID(int=84),
        segment_digest="c" * 64,
        locator=PhysicalLineLocator(
            kind="physical_line", line_number=1, start_codepoint=0, end_codepoint=6
        ),
        validation=RequirementEvidenceValidation(
            validation_reference=UUID(int=85),
            status="valid",
            authorization_decision_reference=UUID(int=86),
            validated_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
        ),
    )
    base = uow.requirements_artifacts.items[0]
    changed = base.requirements[0].model_copy(
        update={
            "source_refs": (source_ref,),
            "evidence_refs": (evidence,),
            "evidence_status": RequirementEvidenceStatus.REFERENCED,
        }
    )
    uow.requirements_artifacts.items[0] = base.model_copy(
        update={"requirements": (changed, base.requirements[1])}
    )

    await RequirementsReviewService(lambda _: _Context(uow)).dispatch(
        context=_context(), command=_command()
    )

    edited = uow.requirements_artifacts.items[-1].requirements[0]
    assert edited.evidence_refs[0].segment_id == UUID(int=84)
    assert edited.evidence_refs[0].validation is None
    assert edited.evidence_status is RequirementEvidenceStatus.UNVERIFIED


def _source_backed_uow() -> _Uow:
    uow = _Uow()
    source_ref = AcceptedSourceRevisionRef(
        source_object_id=UUID(int=80),
        source_revision_id=UUID(int=81),
        provenance_id=UUID(int=82),
    )
    base = uow.requirements_artifacts.items[0]
    uow.requirements_artifacts.items[0] = base.model_copy(
        update={
            "requirements": (
                base.requirements[0].model_copy(update={"source_refs": (source_ref,)}),
                base.requirements[1],
            )
        }
    )
    return uow


@pytest.mark.asyncio
async def test_explicit_manager_reclassification_creates_source_free_v3_successor() -> None:
    uow = _source_backed_uow()
    command = _review_command(
        command_type="reclassify_requirement",
        payload=ReclassifyRequirementCommandV1(
            requirement_id=UUID(int=41),
            statement="The account owner commits completion before the weekend.",
            acceptance_basis="manager_assertion",
            rationale="I own the delivery commitment.",
            independence_attestation="authored_from_independent_knowledge_without_protected_text",
        ),
    )

    receipt = await RequirementsReviewService(lambda _: _Context(uow)).dispatch(
        context=_context(), command=command
    )

    assert receipt is not None
    assert receipt.action == "reclassify_requirement"
    successor = uow.requirements_artifacts.items[-1]
    assert successor.schema_version == "requirements:v3"
    assert successor.predecessor_version == 1
    reclassified = successor.requirements[0]
    expected_requirement_id = uuid5(
        command.command_id,
        f"spine.requirement-reclassification:v1:{UUID(int=41)}",
    )
    assert receipt.changed_requirement_id == expected_requirement_id
    assert reclassified.requirement_id == expected_requirement_id
    assert reclassified.acceptance_basis is RequirementAcceptanceBasis.MANAGER_ASSERTION
    assert reclassified.source_refs == ()
    assert reclassified.evidence_refs == ()
    assert reclassified.attributed_by == command.actor
    assert reclassified.assertion_audit_reference == command.command_id
    assert reclassified.upstream_artifact_ids == (UUID(int=40),)
    assert uow.requirements_artifacts.items[0].requirements[0].source_refs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    ["  DELIVER   on Friday!!! ", "Please deliver on Friday at noon."],
)
async def test_reclassification_rejects_protected_source_text_variants(
    candidate: str,
) -> None:
    uow = _source_backed_uow()
    command = _review_command(
        command_type="reclassify_requirement",
        payload=ReclassifyRequirementCommandV1(
            requirement_id=UUID(int=41),
            statement=candidate,
            acceptance_basis="manager_assertion",
            rationale="Treat as my assertion.",
            independence_attestation="authored_from_independent_knowledge_without_protected_text",
        ),
    )

    with pytest.raises(
        RequirementsReviewUnavailableError,
        match="independently supplied text",
    ):
        await RequirementsReviewService(lambda _: _Context(uow)).dispatch(
            context=_context(), command=command
        )

    assert len(uow.requirements_artifacts.items) == 1


@pytest.mark.asyncio
async def test_reclassification_requires_explicit_resolution_for_conflicting_evidence() -> None:
    uow = _source_backed_uow()
    base = uow.requirements_artifacts.items[0]
    source_ref = base.requirements[0].source_refs[0]
    conflicting = RequirementEvidenceRef(
        source_ref=source_ref,
        parsed_artifact_ref=ArtifactRef(
            id=UUID(int=83),
            schema_name="parsed-document",
            schema_version="parsed-document:v1",
        ),
        segment_id=UUID(int=84),
        segment_digest="c" * 64,
        locator=PhysicalLineLocator(
            kind="physical_line", line_number=1, start_codepoint=0, end_codepoint=6
        ),
        relationship="contradicts",
    )
    uow.requirements_artifacts.items[0] = base.model_copy(
        update={
            "requirements": (
                base.requirements[0].model_copy(
                    update={"evidence_refs": (conflicting,)}
                ),
                base.requirements[1],
            )
        }
    )
    command = _review_command(
        command_type="reclassify_requirement",
        payload=ReclassifyRequirementCommandV1(
            requirement_id=UUID(int=41),
            statement="Our independently owned commitment is Friday noon.",
            acceptance_basis="manager_assertion",
            rationale="I own the commitment.",
            independence_attestation="authored_from_independent_knowledge_without_protected_text",
        ),
    )

    with pytest.raises(
        RequirementsReviewUnavailableError,
        match="explicit resolution",
    ):
        await RequirementsReviewService(lambda _: _Context(uow)).dispatch(
            context=_context(), command=command
        )

    assert len(uow.requirements_artifacts.items) == 1


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


@pytest.mark.asyncio
async def test_cross_workspace_review_returns_not_found_without_protected_state() -> None:
    uow = _Uow()
    other_context = replace(
        _context(), scope=EnvironmentScope(UUID(int=99), UUID(int=2))
    )

    result = await RequirementsReviewService(lambda _: _Context(uow)).dispatch(
        context=other_context,
        command=_command().model_copy(update={"workspace_id": UUID(int=99)}),
    )

    assert result is None
    assert len(uow.requirements_artifacts.items) == 1
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_artifact_from_another_run_is_rejected_without_a_successor() -> None:
    uow = _Uow()
    base = uow.requirements_artifacts.items[0]
    uow.requirements_artifacts.items[0] = base.model_copy(
        update={"run_id": UUID(int=90)}
    )

    with pytest.raises(RequirementsReviewUnavailableError, match="workflow run"):
        await RequirementsReviewService(lambda _: _Context(uow)).dispatch(
            context=_context(), command=_command()
        )

    assert len(uow.requirements_artifacts.items) == 1
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_retry_after_partial_failure_creates_one_logical_successor() -> None:
    uow = _Uow()
    uow.requirements_artifacts.fail_next_persist = True
    service = RequirementsReviewService(lambda _: _Context(uow))

    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        await service.dispatch(context=_context(), command=_command())

    result = await service.dispatch(context=_context(), command=_command())

    assert result is not None
    assert result.resulting_artifact_ref.artifact_version == 2
    assert len(uow.requirements_artifacts.items) == 2
    assert uow.commits == 1
