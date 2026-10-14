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
from spine.domain.sources import (
    AdmissionDecision,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
)
from spine.domain.sources.profile import OBSERVATION_SCHEMA
from spine.domain.workspaces import Environment, Workspace

from spine.application.persistence.context_profiles import ContextProfileRepository

SOURCE_OBSERVATION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourceObservationResult:
    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    replay: bool
    claim: IdempotencyClaimResult
    result_reference: OpaqueResultReference
    receipt_association: SourceRevisionReceiptAssociation | None = None


@dataclass(frozen=True, slots=True)
class SourceObservationCommand:
    """Typed source mutation carrying the existing idempotency identity."""

    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    idempotency_key: IdempotencyKey
    digest: CommandDigest
    receipt_association: SourceRevisionReceiptAssociation | None = None

    @classmethod
    def create(
        cls,
        *,
        source: SourceObject,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        idempotency_key: IdempotencyKey,
        receipt_association: SourceRevisionReceiptAssociation | None = None,
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
            receipt_association=receipt_association,
        )
        return cls(
            source=source,
            revision=revision,
            provenance=provenance,
            idempotency_key=idempotency_key,
            digest=placeholder.expected_digest(PersistenceOperation("source_observation")),
            receipt_association=receipt_association,
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
                "receipt_association": (
                    {
                        "association_id": self.receipt_association.association_id,
                        "provenance_id": self.receipt_association.provenance_id,
                        "original_receipt_id": self.receipt_association.original_receipt_id,
                        "original_command_result_id": self.receipt_association.original_command_result_id,
                        "original_command_result_schema_version": self.receipt_association.original_command_result_schema_version,
                        "original_upload_id": self.receipt_association.original_upload_id,
                        "original_reference": self.receipt_association.original_reference.model_dump(mode="json"),
                        "original_sha256": self.receipt_association.original_sha256,
                        "byte_length": self.receipt_association.byte_length,
                    }
                    if self.receipt_association is not None
                    else None
                ),
            },
        )


class SourceObservationRepository(Protocol):
    async def resolve_source(self, source_object_id: UUID) -> SourceObject | None: ...

    async def resolve_or_create_source(self, source: SourceObject) -> SourceObject: ...

    async def record_observation(self, command: SourceObservationCommand) -> SourceObservationResult: ...

    async def resolve_revision(self, revision_id: UUID) -> SourceRevision | None: ...

    async def resolve_receipt_associations(
        self, revision_id: UUID
    ) -> tuple[SourceRevisionReceiptAssociation, ...]: ...


# Kept as a convenience import for callers that discover all application
# persistence ports from this module. The implementation lives in its own
# module to keep this source-observation contract small.
from spine.application.persistence.ingestion import IngestionRepository  # noqa: E402

class AdmissionRepository(Protocol):
    """Append-only admission decision repository.

    Implementations enforce the predecessor/version CAS chain.  The repository
    does not infer admission from parser or projection state.
    """

    async def append_decision(self, decision: AdmissionDecision) -> AdmissionDecision: ...

    async def resolve(self, decision_id: UUID) -> AdmissionDecision | None: ...

    async def resolve_effective(self, revision_id: UUID) -> AdmissionDecision | None: ...
from spine.application.persistence.idempotency import IdempotencyRepository


class WorkspaceRepository(Protocol):
    async def add(self, workspace: Workspace) -> None: ...

    async def resolve(self, workspace_id: UUID) -> Workspace | None: ...


class EnvironmentRepository(Protocol):
    async def add(self, environment: Environment) -> None: ...

    async def resolve(self, environment_id: UUID) -> Environment | None: ...


__all__ = [
    "EnvironmentRepository",
    "IngestionRepository",
    "AdmissionRepository",
    "IdempotencyRepository",
    "SourceObservationRepository",
    "SourceObservationCommand",
    "SourceObservationResult",
    "SOURCE_OBSERVATION_SCHEMA_VERSION",
    "WorkspaceRepository",
    "ContextProfileRepository",
]
