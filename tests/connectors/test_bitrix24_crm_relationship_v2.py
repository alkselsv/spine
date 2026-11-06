from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import (
    Bitrix24PinnedGrant,
    SecretTokenPair,
)
from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ReadCommand,
    CrmRelationshipBindingOperationsProfile,
)
from spine.connectors.bitrix24_crm_relationship_v2 import (
    Bitrix24CrmActivityBindingReader,
    CrmActivityBindingPage,
    CrmActivityBindingPagePort,
    CrmActivityBindingListRequest,
    CrmActivityEntityPair,
    CrmBindingPageStatus,
)
from spine.domain.connections.provider import OpaqueProviderReference
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile


def ident(value: int) -> UUID:
    return UUID(int=value)


def _profile(**updates: object) -> CrmRelationshipMappingProfile:
    values: dict[str, object] = {
        "schema_version": "bitrix24-crm-files-mapping-profile:v1",
        "record_kind": "mapping_profile",
        "profile_id": ident(1),
        "workspace_id": ident(2),
        "environment_id": ident(3),
        "profile_version": 1,
        "provider_instance": "synthetic-portal",
        "connection_id": ident(4),
        "connection_approval_id": ident(5),
        "qualification_reference": ident(6),
        "approval_reference": ident(7),
        "activity_namespace": "bitrix24-activity",
        "activity_type_ids": ("meeting",),
        "raw_entity_type_ids": ("DEAL",),
        "entity_namespace": "bitrix24-entity",
        "files_namespace": "bitrix24-files",
        "normalizer_id": "crm.activity.binding.list:entityTypeId",
        "normalizer_version": "v1",
        "read_methods": ("crm.activity.binding.list",),
        "mapping_strategy": "direct_disk_object",
        "disk_namespace": "bitrix24-disk",
    }
    values.update(updates)
    return CrmRelationshipMappingProfile(**values)


def _command(**updates: object) -> Bitrix24CrmRelationshipV2ReadCommand:
    values: dict[str, object] = {
        "workspace_id": ident(2),
        "environment_id": ident(3),
        "acting_human_id": ident(8),
        "human_authorization_generation": 3,
        "connection_id": ident(4),
        "connection_version_id": ident(9),
        "authorization_generation": 4,
        "configuration_generation": 5,
        "resource_scope_reference": OpaqueProviderReference(value=ident(10)),
        "provider_instance": "synthetic-portal",
        "provider_acting_user_id": "provider-user",
        "activity_namespace": "bitrix24-activity",
        "activity_type_id": "meeting",
        "activity_id": "activity-17",
        "raw_entity_type_id": "DEAL",
        "entity_namespace": "bitrix24-entity",
        "entity_id": "entity-51",
        "mapping_profile": _profile(),
        "operations_profile": CrmRelationshipBindingOperationsProfile(
            profile_id="synthetic.crm-binding",
            profile_version="v1",
            profile_kind="synthetic",
            page_size=2,
            max_pages=3,
            max_response_bytes=100,
            max_attempts=6,
        ),
        "pinned_grant": Bitrix24PinnedGrant(
            binding_id=ident(11),
            acting_user_id="provider-user",
            token_epoch=1,
            security_fence=1,
            tokens=SecretTokenPair(access_token="secret-a", refresh_token="secret-r"),
        ),
    }
    values.update(updates)
    return Bitrix24CrmRelationshipV2ReadCommand(**values)  # type: ignore[arg-type]


def _page(
    pairs: tuple[tuple[str, str], ...] = (),
    *,
    next_start: int | None = None,
    status: CrmBindingPageStatus = CrmBindingPageStatus.SUCCESS,
    response_bytes: int = 10,
    permission_filtered: bool = False,
) -> CrmActivityBindingPage:
    return CrmActivityBindingPage(
        status=status,
        items=tuple(CrmActivityEntityPair(entity_type_id=kind, entity_id=value) for kind, value in pairs),
        response_bytes=response_bytes,
        next_start=next_start,
        permission_filtered=permission_filtered,
    )


class Pages(CrmActivityBindingPagePort):
    def __init__(self, *responses: CrmActivityBindingPage | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[CrmActivityBindingListRequest] = []
        self.admitted_commands: list[Bitrix24CrmRelationshipV2ReadCommand] = []

    async def list_activity_bindings(
        self,
        request: CrmActivityBindingListRequest,
        *,
        admitted_command: Bitrix24CrmRelationshipV2ReadCommand,
    ) -> CrmActivityBindingPage:
        self.requests.append(request)
        self.admitted_commands.append(admitted_command)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("page_index", [0, 1])
async def test_exact_raw_pair_on_first_or_later_page_is_positive(page_index: int) -> None:
    pages = Pages(
        *(
            [_page((("COMPANY", "unrelated"),), next_start=2)] * page_index
            + [_page((("DEAL", "entity-51"),), next_start=4)]
        )
    )

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.positive_pair_observed is True
    assert result.outcome == "POSITIVE_OBSERVED"
    assert result.binding_method == "crm.activity.binding.list"
    assert result.normalizer_id == "crm.activity.binding.list:entityTypeId"
    assert result.normalizer_version == "v1"
    assert result.raw_entity_type_id == "DEAL"
    assert result.entity_id == "entity-51"
    assert result.page_count == page_index + 1
    assert result.snapshot_atomic is False
    assert [request.activity_id for request in pages.requests] == ["activity-17"] * (page_index + 1)
    assert [request.method for request in pages.requests] == ["crm.activity.binding.list"] * (page_index + 1)
    assert all(
        item.connection_id == ident(4) and item.authorization_generation == 4
        for item in pages.admitted_commands
    )


@pytest.mark.asyncio
async def test_raw_entity_pair_mismatch_is_not_positive() -> None:
    pages = Pages(_page((("COMPANY", "entity-51"),)))

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.positive_pair_observed is False
    assert result.outcome == "NOT_VERIFIED"
    assert result.page_count == 1


@pytest.mark.asyncio
async def test_profile_with_unknown_normalizer_fails_closed_without_provider_call() -> None:
    pages = Pages(_page((("DEAL", "entity-51"),)))

    result = await Bitrix24CrmActivityBindingReader(pages).read(
        _command(mapping_profile=_profile(normalizer_id="unknown-normalizer"))
    )

    assert result.outcome == "NOT_VERIFIED"
    assert pages.requests == []


@pytest.mark.asyncio
async def test_filtered_absence_is_inaccessible() -> None:
    pages = Pages(_page(permission_filtered=True))

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.outcome == "INACCESSIBLE"
    assert result.positive_pair_observed is False


@pytest.mark.asyncio
async def test_exact_pair_on_permission_filtered_page_is_still_positive() -> None:
    pages = Pages(_page((("DEAL", "entity-51"),), permission_filtered=True))

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.outcome == "POSITIVE_OBSERVED"
    assert result.positive_pair_observed is True


@pytest.mark.asyncio
async def test_malformed_page_is_uncertain_without_disclosing_transport_error() -> None:
    pages = Pages(
        _page(
            status=CrmBindingPageStatus.MALFORMED,
        )
    )

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.outcome == "NOT_VERIFIED"
    assert result.positive_pair_observed is False


@pytest.mark.asyncio
async def test_page_cursor_outside_supplied_bounds_is_uncertain() -> None:
    pages = Pages(_page((("COMPANY", "other"),), next_start=7))

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.outcome == "NOT_VERIFIED"
    assert result.page_count == 0


@pytest.mark.asyncio
async def test_transient_page_attempts_are_bounded_by_operations_profile() -> None:
    pages = Pages(
        _page(status=CrmBindingPageStatus.TRANSIENT_FAILURE),
        _page(status=CrmBindingPageStatus.TRANSIENT_FAILURE),
    )
    command = _command(
        operations_profile=CrmRelationshipBindingOperationsProfile(
            profile_id="synthetic.crm-binding",
            profile_version="v1",
            profile_kind="synthetic",
            page_size=2,
            max_pages=3,
            max_response_bytes=100,
            max_attempts=2,
        )
    )

    result = await Bitrix24CrmActivityBindingReader(pages).read(command)

    assert result.outcome == "NOT_VERIFIED"
    assert len(pages.requests) == 2


@pytest.mark.asyncio
async def test_positive_observation_is_fetched_again_instead_of_cached() -> None:
    pages = Pages(_page((("DEAL", "entity-51"),)), _page(()))
    reader = Bitrix24CrmActivityBindingReader(pages)

    first = await reader.read(_command())
    second = await reader.read(_command())

    assert first.positive_pair_observed is True
    assert second.positive_pair_observed is False
    assert len(pages.requests) == 2


@pytest.mark.asyncio
async def test_page_limit_exhaustion_keeps_absence_uncertain() -> None:
    pages = Pages(
        _page((("COMPANY", "other-1"),), next_start=2),
        _page((("COMPANY", "other-2"),), next_start=4),
        _page((("COMPANY", "other-3"),), next_start=6),
    )

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.outcome == "NOT_VERIFIED"
    assert result.page_count == 3
    assert len(pages.requests) == 3


@pytest.mark.asyncio
async def test_response_byte_limit_is_enforced_before_accepting_evidence() -> None:
    pages = Pages(_page((("DEAL", "entity-51"),), response_bytes=101))

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.outcome == "NOT_VERIFIED"
    assert result.positive_pair_observed is False
    assert result.page_count == 0


@pytest.mark.asyncio
async def test_duplicate_pairs_are_typed_uncertainty() -> None:
    pages = Pages(_page((("DEAL", "entity-51"), ("DEAL", "entity-51"))))

    result = await Bitrix24CrmActivityBindingReader(pages).read(_command())

    assert result.outcome == "NOT_VERIFIED"
    assert result.positive_pair_observed is False


@pytest.mark.asyncio
async def test_provider_outage_is_sanitized_and_attempt_bounded() -> None:
    pages = Pages(
        RuntimeError("private response body and entity-51"),
        RuntimeError("private response body and entity-51"),
    )
    command = _command(
        operations_profile=CrmRelationshipBindingOperationsProfile(
            profile_id="synthetic.crm-binding",
            profile_version="v1",
            profile_kind="synthetic",
            page_size=2,
            max_pages=3,
            max_response_bytes=100,
            max_attempts=2,
        )
    )

    result = await Bitrix24CrmActivityBindingReader(pages).read(command)

    assert result.outcome == "AUTHORITY_UNAVAILABLE"
    assert len(pages.requests) == 2
    assert "private response body" not in result.model_dump_json()
    assert result.attempt_count == 2


@pytest.mark.asyncio
async def test_exact_page_request_exposes_no_generic_or_owner_fallback() -> None:
    pages = Pages(_page(()))

    await Bitrix24CrmActivityBindingReader(pages).read(_command())

    request = pages.requests[0]
    assert request.model_dump() == {
        "method": "crm.activity.binding.list",
        "activity_id": "activity-17",
        "start": 0,
        "limit": 2,
        "max_response_bytes": 100,
    }
