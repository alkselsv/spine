from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.domain.sources.canonical_acceptance_selector import CanonicalAcceptanceAuthorityKind
from spine.infrastructure.persistence.postgresql_canonical_acceptance_source import (
    PostgreSQLTrustedCanonicalSourceContextReader,
)


class _Result:
    def __init__(self, *, rows=(), scalar=None) -> None:
        self._rows = rows
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._scalar


class _UnitOfWork:
    def __init__(self, results) -> None:
        self._results = iter(results)
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return None

    async def _execute(self, _statement, *_args):
        return next(self._results)

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_source_reader_returns_only_persisted_upload_authority() -> None:
    workspace_id = UUID("10000000-0000-0000-0000-000000000001")
    environment_id = UUID("20000000-0000-0000-0000-000000000001")
    source_object_id = UUID("30000000-0000-0000-0000-000000000001")
    source_revision_id = UUID("40000000-0000-0000-0000-000000000001")
    upload_id = UUID("50000000-0000-0000-0000-000000000001")
    provenance = SimpleNamespace(
        source_contract_id="original_upload",
        source_contract_version="v1",
        producer_kind="upload",
        provenance_id=UUID("60000000-0000-0000-0000-000000000001"),
        source_kind="document",
    )
    uow = _UnitOfWork(
        (
            _Result(rows=(provenance,)),
            _Result(scalar=upload_id),
            _Result(scalar=upload_id),
        )
    )
    captured_contexts = []
    reader = PostgreSQLTrustedCanonicalSourceContextReader(
        uow_factory=lambda _context: uow,
        context_factory=lambda operation, workspace, environment: captured_contexts.append(
            (operation, workspace, environment)
        ) or object(),
    )

    result = await reader.get_source_context(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
        source_revision_id=source_revision_id,
    )

    assert result is not None
    assert result.authority_kind is CanonicalAcceptanceAuthorityKind.UPLOAD
    assert result.upload_authority_reference == upload_id
    assert result.source_contract_id == "original_upload"
    assert captured_contexts == [
        ("canonical_acceptance_selector.resolve", workspace_id, environment_id)
    ]
    assert uow.rolled_back is True


@pytest.mark.asyncio
async def test_source_reader_denies_upload_without_exact_receipt_association() -> None:
    workspace_id = UUID("10000000-0000-0000-0000-000000000001")
    environment_id = UUID("20000000-0000-0000-0000-000000000001")
    provenance = SimpleNamespace(
        source_contract_id="original_upload",
        source_contract_version="v1",
        producer_kind="upload",
        provenance_id=UUID("60000000-0000-0000-0000-000000000001"),
        source_kind="document",
    )
    uow = _UnitOfWork((_Result(rows=(provenance,)), _Result(scalar=None)))
    reader = PostgreSQLTrustedCanonicalSourceContextReader(
        uow_factory=lambda _context: uow,
        context_factory=lambda *_args: object(),
    )

    result = await reader.get_source_context(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=UUID("30000000-0000-0000-0000-000000000001"),
        source_revision_id=UUID("40000000-0000-0000-0000-000000000001"),
    )

    assert result is None
    assert uow.rolled_back is True


@pytest.mark.asyncio
async def test_source_reader_requires_current_provider_version_and_approval() -> None:
    workspace_id = UUID("10000000-0000-0000-0000-000000000001")
    environment_id = UUID("20000000-0000-0000-0000-000000000001")
    source_object_id = UUID("30000000-0000-0000-0000-000000000001")
    revision_id = UUID("40000000-0000-0000-0000-000000000001")
    connection_id = UUID("70000000-0000-0000-0000-000000000001")
    connection_version_id = UUID("71000000-0000-0000-0000-000000000001")
    approval_id = UUID("72000000-0000-0000-0000-000000000001")
    scope_id = UUID("73000000-0000-0000-0000-000000000001")
    provenance = SimpleNamespace(
        source_contract_id="google_drive",
        source_contract_version="v1",
        producer_kind="connector",
        provenance_id=UUID("60000000-0000-0000-0000-000000000001"),
        source_kind="document",
        source_connection_id=connection_id,
        connection_id=connection_id,
        resource_scope_reference=scope_id,
        bitrix24_disk_membership=None,
    )
    connection = SimpleNamespace(
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        approval_id=approval_id,
        provider_kind="google",
        status="active",
        approved_resource_scope_references=[scope_id],
        authorization_generation=3,
        configuration_generation=4,
    )
    approval = SimpleNamespace(
        approval_id=approval_id,
        approved_resource_scope_references=[scope_id],
        authorization_generation=3,
        configuration_generation=4,
    )

    async def resolve(connection_row):
        uow = _UnitOfWork(
            (
                _Result(rows=(provenance,)),
                _Result(scalar=connection_row),
                _Result(scalar=approval),
            )
        )
        reader = PostgreSQLTrustedCanonicalSourceContextReader(
            uow_factory=lambda _context: uow,
            context_factory=lambda *_args: object(),
        )
        return await reader.get_source_context(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_revision_id=revision_id,
        )

    current = await resolve(connection)
    revoked = await resolve(SimpleNamespace(
        **{**connection.__dict__, "status": "revoked"}
    ))
    assert current is not None
    assert current.provider_approval_id == approval_id
    assert current.provider_connection_version_id == connection_version_id
    assert revoked is None


@pytest.mark.asyncio
async def test_source_reader_requires_current_bitrix_root_and_content_policy_heads() -> None:
    from spine.domain.connections.bitrix24_disk_scope import (
        Bitrix24DiskMembershipEvidence,
        Bitrix24DiskStorageClass,
    )
    from spine.domain.connections.provider import OpaqueProviderReference

    workspace_id = UUID("10000000-0000-0000-0000-000000000001")
    environment_id = UUID("20000000-0000-0000-0000-000000000001")
    source_object_id = UUID("30000000-0000-0000-0000-000000000001")
    revision_id = UUID("40000000-0000-0000-0000-000000000001")
    connection_id = UUID("70000000-0000-0000-0000-000000000001")
    connection_version_id = UUID("71000000-0000-0000-0000-000000000001")
    approval_id = UUID("72000000-0000-0000-0000-000000000001")
    scope_id = UUID("73000000-0000-0000-0000-000000000001")
    root_id = UUID("74000000-0000-0000-0000-000000000001")
    content_id = UUID("75000000-0000-0000-0000-000000000001")
    root_digest, content_digest = "a" * 64, "b" * 64
    evidence = Bitrix24DiskMembershipEvidence(
        workspace_id=workspace_id,
        environment_id=environment_id,
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        resource_scope_reference=OpaqueProviderReference(value=scope_id),
        root_policy_id=root_id,
        root_policy_version="root-v1",
        root_policy_digest=root_digest,
        content_policy_id=content_id,
        content_policy_version="content-v1",
        content_policy_digest=content_digest,
        authorization_generation=3,
        configuration_generation=4,
        grant_binding_id=UUID("76000000-0000-0000-0000-000000000001"),
        grant_token_epoch=1,
        grant_security_fence=0,
        storage_id="disk",
        storage_class=Bitrix24DiskStorageClass.COMMON,
        storage_root_object_id="root",
        approved_root_folder_id="folder",
        object_id="file",
        parent_folder_ids=("folder",),
    )
    provenance = SimpleNamespace(
        source_contract_id="bitrix24_disk",
        source_contract_version="v2",
        producer_kind="connector",
        provenance_id=UUID("60000000-0000-0000-0000-000000000001"),
        source_kind="document",
        source_connection_id=connection_id,
        connection_id=connection_id,
        resource_scope_reference=scope_id,
        bitrix24_disk_membership=evidence,
    )
    connection = SimpleNamespace(
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        approval_id=approval_id,
        provider_kind="bitrix24",
        status="active",
        approved_resource_scope_references=[scope_id],
        authorization_generation=3,
        configuration_generation=4,
    )
    approval = SimpleNamespace(
        approval_id=approval_id,
        approved_resource_scope_references=[scope_id],
        authorization_generation=3,
        configuration_generation=4,
    )
    root_head = SimpleNamespace(current_version="root-v1", current_digest=root_digest)
    content_head = SimpleNamespace(current_version="content-v1", current_digest=content_digest)
    root = SimpleNamespace(
        policy_id=root_id,
        version="root-v1",
        digest=root_digest,
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        resource_scope_reference=scope_id,
        content_policy_id=content_id,
        content_policy_version="content-v1",
        content_policy_digest=content_digest,
    )
    content = SimpleNamespace(
        policy_id=content_id,
        version="content-v1",
        digest=content_digest,
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        resource_scope_reference=scope_id,
    )
    uow = _UnitOfWork(
        (
            _Result(rows=(provenance,)),
            _Result(scalar=connection),
            _Result(scalar=approval),
            _Result(scalar=root_head),
            _Result(scalar=content_head),
            _Result(scalar=root),
            _Result(scalar=content),
        )
    )
    reader = PostgreSQLTrustedCanonicalSourceContextReader(
        uow_factory=lambda _context: uow,
        context_factory=lambda *_args: object(),
    )

    result = await reader.get_source_context(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
        source_revision_id=revision_id,
    )

    assert result is not None
    assert result.root_policy_digest == root_digest
    assert result.content_policy_digest == content_digest
