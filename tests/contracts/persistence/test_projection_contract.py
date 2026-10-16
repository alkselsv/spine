from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

import pytest

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.boundary import CanonicalBoundaryCaptureCommand
from spine.application.persistence.projection import (
    ProjectionActivationCommand,
    ProjectionCandidateSealCommand,
    ProjectionReceiptCommand,
    ProjectionRunCommand,
    ProjectionSnapshotCommand,
    ProjectionValidationCommand,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestedOutboxPayload,
)
from spine.application.projection_recovery_execution import (
    PersistenceCanonicalProjectionRecoveryResolver,
    PersistenceProjectionRecoveryLifecycle,
    ProjectionRecoveryRequestEnvelope,
)
from spine.application.projection_result_mapping import (
    AdapterProjectionReceipt,
    ProjectionMappingHandoff,
)
from spine.application.projection_result_persistence import (
    PersistenceCanonicalProjectionGateway,
)
from spine.domain.projections import ProjectionActivationKind, PublishedActivePublication
from spine.domain.projections import (
    OntologyVersion,
    ProjectionConfigVersion,
    ProjectionOperation,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionRun,
    ProjectionRunState,
    ProjectionRunTransition,
    ProjectionValidation,
    SourceProjectionReference,
    projection_snapshot_manifest_digest,
)
from spine.memory.contracts import (
    ProjectionCount,
    ProjectionEffect,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionLifecycle,
    ProjectionOperation as PhysicalProjectionOperation,
    ProjectionOutcome,
    ProjectionResult,
)
from tests.contracts.persistence.test_canonical_acceptance_contract import prepare_case


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _PreparedProjectionPublication:
    persistence_adapter: object
    activation_context: object
    seal_context: object
    seal_command: ProjectionCandidateSealCommand
    created: object
    sealed: object
    assembled: object
    activation_command: ProjectionActivationCommand
    activated: object
    rollback_snapshot: object
    rollback_activation_command: ProjectionActivationCommand


def _digest(schema: str, payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {"schema": schema, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _ontology(workspace_id: UUID, environment_id: UUID) -> OntologyVersion:
    payload = {
        "schema_version": "ontology-definition:r1",
        "entity_types": [{"name": "document"}],
        "relation_types": [],
    }
    return OntologyVersion(
        ontology_version_id=UUID(int=workspace_id.int + 40),
        workspace_id=workspace_id,
        environment_id=environment_id,
        ontology_name="qa",
        version=1,
        definition_payload=payload,
        definition_digest=_digest("ontology-definition:r1", payload),
        publication_eligible=True,
        publication_decision_reference="decision-1",
        publisher_authority_reference="authority-1",
        audit_event_reference="audit-1",
        published_at=NOW,
    )


async def _prepare_projection_publication(
    persistence_adapter,
    *,
    seed: int = 24000,
) -> _PreparedProjectionPublication:
    case = await prepare_case(persistence_adapter, seed)
    async with persistence_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    boundary_context = persistence_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "canonical_boundary_capture"
    )
    async with persistence_adapter.uow_factory(boundary_context) as uow:
        boundary = await uow.boundaries.capture(
            CanonicalBoundaryCaptureCommand.create(
                projection_kind="qa",
                scope_version="r1",
                idempotency_key=IdempotencyKey("projection-boundary-24000"),
            )
        )
        await uow.commit()

    projection_context = persistence_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_run"
    )
    ontology = _ontology(case.source.workspace_id, case.source.environment_id)
    config_payload = {"extractor": "provider-neutral:v1"}
    config = ProjectionConfigVersion(
        projection_config_version_id=UUID(int=case.source.workspace_id.int + 41),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_kind="qa",
        version=1,
        ontology_version_id=ontology.ontology_version_id,
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload=config_payload,
        config_digest=_digest("projection-config:r1", config_payload),
        created_at=NOW,
    )
    run = ProjectionRun(
        projection_run_id=UUID(int=case.source.workspace_id.int + 42),
        projection_candidate_id=UUID(int=case.source.workspace_id.int + 43),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_kind="qa",
        operation=ProjectionOperation.BUILD,
        projection_config_version_id=config.projection_config_version_id,
        canonical_boundary_id=boundary.boundary.boundary_id,
        command_identity="projection-run-24000",
        command_digest="4" * 64,
        idempotency_receipt_id=UUID(int=case.source.workspace_id.int + 44),
        started_at=NOW,
    )
    async with persistence_adapter.uow_factory(projection_context) as uow:
        await uow.projections.add_ontology_version(ontology)
        await uow.projections.add_config_version(config)
        created = await uow.projections.create_run(
            ProjectionRunCommand.create(
                run=run,
                idempotency_key=IdempotencyKey("projection-run-24000"),
            )
        )
        await uow.commit()

    partition = ProjectionPartition(
        projection_partition_id=UUID(int=case.source.workspace_id.int + 45),
        projection_run_id=run.projection_run_id,
        projection_candidate_id=run.projection_candidate_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        security_domain_reference="security-domain:default",
        security_domain_version="r1",
        partition_identity="default",
        expected_command_identity="partition-command-24000",
        expected_command_digest="5" * 64,
        created_at=NOW,
    )
    receipt = ProjectionReceipt(
        projection_receipt_id=UUID(int=case.source.workspace_id.int + 46),
        projection_run_id=run.projection_run_id,
        projection_candidate_id=run.projection_candidate_id,
        projection_partition_id=partition.projection_partition_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        command_identity=partition.expected_command_identity,
        command_digest=partition.expected_command_digest,
        artifact_kind="cognee-shadow",
        artifact_reference="artifact-24000",
        artifact_integrity_digest="6" * 64,
        counts={"chunks": 1},
        implementation_schema_version="cognee:v1",
        idempotency_receipt_id=UUID(int=case.source.workspace_id.int + 47),
        completed_at=NOW,
    )
    reference = SourceProjectionReference(
        source_projection_reference_id=UUID(int=case.source.workspace_id.int + 48),
        projection_run_id=run.projection_run_id,
        projection_candidate_id=run.projection_candidate_id,
        projection_receipt_id=receipt.projection_receipt_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        source_object_id=case.source.source_object_id,
        source_revision_id=case.revision.revision_id,
        segment_id="chunk-1",
        locator_schema_version="locator:v1",
        locator_reference="page:1",
        locator_digest="7" * 64,
        projected_kind="chunk",
        projected_object_id="chunk-1",
        projection_config_version_id=config.projection_config_version_id,
        security_domain_reference=partition.security_domain_reference,
        created_at=NOW,
    )
    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            case.source.workspace_id, case.source.environment_id, "projection_receipt"
        )
    ) as uow:
        await uow.projections.append_transition(
            ProjectionRunTransition(
                projection_run_transition_id=UUID(int=case.source.workspace_id.int + 49),
                projection_run_id=run.projection_run_id,
                projection_candidate_id=run.projection_candidate_id,
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                state=ProjectionRunState.REQUESTED,
                attempt=1,
                occurred_at=NOW,
            )
        )
        await uow.projections.add_partition(partition)
        await uow.projections.record_receipt(
            ProjectionReceiptCommand.create(
                receipt=receipt,
                idempotency_key=IdempotencyKey("projection-receipt-24000"),
            )
        )
        await uow.projections.add_source_reference(reference)
        await uow.commit()

    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            case.source.workspace_id, case.source.environment_id, "projection_validation"
        )
    ) as uow:
        await uow.projections.record_validation(
            ProjectionValidationCommand.create(
                validation=ProjectionValidation(
                    projection_validation_id=UUID(int=case.source.workspace_id.int + 50),
                    projection_run_id=run.projection_run_id,
                    projection_candidate_id=run.projection_candidate_id,
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    gate_kind="manifest",
                    gate_version="r1",
                    input_digest="8" * 64,
                    result_digest="9" * 64,
                    decision="passed",
                    metrics_schema_version="metrics:v1",
                    evaluator_reference="evaluator-24000",
                    evaluated_at=NOW,
                ),
                idempotency_key=IdempotencyKey("projection-validation-24000"),
            )
        )
        await uow.projections.record_validation(
            ProjectionValidationCommand.create(
                validation=ProjectionValidation(
                    projection_validation_id=UUID(int=case.source.workspace_id.int + 53),
                    projection_run_id=run.projection_run_id,
                    projection_candidate_id=run.projection_candidate_id,
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    gate_kind="physical_artifacts",
                    gate_version="r1",
                    input_digest="a" * 64,
                    result_digest="b" * 64,
                    decision="passed",
                    metrics_schema_version="physical-projection-validation-metrics:v1",
                    metrics_payload={
                        "schema_version": "physical-projection-validation-metrics:v1",
                        "projection_lineage": {
                            "run_id": str(run.projection_run_id),
                            "candidate_id": str(run.projection_candidate_id),
                            "partition_id": str(partition.projection_partition_id),
                            "command_identity": partition.expected_command_identity,
                            "command_digest": partition.expected_command_digest,
                            "projection_config_id": str(config.projection_config_version_id),
                            "projection_config_version": "1",
                            "projection_config_digest": config.config_digest,
                            "namespace_digest": "c" * 64,
                        },
                        "artifact_evidence": {
                            "reference": receipt.artifact_reference,
                            "digest": receipt.artifact_integrity_digest,
                        },
                        "source_lineage": [
                            {
                                "source_object_id": str(case.source.source_object_id),
                                "source_revision_id": str(case.revision.revision_id),
                                "revision_digest": case.revision.revision_digest,
                                "revision_number": 1,
                                "status": "current",
                                "processing_receipt_id": None,
                                "locator_contract_version": "locator:v1",
                            }
                        ],
                        "source_projection_references": [
                            {
                                "reference_id": str(reference.source_projection_reference_id),
                                "source_object_id": str(reference.source_object_id),
                                "source_revision_id": str(reference.source_revision_id),
                                "segment_id": reference.segment_id,
                                "locator_digest": reference.locator_digest,
                                "projected_kind": reference.projected_kind,
                                "projected_object_id": reference.projected_object_id,
                            }
                        ],
                    },
                    evaluator_reference="physical-projection-validator:r1",
                    evaluated_at=NOW,
                ),
                idempotency_key=IdempotencyKey("projection-physical-validation-24000"),
            )
        )
        await uow.commit()

    seal_context = persistence_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_candidate_seal"
    )
    seal = ProjectionCandidateSealCommand.create(
        projection_run_id=run.projection_run_id,
        projection_candidate_id=run.projection_candidate_id,
        idempotency_key=IdempotencyKey("projection-seal-24000"),
    )
    snapshot_context = persistence_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_snapshot"
    )
    async with persistence_adapter.uow_factory(seal_context) as uow:
        sealed = await uow.projections.seal_candidate(seal)
        await uow.commit()

    snapshot = ProjectionSnapshot(
        projection_snapshot_id=UUID(int=case.source.workspace_id.int + 51),
        projection_run_id=run.projection_run_id,
        projection_candidate_id=run.projection_candidate_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        projection_kind=run.projection_kind,
        projection_config_version_id=config.projection_config_version_id,
        canonical_boundary_id=boundary.boundary.boundary_id,
        candidate_evidence_digest=sealed.value.candidate_evidence_digest,
        manifest_digest="0" * 64,
        member_count=1,
        created_at=NOW,
    )
    snapshot_member = ProjectionSnapshotMember(
        projection_snapshot_member_id=UUID(int=case.source.workspace_id.int + 52),
        projection_snapshot_id=snapshot.projection_snapshot_id,
        canonical_boundary_id=boundary.boundary.boundary_id,
        canonical_boundary_member_id=case.source.source_object_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        source_object_id=case.source.source_object_id,
        source_revision_id=case.revision.revision_id,
        disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
        projection_partition_id=partition.projection_partition_id,
        projection_receipt_id=receipt.projection_receipt_id,
    )
    snapshot = snapshot.model_copy(
        update={
            "manifest_digest": projection_snapshot_manifest_digest(snapshot, (snapshot_member,)),
        }
    )
    async with persistence_adapter.uow_factory(snapshot_context) as uow:
        assembled = await uow.projections.assemble_snapshot(
            ProjectionSnapshotCommand.create(
                snapshot=snapshot,
                members=(snapshot_member,),
                idempotency_key=IdempotencyKey("projection-snapshot-24000"),
            )
        )
        await uow.commit()

    activation_context = persistence_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_activation"
    )
    activation = ProjectionActivationCommand.create(
        projection_kind="qa",
        projection_snapshot_id=assembled.value.projection_snapshot_id,
        activation_kind=ProjectionActivationKind.PUBLISH,
        expected_predecessor_snapshot_id=None,
        expected_predecessor_activation_generation=None,
        expected_active_pointer_cas_version=None,
        command_identity="projection-activation-24000",
        idempotency_key=IdempotencyKey("projection-activation-24000"),
    )
    async with persistence_adapter.uow_factory(activation_context) as uow:
        activated = await uow.projections.activate(activation)
        await uow.commit()

    rollback_run = run.model_copy(
        update={
            "projection_run_id": UUID(int=case.source.workspace_id.int + 60),
            "projection_candidate_id": UUID(int=case.source.workspace_id.int + 61),
            "operation": ProjectionOperation.ROLLBACK,
            "predecessor_snapshot_id": assembled.value.projection_snapshot_id,
            "command_identity": "projection-rollback-run-24000",
            "command_digest": "c" * 64,
            "idempotency_receipt_id": UUID(int=case.source.workspace_id.int + 62),
        }
    )
    rollback_partition = partition.model_copy(
        update={
            "projection_partition_id": UUID(int=case.source.workspace_id.int + 63),
            "projection_run_id": rollback_run.projection_run_id,
            "projection_candidate_id": rollback_run.projection_candidate_id,
            "expected_command_identity": "rollback-partition-command-24000",
            "expected_command_digest": "d" * 64,
        }
    )
    rollback_receipt = receipt.model_copy(
        update={
            "projection_receipt_id": UUID(int=case.source.workspace_id.int + 64),
            "projection_run_id": rollback_run.projection_run_id,
            "projection_candidate_id": rollback_run.projection_candidate_id,
            "projection_partition_id": rollback_partition.projection_partition_id,
            "command_identity": rollback_partition.expected_command_identity,
            "command_digest": rollback_partition.expected_command_digest,
            "artifact_reference": "rollback-artifact-24000",
            "artifact_integrity_digest": "e" * 64,
            "idempotency_receipt_id": UUID(int=case.source.workspace_id.int + 65),
        }
    )
    rollback_reference = reference.model_copy(
        update={
            "source_projection_reference_id": UUID(int=case.source.workspace_id.int + 66),
            "projection_run_id": rollback_run.projection_run_id,
            "projection_candidate_id": rollback_run.projection_candidate_id,
            "projection_receipt_id": rollback_receipt.projection_receipt_id,
            "projection_partition_id": rollback_partition.projection_partition_id,
        }
    )
    async with persistence_adapter.uow_factory(projection_context) as uow:
        await uow.projections.create_run(
            ProjectionRunCommand.create(
                run=rollback_run,
                idempotency_key=IdempotencyKey("projection-rollback-run-24000"),
            )
        )
        await uow.projections.append_transition(
            ProjectionRunTransition(
                projection_run_transition_id=UUID(int=case.source.workspace_id.int + 67),
                projection_run_id=rollback_run.projection_run_id,
                projection_candidate_id=rollback_run.projection_candidate_id,
                workspace_id=rollback_run.workspace_id,
                environment_id=rollback_run.environment_id,
                state=ProjectionRunState.REQUESTED,
                attempt=1,
                occurred_at=NOW,
            )
        )
        await uow.projections.add_partition(rollback_partition)
        await uow.commit()
    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            case.source.workspace_id,
            case.source.environment_id,
            "projection_receipt",
        )
    ) as uow:
        await uow.projections.record_receipt(
            ProjectionReceiptCommand.create(
                receipt=rollback_receipt,
                idempotency_key=IdempotencyKey("projection-rollback-receipt-24000"),
            )
        )
        await uow.projections.add_source_reference(rollback_reference)
        await uow.commit()

    rollback_metrics = {
        "schema_version": "physical-projection-validation-metrics:v1",
        "projection_lineage": {
            "run_id": str(rollback_run.projection_run_id),
            "candidate_id": str(rollback_run.projection_candidate_id),
            "partition_id": str(rollback_partition.projection_partition_id),
            "command_identity": rollback_partition.expected_command_identity,
            "command_digest": rollback_partition.expected_command_digest,
            "projection_config_id": str(config.projection_config_version_id),
            "projection_config_version": "1",
            "projection_config_digest": config.config_digest,
            "namespace_digest": "f" * 64,
        },
        "artifact_evidence": {
            "reference": rollback_receipt.artifact_reference,
            "digest": rollback_receipt.artifact_integrity_digest,
        },
        "source_lineage": [
            {
                "source_object_id": str(case.source.source_object_id),
                "source_revision_id": str(case.revision.revision_id),
                "revision_digest": case.revision.revision_digest,
                "revision_number": 1,
                "status": "current",
                "processing_receipt_id": None,
                "locator_contract_version": "locator:v1",
            }
        ],
        "source_projection_references": [
            {
                "reference_id": str(rollback_reference.source_projection_reference_id),
                "source_object_id": str(rollback_reference.source_object_id),
                "source_revision_id": str(rollback_reference.source_revision_id),
                "segment_id": rollback_reference.segment_id,
                "locator_digest": rollback_reference.locator_digest,
                "projected_kind": rollback_reference.projected_kind,
                "projected_object_id": rollback_reference.projected_object_id,
            }
        ],
    }
    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            case.source.workspace_id, case.source.environment_id, "projection_validation"
        )
    ) as uow:
        for offset, gate_kind, metrics in (
            (68, "manifest", None),
            (69, "physical_artifacts", rollback_metrics),
        ):
            await uow.projections.record_validation(
                ProjectionValidationCommand.create(
                    validation=ProjectionValidation(
                        projection_validation_id=UUID(
                            int=case.source.workspace_id.int + offset
                        ),
                        projection_run_id=rollback_run.projection_run_id,
                        projection_candidate_id=rollback_run.projection_candidate_id,
                        workspace_id=rollback_run.workspace_id,
                        environment_id=rollback_run.environment_id,
                        gate_kind=gate_kind,
                        gate_version="r1",
                        input_digest=f"{offset % 10}" * 64,
                        result_digest=f"{(offset + 1) % 10}" * 64,
                        decision="passed",
                        metrics_schema_version=(
                            "physical-projection-validation-metrics:v1"
                            if metrics is not None
                            else "metrics:v1"
                        ),
                        metrics_payload=metrics or {},
                        evaluator_reference=f"rollback-{gate_kind}-validator:r1",
                        evaluated_at=NOW,
                    ),
                    idempotency_key=IdempotencyKey(
                        f"projection-rollback-{gate_kind}-24000"
                    ),
                )
            )
        await uow.commit()

    rollback_seal = ProjectionCandidateSealCommand.create(
        projection_run_id=rollback_run.projection_run_id,
        projection_candidate_id=rollback_run.projection_candidate_id,
        idempotency_key=IdempotencyKey("projection-rollback-seal-24000"),
    )
    async with persistence_adapter.uow_factory(seal_context) as uow:
        sealed_rollback = await uow.projections.seal_candidate(rollback_seal)
        await uow.commit()
    rollback_snapshot = snapshot.model_copy(
        update={
            "projection_snapshot_id": UUID(int=case.source.workspace_id.int + 70),
            "projection_run_id": rollback_run.projection_run_id,
            "projection_candidate_id": rollback_run.projection_candidate_id,
            "candidate_evidence_digest": sealed_rollback.value.candidate_evidence_digest,
            "predecessor_snapshot_id": assembled.value.projection_snapshot_id,
            "manifest_digest": "0" * 64,
        }
    )
    rollback_member = snapshot_member.model_copy(
        update={
            "projection_snapshot_member_id": UUID(int=case.source.workspace_id.int + 71),
            "projection_snapshot_id": rollback_snapshot.projection_snapshot_id,
            "projection_partition_id": rollback_partition.projection_partition_id,
            "projection_receipt_id": rollback_receipt.projection_receipt_id,
        }
    )
    rollback_snapshot = rollback_snapshot.model_copy(
        update={
            "manifest_digest": projection_snapshot_manifest_digest(
                rollback_snapshot, (rollback_member,)
            )
        }
    )
    async with persistence_adapter.uow_factory(snapshot_context) as uow:
        finalized_rollback = await uow.projections.assemble_snapshot(
            ProjectionSnapshotCommand.create(
                snapshot=rollback_snapshot,
                members=(rollback_member,),
                idempotency_key=IdempotencyKey("projection-rollback-snapshot-24000"),
            )
        )
        await uow.commit()

    rollback_activation = ProjectionActivationCommand.create(
        projection_kind="qa",
        projection_snapshot_id=finalized_rollback.value.projection_snapshot_id,
        activation_kind=ProjectionActivationKind.ROLLBACK,
        expected_predecessor_snapshot_id=assembled.value.projection_snapshot_id,
        expected_predecessor_activation_generation=1,
        expected_active_pointer_cas_version=1,
        command_identity="projection-rollback-activation-24000",
        idempotency_key=IdempotencyKey("projection-rollback-activation-24000"),
    )
    return _PreparedProjectionPublication(
        persistence_adapter=persistence_adapter,
        activation_context=activation_context,
        seal_context=seal_context,
        seal_command=seal,
        created=created,
        sealed=sealed,
        assembled=assembled,
        activation_command=activation,
        activated=activated,
        rollback_snapshot=finalized_rollback,
        rollback_activation_command=rollback_activation,
    )


@pytest.mark.asyncio
async def test_projection_candidate_seal_freezes_evidence_and_replays(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter)

    async with persistence_adapter.uow_factory(prepared.seal_context) as uow:
        replayed_seal = await uow.projections.seal_candidate(prepared.seal_command)
        await uow.rollback()
    async with persistence_adapter.uow_factory(prepared.activation_context) as uow:
        replayed_activation = await uow.projections.activate(
            prepared.activation_command
        )
        await uow.rollback()

    assert prepared.created.replay is False
    assert prepared.sealed.value.candidate_evidence_digest is not None
    assert prepared.sealed.value.candidate_state.value == "sealed"
    assert prepared.assembled.value.snapshot_state.value == "finalized"
    assert prepared.activated.replay is False
    assert replayed_seal.replay is True
    assert replayed_seal.result_reference == prepared.sealed.result_reference
    assert replayed_activation.replay is True
    assert replayed_activation.result_reference == prepared.activated.result_reference


@pytest.mark.asyncio
async def test_projection_stale_publication_cas_preserves_the_active_snapshot(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter)
    stale_activation = ProjectionActivationCommand.create(
        projection_kind="qa",
        projection_snapshot_id=prepared.assembled.value.projection_snapshot_id,
        activation_kind=ProjectionActivationKind.PUBLISH,
        expected_predecessor_snapshot_id=None,
        expected_predecessor_activation_generation=None,
        expected_active_pointer_cas_version=None,
        command_identity="projection-activation-stale-24000",
        idempotency_key=IdempotencyKey("projection-activation-stale-24000"),
    )

    async with persistence_adapter.uow_factory(prepared.activation_context) as uow:
        with pytest.raises(OptimisticConflictError):
            await uow.projections.activate(stale_activation)
    async with persistence_adapter.uow_factory(prepared.activation_context) as uow:
        unchanged = await uow.projections.resolve_active_publication("qa")
        await uow.rollback()

    assert isinstance(unchanged, PublishedActivePublication)
    assert (
        unchanged.projection_snapshot.projection_snapshot_id
        == prepared.assembled.value.projection_snapshot_id
    )


@pytest.mark.asyncio
async def test_projection_successor_rollback_advances_the_canonical_pointer(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter)

    async with persistence_adapter.uow_factory(prepared.activation_context) as uow:
        rolled_back = await uow.projections.activate(
            prepared.rollback_activation_command
        )
        await uow.commit()
    async with persistence_adapter.uow_factory(prepared.activation_context) as uow:
        active_after_rollback = await uow.projections.resolve_active_publication("qa")
        await uow.rollback()

    assert rolled_back.activation.activation_kind is ProjectionActivationKind.ROLLBACK
    assert rolled_back.activation.activation_generation == 2
    assert isinstance(active_after_rollback, PublishedActivePublication)
    assert (
        active_after_rollback.projection_snapshot.projection_snapshot_id
        == prepared.rollback_snapshot.value.projection_snapshot_id
    )


@pytest.mark.asyncio
async def test_projection_recovery_resolver_replays_one_shadow_rebuild_plan(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(
        persistence_adapter,
        seed=25000,
    )
    request = ProjectionRecoveryRequestEnvelope(
        event_id=UUID(int=24080),
        workspace_id=prepared.activated.activation.workspace_id,
        environment_id=prepared.activated.activation.environment_id,
        payload=ProjectionRecoveryRequestedOutboxPayload(
            command_id=UUID(int=24081),
            projection_kind="qa",
            action=ProjectionRecoveryAction.REBUILD,
            command_digest="f" * 64,
        ),
    )
    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda envelope: persistence_adapter.ingestion_context(
            envelope.workspace_id,
            envelope.environment_id,
            "projection_run",
        ),
        clock=lambda: NOW,
    )

    first = await resolver.resolve(request)
    replay = await resolver.resolve(request)

    assert replay == first
    assert first.command.operation.value == "rebuild"
    assert first.command.physical_namespace.state.value == "shadow"
    assert first.command.source_revisions
    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            request.workspace_id,
            request.environment_id,
            "projection_run",
        )
    ) as uow:
        transitions = await uow.projections.list_transitions(
            first.command.projection_run_id
        )
        partitions = await uow.projections.list_partitions(
            first.command.projection_run_id
        )
        await uow.rollback()
    assert len(transitions) == 1
    assert len(partitions) == 1
    assert partitions[0].security_domain_reference == "security-domain:default"
    assert partitions[0].security_domain_version == "r1"
    assert partitions[0].partition_identity == "default"


@pytest.mark.asyncio
async def test_projection_recovery_lifecycle_advances_once_through_complete(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter, seed=27000)
    request = ProjectionRecoveryRequestEnvelope(
        event_id=UUID(int=27080),
        workspace_id=prepared.activated.activation.workspace_id,
        environment_id=prepared.activated.activation.environment_id,
        payload=ProjectionRecoveryRequestedOutboxPayload(
            command_id=UUID(int=27081),
            projection_kind="qa",
            action=ProjectionRecoveryAction.REBUILD,
            command_digest="f" * 64,
        ),
    )
    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda envelope: persistence_adapter.ingestion_context(
            envelope.workspace_id, envelope.environment_id, "projection_run"
        ),
        clock=lambda: NOW,
    )
    plan = await resolver.resolve(request)
    lifecycle = PersistenceProjectionRecoveryLifecycle(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda command: persistence_adapter.ingestion_context(
            command.workspace_id, command.environment_id, "projection_run"
        ),
        clock=lambda: NOW,
    )

    await lifecycle.advance(plan, ProjectionRunState.BUILDING)
    await lifecycle.advance(plan, ProjectionRunState.VALIDATING)
    await lifecycle.advance(plan, ProjectionRunState.COMPLETE)
    await lifecycle.advance(plan, ProjectionRunState.COMPLETE)

    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            plan.workspace_id, plan.environment_id, "projection_run"
        )
    ) as uow:
        transitions = await uow.projections.list_transitions(
            plan.command.projection_run_id
        )
        await uow.rollback()
    assert {transition.state for transition in transitions} == {
        ProjectionRunState.REQUESTED,
        ProjectionRunState.BUILDING,
        ProjectionRunState.VALIDATING,
        ProjectionRunState.COMPLETE,
    }


class _UnusedLocatorResolver:
    async def resolve(self, request):
        raise AssertionError("accept() must not resolve already validated locators")


@pytest.mark.asyncio
async def test_projection_recovery_result_bridge_persists_and_replays_canonical_evidence(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter, seed=52000)
    request = ProjectionRecoveryRequestEnvelope(
        event_id=UUID(int=52080),
        workspace_id=prepared.activated.activation.workspace_id,
        environment_id=prepared.activated.activation.environment_id,
        payload=ProjectionRecoveryRequestedOutboxPayload(
            command_id=UUID(int=52081),
            projection_kind="qa",
            action=ProjectionRecoveryAction.REBUILD,
            command_digest="f" * 64,
        ),
    )
    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda envelope: persistence_adapter.ingestion_context(
            envelope.workspace_id, envelope.environment_id, "projection_run"
        ),
        clock=lambda: NOW,
    )
    plan = await resolver.resolve(request)
    gateway = PersistenceCanonicalProjectionGateway(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda command: persistence_adapter.ingestion_context(
            command.workspace_id, command.environment_id, "projection_receipt"
        ),
        locator_resolver=_UnusedLocatorResolver(),
    )
    source = plan.command.source_revisions[0]
    result = ProjectionResult(
        operation=PhysicalProjectionOperation.REBUILD,
        outcome=ProjectionOutcome.SUCCEEDED,
        lifecycle=ProjectionLifecycle.RECOVERED,
        command_identity=plan.command.command_identity,
        command_digest=plan.command.command_digest,
        artifact_reference="artifact:recovery:52000",
        counts=(ProjectionCount(name="chunks", value=1),),
        effects=(
            ProjectionEffect(
                effect_id="recovery-52000:1",
                operation=PhysicalProjectionOperation.REBUILD,
                action=ProjectionEffectAction.UPSERT_SOURCE,
                status=ProjectionEffectStatus.APPLIED,
                workspace_id=plan.workspace_id,
                environment_id=plan.environment_id,
                command_identity=plan.command.command_identity,
                command_digest=plan.command.command_digest,
                projection_candidate=plan.command.projection_candidate,
                projection_config=plan.command.projection_config,
                canonical_boundary=plan.command.canonical_boundary,
                physical_namespace=plan.command.physical_namespace,
                source_object_id=source.source_object_id,
                source_revision_id=source.source_revision_id,
                revision_digest=source.revision_digest,
                revision_number=source.revision_number,
                processing_receipt_id=source.processing_receipt_id,
                locator_contract_version=source.locator_contract_version,
            ),
        ),
    )
    adapter_receipt = AdapterProjectionReceipt(
        result=result,
        artifact_kind="cognee-shadow",
        artifact_integrity_digest="e" * 64,
        implementation_schema_version="cognee-context-projection:v1",
        implementation_metadata={"adapter_contract": "adapter:v1"},
        idempotency_key=f"recovery-receipt-{plan.command.command_identity}",
        completed_at=NOW,
    )
    receipt = ProjectionReceipt(
        projection_receipt_id=UUID(int=52082),
        projection_run_id=plan.command.projection_run_id,
        projection_candidate_id=plan.command.projection_candidate.candidate_id,
        projection_partition_id=plan.command.projection_candidate.partition_id,
        workspace_id=plan.workspace_id,
        environment_id=plan.environment_id,
        command_identity=plan.command.command_identity,
        command_digest=plan.command.command_digest,
        artifact_kind="cognee-shadow",
        artifact_reference="artifact:recovery:52000",
        artifact_integrity_digest="e" * 64,
        counts={"chunks": 1},
        implementation_schema_version="cognee-context-projection:v1",
        implementation_metadata={"adapter_contract": "adapter:v1"},
        idempotency_receipt_id=UUID(int=52083),
        completed_at=NOW,
    )
    reference = SourceProjectionReference(
        source_projection_reference_id=UUID(int=52084),
        projection_run_id=receipt.projection_run_id,
        projection_candidate_id=receipt.projection_candidate_id,
        projection_receipt_id=receipt.projection_receipt_id,
        workspace_id=plan.workspace_id,
        environment_id=plan.environment_id,
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        segment_id="segment-1",
        locator_schema_version=source.locator_contract_version,
        locator_reference="page:1",
        locator_digest="d" * 64,
        projected_kind="chunk",
        projected_object_id="chunk-1",
        projection_config_version_id=plan.command.projection_config.config_id,
        security_domain_reference="security-domain:default",
        created_at=NOW,
    )
    handoff = ProjectionMappingHandoff(
        command=plan.command,
        adapter_receipt=adapter_receipt,
        receipt_command=ProjectionReceiptCommand.create(
            receipt=receipt,
            idempotency_key=IdempotencyKey("recovery-result-52000"),
        ),
        source_references=(reference,),
    )

    first = await gateway.accept(handoff)
    replay = await gateway.accept(handoff)
    assert first.replay is False
    assert replay.replay is True
    assert await gateway.is_recorded(plan.command) is True
    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            plan.workspace_id, plan.environment_id, "projection_receipt"
        )
    ) as uow:
        receipts = await uow.projections.list_receipts(
            plan.command.projection_run_id
        )
        references = await uow.projections.list_source_references(
            plan.command.projection_run_id
        )
        await uow.rollback()
    assert len(receipts) == 1
    assert len(references) == 1
    assert references[0].processing_receipt_id is None
