from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

import pytest

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.boundary import CanonicalBoundaryCaptureCommand
from spine.application.persistence.projection import (
    ProjectionActivationCommand,
    ProjectionCandidateSealCommand,
    ProjectionReceiptCommand,
    ProjectionRunCommand,
    ProjectionSnapshotCommand,
    ProjectionValidationCommand,
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
from tests.contracts.persistence.test_canonical_acceptance_contract import prepare_case


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


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


@pytest.mark.asyncio
async def test_projection_candidate_seal_freezes_evidence_and_replays(
    persistence_adapter,
) -> None:
    case = await prepare_case(persistence_adapter, 24000)
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
    assert created.replay is False

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
    assert sealed.value.candidate_evidence_digest is not None
    assert sealed.value.candidate_state.value == "sealed"

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
    assert assembled.value.snapshot_state.value == "finalized"
    async with persistence_adapter.uow_factory(snapshot_context) as uow:
        persisted_snapshot = await uow.projections.resolve_snapshot(
            assembled.value.projection_snapshot_id
        )
        await uow.rollback()
    assert persisted_snapshot == assembled.value

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
    assert activated.replay is False
    assert activated.activation.activation_generation == 1
    async with persistence_adapter.uow_factory(activation_context) as uow:
        resolved = await uow.projections.resolve_active_publication("qa")
        replayed = await uow.projections.activate(activation)
        await uow.rollback()
    assert isinstance(resolved, PublishedActivePublication)
    assert resolved.freshness.value == "current"
    assert replayed.replay is True
    assert replayed.result_reference == activated.result_reference

    async with persistence_adapter.uow_factory(seal_context) as uow:
        replay = await uow.projections.seal_candidate(seal)
        await uow.rollback()
    assert replay.replay is True
    assert replay.result_reference == sealed.result_reference
