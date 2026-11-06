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
from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.application.persistence.canonical import (
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceReadResult,
    CanonicalAcceptanceResult,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.repositories import (
    SourceObservationCommand,
    SourceObservationResult,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.common import DefinitionModel
from spine.domain.sources import (
    CurrentSourceHead,
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


class GovernedReappearanceAuthorization(DefinitionModel):
    """Immutable authority for one exact post-tombstone content revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    tombstone_revision_id: UUID
    content_revision_id: UUID
    identity_continuity_evidence_id: UUID
    identity_continuity_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pre_tombstone_access_policy_version_id: UUID
    fresh_admission_decision_id: UUID
    fresh_access_policy_version_id: UUID
    authority_generation: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_identifiers(self) -> "GovernedReappearanceAuthorization":
        identifiers = (
            self.authorization_id,
            self.workspace_id,
            self.environment_id,
            self.source_object_id,
            self.tombstone_revision_id,
            self.content_revision_id,
            self.identity_continuity_evidence_id,
            self.pre_tombstone_access_policy_version_id,
            self.fresh_admission_decision_id,
            self.fresh_access_policy_version_id,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("reappearance authorization identifiers must be non-zero")
        if (
            self.pre_tombstone_access_policy_version_id
            == self.fresh_access_policy_version_id
        ):
            raise ValueError("reappearance requires a fresh access policy")
        return self


class ReappearanceCanonicalReader(Protocol):
    async def resolve_acceptance_read(
        self, canonical_acceptance_id: UUID
    ) -> CanonicalAcceptanceReadResult | None: ...


class GovernedReappearanceAuthorizationReader(Protocol):
    async def resolve_current(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> GovernedReappearanceAuthorization | None: ...


class GovernedReappearanceAuthority(Protocol):
    async def is_current(
        self,
        authorization: GovernedReappearanceAuthorization,
        *,
        canonical: ReappearanceCanonicalReader,
        current: CurrentSourceHead,
    ) -> bool: ...


class CanonicalGovernedReappearanceAuthority:
    """Resolve current authority, then prove exact tombstone and prior policy."""

    def __init__(
        self, authorizations: GovernedReappearanceAuthorizationReader
    ) -> None:
        self._authorizations = authorizations

    async def is_current(
        self,
        authorization: GovernedReappearanceAuthorization,
        *,
        canonical: ReappearanceCanonicalReader,
        current: CurrentSourceHead,
    ) -> bool:
        try:
            resolved = await self._authorizations.resolve_current(
                workspace_id=authorization.workspace_id,
                environment_id=authorization.environment_id,
                source_object_id=authorization.source_object_id,
            )
            if resolved != authorization:
                return False
            tip = await canonical.resolve_acceptance_read(current.acceptance_id)
            if (
                tip is None
                or not tip.is_current
                or tip.current != current
            ):
                return False
            if current.current_revision_kind is RevisionKind.TOMBSTONE:
                tombstone = tip
            elif (
                current.current_revision_kind is RevisionKind.CONTENT
                and current.current_revision_id == authorization.content_revision_id
                and tip.acceptance.source_revision_id
                == authorization.content_revision_id
                and tip.acceptance.admission_decision_id
                == authorization.fresh_admission_decision_id
                and tip.acceptance.access_policy_version_id
                == authorization.fresh_access_policy_version_id
                and tip.acceptance.predecessor_acceptance_id is not None
            ):
                tombstone = await canonical.resolve_acceptance_read(
                    tip.acceptance.predecessor_acceptance_id
                )
            else:
                return False
            if (
                tombstone is None
                or tombstone.acceptance.source_revision_id
                != authorization.tombstone_revision_id
                or tombstone.acceptance.predecessor_acceptance_id is None
            ):
                return False
            predecessor = await canonical.resolve_acceptance_read(
                tombstone.acceptance.predecessor_acceptance_id
            )
            return bool(
                predecessor is not None
                and predecessor.acceptance.workspace_id
                == authorization.workspace_id
                and predecessor.acceptance.environment_id
                == authorization.environment_id
                and predecessor.acceptance.source_object_id
                == authorization.source_object_id
                and predecessor.acceptance.access_policy_version_id
                == authorization.pre_tombstone_access_policy_version_id
                and predecessor.acceptance.access_policy_version_id
                != authorization.fresh_access_policy_version_id
            )
        except Exception:
            return False


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
    reappearance_authorization: GovernedReappearanceAuthorization | None = None

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
        if self.kind is SourceLifecycleKind.REAPPEARANCE:
            authorization = self.reappearance_authorization
            if authorization is None or (
                authorization.workspace_id != self.source.workspace_id
                or authorization.environment_id != self.source.environment_id
                or authorization.source_object_id != self.source.source_object_id
                or authorization.tombstone_revision_id
                != self.revision.reappearance_after_tombstone_revision_id
                or authorization.content_revision_id != self.revision.revision_id
                or authorization.fresh_admission_decision_id
                != self.canonical_acceptance.admission_decision_id
                or authorization.fresh_access_policy_version_id
                != self.canonical_acceptance.access_policy_version_id
                or authorization.pre_tombstone_access_policy_version_id
                == authorization.fresh_access_policy_version_id
            ):
                raise SourceLifecycleValidationError(
                    "Reappearance requires exact current identity and fresh authority."
                )
        elif self.reappearance_authorization is not None:
            raise SourceLifecycleValidationError(
                "Reappearance authority is forbidden for other lifecycle commands."
            )
        if self.kind in {
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
    reappearance_authority: GovernedReappearanceAuthority | None = None

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
                current = await uow.canonical.resolve_current(
                    command.source.source_object_id
                )
                if (
                    predecessor is None
                    or predecessor.source_object_id != command.source.source_object_id
                    or predecessor.workspace_id != command.source.workspace_id
                    or predecessor.environment_id != command.source.environment_id
                    or predecessor.kind is not RevisionKind.TOMBSTONE
                    or current is None
                    or current.workspace_id != command.source.workspace_id
                    or current.environment_id != command.source.environment_id
                    or current.source_object_id != command.source.source_object_id
                    or current.current_revision_id != predecessor_id
                    or current.current_revision_kind is not RevisionKind.TOMBSTONE
                ):
                    raise SourceLifecycleValidationError(
                        "Reappearance tombstone continuity is unavailable."
                    )
                authorization = command.reappearance_authorization
                authority = self.reappearance_authority
                try:
                    authorized = (
                        authorization is not None
                        and authority is not None
                        and await authority.is_current(
                            authorization,
                            canonical=uow.canonical,
                            current=current,
                        )
                    )
                except Exception:
                    authorized = False
                if not authorized:
                    raise SourceLifecycleValidationError(
                        "Reappearance authority is unavailable or stale."
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
    "CanonicalGovernedReappearanceAuthority",
    "GovernedReappearanceAuthorization",
    "GovernedReappearanceAuthorizationReader",
    "GovernedReappearanceAuthority",
    "ReappearanceCanonicalReader",
    "SourceLifecycleCommand",
    "SourceLifecycleCoordinator",
    "SourceLifecycleKind",
    "SourceLifecycleResult",
    "SourceLifecycleValidationError",
]
