from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from uuid import UUID

import pytest

from spine.application.connections.provider import (
    ProviderCapabilityUnavailableError,
    ProviderReadAdmission,
    ProviderReadCommand,
    ProviderReadDeniedError,
)
from spine.application.lifecycle import LifecycleEligibilityQuery
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    LifecycleObservationRepresentation,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderFileKey,
    ProviderLifecycleState,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources.lifecycle import (
    CurrentLifecycleEligibility,
    LifecycleEligibilityStatus,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def _context(
    *, workspace_id: UUID = _id(4), environment_id: UUID = _id(5)
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=workspace_id, environment_id=environment_id
        ),
        origin=ContextOrigin.WORKER,
        purpose=PersistencePurpose("connector.ingestion.v1"),
        operation=PersistenceOperation("provider_connection_read"),
        trace_id=_id(70),
        acting_subject_id=None,
        service_principal_id=_id(6),
        provenance=TrustedContextProvenance(
            issuer_id=_id(71), signature=b"trusted-test-context"
        ),
    )


def _capability(
    kind: ProviderCapabilityKind,
    status: ProviderCapabilityStatus,
) -> ProviderCapability:
    return ProviderCapability(
        kind=kind,
        status=status,
        qualification_reference=(
            OpaqueProviderReference(value=_id(50 + len(kind.value)))
            if status is ProviderCapabilityStatus.SUPPORTED
            else None
        ),
    )


def _connection(
    *capabilities: ProviderCapability,
    workspace_id: UUID = _id(4),
    environment_id: UUID = _id(5),
    status: ProviderConnectionStatus = ProviderConnectionStatus.ACTIVE,
) -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=_id(1),
        connection_id=_id(2),
        approval_id=_id(3),
        version=1,
        workspace_id=workspace_id,
        environment_id=environment_id,
        service_principal_id=_id(6),
        provider_kind=ProviderKind.GOOGLE,
        credential_reference=OpaqueProviderReference(value=_id(7)),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        approved_resource_scope_references=(OpaqueProviderReference(value=_id(9)),),
        authorization_generation=2,
        configuration_generation=3,
        status=status,
        capabilities=capabilities,
    )


class _Repository:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection
        self.calls = []

    @asynccontextmanager
    async def admit_read(self, command, context):
        assert isinstance(context, TrustedPersistenceContext)
        assert isinstance(context.scope, EnvironmentScope)
        assert context.scope.workspace_id == command.workspace_id
        assert context.scope.environment_id == command.environment_id
        self.calls.append((command, context))
        yield self.connection


class _Provider:
    def __init__(self, observation: NormalizedExternalObservationV2 | None = None) -> None:
        self.observation = observation
        self.calls = 0

    async def read(self, connection, command):
        del connection, command
        self.calls += 1
        assert self.observation is not None
        return self.observation


class _LifecycleReader:
    def __init__(self, *statuses: LifecycleEligibilityStatus) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    async def current_eligibility(self, query, context):
        del context
        self.calls += 1
        status = self.statuses[min(self.calls - 1, len(self.statuses) - 1)]
        return CurrentLifecycleEligibility(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            source_object_id=query.source_object_id,
            connection_id=query.connection_id,
            connection_version_id=query.connection_version_id,
            scope_reference=query.scope_reference,
            scope_version_id=query.scope_version_id,
            scope_digest=query.scope_digest,
            current_decision_id=_id(100),
            generation=1,
            status=status,
            legal_hold_active=False,
        )


def _command(
    *required: ProviderCapabilityKind,
    workspace_id: UUID = _id(4),
    environment_id: UUID = _id(5),
) -> ProviderReadCommand:
    return ProviderReadCommand(
        workspace_id=workspace_id,
        environment_id=environment_id,
        connection_id=_id(2),
        connection_version_id=_id(1),
        resource_scope_reference=OpaqueProviderReference(value=_id(9)),
        authorization_generation=2,
        configuration_generation=3,
        required_capabilities=required,
        resource_object_reference="file-1",
    )


def _lifecycle_query() -> LifecycleEligibilityQuery:
    return LifecycleEligibilityQuery(
        workspace_id=_id(4),
        environment_id=_id(5),
        source_object_id=_id(30),
        connection_id=_id(2),
        connection_version_id=_id(1),
        scope_reference=_id(9),
        scope_version_id=_id(3),
        scope_digest="a" * 64,
        provider_object_reference="file-1",
    )


@pytest.mark.asyncio
async def test_active_scoped_connection_allows_qualified_binary_read() -> None:
    connection = _connection(
        _capability(
            ProviderCapabilityKind.BINARY_READ, ProviderCapabilityStatus.SUPPORTED
        )
    )
    repository = _Repository(connection)
    observation = _observation(assurance=ExternalIdentityAssurance.UNPROVEN)
    provider = _Provider(observation)
    admission = ProviderReadAdmission(repository=repository, provider=provider)
    command = _command(ProviderCapabilityKind.BINARY_READ)

    context = _context()
    result = await admission.read(command, context)

    assert result == observation
    assert provider.calls == 1
    assert repository.calls[0][0] == command
    assert repository.calls[0][1] is context


@pytest.mark.asyncio
async def test_withdrawn_source_is_denied_before_provider_read() -> None:
    connection = _connection(
        _capability(ProviderCapabilityKind.BINARY_READ, ProviderCapabilityStatus.SUPPORTED)
    )
    provider = _Provider(_observation(assurance=ExternalIdentityAssurance.UNPROVEN))
    lifecycle = _LifecycleReader(LifecycleEligibilityStatus.READ_WITHDRAWN)
    admission = ProviderReadAdmission(
        repository=_Repository(connection),
        provider=provider,
        lifecycle_reader=lifecycle,
    )
    command = _command(ProviderCapabilityKind.BINARY_READ)
    command = replace(command, lifecycle_query=_lifecycle_query())

    with pytest.raises(ProviderReadDeniedError):
        await admission.read(command, _context())

    assert lifecycle.calls == 1
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_configured_lifecycle_authority_rejects_unbound_provider_read() -> None:
    connection = _connection(
        _capability(ProviderCapabilityKind.BINARY_READ, ProviderCapabilityStatus.SUPPORTED)
    )
    provider = _Provider(_observation(assurance=ExternalIdentityAssurance.UNPROVEN))
    lifecycle = _LifecycleReader(LifecycleEligibilityStatus.ELIGIBLE)
    admission = ProviderReadAdmission(
        repository=_Repository(connection),
        provider=provider,
        lifecycle_reader=lifecycle,
    )

    with pytest.raises(ProviderReadDeniedError):
        await admission.read(_command(ProviderCapabilityKind.BINARY_READ), _context())

    assert provider.calls == 0
    assert lifecycle.calls == 0


def test_lifecycle_query_rejects_different_requested_provider_object() -> None:
    with pytest.raises(ValueError, match="exact provider read scope"):
        replace(
            _command(),
            resource_object_reference="another-file",
            lifecycle_query=_lifecycle_query(),
        )


@pytest.mark.asyncio
async def test_withdrawal_after_download_discards_provider_observation() -> None:
    connection = _connection(
        _capability(ProviderCapabilityKind.BINARY_READ, ProviderCapabilityStatus.SUPPORTED)
    )
    provider = _Provider(_observation(assurance=ExternalIdentityAssurance.UNPROVEN))
    lifecycle = _LifecycleReader(
        LifecycleEligibilityStatus.ELIGIBLE,
        LifecycleEligibilityStatus.READ_WITHDRAWN,
    )
    admission = ProviderReadAdmission(
        repository=_Repository(connection),
        provider=provider,
        lifecycle_reader=lifecycle,
    )
    command = _command(ProviderCapabilityKind.BINARY_READ)
    command = replace(command, lifecycle_query=_lifecycle_query())

    with pytest.raises(ProviderReadDeniedError):
        await admission.read(command, _context())

    assert lifecycle.calls == 2
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_cross_workspace_read_is_denied_before_provider_call_without_leakage() -> None:
    connection = _connection(
        _capability(
            ProviderCapabilityKind.BINARY_READ, ProviderCapabilityStatus.SUPPORTED
        )
    )
    provider = _Provider(
        _observation(assurance=ExternalIdentityAssurance.UNPROVEN)
    )
    admission = ProviderReadAdmission(
        repository=_Repository(connection), provider=provider
    )

    with pytest.raises(ProviderReadDeniedError) as failure:
        await admission.read(
            _command(
                ProviderCapabilityKind.BINARY_READ,
                workspace_id=_id(40),
            ),
            _context(workspace_id=_id(40)),
        )

    assert str(failure.value) == "Provider connection read denied."
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection", "command"),
    (
        (_connection(environment_id=_id(50)), _command()),
        (
            _connection(status=ProviderConnectionStatus.REVOKED),
            _command(),
        ),
    ),
    ids=("wrong_environment", "revoked"),
)
async def test_mismatched_or_inactive_connection_is_denied_before_provider_call(
    connection: ProviderConnection,
    command: ProviderReadCommand,
) -> None:
    provider = _Provider(
        _observation(assurance=ExternalIdentityAssurance.UNPROVEN)
    )
    admission = ProviderReadAdmission(
        repository=_Repository(connection), provider=provider
    )

    with pytest.raises(ProviderReadDeniedError):
        await admission.read(command, _context())

    assert provider.calls == 0


def _observation(
    *, assurance: ExternalIdentityAssurance
) -> NormalizedExternalObservationV2:
    content = b"provider-neutral file"
    return NormalizedExternalObservationV2(
        workspace_id=_id(4),
        environment_id=_id(5),
        connection_id=_id(2),
        connection_version_id=_id(1),
        provider_kind=ProviderKind.GOOGLE,
        resource_scope_reference=OpaqueProviderReference(value=_id(9)),
        authorization_generation=2,
        configuration_generation=3,
        source_kind="document",
        external_identity=ProviderExternalIdentity(
            external_namespace="google",
            external_generation="google-source-key:v1",
            external_object_id="gsk1:" + "a" * 52,
            provider_key=ProviderFileKey(
                connection_id=_id(2), provider_object_id="file-1"
            ),
            assurance=assurance,
            assurance_evidence_reference="google-drive-file-id:v1",
        ),
        representation=BinaryObservationRepresentation(
            original_bytes=content,
            media_type="application/pdf",
            extraction_profile="google-drive-download:v1",
            locator_contract_version="locator:google-drive-file:v1",
        ),
        coverage=ConnectorCoverage.COMPLETE,
        producer_reference="google-drive-reader",
        event_identity="event-1",
        event_digest=hashlib.sha256(content).hexdigest(),
        observer_service="connector-worker",
        received_at=NOW,
        observed_at=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (ProviderCapabilityStatus.UNSUPPORTED, ProviderCapabilityStatus.UNQUALIFIED),
)
async def test_required_guarantee_is_rejected_before_provider_call(
    status: ProviderCapabilityStatus,
) -> None:
    connection = _connection(
        _capability(ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY, status)
    )
    provider = _Provider()
    admission = ProviderReadAdmission(
        repository=_Repository(connection), provider=provider
    )

    with pytest.raises(ProviderCapabilityUnavailableError):
        await admission.read(
            _command(ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY), _context()
        )

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_unqualified_stable_identity_cannot_be_returned_as_provider_stable() -> None:
    connection = _connection(
        _capability(
            ProviderCapabilityKind.BINARY_READ, ProviderCapabilityStatus.SUPPORTED
        ),
        _capability(
            ProviderCapabilityKind.STABLE_IDENTITY,
            ProviderCapabilityStatus.UNQUALIFIED,
        ),
    )
    provider = _Provider(
        _observation(assurance=ExternalIdentityAssurance.PROVIDER_STABLE)
    )
    admission = ProviderReadAdmission(
        repository=_Repository(connection), provider=provider
    )

    with pytest.raises(ProviderCapabilityUnavailableError):
        await admission.read(
            _command(ProviderCapabilityKind.BINARY_READ), _context()
        )

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_unqualified_lifecycle_evidence_cannot_claim_confirmed_deletion() -> None:
    connection = _connection(
        _capability(
            ProviderCapabilityKind.LIFECYCLE_EVIDENCE,
            ProviderCapabilityStatus.UNQUALIFIED,
        )
    )
    observation = _observation(
        assurance=ExternalIdentityAssurance.UNPROVEN
    ).model_copy(
        update={
            "representation": LifecycleObservationRepresentation(
                state=ProviderLifecycleState.CONFIRMED_DELETED,
                evidence_reference="reconciliation-1",
                evidence_digest="0" * 64,
            )
        }
    )
    provider = _Provider(observation)
    admission = ProviderReadAdmission(
        repository=_Repository(connection), provider=provider
    )

    with pytest.raises(ProviderCapabilityUnavailableError):
        await admission.read(_command(), _context())

    assert provider.calls == 1
