from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.requirements_acceptance import (
    CurrentRequirementsAcceptanceEvidenceValidator,
    REQUIREMENTS_ACCEPTANCE_POLICY_VERSION,
    RequirementsAcceptanceAuthority,
    RequirementsAcceptanceService,
    RequirementsAcceptanceDeniedError,
)
from spine.application.transcript_inspection import (
    TranscriptInspectionStatus,
)
from spine.application.requirements_review import RequirementsArtifactStaleError
from spine.application.workflow_runs import (
    AcceptRequirementsCommandV1,
    WorkflowArtifactRef,
    WorkflowCommand,
)
from spine.domain.artifacts import ArtifactRef, PhysicalLineLocator, TimeRangeLocator
from spine.domain.common import ActorKind, ActorRef, ContextOrigin
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    Requirement,
    RequirementV3,
    RequirementAcceptanceBasis,
    RequirementEvidenceRef,
    RequirementEvidenceRelationship,
    RequirementEvidenceStatus,
    RequirementEvidenceValidation,
    RequirementReviewState,
    RequirementState,
    RequirementsArtifact,
    WorkflowCommandReceipt,
    WorkflowRunState,
)


def _context(
    workspace_id: UUID = UUID(int=1),
    acting_subject_id: UUID = UUID(int=21),
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id, UUID(int=2)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow.run.command"),
        trace_id=UUID(int=30),
        acting_subject_id=acting_subject_id,
        service_principal_id=None,
        provenance=TrustedContextProvenance(UUID(int=31), b"trusted"),
    )


def _artifact() -> RequirementsArtifact:
    source = AcceptedSourceRevisionRef(
        source_object_id=UUID(int=80),
        source_revision_id=UUID(int=81),
        provenance_id=UUID(int=82),
    )
    evidence = RequirementEvidenceRef(
        source_ref=source,
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
    )
    return RequirementsArtifact(
        artifact_id=UUID(int=40),
        artifact_version=1,
        production_identity="requirements-review-v1",
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
                state=RequirementState.HUMAN_REVIEWED,
                review_state=RequirementReviewState.REVIEWED,
                source_refs=(source,),
                evidence_refs=(evidence,),
                evidence_status=RequirementEvidenceStatus.UNVERIFIED,
            ),
        ),
        state=RequirementState.HUMAN_REVIEWED,
    )


def _command() -> WorkflowCommand:
    return WorkflowCommand(
        command_id=UUID(int=50),
        idempotency_key="accept-50",
        run_id=UUID(int=3),
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        actor=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        command_type="accept_requirements",
        command_schema_version=1,
        expected_run_version=1,
        payload=AcceptRequirementsCommandV1(
            artifact_digest="a" * 64,
            policy_version=REQUIREMENTS_ACCEPTANCE_POLICY_VERSION,
            acknowledged_assertion_count=0,
            acknowledged_assumption_count=0,
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


def _authority() -> RequirementsAcceptanceAuthority:
    return RequirementsAcceptanceAuthority(
        actor_id=UUID(int=21),
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        permission="requirements_artifact.accept",
        policy_version=REQUIREMENTS_ACCEPTANCE_POLICY_VERSION,
        authentication_configuration_version="test-auth:v1",
        authorization_generation=7,
        allowed=True,
    )


class _Evidence:
    async def validate_artifact(self, *, context, requester, artifact):
        assert requester == ParsedContentInspectionRequester(subject_id=UUID(int=21))
        evidence = artifact.requirements[0].evidence_refs[0].model_copy(
            update={
                "validation": RequirementEvidenceValidation(
                    validation_reference=UUID(int=70),
                    status="valid",
                    authorization_decision_reference=UUID(int=71),
                    validated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                )
            }
        )
        requirement = artifact.requirements[0].model_copy(
            update={
                "evidence_refs": (evidence,),
                "evidence_status": RequirementEvidenceStatus.REFERENCED,
            }
        )
        return artifact.model_copy(
            update={"requirements": (requirement, *artifact.requirements[1:])}
        )


class _ParsedPassThrough:
    async def validate_artifact(self, *, context, requester, artifact):
        return artifact


class _TranscriptInspection:
    def __init__(self, status: TranscriptInspectionStatus) -> None:
        self.status = status

    async def inspect(self, **kwargs):
        return SimpleNamespace(
            status=self.status,
            authorization_decision_reference=UUID(int=78),
            audit_reference=UUID(int=79),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inspection_status", "validation_status"),
    [
        (TranscriptInspectionStatus.CURRENT, "valid"),
        (TranscriptInspectionStatus.PERMISSION_DENIED, "denied"),
        (TranscriptInspectionStatus.STALE, "stale"),
        (TranscriptInspectionStatus.SUPERSEDED, "stale"),
        (TranscriptInspectionStatus.DEGRADED, "degraded"),
    ],
)
async def test_transcript_evidence_is_revalidated_through_current_inspection_gate(
    inspection_status: TranscriptInspectionStatus,
    validation_status: str,
) -> None:
    artifact = _artifact()
    source = artifact.requirements[0].source_refs[0]
    locator = TimeRangeLocator(
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        source_revision_digest="e" * 64,
        transcript_artifact_id=UUID(int=85),
        transcript_artifact_digest="f" * 64,
        transcript_artifact_schema_version="transcription-artifact:v1",
        start_ms=0,
        end_ms=1_000,
        segment_ids=(UUID(int=84),),
    )
    transcript_evidence = RequirementEvidenceRef(
        source_ref=source,
        segment_id=UUID(int=84),
        segment_digest="c" * 64,
        locator_contract_version="time-range-locator:v1",
        transcript_locator=locator,
    )
    requirement = artifact.requirements[0].model_copy(
        update={"evidence_refs": (transcript_evidence,)}
    )
    artifact = artifact.model_copy(update={"requirements": (requirement,)})

    validated = await CurrentRequirementsAcceptanceEvidenceValidator(
        parsed=_ParsedPassThrough(),
        transcripts=_TranscriptInspection(inspection_status),
    ).validate_artifact(
        context=_context(),
        requester=ParsedContentInspectionRequester(
            subject_id=UUID(int=21), authorization_generation=7
        ),
        artifact=artifact,
    )

    validation = validated.requirements[0].evidence_refs[0].validation
    assert validation is not None
    assert validation.status == validation_status
    assert (validation.authorization_decision_reference is not None) is (
        validation_status == "valid"
    )


class _AcceptanceDecisions:
    def __init__(self) -> None:
        self.items = {}

    async def get(self, *, command_id):
        return self.items.get(command_id)

    async def create(self, receipt):
        self.items[receipt.command_receipt.command_id] = receipt
        return receipt


class _Runs:
    def __init__(self) -> None:
        self.admission = "accepted"

    async def get_run(self, **kwargs):
        if kwargs != {
            "workspace_id": UUID(int=1),
            "environment_id": UUID(int=2),
            "run_id": UUID(int=3),
        }:
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
            state=WorkflowRunState.COMPLETED,
            run_version=2,
            admission=self.admission,
        )


class _Artifacts:
    def __init__(self) -> None:
        self.items = [_artifact()]

    async def versions(self, **kwargs):
        return tuple(
            item
            for item in self.items
            if item.workspace_id == kwargs["workspace_id"]
            and item.environment_id == kwargs["environment_id"]
            and item.artifact_id == kwargs["artifact_id"]
        )


class _Uow:
    def __init__(self) -> None:
        self.workflow_runs = _Runs()
        self.requirements_artifacts = _Artifacts()
        self.requirements_acceptances = _AcceptanceDecisions()
        self.commits = 0

    async def commit(self):
        self.commits += 1


class _Context:
    def __init__(self, uow):
        self.uow = uow

    async def __aenter__(self):
        return self.uow

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
async def test_authorized_exact_version_acceptance_records_one_terminal_receipt() -> None:
    uow = _Uow()
    service = RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_Evidence()
    )

    receipt = await service.dispatch(
        context=_context(),
        command=_command(),
        authority=_authority(),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )

    assert receipt is not None
    assert receipt.command_receipt.state is WorkflowRunState.COMPLETED
    assert receipt.accepted_artifact_ref == _command().artifact_refs[0]
    assert receipt.accepted_artifact_digest == "a" * 64
    assert receipt.actor == _command().actor
    assert receipt.policy_version == REQUIREMENTS_ACCEPTANCE_POLICY_VERSION
    assert receipt.authentication_configuration_version == "test-auth:v1"
    assert receipt.authorization_generation == 7
    assert receipt.evidence_validation_references == (UUID(int=70),)
    assert receipt.correlation_id == UUID(int=51)
    assert len(uow.requirements_acceptances.items) == 1
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_mixed_valid_assertion_and_assumption_artifact_is_accepted_atomically() -> None:
    uow = _Uow()
    base = uow.requirements_artifacts.items[0]
    assertion = RequirementV3(
        requirement_id=UUID(int=42),
        category="constraint",
        statement="Use the client's internal naming convention",
        state=RequirementState.HUMAN_REVIEWED,
        review_state=RequirementReviewState.REVIEWED,
        acceptance_basis=RequirementAcceptanceBasis.MANAGER_ASSERTION,
        acceptance_rationale="The account manager confirmed this directly.",
        attributed_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        assertion_audit_reference=UUID(int=72),
        independent_human_assertion=True,
    )
    assumption = RequirementV3(
        requirement_id=UUID(int=43),
        category="assumption",
        statement="The customer will provide final copy before design freeze",
        state=RequirementState.HUMAN_REVIEWED,
        review_state=RequirementReviewState.REVIEWED,
        acceptance_basis=RequirementAcceptanceBasis.EXPLICIT_ASSUMPTION,
        acceptance_rationale="The schedule depends on customer copy.",
        false_consequence="The delivery date must move.",
        attributed_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
    )
    uow.requirements_artifacts.items[0] = base.model_copy(
        update={
            "schema_version": "requirements:v3",
            "requirements": (*base.requirements, assertion, assumption),
        }
    )
    command = _command().model_copy(
        update={
            "artifact_refs": (
                _command().artifact_refs[0].model_copy(
                    update={"schema_version": "requirements:v3"}
                ),
            ),
            "payload": _command().payload.model_copy(
                update={
                    "acknowledged_assertion_count": 1,
                    "acknowledged_assumption_count": 1,
                }
            )
        }
    )

    receipt = await RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_Evidence()
    ).dispatch(
        context=_context(),
        command=command,
        authority=_authority(),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )

    assert receipt is not None
    assert receipt.assertion_count == 1
    assert receipt.assumption_count == 1


@pytest.mark.asyncio
async def test_exact_acceptance_replay_returns_stable_receipt_without_duplicate_effect() -> None:
    uow = _Uow()
    service = RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_Evidence()
    )
    first = await service.dispatch(
        context=_context(), command=_command(), authority=_authority(),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )
    uow.workflow_runs.admission = "replayed"
    replay = await service.dispatch(
        context=_context(), command=_command(), authority=_authority(),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )

    assert first is not None and replay is not None
    assert replay.model_copy(update={"command_receipt": first.command_receipt}) == first
    assert replay.command_receipt.admission == "replayed"
    assert len(uow.requirements_acceptances.items) == 1


@pytest.mark.asyncio
async def test_exact_replay_returns_stable_receipt_after_evidence_revocation() -> None:
    uow = _Uow()
    first = await RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_Evidence()
    ).dispatch(
        context=_context(), command=_command(), authority=_authority(),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )

    replay = await RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_RevokedEvidence()
    ).dispatch(
        context=_context(), command=_command(), authority=_authority(),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )

    assert first is not None and replay is not None
    assert replay.command_receipt.admission == "replayed"
    assert replay.accepted_artifact_ref == first.accepted_artifact_ref
    assert len(uow.requirements_acceptances.items) == 1


@pytest.mark.asyncio
async def test_changed_payload_replay_conflicts_without_revalidation_or_new_effect() -> None:
    uow = _Uow()
    service = RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_Evidence()
    )
    await service.dispatch(
        context=_context(), command=_command(), authority=_authority(),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )
    changed = _command().model_copy(
        update={
            "payload": _command().payload.model_copy(
                update={"acknowledged_assumption_count": 1}
            )
        }
    )

    with pytest.raises(ConstraintConflictError):
        await service.dispatch(
            context=_context(), command=changed, authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )

    assert len(uow.requirements_acceptances.items) == 1


@pytest.mark.asyncio
async def test_unauthorized_actor_is_rejected_without_changing_outcome() -> None:
    uow = _Uow()
    denied = _authority().model_copy(update={"allowed": False})

    with pytest.raises(RequirementsAcceptanceDeniedError):
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_ParsedPassThrough()
        ).dispatch(
            context=_context(), command=_command(), authority=denied,
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )

    assert uow.requirements_acceptances.items == {}
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_stale_artifact_is_rejected_without_changing_outcome() -> None:
    uow = _Uow()
    current = _artifact().model_copy(
        update={
            "artifact_version": 2,
            "predecessor_version": 1,
            "production_identity": "requirements-review-v2",
            "production_digest": "b" * 64,
        }
    )
    uow.requirements_artifacts.items.append(current)

    with pytest.raises(RequirementsArtifactStaleError) as error:
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_Evidence()
        ).dispatch(
            context=_context(), command=_command(), authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )

    assert error.value.current_ref.artifact_version == 2
    assert uow.requirements_acceptances.items == {}
    assert uow.commits == 0


class _RevokedEvidence:
    async def validate_artifact(self, *, context, requester, artifact):
        evidence = artifact.requirements[0].evidence_refs[0].model_copy(
            update={
                "validation": RequirementEvidenceValidation(
                    validation_reference=UUID(int=73),
                    status="denied",
                    validated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                )
            }
        )
        requirement = artifact.requirements[0].model_copy(
            update={
                "evidence_refs": (evidence,),
                "evidence_status": RequirementEvidenceStatus.UNSUPPORTED,
            }
        )
        return artifact.model_copy(update={"requirements": (requirement,)})


class _StatusEvidence:
    def __init__(self, status: str) -> None:
        self.status = status

    async def validate_artifact(self, *, context, requester, artifact):
        evidence = artifact.requirements[0].evidence_refs[0].model_copy(
            update={
                "validation": RequirementEvidenceValidation(
                    validation_reference=UUID(int=74),
                    status=self.status,
                    authorization_decision_reference=(
                        UUID(int=75) if self.status == "valid" else None
                    ),
                    validated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                )
            }
        )
        requirement = artifact.requirements[0].model_copy(
            update={"evidence_refs": (evidence,)}
        )
        return artifact.model_copy(update={"requirements": (requirement,)})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "unverified",
        "denied",
        "stale",
        "tombstoned",
        "missing",
        "locator_mismatch",
        "fabricated",
        "degraded",
    ],
)
async def test_each_nonvalid_source_evidence_state_blocks_acceptance(status: str) -> None:
    uow = _Uow()

    with pytest.raises(RequirementsAcceptanceDeniedError):
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_StatusEvidence(status)
        ).dispatch(
            context=_context(), command=_command(), authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )

    assert uow.requirements_acceptances.items == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requirement_update",
    [
        {"state": RequirementState.PARTIAL},
        {"state": RequirementState.UNVERIFIED},
        {"state": RequirementState.CANDIDATE},
        {"ambiguous_markers": ("Which Friday?",)},
        {"missing_markers": ("Approved delivery timezone",)},
    ],
)
async def test_incomplete_or_ambiguous_requirement_blocks_acceptance(
    requirement_update: dict[str, object],
) -> None:
    uow = _Uow()
    artifact = _artifact()
    requirement = artifact.requirements[0].model_copy(update=requirement_update)
    uow.requirements_artifacts.items[0] = artifact.model_copy(
        update={"requirements": (requirement,)}
    )

    with pytest.raises(RequirementsAcceptanceDeniedError):
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_Evidence()
        ).dispatch(
            context=_context(), command=_command(), authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )


@pytest.mark.asyncio
async def test_manager_assertion_cannot_launder_attached_source_evidence() -> None:
    uow = _Uow()
    base = _artifact()
    source_backed = RequirementV3.model_validate(
        {
            **base.requirements[0].model_dump(mode="python"),
            "schema_version": "requirement:v3",
            "acceptance_basis": "manager_assertion",
            "acceptance_rationale": "Independently known by the manager.",
            "attributed_by": ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
            "assertion_audit_reference": UUID(int=76),
            "independent_human_assertion": True,
        }
    )
    uow.requirements_artifacts.items[0] = base.model_copy(
        update={
            "schema_version": "requirements:v3",
            "requirements": (source_backed,),
        }
    )
    command = _command().model_copy(
        update={
            "artifact_refs": (
                _command().artifact_refs[0].model_copy(
                    update={"schema_version": "requirements:v3"}
                ),
            ),
            "payload": _command().payload.model_copy(
                update={"acknowledged_assertion_count": 1}
            ),
        }
    )

    with pytest.raises(RequirementsAcceptanceDeniedError):
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_Evidence()
        ).dispatch(
            context=_context(), command=command, authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )


@pytest.mark.asyncio
async def test_manager_assertion_cannot_copy_protected_predecessor_text_verbatim() -> None:
    uow = _Uow()
    base = _artifact()
    reclassification_command_id = UUID(int=78)
    copied = RequirementV3(
        requirement_id=uuid5(
            reclassification_command_id,
            f"spine.requirement-reclassification:v1:{base.requirements[0].requirement_id}",
        ),
        category="functional",
        statement=f"  {base.requirements[0].statement.upper()}  ",
        state=RequirementState.HUMAN_REVIEWED,
        review_state=RequirementReviewState.REVIEWED,
        acceptance_basis=RequirementAcceptanceBasis.MANAGER_ASSERTION,
        acceptance_rationale="Treat the copied source text as my assertion.",
        attributed_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        assertion_audit_reference=reclassification_command_id,
        independent_human_assertion=True,
        upstream_artifact_ids=(base.artifact_id,),
    )
    uow.requirements_artifacts.items.append(
        base.model_copy(
            update={
                "artifact_version": 2,
                "predecessor_version": 1,
                "schema_version": "requirements:v3",
                "production_identity": (
                    "requirements-human-reclassify_requirement:v1:"
                    f"{reclassification_command_id}"
                ),
                "production_digest": "c" * 64,
                "requirements": (copied,),
            }
        )
    )
    command = _command().model_copy(
        update={
            "payload": _command().payload.model_copy(
                update={
                    "artifact_digest": "c" * 64,
                    "acknowledged_assertion_count": 1,
                }
            ),
            "artifact_refs": (
                _command().artifact_refs[0].model_copy(
                    update={"schema_version": "requirements:v3", "artifact_version": 2}
                ),
            ),
        }
    )

    with pytest.raises(RequirementsAcceptanceDeniedError):
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_ParsedPassThrough()
        ).dispatch(
            context=_context(), command=command, authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )


@pytest.mark.asyncio
async def test_reclassified_conflicting_predecessor_requires_explicit_resolution() -> None:
    uow = _Uow()
    base = _artifact()
    base = base.model_copy(
        update={
            "requirements": (
                base.requirements[0].model_copy(
                    update={
                        "evidence_refs": (
                            base.requirements[0].evidence_refs[0].model_copy(
                                update={
                                    "relationship": RequirementEvidenceRelationship.CONTRADICTS
                                }
                            ),
                        )
                    }
                ),
            )
        }
    )
    uow.requirements_artifacts.items[0] = base
    reclassification_command_id = UUID(int=79)
    reclassified = RequirementV3(
        requirement_id=uuid5(
            reclassification_command_id,
            f"spine.requirement-reclassification:v1:{base.requirements[0].requirement_id}",
        ),
        category="functional",
        statement="Our independently owned commitment is Friday noon.",
        state=RequirementState.HUMAN_REVIEWED,
        review_state=RequirementReviewState.REVIEWED,
        acceptance_basis=RequirementAcceptanceBasis.MANAGER_ASSERTION,
        acceptance_rationale="The manager owns the commitment.",
        attributed_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        assertion_audit_reference=reclassification_command_id,
        independent_human_assertion=True,
        upstream_artifact_ids=(base.artifact_id,),
    )
    uow.requirements_artifacts.items.append(
        base.model_copy(
            update={
                "artifact_version": 2,
                "predecessor_version": 1,
                "schema_version": "requirements:v3",
                "production_identity": (
                    "requirements-human-reclassify_requirement:v1:"
                    f"{reclassification_command_id}"
                ),
                "production_digest": "d" * 64,
                "requirements": (reclassified,),
            }
        )
    )
    command = _command().model_copy(
        update={
            "payload": _command().payload.model_copy(
                update={
                    "artifact_digest": "d" * 64,
                    "acknowledged_assertion_count": 1,
                }
            ),
            "artifact_refs": (
                _command().artifact_refs[0].model_copy(
                    update={"schema_version": "requirements:v3", "artifact_version": 2}
                ),
            ),
        }
    )

    with pytest.raises(RequirementsAcceptanceDeniedError):
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_ParsedPassThrough()
        ).dispatch(
            context=_context(), command=command, authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("accepting_manager_id", [UUID(int=21), UUID(int=22)])
async def test_authorized_manager_accepts_explicit_reclassification_in_successor_version(
    accepting_manager_id: UUID,
) -> None:
    uow = _Uow()
    base = _artifact()
    reclassification_command_id = UUID(int=77)
    reclassified = RequirementV3(
        requirement_id=uuid5(
            reclassification_command_id,
            f"spine.requirement-reclassification:v1:{base.requirements[0].requirement_id}",
        ),
        category="functional",
        statement="The account owner commits completion before the weekend.",
        state=RequirementState.HUMAN_REVIEWED,
        review_state=RequirementReviewState.REVIEWED,
        acceptance_basis=RequirementAcceptanceBasis.MANAGER_ASSERTION,
        acceptance_rationale="The manager independently confirmed the deadline.",
        attributed_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=21)),
        assertion_audit_reference=reclassification_command_id,
        independent_human_assertion=True,
        conflict_resolution_rationale="Prefer the manager's current commitment.",
        upstream_artifact_ids=(base.artifact_id,),
    )
    successor = base.model_copy(
        update={
            "artifact_version": 2,
            "predecessor_version": 1,
            "schema_version": "requirements:v3",
            "production_identity": (
                "requirements-human-reclassify_requirement:v1:"
                f"{reclassification_command_id}"
            ),
            "production_digest": "b" * 64,
            "requirements": (reclassified,),
        }
    )
    uow.requirements_artifacts.items.append(successor)
    command = _command().model_copy(
        update={
            "actor": ActorRef(kind=ActorKind.HUMAN, id=accepting_manager_id),
            "payload": _command().payload.model_copy(
                update={
                    "artifact_digest": "b" * 64,
                    "acknowledged_assertion_count": 1,
                }
            ),
            "artifact_refs": (
                _command().artifact_refs[0].model_copy(
                    update={
                        "schema_version": "requirements:v3",
                        "artifact_version": 2,
                    }
                ),
            ),
        }
    )

    receipt = await RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_ParsedPassThrough()
    ).dispatch(
        context=_context(acting_subject_id=accepting_manager_id),
        command=command,
        authority=_authority().model_copy(update={"actor_id": accepting_manager_id}),
        requester=ParsedContentInspectionRequester(subject_id=accepting_manager_id),
    )

    assert receipt is not None
    assert receipt.accepted_artifact_ref.artifact_version == 2
    assert receipt.assertion_count == 1
    assert uow.requirements_artifacts.items[0].requirements[0].evidence_refs


@pytest.mark.asyncio
async def test_revoked_evidence_is_rejected_without_granting_source_access() -> None:
    uow = _Uow()

    with pytest.raises(RequirementsAcceptanceDeniedError):
        await RequirementsAcceptanceService(
            lambda _: _Context(uow), evidence_validator=_RevokedEvidence()
        ).dispatch(
            context=_context(), command=_command(), authority=_authority(),
            requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
        )

    assert uow.requirements_acceptances.items == {}
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_other_workspace_cannot_observe_or_accept_artifact() -> None:
    uow = _Uow()
    command = _command().model_copy(update={"workspace_id": UUID(int=99)})

    result = await RequirementsAcceptanceService(
        lambda _: _Context(uow), evidence_validator=_Evidence()
    ).dispatch(
        context=_context(UUID(int=99)), command=command,
        authority=_authority().model_copy(update={"workspace_id": UUID(int=99)}),
        requester=ParsedContentInspectionRequester(subject_id=UUID(int=21)),
    )

    assert result is None
    assert uow.requirements_acceptances.items == {}
    assert uow.commits == 0
