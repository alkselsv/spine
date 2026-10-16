"""Apply verified source deletion and reappearance observations.

This module is intentionally a thin ingestion boundary.  Source observation
and canonical current-head transitions remain owned by their persistence ports;
the coordinator only validates lifecycle-specific evidence and composes both
ports in one Unit of Work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from spine.application.persistence.canonical import (
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceResult,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.repositories import (
    SourceObservationCommand,
    SourceObservationResult,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.sources import (
    RevisionKind,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
)


class SourceLifecycleKind(str, Enum):
    DELETION = "deletion"
    REPLACEMENT = "replacement"
    REAPPEARANCE = "reappearance"


class SourceLifecycleValidationError(ValueError):
    """The lifecycle command does not carry safe, complete evidence."""


@dataclass(frozen=True, slots=True)
class SourceLifecycleCommand:
    """One verified lifecycle observation and its #84 acceptance command.

    ``canonical_acceptance`` is built by the composition boundary that owns
    audit/outbox evidence.  Keeping it as the existing #84 command prevents
    this ingestion slice from reimplementing canonical transition policy.
    """

    kind: SourceLifecycleKind
    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    observation_idempotency_key: IdempotencyKey
    canonical_acceptance: CanonicalAcceptanceCommand
    receipt_association: SourceRevisionReceiptAssociation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceLifecycleKind):
            raise SourceLifecycleValidationError("Unknown source lifecycle kind.")
        if self.revision.source_object_id != self.source.source_object_id:
            raise SourceLifecycleValidationError("Source lifecycle identity is invalid.")
        if (
            self.provenance.source_object_id != self.source.source_object_id
            or self.provenance.revision_id != self.revision.revision_id
            or (self.provenance.workspace_id, self.provenance.environment_id)
            != (self.source.workspace_id, self.source.environment_id)
        ):
            raise SourceLifecycleValidationError("Source lifecycle provenance is invalid.")
        if (
            self.canonical_acceptance.source != self.source
            or self.canonical_acceptance.revision != self.revision
            or self.canonical_acceptance.provenance != self.provenance
        ):
            raise SourceLifecycleValidationError("Canonical lifecycle evidence is invalid.")
        if self.kind is SourceLifecycleKind.DELETION:
            if self.revision.kind is not RevisionKind.TOMBSTONE:
                raise SourceLifecycleValidationError("Deletion requires a tombstone revision.")
            if (
                self.canonical_acceptance.admission_decision_id is not None
                or self.canonical_acceptance.access_policy_version_id is not None
            ):
                raise SourceLifecycleValidationError(
                    "Deletion cannot carry content admission or policy evidence."
                )
        elif self.kind is SourceLifecycleKind.REAPPEARANCE and (
            self.revision.kind is not RevisionKind.CONTENT
            or self.revision.reappearance_after_tombstone_revision_id is None
        ):
            raise SourceLifecycleValidationError(
                "Reappearance requires a content revision with tombstone continuity."
            )
        elif self.kind is SourceLifecycleKind.REPLACEMENT and (
            self.revision.kind is not RevisionKind.CONTENT
            or self.revision.reappearance_after_tombstone_revision_id is not None
        ):
            raise SourceLifecycleValidationError(
                "Replacement requires an ordinary immutable content revision."
            )
        elif self.kind in {
            SourceLifecycleKind.REPLACEMENT,
            SourceLifecycleKind.REAPPEARANCE,
        } and (
            self.canonical_acceptance.admission_decision_id is None
            or self.canonical_acceptance.access_policy_version_id is None
        ):
            raise SourceLifecycleValidationError(
                "Content lifecycle requires fresh admission and policy evidence."
            )
        elif self.kind in {
            SourceLifecycleKind.REPLACEMENT,
            SourceLifecycleKind.REAPPEARANCE,
        } and (self.receipt_association is None or (
            self.receipt_association.source_object_id != self.source.source_object_id
            or self.receipt_association.revision_id != self.revision.revision_id
            or self.receipt_association.provenance_id != self.provenance.provenance_id
            or (
                self.receipt_association.workspace_id,
                self.receipt_association.environment_id,
            )
            != (self.source.workspace_id, self.source.environment_id)
            or self.receipt_association.original_reference != self.revision.original_reference
            or self.receipt_association.original_sha256 != self.revision.original_sha256
            or self.receipt_association.byte_length != self.revision.byte_length
        )):
            raise SourceLifecycleValidationError(
                "Content lifecycle requires verified finalized content evidence."
            )


@dataclass(frozen=True, slots=True)
class SourceLifecycleResult:
    observation: SourceObservationResult
    acceptance: CanonicalAcceptanceResult

    @property
    def replay(self) -> bool:
        """Whether both durable lifecycle boundaries replayed their result."""

        return self.observation.replay and self.acceptance.replay


@dataclass(frozen=True, slots=True)
class SourceLifecycleCoordinator:
    """Compose source observation and canonical acceptance atomically."""

    uow_factory: UnitOfWorkFactory
    context_factory: Callable[[SourceLifecycleCommand], TrustedPersistenceContext]

    async def apply(self, command: SourceLifecycleCommand) -> SourceLifecycleResult:
        context = self.context_factory(command)
        async with self.uow_factory(context) as uow:
            source = await uow.sources.resolve_source(command.source.source_object_id)
            if source != command.source:
                raise SourceLifecycleValidationError("Source lifecycle identity is unavailable.")

            if command.kind is SourceLifecycleKind.REAPPEARANCE:
                predecessor_id = command.revision.reappearance_after_tombstone_revision_id
                if predecessor_id is None:
                    raise SourceLifecycleValidationError(
                        "Reappearance tombstone continuity is unavailable."
                    )
                predecessor = await uow.sources.resolve_revision(predecessor_id)
                if (
                    predecessor is None
                    or predecessor.source_object_id != command.source.source_object_id
                    or predecessor.workspace_id != command.source.workspace_id
                    or predecessor.environment_id != command.source.environment_id
                    or predecessor.kind is not RevisionKind.TOMBSTONE
                ):
                    raise SourceLifecycleValidationError(
                        "Reappearance tombstone continuity is unavailable."
                    )

            observation = await uow.sources.record_observation(
                SourceObservationCommand.create(
                    source=command.source,
                    revision=command.revision,
                    provenance=command.provenance,
                    idempotency_key=command.observation_idempotency_key,
                    receipt_association=command.receipt_association,
                    operation=context.operation,
                )
            )
            if (
                observation.source != command.source
                or observation.revision != command.revision
                or observation.provenance != command.provenance
            ):
                raise SourceLifecycleValidationError(
                    "Source lifecycle observation identity changed."
                )
            acceptance = await uow.canonical.accept(command.canonical_acceptance)
            await uow.commit()
            return SourceLifecycleResult(observation=observation, acceptance=acceptance)


__all__ = [
    "SourceLifecycleCommand",
    "SourceLifecycleCoordinator",
    "SourceLifecycleKind",
    "SourceLifecycleResult",
    "SourceLifecycleValidationError",
]
