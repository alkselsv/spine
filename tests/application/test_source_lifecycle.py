from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.source_lifecycle import (
    CanonicalGovernedReappearanceAuthority,
    GovernedReappearanceAuthorization,
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
    SourceLifecycleValidationError,
)
from spine.domain.sources import (
    CurrentSourceHead,
    IdentityMode,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
    canonical_revision_metadata_digest,
)
from spine.domain.sources.canonicalization import content_revision, tombstone_revision
from spine.infrastructure.object_storage.contracts import ObjectReference


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"source-lifecycle-test:{value}")


def _context():
    return SimpleNamespace(operation=PersistenceOperation("canonical_acceptance"))


def _source(
    *,
    workspace_id: UUID = WORKSPACE,
    environment_id: UUID = ENVIRONMENT,
    identity: str = "default",
) -> SourceObject:
    return SourceObject(
        source_object_id=_id(f"source:{workspace_id}:{environment_id}:{identity}"),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity=f"lifecycle-source-{identity}",
        created_at=NOW,
    )


def _provenance(source: SourceObject, revision_id: UUID, event: str) -> SourceRevisionProvenance:
    return SourceRevisionProvenance(
        provenance_id=_id(f"provenance:{source.source_object_id}:{event}"),
        source_object_id=source.source_object_id,
        revision_id=revision_id,
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        producer_kind="connector",
        producer_reference="lifecycle-connector",
        event_identity=event,
        event_digest="d" * 64,
        observer_service="lifecycle-observer",
        received_at=NOW,
        observed_at=NOW,
    )


def _tombstone(source: SourceObject, revision_id: UUID | None = None):
    revision_id = revision_id or _id(f"tombstone:{source.source_object_id}")
    return tombstone_revision(
        revision_id=revision_id,
        source_object_id=source.source_object_id,
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        kind=RevisionKind.TOMBSTONE,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        deletion_reason="source_deleted",
        deletion_provenance="provider-delete-event",
        observed_at=NOW,
    )


def _content(source: SourceObject, predecessor: UUID | None = None):
    metadata = RevisionMetadata(embedded_title="Reappeared")
    digest = "a" * 64
    return content_revision(
        revision_id=_id(f"content:{source.source_object_id}:{predecessor}"),
        source_object_id=source.source_object_id,
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=metadata,
        revision_metadata_digest=canonical_revision_metadata_digest(metadata),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=_id(f"object:{source.source_object_id}:{predecessor}"),
            storage_generation=_id(f"generation:{source.source_object_id}:{predecessor}"),
            digest_algorithm="sha256",
            digest_hex=digest,
            byte_length=4,
        ),
        original_sha256=digest,
        byte_length=4,
        media_type="text/plain",
        reappearance_after_tombstone_revision_id=predecessor,
        observed_at=NOW,
    )


def _receipt(source: SourceObject, revision, provenance):
    assert revision.original_reference is not None
    return SourceRevisionReceiptAssociation(
        association_id=_id(f"association:{revision.revision_id}"),
        source_object_id=source.source_object_id,
        revision_id=revision.revision_id,
        provenance_id=provenance.provenance_id,
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=_id(f"receipt:{revision.revision_id}"),
        original_command_result_id=_id(f"command-result:{revision.revision_id}"),
        original_command_result_schema_version=1,
        original_upload_id=_id(f"upload:{revision.revision_id}"),
        original_reference=revision.original_reference,
        original_sha256=revision.original_sha256,
        byte_length=revision.byte_length,
        finalized_at=NOW,
        associated_at=NOW,
    )


def _canonical(source, revision, provenance, *, key: str):
    return CanonicalAcceptanceCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        expected_current_revision_id=(
            revision.reappearance_after_tombstone_revision_id
            if revision.kind is RevisionKind.CONTENT
            else _id(f"expected-current:{revision.revision_id}")
        ),
        idempotency_key=IdempotencyKey(key),
        canonical_acceptance_id=_id(f"acceptance:{key}"),
        admission_decision_id=_id(f"admission:{key}") if revision.kind is RevisionKind.CONTENT else None,
        access_policy_version_id=_id(f"policy:{key}") if revision.kind is RevisionKind.CONTENT else None,
        audit_event_id=_id(f"audit:{key}"),
        outbox_event_id=_id(f"outbox:{key}"),
        accepted_at=NOW,
    )


@dataclass
class _Sources:
    source: SourceObject
    revisions: dict[UUID, object]
    recorded: list[object]

    async def resolve_source(self, source_object_id: UUID):
        if source_object_id != self.source.source_object_id:
            return None
        return self.source

    async def resolve_revision(self, revision_id: UUID):
        return self.revisions.get(revision_id)

    async def record_observation(self, command):
        replay = any(item.idempotency_key == command.idempotency_key for item in self.recorded)
        self.recorded.append(command)
        return SimpleNamespace(
            source=command.source,
            revision=command.revision,
            provenance=command.provenance,
            replay=replay,
        )


class _Canonical:
    def __init__(self, result=None, error: Exception | None = None, current=None):
        self.commands = []
        self.result = result or SimpleNamespace(advanced=True, replay=False)
        self.error = error
        self.current = current

    async def resolve_current(self, source_object_id: UUID):
        if self.current is None or self.current.source_object_id != source_object_id:
            return None
        return self.current

    async def accept(self, command):
        replay = any(item.idempotency_key == command.idempotency_key for item in self.commands)
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(advanced=not replay, replay=replay)


class _Uow:
    def __init__(self, sources, canonical):
        self.sources = sources
        self.canonical = canonical
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            await self.rollback()

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _command(
    source,
    revision,
    provenance,
    *,
    key: str,
    kind: SourceLifecycleKind,
    receipt_association=None,
):
    canonical = _canonical(source, revision, provenance, key=f"canonical-{key}")
    authorization = None
    if kind is SourceLifecycleKind.REAPPEARANCE:
        authorization = GovernedReappearanceAuthorization(
            authorization_id=_id(f"reappearance-authorization:{key}"),
            workspace_id=source.workspace_id,
            environment_id=source.environment_id,
            source_object_id=source.source_object_id,
            tombstone_revision_id=revision.reappearance_after_tombstone_revision_id,
            content_revision_id=revision.revision_id,
            identity_continuity_evidence_id=_id(f"identity-evidence:{key}"),
            identity_continuity_evidence_digest="e" * 64,
            pre_tombstone_access_policy_version_id=_id(
                f"pre-tombstone-policy:{key}"
            ),
            fresh_admission_decision_id=canonical.admission_decision_id,
            fresh_access_policy_version_id=canonical.access_policy_version_id,
            authority_generation=1,
        )
    return SourceLifecycleCommand(
        kind=kind,
        source=source,
        revision=revision,
        provenance=provenance,
        observation_idempotency_key=IdempotencyKey(f"observation-{key}"),
        canonical_acceptance=canonical,
        receipt_association=receipt_association,
        reappearance_authorization=authorization,
    )


class _ReappearanceAuthority:
    def __init__(self, current: bool = True) -> None:
        self.current = current
        self.authorizations = []

    async def is_current(self, authorization, *, canonical, current):
        del canonical, current
        self.authorizations.append(authorization)
        return self.current


def _current_tombstone(source: SourceObject, revision_id: UUID) -> CurrentSourceHead:
    return CurrentSourceHead(
        source_object_id=source.source_object_id,
        workspace_id=source.workspace_id,
        environment_id=source.environment_id,
        current_revision_id=revision_id,
        current_revision_kind=RevisionKind.TOMBSTONE,
        acceptance_id=_id(f"tombstone-acceptance:{revision_id}"),
        accepted_generation=1,
        cas_version=1,
        changed_at=NOW,
    )


@pytest.mark.asyncio
async def test_deletion_records_observation_and_delegates_canonical_transition_atomically():
    source = _source()
    revision = _tombstone(source)
    provenance = _provenance(source, revision.revision_id, "delete-1")
    sources = _Sources(source, {revision.revision_id: revision}, [])
    canonical = _Canonical()
    uows = []
    coordinator = SourceLifecycleCoordinator(
        lambda _context: uows.append(_Uow(sources, canonical)) or uows[-1],
        lambda _command: _context(),
    )

    result = await coordinator.apply(
        _command(source, revision, provenance, key="delete-1", kind=SourceLifecycleKind.DELETION)
    )

    assert result.acceptance.advanced is True
    assert len(sources.recorded) == 1
    assert canonical.commands[0].revision == revision
    assert uows[0].committed is True
    assert uows[0].rolled_back is False


@pytest.mark.asyncio
async def test_lifecycle_observation_uses_the_atomic_uow_operation_digest():
    source = _source()
    revision = _tombstone(source)
    provenance = _provenance(source, revision.revision_id, "delete-operation")
    sources = _Sources(source, {revision.revision_id: revision}, [])
    canonical = _Canonical()
    context = SimpleNamespace(operation=PersistenceOperation("canonical_acceptance"))
    uow = _Uow(sources, canonical)

    await SourceLifecycleCoordinator(
        lambda _context: uow,
        lambda _command: context,
    ).apply(
        _command(
            source,
            revision,
            provenance,
            key="delete-operation",
            kind=SourceLifecycleKind.DELETION,
        )
    )

    assert sources.recorded[0].digest.operation == context.operation
    assert canonical.commands[0].digest.operation == context.operation


@pytest.mark.asyncio
async def test_replacement_records_new_content_revision_without_reappearance_semantics():
    source = _source()
    previous = _content(source)
    revision = _content(source)
    revision = revision.model_copy(
        update={
            "revision_id": _id("replacement-revision"),
            "reappearance_after_tombstone_revision_id": None,
        }
    )
    provenance = _provenance(source, revision.revision_id, "replace-1")
    receipt_association = _receipt(source, revision, provenance)
    canonical_command = _canonical(source, revision, provenance, key="canonical-replace-1")
    canonical_command = CanonicalAcceptanceCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        expected_current_revision_id=previous.revision_id,
        idempotency_key=canonical_command.idempotency_key,
        canonical_acceptance_id=canonical_command.canonical_acceptance_id,
        admission_decision_id=canonical_command.admission_decision_id,
        access_policy_version_id=canonical_command.access_policy_version_id,
        audit_event_id=canonical_command.audit_event_id,
        outbox_event_id=canonical_command.outbox_event_id,
        accepted_at=NOW,
    )
    sources = _Sources(
        source,
        {previous.revision_id: previous, revision.revision_id: revision},
        [],
    )
    canonical = _Canonical()
    uow = _Uow(sources, canonical)

    result = await SourceLifecycleCoordinator(
        lambda _context: uow, lambda _command: _context()
    ).apply(
        SourceLifecycleCommand(
            kind=SourceLifecycleKind.REPLACEMENT,
            source=source,
            revision=revision,
            provenance=provenance,
            observation_idempotency_key=IdempotencyKey("observation-replace-1"),
            canonical_acceptance=canonical_command,
            receipt_association=receipt_association,
        )
    )

    assert result.acceptance.advanced is True
    assert sources.recorded[0].revision.revision_id == revision.revision_id
    assert sources.recorded[0].receipt_association == receipt_association
    assert canonical.commands[0].expected_current_revision_id == previous.revision_id
    assert revision.reappearance_after_tombstone_revision_id is None


@pytest.mark.asyncio
async def test_reappearance_requires_existing_tombstone_and_fresh_canonical_evidence():
    source = _source()
    tombstone = _tombstone(source)
    revision = _content(source, tombstone.revision_id)
    provenance = _provenance(source, revision.revision_id, "reappear-1")
    receipt_association = _receipt(source, revision, provenance)
    sources = _Sources(
        source,
        {tombstone.revision_id: tombstone, revision.revision_id: revision},
        [],
    )
    canonical = _Canonical(current=_current_tombstone(source, tombstone.revision_id))
    uow = _Uow(sources, canonical)
    authority = _ReappearanceAuthority()

    result = await SourceLifecycleCoordinator(
        lambda _context: uow,
        lambda _command: _context(),
        reappearance_authority=authority,
    ).apply(
        _command(
            source,
            revision,
            provenance,
            key="reappear-1",
            kind=SourceLifecycleKind.REAPPEARANCE,
            receipt_association=receipt_association,
        )
    )

    assert result.acceptance.advanced is True
    assert canonical.commands[0].admission_decision_id is not None
    assert canonical.commands[0].access_policy_version_id is not None
    assert len(authority.authorizations) == 1


@pytest.mark.asyncio
async def test_reappearance_without_current_governed_authority_fails_closed():
    source = _source()
    tombstone = _tombstone(source)
    revision = _content(source, tombstone.revision_id)
    provenance = _provenance(source, revision.revision_id, "missing-authority")
    sources = _Sources(
        source,
        {tombstone.revision_id: tombstone, revision.revision_id: revision},
        [],
    )
    canonical = _Canonical(current=_current_tombstone(source, tombstone.revision_id))

    with pytest.raises(SourceLifecycleValidationError, match="authority"):
        await SourceLifecycleCoordinator(
            lambda _context: _Uow(sources, canonical),
            lambda _command: _context(),
        ).apply(
            _command(
                source,
                revision,
                provenance,
                key="missing-authority",
                kind=SourceLifecycleKind.REAPPEARANCE,
                receipt_association=_receipt(source, revision, provenance),
            )
        )

    assert sources.recorded == []
    assert canonical.commands == []


@pytest.mark.asyncio
async def test_canonical_reappearance_authority_binds_exact_tombstone_and_prior_policy():
    source = _source()
    tombstone = _tombstone(source)
    revision = _content(source, tombstone.revision_id)
    provenance = _provenance(source, revision.revision_id, "canonical-authority")
    command = _command(
        source,
        revision,
        provenance,
        key="canonical-authority",
        kind=SourceLifecycleKind.REAPPEARANCE,
        receipt_association=_receipt(source, revision, provenance),
    )
    authorization = command.reappearance_authorization
    assert authorization is not None
    current = _current_tombstone(source, tombstone.revision_id)
    predecessor_acceptance_id = _id("pre-tombstone-acceptance")
    reads = {
        current.acceptance_id: SimpleNamespace(
            is_current=True,
            current=current,
            acceptance=SimpleNamespace(
                source_revision_id=tombstone.revision_id,
                predecessor_acceptance_id=predecessor_acceptance_id,
            ),
        ),
        predecessor_acceptance_id: SimpleNamespace(
            acceptance=SimpleNamespace(
                workspace_id=source.workspace_id,
                environment_id=source.environment_id,
                source_object_id=source.source_object_id,
                access_policy_version_id=(
                    authorization.pre_tombstone_access_policy_version_id
                ),
            )
        ),
    }

    class Canonical:
        async def resolve_acceptance_read(self, acceptance_id):
            return reads.get(acceptance_id)

    class Authorizations:
        current = authorization

        async def resolve_current(self, **scope):
            assert scope == {
                "workspace_id": source.workspace_id,
                "environment_id": source.environment_id,
                "source_object_id": source.source_object_id,
            }
            return self.current

    authorizations = Authorizations()
    authority = CanonicalGovernedReappearanceAuthority(authorizations)
    assert await authority.is_current(
        authorization, canonical=Canonical(), current=current
    )

    content_acceptance_id = _id("reappearance-content-acceptance")
    content_current = current.model_copy(
        update={
            "current_revision_id": authorization.content_revision_id,
            "current_revision_kind": RevisionKind.CONTENT,
            "acceptance_id": content_acceptance_id,
        }
    )
    reads[content_acceptance_id] = SimpleNamespace(
        is_current=True,
        current=content_current,
        acceptance=SimpleNamespace(
            source_revision_id=authorization.content_revision_id,
            admission_decision_id=authorization.fresh_admission_decision_id,
            access_policy_version_id=authorization.fresh_access_policy_version_id,
            predecessor_acceptance_id=current.acceptance_id,
        ),
    )
    assert await authority.is_current(
        authorization, canonical=Canonical(), current=content_current
    )

    reads[content_acceptance_id].acceptance.access_policy_version_id = (
        authorization.pre_tombstone_access_policy_version_id
    )
    assert not await authority.is_current(
        authorization, canonical=Canonical(), current=content_current
    )
    reads[content_acceptance_id].acceptance.access_policy_version_id = (
        authorization.fresh_access_policy_version_id
    )
    reads[content_acceptance_id].acceptance.admission_decision_id = _id(
        "unrelated-admission"
    )
    assert not await authority.is_current(
        authorization, canonical=Canonical(), current=content_current
    )
    reads[content_acceptance_id].acceptance.admission_decision_id = (
        authorization.fresh_admission_decision_id
    )

    reads[predecessor_acceptance_id].acceptance.access_policy_version_id = (
        authorization.fresh_access_policy_version_id
    )
    assert not await authority.is_current(
        authorization, canonical=Canonical(), current=current
    )

    reads[predecessor_acceptance_id].acceptance.access_policy_version_id = (
        authorization.pre_tombstone_access_policy_version_id
    )
    authorizations.current = None
    assert not await authority.is_current(
        authorization, canonical=Canonical(), current=current
    )


@pytest.mark.asyncio
async def test_reappearance_rejects_superseded_tombstone_before_recording() -> None:
    source = _source()
    tombstone = _tombstone(source)
    newer_tombstone = _tombstone(source, _id("newer-tombstone"))
    revision = _content(source, tombstone.revision_id)
    provenance = _provenance(source, revision.revision_id, "stale-reappearance")
    sources = _Sources(
        source,
        {
            tombstone.revision_id: tombstone,
            newer_tombstone.revision_id: newer_tombstone,
            revision.revision_id: revision,
        },
        [],
    )
    canonical = _Canonical(
        current=_current_tombstone(source, newer_tombstone.revision_id)
    )
    uow = _Uow(sources, canonical)

    with pytest.raises(SourceLifecycleValidationError):
        await SourceLifecycleCoordinator(
            lambda _context: uow,
            lambda _command: _context(),
            reappearance_authority=_ReappearanceAuthority(),
        ).apply(
            _command(
                source,
                revision,
                provenance,
                key="stale-reappearance",
                kind=SourceLifecycleKind.REAPPEARANCE,
                receipt_association=_receipt(source, revision, provenance),
            )
        )

    assert sources.recorded == []
    assert canonical.commands == []


def test_reappearance_cannot_transfer_a_pre_tombstone_access_policy() -> None:
    source = _source()
    tombstone = _tombstone(source)
    revision = _content(source, tombstone.revision_id)
    provenance = _provenance(source, revision.revision_id, "policy-transfer")
    command = _command(
        source,
        revision,
        provenance,
        key="policy-transfer",
        kind=SourceLifecycleKind.REAPPEARANCE,
        receipt_association=_receipt(source, revision, provenance),
    )
    stale = command.reappearance_authorization.model_copy(
        update={
            "pre_tombstone_access_policy_version_id": (
                command.canonical_acceptance.access_policy_version_id
            )
        }
    )

    with pytest.raises(SourceLifecycleValidationError):
        SourceLifecycleCommand(
            kind=command.kind,
            source=command.source,
            revision=command.revision,
            provenance=command.provenance,
            observation_idempotency_key=command.observation_idempotency_key,
            canonical_acceptance=command.canonical_acceptance,
            receipt_association=command.receipt_association,
            reappearance_authorization=stale,
        )


@pytest.mark.asyncio
async def test_repeating_same_lifecycle_command_returns_delegated_replay_without_new_effect():
    source = _source()
    revision = _tombstone(source)
    provenance = _provenance(source, revision.revision_id, "delete-replay")
    sources = _Sources(source, {revision.revision_id: revision}, [])
    canonical = _Canonical()
    uows = []
    coordinator = SourceLifecycleCoordinator(
        lambda _context: uows.append(_Uow(sources, canonical)) or uows[-1],
        lambda _command: _context(),
    )
    command = _command(source, revision, provenance, key="delete-replay", kind=SourceLifecycleKind.DELETION)

    first = await coordinator.apply(command)
    second = await coordinator.apply(command)

    assert first.replay is False
    assert second.replay is True
    assert len(canonical.commands) == 2
    assert len(sources.recorded) == 2
    assert all(uow.committed for uow in uows)


@pytest.mark.asyncio
async def test_missing_source_fails_closed_without_creating_or_recording_deletion():
    source = _source()
    revision = _tombstone(source)
    provenance = _provenance(source, revision.revision_id, "delete-missing")
    sources = _Sources(_source(identity="other"), {revision.revision_id: revision}, [])
    canonical = _Canonical()
    uow = _Uow(sources, canonical)

    with pytest.raises(SourceLifecycleValidationError):
        await SourceLifecycleCoordinator(
            lambda _context: uow, lambda _command: _context()
        ).apply(
            _command(source, revision, provenance, key="delete-missing", kind=SourceLifecycleKind.DELETION)
        )

    assert sources.recorded == []
    assert canonical.commands == []
    assert uow.committed is False


@pytest.mark.asyncio
async def test_cross_workspace_source_identity_fails_closed_without_canonical_call():
    source = _source()
    foreign_source = _source(workspace_id=UUID("10000000-0000-4000-8000-000000000099"))
    revision = _tombstone(source)
    provenance = _provenance(source, revision.revision_id, "delete-cross-workspace")
    sources = _Sources(foreign_source, {revision.revision_id: revision}, [])
    canonical = _Canonical()
    uow = _Uow(sources, canonical)

    with pytest.raises(SourceLifecycleValidationError):
        await SourceLifecycleCoordinator(
            lambda _context: uow, lambda _command: _context()
        ).apply(
            _command(
                source,
                revision,
                provenance,
                key="delete-cross-workspace",
                kind=SourceLifecycleKind.DELETION,
            )
        )

    assert canonical.commands == []
    assert sources.recorded == []


@pytest.mark.asyncio
async def test_reappearance_with_unknown_predecessor_fails_without_canonical_call():
    source = _source()
    revision = _content(source, _id("unknown-predecessor"))
    provenance = _provenance(source, revision.revision_id, "reappear-unknown")
    sources = _Sources(source, {revision.revision_id: revision}, [])
    canonical = _Canonical()
    uow = _Uow(sources, canonical)
    receipt_association = _receipt(source, revision, provenance)

    with pytest.raises(SourceLifecycleValidationError):
        await SourceLifecycleCoordinator(
            lambda _context: uow, lambda _command: _context()
        ).apply(
            _command(
                source,
                revision,
                provenance,
                key="reappear-unknown",
                kind=SourceLifecycleKind.REAPPEARANCE,
                receipt_association=receipt_association,
            )
        )

    assert canonical.commands == []
    assert sources.recorded == []


@pytest.mark.asyncio
async def test_canonical_failure_rolls_back_observation_and_never_commits_partial_lifecycle():
    source = _source()
    revision = _tombstone(source)
    provenance = _provenance(source, revision.revision_id, "delete-fails")
    sources = _Sources(source, {revision.revision_id: revision}, [])
    uow = _Uow(sources, _Canonical(error=RuntimeError("canonical unavailable")))

    with pytest.raises(RuntimeError, match="canonical unavailable"):
        await SourceLifecycleCoordinator(
            lambda _context: uow, lambda _command: _context()
        ).apply(
            _command(source, revision, provenance, key="delete-fails", kind=SourceLifecycleKind.DELETION)
        )

    assert uow.committed is False
    assert uow.rolled_back is True
