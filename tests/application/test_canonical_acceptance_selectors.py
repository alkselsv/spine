from datetime import UTC, datetime
from uuid import UUID

import pytest

from spine.application.canonical_acceptance_selectors import (
    CanonicalAcceptanceSelectorError,
    TrustedCanonicalAcceptanceSelectorResolver,
)
from spine.domain.sources.canonical_acceptance_selector import (
    CanonicalAcceptanceAuthorityKind,
    CanonicalAcceptanceSelectorHead,
    TrustedCanonicalAcceptanceSelector,
    TrustedSourceSelectorContext,
)


def ident(value: int) -> UUID:
    return UUID(int=value)


def source(**overrides: object) -> TrustedSourceSelectorContext:
    values: dict[str, object] = {
        "workspace_id": ident(1),
        "environment_id": ident(2),
        "source_object_id": ident(3),
        "source_kind": "google_drive",
        "source_contract_id": "google-drive-content",
        "source_contract_version": "v1",
        "authority_kind": CanonicalAcceptanceAuthorityKind.PROVIDER_CONNECTION,
        "approved_scope_reference": ident(4),
        "provider_kind": "google",
        "provider_connection_id": ident(5),
        "provider_connection_version_id": ident(6),
        "provider_approval_id": ident(7),
        "resource_scope_reference": ident(8),
    }
    values.update(overrides)
    return TrustedSourceSelectorContext.model_validate(values)


def selected(**overrides: object) -> TrustedCanonicalAcceptanceSelector:
    values: dict[str, object] = {
        "selector_id": ident(10),
        "selector_series_id": ident(11),
        "selector_version": 1,
        "workspace_id": ident(1),
        "environment_id": ident(2),
        "source_kind": "google_drive",
        "source_contract_id": "google-drive-content",
        "source_contract_version": "v1",
        "authority_kind": CanonicalAcceptanceAuthorityKind.PROVIDER_CONNECTION,
        "approved_scope_reference": ident(4),
        "provider_kind": "google",
        "provider_connection_id": ident(5),
        "provider_connection_version_id": ident(6),
        "provider_approval_id": ident(7),
        "resource_scope_reference": ident(8),
        "required_canonical_acceptance_schema_version": 1,
        "approval_reference": ident(12),
        "audit_reference": ident(13),
        "idempotency_identity": "google-v1-compatibility",
        "created_at": datetime(2026, 10, 31, tzinfo=UTC),
    }
    values.update(overrides)
    return TrustedCanonicalAcceptanceSelector.model_validate(values)


class SelectorReader:
    def __init__(self, *selectors: TrustedCanonicalAcceptanceSelector) -> None:
        self.selectors = selectors

    async def list_current(self, *, workspace_id: UUID, environment_id: UUID):
        return tuple(
            (
                item,
                CanonicalAcceptanceSelectorHead(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    selector_series_id=item.selector_series_id,
                    current_selector_id=item.selector_id,
                    cas_version=item.selector_version,
                ),
            )
            for item in self.selectors
        )


class SourceReader:
    def __init__(self, persisted_source: TrustedSourceSelectorContext | None = None) -> None:
        self.persisted_source = persisted_source or source()

    async def get_source_context(self, *, workspace_id: UUID, environment_id: UUID,
                                source_object_id: UUID, source_revision_id: UUID):
        return self.persisted_source


def resolver(*selectors: TrustedCanonicalAcceptanceSelector,
             persisted_source: TrustedSourceSelectorContext | None = None
             ) -> TrustedCanonicalAcceptanceSelectorResolver:
    return TrustedCanonicalAcceptanceSelectorResolver(
        SelectorReader(*selectors), SourceReader(persisted_source)
    )


async def resolve(service: TrustedCanonicalAcceptanceSelectorResolver):
    return await service.resolve(
        workspace_id=ident(1),
        environment_id=ident(2),
        source_object_id=ident(3),
        source_revision_id=ident(30),
    )


@pytest.mark.asyncio
async def test_resolver_returns_explicit_google_v1_compatibility_selector() -> None:
    selector = selected()
    service = resolver(selector)

    result = await resolve(service)

    assert result.selector.selector_id == selector.selector_id
    assert result.selector.required_canonical_acceptance_schema_version == 1
    assert result.head.cas_version == 1


@pytest.mark.asyncio
async def test_resolver_denies_missing_compatibility_selector() -> None:
    service = resolver()

    with pytest.raises(CanonicalAcceptanceSelectorError, match="missing or ambiguous"):
        await resolve(service)


@pytest.mark.asyncio
async def test_resolver_denies_stale_provider_version() -> None:
    service = resolver(
        selected(), persisted_source=source(provider_connection_version_id=ident(99))
    )

    with pytest.raises(CanonicalAcceptanceSelectorError, match="missing or ambiguous"):
        await service.resolve(workspace_id=ident(1), environment_id=ident(2),
                              source_object_id=ident(3), source_revision_id=ident(31))


@pytest.mark.asyncio
async def test_resolver_denies_overlapping_authority_with_conflicting_policy() -> None:
    one = selected(root_policy_id=ident(30), root_policy_version="v1", root_policy_digest="a" * 64,
                   content_policy_id=ident(31), content_policy_version="v1", content_policy_digest="b" * 64)
    two = selected(selector_id=ident(20), selector_series_id=ident(21),
                   root_policy_id=ident(32), root_policy_version="v2", root_policy_digest="c" * 64,
                   content_policy_id=ident(33), content_policy_version="v2", content_policy_digest="d" * 64)
    service = resolver(one, two)

    with pytest.raises(CanonicalAcceptanceSelectorError, match="missing or ambiguous"):
        await resolve(service)


@pytest.mark.asyncio
async def test_resolver_denies_current_head_mismatch() -> None:
    selector = selected()

    class StaleHeadReader(SelectorReader):
        async def list_current(self, *, workspace_id: UUID, environment_id: UUID):
            return ((selector, CanonicalAcceptanceSelectorHead(
                workspace_id=workspace_id,
                environment_id=environment_id,
                selector_series_id=selector.selector_series_id,
                current_selector_id=ident(99),
                cas_version=selector.selector_version,
            )),)

    service = TrustedCanonicalAcceptanceSelectorResolver(StaleHeadReader(selector), SourceReader())
    with pytest.raises(CanonicalAcceptanceSelectorError, match="head is stale"):
        await resolve(service)


@pytest.mark.asyncio
async def test_resolver_rejects_unsupported_schema_version() -> None:
    service = resolver(selected(required_canonical_acceptance_schema_version=3))
    with pytest.raises(CanonicalAcceptanceSelectorError, match="unsupported"):
        await resolve(service)
