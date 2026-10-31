"""PostgreSQL adapter for immutable disclosure holds and release CAS."""

from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from spine.application.disclosure_holds import (
    CurrentDisclosureHolds,
    DisclosureConstraintQuery,
    DisclosureHoldAuthority,
    DisclosureHoldAuthorityError,
    DisclosureHoldConflictError,
    DisclosureHoldEffectsWriter,
    DisclosureHoldRepository,
)
from spine.application.lifecycle import (
    lifecycle_context_actor,
    validate_lifecycle_context,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import ConstraintConflictError
from spine.domain.sources.disclosure_holds import DisclosureHold, DisclosureHoldRelease
from spine.infrastructure.persistence.postgresql_mappings import (
    current_disclosure_holds,
    current_lifecycle_authorities,
    disclosure_hold_releases,
    disclosure_hold_source_revisions,
    disclosure_holds,
    lifecycle_decisions,
)


class PostgreSQLDisclosureHoldRepository(DisclosureHoldRepository):
    def __init__(
        self,
        *,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        authority: DisclosureHoldAuthority,
        effects: DisclosureHoldEffectsWriter,
    ) -> None:
        self._uow_factory = uow_factory
        self._authority = authority
        self._effects = effects

    async def create_hold(
        self, hold: DisclosureHold, context: TrustedPersistenceContext
    ) -> DisclosureHold:
        _context(context, hold.workspace_id, hold.environment_id, hold.actor_id)
        try:
            async with self._uow_factory(context) as uow:
                existing = await _hold_by_id(uow, hold.hold_id)
                if existing is not None:
                    await uow.rollback()  # type: ignore[attr-defined]
                    if existing != hold:
                        raise DisclosureHoldConflictError()
                    return existing
                current_result = await uow._execute(  # type: ignore[attr-defined]
                    select(
                        lifecycle_decisions.c.decision_id,
                        lifecycle_decisions.c.observation_id,
                        lifecycle_decisions.c.status,
                        lifecycle_decisions.c.effect,
                    )
                    .select_from(
                        current_lifecycle_authorities.join(
                            lifecycle_decisions,
                            (
                                lifecycle_decisions.c.workspace_id
                                == current_lifecycle_authorities.c.workspace_id
                            )
                            & (
                                lifecycle_decisions.c.environment_id
                                == current_lifecycle_authorities.c.environment_id
                            )
                            & (
                                lifecycle_decisions.c.source_object_id
                                == current_lifecycle_authorities.c.source_object_id
                            )
                            & (
                                lifecycle_decisions.c.decision_id
                                == current_lifecycle_authorities.c.current_decision_id
                            )
                            & (
                                lifecycle_decisions.c.generation
                                == current_lifecycle_authorities.c.generation
                            ),
                        )
                    )
                    .where(
                        current_lifecycle_authorities.c.workspace_id
                        == hold.workspace_id,
                        current_lifecycle_authorities.c.environment_id
                        == hold.environment_id,
                        current_lifecycle_authorities.c.source_object_id
                        == hold.source_object_id,
                    )
                    .with_for_update(of=current_lifecycle_authorities)
                )
                current = current_result.mappings().one_or_none()
                if current is None or (
                    current["decision_id"],
                    current["observation_id"],
                    current["status"],
                    current["effect"],
                ) != (
                    hold.review_decision_id,
                    hold.evidence_observation_id,
                    "disclosure_review_required",
                    "disclosure_review_required",
                ):
                    raise DisclosureHoldAuthorityError()
                if hold.supersedes_hold_id is not None:
                    predecessor_pointer_result = await uow._execute(  # type: ignore[attr-defined]
                        select(current_disclosure_holds.c.state)
                        .where(
                            current_disclosure_holds.c.workspace_id
                            == hold.workspace_id,
                            current_disclosure_holds.c.environment_id
                            == hold.environment_id,
                            current_disclosure_holds.c.source_object_id
                            == hold.source_object_id,
                            current_disclosure_holds.c.hold_id
                            == hold.supersedes_hold_id,
                        )
                        .with_for_update()
                    )
                    if predecessor_pointer_result.scalar_one_or_none() != "active":
                        raise DisclosureHoldConflictError()
                    successor_result = await uow._execute(  # type: ignore[attr-defined]
                        select(disclosure_holds.c.hold_id).where(
                            disclosure_holds.c.workspace_id == hold.workspace_id,
                            disclosure_holds.c.environment_id == hold.environment_id,
                            disclosure_holds.c.source_object_id == hold.source_object_id,
                            disclosure_holds.c.supersedes_hold_id
                            == hold.supersedes_hold_id,
                        )
                    )
                    if successor_result.scalar_one_or_none() is not None:
                        raise DisclosureHoldConflictError()
                await self._authority.authorize_create(hold, context, uow)
                if await self._effects.persist_hold(hold, context, uow) != (
                    hold.audit_event_id,
                    hold.outbox_event_id,
                ):
                    raise DisclosureHoldAuthorityError()
                await uow._execute(  # type: ignore[attr-defined]
                    insert(disclosure_holds).values(**_hold_columns(hold))
                )
                await uow._execute(  # type: ignore[attr-defined]
                    insert(disclosure_hold_source_revisions),
                    [
                        {
                            "workspace_id": hold.workspace_id,
                            "environment_id": hold.environment_id,
                            "source_object_id": hold.source_object_id,
                            "hold_id": hold.hold_id,
                            "source_revision_id": revision_id,
                        }
                        for revision_id in hold.source_revision_ids
                    ],
                )
                await uow._execute(  # type: ignore[attr-defined]
                    insert(current_disclosure_holds).values(
                        workspace_id=hold.workspace_id,
                        environment_id=hold.environment_id,
                        source_object_id=hold.source_object_id,
                        hold_id=hold.hold_id,
                        current_release_id=None,
                        state="active",
                        generation=1,
                        updated_at=hold.effective_from,
                    )
                )
                await uow.commit()  # type: ignore[attr-defined]
                return hold
        except (IntegrityError, ConstraintConflictError):
            raise DisclosureHoldAuthorityError() from None

    async def release_hold(
        self, release: DisclosureHoldRelease, context: TrustedPersistenceContext
    ) -> DisclosureHoldRelease:
        _context(
            context,
            release.workspace_id,
            release.environment_id,
            release.actor_id,
        )
        try:
            async with self._uow_factory(context) as uow:
                result = await uow._execute(  # type: ignore[attr-defined]
                    select(disclosure_hold_releases).where(
                        disclosure_hold_releases.c.workspace_id == release.workspace_id,
                        disclosure_hold_releases.c.environment_id
                        == release.environment_id,
                        disclosure_hold_releases.c.release_id == release.release_id,
                    )
                )
                row = result.mappings().one_or_none()
                if row is not None:
                    existing = _release(row)
                    await uow.rollback()  # type: ignore[attr-defined]
                    if existing != release:
                        raise DisclosureHoldConflictError()
                    return existing
                hold = await _hold_by_id(uow, release.hold_id)
                if hold is None:
                    raise DisclosureHoldAuthorityError()
                try:
                    release.validate_hold(hold)
                except ValueError:
                    raise DisclosureHoldAuthorityError() from None
                successor_result = await uow._execute(  # type: ignore[attr-defined]
                    select(disclosure_holds.c.hold_id).where(
                        disclosure_holds.c.workspace_id == hold.workspace_id,
                        disclosure_holds.c.environment_id == hold.environment_id,
                        disclosure_holds.c.source_object_id == hold.source_object_id,
                        disclosure_holds.c.supersedes_hold_id == hold.hold_id,
                    )
                )
                successor_id = successor_result.scalar_one_or_none()
                if successor_id is not None and release.superseded_by_hold_id != successor_id:
                    raise DisclosureHoldAuthorityError()
                if release.superseded_by_hold_id is not None:
                    successor = await _hold_by_id(uow, release.superseded_by_hold_id)
                    if (
                        successor is None
                        or successor.supersedes_hold_id != hold.hold_id
                        or (
                            successor.workspace_id,
                            successor.environment_id,
                            successor.source_object_id,
                        )
                        != (
                            hold.workspace_id,
                            hold.environment_id,
                            hold.source_object_id,
                        )
                    ):
                        raise DisclosureHoldAuthorityError()
                pointer_result = await uow._execute(  # type: ignore[attr-defined]
                    select(current_disclosure_holds)
                    .where(
                        current_disclosure_holds.c.workspace_id == release.workspace_id,
                        current_disclosure_holds.c.environment_id
                        == release.environment_id,
                        current_disclosure_holds.c.source_object_id
                        == release.source_object_id,
                        current_disclosure_holds.c.hold_id == release.hold_id,
                    )
                    .with_for_update()
                )
                pointer = pointer_result.mappings().one_or_none()
                if (
                    pointer is None
                    or pointer["state"] != "active"
                    or pointer["generation"] != 1
                ):
                    raise DisclosureHoldConflictError()
                await self._authority.authorize_release(release, hold, context, uow)
                if await self._effects.persist_release(release, context, uow) != (
                    release.audit_event_id,
                    release.outbox_event_id,
                ):
                    raise DisclosureHoldAuthorityError()
                await uow._execute(  # type: ignore[attr-defined]
                    insert(disclosure_hold_releases).values(**_release_columns(release))
                )
                write = await uow._execute(  # type: ignore[attr-defined]
                    update(current_disclosure_holds)
                    .where(
                        current_disclosure_holds.c.workspace_id == release.workspace_id,
                        current_disclosure_holds.c.environment_id
                        == release.environment_id,
                        current_disclosure_holds.c.source_object_id
                        == release.source_object_id,
                        current_disclosure_holds.c.hold_id == release.hold_id,
                        current_disclosure_holds.c.state == "active",
                        current_disclosure_holds.c.generation == 1,
                    )
                    .values(
                        current_release_id=release.release_id,
                        state="released",
                        generation=2,
                        updated_at=release.released_at,
                    )
                    .returning(current_disclosure_holds.c.current_release_id)
                )
                if write.scalar_one_or_none() != release.release_id:
                    raise DisclosureHoldConflictError()
                await uow.commit()  # type: ignore[attr-defined]
                return release
        except (IntegrityError, ConstraintConflictError):
            raise DisclosureHoldAuthorityError() from None

    async def active_holds(
        self,
        query: DisclosureConstraintQuery,
        context: TrustedPersistenceContext,
    ) -> CurrentDisclosureHolds:
        _context(context, query.workspace_id, query.environment_id, None)
        async with self._uow_factory(context) as uow:
            result = await uow._execute(  # type: ignore[attr-defined]
                select(disclosure_holds.c.hold_id)
                .select_from(
                    disclosure_holds.join(
                        current_disclosure_holds,
                        (
                            current_disclosure_holds.c.workspace_id
                            == disclosure_holds.c.workspace_id
                        )
                        & (
                            current_disclosure_holds.c.environment_id
                            == disclosure_holds.c.environment_id
                        )
                        & (
                            current_disclosure_holds.c.source_object_id
                            == disclosure_holds.c.source_object_id
                        )
                        & (
                            current_disclosure_holds.c.hold_id
                            == disclosure_holds.c.hold_id
                        ),
                    ).outerjoin(
                        disclosure_hold_releases,
                        (
                            disclosure_hold_releases.c.workspace_id
                            == current_disclosure_holds.c.workspace_id
                        )
                        & (
                            disclosure_hold_releases.c.environment_id
                            == current_disclosure_holds.c.environment_id
                        )
                        & (
                            disclosure_hold_releases.c.source_object_id
                            == current_disclosure_holds.c.source_object_id
                        )
                        & (
                            disclosure_hold_releases.c.release_id
                            == current_disclosure_holds.c.current_release_id
                        ),
                    )
                )
                .where(
                    disclosure_holds.c.workspace_id == query.workspace_id,
                    disclosure_holds.c.environment_id == query.environment_id,
                    disclosure_holds.c.source_object_id == query.source_object_id,
                    or_(
                        current_disclosure_holds.c.state == "active",
                        disclosure_hold_releases.c.released_at > query.evaluated_at,
                    ),
                    disclosure_holds.c.source_revision_ids.any(
                        query.source_revision_id
                    ),
                    disclosure_holds.c.dependency_ids.any(query.dependency_id),
                    disclosure_holds.c.artifact_ids.any(query.artifact_id),
                    disclosure_holds.c.prohibited_effects.any(query.effect.value),
                    disclosure_holds.c.effective_from <= query.evaluated_at,
                    disclosure_holds.c.effective_until > query.evaluated_at,
                )
                .order_by(disclosure_holds.c.hold_id)
            )
            hold_ids = tuple(result.scalars().all())
            await uow.rollback()  # type: ignore[attr-defined]
        return CurrentDisclosureHolds(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            source_object_id=query.source_object_id,
            hold_ids=hold_ids,
        )


async def _hold_by_id(uow: object, hold_id: object) -> DisclosureHold | None:
    result = await uow._execute(  # type: ignore[attr-defined]
        select(disclosure_holds).where(disclosure_holds.c.hold_id == hold_id)
    )
    row = result.mappings().one_or_none()
    return None if row is None else _hold(row)


def _context(
    context: TrustedPersistenceContext,
    workspace_id: object,
    environment_id: object,
    actor_id: object | None,
) -> None:
    try:
        validate_lifecycle_context(context, workspace_id, environment_id)  # type: ignore[arg-type]
    except Exception:
        raise DisclosureHoldAuthorityError() from None
    if actor_id is not None and lifecycle_context_actor(context) != actor_id:
        raise DisclosureHoldAuthorityError()


def _hold_columns(hold: DisclosureHold) -> dict[str, object]:
    return {
        "hold_id": hold.hold_id,
        "workspace_id": hold.workspace_id,
        "environment_id": hold.environment_id,
        "source_object_id": hold.source_object_id,
        "review_decision_id": hold.review_decision_id,
        "evidence_observation_id": hold.evidence_observation_id,
        "source_revision_ids": list(hold.source_revision_ids),
        "dependency_ids": list(hold.dependency_ids),
        "artifact_ids": list(hold.artifact_ids),
        "prohibited_effects": [effect.value for effect in hold.prohibited_effects],
        "audit_event_id": hold.audit_event_id,
        "outbox_event_id": hold.outbox_event_id,
        "supersedes_hold_id": hold.supersedes_hold_id,
        "record": hold.model_dump(mode="json"),
        "effective_from": hold.effective_from,
        "effective_until": hold.effective_until,
    }


def _release_columns(release: DisclosureHoldRelease) -> dict[str, object]:
    return {
        "release_id": release.release_id,
        "workspace_id": release.workspace_id,
        "environment_id": release.environment_id,
        "source_object_id": release.source_object_id,
        "hold_id": release.hold_id,
        "audit_event_id": release.audit_event_id,
        "outbox_event_id": release.outbox_event_id,
        "superseded_by_hold_id": release.superseded_by_hold_id,
        "record": release.model_dump(mode="json"),
        "released_at": release.released_at,
    }


def _hold(row: object) -> DisclosureHold:
    return DisclosureHold.model_validate_json(json.dumps(_record(row)))


def _release(row: object) -> DisclosureHoldRelease:
    return DisclosureHoldRelease.model_validate_json(json.dumps(_record(row)))


def _record(row: object) -> object:
    try:
        return row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        return row._mapping["record"]  # type: ignore[attr-defined]


__all__ = ["PostgreSQLDisclosureHoldRepository"]
