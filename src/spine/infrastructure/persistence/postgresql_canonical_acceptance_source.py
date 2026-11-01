"""Trusted source and current-authority reader for acceptance selectors."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select

from spine.domain.connections.bitrix24_disk_scope import Bitrix24DiskMembershipEvidence
from spine.domain.sources.canonical_acceptance_selector import (
    CanonicalAcceptanceAuthorityKind,
    TrustedSourceSelectorContext,
)
from spine.infrastructure.connections.postgresql_provider_management import (
    provider_connection_approvals,
    provider_connection_versions,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    bitrix24_disk_content_policy_versions,
    bitrix24_disk_root_policy_versions,
    current_bitrix24_disk_content_policies,
    current_bitrix24_disk_root_policies,
    original_upload_records,
    source_objects,
    source_revision_provenance,
    source_revision_receipt_associations,
    source_revisions,
)


class PostgreSQLTrustedCanonicalSourceContextReader:
    """Resolve exact source lineage and current provider authority from PostgreSQL."""

    def __init__(self, *, uow_factory: Callable[..., object], context_factory: Callable[[str, UUID, UUID], object]) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def get_source_context(
        self, *, workspace_id: UUID, environment_id: UUID,
        source_object_id: UUID, source_revision_id: UUID,
    ) -> TrustedSourceSelectorContext | None:
        context = self._context_factory("canonical_acceptance_selector.resolve", workspace_id, environment_id)
        async with self._uow_factory(context) as uow:
            rows = (await uow._execute(  # type: ignore[attr-defined]
                select(
                    source_objects.c.source_kind,
                    source_objects.c.connection_id.label("source_connection_id"),
                    source_revisions.c.kind.label("revision_kind"),
                    source_revision_provenance,
                ).select_from(
                    source_objects.join(
                        source_revisions,
                        (source_objects.c.source_object_id == source_revisions.c.source_object_id)
                        & (source_objects.c.workspace_id == source_revisions.c.workspace_id)
                        & (source_objects.c.environment_id == source_revisions.c.environment_id),
                    ).join(
                        source_revision_provenance,
                        (source_revisions.c.revision_id == source_revision_provenance.c.revision_id)
                        & (source_revisions.c.source_object_id == source_revision_provenance.c.source_object_id)
                        & (source_revisions.c.workspace_id == source_revision_provenance.c.workspace_id)
                        & (source_revisions.c.environment_id == source_revision_provenance.c.environment_id),
                    )
                ).where(
                    source_objects.c.workspace_id == workspace_id,
                    source_objects.c.environment_id == environment_id,
                    source_objects.c.source_object_id == source_object_id,
                    source_revisions.c.revision_id == source_revision_id,
                    source_revisions.c.kind == "content",
                )
            )).mappings().all()
            candidates: list[TrustedSourceSelectorContext] = []
            for row in rows:
                if row.source_contract_id is None or row.source_contract_version is None:
                    continue
                if row.producer_kind == "upload":
                    candidate = await self._upload_context(
                        uow, workspace_id, environment_id, source_object_id,
                        source_revision_id, row,
                    )
                else:
                    candidate = await self._provider_context(
                        uow, workspace_id, environment_id, source_object_id, row,
                    )
                if candidate is not None:
                    candidates.append(candidate)
            await uow.rollback()  # type: ignore[attr-defined]
            unique_contexts = {item.model_dump_json() for item in candidates}
            return candidates[0] if len(unique_contexts) == 1 else None

    async def _upload_context(self, uow: object, workspace_id: UUID, environment_id: UUID,
                              source_object_id: UUID, source_revision_id: UUID,
                              provenance: object) -> TrustedSourceSelectorContext | None:
        association = (await uow._execute(  # type: ignore[attr-defined]
            select(source_revision_receipt_associations.c.original_upload_id).where(
                source_revision_receipt_associations.c.workspace_id == workspace_id,
                source_revision_receipt_associations.c.environment_id == environment_id,
                source_revision_receipt_associations.c.source_object_id == source_object_id,
                source_revision_receipt_associations.c.revision_id == source_revision_id,
                source_revision_receipt_associations.c.provenance_id == provenance.provenance_id,
            )
        )).scalar_one_or_none()
        if association is None:
            return None
        upload_exists = (await uow._execute(  # type: ignore[attr-defined]
            select(original_upload_records.c.upload_id).where(
                original_upload_records.c.workspace_id == workspace_id,
                original_upload_records.c.environment_id == environment_id,
                original_upload_records.c.upload_id == association,
            )
        )).scalar_one_or_none()
        if upload_exists is None:
            return None
        return TrustedSourceSelectorContext(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_kind=provenance.source_kind,
            source_contract_id=provenance.source_contract_id,
            source_contract_version=provenance.source_contract_version,
            authority_kind=CanonicalAcceptanceAuthorityKind.UPLOAD,
            approved_scope_reference=association,
            upload_authority_reference=association,
        )

    async def _provider_context(self, uow: object, workspace_id: UUID, environment_id: UUID,
                                source_object_id: UUID, provenance: object
                                ) -> TrustedSourceSelectorContext | None:
        connection_id = provenance.source_connection_id
        scope = provenance.resource_scope_reference
        if connection_id is None or scope is None or provenance.connection_id != connection_id:
            return None
        connection = (await uow._execute(  # type: ignore[attr-defined]
            select(provider_connection_versions)
            .where(
                provider_connection_versions.c.workspace_id == workspace_id,
                provider_connection_versions.c.environment_id == environment_id,
                provider_connection_versions.c.connection_id == connection_id,
            )
            .order_by(provider_connection_versions.c.version.desc())
            .limit(1)
        )).mappings().one_or_none()
        if connection is None or connection.status != "active" or scope not in connection.approved_resource_scope_references:
            return None
        approval = (await uow._execute(  # type: ignore[attr-defined]
            select(provider_connection_approvals).where(
                provider_connection_approvals.c.workspace_id == workspace_id,
                provider_connection_approvals.c.environment_id == environment_id,
                provider_connection_approvals.c.approval_id == connection.approval_id,
                provider_connection_approvals.c.provider_kind == connection.provider_kind,
                provider_connection_approvals.c.status == "active",
            )
        )).mappings().one_or_none()
        if (approval is None
                or scope not in approval.approved_resource_scope_references
                or approval.authorization_generation != connection.authorization_generation
                or approval.configuration_generation != connection.configuration_generation):
            return None
        root = content = None
        if connection.provider_kind == "bitrix24":
            policies = await self._current_bitrix_policy(
                uow, workspace_id, environment_id, connection, scope,
                provenance.bitrix24_disk_membership,
            )
            if policies is None:
                return None
            root, content = policies
        return TrustedSourceSelectorContext(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_kind=provenance.source_kind,
            source_contract_id=provenance.source_contract_id,
            source_contract_version=provenance.source_contract_version,
            authority_kind=CanonicalAcceptanceAuthorityKind.PROVIDER_CONNECTION,
            approved_scope_reference=scope,
            provider_kind=connection.provider_kind,
            provider_connection_id=connection_id,
            provider_connection_version_id=connection.connection_version_id,
            provider_approval_id=approval.approval_id,
            resource_scope_reference=scope,
            root_policy_id=None if root is None else root.policy_id,
            root_policy_version=None if root is None else root.version,
            root_policy_digest=None if root is None else root.digest,
            content_policy_id=None if content is None else content.policy_id,
            content_policy_version=None if content is None else content.version,
            content_policy_digest=None if content is None else content.digest,
        )

    async def _current_bitrix_policy(self, uow: object, workspace_id: UUID, environment_id: UUID,
                                    connection: object, scope: UUID,
                                    evidence_record: object
                                    ) -> tuple[object, object] | None:
        if evidence_record is None:
            return None
        try:
            evidence = Bitrix24DiskMembershipEvidence.model_validate(evidence_record)
        except Exception:
            return None
        if (evidence.connection_id != connection.connection_id
                or evidence.connection_version_id != connection.connection_version_id
                or evidence.resource_scope_reference.value != scope):
            return None
        root_head = (await uow._execute(  # type: ignore[attr-defined]
            select(current_bitrix24_disk_root_policies).where(
                current_bitrix24_disk_root_policies.c.workspace_id == workspace_id,
                current_bitrix24_disk_root_policies.c.environment_id == environment_id,
                current_bitrix24_disk_root_policies.c.policy_id == evidence.root_policy_id,
            )
        )).mappings().one_or_none()
        content_head = (await uow._execute(  # type: ignore[attr-defined]
            select(current_bitrix24_disk_content_policies).where(
                current_bitrix24_disk_content_policies.c.workspace_id == workspace_id,
                current_bitrix24_disk_content_policies.c.environment_id == environment_id,
                current_bitrix24_disk_content_policies.c.policy_id == evidence.content_policy_id,
            )
        )).mappings().one_or_none()
        if root_head is None or content_head is None:
            return None
        if (root_head.current_version, root_head.current_digest) != (evidence.root_policy_version, evidence.root_policy_digest):
            return None
        if (content_head.current_version, content_head.current_digest) != (evidence.content_policy_version, evidence.content_policy_digest):
            return None
        root = (await uow._execute(  # type: ignore[attr-defined]
            select(bitrix24_disk_root_policy_versions).where(
                bitrix24_disk_root_policy_versions.c.workspace_id == workspace_id,
                bitrix24_disk_root_policy_versions.c.environment_id == environment_id,
                bitrix24_disk_root_policy_versions.c.policy_id == evidence.root_policy_id,
                bitrix24_disk_root_policy_versions.c.version == evidence.root_policy_version,
                bitrix24_disk_root_policy_versions.c.digest == evidence.root_policy_digest,
            )
        )).mappings().one_or_none()
        content = (await uow._execute(  # type: ignore[attr-defined]
            select(bitrix24_disk_content_policy_versions).where(
                bitrix24_disk_content_policy_versions.c.workspace_id == workspace_id,
                bitrix24_disk_content_policy_versions.c.environment_id == environment_id,
                bitrix24_disk_content_policy_versions.c.policy_id == evidence.content_policy_id,
                bitrix24_disk_content_policy_versions.c.version == evidence.content_policy_version,
                bitrix24_disk_content_policy_versions.c.digest == evidence.content_policy_digest,
            )
        )).mappings().one_or_none()
        if root is None or content is None:
            return None
        if (root.connection_id != connection.connection_id
                or root.connection_version_id != connection.connection_version_id
                or root.resource_scope_reference != scope
                or root.content_policy_id != content.policy_id
                or root.content_policy_version != content.version
                or root.content_policy_digest != content.digest
                or content.connection_id != connection.connection_id
                or content.connection_version_id != connection.connection_version_id
                or content.resource_scope_reference != scope):
            return None
        return root, content


__all__ = ["PostgreSQLTrustedCanonicalSourceContextReader"]
