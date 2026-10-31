from dataclasses import replace

import pytest

from spine.application.disclosure_holds import DisclosureHoldAuthorityError
from spine.application.persistence.context import (
    PersistenceOperation,
    PersistencePurpose,
)
from spine.domain.common import ContextOrigin
from spine.infrastructure.disclosure_hold_composition import (
    PostgreSQLDisclosureHoldEffectsWriter,
    PostgreSQLCurrentDisclosureHoldAuthority,
)
from tests.application.test_disclosure_holds import _context, _hold, _release


class _Mappings:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def one_or_none(self) -> dict[str, object] | None:
        return self.row


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def mappings(self) -> _Mappings:
        return _Mappings(self.row)


class _Transaction:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self.statements: list[object] = []
        self.row = row

    async def _execute(self, statement: object, parameters: object | None = None) -> object:
        self.statements.append((statement, parameters))
        return _Result(self.row)


def _authority_row(record: object) -> dict[str, object]:
    return {
        "authority_reference": record.authority_reference,
        "authority_version_id": record.authority_version_id,
        "authority_digest": record.authority_digest,
        "actor_id": record.actor_id,
    }


def _governance_context(*, release: bool = False):
    return replace(
        _context(),
        purpose=PersistencePurpose("source_governance"),
        operation=PersistenceOperation("disclosure_hold.release" if release else "disclosure_hold.create"),
    )


@pytest.mark.asyncio
async def test_bound_authority_accepts_only_interactive_exact_operation_and_grant() -> None:
    hold = _hold()
    transaction = _Transaction(_authority_row(hold))
    authority = PostgreSQLCurrentDisclosureHoldAuthority()

    await authority.authorize_create(hold, _governance_context(), transaction)

    worker = replace(
        _governance_context(),
        origin=ContextOrigin.WORKER,
        acting_subject_id=None,
        service_principal_id=hold.actor_id,
    )
    with pytest.raises(DisclosureHoldAuthorityError):
        await authority.authorize_create(hold, worker, transaction)
    with pytest.raises(DisclosureHoldAuthorityError):
        await authority.authorize_create(
            hold,
            replace(
                _governance_context(),
                operation=PersistenceOperation("disclosure_hold.release"),
            ),
            transaction,
        )
    with pytest.raises(DisclosureHoldAuthorityError):
        await authority.authorize_create(
            hold.model_copy(update={"authority_digest": "f" * 64}),
            _governance_context(),
            transaction,
        )


@pytest.mark.asyncio
async def test_bound_release_and_effects_emit_separate_safe_records() -> None:
    hold = _hold()
    release = _release(hold)
    transaction = _Transaction(_authority_row(release))
    authority = PostgreSQLCurrentDisclosureHoldAuthority()
    context = _governance_context(release=True)

    await authority.authorize_release(release, hold, context, transaction)
    result = await PostgreSQLDisclosureHoldEffectsWriter().persist_release(release, context, transaction)

    assert result == (release.audit_event_id, release.outbox_event_id)
    assert len(transaction.statements) == 3
