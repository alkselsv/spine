"""Map non-authoritative projection outcomes into the Issue #7 handoff.

The bridge validates and transports adapter evidence.  It deliberately has no
candidate sealing, validation, snapshot assembly, or publication operation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, StrictBool, model_validator

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.projection import ProjectionReceiptCommand
from spine.domain.common import DefinitionModel
from spine.domain.projections import (
    ProjectionCandidateState,
    ProjectionConfigVersion as CanonicalProjectionConfigVersion,
    ProjectionDiagnostic as CanonicalProjectionDiagnostic,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionRun,
    SourceProjectionReference,
)
from spine.domain.sources import (
    CanonicalBoundary as CanonicalSourceBoundary,
    CanonicalBoundaryMember,
)
from spine.memory.contracts import (
    ProjectionCommand,
    ProjectionDiagnostic,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionLifecycle,
    ProjectionNamespaceState,
    ProjectionOperation,
    ProjectionResult,
    StableLocator,
)
from spine.memory.namespace import (
    NamespaceRoutingError,
    ProjectionNamespaceContext,
    ProjectionNamespaceRouter,
)


_DIGEST = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class ProjectionMappingError(ValueError):
    """The adapter outcome cannot be mapped without losing authority or lineage."""


class ProjectionMappingConflictError(ProjectionMappingError):
    """A command identity was already mapped with a different digest."""


class ProjectionPreparationStatus(str, Enum):
    """Named preflight outcome for physical projection execution."""

    READY = "ready"
    RECORDED = "recorded"


class _MappingModel(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdapterProjectionDiagnostic(_MappingModel):
    """Non-authoritative adapter diagnostic retained separately from ledgers."""

    schema_version: Literal["adapter-projection-diagnostic:v1"] = (
        "adapter-projection-diagnostic:v1"
    )
    code: str = Field(pattern=_IDENTIFIER)
    retryable: StrictBool
    safe_detail: ProjectionDiagnostic


class AdapterProjectedObject(_MappingModel):
    """One content-free object identity emitted by the physical adapter."""

    schema_version: Literal["adapter-projected-object:v1"] = (
        "adapter-projected-object:v1"
    )
    kind: str = Field(pattern=_IDENTIFIER)
    object_id: str = Field(min_length=1, max_length=256)


class AdapterImplementationMetadata(_MappingModel):
    """Allowlisted, content-free implementation diagnostics."""

    schema_version: Literal["adapter-implementation-metadata:v1"] = (
        "adapter-implementation-metadata:v1"
    )
    adapter_contract: str = Field(pattern=_VERSION)
    adapter_name: str | None = Field(default=None, pattern=_IDENTIFIER)
    adapter_version: str | None = Field(default=None, pattern=_VERSION)
    backend_profile: str | None = Field(default=None, pattern=_VERSION)


class AdapterSourceProjectionReference(_MappingModel):
    """One adapter-emitted physical object and its exact source lineage."""

    schema_version: Literal["adapter-source-projection-reference:v1"] = (
        "adapter-source-projection-reference:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    namespace_digest: str = Field(pattern=_DIGEST)
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=_DIGEST)
    processing_receipt_id: UUID
    segment_id: str = Field(pattern=_IDENTIFIER)
    locator_schema_version: str = Field(pattern=_VERSION)
    locator: StableLocator
    locator_digest: str = Field(pattern=_DIGEST)
    projected_kind: str = Field(pattern=_IDENTIFIER)
    projected_object_id: str = Field(min_length=1, max_length=256)
    projection_config_version_id: UUID
    security_domain_reference: str = Field(min_length=1, max_length=256)
    security_domain_version: str = Field(pattern=_VERSION)

    @model_validator(mode="after")
    def validate_ids(self) -> "AdapterSourceProjectionReference":
        for name in (
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
            "processing_receipt_id",
            "projection_config_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        return self


class AdapterProjectionReceipt(_MappingModel):
    """Disclosure-safe adapter output; never a canonical ProjectionReceipt."""

    schema_version: Literal["context-projection-adapter-result:v1"] = (
        "context-projection-adapter-result:v1"
    )
    result: ProjectionResult
    artifact_kind: str = Field(pattern=_IDENTIFIER)
    artifact_integrity_digest: str = Field(pattern=_DIGEST)
    emitted_objects: tuple[AdapterProjectedObject, ...] = ()
    source_references: tuple[AdapterSourceProjectionReference, ...] = ()
    warnings: tuple[AdapterProjectionDiagnostic, ...] = ()
    failures: tuple[AdapterProjectionDiagnostic, ...] = ()
    implementation_schema_version: str = Field(pattern=_VERSION)
    implementation_metadata: AdapterImplementationMetadata
    idempotency_key: str = Field(min_length=1, max_length=256)
    completed_at: datetime

    @model_validator(mode="after")
    def validate_result(self) -> "AdapterProjectionReceipt":
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        if self.result.artifact_reference is None:
            raise ValueError("adapter result must carry its opaque artifact reference")
        if self.result.failure is None:
            if self.failures:
                raise ValueError("successful adapter result cannot carry failures")
        elif not any(
            item.code == self.result.failure.code.value
            and item.retryable is self.result.failure.retryable
            and item.safe_detail == self.result.failure.safe_detail
            for item in self.failures
        ):
            raise ValueError("adapter failures must retain the result failure")
        count_names = [count.name for count in self.result.counts]
        if len(count_names) != len(set(count_names)):
            raise ValueError("adapter count names must be unique")
        object_keys = [(item.kind, item.object_id) for item in self.emitted_objects]
        if len(object_keys) != len(set(object_keys)):
            raise ValueError("adapter emitted object identities must be unique")
        return self


class CanonicalProjectionLocator(_MappingModel):
    """Canonical input evidence against which adapter provenance is checked."""

    schema_version: Literal["canonical-projection-locator:v1"] = (
        "canonical-projection-locator:v1"
    )
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=_DIGEST)
    processing_receipt_id: UUID
    segment_id: str = Field(pattern=_IDENTIFIER)
    locator_schema_version: str = Field(pattern=_VERSION)
    locator: StableLocator
    locator_digest: str = Field(pattern=_DIGEST)


class CanonicalProjectionMappingContext(_MappingModel):
    """Issue #7-owned identities needed to validate and construct a handoff."""

    schema_version: Literal["issue7-projection-mapping-context:v1"] = (
        "issue7-projection-mapping-context:v1"
    )
    run: ProjectionRun
    partition: ProjectionPartition
    projection_config: CanonicalProjectionConfigVersion
    canonical_boundary: CanonicalSourceBoundary
    boundary_members: tuple[CanonicalBoundaryMember, ...]
    locators: tuple[CanonicalProjectionLocator, ...]
    projection_receipt_id: UUID
    idempotency_receipt_id: UUID

    @model_validator(mode="after")
    def validate_scope(self) -> "CanonicalProjectionMappingContext":
        run = self.run
        partition = self.partition
        if (
            partition.projection_run_id != run.projection_run_id
            or partition.projection_candidate_id != run.projection_candidate_id
            or partition.workspace_id != run.workspace_id
            or partition.environment_id != run.environment_id
        ):
            raise ValueError("partition must belong to the canonical projection run")
        if (
            self.projection_config.projection_config_version_id
            != run.projection_config_version_id
            or self.projection_config.workspace_id != run.workspace_id
            or self.projection_config.environment_id != run.environment_id
        ):
            raise ValueError("projection config must belong to the canonical run")
        if (
            self.canonical_boundary.boundary_id != run.canonical_boundary_id
            or self.canonical_boundary.workspace_id != run.workspace_id
            or self.canonical_boundary.environment_id != run.environment_id
        ):
            raise ValueError("canonical boundary must belong to the canonical run")
        if not (
            run.projection_kind
            == self.projection_config.projection_kind
            == self.canonical_boundary.projection_kind
        ):
            raise ValueError("canonical projection kinds must match")
        for name in ("projection_receipt_id", "idempotency_receipt_id"):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        keys = [_locator_key(locator) for locator in self.locators]
        if len(keys) != len(set(keys)):
            raise ValueError("canonical projection locators must be unique")
        members = {
            (member.source_object_id, member.source_revision_id): member
            for member in self.boundary_members
        }
        if len({member.source_object_id for member in self.boundary_members}) != len(
            self.boundary_members
        ):
            raise ValueError("canonical boundary members must have unique source IDs")
        for member in self.boundary_members:
            if (
                member.boundary_id != self.canonical_boundary.boundary_id
                or member.workspace_id != run.workspace_id
                or member.environment_id != run.environment_id
                or member.accepted_generation
                > self.canonical_boundary.accepted_through_generation
            ):
                raise ValueError("canonical boundary member does not match the mapping context")
        if any(
            (locator.source_object_id, locator.source_revision_id) not in members
            for locator in self.locators
        ):
            raise ValueError("projection locator is not in the canonical boundary")
        return self


class ProjectionMappingHandoff(_MappingModel):
    """Validated inputs accepted by the Issue #7 persistence boundary."""

    contract_version: Literal["issue15-to-issue7-projection:v1"] = (
        "issue15-to-issue7-projection:v1"
    )
    command: ProjectionCommand
    adapter_receipt: AdapterProjectionReceipt
    receipt_command: ProjectionReceiptCommand
    source_references: tuple[SourceProjectionReference, ...]


class ProjectionMappingResult(_MappingModel):
    handoff: ProjectionMappingHandoff
    replay: bool = False


class CanonicalProjectionGateway(Protocol):
    """Canonical lookup/idempotency seam; persistence is supplied by Issue #7."""

    async def is_recorded(self, command: ProjectionCommand) -> bool: ...

    async def replay_for(self, command: ProjectionCommand) -> ProjectionMappingResult | None: ...

    async def resolve_context(
        self, command: ProjectionCommand
    ) -> CanonicalProjectionMappingContext: ...

    async def accept(self, handoff: ProjectionMappingHandoff) -> ProjectionMappingResult: ...


class ProjectionResultMappingBridge:
    """Fail-closed adapter-result mapping with no publication authority."""

    def __init__(
        self,
        gateway: CanonicalProjectionGateway,
        *,
        id_source: Callable[[], UUID] = uuid4,
    ) -> None:
        self._gateway = gateway
        self._id_source = id_source

    async def prepare(
        self,
        command: ProjectionCommand,
    ) -> ProjectionPreparationStatus:
        """Validate canonical command context before any physical mutation."""

        context = await self._gateway.resolve_context(command)
        recorded = await self._gateway.is_recorded(command)
        self._validate_command_context(command, context, allow_recorded=recorded)
        if recorded:
            return ProjectionPreparationStatus.RECORDED
        return ProjectionPreparationStatus.READY

    async def map(
        self,
        command: ProjectionCommand,
        adapter_receipt: AdapterProjectionReceipt,
    ) -> ProjectionMappingResult:
        adapter_receipt = AdapterProjectionReceipt.model_validate(
            adapter_receipt.model_dump(mode="python")
        )
        context = await self._gateway.resolve_context(command)
        recorded = await self._gateway.is_recorded(command)
        self._validate_command_context(command, context, allow_recorded=recorded)
        replay = await self._gateway.replay_for(command)
        if replay is not None:
            return replay
        self._validate_adapter_result(command, adapter_receipt)
        references = self._map_references(command, adapter_receipt, context)
        receipt = ProjectionReceipt(
            projection_receipt_id=context.projection_receipt_id,
            projection_run_id=context.run.projection_run_id,
            projection_candidate_id=context.run.projection_candidate_id,
            projection_partition_id=context.partition.projection_partition_id,
            workspace_id=context.run.workspace_id,
            environment_id=context.run.environment_id,
            command_identity=command.command_identity,
            command_digest=command.command_digest,
            artifact_kind=adapter_receipt.artifact_kind,
            artifact_reference=adapter_receipt.result.artifact_reference,
            artifact_integrity_digest=adapter_receipt.artifact_integrity_digest,
            counts={count.name: count.value for count in adapter_receipt.result.counts},
            warnings=tuple(_canonical_diagnostic(item) for item in adapter_receipt.warnings),
            failures=tuple(_canonical_diagnostic(item) for item in adapter_receipt.failures),
            implementation_schema_version=adapter_receipt.implementation_schema_version,
            implementation_metadata=adapter_receipt.implementation_metadata.model_dump(
                mode="python", exclude_none=True
            ),
            idempotency_receipt_id=context.idempotency_receipt_id,
            completed_at=adapter_receipt.completed_at,
        )
        handoff = ProjectionMappingHandoff(
            command=command,
            adapter_receipt=adapter_receipt,
            receipt_command=ProjectionReceiptCommand.create(
                receipt=receipt,
                idempotency_key=IdempotencyKey(adapter_receipt.idempotency_key),
            ),
            source_references=references,
        )
        return await self._gateway.accept(handoff)

    def _validate_command_context(
        self,
        command: ProjectionCommand,
        context: CanonicalProjectionMappingContext,
        *,
        allow_recorded: bool = False,
    ) -> None:
        if command.operation not in {
            ProjectionOperation.PROJECT,
            ProjectionOperation.UPDATE,
            ProjectionOperation.REMOVE,
            ProjectionOperation.REBUILD,
        }:
            raise ProjectionMappingError(
                "only physical artifact operations can map to a projection receipt"
            )
        if (
            context.run.candidate_state is not ProjectionCandidateState.OPEN
            and not allow_recorded
        ):
            raise ProjectionMappingError("sealed projection candidates reject new mappings")
        if command.physical_namespace.state not in {
            ProjectionNamespaceState.SHADOW,
            ProjectionNamespaceState.REBUILDING,
        }:
            raise ProjectionMappingError("mapping requires a shadow or rebuilding namespace")
        if command.projection_candidate.snapshot_id is not None:
            raise ProjectionMappingError("mapping cannot select a projection snapshot")
        try:
            ProjectionNamespaceRouter().validate(
                command.physical_namespace,
                ProjectionNamespaceContext(
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    projection_kind=context.run.projection_kind,
                    projection_candidate=command.projection_candidate,
                    projection_config=command.projection_config,
                    canonical_boundary=command.canonical_boundary,
                    state=command.physical_namespace.state,
                ),
            )
        except NamespaceRoutingError:
            raise ProjectionMappingError(
                "physical namespace is not server-routed for the canonical context"
            ) from None
        run = context.run
        partition = context.partition
        if (
            command.workspace_id != run.workspace_id
            or command.environment_id != run.environment_id
            or command.projection_run_id != run.projection_run_id
            or command.projection_candidate.candidate_id != run.projection_candidate_id
            or command.projection_candidate.partition_id != partition.projection_partition_id
        ):
            raise ProjectionMappingError("adapter command scope does not match canonical context")
        if (
            partition.expected_command_identity == command.command_identity
            and partition.expected_command_digest != command.command_digest
        ):
            raise ProjectionMappingConflictError(
                "projection mapping digest conflict"
            )
        if partition.expected_command_identity != command.command_identity:
            raise ProjectionMappingError("adapter command does not match its canonical partition")
        config = context.projection_config
        if (
            command.projection_config.config_id != config.projection_config_version_id
            or command.projection_config.config_digest != config.config_digest
            or command.projection_config.version != str(config.version)
            or command.projection_config.adapter_contract_version
            != config.adapter_contract_version
        ):
            raise ProjectionMappingError(
                "adapter projection config does not match canonical config"
            )
        boundary = context.canonical_boundary
        if (
            command.canonical_boundary.boundary_id != boundary.boundary_id
            or command.canonical_boundary.boundary_digest != boundary.manifest_digest
            or command.canonical_boundary.accepted_through_generation
            != boundary.accepted_through_generation
        ):
            raise ProjectionMappingError("adapter canonical boundary does not match")
        canonical_sources = {
            (locator.source_object_id, locator.source_revision_id): locator
            for locator in context.locators
        }
        command_source_ids = {
            (source.source_object_id, source.source_revision_id)
            for source in command.source_revisions
        }
        assigned_member_ids = {
            (member.source_object_id, member.source_revision_id)
            for member in context.boundary_members
        }
        if command_source_ids != assigned_member_ids:
            raise ProjectionMappingError(
                "projection command must cover its exact assigned boundary members"
            )
        for source in command.source_revisions:
            locator = canonical_sources.get(
                (source.source_object_id, source.source_revision_id)
            )
            if (
                locator is None
                or source.revision_digest != locator.revision_digest
                or source.processing_receipt_id != locator.processing_receipt_id
                or source.locator_contract_version != locator.locator_schema_version
            ):
                raise ProjectionMappingError("SourceRevision context is missing or fabricated")

    def _validate_adapter_result(
        self,
        command: ProjectionCommand,
        receipt: AdapterProjectionReceipt,
    ) -> None:
        if receipt.result.operation is not command.operation:
            raise ProjectionMappingError("adapter operation does not match the command")
        if (
            receipt.result.command_identity != command.command_identity
            or receipt.result.command_digest != command.command_digest
        ):
            raise ProjectionMappingError("adapter command identity or digest does not match")
        if (
            receipt.implementation_metadata.adapter_contract
            != command.projection_config.adapter_contract_version
        ):
            raise ProjectionMappingError(
                "adapter implementation metadata does not match the pinned contract"
            )
        self._validate_effects(command, receipt)

    @staticmethod
    def _validate_effects(
        command: ProjectionCommand,
        receipt: AdapterProjectionReceipt,
    ) -> None:
        sources = {
            (source.source_object_id, source.source_revision_id): source
            for source in command.source_revisions
        }
        effect_sources = [
            (effect.source_object_id, effect.source_revision_id)
            for effect in receipt.result.effects
        ]
        if len(effect_sources) != len(set(effect_sources)):
            raise ProjectionMappingError("adapter effects contain ambiguous source mappings")
        if (
            receipt.result.lifecycle
            in {
                ProjectionLifecycle.SUCCEEDED,
                ProjectionLifecycle.RECOVERED,
                ProjectionLifecycle.REPLAYED,
            }
            and set(effect_sources) != set(sources)
        ):
            raise ProjectionMappingError("adapter effect mapping is incomplete")
        expected_action = (
            ProjectionEffectAction.REMOVE_SOURCE
            if command.operation is ProjectionOperation.REMOVE
            else ProjectionEffectAction.UPSERT_SOURCE
        )
        for effect in receipt.result.effects:
            source = sources.get((effect.source_object_id, effect.source_revision_id))
            if (
                effect.workspace_id != command.workspace_id
                or effect.environment_id != command.environment_id
                or effect.command_identity != command.command_identity
                or effect.command_digest != command.command_digest
                or effect.operation is not command.operation
                or effect.action is not expected_action
                or effect.projection_candidate != command.projection_candidate
                or effect.projection_config != command.projection_config
                or effect.canonical_boundary != command.canonical_boundary
                or effect.physical_namespace != command.physical_namespace
                or source is None
                or effect.revision_digest != source.revision_digest
                or effect.revision_number != source.revision_number
            ):
                raise ProjectionMappingError(
                    "adapter effect envelope does not match the projection command"
                )

    def _map_references(
        self,
        command: ProjectionCommand,
        receipt: AdapterProjectionReceipt,
        context: CanonicalProjectionMappingContext,
    ) -> tuple[SourceProjectionReference, ...]:
        successful_upserts = {
            (effect.source_object_id, effect.source_revision_id)
            for effect in receipt.result.effects
            if effect.action is ProjectionEffectAction.UPSERT_SOURCE
            and effect.status in {ProjectionEffectStatus.APPLIED, ProjectionEffectStatus.REPLAYED}
        }
        mapped_sources = {
            (reference.source_object_id, reference.source_revision_id)
            for reference in receipt.source_references
        }
        if not successful_upserts.issubset(mapped_sources):
            raise ProjectionMappingError("missing source-to-projection reference")

        emitted_objects = {
            (item.kind, item.object_id) for item in receipt.emitted_objects
        }
        referenced_objects = {
            (item.projected_kind, item.projected_object_id)
            for item in receipt.source_references
        }
        if emitted_objects != referenced_objects:
            raise ProjectionMappingError(
                "emitted objects require exact per-object provenance"
            )

        allowed = {_locator_key(locator): locator for locator in context.locators}
        command_sources = {
            (source.source_object_id, source.source_revision_id)
            for source in command.source_revisions
        }
        canonical: list[SourceProjectionReference] = []
        projected_keys: set[tuple[str, str]] = set()
        for reference in receipt.source_references:
            if (
                reference.workspace_id != command.workspace_id
                or reference.environment_id != command.environment_id
            ):
                raise ProjectionMappingError("source reference workspace or environment mismatch")
            if reference.namespace_digest != command.physical_namespace.namespace_digest:
                raise ProjectionMappingError("source reference namespace mismatch")
            if (
                reference.source_object_id,
                reference.source_revision_id,
            ) not in command_sources:
                raise ProjectionMappingError("source reference is not part of the command")
            if (
                reference.projection_config_version_id
                != context.projection_config.projection_config_version_id
                or reference.security_domain_reference
                != context.partition.security_domain_reference
                or reference.security_domain_version
                != context.partition.security_domain_version
            ):
                raise ProjectionMappingError("source reference canonical context mismatch")
            expected = allowed.get(_locator_key(reference))
            if expected is None or (
                reference.revision_digest != expected.revision_digest
                or reference.processing_receipt_id != expected.processing_receipt_id
                or reference.locator != expected.locator
                or reference.locator_digest != expected.locator_digest
            ):
                raise ProjectionMappingError("fabricated or ambiguous source reference")
            projected_key = (reference.projected_kind, reference.projected_object_id)
            if projected_key in projected_keys:
                raise ProjectionMappingError("ambiguous duplicate projected object reference")
            projected_keys.add(projected_key)
            canonical.append(
                SourceProjectionReference(
                    source_projection_reference_id=self._id_source(),
                    projection_run_id=context.run.projection_run_id,
                    projection_candidate_id=context.run.projection_candidate_id,
                    projection_receipt_id=context.projection_receipt_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    source_object_id=reference.source_object_id,
                    source_revision_id=reference.source_revision_id,
                    processing_receipt_id=reference.processing_receipt_id,
                    segment_id=reference.segment_id,
                    locator_schema_version=reference.locator_schema_version,
                    locator_reference=f"{reference.locator.kind}:{reference.locator.value}",
                    locator_digest=reference.locator_digest,
                    projected_kind=reference.projected_kind,
                    projected_object_id=reference.projected_object_id,
                    projection_config_version_id=reference.projection_config_version_id,
                    security_domain_reference=reference.security_domain_reference,
                    created_at=receipt.completed_at,
                )
            )
        return tuple(canonical)


def _locator_key(
    value: CanonicalProjectionLocator | AdapterSourceProjectionReference,
) -> tuple[UUID, UUID, str, str, str]:
    return (
        value.source_object_id,
        value.source_revision_id,
        value.segment_id,
        value.locator_schema_version,
        value.locator_digest,
    )


def _canonical_diagnostic(
    diagnostic: AdapterProjectionDiagnostic,
) -> CanonicalProjectionDiagnostic:
    detail = diagnostic.safe_detail
    return CanonicalProjectionDiagnostic(
        code=diagnostic.code,
        attempt=detail.attempt,
        item_count=detail.item_count,
        duration_ms=detail.duration_ms,
        opaque_detail_ref=detail.opaque_detail_ref,
    )


__all__ = [
    "AdapterImplementationMetadata",
    "AdapterProjectedObject",
    "AdapterProjectionDiagnostic",
    "AdapterProjectionReceipt",
    "AdapterSourceProjectionReference",
    "CanonicalProjectionGateway",
    "CanonicalProjectionLocator",
    "CanonicalProjectionMappingContext",
    "ProjectionMappingConflictError",
    "ProjectionMappingError",
    "ProjectionMappingHandoff",
    "ProjectionMappingResult",
    "ProjectionResultMappingBridge",
]
