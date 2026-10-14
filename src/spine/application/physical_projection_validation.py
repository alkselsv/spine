"""Bridge physical adapter validation into the canonical projection boundary.

The physical validator owns observations about a derived namespace.  This
module turns those observations, together with the already accepted #208
mapping, into the typed #88 validation and reconciliation commands.  It never
changes a snapshot or selects an active publication.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from hashlib import sha256
import json
from types import TracebackType
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.projection import (
    ProjectionCandidateSealCommand,
    ProjectionMutationResult,
    PROJECTION_PHYSICAL_VALIDATION_GATE_KIND,
    PROJECTION_PHYSICAL_VALIDATION_GATE_VERSION,
    ProjectionReconciliationCommand,
    ProjectionRepository,
    ProjectionValidationCommand,
)
from spine.application.projection_result_mapping import (
    ProjectionMappingResult,
    ProjectionMappingError,
)
from spine.domain.common import DefinitionModel
from spine.domain.projections import (
    ProjectionActivation,
    ProjectionReconciliationOutcome,
    ProjectionReconciliationResult,
    ProjectionSnapshot,
    ProjectionSnapshotState,
    ProjectionValidation,
    ProjectionValidationDecision,
    projection_reconciliation_findings_digest,
)
from spine.memory.contracts import (
    ProjectionLifecycle,
    ProjectionRecoveryEvidence,
    ProjectionRecoveryOperation,
    ProjectionRecoveryStatus,
)
from spine.memory.validation import (
    ArtifactKind,
    PhysicalProjectionExpectation,
    PhysicalValidationResult,
)


PHYSICAL_VALIDATION_GATE_KIND = PROJECTION_PHYSICAL_VALIDATION_GATE_KIND
PHYSICAL_VALIDATION_GATE_VERSION = PROJECTION_PHYSICAL_VALIDATION_GATE_VERSION
PHYSICAL_VALIDATION_METRICS_SCHEMA_VERSION = "physical-projection-validation-metrics:v1"
PHYSICAL_RECONCILIATION_FINDINGS_SCHEMA_VERSION = (
    "physical-projection-reconciliation-findings:v1"
)


class PhysicalProjectionValidationRejected(ProjectionMappingError):
    """Physical evidence was durably recorded but cannot seal a candidate."""

    def __init__(self, validation: ProjectionValidation) -> None:
        super().__init__("physical projection validation rejected candidate sealing")
        self.validation = validation


class PhysicalProjectionEvidence(DefinitionModel):
    """One adapter observation joined to the accepted #208 canonical handoff."""

    mapping: ProjectionMappingResult
    expectation: PhysicalProjectionExpectation
    result: PhysicalValidationResult
    recovery: ProjectionRecoveryEvidence | None = None
    evaluated_at: datetime

    @model_validator(mode="after")
    def validate_lineage(self) -> "PhysicalProjectionEvidence":
        command = self.mapping.handoff.command
        receipt = self.mapping.handoff.receipt_command.receipt
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if (
            self.expectation.workspace_id != command.workspace_id
            or self.expectation.environment_id != command.environment_id
        ):
            raise ValueError("physical validation workspace or environment mismatch")
        if (
            self.expectation.projection_config_id != command.projection_config.config_id
            or self.expectation.projection_config_version != command.projection_config.version
            or self.expectation.projection_config_digest
            != command.projection_config.config_digest
        ):
            raise ValueError("physical validation projection configuration mismatch")
        if (
            self.expectation.namespace_digest
            != command.physical_namespace.namespace_digest
        ):
            raise ValueError("physical validation namespace mismatch")
        command_sources = {
            (source.source_object_id, source.source_revision_id): source
            for source in command.source_revisions
        }
        expectation_sources = {
            (source.source_object_id, source.source_revision_id): source
            for source in self.expectation.sources
        }
        if set(expectation_sources) != set(command_sources):
            raise ValueError("physical validation source lineage is incomplete")
        for key, expected in expectation_sources.items():
            source = command_sources[key]
            if (
                expected.revision_digest != source.revision_digest
                or expected.revision_number != source.revision_number
                or expected.status != source.status
            ):
                raise ValueError("physical validation SourceRevision lineage mismatch")
        if (
            receipt.workspace_id != command.workspace_id
            or receipt.environment_id != command.environment_id
            or receipt.projection_run_id != command.projection_run_id
            or receipt.projection_candidate_id != command.projection_candidate.candidate_id
            or receipt.projection_partition_id != command.projection_candidate.partition_id
            or receipt.command_identity != command.command_identity
            or receipt.command_digest != command.command_digest
        ):
            raise ValueError("physical validation adapter receipt lineage mismatch")
        adapter_result = self.mapping.handoff.adapter_receipt.result
        if (
            adapter_result.command_identity != command.command_identity
            or adapter_result.command_digest != command.command_digest
            or adapter_result.operation is not command.operation
        ):
            raise ValueError("physical validation adapter result lineage mismatch")
        for reference in self.mapping.handoff.source_references:
            source = command_sources.get(
                (reference.source_object_id, reference.source_revision_id)
            )
            if (
                reference.workspace_id != command.workspace_id
                or reference.environment_id != command.environment_id
                or (reference.source_object_id, reference.source_revision_id)
                not in set(command_sources)
                or source is None
                or reference.processing_receipt_id != source.processing_receipt_id
                or reference.projection_config_version_id
                != command.projection_config.config_id
                or reference.projection_candidate_id
                != command.projection_candidate.candidate_id
            ):
                raise ValueError("physical validation source reference lineage mismatch")
            if (
                reference.projection_run_id != receipt.projection_run_id
                or reference.projection_receipt_id != receipt.projection_receipt_id
            ):
                raise ValueError("physical validation source reference run mismatch")
        if self.recovery is not None:
            if (
                self.recovery.workspace_id != command.workspace_id
                or self.recovery.environment_id != command.environment_id
                or
                self.recovery.projection_run_id != command.projection_run_id
                or self.recovery.projection_candidate_id
                != command.projection_candidate.candidate_id
                or self.recovery.namespace_digest
                != command.physical_namespace.namespace_digest
            ):
                raise ValueError("physical recovery evidence lineage mismatch")
            recovery_sources = {
                source.source_object_id: source for source in self.recovery.source_revisions
            }
            if set(recovery_sources) != set(command_sources):
                raise ValueError("physical recovery source lineage is incomplete")
            for source_id, recovered in recovery_sources.items():
                expected = command_sources[source_id]
                if (
                    recovered.source_revision_id != expected.source_revision_id
                    or recovered.revision_digest != expected.revision_digest
                    or recovered.revision_number != expected.revision_number
                    or recovered.status != expected.status
                    or recovered.processing_receipt_id != expected.processing_receipt_id
                    or recovered.locator_contract_version
                    != expected.locator_contract_version
                ):
                    raise ValueError("physical recovery SourceRevision lineage mismatch")
        return self


class PhysicalProjectionReconciliationRequest(DefinitionModel):
    """Canonical activation context required for a runtime physical check."""

    snapshot: ProjectionSnapshot
    activation: ProjectionActivation
    attempt_identity: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    checked_at: datetime
    idempotency_receipt_id: UUID
    predecessor_result_id: UUID | None = None

    @model_validator(mode="after")
    def validate_context(self) -> "PhysicalProjectionReconciliationRequest":
        if self.snapshot.snapshot_state is not ProjectionSnapshotState.FINALIZED:
            raise ValueError("physical reconciliation requires a finalized snapshot")
        if (
            self.activation.projection_snapshot_id
            != self.snapshot.projection_snapshot_id
            or self.activation.workspace_id != self.snapshot.workspace_id
            or self.activation.environment_id != self.snapshot.environment_id
            or self.activation.projection_kind != self.snapshot.projection_kind
        ):
            raise ValueError("physical reconciliation activation lineage mismatch")
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        for name in ("idempotency_receipt_id", "predecessor_result_id"):
            value = getattr(self, name)
            if value is not None and value.int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        return self


class PhysicalProjectionEvidenceTranslator:
    """Translate safe adapter evidence into the two #88 owned commands."""

    def to_validation_command(
        self, evidence: PhysicalProjectionEvidence
    ) -> ProjectionValidationCommand:
        metrics = _validation_metrics(evidence)
        decision = (
            ProjectionValidationDecision.PASSED
            if _validation_passed(evidence)
            else ProjectionValidationDecision.FAILED
        )
        input_digest = _input_digest(evidence)
        validation = ProjectionValidation(
            projection_validation_id=_validation_id(evidence, input_digest),
            projection_run_id=evidence.mapping.handoff.command.projection_run_id,
            projection_candidate_id=evidence.mapping.handoff.command.projection_candidate.candidate_id,
            workspace_id=evidence.mapping.handoff.command.workspace_id,
            environment_id=evidence.mapping.handoff.command.environment_id,
            gate_kind=PHYSICAL_VALIDATION_GATE_KIND,
            gate_version=PHYSICAL_VALIDATION_GATE_VERSION,
            input_digest=input_digest,
            result_digest=_digest("physical-projection-validation-result:r1", metrics),
            decision=decision,
            metrics_schema_version=PHYSICAL_VALIDATION_METRICS_SCHEMA_VERSION,
            metrics_payload=metrics,
            evaluator_reference="physical-projection-validator:r1",
            evaluated_at=evidence.evaluated_at,
        )
        return ProjectionValidationCommand.create(
            validation=validation,
            idempotency_key=IdempotencyKey(_validation_idempotency_key(evidence, input_digest)),
        )

    def to_reconciliation_command(
        self,
        evidence: PhysicalProjectionEvidence,
        request: PhysicalProjectionReconciliationRequest,
    ) -> ProjectionReconciliationCommand:
        command = evidence.mapping.handoff.command
        snapshot = request.snapshot
        if (
            command.projection_run_id != snapshot.projection_run_id
            or command.projection_candidate.candidate_id != snapshot.projection_candidate_id
            or command.workspace_id != snapshot.workspace_id
            or command.environment_id != snapshot.environment_id
            or command.projection_config.config_id != snapshot.projection_config_version_id
        ):
            raise ValueError("physical reconciliation projection lineage mismatch")
        validation_command = self.to_validation_command(evidence)
        findings = {
            "schema_version": PHYSICAL_RECONCILIATION_FINDINGS_SCHEMA_VERSION,
            "validation_id": str(validation_command.validation.projection_validation_id),
            "validation_decision": validation_command.validation.decision.value,
            "metrics": validation_command.validation.metrics_payload,
        }
        attempt_digest = _digest("physical-projection-reconciliation-attempt:r1", findings)
        result = ProjectionReconciliationResult(
            reconciliation_result_id=uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "spine-physical-reconciliation-v1",
                        str(snapshot.projection_snapshot_id),
                        str(request.activation.projection_activation_id),
                        request.attempt_identity,
                    )
                ),
            ),
            workspace_id=snapshot.workspace_id,
            environment_id=snapshot.environment_id,
            projection_kind=snapshot.projection_kind,
            projection_snapshot_id=snapshot.projection_snapshot_id,
            projection_activation_id=request.activation.projection_activation_id,
            activation_generation=request.activation.activation_generation,
            attempt_identity=request.attempt_identity,
            attempt_digest=attempt_digest,
            predecessor_result_id=request.predecessor_result_id,
            outcome=(
                ProjectionReconciliationOutcome.HEALTHY
                if _validation_passed(evidence)
                else ProjectionReconciliationOutcome.DEGRADED
            ),
            findings_schema_version=PHYSICAL_RECONCILIATION_FINDINGS_SCHEMA_VERSION,
            findings_payload=findings,
            findings_digest=projection_reconciliation_findings_digest(
                findings_schema_version=PHYSICAL_RECONCILIATION_FINDINGS_SCHEMA_VERSION,
                findings_payload=findings,
            ),
            artifact_evidence_reference=(
                evidence.mapping.handoff.receipt_command.receipt.artifact_reference
            ),
            artifact_evidence_digest=(
                evidence.mapping.handoff.receipt_command.receipt.artifact_integrity_digest
            ),
            idempotency_receipt_id=request.idempotency_receipt_id,
            audit_event_reference=(
                f"physical-reconciliation:{request.activation.projection_activation_id}"
            ),
            checked_at=request.checked_at,
        )
        return ProjectionReconciliationCommand.create(
            result=result,
            idempotency_key=IdempotencyKey(
                "physical-reconciliation:"
                f"{snapshot.projection_snapshot_id.hex[:16]}:{request.attempt_identity}"
            ),
        )


class PhysicalProjectionEvidenceUnitOfWork(Protocol):
    @property
    def projections(self) -> ProjectionRepository: ...

    async def __aenter__(self) -> "PhysicalProjectionEvidenceUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class PhysicalProjectionEvidenceCoordinator:
    """Persist validation, seal only after a passing physical gate, and reconcile."""

    def __init__(
        self,
        *,
        uow_factory: Callable[
            [TrustedPersistenceContext], PhysicalProjectionEvidenceUnitOfWork
        ],
        context_factory: Callable[
            [
                ProjectionValidationCommand
                | ProjectionCandidateSealCommand
                | ProjectionReconciliationCommand
            ],
            TrustedPersistenceContext,
        ],
        translator: PhysicalProjectionEvidenceTranslator | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._translator = translator or PhysicalProjectionEvidenceTranslator()

    async def record_validation(
        self, evidence: PhysicalProjectionEvidence
    ) -> ProjectionMutationResult:
        command = self._translator.to_validation_command(evidence)
        async with self._uow_factory(self._context_factory(command)) as uow:
            result = await uow.projections.record_validation(command)
            await uow.commit()
            return result

    async def validate_and_seal(
        self,
        evidence: PhysicalProjectionEvidence,
        seal_command: ProjectionCandidateSealCommand,
    ) -> ProjectionMutationResult:
        validation_result = await self.record_validation(evidence)
        validation = validation_result.value
        if not isinstance(validation, ProjectionValidation):
            raise ProjectionMappingError(
                "physical validation persistence returned an invalid record"
            )
        if validation.decision is not ProjectionValidationDecision.PASSED:
            raise PhysicalProjectionValidationRejected(validation)
        # Validation and sealing use their own operation-owned UoWs. The seal
        # repository rechecks all immutable validation evidence before mutating
        # the candidate, so a concurrent failed gate cannot be bypassed.
        async with self._uow_factory(self._context_factory(seal_command)) as uow:
            sealed = await uow.projections.seal_candidate(seal_command)
            await uow.commit()
            return sealed

    async def reconcile(
        self,
        evidence: PhysicalProjectionEvidence,
        request: PhysicalProjectionReconciliationRequest,
    ) -> ProjectionMutationResult:
        command = self._translator.to_reconciliation_command(evidence, request)
        async with self._uow_factory(self._context_factory(command)) as uow:
            result = await uow.projections.record_reconciliation(command)
            await uow.commit()
            return result


def _validation_passed(evidence: PhysicalProjectionEvidence) -> bool:
    lifecycle = evidence.mapping.handoff.adapter_receipt.result.lifecycle
    recovery_passed = evidence.recovery is None or (
        evidence.recovery.operation is ProjectionRecoveryOperation.REPAIR
        and evidence.recovery.status
        in {ProjectionRecoveryStatus.SUCCEEDED, ProjectionRecoveryStatus.REPLAYED}
    )
    return evidence.result.passed and recovery_passed and lifecycle in {
        ProjectionLifecycle.SUCCEEDED,
        ProjectionLifecycle.REPLAYED,
        ProjectionLifecycle.RECOVERED,
    }


def _validation_metrics(evidence: PhysicalProjectionEvidence) -> dict[str, Any]:
    command = evidence.mapping.handoff.command
    receipt = evidence.mapping.handoff.receipt_command.receipt
    expectation = evidence.expectation
    result = evidence.result
    expected_counts = {kind.value: 0 for kind in _artifact_kinds()}
    for artifact in expectation.expected_artifacts:
        expected_counts[artifact.artifact_kind.value] += 1
    return {
        "schema_version": PHYSICAL_VALIDATION_METRICS_SCHEMA_VERSION,
        "projection_lineage": {
            "run_id": str(command.projection_run_id),
            "candidate_id": str(command.projection_candidate.candidate_id),
            "partition_id": str(command.projection_candidate.partition_id),
            "command_identity": command.command_identity,
            "command_digest": command.command_digest,
            "projection_config_id": str(expectation.projection_config_id),
            "projection_config_version": expectation.projection_config_version,
            "projection_config_digest": expectation.projection_config_digest,
            "namespace_digest": command.physical_namespace.namespace_digest,
        },
        "artifact_evidence": {
            "reference": receipt.artifact_reference,
            "digest": receipt.artifact_integrity_digest,
        },
        "source_lineage": [
            {
                "source_object_id": str(source.source_object_id),
                "source_revision_id": str(source.source_revision_id),
                "revision_digest": source.revision_digest,
                "revision_number": source.revision_number,
                "status": source.status,
                "processing_receipt_id": (
                    str(source.processing_receipt_id)
                    if source.processing_receipt_id is not None
                    else None
                ),
                "locator_contract_version": source.locator_contract_version,
            }
            for source in sorted(
                command.source_revisions,
                key=lambda item: (item.source_object_id.int, item.source_revision_id.int),
            )
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
            for reference in sorted(
                evidence.mapping.handoff.source_references,
                key=lambda item: item.source_projection_reference_id.hex,
            )
        ],
        "expected_counts": expected_counts,
        "observed_counts": {
            count.artifact_kind.value: count.value for count in result.counts
        },
        "diagnostics": [
            diagnostic.model_dump(mode="json")
            for diagnostic in result.diagnostics
        ],
        "repair_candidates": [
            candidate.model_dump(mode="json")
            for candidate in result.repair_candidates
        ],
        "adapter_result": {
            "outcome": evidence.mapping.handoff.adapter_receipt.result.outcome.value,
            "lifecycle": evidence.mapping.handoff.adapter_receipt.result.lifecycle.value,
            "failure": (
                evidence.mapping.handoff.adapter_receipt.result.failure.model_dump(mode="json")
                if evidence.mapping.handoff.adapter_receipt.result.failure is not None
                else None
            ),
            "failures": [
                failure.model_dump(mode="json")
                for failure in evidence.mapping.handoff.adapter_receipt.failures
            ],
        },
        "recovery": (
            {
                "operation": evidence.recovery.operation.value,
                "status": evidence.recovery.status.value,
                "workspace_id": str(evidence.recovery.workspace_id),
                "environment_id": str(evidence.recovery.environment_id),
                "projection_run_id": str(evidence.recovery.projection_run_id),
                "projection_candidate_id": str(
                    evidence.recovery.projection_candidate_id
                ),
                "namespace_digest": evidence.recovery.namespace_digest,
                "source_revisions": [
                    source.model_dump(mode="json")
                    for source in evidence.recovery.source_revisions
                ],
                "artifact_ids": evidence.recovery.artifact_ids,
                "failure_code": (
                    evidence.recovery.failure_code.value
                    if evidence.recovery.failure_code is not None
                    else None
                ),
            }
            if evidence.recovery is not None
            else None
        ),
        "input_digest": result.input_digest,
        "passed": _validation_passed(evidence),
    }


def _input_digest(evidence: PhysicalProjectionEvidence) -> str:
    command = evidence.mapping.handoff.command
    receipt = evidence.mapping.handoff.receipt_command.receipt
    return _digest(
        "physical-projection-validation-input:r1",
        {
            "physical_input_digest": evidence.result.input_digest,
            "command_digest": command.command_digest,
            "artifact_evidence_digest": receipt.artifact_integrity_digest,
            "projection_run_id": str(command.projection_run_id),
            "projection_candidate_id": str(command.projection_candidate.candidate_id),
        },
    )


def _validation_id(evidence: PhysicalProjectionEvidence, input_digest: str) -> UUID:
    command = evidence.mapping.handoff.command
    return uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "spine-physical-validation-v1",
                str(command.workspace_id),
                str(command.environment_id),
                str(command.projection_run_id),
                str(command.projection_candidate.candidate_id),
                input_digest,
            )
        ),
    )


def _validation_idempotency_key(
    evidence: PhysicalProjectionEvidence, input_digest: str
) -> str:
    command = evidence.mapping.handoff.command
    return (
        "physical-validation:"
        f"{command.projection_run_id.hex[:16]}:{input_digest[:32]}"
    )


def _digest(schema: str, payload: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            {"schema": schema, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _artifact_kinds() -> tuple[ArtifactKind, ...]:
    return tuple(ArtifactKind)


__all__ = [
    "PHYSICAL_RECONCILIATION_FINDINGS_SCHEMA_VERSION",
    "PHYSICAL_VALIDATION_GATE_KIND",
    "PHYSICAL_VALIDATION_GATE_VERSION",
    "PHYSICAL_VALIDATION_METRICS_SCHEMA_VERSION",
    "PhysicalProjectionEvidence",
    "PhysicalProjectionEvidenceCoordinator",
    "PhysicalProjectionEvidenceTranslator",
    "PhysicalProjectionReconciliationRequest",
    "PhysicalProjectionValidationRejected",
]
