"""Persist validated ContextProjection evidence through the Issue #7 ledgers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from spine.application.persistence.boundary import CanonicalBoundaryRepository
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    OptimisticConflictError,
)
from spine.application.persistence.projection import (
    ProjectionReceiptCommand,
    ProjectionRepository,
)
from spine.application.projection_result_mapping import (
    CanonicalProjectionLocator,
    CanonicalProjectionMappingContext,
    ProjectionMappingConflictError,
    ProjectionMappingError,
    ProjectionMappingHandoff,
    ProjectionMappingResult,
    ProjectionResultMappingBridge,
)
from spine.domain.projections import (
    OntologyVersion,
    ProjectionConfigVersion,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionRun,
    SourceProjectionReference,
)
from spine.domain.sources import CanonicalBoundary, CanonicalBoundaryMember
from spine.memory.contracts import ProjectionCommand


@dataclass(frozen=True, slots=True)
class CanonicalProjectionEvidenceRequest:
    """Canonical identities used to resolve exact stable-locator evidence."""

    command: ProjectionCommand
    run: ProjectionRun
    partition: ProjectionPartition
    projection_config: ProjectionConfigVersion
    ontology: OntologyVersion
    canonical_boundary: CanonicalBoundary
    boundary_members: tuple[CanonicalBoundaryMember, ...]


class CanonicalProjectionLocatorResolver(Protocol):
    """Resolve canonical locator evidence without invoking a physical projection."""

    async def resolve(
        self, request: CanonicalProjectionEvidenceRequest
    ) -> tuple[CanonicalProjectionLocator, ...]: ...


class CanonicalProjectionGatewayUnitOfWork(Protocol):
    @property
    def projections(self) -> ProjectionRepository: ...

    @property
    def boundaries(self) -> CanonicalBoundaryRepository: ...

    async def __aenter__(self) -> "CanonicalProjectionGatewayUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class PersistenceCanonicalProjectionGateway:
    """Resolve and atomically persist one non-authoritative adapter handoff."""

    def __init__(
        self,
        *,
        uow_factory: Callable[
            [TrustedPersistenceContext], CanonicalProjectionGatewayUnitOfWork
        ],
        context_factory: Callable[[ProjectionCommand], TrustedPersistenceContext],
        locator_resolver: CanonicalProjectionLocatorResolver,
        receipt_id_source: Callable[[ProjectionCommand], UUID] | None = None,
        idempotency_receipt_id_source: Callable[[ProjectionCommand], UUID] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._locator_resolver = locator_resolver
        self._receipt_id_source = receipt_id_source or _projection_receipt_id
        self._idempotency_receipt_id_source = (
            idempotency_receipt_id_source or _idempotency_receipt_id
        )

    async def is_recorded(self, command: ProjectionCommand) -> bool:
        """Probe canonical receipt identity without requiring an open candidate."""

        async with self._uow_factory(self._context_factory(command)) as uow:
            receipts = await uow.projections.list_receipts(command.projection_run_id)
            for receipt in receipts:
                if receipt.command_identity != command.command_identity:
                    continue
                if (
                    receipt.workspace_id != command.workspace_id
                    or receipt.environment_id != command.environment_id
                ):
                    raise ProjectionMappingError(
                        "canonical projection receipt scope is unauthorized"
                    )
                if receipt.command_digest != command.command_digest:
                    raise ProjectionMappingConflictError(
                        "projection mapping digest conflict"
                    )
                return True
        return False

    async def replay_for(
        self, command: ProjectionCommand
    ) -> ProjectionMappingResult | None:
        """Defer replay reconstruction until the validated handoff reaches accept."""

        return None

    async def resolve_context(
        self, command: ProjectionCommand
    ) -> CanonicalProjectionMappingContext:
        persistence_context = self._context_factory(command)
        async with self._uow_factory(persistence_context) as uow:
            run = await uow.projections.resolve_run(command.projection_run_id)
            if run is None:
                raise ProjectionMappingError("canonical projection run is missing")
            if (
                run.workspace_id != command.workspace_id
                or run.environment_id != command.environment_id
                or run.projection_candidate_id
                != command.projection_candidate.candidate_id
            ):
                raise ProjectionMappingError("canonical projection run scope is unauthorized")

            receipts = await uow.projections.list_receipts(run.projection_run_id)
            for receipt in receipts:
                if receipt.command_identity != command.command_identity:
                    continue
                if receipt.command_digest != command.command_digest:
                    raise ProjectionMappingConflictError(
                        "projection mapping digest conflict"
                    )

            partitions = await uow.projections.list_partitions(run.projection_run_id)
            partition = next(
                (
                    item
                    for item in partitions
                    if item.projection_partition_id
                    == command.projection_candidate.partition_id
                ),
                None,
            )
            if partition is None:
                raise ProjectionMappingError("canonical projection partition is missing")
            if (
                partition.expected_command_identity == command.command_identity
                and partition.expected_command_digest != command.command_digest
            ):
                raise ProjectionMappingConflictError(
                    "projection mapping digest conflict"
                )

            projection_config = await uow.projections.resolve_config_version(
                run.projection_config_version_id
            )
            if projection_config is None:
                raise ProjectionMappingError("canonical projection config is missing")
            ontology = await uow.projections.resolve_ontology_version(
                projection_config.ontology_version_id
            )
            if (
                ontology is None
                or not ontology.publication_eligible
                or ontology.workspace_id != run.workspace_id
                or ontology.environment_id != run.environment_id
            ):
                raise ProjectionMappingError(
                    "canonical projection ontology is missing or ineligible"
                )
            canonical_boundary = await uow.boundaries.resolve(
                run.canonical_boundary_id
            )
            if canonical_boundary is None:
                raise ProjectionMappingError("canonical boundary is missing")
            boundary_members = await uow.boundaries.resolve_members(
                run.canonical_boundary_id
            )

        evidence_request = CanonicalProjectionEvidenceRequest(
            command=command,
            run=run,
            partition=partition,
            projection_config=projection_config,
            ontology=ontology,
            canonical_boundary=canonical_boundary,
            boundary_members=boundary_members,
        )
        locators = await self._locator_resolver.resolve(evidence_request)
        if not locators:
            raise ProjectionMappingError("canonical projection locator evidence is missing")
        try:
            return CanonicalProjectionMappingContext(
                run=run,
                partition=partition,
                projection_config=projection_config,
                canonical_boundary=canonical_boundary,
                boundary_members=boundary_members,
                locators=locators,
                projection_receipt_id=self._receipt_id_source(command),
                idempotency_receipt_id=self._idempotency_receipt_id_source(command),
            )
        except ValueError as error:
            raise ProjectionMappingError(
                "canonical projection evidence is incomplete or stale"
            ) from error

    async def accept(self, handoff: ProjectionMappingHandoff) -> ProjectionMappingResult:
        try:
            async with self._uow_factory(
                self._context_factory(handoff.command)
            ) as uow:
                existing_receipts = await uow.projections.list_receipts(
                    handoff.command.projection_run_id
                )
                receipt = next(
                    (
                        item
                        for item in existing_receipts
                        if item.command_identity
                        == handoff.command.command_identity
                    ),
                    None,
                )
                if receipt is not None:
                    replay = True
                    if receipt.command_digest != handoff.command.command_digest:
                        raise ProjectionMappingConflictError(
                            "projection mapping digest conflict"
                        )
                    references = tuple(
                        reference
                        for reference in await uow.projections.list_source_references(
                            receipt.projection_run_id
                        )
                        if reference.projection_receipt_id
                        == receipt.projection_receipt_id
                    )
                    self._require_same_evidence(receipt, references, handoff)
                else:
                    mutation = await uow.projections.record_receipt(
                        handoff.receipt_command
                    )
                    receipt = mutation.value
                    if not isinstance(receipt, ProjectionReceipt):
                        raise ProjectionMappingError(
                            "canonical projection receipt persistence is invalid"
                        )
                    replay = bool(mutation.replay)
                    if replay:
                        references = tuple(
                            reference
                            for reference in await uow.projections.list_source_references(
                                receipt.projection_run_id
                            )
                            if reference.projection_receipt_id
                            == receipt.projection_receipt_id
                        )
                        self._require_same_evidence(receipt, references, handoff)
                    else:
                        references = tuple(
                            [
                                await uow.projections.add_source_reference(reference)
                                for reference in handoff.source_references
                            ]
                        )
                        await uow.commit()
                references = tuple(sorted(references, key=_reference_sort_key))
        except IdempotencyConflictError as error:
            raise ProjectionMappingConflictError(
                "projection mapping digest conflict"
            ) from error
        except (ConstraintConflictError, OptimisticConflictError) as error:
            raise ProjectionMappingError(
                "canonical projection evidence was rejected"
            ) from error

        canonical_handoff = handoff.model_copy(
            update={
                "receipt_command": ProjectionReceiptCommand.create(
                    receipt=receipt,
                    idempotency_key=handoff.receipt_command.idempotency_key,
                ),
                "source_references": references,
            }
        )
        return ProjectionMappingResult(
            handoff=canonical_handoff,
            replay=replay,
        )

    @staticmethod
    def _require_same_evidence(
        receipt: ProjectionReceipt,
        references: tuple[SourceProjectionReference, ...],
        handoff: ProjectionMappingHandoff,
    ) -> None:
        incoming_receipt = handoff.receipt_command.receipt
        if receipt.model_dump(
            mode="python", exclude={"idempotency_receipt_id"}
        ) != incoming_receipt.model_dump(
            mode="python", exclude={"idempotency_receipt_id"}
        ):
            raise ProjectionMappingConflictError(
                "projection mapping canonical receipt conflict"
            )
        stored = tuple(
            reference.model_dump(
                mode="python", exclude={"source_projection_reference_id"}
            )
            for reference in sorted(references, key=_reference_sort_key)
        )
        incoming = tuple(
            reference.model_dump(
                mode="python", exclude={"source_projection_reference_id"}
            )
            for reference in sorted(
                handoff.source_references, key=_reference_sort_key
            )
        )
        if stored != incoming:
            raise ProjectionMappingConflictError(
                "projection mapping source reference conflict"
            )


def build_projection_result_persistence(
    *,
    uow_factory: Callable[
        [TrustedPersistenceContext], CanonicalProjectionGatewayUnitOfWork
    ],
    context_factory: Callable[[ProjectionCommand], TrustedPersistenceContext],
    locator_resolver: CanonicalProjectionLocatorResolver,
    reference_id_source: Callable[[], UUID] = uuid4,
) -> ProjectionResultMappingBridge:
    """Compose the approved #207 bridge with the canonical #87 persistence adapter."""

    return ProjectionResultMappingBridge(
        PersistenceCanonicalProjectionGateway(
            uow_factory=uow_factory,
            context_factory=context_factory,
            locator_resolver=locator_resolver,
        ),
        id_source=reference_id_source,
    )


def _reference_sort_key(
    reference: SourceProjectionReference,
) -> tuple[str, str, str, str, str, str]:
    return (
        reference.source_object_id.hex,
        reference.source_revision_id.hex,
        reference.segment_id,
        reference.locator_digest,
        reference.projected_kind,
        reference.projected_object_id,
    )


def _projection_receipt_id(command: ProjectionCommand) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "spine-projection-receipt-v1",
                str(command.workspace_id),
                str(command.environment_id),
                command.command_identity,
            )
        ),
    )


def _idempotency_receipt_id(command: ProjectionCommand) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "spine-projection-receipt-claim-v1",
                str(command.workspace_id),
                str(command.environment_id),
                command.command_identity,
            )
        ),
    )


__all__ = [
    "CanonicalProjectionEvidenceRequest",
    "CanonicalProjectionGatewayUnitOfWork",
    "CanonicalProjectionLocatorResolver",
    "PersistenceCanonicalProjectionGateway",
    "build_projection_result_persistence",
]
