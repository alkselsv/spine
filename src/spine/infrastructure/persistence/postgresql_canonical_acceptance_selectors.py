"""PostgreSQL storage for selector history and serialized current heads."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from spine.application.canonical_acceptance_selectors import CanonicalAcceptanceSelectorError
from spine.domain.sources.canonical_acceptance_selector import (
    CanonicalAcceptanceSelectorHead,
    TrustedCanonicalAcceptanceSelector,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    current_trusted_canonical_acceptance_selectors,
    trusted_canonical_acceptance_selector_versions,
)


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
    ) -> CanonicalAcceptanceSelectorHead:
        if (expected_current_selector_id is None) != (expected_cas_version is None):
            raise ValueError("expected selector ID and CAS version must be supplied together")
        context = self._context_factory("canonical_acceptance_selector.write", selector.workspace_id, selector.environment_id)
        heads = current_trusted_canonical_acceptance_selectors
        versions = trusted_canonical_acceptance_selector_versions
        try:
            async with self._uow_factory(context) as uow:
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
                return CanonicalAcceptanceSelectorHead(
                    workspace_id=selector.workspace_id,
                    environment_id=selector.environment_id,
                    selector_series_id=selector.selector_series_id,
                    current_selector_id=selector.selector_id,
                    cas_version=version,
                )
        except IntegrityError as error:
            raise CanonicalAcceptanceSelectorError("Canonical acceptance selector conflicts with immutable history.") from error


__all__ = ["PostgreSQLCanonicalAcceptanceSelectorRepository"]
