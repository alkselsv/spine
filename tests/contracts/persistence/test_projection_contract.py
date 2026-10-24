from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.ingestion import (
    IngestionBindingCommand,
    IngestionRunCommand,
    ProcessingReceiptCommand as IngestionProcessingReceiptCommand,
)
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.boundary import CanonicalBoundaryCaptureCommand
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.persistence.projection import (
    ProjectionActivationCommand,
    ProjectionCandidateSealCommand,
    ProjectionReceiptCommand,
    ProjectionRunCommand,
    ProjectionSnapshotCommand,
    ProjectionValidationCommand,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestedOutboxPayload,
    ProjectionSuccessorRequestedOutboxPayload,
)
from spine.application.projection_recovery_execution import (
    PersistenceCanonicalProjectionRecoveryResolver,
    PersistenceProjectionRecoveryLifecycle,
    ProjectionMappingError,
    ProjectionRecoveryRequestEnvelope,
)
from spine.application.projection_control import ProjectionRecoveryRequestService
from spine.application.projection_publication_faults import (
    ProjectionPublicationFaultInjected,
    ProjectionPublicationFaultReason,
)
from spine.application.projection_result_mapping import (
    AdapterProjectedObject,
    AdapterProjectionReceipt,
    AdapterSourceProjectionReference,
    CanonicalProjectionLocator,
    ProjectionMappingHandoff,
    ProjectionResultMappingBridge,
)
from spine.application.projection_result_persistence import (
    PersistenceCanonicalProjectionGateway,
)
from spine.domain.ingestion import (
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    ProcessingCoverage,
    ProcessingReceipt,
)
from spine.domain.sources import (
    AdmissionDecision,
    AdmissionDisposition,
    RevisionKind,
    content_revision,
)
from spine.domain.projections import ProjectionActivationKind, PublishedActivePublication
from spine.domain.projections import (
    OntologyVersion,
    ProjectionConfigVersion,
    ProjectionCandidateState,
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
    StableLocator,
)
from spine.workers.runtime import ProjectionPublicationCompletion
from spine.infrastructure.projection_publication_fault_injection import (
    ProjectionPublicationFaultController,
    ProjectionPublicationFaultInjectionProfile,
)
from tests.contracts.persistence.test_canonical_acceptance_contract import (
    _activate_policy,
    _audit,
    _canonical_outbox,
    _original_receipt,
    _receipt_association,
    prepare_case,
)
from tests.contracts.persistence.test_source_observation_contract import (
    provenance,
    tombstone,
)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _PreparedProjectionPublication:
    canonical_case: object
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


def _successor_request(activation, captured, *, command_id: UUID):
    return ProjectionRecoveryRequestEnvelope(
        event_id=UUID(int=command_id.int + 1),
        workspace_id=activation.workspace_id,
        environment_id=activation.environment_id,
        payload=ProjectionSuccessorRequestedOutboxPayload(
            command_id=command_id,
            projection_kind=activation.projection_kind,
            canonical_boundary_id=captured.boundary.boundary_id,
            canonical_boundary_digest=captured.boundary.manifest_digest,
            accepted_through_generation=(
                captured.boundary.accepted_through_generation
            ),
            command_digest="4" * 64,
        ),
    )


def _ontology(
    workspace_id: UUID, environment_id: UUID, projection_kind: str = "qa"
) -> OntologyVersion:
    payload = {
        "schema_version": "ontology-definition:r1",
        "entity_types": [{"name": "document"}],
        "relation_types": [],
    }
    return OntologyVersion(
        ontology_version_id=UUID(int=workspace_id.int + 40),
        workspace_id=workspace_id,
        environment_id=environment_id,
        ontology_name=projection_kind,
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
    include_shared_segment_graph_reference: bool = False,
    projection_kind: str = "qa",
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
                projection_kind=projection_kind,
                scope_version="r1",
                idempotency_key=IdempotencyKey("projection-boundary-24000"),
            )
        )
        await uow.commit()

    projection_context = persistence_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "projection_run"
    )
    ontology = _ontology(
        case.source.workspace_id,
        case.source.environment_id,
        projection_kind,
    )
    config_payload = {"extractor": "provider-neutral:v1"}
    config = ProjectionConfigVersion(
        projection_config_version_id=UUID(int=case.source.workspace_id.int + 41),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        projection_kind=projection_kind,
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
        projection_kind=projection_kind,
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
    graph_reference = reference.model_copy(
        update={
            "source_projection_reference_id": UUID(
                int=case.source.workspace_id.int + 54
            ),
            "projected_kind": "entity",
            "projected_object_id": "entity-1",
        }
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
        if include_shared_segment_graph_reference:
            await uow.projections.add_source_reference(graph_reference)
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
                            },
                            *(
                                [
                                    {
                                        "reference_id": str(
                                            graph_reference.source_projection_reference_id
                                        ),
                                        "source_object_id": str(
                                            graph_reference.source_object_id
                                        ),
                                        "source_revision_id": str(
                                            graph_reference.source_revision_id
                                        ),
                                        "segment_id": graph_reference.segment_id,
                                        "locator_digest": graph_reference.locator_digest,
                                        "projected_kind": graph_reference.projected_kind,
                                        "projected_object_id": (
                                            graph_reference.projected_object_id
                                        ),
                                    }
                                ]
                                if include_shared_segment_graph_reference
                                else []
                            ),
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
        projection_kind=projection_kind,
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
        canonical_case=case,
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
    prepared = await _prepare_projection_publication(
        persistence_adapter,
        include_shared_segment_graph_reference=True,
    )

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
    run = prepared.created.value
    context = persistence_adapter.ingestion_context(
        run.workspace_id, run.environment_id, "projection_receipt"
    )
    async with persistence_adapter.uow_factory(context) as uow:
        references = await uow.projections.list_source_references(
            run.projection_run_id
        )
    assert {reference.projected_kind for reference in references} == {
        "chunk",
        "entity",
    }


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


async def _accept_additional_canonical_source(
    persistence_adapter,
    prepared: _PreparedProjectionPublication,
    *,
    seed: int,
):
    case = prepared.canonical_case
    source = case.source.model_copy(
        update={
            "source_object_id": UUID(int=seed),
            "upload_identity": f"projection-mixed-format-{seed}",
        }
    )
    original_reference = case.revision.original_reference
    assert original_reference is not None
    revision_values = case.revision.model_dump(
        mode="python", exclude={"revision_digest"}
    )
    revision_values.update(
        revision_id=UUID(int=seed + 1),
        source_object_id=source.source_object_id,
        original_reference=original_reference.model_copy(
            update={
                "object_id": UUID(int=seed + 2),
                "storage_generation": UUID(int=seed + 3),
            }
        ),
    )
    revision = content_revision(**revision_values)
    provenance = case.provenance.model_copy(
        update={
            "provenance_id": UUID(int=seed + 4),
            "source_object_id": source.source_object_id,
            "revision_id": revision.revision_id,
            "event_identity": f"projection-mixed-format-{seed}",
        }
    )
    source_context = persistence_adapter.source_context(
        source.workspace_id, source.environment_id
    )
    original_receipt_id = await _original_receipt(
        persistence_adapter, source_context, seed
    )
    association = _receipt_association(
        source,
        revision,
        provenance,
        receipt_id=original_receipt_id,
        number=seed,
    )
    observation = SourceObservationCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        idempotency_key=IdempotencyKey(f"projection-mixed-observation-{seed}"),
        receipt_association=association,
    )
    async with persistence_adapter.uow_factory(source_context) as uow:
        await uow.sources.record_observation(observation)
        await uow.commit()

    policy_context = persistence_adapter.ingestion_context(
        source.workspace_id, source.environment_id, "access_policy_activation"
    )
    policy_id = await _activate_policy(
        persistence_adapter,
        context=policy_context,
        source_object=source,
        number=seed,
    )
    canonical_context = persistence_adapter.ingestion_context(
        source.workspace_id, source.environment_id, "canonical_acceptance"
    )
    admission_id = UUID(int=seed + 5)
    async with persistence_adapter.uow_factory(canonical_context) as uow:
        await uow.admissions.append_decision(
            AdmissionDecision(
                decision_id=admission_id,
                source_object_id=source.source_object_id,
                revision_id=revision.revision_id,
                workspace_id=source.workspace_id,
                environment_id=source.environment_id,
                decision_version=1,
                disposition=AdmissionDisposition.ADMITTED,
                validator_profile_version="admission:r1",
                reason_schema_version="reason.v1",
                reason_codes=("clean",),
                authority_reference={
                    "reference_type": "validator",
                    "reference_id": UUID(int=seed + 6),
                    "schema_version": 1,
                },
                trace_id=canonical_context.trace_id,
                audit_event_id=UUID(int=seed + 7),
                decided_at=NOW,
            )
        )
        await uow.commit()
    acceptance_id = UUID(int=seed + 8)
    audit_id = UUID(int=seed + 9)
    outbox_id = UUID(int=seed + 10)
    acceptance = CanonicalAcceptanceCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        expected_current_revision_id=None,
        idempotency_key=IdempotencyKey(f"projection-mixed-acceptance-{seed}"),
        canonical_acceptance_id=acceptance_id,
        admission_decision_id=admission_id,
        access_policy_version_id=policy_id,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=_audit(
            context=canonical_context,
            source_revision_id=revision.revision_id,
            event_id=audit_id,
            reason="source_accepted",
        ),
        outbox_intent=_canonical_outbox(
            persistence_adapter,
            context=canonical_context,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            acceptance_id=acceptance_id,
            generation=2,
            audit_event_id=audit_id,
            event_id=outbox_id,
        ),
    )
    async with persistence_adapter.uow_factory(canonical_context) as uow:
        await uow.canonical.accept(acceptance)
        await uow.commit()
    return source, revision, provenance


async def _record_projection_processing_receipts(
    persistence_adapter,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    sources: tuple[tuple[object, object, object, str], ...],
) -> tuple[ProcessingReceipt, ...]:
    run = IngestionRun(
        ingestion_run_id=UUID(int=58120),
        workspace_id=workspace_id,
        environment_id=environment_id,
        command_identity="projection-mixed-run",
        command_digest="1" * 64,
        trigger_kind="reprocess",
        requested_scope_schema="ingestion-scope:v1",
        requested_scope_digest="2" * 64,
        requested_at=NOW,
    )
    context = persistence_adapter.ingestion_context(
        workspace_id, environment_id, "ingestion_run"
    )
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.ingestion.create_run(
            IngestionRunCommand.create(
                run=run,
                idempotency_key=IdempotencyKey("projection-mixed-run-58120"),
            )
        )
        for index, (source, revision, _, _) in enumerate(sources, start=1):
            item = IngestionRunSource(
                ingestion_run_source_id=UUID(int=58120 + index),
                ingestion_run_id=run.ingestion_run_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                input_ordinal=index,
                input_identity=f"projection-mixed-input-{index}",
                input_digest=str(index) * 64,
                command_identity=f"projection-mixed-item-{index}",
                command_digest=str(index + 2) * 64,
                created_at=NOW,
            )
            await uow.ingestion.create_item(item)
        await uow.commit()

    receipts = []
    for index, (source, revision, provenance, locator_contract) in enumerate(
        sources, start=1
    ):
        item_id = UUID(int=58120 + index)
        binding = IngestionRunSourceBinding(
            binding_id=UUID(int=58130 + index),
            ingestion_run_source_id=item_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            binding_command_identity=f"projection-mixed-binding-{index}",
            binding_command_digest="4" * 64,
            original_reference=revision.original_reference,
            original_receipt_kind="issue6.write_receipt",
            original_receipt_id=await _original_receipt(
                persistence_adapter,
                persistence_adapter.source_context(workspace_id, environment_id),
                58300 + index,
            ),
            original_receipt_digest=revision.original_sha256,
            bound_at=NOW,
        )
        item = IngestionRunSource(
            ingestion_run_source_id=item_id,
            ingestion_run_id=run.ingestion_run_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            input_ordinal=index,
            input_identity=f"projection-mixed-input-{index}",
            input_digest=str(index) * 64,
            command_identity=f"projection-mixed-item-{index}",
            command_digest=str(index + 2) * 64,
            created_at=NOW,
        )
        binding_context = persistence_adapter.ingestion_context(
            workspace_id, environment_id, "ingestion_binding"
        )
        binding_provenance = provenance.model_copy(
            update={
                "provenance_id": UUID(int=58310 + index),
                "event_identity": f"projection-mixed-binding-{index}",
            }
        )
        association = _receipt_association(
            source,
            revision,
            binding_provenance,
            receipt_id=binding.original_receipt_id,
            number=58320 + index,
        )
        observation = SourceObservationCommand.create(
            source=source,
            revision=revision,
            provenance=binding_provenance,
            idempotency_key=IdempotencyKey(
                f"projection-mixed-binding-observation-{index}"
            ),
            receipt_association=association,
        )
        async with persistence_adapter.uow_factory(binding_context) as uow:
            await uow.ingestion.bind_source(
                IngestionBindingCommand.create(
                    item=item,
                    binding=binding,
                    observation=observation,
                    idempotency_key=IdempotencyKey(
                        f"projection-mixed-binding-{index}"
                    ),
                )
            )
            await uow.commit()
        receipt = ProcessingReceipt(
            processing_receipt_id=UUID(int=58140 + index),
            workspace_id=workspace_id,
            environment_id=environment_id,
            ingestion_run_source_id=item_id,
            binding_id=binding.binding_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            processor_kind="document-ingestion",
            parser_name="synthetic",
            parser_version="parser:v1",
            parser_config_reference="parser-default:v1",
            locator_contract_version=locator_contract,
            original_sha256=revision.original_sha256,
            output_kind="parsed-document",
            output_reference=UUID(int=58150 + index),
            output_digest="5" * 64,
            coverage=ProcessingCoverage.COMPLETE,
            command_identity=f"projection-mixed-processing-{index}",
            command_digest="6" * 64,
            idempotency_receipt_id=UUID(int=58160 + index),
            completed_at=NOW,
        )
        receipt_context = persistence_adapter.ingestion_context(
            workspace_id, environment_id, "processing_receipt"
        )
        async with persistence_adapter.uow_factory(receipt_context) as uow:
            result = await uow.ingestion.record_processing_receipt(
                IngestionProcessingReceiptCommand.create(
                    receipt=receipt,
                    idempotency_key=IdempotencyKey(
                        f"projection-mixed-processing-{index}"
                    ),
                )
            )
            await uow.commit()
        receipts.append(result.value)
    return tuple(receipts)


@pytest.mark.asyncio
async def test_projection_successor_resolver_accepts_canonical_multi_format_boundary(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(
        persistence_adapter,
        seed=58100,
    )
    activation = prepared.activated.activation
    second_source, second_revision, second_provenance = await _accept_additional_canonical_source(
        persistence_adapter,
        prepared,
        seed=58200,
    )
    receipt, second_receipt = await _record_projection_processing_receipts(
        persistence_adapter,
        workspace_id=activation.workspace_id,
        environment_id=activation.environment_id,
        sources=(
            (
                prepared.canonical_case.source,
                prepared.canonical_case.revision,
                prepared.canonical_case.provenance,
                "pdf-page-extracted-codepoint:v1",
            ),
            (
                second_source,
                second_revision,
                second_provenance,
                "physical-line-codepoint:v1",
            ),
        ),
    )

    captured_intents = []

    class Outbox:
        def __init__(self, inner):
            self._inner = inner

        async def append(self, intent):
            captured_intents.append(intent)
            return await self._inner.append(intent)

        async def resolve_delivery_status(self, event_id):
            return await self._inner.resolve_delivery_status(event_id)

    class RequestUnitOfWork:
        def __init__(self, inner):
            self._inner = inner

        async def __aenter__(self):
            await self._inner.__aenter__()
            self.idempotency = self._inner.idempotency
            self.boundaries = self._inner.boundaries
            self.projections = self._inner.projections
            self.outbox = Outbox(self._inner.outbox)
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            await self._inner.__aexit__(exc_type, exc_value, traceback)

        async def commit(self):
            await self._inner.commit()

        async def rollback(self):
            await self._inner.rollback()

    successor_service = ProjectionRecoveryRequestService(
        uow_factory=lambda context: RequestUnitOfWork(
            persistence_adapter.uow_factory(context)
        ),
        outbox_events=persistence_adapter.outbox_events,
    )
    successor_context = persistence_adapter.ingestion_context(
        activation.workspace_id,
        activation.environment_id,
        "request_projection_recovery",
    )
    accepted = await successor_service.request(
        context=successor_context,
        projection_kind="qa",
        action=ProjectionRecoveryAction.SUCCESSOR,
        idempotency_key=IdempotencyKey("projection-successor-public-path-58100"),
    )
    assert accepted.intent_id is not None
    intent = next(
        value for value in captured_intents if value.event_id == accepted.intent_id
    )
    assert intent is not None
    assert isinstance(intent.payload, ProjectionSuccessorRequestedOutboxPayload)
    request = ProjectionRecoveryRequestEnvelope(
        event_id=accepted.intent_id,
        workspace_id=activation.workspace_id,
        environment_id=activation.environment_id,
        payload=intent.payload,
    )
    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda envelope: persistence_adapter.ingestion_context(
            envelope.workspace_id, envelope.environment_id, "projection_run"
        ),
        clock=lambda: NOW,
    )

    plan = await resolver.resolve(request)

    assert plan.action is ProjectionRecoveryAction.SUCCESSOR
    assert plan.command.canonical_boundary.boundary_id == request.payload.canonical_boundary_id
    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            activation.workspace_id,
            activation.environment_id,
            "canonical_boundary_capture",
        )
    ) as uow:
        members = await uow.boundaries.resolve_members(
            request.payload.canonical_boundary_id
        )
        await uow.rollback()
    assert len(members) == 2
    member_by_source = {value.source_object_id: value for value in members}
    member = member_by_source[prepared.canonical_case.source.source_object_id]
    second_member = member_by_source[second_source.source_object_id]
    source_revisions = {
        value.source_revision_id: value for value in plan.command.source_revisions
    }
    assert source_revisions[member.source_revision_id].processing_receipt_id == receipt.processing_receipt_id
    assert source_revisions[member.source_revision_id].locator_contract_version == "pdf-page-extracted-codepoint:v1"
    assert source_revisions[second_member.source_revision_id].processing_receipt_id == second_receipt.processing_receipt_id
    assert source_revisions[second_member.source_revision_id].locator_contract_version == "physical-line-codepoint:v1"

    locators = tuple(
        CanonicalProjectionLocator(
            source_object_id=source.source_object_id,
            source_revision_id=source.source_revision_id,
            revision_digest=source.revision_digest,
            processing_receipt_id=source.processing_receipt_id,
            segment_id=f"segment-{index}",
            locator_schema_version=source.locator_contract_version,
            locator=StableLocator(
                kind="page" if index == 1 else "character_range",
                value="page:1:0-4" if index == 1 else "line:1:0-4",
            ),
            locator_digest=str(index + 6) * 64,
        )
        for index, source in enumerate(plan.command.source_revisions, start=1)
    )

    class LocatorResolver:
        async def resolve(self, evidence_request):
            assert evidence_request.command == plan.command
            return locators

    gateway = PersistenceCanonicalProjectionGateway(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda command: persistence_adapter.ingestion_context(
            command.workspace_id, command.environment_id, "projection_receipt"
        ),
        locator_resolver=LocatorResolver(),
    )
    references = tuple(
        AdapterSourceProjectionReference(
            workspace_id=plan.workspace_id,
            environment_id=plan.environment_id,
            namespace_digest=plan.command.physical_namespace.namespace_digest,
            source_object_id=locator.source_object_id,
            source_revision_id=locator.source_revision_id,
            revision_digest=locator.revision_digest,
            processing_receipt_id=locator.processing_receipt_id,
            segment_id=locator.segment_id,
            locator_schema_version=locator.locator_schema_version,
            locator=locator.locator,
            locator_digest=locator.locator_digest,
            projected_kind="chunk",
            projected_object_id=f"chunk-{index}",
            projection_config_version_id=plan.command.projection_config.config_id,
            security_domain_reference="security-domain:default",
            security_domain_version="r1",
        )
        for index, locator in enumerate(locators, start=1)
    )
    effects = tuple(
        ProjectionEffect(
            effect_id=f"projection-mixed-effect-{index}",
            operation=plan.command.operation,
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
        )
        for index, source in enumerate(plan.command.source_revisions, start=1)
    )
    adapter_receipt = AdapterProjectionReceipt(
        result=ProjectionResult(
            operation=plan.command.operation,
            outcome=ProjectionOutcome.SUCCEEDED,
            lifecycle=ProjectionLifecycle.SUCCEEDED,
            command_identity=plan.command.command_identity,
            command_digest=plan.command.command_digest,
            artifact_reference="artifact:projection-mixed-58100",
            counts=(ProjectionCount(name="chunks", value=2),),
            effects=effects,
        ),
        artifact_kind="cognee-shadow",
        artifact_integrity_digest="9" * 64,
        emitted_objects=tuple(
            AdapterProjectedObject(kind="chunk", object_id=f"chunk-{index}")
            for index in range(1, 3)
        ),
        source_references=references,
        implementation_schema_version="cognee-context-projection:v1",
        implementation_metadata={"adapter_contract": "adapter:v1"},
        idempotency_key="projection-mixed-mapping-58100",
        completed_at=NOW,
    )
    mapped = await ProjectionResultMappingBridge(
        gateway,
        id_source=iter((UUID(int=58170), UUID(int=58171))).__next__,
    ).map(plan.command, adapter_receipt)
    mapped_lineage = {
        value.source_revision_id: value
        for value in mapped.handoff.source_references
    }
    assert mapped_lineage[member.source_revision_id].processing_receipt_id == receipt.processing_receipt_id
    assert mapped_lineage[member.source_revision_id].locator_schema_version == "pdf-page-extracted-codepoint:v1"
    assert mapped_lineage[second_member.source_revision_id].processing_receipt_id == second_receipt.processing_receipt_id
    assert mapped_lineage[second_member.source_revision_id].locator_schema_version == "physical-line-codepoint:v1"
    async with persistence_adapter.uow_factory(
        persistence_adapter.ingestion_context(
            activation.workspace_id, activation.environment_id, "projection_run"
        )
    ) as uow:
        run = await uow.projections.resolve_run(plan.command.projection_run_id)
        persisted_references = await uow.projections.list_source_references(
            plan.command.projection_run_id
        )
        await uow.rollback()
    assert run is not None
    assert run.operation is ProjectionOperation.CATCH_UP
    assert run.predecessor_snapshot_id == prepared.assembled.value.projection_snapshot_id
    persisted_lineage = {
        value.source_revision_id: value for value in persisted_references
    }
    assert persisted_lineage == mapped_lineage
    status = await successor_service.successor_status(
        context=persistence_adapter.ingestion_context(
            activation.workspace_id,
            activation.environment_id,
            "inspect_projection_status",
        ),
        projection_kind="qa",
        command_id=request.payload.command_id,
    )
    assert status.status == "running"
    assert status.run_id == plan.command.projection_run_id
    assert status.candidate_id == plan.command.projection_candidate.candidate_id


@pytest.mark.asyncio
async def test_projection_successor_public_resolver_rejects_changed_head(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter, seed=26100)
    activation = prepared.activated.activation
    context = persistence_adapter.ingestion_context(
        activation.workspace_id, activation.environment_id, "projection_run"
    )
    capture_context = persistence_adapter.ingestion_context(
        activation.workspace_id,
        activation.environment_id,
        "canonical_boundary_capture",
    )
    async with persistence_adapter.uow_factory(capture_context) as uow:
        captured = await uow.boundaries.capture(
            CanonicalBoundaryCaptureCommand.create(
                projection_kind="qa",
                scope_version="r1",
                idempotency_key=IdempotencyKey("successor-stale-boundary-26100"),
            )
        )
        current = await uow.canonical.resolve_current(captured.members[0].source_object_id)
        await uow.commit()
    assert current is not None

    class Canonical:
        async def resolve_current(self, source_object_id):
            assert source_object_id == captured.members[0].source_object_id
            return current.model_copy(
                update={"current_revision_id": UUID(int=26199)}
            )

    class UnitOfWork:
        def __init__(self, inner):
            self._inner = inner
            self.canonical = Canonical()

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            await self._inner.__aexit__(exc_type, exc_value, traceback)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    request = _successor_request(activation, captured, command_id=UUID(int=26198))
    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=lambda trusted: UnitOfWork(
            persistence_adapter.uow_factory(trusted)
        ),
        context_factory=lambda _: context,
        clock=lambda: NOW,
    )
    with pytest.raises(ProjectionMappingError, match="stale"):
        await resolver.resolve(request)


@pytest.mark.asyncio
async def test_projection_successor_public_resolver_rejects_foreign_boundary_member(
    persistence_adapter,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter, seed=26120)
    activation = prepared.activated.activation
    context = persistence_adapter.ingestion_context(
        activation.workspace_id, activation.environment_id, "projection_run"
    )
    capture_context = persistence_adapter.ingestion_context(
        activation.workspace_id,
        activation.environment_id,
        "canonical_boundary_capture",
    )
    async with persistence_adapter.uow_factory(capture_context) as uow:
        captured = await uow.boundaries.capture(
            CanonicalBoundaryCaptureCommand.create(
                projection_kind="qa",
                scope_version="r1",
                idempotency_key=IdempotencyKey("successor-foreign-boundary-26120"),
            )
        )
        await uow.commit()

    class Boundaries:
        def __init__(self, inner):
            self._inner = inner

        async def resolve(self, boundary_id):
            return await self._inner.resolve(boundary_id)

        async def resolve_members(self, boundary_id):
            members = await self._inner.resolve_members(boundary_id)
            return (
                members[0].model_copy(update={"workspace_id": UUID(int=26129)}),
            )

    class UnitOfWork:
        def __init__(self, inner):
            self._inner = inner
            self.boundaries = None

        async def __aenter__(self):
            await self._inner.__aenter__()
            self.boundaries = Boundaries(self._inner.boundaries)
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            await self._inner.__aexit__(exc_type, exc_value, traceback)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=lambda trusted: UnitOfWork(
            persistence_adapter.uow_factory(trusted)
        ),
        context_factory=lambda _: context,
        clock=lambda: NOW,
    )

    with pytest.raises(ProjectionMappingError, match="foreign"):
        await resolver.resolve(
            _successor_request(activation, captured, command_id=UUID(int=26128))
        )


@pytest.mark.parametrize("receipt_count", [0, 2], ids=["missing", "ambiguous"])
@pytest.mark.asyncio
async def test_projection_successor_public_resolver_rejects_non_unique_receipt(
    persistence_adapter,
    receipt_count: int,
) -> None:
    prepared = await _prepare_projection_publication(persistence_adapter, seed=26140)
    activation = prepared.activated.activation
    context = persistence_adapter.ingestion_context(
        activation.workspace_id, activation.environment_id, "projection_run"
    )
    capture_context = persistence_adapter.ingestion_context(
        activation.workspace_id,
        activation.environment_id,
        "canonical_boundary_capture",
    )
    async with persistence_adapter.uow_factory(capture_context) as uow:
        captured = await uow.boundaries.capture(
            CanonicalBoundaryCaptureCommand.create(
                projection_kind="qa",
                scope_version="r1",
                idempotency_key=IdempotencyKey("successor-receipt-boundary-26140"),
            )
        )
        member = captured.members[0]
        revision = await uow.sources.resolve_revision(member.source_revision_id)
        await uow.commit()
    assert revision is not None
    bindings = tuple(
        IngestionRunSourceBinding(
            binding_id=UUID(int=26141 + index),
            ingestion_run_source_id=UUID(int=26151 + index),
            source_object_id=member.source_object_id,
            source_revision_id=member.source_revision_id,
            workspace_id=activation.workspace_id,
            environment_id=activation.environment_id,
            binding_command_identity=f"successor-binding-26140-{index}",
            binding_command_digest="1" * 64,
            bound_at=NOW,
        )
        for index in range(receipt_count)
    )
    receipts = {
        binding.ingestion_run_source_id: ProcessingReceipt(
            processing_receipt_id=UUID(int=26161 + index),
            workspace_id=activation.workspace_id,
            environment_id=activation.environment_id,
            ingestion_run_source_id=binding.ingestion_run_source_id,
            binding_id=binding.binding_id,
            source_object_id=member.source_object_id,
            source_revision_id=member.source_revision_id,
            processor_kind="document-ingestion",
            parser_name="text",
            parser_version="parser:v1",
            parser_config_reference="parser-default:v1",
            locator_contract_version="locator:v1",
            original_sha256=revision.original_sha256,
            output_kind="parsed-document",
            output_reference=UUID(int=26171 + index),
            output_digest="2" * 64,
            coverage=ProcessingCoverage.COMPLETE,
            command_identity=f"successor-processing-26140-{index}",
            command_digest="3" * 64,
            idempotency_receipt_id=UUID(int=26181 + index),
            completed_at=NOW,
        )
        for index, binding in enumerate(bindings)
    }

    class Ingestion:
        async def list_bindings_for_source_revision(self, *args):
            del args
            return bindings

        async def resolve_processing_receipt(self, item_id):
            return receipts.get(item_id)

    class UnitOfWork:
        def __init__(self, inner):
            self._inner = inner
            self.ingestion = Ingestion()

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            await self._inner.__aexit__(exc_type, exc_value, traceback)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=lambda trusted: UnitOfWork(
            persistence_adapter.uow_factory(trusted)
        ),
        context_factory=lambda _: context,
        clock=lambda: NOW,
    )

    with pytest.raises(ProjectionMappingError, match="missing or ambiguous"):
        await resolver.resolve(
            _successor_request(activation, captured, command_id=UUID(int=26188))
        )


async def _prepare_projection_successor_tombstone_case(
    persistence_adapter,
    *,
    seed: int,
):
    prepared = await _prepare_projection_publication(
        persistence_adapter,
        seed=seed,
        projection_kind="context",
    )
    activation = prepared.activated.activation
    case = prepared.canonical_case
    context = persistence_adapter.ingestion_context(
        activation.workspace_id, activation.environment_id, "projection_run"
    )
    tombstone_revision = tombstone(case.source, UUID(int=seed + 99))
    tombstone_provenance = provenance(
        case.source, tombstone_revision.revision_id, "event-2"
    )
    source_context = persistence_adapter.source_context(
        activation.workspace_id, activation.environment_id
    )
    async with persistence_adapter.uow_factory(source_context) as uow:
        await uow.sources.record_observation(
            SourceObservationCommand.create(
                source=case.source,
                revision=tombstone_revision,
                provenance=tombstone_provenance,
                idempotency_key=IdempotencyKey(
                    f"successor-tombstone-observation-{seed}"
                ),
            )
        )
        await uow.commit()
    audit_id = UUID(int=seed + 96)
    outbox_id = UUID(int=seed + 97)
    acceptance_id = UUID(int=seed + 95)
    tombstone_acceptance = CanonicalAcceptanceCommand.create(
        source=case.source,
        revision=tombstone_revision,
        provenance=tombstone_provenance,
        expected_current_revision_id=case.revision.revision_id,
        idempotency_key=IdempotencyKey(
            f"successor-tombstone-acceptance-{seed}"
        ),
        canonical_acceptance_id=acceptance_id,
        admission_decision_id=None,
        access_policy_version_id=None,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=_audit(
            context=case.context,
            source_revision_id=tombstone_revision.revision_id,
            event_id=audit_id,
            reason="source_tombstoned",
            transition="source.tombstoned",
        ),
        outbox_intent=_canonical_outbox(
            persistence_adapter,
            context=case.context,
            source_object_id=case.source.source_object_id,
            source_revision_id=tombstone_revision.revision_id,
            acceptance_id=acceptance_id,
            generation=2,
            audit_event_id=audit_id,
            event_id=outbox_id,
            transition="source.tombstoned",
        ),
    )
    async with persistence_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(tombstone_acceptance)
        await uow.commit()
    capture_context = persistence_adapter.ingestion_context(
        activation.workspace_id,
        activation.environment_id,
        "canonical_boundary_capture",
    )
    async with persistence_adapter.uow_factory(capture_context) as uow:
        captured = await uow.boundaries.capture(
            CanonicalBoundaryCaptureCommand.create(
                projection_kind="context",
                scope_version="r1",
                idempotency_key=IdempotencyKey(
                    f"successor-tombstone-boundary-{seed}"
                ),
            )
        )
        await uow.commit()
    request = _successor_request(
        activation, captured, command_id=UUID(int=seed + 98)
    )
    resolver = PersistenceCanonicalProjectionRecoveryResolver(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda _: context,
        clock=lambda: NOW,
    )

    plan = await resolver.resolve(request)
    assert plan.command.source_revisions == ()

    result = ProjectionResult(
        operation=PhysicalProjectionOperation.REBUILD,
        outcome=ProjectionOutcome.SUCCEEDED,
        lifecycle=ProjectionLifecycle.RECOVERED,
        command_identity=plan.command.command_identity,
        command_digest=plan.command.command_digest,
        artifact_reference="artifact:successor:tombstone",
        counts=(),
        effects=(),
    )
    adapter_receipt = AdapterProjectionReceipt(
        result=result,
        artifact_kind="cognee-shadow",
        artifact_integrity_digest="e" * 64,
        implementation_schema_version="cognee-context-projection:v1",
        implementation_metadata={"adapter_contract": "adapter:v1"},
        idempotency_key=f"successor-tombstone-receipt-{seed}",
        completed_at=NOW,
    )
    receipt = ProjectionReceipt(
        projection_receipt_id=UUID(int=seed + 90),
        projection_run_id=plan.command.projection_run_id,
        projection_candidate_id=plan.command.projection_candidate.candidate_id,
        projection_partition_id=plan.command.projection_candidate.partition_id,
        workspace_id=plan.workspace_id,
        environment_id=plan.environment_id,
        command_identity=plan.command.command_identity,
        command_digest=plan.command.command_digest,
        artifact_kind="cognee-shadow",
        artifact_reference="artifact:successor:tombstone",
        artifact_integrity_digest="e" * 64,
        counts={},
        implementation_schema_version="cognee-context-projection:v1",
        implementation_metadata={"adapter_contract": "adapter:v1"},
        idempotency_receipt_id=UUID(int=seed + 91),
        completed_at=NOW,
    )
    handoff = ProjectionMappingHandoff(
        command=plan.command,
        adapter_receipt=adapter_receipt,
        receipt_command=ProjectionReceiptCommand.create(
            receipt=receipt,
            idempotency_key=IdempotencyKey(
                f"successor-tombstone-mapping-{seed}"
            ),
        ),
        source_references=(),
    )
    gateway = PersistenceCanonicalProjectionGateway(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda command: persistence_adapter.ingestion_context(
            command.workspace_id, command.environment_id, "projection_receipt"
        ),
        locator_resolver=_UnusedLocatorResolver(),
    )
    mapping = await gateway.accept(handoff)

    class PhysicalStore:
        def __init__(self) -> None:
            self.reads = 0

        async def physical_artifacts_for(self, **kwargs):
            del kwargs
            self.reads += 1
            return ()

        def data_id_for(self, **kwargs):
            raise AssertionError("tombstone projection has no physical artifacts")

    class ArtifactResolver:
        async def document_for(self, command, processing_receipt_id):
            del command, processing_receipt_id
            raise AssertionError("tombstone projection has no parsed content")

    return SimpleNamespace(
        prepared=prepared,
        plan=plan,
        mapping=mapping,
        context=context,
        physical_store=PhysicalStore(),
        artifact_resolver=ArtifactResolver(),
    )


def _projection_fault_controller(persistence_adapter, case, *, boundary: str):
    reasons = {
        "before_physical_validation": (
            "projection.qualification.before_physical_validation"
        ),
        "after_validation_before_activation": (
            "projection.qualification.after_validation_before_activation"
        ),
    }
    fault_profile = ProjectionPublicationFaultInjectionProfile.model_validate(
        {
            "profile_id": "qualification-issue578-contract",
            "profile_version": "v1",
            "commit_sha": "a" * 40,
            "deployment_fingerprint": "b" * 64,
            "provider_profile_digest": "c" * 64,
            "workspace_id": case.plan.command.workspace_id,
            "environment_id": case.plan.command.environment_id,
            "projection_kind": "context",
            "successor_command_identity": case.plan.command.command_identity,
            "rules": (
                {
                    "boundary": boundary,
                    "reason_code": reasons[boundary],
                },
            ),
        }
    )
    return ProjectionPublicationFaultController(
        fault_profile,
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda workspace_id, environment_id, operation, run_id: (
            persistence_adapter.ingestion_context(
                workspace_id, environment_id, operation
            )
        ),
        clock=lambda: NOW,
    )


def _projection_completion(persistence_adapter, case, *, fault_controller=None):
    return ProjectionPublicationCompletion(
        uow_factory=persistence_adapter.uow_factory,
        context_factory=lambda workspace_id, environment_id, operation, run_id: (
            persistence_adapter.ingestion_context(
                workspace_id, environment_id, operation
            )
        ),
        artifact_resolver=case.artifact_resolver,
        physical_store=case.physical_store,
        clock=lambda: NOW,
        fault_injector=fault_controller,
    )


@pytest.mark.asyncio
async def test_projection_successor_public_resolver_retains_tombstone(
    persistence_adapter,
) -> None:
    case = await _prepare_projection_successor_tombstone_case(
        persistence_adapter, seed=26200
    )
    completion = _projection_completion(persistence_adapter, case)

    await completion.complete(case.mapping)

    async with persistence_adapter.uow_factory(case.context) as uow:
        publication = await uow.projections.resolve_active_publication("context")
        assert isinstance(publication, PublishedActivePublication)
        members = await uow.projections.list_snapshot_members(
            publication.projection_snapshot.projection_snapshot_id
        )
        await uow.rollback()
    assert publication.projection_kind == "context"
    assert publication.projection_snapshot.projection_kind == "context"
    assert len(members) == 1
    assert members[0].disposition is ProjectionSnapshotMemberDisposition.TOMBSTONED


@pytest.mark.asyncio
async def test_projection_pre_validation_fault_avoids_physical_read_and_preserves_active(
    persistence_adapter,
) -> None:
    case = await _prepare_projection_successor_tombstone_case(
        persistence_adapter, seed=26300
    )
    controller = _projection_fault_controller(
        persistence_adapter, case, boundary="before_physical_validation"
    )
    completion = _projection_completion(
        persistence_adapter, case, fault_controller=controller
    )

    with pytest.raises(ProjectionPublicationFaultInjected) as before_validation:
        await completion.complete(case.mapping)
    assert (
        before_validation.value.reason
        is ProjectionPublicationFaultReason.BEFORE_PHYSICAL_VALIDATION
    )
    async with persistence_adapter.uow_factory(case.context) as uow:
        active = await uow.projections.resolve_active_publication("context")
        run_after_first_fault = await uow.projections.resolve_run(
            case.plan.command.projection_run_id
        )
        await uow.rollback()
    assert case.physical_store.reads == 0
    assert isinstance(active, PublishedActivePublication)
    assert (
        active.projection_snapshot.projection_snapshot_id
        == case.prepared.assembled.value.projection_snapshot_id
    )
    assert run_after_first_fault.candidate_state is not ProjectionCandidateState.SEALED


@pytest.mark.asyncio
async def test_projection_post_validation_fault_retains_seal_and_preserves_active(
    persistence_adapter,
) -> None:
    case = await _prepare_projection_successor_tombstone_case(
        persistence_adapter, seed=26400
    )
    controller = _projection_fault_controller(
        persistence_adapter,
        case,
        boundary="after_validation_before_activation",
    )
    completion = _projection_completion(
        persistence_adapter, case, fault_controller=controller
    )

    with pytest.raises(ProjectionPublicationFaultInjected) as before_activation:
        await completion.complete(case.mapping)
    assert (
        before_activation.value.reason
        is ProjectionPublicationFaultReason.AFTER_VALIDATION_BEFORE_ACTIVATION
    )
    async with persistence_adapter.uow_factory(case.context) as uow:
        active = await uow.projections.resolve_active_publication("context")
        run_after_second_fault = await uow.projections.resolve_run(
            case.plan.command.projection_run_id
        )
        await uow.rollback()
    assert case.physical_store.reads == 1
    assert isinstance(active, PublishedActivePublication)
    assert (
        active.projection_snapshot.projection_snapshot_id
        == case.prepared.assembled.value.projection_snapshot_id
    )
    assert run_after_second_fault.candidate_state is ProjectionCandidateState.SEALED


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
