"""Purpose-specific canonical Workspace and Environment repositories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    OpaqueResultReference,
)
from spine.application.persistence.context import PersistenceOperation
from spine.domain.sources import SourceObject, SourceRevision, SourceRevisionProvenance
from spine.domain.sources.profile import OBSERVATION_SCHEMA
from spine.domain.workspaces import Environment, Workspace

SOURCE_OBSERVATION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourceObservationResult:
    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    replay: bool
    claim: IdempotencyClaimResult
    result_reference: OpaqueResultReference


@dataclass(frozen=True, slots=True)
class SourceObservationCommand:
    """Typed source mutation carrying the existing idempotency identity."""

    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls,
        *,
        source: SourceObject,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        idempotency_key: IdempotencyKey,
    ) -> "SourceObservationCommand":
        placeholder = cls(
            source=source,
            revision=revision,
            provenance=provenance,
            idempotency_key=idempotency_key,
            digest=digest_command(
                operation=PersistenceOperation("source_observation"),
                operation_schema_version=SOURCE_OBSERVATION_SCHEMA_VERSION,
                payload={"placeholder": True},
            ),
        )
        return cls(
            source=source,
            revision=revision,
            provenance=provenance,
            idempotency_key=idempotency_key,
            digest=placeholder.expected_digest(PersistenceOperation("source_observation")),
        )

    def expected_digest(self, operation: PersistenceOperation) -> CommandDigest:
        """Build the Issue #41 digest from all safe observation command fields."""

        source = self.source
        reference = self.revision.original_reference
        return digest_command(
            operation=operation,
            operation_schema_version=SOURCE_OBSERVATION_SCHEMA_VERSION,
            payload={
                "schema": OBSERVATION_SCHEMA,
                "source": {
                    "source_object_id": source.source_object_id,
                    "identity_mode": source.identity_mode,
                    "connection_id": source.connection_id,
                    "external_namespace": source.external_namespace,
                    "external_generation": source.external_generation,
                    "external_object_id": source.external_object_id,
                    "upload_identity": source.upload_identity,
                },
                "revision_digest": self.revision.revision_digest,
                "original_reference": (
                    {
                        "schema_version": reference.schema_version,
                        "object_id": reference.object_id,
                        "storage_generation": reference.storage_generation,
                        "digest_algorithm": reference.digest_algorithm,
                        "digest_hex": reference.digest_hex,
                        "byte_length": reference.byte_length,
                    }
                    if reference is not None else None
                ),
                "ordering": {
                    "scheme": self.provenance.order_scheme,
                    "token": self.provenance.order_token,
                },
                "provenance": {
                    "producer_kind": self.provenance.producer_kind,
                    "producer_reference": self.provenance.producer_reference,
                    "event_identity": self.provenance.event_identity,
                    "event_digest": self.provenance.event_digest,
                    "connection_id": self.provenance.connection_id,
                    "upload_command_reference": self.provenance.upload_command_reference,
                    "origin_locator_kind": self.provenance.origin_locator_kind,
                    "origin_locator_value": self.provenance.origin_locator_value,
                    "origin_locator_schema": self.provenance.origin_locator_schema,
                    "origin_locator_digest": self.provenance.origin_locator_digest,
                    "observer_service": self.provenance.observer_service,
                },
                "idempotency_key": self.idempotency_key.value,
            },
        )


class SourceObservationRepository(Protocol):
    async def resolve_or_create_source(self, source: SourceObject) -> SourceObject: ...

    async def record_observation(self, command: SourceObservationCommand) -> SourceObservationResult: ...

    async def resolve_revision(self, revision_id: UUID) -> SourceRevision | None: ...
from spine.application.persistence.idempotency import IdempotencyRepository


class WorkspaceRepository(Protocol):
    async def add(self, workspace: Workspace) -> None: ...

    async def resolve(self, workspace_id: UUID) -> Workspace | None: ...


class EnvironmentRepository(Protocol):
    async def add(self, environment: Environment) -> None: ...

    async def resolve(self, environment_id: UUID) -> Environment | None: ...


__all__ = [
    "EnvironmentRepository",
    "IdempotencyRepository",
    "SourceObservationRepository",
    "SourceObservationCommand",
    "SourceObservationResult",
    "SOURCE_OBSERVATION_SCHEMA_VERSION",
    "WorkspaceRepository",
]
