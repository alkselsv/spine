"""Provider-neutral, fail-closed physical read planning.

The Context Broker owns this boundary.  A physical provider receives a plan
only after the broker has proved the active publication, canonical projection
lineage, registered namespace lifecycle, and source-level isolation contract.
The plan never treats a provider dataset or node set as an authorization
boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.diagnostics import DiagnosticContext
from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScope,
    AuthorizedSourceConstraint,
)
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.projections import (
    ProjectionCandidateState,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionRun,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    SourceProjectionReference,
)

from .contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion as ProviderProjectionConfigVersion,
    ProjectionNamespace,
    ProjectionNamespaceState,
    PublicationPin,
)
from .namespace import (
    ProjectionNamespaceContext,
    ProjectionNamespaceRegistry,
    ProjectionNamespaceRouter,
)


_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"


def _non_zero(value: UUID, field_name: str) -> None:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")


def _unique(values: tuple[UUID, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique IDs")


class PhysicalReadIsolationRequirement(ValidatedDefinitionModel):
    """The source restriction a provider must enforce before searching."""

    schema_version: Literal["physical-read-isolation:v1"] = "physical-read-isolation:v1"
    provider_capability_reference: str = Field(min_length=1, max_length=256)
    source_object_ids: tuple[UUID, ...] = Field(min_length=1)
    source_revision_ids: tuple[UUID, ...] = Field(min_length=1)
    pre_retrieval_enforced: bool

    @model_validator(mode="after")
    def validate_restriction(self) -> "PhysicalReadIsolationRequirement":
        for value, name in (
            (self.source_object_ids, "source_object_ids"),
            (self.source_revision_ids, "source_revision_ids"),
        ):
            for item in value:
                _non_zero(item, name)
            _unique(value, name)
        if len(self.source_object_ids) != len(self.source_revision_ids):
            raise ValueError("source object and revision restrictions must be paired")
        return self


class PhysicalReadContext(ValidatedDefinitionModel):
    """Immutable trusted inputs for one provider-neutral read attempt.

    ``publication_pin`` is issued by the Context Broker from the PostgreSQL
    active-publication resolver.  The canonical records below are supplied by
    the read-side repository and are rechecked against that pin before a plan
    is returned.
    """

    schema_version: Literal["physical-read-context:v1"] = "physical-read-context:v1"
    query: str = Field(min_length=1, max_length=16_384)
    diagnostic_context: DiagnosticContext
    publication_pin: PublicationPin
    authorized_scope: AuthorizedRetrievalScope
    projection_run: ProjectionRun
    snapshot_members: tuple[ProjectionSnapshotMember, ...] = Field(min_length=1)
    partitions: tuple[ProjectionPartition, ...] = Field(min_length=1)
    receipts: tuple[ProjectionReceipt, ...] = Field(min_length=1)
    source_references: tuple[SourceProjectionReference, ...] = Field(min_length=1)
    source_isolation: PhysicalReadIsolationRequirement
    max_partitions: int = Field(default=64, ge=1, le=128)


class PhysicalReadPlanOutcome(str, Enum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"
    UNSAFE = "unsafe"
    CONFLICT = "conflict"


class PhysicalReadPlanReason(str, Enum):
    READY = "ready"
    INVALID_PUBLICATION_PIN = "invalid_publication_pin"
    PUBLICATION_UNAVAILABLE = "publication_unavailable"
    SCOPE_MISMATCH = "scope_mismatch"
    CROSS_WORKSPACE = "cross_workspace"
    INCOMPLETE_LINEAGE = "incomplete_lineage"
    FABRICATED_LINEAGE = "fabricated_lineage"
    INELIGIBLE_NAMESPACE = "ineligible_namespace"
    SOURCE_ISOLATION_UNPROVEN = "source_isolation_unproven"
    FANOUT_LIMIT = "fanout_limit"


class PhysicalReadOperation(ValidatedDefinitionModel):
    """One bounded provider call with complete canonical and namespace lineage."""

    schema_version: Literal["physical-read-operation:v1"] = "physical-read-operation:v1"
    query: str = Field(min_length=1, max_length=16_384)
    workspace_id: UUID
    environment_id: UUID
    publication_pin_id: UUID
    publication_pin_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_snapshot_id: UUID
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    projection_run: ProjectionRun
    projection_partition: ProjectionPartition
    projection_receipt: ProjectionReceipt
    snapshot_members: tuple[ProjectionSnapshotMember, ...] = Field(min_length=1)
    authorized_source_constraints: tuple[AuthorizedSourceConstraint, ...] = Field(min_length=1)
    source_references: tuple[SourceProjectionReference, ...] = Field(min_length=1)
    physical_namespace: ProjectionNamespace
    source_object_ids: tuple[UUID, ...] = Field(min_length=1)
    source_revision_ids: tuple[UUID, ...] = Field(min_length=1)
    source_isolation: PhysicalReadIsolationRequirement
    result_limit: int = Field(ge=1, le=1000)
    token_limit: int = Field(ge=1, le=65_536)

    @model_validator(mode="after")
    def validate_identity(self) -> "PhysicalReadOperation":
        for value, name in (
            (self.workspace_id, "workspace_id"),
            (self.environment_id, "environment_id"),
            (self.publication_pin_id, "publication_pin_id"),
            (self.projection_snapshot_id, "projection_snapshot_id"),
            (self.projection_activation_id, "projection_activation_id"),
        ):
            _non_zero(value, name)
        for value, name in (
            (self.source_object_ids, "source_object_ids"),
            (self.source_revision_ids, "source_revision_ids"),
        ):
            _unique(value, name)
            for item in value:
                _non_zero(item, name)
        return self


class PhysicalReadPlan(ValidatedDefinitionModel):
    """A safe, immutable fan-out plan; unsafe outcomes cannot contain one."""

    schema_version: Literal["physical-read-plan:v1"] = "physical-read-plan:v1"
    context: PhysicalReadContext
    operations: tuple[PhysicalReadOperation, ...] = Field(min_length=1)
    source_isolation: PhysicalReadIsolationRequirement
    result_limit: int = Field(ge=1, le=1000)
    token_limit: int = Field(ge=1, le=65_536)

    @model_validator(mode="after")
    def validate_plan(self) -> "PhysicalReadPlan":
        if len(self.operations) > self.context.max_partitions:
            raise ValueError("physical read plan exceeds its fan-out limit")
        if self.context.source_isolation != self.source_isolation:
            raise ValueError("plan source isolation must match its read context")
        return self


class PhysicalReadPlanResolution(ValidatedDefinitionModel):
    """Typed result of planning; failures carry no provider or source guesses."""

    schema_version: Literal["physical-read-plan-resolution:v1"] = (
        "physical-read-plan-resolution:v1"
    )
    outcome: PhysicalReadPlanOutcome
    reason: PhysicalReadPlanReason
    plan: PhysicalReadPlan | None = None
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    diagnostic_context: DiagnosticContext | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "PhysicalReadPlanResolution":
        if self.outcome is PhysicalReadPlanOutcome.READY:
            if self.reason is not PhysicalReadPlanReason.READY or self.plan is None:
                raise ValueError("ready resolution requires a ready plan")
        elif self.plan is not None or self.reason is PhysicalReadPlanReason.READY:
            raise ValueError("failed resolution cannot carry a plan")
        return self


class PhysicalNamespaceRegistry(Protocol):
    def require(
        self,
        namespace: ProjectionNamespace,
        context: ProjectionNamespaceContext,
    ) -> ProjectionNamespace: ...


class PhysicalReadPlanBuilder:
    """Prove one read plan using only immutable canonical evidence."""

    def __init__(
        self,
        *,
        namespace_registry: PhysicalNamespaceRegistry,
        namespace_router: ProjectionNamespaceRouter | None = None,
    ) -> None:
        self._namespace_registry = namespace_registry
        self._namespace_router = namespace_router or ProjectionNamespaceRouter()

    def build(self, context: PhysicalReadContext) -> PhysicalReadPlanResolution:
        """Return READY only when every pre-retrieval gate has passed."""

        try:
            resolution = self._build(context)
        except Exception:
            # The physical boundary must not turn a lineage or registry detail
            # into an authorization or provider error.
            resolution = self._failure(
                PhysicalReadPlanOutcome.INCOMPLETE,
                PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
            )
        return resolution.model_copy(
            update={
                "workspace_id": context.authorized_scope.workspace_id,
                "environment_id": context.authorized_scope.environment_id,
                "diagnostic_context": context.diagnostic_context,
            }
        )

    async def resolve(self, context: PhysicalReadContext) -> PhysicalReadPlanResolution:
        """Async alias for application/read-side composition ports."""

        return self.build(context)

    def _build(self, context: PhysicalReadContext) -> PhysicalReadPlanResolution:
        pin = context.publication_pin
        scope = context.authorized_scope
        snapshot = pin.projection_snapshot
        if not pin.has_valid_digest():
            return self._failure(
                PhysicalReadPlanOutcome.CONFLICT,
                PhysicalReadPlanReason.INVALID_PUBLICATION_PIN,
            )
        if snapshot.snapshot_state is not ProjectionSnapshotState.FINALIZED:
            return self._failure(
                PhysicalReadPlanOutcome.UNAVAILABLE,
                PhysicalReadPlanReason.PUBLICATION_UNAVAILABLE,
            )
        if not self._pin_matches_scope(pin, scope):
            return self._failure(
                PhysicalReadPlanOutcome.CONFLICT,
                PhysicalReadPlanReason.SCOPE_MISMATCH,
            )
        if not self._run_matches_pin(context.projection_run, pin):
            return self._failure(
                PhysicalReadPlanOutcome.INCOMPLETE,
                PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
            )
        if not context.source_isolation.pre_retrieval_enforced:
            return self._failure(
                PhysicalReadPlanOutcome.UNSAFE,
                PhysicalReadPlanReason.SOURCE_ISOLATION_UNPROVEN,
            )

        constraints = {
            item.projection_snapshot_member_id: item
            for item in scope.source_constraints
        }
        if len(constraints) != len(scope.source_constraints):
            return self._failure(
                PhysicalReadPlanOutcome.INCOMPLETE,
                PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
            )
        members = {
            item.projection_snapshot_member_id: item for item in context.snapshot_members
        }
        if len(members) != len(context.snapshot_members) or set(members) != set(constraints):
            return self._failure(
                PhysicalReadPlanOutcome.INCOMPLETE,
                PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
            )

        for member_id, member in members.items():
            constraint = constraints[member_id]
            if not self._member_matches_pin(member, pin, constraint):
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.FABRICATED_LINEAGE,
                )

        partition_by_id = self._unique_by_id(
            context.partitions, lambda item: item.projection_partition_id
        )
        receipt_by_id = self._unique_by_id(
            context.receipts, lambda item: item.projection_receipt_id
        )
        if partition_by_id is None or receipt_by_id is None:
            return self._failure(
                PhysicalReadPlanOutcome.INCOMPLETE,
                PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
            )
        partition_ids = {member.projection_partition_id for member in members.values()}
        if (
            None in partition_ids
            or not partition_ids.issubset(partition_by_id)
            or set(partition_by_id) != {item for item in partition_ids if item is not None}
        ):
            return self._failure(
                PhysicalReadPlanOutcome.INCOMPLETE,
                PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
            )
        ordered_partitions = tuple(
            sorted(
                (partition_by_id[item] for item in partition_ids if item is not None),
                key=lambda item: (item.partition_identity, item.projection_partition_id.hex),
            )
        )
        if len(ordered_partitions) > context.max_partitions:
            return self._failure(
                PhysicalReadPlanOutcome.UNSAFE,
                PhysicalReadPlanReason.FANOUT_LIMIT,
            )

        references_by_partition: dict[UUID, list[SourceProjectionReference]] = {
            item.projection_partition_id: [] for item in ordered_partitions
        }
        for reference in context.source_references:
            if not self._reference_matches_run(reference, context.projection_run, pin):
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.FABRICATED_LINEAGE,
                )
            member = next(
                (
                    candidate
                    for candidate in members.values()
                    if candidate.source_object_id == reference.source_object_id
                    and candidate.source_revision_id == reference.source_revision_id
                ),
                None,
            )
            if member is None or member.projection_partition_id is None:
                return self._failure(
                    PhysicalReadPlanOutcome.UNSAFE,
                    PhysicalReadPlanReason.CROSS_WORKSPACE,
                )
            if reference.projection_receipt_id != member.projection_receipt_id:
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.FABRICATED_LINEAGE,
                )
            partition = partition_by_id[member.projection_partition_id]
            if reference.security_domain_reference != partition.security_domain_reference:
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.FABRICATED_LINEAGE,
                )
            references_by_partition[member.projection_partition_id].append(reference)

        operations: list[PhysicalReadOperation] = []
        ordered_constraints = tuple(
            sorted(
                scope.source_constraints,
                key=lambda item: item.source_object_id.hex,
            )
        )
        expected_objects = tuple(item.source_object_id for item in ordered_constraints)
        expected_revisions = tuple(item.source_revision_id for item in ordered_constraints)
        if (
            set(zip(context.source_isolation.source_object_ids, context.source_isolation.source_revision_ids))
            != set(zip(expected_objects, expected_revisions))
        ):
            return self._failure(
                PhysicalReadPlanOutcome.UNSAFE,
                PhysicalReadPlanReason.SOURCE_ISOLATION_UNPROVEN,
            )

        for partition in ordered_partitions:
            member_group = tuple(
                sorted(
                    (
                        member
                        for member in members.values()
                        if member.projection_partition_id == partition.projection_partition_id
                    ),
                    key=lambda item: item.projection_snapshot_member_id.hex,
                )
            )
            if not member_group or any(
                member.projection_receipt_id is None for member in member_group
            ):
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
                )
            receipt_ids = {member.projection_receipt_id for member in member_group}
            if len(receipt_ids) != 1:
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
                )
            receipt = receipt_by_id.get(next(iter(receipt_ids)))
            if (
                receipt is None
                or not self._partition_matches_run(partition, context.projection_run, pin)
                or not self._receipt_matches_partition(receipt, partition, pin)
            ):
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.FABRICATED_LINEAGE,
                )
            references = tuple(
                sorted(
                    references_by_partition[partition.projection_partition_id],
                    key=lambda item: (
                        item.source_object_id.hex,
                        item.projected_kind,
                        item.projected_object_id,
                    ),
                )
            )
            if not references or {
                (item.source_object_id, item.source_revision_id) for item in references
            } != {
                (item.source_object_id, item.source_revision_id) for item in member_group
            }:
                return self._failure(
                    PhysicalReadPlanOutcome.INCOMPLETE,
                    PhysicalReadPlanReason.INCOMPLETE_LINEAGE,
                )
            try:
                namespace = self._registered_active_namespace(pin, context.projection_run, partition)
            except Exception:
                return self._failure(
                    PhysicalReadPlanOutcome.UNSAFE,
                    PhysicalReadPlanReason.INELIGIBLE_NAMESPACE,
                )
            constraints_for_partition = tuple(
                sorted(
                    (constraints[member.projection_snapshot_member_id] for member in member_group),
                    key=lambda item: item.source_object_id.hex,
                )
            )
            operation_source_objects = tuple(item.source_object_id for item in constraints_for_partition)
            operation_source_revisions = tuple(item.source_revision_id for item in constraints_for_partition)
            operations.append(
                PhysicalReadOperation(
                    query=context.query,
                    workspace_id=scope.workspace_id,
                    environment_id=scope.environment_id,
                    publication_pin_id=pin.pin_id,
                    publication_pin_digest=pin.pin_digest,
                    projection_snapshot_id=pin.projection_snapshot.projection_snapshot_id,
                    projection_activation_id=pin.projection_activation.projection_activation_id,
                    activation_generation=pin.activation_generation,
                    projection_run=context.projection_run,
                    projection_partition=partition,
                    projection_receipt=receipt,
                    snapshot_members=member_group,
                    authorized_source_constraints=constraints_for_partition,
                    source_references=references,
                    physical_namespace=namespace,
                    source_object_ids=operation_source_objects,
                    source_revision_ids=operation_source_revisions,
                    source_isolation=PhysicalReadIsolationRequirement(
                        provider_capability_reference=context.source_isolation.provider_capability_reference,
                        source_object_ids=operation_source_objects,
                        source_revision_ids=operation_source_revisions,
                        pre_retrieval_enforced=True,
                    ),
                    result_limit=scope.max_results,
                    token_limit=scope.max_tokens,
                )
            )

        plan = PhysicalReadPlan(
            context=context,
            operations=tuple(operations),
            source_isolation=context.source_isolation,
            result_limit=scope.max_results,
            token_limit=scope.max_tokens,
        )
        return PhysicalReadPlanResolution(
            outcome=PhysicalReadPlanOutcome.READY,
            reason=PhysicalReadPlanReason.READY,
            plan=plan,
        )

    @staticmethod
    def _failure(
        outcome: PhysicalReadPlanOutcome,
        reason: PhysicalReadPlanReason,
    ) -> PhysicalReadPlanResolution:
        return PhysicalReadPlanResolution(outcome=outcome, reason=reason)

    @staticmethod
    def _pin_matches_scope(pin: PublicationPin, scope: AuthorizedRetrievalScope) -> bool:
        snapshot = pin.projection_snapshot
        return (
            pin.workspace_id == scope.workspace_id
            and pin.environment_id == scope.environment_id
            and pin.projection_kind == scope.projection_kind
            and snapshot.workspace_id == scope.workspace_id
            and snapshot.environment_id == scope.environment_id
            and snapshot.projection_snapshot_id == scope.projection_snapshot_id
            and pin.projection_activation.projection_activation_id == scope.projection_activation_id
            and pin.activation_generation == scope.activation_generation
            and pin.active_pointer_cas_version == scope.active_pointer_cas_version
            and pin.canonical_boundary.boundary_id == scope.canonical_boundary_id
            and pin.canonical_boundary.boundary_digest == scope.canonical_boundary_manifest_digest
            and pin.as_of == scope.canonical_as_of
            and pin.projection_config_version.projection_config_version_id
            == scope.projection_config_version_id
            and pin.freshness.value == scope.publication_freshness.value
            and pin.health.value == scope.publication_health.value
        )

    @staticmethod
    def _run_matches_pin(run: ProjectionRun, pin: PublicationPin) -> bool:
        snapshot = pin.projection_snapshot
        return (
            run.candidate_state is ProjectionCandidateState.SEALED
            and run.projection_run_id == snapshot.projection_run_id
            and run.projection_candidate_id == snapshot.projection_candidate_id
            and run.workspace_id == pin.workspace_id
            and run.environment_id == pin.environment_id
            and run.projection_kind == pin.projection_kind
            and run.projection_config_version_id
            == pin.projection_config_version.projection_config_version_id
            and run.canonical_boundary_id == pin.canonical_boundary.boundary_id
            and run.candidate_evidence_digest == snapshot.candidate_evidence_digest
        )

    @staticmethod
    def _member_matches_pin(
        member: ProjectionSnapshotMember,
        pin: PublicationPin,
        constraint: AuthorizedSourceConstraint,
    ) -> bool:
        return (
            member.disposition is ProjectionSnapshotMemberDisposition.INCLUDED
            and member.projection_snapshot_id == pin.projection_snapshot.projection_snapshot_id
            and member.canonical_boundary_id == pin.canonical_boundary.boundary_id
            and member.workspace_id == pin.workspace_id
            and member.environment_id == pin.environment_id
            and member.source_object_id == constraint.source_object_id
            and member.source_revision_id == constraint.source_revision_id
            and member.projection_partition_id is not None
            and member.projection_receipt_id is not None
        )

    @staticmethod
    def _reference_matches_run(
        reference: SourceProjectionReference,
        run: ProjectionRun,
        pin: PublicationPin,
    ) -> bool:
        return (
            reference.projection_run_id == run.projection_run_id
            and reference.projection_candidate_id == run.projection_candidate_id
            and reference.workspace_id == pin.workspace_id
            and reference.environment_id == pin.environment_id
            and reference.projection_config_version_id
            == pin.projection_config_version.projection_config_version_id
        )

    @staticmethod
    def _receipt_matches_partition(
        receipt: ProjectionReceipt,
        partition: ProjectionPartition,
        pin: PublicationPin,
    ) -> bool:
        return (
            receipt.projection_run_id == partition.projection_run_id
            and receipt.projection_candidate_id == partition.projection_candidate_id
            and receipt.projection_partition_id == partition.projection_partition_id
            and receipt.workspace_id == pin.workspace_id
            and receipt.environment_id == pin.environment_id
            and receipt.command_identity == partition.expected_command_identity
            and receipt.command_digest == partition.expected_command_digest
            and not receipt.failures
        )

    def _registered_active_namespace(
        self,
        pin: PublicationPin,
        run: ProjectionRun,
        partition: ProjectionPartition,
    ) -> ProjectionNamespace:
        config = pin.projection_config_version
        namespace_context = ProjectionNamespaceContext(
            workspace_id=pin.workspace_id,
            environment_id=pin.environment_id,
            projection_kind=pin.projection_kind,
            projection_candidate=ProjectionCandidate(
                candidate_id=run.projection_candidate_id,
                run_id=run.projection_run_id,
                partition_id=partition.projection_partition_id,
                snapshot_id=pin.projection_snapshot.projection_snapshot_id,
            ),
            projection_config=ProviderProjectionConfigVersion(
                config_id=config.projection_config_version_id,
                version=str(config.version),
                config_digest=config.config_digest,
                ontology_version=str(config.ontology_version_id),
                adapter_contract_version=config.adapter_contract_version,
            ),
            canonical_boundary=CanonicalBoundary(
                boundary_id=pin.canonical_boundary.boundary_id,
                boundary_digest=pin.canonical_boundary.boundary_digest,
                accepted_through_generation=pin.canonical_boundary.accepted_through_generation,
            ),
            state=ProjectionNamespaceState.ACTIVE,
        )
        expected = self._namespace_router.route(namespace_context)
        registered = self._namespace_registry.require(expected, namespace_context)
        if registered.state is not ProjectionNamespaceState.ACTIVE:
            raise ValueError("namespace is not active")
        return registered

    @staticmethod
    def _partition_matches_run(
        partition: ProjectionPartition,
        run: ProjectionRun,
        pin: PublicationPin,
    ) -> bool:
        return (
            partition.projection_run_id == run.projection_run_id
            and partition.projection_candidate_id == run.projection_candidate_id
            and partition.workspace_id == pin.workspace_id
            and partition.environment_id == pin.environment_id
        )

    @staticmethod
    def _unique_by_id(items: tuple[object, ...], key: object) -> dict[UUID, object] | None:
        result: dict[UUID, object] = {}
        for item in items:
            identifier = key(item)  # type: ignore[operator]
            if identifier in result:
                return None
            result[identifier] = item
        return result


__all__ = [
    "PhysicalNamespaceRegistry",
    "PhysicalReadContext",
    "PhysicalReadIsolationRequirement",
    "PhysicalReadOperation",
    "PhysicalReadPlan",
    "PhysicalReadPlanBuilder",
    "PhysicalReadPlanOutcome",
    "PhysicalReadPlanReason",
    "PhysicalReadPlanResolution",
]
