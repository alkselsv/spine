from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from spine.domain.sources import (
    Bitrix24CrmMaterialAcceptance,
    Bitrix24CrmMaterialClaim,
    Bitrix24CrmMaterialRevocation,
    Bitrix24CrmMaterialState,
)
from spine.domain.sources.bitrix24_crm import (
    Bitrix24CrmMaterialHandoff,
    Bitrix24CrmMaterialV1OperationDeniedError,
)
from spine.infrastructure.persistence.postgresql_bitrix24_crm_material import (
    PostgreSQLBitrix24CrmMaterialAssociationRepository,
)

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def _claim() -> Bitrix24CrmMaterialClaim:
    return Bitrix24CrmMaterialClaim(
        claim_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        connection_id=_id(4),
        connection_version_id=_id(5),
        resource_scope_reference=_id(6),
        crm_entity_type="deal",
        crm_entity_id="932",
        crm_activity_id="501",
        crm_activity_revision="activity-v7",
        crm_attachment_id="attachment-1",
        relationship_revision="link-v3",
        target_source_object_id=_id(7),
        target_source_revision_id=_id(8),
        disk_file_id="9043",
        disk_file_revision="42",
        binding_evidence_digest="a" * 64,
        observed_at=NOW,
    )


def _acceptance() -> Bitrix24CrmMaterialAcceptance:
    return Bitrix24CrmMaterialAcceptance(
        acceptance_id=_id(10),
        claim_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        target_source_object_id=_id(7),
        target_source_revision_id=_id(8),
        disk_file_id="9043",
        disk_file_revision="42",
        accepted_by=_id(11),
        accepted_at=NOW,
    )


def _revocation() -> Bitrix24CrmMaterialRevocation:
    return Bitrix24CrmMaterialRevocation(
        revocation_id=_id(12),
        claim_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        state=Bitrix24CrmMaterialState.RELATION_REVOKED,
        reason="activity_attachment_removed",
        observed_at=NOW,
    )


def _repository() -> PostgreSQLBitrix24CrmMaterialAssociationRepository:
    def unexpected_uow(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("legacy CRM v1 operation reached PostgreSQL persistence")

    def unexpected_context(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("legacy CRM v1 operation created a persistence context")

    return PostgreSQLBitrix24CrmMaterialAssociationRepository(
        uow_factory=unexpected_uow,
        context_factory=unexpected_context,
    )


@pytest.mark.asyncio
async def test_direct_record_claim_denies_before_creating_postgresql_uow() -> None:
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await _repository().record_claim(_claim())


@pytest.mark.asyncio
async def test_direct_accept_claim_denies_before_creating_postgresql_uow() -> None:
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await _repository().accept_claim(claim_id=_id(1), acceptance=_acceptance())


@pytest.mark.asyncio
async def test_direct_revoke_claim_denies_before_creating_postgresql_uow() -> None:
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await _repository().revoke_claim(_revocation())


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None

    def first(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _ReadOnlyUow:
    def __init__(
        self,
        claim: Bitrix24CrmMaterialClaim,
        acceptance: Bitrix24CrmMaterialAcceptance | None = None,
        revocation: Bitrix24CrmMaterialRevocation | None = None,
    ) -> None:
        self._rows = {
            "bitrix24_crm_material_claims": [{"record": claim.model_dump(mode="json")}],
            "bitrix24_crm_material_acceptances": (
                [] if acceptance is None else [{"record": acceptance.model_dump(mode="json")}]
            ),
            "bitrix24_crm_material_revocations": (
                [] if revocation is None else [{"record": revocation.model_dump(mode="json")}]
            ),
        }

    async def __aenter__(self) -> _ReadOnlyUow:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def _execute(self, statement: Any) -> _Result:
        table = statement.get_final_froms()[0].name
        return _Result(self._rows[table])

    async def rollback(self) -> None:
        return None


def _read_repository(
    claim: Bitrix24CrmMaterialClaim,
    *,
    acceptance: Bitrix24CrmMaterialAcceptance | None = None,
    revocation: Bitrix24CrmMaterialRevocation | None = None,
) -> PostgreSQLBitrix24CrmMaterialAssociationRepository:
    return PostgreSQLBitrix24CrmMaterialAssociationRepository(
        uow_factory=lambda _: _ReadOnlyUow(claim, acceptance, revocation),
        context_factory=lambda *_: object(),
    )


@pytest.mark.asyncio
async def test_historical_claim_read_preserves_persisted_v1_record() -> None:
    claim = _claim()

    replay = await _read_repository(claim).resolve_claim(claim.claim_id)

    assert replay == claim


@pytest.mark.asyncio
async def test_historical_handoff_read_preserves_acceptance_and_revocation() -> None:
    claim = _claim()
    acceptance = _acceptance()
    revocation = _revocation()

    accepted = await _read_repository(claim, acceptance=acceptance).resolve_handoff(
        claim.claim_id
    )
    revoked = await _read_repository(claim, revocation=revocation).resolve_handoff(
        claim.claim_id
    )

    assert accepted == Bitrix24CrmMaterialHandoff(claim=claim, acceptance=acceptance)
    assert revoked == Bitrix24CrmMaterialHandoff(claim=claim, revocation=revocation)
