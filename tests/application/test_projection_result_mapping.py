from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.projection_result_mapping import (
    AdapterProjectedObject,
    AdapterProjectionDiagnostic,
    AdapterProjectionReceipt,
    AdapterSourceProjectionReference,
    CanonicalProjectionLocator,
    CanonicalProjectionMappingContext,
    ProjectionMappingConflictError,
    ProjectionMappingError,
    ProjectionResultMappingBridge,
)
from spine.application.testing import DeterministicCanonicalProjectionGateway
from spine.domain.projections import (
    ProjectionCandidateState,
    ProjectionConfigVersion as CanonicalProjectionConfigVersion,
    ProjectionOperation as CanonicalProjectionOperation,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionRun,
    SourceProjectionReference,
)
from spine.domain.sources import (
    CanonicalBoundary as CanonicalSourceBoundary,
    CanonicalBoundaryMember,
    RevisionKind,
)
from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectCommand,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionCount,
    ProjectionDiagnostic,
    ProjectionEffect,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionFailure,
    ProjectionFailureCode,
    ProjectionLifecycle,
    ProjectionNamespaceState,
    ProjectionOperation,
    ProjectionOutcome,
    ProjectionResult,
    RetrieveCommand,
    ProjectionSourceRevision,
    StableLocator,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SOURCE = UUID(int=3)
REVISION = UUID(int=4)
RUN = UUID(int=5)
CANDIDATE = UUID(int=6)
PARTITION = UUID(int=7)
CONFIG = UUID(int=8)
BOUNDARY = UUID(int=9)
PROCESSING_RECEIPT = UUID(int=10)


def _digest(schema: str, payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {"schema": schema, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _command(
    *,
    digest: str = "e" * 64,
    active: bool = False,
    workspace_id: UUID = WORKSPACE,
    environment_id: UUID = ENVIRONMENT,
) -> ProjectCommand:
    source = ProjectionSourceRevision(
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        revision_digest="a" * 64,
        processing_receipt_id=PROCESSING_RECEIPT,
        locator_contract_version="locator:v1",
    )
    candidate = ProjectionCandidate(
        candidate_id=CANDIDATE,
        run_id=RUN,
        partition_id=PARTITION,
    )
    config = ProjectionConfigVersion(
        config_id=CONFIG,
        version="1",
        config_digest=_digest(
            "projection-config:r1", {"adapter": "context-projection:v1"}
        ),
        ontology_version="ontology-1",
        adapter_contract_version="context-projection:v1",
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY,
        boundary_digest="c" * 64,
        accepted_through_generation=1,
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=workspace_id,
            environment_id=environment_id,
            projection_kind="context",
            projection_candidate=candidate,
            projection_config=config,
            canonical_boundary=boundary,
            state=(
                ProjectionNamespaceState.ACTIVE
                if active
                else ProjectionNamespaceState.SHADOW
            ),
        )
    )
    return ProjectCommand(
        workspace_id=workspace_id,
        environment_id=environment_id,
        projection_run_id=RUN,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        source_revisions=(source,),
        physical_namespace=namespace,
        command_identity="partition-project-1",
        command_digest=digest,
    )


def _context(command: ProjectCommand) -> CanonicalProjectionMappingContext:
    workspace_id = command.workspace_id
    environment_id = command.environment_id
    config_payload = {"adapter": "context-projection:v1"}
    canonical_config = CanonicalProjectionConfigVersion(
        projection_config_version_id=CONFIG,
        workspace_id=workspace_id,
        environment_id=environment_id,
        projection_kind="context",
        version=1,
        ontology_version_id=UUID(int=20),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extraction:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="context-projection:v1",
        config_payload=config_payload,
        config_digest=_digest("projection-config:r1", config_payload),
        created_at=NOW,
    )
    canonical_boundary = CanonicalSourceBoundary(
        boundary_id=BOUNDARY,
        workspace_id=workspace_id,
        environment_id=environment_id,
        projection_kind="context",
        scope_version="r1",
        accepted_through_generation=1,
        boundary_time=NOW,
        manifest_digest=command.canonical_boundary.boundary_digest,
        idempotency_receipt_id=UUID(int=21),
    )
    return CanonicalProjectionMappingContext(
        run=ProjectionRun(
            projection_run_id=RUN,
            projection_candidate_id=CANDIDATE,
            workspace_id=workspace_id,
            environment_id=environment_id,
            projection_kind="context",
            operation=CanonicalProjectionOperation.BUILD,
            projection_config_version_id=CONFIG,
            canonical_boundary_id=BOUNDARY,
            command_identity="projection-run-1",
            command_digest="1" * 64,
            idempotency_receipt_id=UUID(int=22),
            started_at=NOW,
        ),
        partition=ProjectionPartition(
            projection_partition_id=PARTITION,
            projection_run_id=RUN,
            projection_candidate_id=CANDIDATE,
            workspace_id=workspace_id,
            environment_id=environment_id,
            security_domain_reference="security-domain:finance",
            security_domain_version="r1",
            partition_identity="finance",
            expected_command_identity=command.command_identity,
            expected_command_digest=command.command_digest,
            created_at=NOW,
        ),
        projection_config=canonical_config,
        canonical_boundary=canonical_boundary,
        boundary_members=(
            CanonicalBoundaryMember(
                boundary_id=BOUNDARY,
                source_object_id=SOURCE,
                source_revision_id=REVISION,
                acceptance_id=UUID(int=25),
                workspace_id=workspace_id,
                environment_id=environment_id,
                revision_kind=RevisionKind.CONTENT,
                accepted_generation=1,
            ),
        ),
        locators=(
            CanonicalProjectionLocator(
                source_object_id=SOURCE,
                source_revision_id=REVISION,
                revision_digest="a" * 64,
                processing_receipt_id=PROCESSING_RECEIPT,
                segment_id="segment-1",
                locator_schema_version="locator:v1",
                locator=StableLocator(kind="page", value="2"),
                locator_digest="f" * 64,
            ),
        ),
        projection_receipt_id=UUID(int=23),
        idempotency_receipt_id=UUID(int=24),
    )


def _adapter_receipt(
    command: ProjectCommand,
    *,
    reference: AdapterSourceProjectionReference | None = None,
) -> AdapterProjectionReceipt:
    source = command.source_revisions[0]
    effect = ProjectionEffect(
        effect_id="partition-project-1:1",
        operation=ProjectionOperation.PROJECT,
        action=ProjectionEffectAction.UPSERT_SOURCE,
        status=ProjectionEffectStatus.APPLIED,
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        command_identity=command.command_identity,
        command_digest=command.command_digest,
        projection_candidate=command.projection_candidate,
        projection_config=command.projection_config,
        canonical_boundary=command.canonical_boundary,
        physical_namespace=command.physical_namespace,
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        revision_digest=source.revision_digest,
        revision_number=source.revision_number,
    )
    result = ProjectionResult(
        operation=ProjectionOperation.PROJECT,
        outcome=ProjectionOutcome.SUCCEEDED,
        lifecycle=ProjectionLifecycle.SUCCEEDED,
        command_identity=command.command_identity,
        command_digest=command.command_digest,
        artifact_reference="artifact:shadow:partition-project-1",
        counts=(ProjectionCount(name="chunks", value=1),),
        effects=(effect,),
    )
    mapped_reference = reference or AdapterSourceProjectionReference(
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        namespace_digest=command.physical_namespace.namespace_digest,
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        revision_digest=source.revision_digest,
        processing_receipt_id=source.processing_receipt_id,
        segment_id="segment-1",
        locator_schema_version="locator:v1",
        locator=StableLocator(kind="page", value="2"),
        locator_digest="f" * 64,
        projected_kind="chunk",
        projected_object_id="chunk-1",
        projection_config_version_id=command.projection_config.config_id,
        security_domain_reference="security-domain:finance",
        security_domain_version="r1",
    )
    return AdapterProjectionReceipt(
        result=result,
        artifact_kind="cognee-shadow",
        artifact_integrity_digest="9" * 64,
        emitted_objects=(AdapterProjectedObject(kind="chunk", object_id="chunk-1"),),
        source_references=(mapped_reference,),
        implementation_schema_version="cognee-context-projection:v1",
        implementation_metadata={"adapter_contract": "context-projection:v1"},
        idempotency_key="projection-receipt-1",
        completed_at=NOW,
    )


@pytest.mark.asyncio
async def test_valid_adapter_result_maps_to_issue7_receipt_and_source_reference() -> None:
    command = _command()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    bridge = ProjectionResultMappingBridge(gateway, id_source=iter((UUID(int=30),)).__next__)

    mapped = await bridge.map(command, _adapter_receipt(command))

    assert mapped.replay is False
    assert mapped.handoff.contract_version == "issue15-to-issue7-projection:v1"
    assert mapped.handoff.command == command
    assert isinstance(mapped.handoff.receipt_command.receipt, ProjectionReceipt)
    assert mapped.handoff.receipt_command.receipt.command_identity == command.command_identity
    assert mapped.handoff.receipt_command.receipt.command_digest == command.command_digest
    assert mapped.handoff.receipt_command.receipt.counts == {"chunks": 1}
    assert mapped.handoff.receipt_command.receipt.projection_candidate_id == CANDIDATE
    assert mapped.handoff.receipt_command.receipt.projection_partition_id == PARTITION
    assert mapped.handoff.receipt_command.receipt.idempotency_receipt_id == UUID(int=24)
    assert mapped.handoff.source_references == (
        SourceProjectionReference(
            source_projection_reference_id=UUID(int=30),
            projection_run_id=RUN,
            projection_candidate_id=CANDIDATE,
            projection_receipt_id=UUID(int=23),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=SOURCE,
            source_revision_id=REVISION,
            processing_receipt_id=PROCESSING_RECEIPT,
            segment_id="segment-1",
            locator_schema_version="locator:v1",
            locator_reference="page:2",
            locator_digest="f" * 64,
            projected_kind="chunk",
            projected_object_id="chunk-1",
            projection_config_version_id=CONFIG,
            security_domain_reference="security-domain:finance",
            created_at=NOW,
        ),
    )
    assert not hasattr(mapped.handoff, "validation")
    assert not hasattr(mapped.handoff, "snapshot")


@pytest.mark.asyncio
async def test_same_identity_and_digest_replays_same_mapping() -> None:
    command = _command()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    bridge = ProjectionResultMappingBridge(
        gateway,
        id_source=iter((UUID(int=30), UUID(int=31))).__next__,
    )

    first = await bridge.map(command, _adapter_receipt(command))
    replay = await bridge.map(command, _adapter_receipt(command))

    assert first.replay is False
    assert replay.replay is True
    assert replay.handoff == first.handoff


@pytest.mark.asyncio
async def test_same_identity_with_changed_digest_is_explicit_conflict() -> None:
    command = _command()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    bridge = ProjectionResultMappingBridge(gateway, id_source=iter((UUID(int=30),)).__next__)
    await bridge.map(command, _adapter_receipt(command))
    conflicting_command = _command(digest="0" * 64)
    conflicting_context = _context(conflicting_command)
    gateway.add_context(conflicting_context)

    with pytest.raises(ProjectionMappingConflictError, match="digest conflict"):
        await bridge.map(conflicting_command, _adapter_receipt(conflicting_command))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_factory, message",
    [
        (
            lambda command: _adapter_receipt(command).model_copy(
                update={"source_references": ()}
            ),
            "missing",
        ),
        (
            lambda command: _adapter_receipt(
                command,
                reference=_adapter_receipt(command).source_references[0].model_copy(
                    update={"locator_digest": "0" * 64}
                ),
            ),
            "fabricated",
        ),
        (
            lambda command: _adapter_receipt(
                command,
                reference=_adapter_receipt(command).source_references[0].model_copy(
                    update={"workspace_id": UUID(int=99)}
                ),
            ),
            "workspace",
        ),
    ],
)
async def test_invalid_source_provenance_is_rejected(receipt_factory, message: str) -> None:
    command = _command()
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match=message):
        await bridge.map(command, receipt_factory(command))


@pytest.mark.asyncio
async def test_partial_failure_retry_replays_stable_receipt_without_becoming_validation() -> None:
    command = _command()
    receipt = _adapter_receipt(command)
    safe_detail = ProjectionDiagnostic(attempt=2, item_count=1)
    failure = AdapterProjectionDiagnostic(
        code="physical_failure",
        retryable=True,
        safe_detail=safe_detail,
    )
    failed_result = ProjectionResult(
        operation=ProjectionOperation.PROJECT,
        outcome=ProjectionOutcome.FAILED,
        lifecycle=ProjectionLifecycle.FAILED,
        command_identity=command.command_identity,
        command_digest=command.command_digest,
        artifact_reference="artifact:shadow:partition-project-1",
        effects=receipt.result.effects,
        failure=ProjectionFailure(
            code=ProjectionFailureCode.PHYSICAL_FAILURE,
            retryable=True,
            safe_detail=safe_detail,
        ),
    )
    receipt = receipt.model_copy(
        update={
            "result": failed_result,
            "warnings": (
                AdapterProjectionDiagnostic(
                    code="degraded_artifact",
                    retryable=False,
                    safe_detail=ProjectionDiagnostic(item_count=1),
                ),
            ),
            "failures": (failure,),
        }
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    mapped = await bridge.map(command, receipt)
    replay = await bridge.map(command, receipt)

    canonical_receipt = mapped.handoff.receipt_command.receipt
    assert canonical_receipt.warnings[0].code == "degraded_artifact"
    assert canonical_receipt.failures[0].code == "physical_failure"
    assert isinstance(mapped.handoff.adapter_receipt.failures[0], AdapterProjectionDiagnostic)
    assert not hasattr(mapped.handoff, "validation")
    assert replay.replay is True
    assert replay.handoff == mapped.handoff


@pytest.mark.asyncio
async def test_active_namespace_cannot_be_mapped_or_selected() -> None:
    command = _command(active=True)
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="shadow"):
        await bridge.map(command, _adapter_receipt(command))


@pytest.mark.asyncio
async def test_candidate_snapshot_cannot_be_selected_by_mapping() -> None:
    command = _command()
    command = command.model_copy(
        update={
            "projection_candidate": command.projection_candidate.model_copy(
                update={"snapshot_id": UUID(int=90)}
            )
        }
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="cannot select"):
        await bridge.map(command, _adapter_receipt(command))


@pytest.mark.asyncio
async def test_sealed_candidate_rejects_new_adapter_mapping() -> None:
    command = _command()
    context = _context(command).model_copy(
        update={
            "run": _context(command).run.model_copy(
                update={"candidate_state": ProjectionCandidateState.SEALED}
            )
        }
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((context,)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="sealed"):
        await bridge.map(command, _adapter_receipt(command))


@pytest.mark.asyncio
async def test_retrieval_result_cannot_map_to_artifact_receipt() -> None:
    project = _command()
    command = RetrieveCommand(
        **project.model_dump(mode="python", exclude={"operation"}),
        query="synthetic query",
    )
    receipt = _adapter_receipt(project)
    receipt = receipt.model_copy(
        update={
            "result": receipt.result.model_copy(
                update={"operation": ProjectionOperation.RETRIEVE}
            )
        }
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(project),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="physical artifact operations"):
        await bridge.map(command, receipt)


@pytest.mark.asyncio
async def test_duplicate_projected_object_mapping_is_rejected_as_ambiguous() -> None:
    command = _command()
    receipt = _adapter_receipt(command)
    receipt = receipt.model_copy(
        update={"source_references": receipt.source_references * 2}
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="ambiguous duplicate"):
        await bridge.map(command, receipt)


@pytest.mark.asyncio
async def test_missing_per_object_provenance_is_rejected() -> None:
    command = _command()
    receipt = _adapter_receipt(command).model_copy(
        update={
            "emitted_objects": (
                AdapterProjectedObject(kind="chunk", object_id="chunk-1"),
                AdapterProjectedObject(kind="chunk", object_id="chunk-2"),
            )
        }
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="per-object provenance"):
        await bridge.map(command, receipt)


@pytest.mark.asyncio
async def test_omitted_assigned_boundary_member_is_rejected() -> None:
    command = _command()
    context = _context(command)
    omitted = context.boundary_members[0].model_copy(
        update={
            "source_object_id": UUID(int=110),
            "source_revision_id": UUID(int=111),
            "acceptance_id": UUID(int=112),
        }
    )
    context = context.model_copy(
        update={"boundary_members": context.boundary_members + (omitted,)}
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((context,)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="exact assigned boundary"):
        await bridge.map(command, _adapter_receipt(command))


@pytest.mark.asyncio
async def test_foreign_workspace_effect_envelope_is_rejected() -> None:
    command = _command()
    receipt = _adapter_receipt(command)
    foreign_effect = receipt.result.effects[0].model_copy(
        update={"workspace_id": UUID(int=99)}
    )
    receipt = receipt.model_copy(
        update={"result": receipt.result.model_copy(update={"effects": (foreign_effect,)})}
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="effect envelope"):
        await bridge.map(command, receipt)


@pytest.mark.asyncio
async def test_same_command_identity_is_isolated_between_workspaces() -> None:
    workspace_a = _command(workspace_id=UUID(int=101))
    workspace_b = _command(workspace_id=UUID(int=102))
    gateway = DeterministicCanonicalProjectionGateway(
        (_context(workspace_a), _context(workspace_b))
    )
    bridge = ProjectionResultMappingBridge(
        gateway,
        id_source=iter((UUID(int=103), UUID(int=104))).__next__,
    )

    mapped_a = await bridge.map(workspace_a, _adapter_receipt(workspace_a))
    mapped_b = await bridge.map(workspace_b, _adapter_receipt(workspace_b))

    assert mapped_a.replay is False
    assert mapped_b.replay is False
    assert mapped_a.handoff.receipt_command.receipt.workspace_id == UUID(int=101)
    assert mapped_b.handoff.receipt_command.receipt.workspace_id == UUID(int=102)


@pytest.mark.asyncio
async def test_unrouted_shadow_namespace_is_rejected() -> None:
    command = _command()
    command = command.model_copy(
        update={
            "physical_namespace": command.physical_namespace.model_copy(
                update={"namespace_digest": "0" * 64}
            )
        }
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="routed"):
        await bridge.map(command, _adapter_receipt(command))


@pytest.mark.asyncio
async def test_namespace_change_cannot_bypass_digest_conflict() -> None:
    command = _command()
    gateway = DeterministicCanonicalProjectionGateway((_context(command),))
    bridge = ProjectionResultMappingBridge(gateway, id_source=iter((UUID(int=30),)).__next__)
    await bridge.map(command, _adapter_receipt(command))
    conflict = command.model_copy(
        update={
            "command_digest": "0" * 64,
            "physical_namespace": command.physical_namespace.model_copy(
                update={
                    "namespace": "spine/context/v1/" + "1" * 64,
                    "namespace_digest": "1" * 64,
                }
            ),
        }
    )

    with pytest.raises(ProjectionMappingConflictError, match="digest conflict"):
        await bridge.map(conflict, _adapter_receipt(conflict))


def test_unallowlisted_adapter_metadata_is_rejected() -> None:
    command = _command()
    receipt = _adapter_receipt(command)
    payload = receipt.model_dump(mode="python", exclude={"implementation_metadata"})

    with pytest.raises(ValidationError, match="provider_error"):
        AdapterProjectionReceipt.model_validate(
            {
                **payload,
                "implementation_metadata": {"provider_error": "protected text"},
            }
        )


@pytest.mark.asyncio
async def test_unpinned_adapter_contract_metadata_is_rejected() -> None:
    command = _command()
    receipt = _adapter_receipt(command).model_copy(
        update={
            "implementation_metadata": _adapter_receipt(
                command
            ).implementation_metadata.model_copy(
                update={"adapter_contract": "context-projection:v2"}
            )
        }
    )
    bridge = ProjectionResultMappingBridge(
        DeterministicCanonicalProjectionGateway((_context(command),)),
        id_source=iter((UUID(int=30),)).__next__,
    )

    with pytest.raises(ProjectionMappingError, match="pinned contract"):
        await bridge.map(command, receipt)


def test_bridge_contract_does_not_define_projection_version() -> None:
    import spine.application.projection_result_mapping as mapping

    assert "ProjectionVersion" not in mapping.__dict__
