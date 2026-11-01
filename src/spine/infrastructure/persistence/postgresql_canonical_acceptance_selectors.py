"""PostgreSQL storage for selector history and serialized current heads."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from spine.domain.audit import AuditEvent

from spine.application.canonical_acceptance_selectors import (
    CanonicalAcceptanceSelectorError,
)
from spine.application.persistence.context import PersistenceOperation, PersistencePurpose
from spine.domain.common import ContextOrigin
from spine.domain.sources.canonical_acceptance_selector import (
    CanonicalAcceptanceSelectorHead,
    TrustedCanonicalAcceptanceSelector,
)
from spine.infrastructure.connections.postgresql_provider_management import (
    provider_connection_approvals,
    provider_connection_versions,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    current_trusted_canonical_acceptance_selectors,
    trusted_canonical_acceptance_selector_versions,
    original_upload_records,
    bitrix24_disk_root_policy_versions,
    bitrix24_disk_content_policy_versions,
    current_bitrix24_disk_root_policies,
    current_bitrix24_disk_content_policies,
)


@dataclass(frozen=True, slots=True)
class SelectorPublicationResult:
    selector: TrustedCanonicalAcceptanceSelector
    committed_cas_version: int
    replayed: bool


class PostgreSQLCanonicalAcceptanceSelectorRepository:
    """Append selector versions and advance their tenant-scoped CAS heads."""

    def __init__(self, *, uow_factory: Callable[..., object], context_factory: Callable[[str, UUID, UUID], object]) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def list_current(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> tuple[tuple[TrustedCanonicalAcceptanceSelector, CanonicalAcceptanceSelectorHead], ...]:
        context = self._context_factory("canonical_acceptance_selector.read", workspace_id, environment_id)
        async with self._uow_factory(context) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(
                    trusted_canonical_acceptance_selector_versions.c.record,
                    current_trusted_canonical_acceptance_selectors.c.current_selector_id,
                    current_trusted_canonical_acceptance_selectors.c.cas_version,
                ).select_from(
                    trusted_canonical_acceptance_selector_versions.join(
                        current_trusted_canonical_acceptance_selectors,
                        (trusted_canonical_acceptance_selector_versions.c.workspace_id == current_trusted_canonical_acceptance_selectors.c.workspace_id)
                        & (trusted_canonical_acceptance_selector_versions.c.environment_id == current_trusted_canonical_acceptance_selectors.c.environment_id)
                        & (trusted_canonical_acceptance_selector_versions.c.selector_series_id == current_trusted_canonical_acceptance_selectors.c.selector_series_id)
                        & (trusted_canonical_acceptance_selector_versions.c.selector_id == current_trusted_canonical_acceptance_selectors.c.current_selector_id),
                    )
                ).where(
                    current_trusted_canonical_acceptance_selectors.c.workspace_id == workspace_id,
                    current_trusted_canonical_acceptance_selectors.c.environment_id == environment_id,
                )
            )
            records = []
            for row in result.mappings():
                selector = TrustedCanonicalAcceptanceSelector.model_validate(row.record)
                records.append((
                    selector,
                    CanonicalAcceptanceSelectorHead(
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                        selector_series_id=selector.selector_series_id,
                        current_selector_id=row.current_selector_id,
                        cas_version=row.cas_version,
                    ),
                ))
            await uow.rollback()  # type: ignore[attr-defined]
            return tuple(records)

    async def publish(
        self,
        selector: TrustedCanonicalAcceptanceSelector,
        *,
        expected_current_selector_id: UUID | None,
        expected_cas_version: int | None,
        recorded_at: datetime,
        audit_event: AuditEvent,
    ) -> SelectorPublicationResult:
        if (expected_current_selector_id is None) != (expected_cas_version is None):
            raise ValueError("expected selector ID and CAS version must be supplied together")
        context = self._context_factory(
            "canonical_acceptance_selector.publish",
            selector.workspace_id,
            selector.environment_id,
        )
        if (
            context.origin is not ContextOrigin.INTERACTIVE
            or context.acting_subject_id is None
            or context.purpose != PersistencePurpose("canonical_acceptance_selector")
            or context.operation != PersistenceOperation("canonical_acceptance_selector.publish")
        ):
            raise CanonicalAcceptanceSelectorError(
                "Only an authorized interactive context may publish canonical acceptance selectors."
            )
        heads = current_trusted_canonical_acceptance_selectors
        versions = trusted_canonical_acceptance_selector_versions
        try:
            async with self._uow_factory(context) as uow:
                replay_result = await uow._execute(  # type: ignore[attr-defined]
                    select(versions.c.record).where(
                        versions.c.workspace_id == selector.workspace_id,
                        versions.c.environment_id == selector.environment_id,
                        versions.c.idempotency_identity == selector.idempotency_identity,
                    )
                )
                replay_row = replay_result.one_or_none()
                if replay_row is not None:
                    existing = TrustedCanonicalAcceptanceSelector.model_validate(replay_row.record)
                    if existing != selector:
                        raise CanonicalAcceptanceSelectorError(
                            "Selector idempotency identity conflicts with prior command."
                        )
                    await uow.rollback()  # type: ignore[attr-defined]
                    return SelectorPublicationResult(existing, existing.selector_version, True)
                if selector.provider_connection_id is not None:
                    # Provider version publication takes the exclusive form of
                    # this same lock. Hold a shared lock through our commit so
                    # the authority cannot be superseded after validation.
                    await uow._execute(  # type: ignore[attr-defined]
                        text(
                            "SELECT pg_advisory_xact_lock_shared("
                            "hashtextextended(:connection_lock, 0))"
                        ),
                        {"connection_lock": str(selector.provider_connection_id)},
                    )
                await self._validate_approved_authority(uow, selector)
                if (
                    audit_event.audit_event_id != selector.audit_reference
                    or audit_event.workspace_id != selector.workspace_id
                    or audit_event.environment_id != selector.environment_id
                    or audit_event.event_type != "command.outcome"
                    or audit_event.outcome.value != "accepted"
                    or audit_event.origin is not context.origin
                    or audit_event.acting_subject_id != context.acting_subject_id
                    or audit_event.service_principal_id != context.service_principal_id
                ):
                    raise CanonicalAcceptanceSelectorError(
                        "Selector Audit Event is invalid for this command."
                    )
                await uow.audit.append(audit_event)  # type: ignore[attr-defined]
                head_result = await uow._execute(  # type: ignore[attr-defined]
                    select(heads).where(
                        heads.c.workspace_id == selector.workspace_id,
                        heads.c.environment_id == selector.environment_id,
                        heads.c.selector_series_id == selector.selector_series_id,
                    ).with_for_update()
                )
                current = head_result.mappings().one_or_none()
                actual = None if current is None else (current.current_selector_id, current.cas_version)
                expected = None if expected_current_selector_id is None else (expected_current_selector_id, expected_cas_version)
                if actual != expected:
                    raise CanonicalAcceptanceSelectorError("Canonical acceptance selector head changed.")
                version = 1 if current is None else current.cas_version + 1
                if selector.selector_version != version or selector.predecessor_selector_id != (None if current is None else current.current_selector_id):
                    raise CanonicalAcceptanceSelectorError("Canonical acceptance selector successor is invalid.")
                if current is not None:
                    predecessor_result = await uow._execute(  # type: ignore[attr-defined]
                        select(versions.c.record).where(
                            versions.c.workspace_id == selector.workspace_id,
                            versions.c.environment_id == selector.environment_id,
                            versions.c.selector_id == current.current_selector_id,
                        )
                    )
                    predecessor_row = predecessor_result.one_or_none()
                    if predecessor_row is None:
                        raise CanonicalAcceptanceSelectorError("Canonical acceptance selector history is incomplete.")
                    predecessor = TrustedCanonicalAcceptanceSelector.model_validate(predecessor_row.record)
                    if predecessor.series_identity != selector.series_identity:
                        raise CanonicalAcceptanceSelectorError("Canonical acceptance selector series identity cannot change.")
                await uow._execute(  # type: ignore[attr-defined]
                    insert(versions).values(
                        selector_id=selector.selector_id,
                        selector_series_id=selector.selector_series_id,
                        selector_version=selector.selector_version,
                        workspace_id=selector.workspace_id,
                        environment_id=selector.environment_id,
                        source_kind=selector.source_kind,
                        source_contract_id=selector.source_contract_id,
                        source_contract_version=selector.source_contract_version,
                        authority_kind=selector.authority_kind.value,
                        approved_scope_reference=selector.approved_scope_reference,
                        provider_kind=selector.provider_kind,
                        provider_connection_id=selector.provider_connection_id,
                        provider_connection_version_id=selector.provider_connection_version_id,
                        provider_approval_id=selector.provider_approval_id,
                        resource_scope_reference=selector.resource_scope_reference,
                        upload_authority_reference=selector.upload_authority_reference,
                        root_policy_id=selector.root_policy_id,
                        root_policy_version=selector.root_policy_version,
                        root_policy_digest=selector.root_policy_digest,
                        content_policy_id=selector.content_policy_id,
                        content_policy_version=selector.content_policy_version,
                        content_policy_digest=selector.content_policy_digest,
                        required_schema_version=selector.required_canonical_acceptance_schema_version,
                        approval_reference=selector.approval_reference,
                        predecessor_selector_id=selector.predecessor_selector_id,
                        audit_reference=selector.audit_reference,
                        idempotency_identity=selector.idempotency_identity,
                        record=selector.model_dump(mode="json"),
                        created_at=recorded_at,
                    )
                )
                if current is None:
                    await uow._execute(  # type: ignore[attr-defined]
                        insert(heads).values(
                            workspace_id=selector.workspace_id,
                            environment_id=selector.environment_id,
                            selector_series_id=selector.selector_series_id,
                            current_selector_id=selector.selector_id,
                            cas_version=1,
                            updated_at=recorded_at,
                        )
                    )
                else:
                    result = await uow._execute(  # type: ignore[attr-defined]
                        update(heads).where(
                            heads.c.workspace_id == selector.workspace_id,
                            heads.c.environment_id == selector.environment_id,
                            heads.c.selector_series_id == selector.selector_series_id,
                            heads.c.current_selector_id == expected_current_selector_id,
                            heads.c.cas_version == expected_cas_version,
                        ).values(
                            current_selector_id=selector.selector_id,
                            cas_version=version,
                            updated_at=recorded_at,
                        )
                    )
                    if result.rowcount != 1:
                        raise CanonicalAcceptanceSelectorError("Canonical acceptance selector head changed.")
                await uow.commit()  # type: ignore[attr-defined]
                return SelectorPublicationResult(selector, version, False)
        except IntegrityError as error:
            raise CanonicalAcceptanceSelectorError("Canonical acceptance selector conflicts with immutable history.") from error

    @staticmethod
    async def _validate_approved_authority(
        uow: object, selector: TrustedCanonicalAcceptanceSelector
    ) -> None:
        if selector.provider_connection_id is None:
            upload = (await uow._execute(  # type: ignore[attr-defined]
                select(original_upload_records.c.upload_id).where(
                    original_upload_records.c.workspace_id == selector.workspace_id,
                    original_upload_records.c.environment_id == selector.environment_id,
                    original_upload_records.c.upload_id == selector.upload_authority_reference,
                )
            )).scalar_one_or_none()
            if upload is None:
                raise CanonicalAcceptanceSelectorError("Approved upload authority is unavailable.")
            return

        connection = (await uow._execute(  # type: ignore[attr-defined]
            select(provider_connection_versions).where(
                provider_connection_versions.c.workspace_id == selector.workspace_id,
                provider_connection_versions.c.environment_id == selector.environment_id,
                provider_connection_versions.c.connection_id == selector.provider_connection_id,
                provider_connection_versions.c.connection_version_id == selector.provider_connection_version_id,
            )
        )).mappings().one_or_none()
        latest_id = (await uow._execute(  # type: ignore[attr-defined]
            select(provider_connection_versions.c.connection_version_id)
            .where(
                provider_connection_versions.c.workspace_id == selector.workspace_id,
                provider_connection_versions.c.environment_id == selector.environment_id,
                provider_connection_versions.c.connection_id == selector.provider_connection_id,
            )
            .order_by(provider_connection_versions.c.version.desc())
            .limit(1)
        )).scalar_one_or_none()
        if (
            connection is None
            or latest_id != selector.provider_connection_version_id
            or connection.status != "active"
            or connection.provider_kind != selector.provider_kind
            or connection.approval_id != selector.provider_approval_id
            or selector.resource_scope_reference not in connection.approved_resource_scope_references
        ):
            raise CanonicalAcceptanceSelectorError("Current provider connection authority does not match selector.")
        approval = (await uow._execute(  # type: ignore[attr-defined]
            select(provider_connection_approvals).where(
                provider_connection_approvals.c.workspace_id == selector.workspace_id,
                provider_connection_approvals.c.environment_id == selector.environment_id,
                provider_connection_approvals.c.approval_id == selector.provider_approval_id,
                provider_connection_approvals.c.provider_kind == selector.provider_kind,
                provider_connection_approvals.c.status == "active",
            )
        )).mappings().one_or_none()
        if (approval is None
                or selector.resource_scope_reference not in approval.approved_resource_scope_references
                or approval.authorization_generation != connection.authorization_generation
                or approval.configuration_generation != connection.configuration_generation):
            raise CanonicalAcceptanceSelectorError("Current provider approval is unavailable.")
        if selector.root_policy_id is None:
            if selector.provider_kind == "bitrix24":
                raise CanonicalAcceptanceSelectorError("Bitrix24 selectors require root and content policies.")
            return
        root_head = (await uow._execute(  # type: ignore[attr-defined]
            select(current_bitrix24_disk_root_policies).where(
                current_bitrix24_disk_root_policies.c.workspace_id == selector.workspace_id,
                current_bitrix24_disk_root_policies.c.environment_id == selector.environment_id,
                current_bitrix24_disk_root_policies.c.policy_id == selector.root_policy_id,
                current_bitrix24_disk_root_policies.c.current_version == selector.root_policy_version,
                current_bitrix24_disk_root_policies.c.current_digest == selector.root_policy_digest,
            ).with_for_update()
        )).mappings().one_or_none()
        content_head = (await uow._execute(  # type: ignore[attr-defined]
            select(current_bitrix24_disk_content_policies).where(
                current_bitrix24_disk_content_policies.c.workspace_id == selector.workspace_id,
                current_bitrix24_disk_content_policies.c.environment_id == selector.environment_id,
                current_bitrix24_disk_content_policies.c.policy_id == selector.content_policy_id,
                current_bitrix24_disk_content_policies.c.current_version == selector.content_policy_version,
                current_bitrix24_disk_content_policies.c.current_digest == selector.content_policy_digest,
            ).with_for_update()
        )).mappings().one_or_none()
        root = (await uow._execute(  # type: ignore[attr-defined]
            select(bitrix24_disk_root_policy_versions).where(
                bitrix24_disk_root_policy_versions.c.workspace_id == selector.workspace_id,
                bitrix24_disk_root_policy_versions.c.environment_id == selector.environment_id,
                bitrix24_disk_root_policy_versions.c.policy_id == selector.root_policy_id,
                bitrix24_disk_root_policy_versions.c.version == selector.root_policy_version,
                bitrix24_disk_root_policy_versions.c.digest == selector.root_policy_digest,
            )
        )).mappings().one_or_none()
        content = (await uow._execute(  # type: ignore[attr-defined]
            select(bitrix24_disk_content_policy_versions).where(
                bitrix24_disk_content_policy_versions.c.workspace_id == selector.workspace_id,
                bitrix24_disk_content_policy_versions.c.environment_id == selector.environment_id,
                bitrix24_disk_content_policy_versions.c.policy_id == selector.content_policy_id,
                bitrix24_disk_content_policy_versions.c.version == selector.content_policy_version,
                bitrix24_disk_content_policy_versions.c.digest == selector.content_policy_digest,
            )
        )).mappings().one_or_none()
        if (
            root_head is None or content_head is None or root is None or content is None
            or root.connection_id != selector.provider_connection_id
            or root.connection_version_id != selector.provider_connection_version_id
            or root.resource_scope_reference != selector.resource_scope_reference
            or root.content_policy_id != content.policy_id
            or root.content_policy_version != content.version
            or root.content_policy_digest != content.digest
            or content.connection_id != selector.provider_connection_id
            or content.connection_version_id != selector.provider_connection_version_id
            or content.resource_scope_reference != selector.resource_scope_reference
        ):
            raise CanonicalAcceptanceSelectorError("Exact current root/content policy authority does not match selector.")


__all__ = ["PostgreSQLCanonicalAcceptanceSelectorRepository", "SelectorPublicationResult"]
