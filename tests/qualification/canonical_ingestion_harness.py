"""Owned canonical-ingestion fixture for cross-provider qualification tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import AsyncIterable
from uuid import UUID

from spine.application.connector_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    ConnectorIngestionCommand,
    ConnectorIngestionOrchestrator,
    ConnectorIngestionResult,
    build_connector_canonical_acceptance_command,
)
from spine.application.diagnostics import AuditEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.storage import OriginalUploadOutcome
from spine.connectors.observations import NormalizedConnectorObservation
from spine.connectors.observations_v2 import NormalizedExternalObservationV2
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.object_storage.contracts import (
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ObservedContentIdentity,
    OriginalUploadCommandInput,
    OriginalUploadCommandResult,
    PreWriteRequestDigest,
    WriteReceipt,
)
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from tests.qualification.provider_adapter_harness import (
    ENVIRONMENT_ID,
    NOW,
    WORKSPACE_ID,
    stable_id,
)

POLICY_ID = stable_id("access-policy")


class RecordingOutboxEventRegistry(OutboxEventRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.built_intents: list[object] = []

    def build_intent(self, **values: object) -> object:
        intent = super().build_intent(**values)
        self.built_intents.append(intent)
        return intent


class EligiblePolicyRepository:
    async def resolve_current(
        self, source_object_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> object:
        del source_object_id, workspace_id, environment_id
        return SimpleNamespace(access_policy_version_id=POLICY_ID)

    async def resolve_policy(
        self, policy_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> object | None:
        del workspace_id, environment_id
        if policy_id != POLICY_ID:
            return None
        return SimpleNamespace(
            allows_service_processing=lambda principal, purpose, operation, now: (
                principal == stable_id("service")
                and purpose == "connector.ingestion.v1"
                and operation == "canonical_acceptance"
            )
        )


class QualificationUploader:
    async def upload(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
        chunks: AsyncIterable[bytes],
    ) -> OriginalUploadOutcome:
        del context
        content = b"".join([part async for part in chunks])
        digest = hashlib.sha256(content).hexdigest()
        reference = ObjectReference(
            schema_version=1,
            object_id=stable_id(f"object:{digest}"),
            storage_generation=stable_id(f"generation:{digest}"),
            digest_algorithm="sha256",
            digest_hex=digest,
            byte_length=len(content),
        )
        receipt = WriteReceipt(
            schema_version=1,
            upload_id=stable_id(f"upload:{command.idempotency_key}"),
            object_reference=reference,
            observed_content=ObservedContentIdentity(
                observed_sha256=digest, observed_byte_length=len(content)
            ),
            verification=IntegrityResult(
                status=IntegrityStatus.VERIFIED,
                algorithm="sha256",
                observed_digest=digest,
                observed_length=len(content),
            ),
            storage_status="finalized_verified",
            finalized_at=NOW,
        )
        result = OriginalUploadCommandResult(
            request_digest=PreWriteRequestDigest(
                algorithm="sha256",
                algorithm_version="spine.command-digest.v1",
                digest_hex="a" * 64,
            ),
            upload_id=receipt.upload_id,
            object_id=reference.object_id,
            issue8_result_reference={
                "receipt_id": stable_id(f"receipt:{command.idempotency_key}")
            },
        )
        return OriginalUploadOutcome(result=result, write_receipt=receipt, replay=False)


class UnusedReader:
    async def read(
        self, command: ConnectorIngestionCommand, context: TrustedPersistenceContext
    ) -> NormalizedConnectorObservation | NormalizedExternalObservationV2:
        del command, context
        raise AssertionError("qualification supplies a pre-read adapter observation")


@dataclass(frozen=True, slots=True)
class CanonicalIngestionHarness:
    orchestrator: ConnectorIngestionOrchestrator
    events: RecordingOutboxEventRegistry

    @property
    def projection_intent_count(self) -> int:
        return sum(
            getattr(intent, "event_type", None) == PROJECTION_REQUEST_EVENT_TYPE
            for intent in self.events.built_intents
        )

    async def ingest_twice(
        self,
        command: ConnectorIngestionCommand,
        observation: NormalizedConnectorObservation | NormalizedExternalObservationV2,
    ) -> tuple[ConnectorIngestionResult, ConnectorIngestionResult]:
        first = await self.orchestrator.ingest_observation(command, observation)
        replay = await self.orchestrator.ingest_observation(command, observation)
        return first, replay


async def build_canonical_ingestion_harness() -> CanonicalIngestionHarness:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=stable_id("issuer"), secret=b"issue-677-qualification-secret-32"
    )
    events = RecordingOutboxEventRegistry()
    audits = AuditEventRegistry.with_default_families()
    authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=events,
        audit_events=audits,
        bootstrap_authority=authority,
        canonical_policy_repository=EligiblePolicyRepository(),
        canonical_clock=lambda: NOW,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        authority,
        Workspace(id=WORKSPACE_ID, slug="issue-677", display_name="Issue 677"),
    )
    async with persistence.uow_factory(
        boundary.worker(
            scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
            service_principal_id=stable_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=stable_id("environment-trace"),
        )
    ) as uow:
        await uow.environments.add(
            Environment(
                id=ENVIRONMENT_ID,
                workspace_id=WORKSPACE_ID,
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="qualification",
            )
        )
        await uow.commit()

    def context_factory(
        operation: PersistenceOperation, trace_id: UUID
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
            ),
            service_principal_id=stable_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=operation,
            trace_id=trace_id,
        )

    return CanonicalIngestionHarness(
        orchestrator=ConnectorIngestionOrchestrator(
            uow_factory=persistence.uow_factory,
            observation_reader=UnusedReader(),
            uploader=QualificationUploader(),
            read_context_factory=context_factory,
            context_factory=context_factory,
            canonical_command_factory=lambda **values: build_connector_canonical_acceptance_command(
                outbox_events=events, audit_events=audits, **values
            ),
            outbox_events=events,
            audit_events=audits,
            read_operation=PersistenceOperation("provider_connection_read"),
            admission_reference_type="provider_read_admission",
            clock=lambda: datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
        ),
        events=events,
    )


def ingestion_command(
    observation: NormalizedConnectorObservation | NormalizedExternalObservationV2,
    *,
    identity: str,
) -> ConnectorIngestionCommand:
    return ConnectorIngestionCommand(
        workspace_id=observation.workspace_id,
        environment_id=observation.environment_id,
        command_identity=f"qualification.issue677.{identity}",
        idempotency_key=f"issue677:{identity}",
        connection_id=observation.connection_id,
        resource_scope_reference=(
            observation.resource_scope_reference.value
            if isinstance(observation, NormalizedExternalObservationV2)
            else observation.resource_scope_reference
        ),
        authorization_generation=observation.authorization_generation,
        configuration_generation=observation.configuration_generation,
        resource_object_reference=(
            observation.external_identity.provider_key.provider_object_id
            if isinstance(observation, NormalizedExternalObservationV2)
            and hasattr(
                observation.external_identity.provider_key, "provider_object_id"
            )
            else None
        ),
        access_policy_version_id=POLICY_ID,
    )
