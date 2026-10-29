from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.mounted_filesystem_reconciliation import (
    AuthorizedMountedFilesystemScanner,
    InMemoryMountedFilesystemRemovalPolicyAdmission,
    InMemoryMountedFilesystemReconciliationRepository,
    MountedFilesystemCanonicalSource,
    MountedFilesystemLifecycleState,
    MountedFilesystemReconciliationCommand,
    MountedFilesystemReconciliationCoordinator,
    MountedFilesystemReconciliationState,
    MountedFilesystemRemovalPolicy,
)
from spine.application.connections.provider import ProviderReadDeniedError
from spine.connectors.files.mounted_filesystem import (
    MountedFilesystemReadError,
    MountedFilesystemRoot,
    MountedFilesystemTreeScanner,
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
from spine.domain.sources import RevisionKind

NOW = datetime(2026, 10, 29, 18, tzinfo=timezone.utc)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT = UUID("22222222-2222-4222-8222-222222222222")
CONNECTION = UUID("33333333-3333-4333-8333-333333333333")
CONNECTION_VERSION = UUID("34343434-3434-4434-8434-343434343434")
SCOPE = UUID("44444444-4444-4444-8444-444444444444")
POLICY = UUID("55555555-5555-4555-8555-555555555555")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"test:mounted-reconciliation:{value}")


def _connection() -> ProviderConnection:
    qualification = OpaqueProviderReference(value=_id("qualification"))
    return ProviderConnection(
        connection_version_id=CONNECTION_VERSION,
        connection_id=CONNECTION,
        approval_id=_id("approval"),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=_id("principal"),
        provider_kind=ProviderKind.MOUNTED_FILESYSTEM,
        credential_reference=OpaqueProviderReference(value=_id("credential")),
        grant_reference=OpaqueProviderReference(value=_id("grant")),
        approved_resource_scope_references=(OpaqueProviderReference(value=SCOPE),),
        authorization_generation=3,
        configuration_generation=4,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=tuple(
            ProviderCapability(
                kind=kind,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=qualification,
            )
            for kind in (
                ProviderCapabilityKind.ENUMERATION,
                ProviderCapabilityKind.BINARY_READ,
                ProviderCapabilityKind.FULL_RECONCILIATION,
                ProviderCapabilityKind.LIFECYCLE_EVIDENCE,
            )
        ),
    )


def _command(key: str = "scan-1") -> MountedFilesystemReconciliationCommand:
    return MountedFilesystemReconciliationCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        connection_version_id=CONNECTION_VERSION,
        resource_scope_reference=SCOPE,
        authorization_generation=3,
        configuration_generation=4,
        access_policy_version_id=_id("access-policy"),
        idempotency_key=key,
    )


def _policy() -> MountedFilesystemRemovalPolicy:
    return MountedFilesystemRemovalPolicy(
        policy_version_id=POLICY,
        approval_id=_id("policy-approval"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorize_complete_scope_absence=True,
        approved_at=NOW,
    )


def _source(path: str, revision: str, *, tombstone: bool = False):
    return MountedFilesystemCanonicalSource(
        provider_object_reference=path,
        source_object_id=_id(f"source:{path}:{'old' if tombstone else 'current'}"),
        current_revision_id=_id(f"revision:{path}:{revision}"),
        current_revision_kind=(
            RevisionKind.TOMBSTONE if tombstone else RevisionKind.CONTENT
        ),
    )


def _evidence_for(result, path: str):
    path_digest = hashlib.sha256(path.encode()).hexdigest()
    return next(
        item
        for item in result.lifecycle_evidence
        if item.provider_object_reference_digest == path_digest
    )


class ConnectionRepository:
    @asynccontextmanager
    async def admit_read(self, command, context):
        del command, context
        yield _connection()


class Sources:
    def __init__(self, values=()):
        self.values = tuple(values)

    async def list_sources(self, command, context):
        del command, context
        return self.values


class Ingestion:
    def __init__(self, root: Path, *, failures: int = 0):
        self.root = root
        self.failures = failures
        self.commands = []
        self.logical_revisions: set[tuple[str, str]] = set()

    async def ingest(self, command):
        self.commands.append(command)
        if self.failures:
            self.failures -= 1
            raise MountedFilesystemReadError()
        content = (self.root / command.resource_object_reference).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        self.logical_revisions.add((command.resource_object_reference, digest))
        return SimpleNamespace(
            source=SimpleNamespace(
                source_object_id=_id(
                    f"source:{command.resource_object_reference}:"
                    f"{command.external_identity_occurrence or 'initial'}"
                )
            ),
            revision=SimpleNamespace(
                revision_id=_id(
                    f"revision:{command.resource_object_reference}:{digest}"
                ),
                kind=RevisionKind.CONTENT,
            ),
        )


class MutatingIngestion(Ingestion):
    async def ingest(self, command):
        result = await super().ingest(command)
        (self.root / command.resource_object_reference).write_text("changed-after-read")
        return result


class Removals:
    def __init__(self):
        self.calls = []

    async def confirm_absent(
        self, command, source, *, policy, idempotency_key, evidence_reference, context
    ):
        self.calls.append(
            (command, source, idempotency_key, evidence_reference, context, policy)
        )
        return _id(f"tombstone:{source.source_object_id}")


def _service(root: Path, *, sources=(), ingestion=None, removals=None, attempts=2):
    provider = MountedFilesystemTreeScanner(
        roots={
            CONNECTION_VERSION: MountedFilesystemRoot(
                resource_scope_reference=OpaqueProviderReference(value=SCOPE),
                path=root,
            )
        }
    )
    scanner = AuthorizedMountedFilesystemScanner(
        repository=ConnectionRepository(), provider=provider
    )
    return MountedFilesystemReconciliationCoordinator(
        repository=InMemoryMountedFilesystemReconciliationRepository(),
        scanner=scanner,
        ingestion=ingestion or Ingestion(root),
        sources=Sources(sources),
        removals=removals or Removals(),
        removal_policies=InMemoryMountedFilesystemRemovalPolicyAdmission((_policy(),)),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity
        ),
        clock=lambda: NOW,
        max_read_attempts=attempts,
    )


@pytest.mark.asyncio
async def test_missing_root_marks_connection_unavailable_without_mass_tombstone(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    removals = Removals()
    service = _service(
        root,
        sources=(_source("missing.md", "old"),),
        removals=removals,
    )
    root.rename(tmp_path / "detached-mount")

    result = await service.run(_command(), _policy())

    assert result.state is MountedFilesystemReconciliationState.UNAVAILABLE
    assert result.connection_available is False
    assert result.confirmed_scan_generation is None
    assert [item.state for item in result.lifecycle_evidence] == [
        MountedFilesystemLifecycleState.TEMPORARILY_UNAVAILABLE
    ]
    assert removals.calls == []


@pytest.mark.asyncio
async def test_complete_generation_distinguishes_created_changed_and_confirmed_absent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "created.md").write_text("new")
    (root / "changed.md").write_text("after")
    changed = _source("changed.md", "before")
    absent = _source("absent.md", "before")
    removals = Removals()
    service = _service(root, sources=(changed, absent), removals=removals)

    result = await service.run(_command(), _policy())

    assert result.state is MountedFilesystemReconciliationState.COMPLETE
    assert result.confirmed_scan_generation is not None
    assert (
        _evidence_for(result, "created.md").state
        is MountedFilesystemLifecycleState.OBSERVED
    )
    assert (
        _evidence_for(result, "changed.md").state
        is MountedFilesystemLifecycleState.CHANGED
    )
    assert (
        _evidence_for(result, "absent.md").state
        is MountedFilesystemLifecycleState.CONFIRMED_ABSENT
    )
    assert len(removals.calls) == 1


@pytest.mark.asyncio
async def test_path_scoped_rename_is_old_absence_plus_new_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "new-name.md").write_text("same bytes")
    old = _source("old-name.md", "old")
    service = _service(root, sources=(old,))

    result = await service.run(_command(), _policy())

    observed = _evidence_for(result, "new-name.md")
    absent = _evidence_for(result, "old-name.md")
    assert observed.state is MountedFilesystemLifecycleState.OBSERVED
    assert absent.state is MountedFilesystemLifecycleState.CONFIRMED_ABSENT
    assert observed.identity_continuity_proven is False
    assert observed.source_object_id != old.source_object_id


@pytest.mark.asyncio
async def test_unstable_read_is_retried_then_quarantined_without_absence_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "unstable.md").write_text("moving")
    ingestion = Ingestion(root, failures=3)
    removals = Removals()
    service = _service(
        root,
        sources=(_source("previous.md", "old"),),
        ingestion=ingestion,
        removals=removals,
        attempts=2,
    )

    result = await service.run(_command(), _policy())

    assert result.state is MountedFilesystemReconciliationState.INCOMPLETE
    assert result.confirmed_scan_generation is None
    assert len(ingestion.commands) == 2
    assert [item.state for item in result.lifecycle_evidence] == [
        MountedFilesystemLifecycleState.QUARANTINED
    ]
    assert removals.calls == []


@pytest.mark.asyncio
async def test_changed_verification_snapshot_keeps_generation_incomplete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "moving.md").write_text("initial")
    removals = Removals()
    service = _service(
        root,
        sources=(_source("previous.md", "old"),),
        ingestion=MutatingIngestion(root),
        removals=removals,
    )

    result = await service.run(_command(), _policy())

    assert result.state is MountedFilesystemReconciliationState.INCOMPLETE
    assert result.confirmed_scan_generation is None
    assert removals.calls == []


@pytest.mark.asyncio
async def test_recovery_and_repeated_scan_create_one_logical_effect_per_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "guide.md").write_text("stable")
    ingestion = Ingestion(root)
    service = _service(root, ingestion=ingestion)
    detached = tmp_path / "detached"
    root.rename(detached)

    unavailable = await service.run(_command("recover"), _policy())
    detached.rename(root)
    recovered = await service.run(_command("recover"), _policy())
    replay = await service.run(_command("recover"), _policy())

    assert unavailable.state is MountedFilesystemReconciliationState.UNAVAILABLE
    assert recovered.state is MountedFilesystemReconciliationState.COMPLETE
    assert replay == recovered
    assert len(ingestion.logical_revisions) == 1
    assert len(ingestion.commands) == 1


@pytest.mark.asyncio
async def test_independent_unchanged_scan_generations_reuse_revision_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "guide.md").write_text("stable")
    ingestion = Ingestion(root)

    first = await _service(root, ingestion=ingestion).run(
        _command("generation-1"), _policy()
    )
    second = await _service(root, ingestion=ingestion).run(
        _command("generation-2"), _policy()
    )

    assert first.state is MountedFilesystemReconciliationState.COMPLETE
    assert second.state is MountedFilesystemReconciliationState.COMPLETE
    assert len(ingestion.logical_revisions) == 1
    assert ingestion.commands[0].idempotency_key == ingestion.commands[1].idempotency_key
    assert (
        ingestion.commands[0].expected_provider_revision
        == ingestion.commands[1].expected_provider_revision
    )


@pytest.mark.asyncio
async def test_replaced_file_is_a_new_scan_revision(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    path = root / "guide.md"
    path.write_text("before")
    ingestion = Ingestion(root)

    await _service(root, ingestion=ingestion).run(_command("before"), _policy())
    path.unlink()
    path.write_text("after")
    await _service(root, ingestion=ingestion).run(_command("after"), _policy())

    assert len(ingestion.logical_revisions) == 2
    assert (
        ingestion.commands[0].expected_provider_revision
        != ingestion.commands[1].expected_provider_revision
    )


@pytest.mark.asyncio
async def test_unapproved_removal_policy_is_denied_before_scan(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    service = _service(root)
    unapproved = _policy().model_copy(update={"approval_id": _id("unapproved")})

    with pytest.raises(ProviderReadDeniedError):
        await service.run(_command(), unapproved)


@pytest.mark.asyncio
async def test_independent_absence_generations_reuse_tombstone_key(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    source = _source("missing.md", "current")
    removals = Removals()

    await _service(root, sources=(source,), removals=removals).run(
        _command("absence-1"), _policy()
    )
    await _service(root, sources=(source,), removals=removals).run(
        _command("absence-2"), _policy()
    )

    assert len(removals.calls) == 2
    assert removals.calls[0][2] == removals.calls[1][2]


@pytest.mark.asyncio
async def test_reappeared_path_uses_new_occurrence_without_claiming_continuity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "returned.md").write_text("returned")
    prior = _source("returned.md", "deleted", tombstone=True)
    ingestion = Ingestion(root)
    service = _service(root, sources=(prior,), ingestion=ingestion)

    result = await service.run(_command(), _policy())

    evidence = result.lifecycle_evidence[0]
    assert evidence.state is MountedFilesystemLifecycleState.REAPPEARED
    assert evidence.identity_continuity_proven is False
    assert evidence.predecessor_source_object_id == prior.source_object_id
    assert evidence.source_object_id != prior.source_object_id
    assert ingestion.commands[0].external_identity_occurrence is not None
