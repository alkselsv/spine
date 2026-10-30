"""Reusable fake-backed conformance seam for provider source qualifications."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.authorized_context_broker import (
    AuthorizedContextBrokerApplicationService,
    AuthorizedContextRequest,
    ContextBrokerReadModel,
    InMemoryContextBundleStore,
)
from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScopeOutcome,
    AuthorizedRetrievalScopeReason,
    AuthorizedRetrievalScopeResolution,
)
from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionResult,
)
from spine.application.content_authorization import (
    AuthorizationContextMode,
    TrustedAuthorizationContext,
)
from spine.application.context_profiles import (
    ContextProfileResolution,
    ContextProfileResolutionOutcome,
    ContextProfileResolutionReason,
    EffectiveContextRetrievalPolicy,
)
from spine.application.diagnostics import DiagnosticContext
from spine.application.evidence_lineage_verification import VerifiedEvidenceItem
from spine.connectors.google_observation_compatibility import (
    google_v1_observation_to_v2,
)
from spine.connectors.observations import (
    ConnectorCoverage,
    NormalizedConnectorObservation,
)
from spine.connectors.observations_v2 import (
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderStructuredObjectKey,
    ProviderTabularRecordKey,
    StructuredObservationRepresentation,
    TabularObservationRepresentation,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderKind,
)
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.ingestion import ProcessingReceipt
from spine.domain.projections import (
    ProjectionReconciliationOutcome,
    ProjectionReconciliationResult,
    projection_reconciliation_findings_digest,
)
from spine.domain.sources import (
    CanonicalBoundary,
    SourceRevision,
    SourceRevisionProvenance,
)
from spine.domain.sources.google import (
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    google_source_identity,
)
from spine.memory.contracts import (
    ContextOutcome,
    EvidenceReference,
    ProjectionChunk,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    PublicationPin,
    RetrievalReadContext,
    StableLocator,
)
from tests.qualification.authorized_retrieval_harness import retrieve_canonical_revision
from tests.qualification.canonical_ingestion_harness import (
    build_canonical_ingestion_harness,
    ingestion_command,
)
from tests.qualification.provider_adapter_harness import (
    BINARY_PROVIDER_CASES,
    ENVIRONMENT_ID,
    NOW,
    WORKSPACE_ID,
    BinaryProviderCase,
    read_provider_observation,
    read_structured_observation,
    read_tabular_observation,
    stable_id,
)
from tests.qualification.retrieval_fixture import (
    qualification_physical_context,
    qualification_projection_config,
    qualification_publication,
)


async def representation_observations(
    root: Path,
) -> tuple[NormalizedExternalObservationV2, ...]:
    """Return equivalent typed payloads without flattening provider provenance."""

    binary, _ = await read_provider_observation(BINARY_PROVIDER_CASES[3], root)
    tabular = await read_tabular_observation()
    structured = await read_structured_observation()
    return binary, tabular, structured


@dataclass(frozen=True, slots=True)
class IngestedProviderCase:
    case: BinaryProviderCase
    first: ConnectorIngestionResult
    replay: ConnectorIngestionResult
    admission_count: int
    outbox_intent_count: int


async def ingest_pre_read_observation(
    observation: NormalizedExternalObservationV2,
) -> tuple[ConnectorIngestionResult, int]:
    harness = await build_canonical_ingestion_harness()
    first, _ = await harness.ingest_twice(
        ingestion_command(
            observation, identity=f"representation-{observation.representation.kind}"
        ),
        observation,
    )
    return first, harness.projection_intent_count


async def ingest_case(case: BinaryProviderCase, root: Path) -> IngestedProviderCase:
    observation, admission_count = await read_provider_observation(case, root)
    harness = await build_canonical_ingestion_harness()
    first, replay = await harness.ingest_twice(
        ingestion_command(observation, identity=case.name), observation
    )
    return IngestedProviderCase(
        case=case,
        first=first,
        replay=replay,
        admission_count=admission_count,
        outbox_intent_count=harness.projection_intent_count,
    )
