"""PostgreSQL adapter for the governed transcript publication ledger."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.transcript_publication import (
    TranscriptArtifactRepository,
    TranscriptPublicationRepository,
)
from spine.domain.artifacts import (
    TranscriptPublication,
    TranscriptPublicationState,
    TranscriptProjectionSegment,
    TranscriptProjectionState,
    TranscriptionArtifact,
)
from spine.infrastructure.persistence.postgresql_mappings import transcript_publications


class PostgreSQLTranscriptPublicationRepository(TranscriptPublicationRepository):
    """Tenant-scoped persistence for immutable projection payloads and state."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        artifact_repository: TranscriptArtifactRepository,
        context_factory: Callable[[UUID, UUID], TrustedPersistenceContext],
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_repository = artifact_repository
        self._context_factory = context_factory
        self._context_verifier = context_verifier

    async def get_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptionArtifact | None:
        return await self._artifact_repository.get_artifact(
            workspace_id=workspace_id,
            environment_id=environment_id,
            artifact_id=artifact_id,
        )

    async def create_or_replay(
        self, publication: TranscriptPublication
    ) -> tuple[TranscriptPublication, bool]:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(
                    session, publication.workspace_id, publication.environment_id
                )
                inserted = await session.execute(
                    postgresql_insert(transcript_publications)
                    .values(_publication_values(publication))
                    .on_conflict_do_nothing()
                    .returning(transcript_publications.c.publication_id)
                )
                if inserted.scalar_one_or_none() is not None:
                    return publication, False
                row = (
                    await session.execute(
                        select(transcript_publications).where(
                            transcript_publications.c.workspace_id == publication.workspace_id,
                            transcript_publications.c.environment_id == publication.environment_id,
                            transcript_publications.c.transcript_artifact_id
                            == publication.transcript_artifact_id,
                            transcript_publications.c.is_current.is_(True),
                        )
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise ValueError("transcript publication conflict is unavailable")
                existing = _publication_from_row(row)
                if existing.publication_digest != publication.publication_digest:
                    raise ValueError("transcript publication conflicts with history")
                return existing, True

    async def get_publication(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        transcript_artifact_id: UUID,
    ) -> TranscriptPublication | None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    await session.execute(
                        select(transcript_publications).where(
                            transcript_publications.c.workspace_id == workspace_id,
                            transcript_publications.c.environment_id == environment_id,
                            transcript_publications.c.transcript_artifact_id
                            == transcript_artifact_id,
                            transcript_publications.c.is_current.is_(True),
                        )
                    )
                ).mappings().one_or_none()
                return _publication_from_row(row) if row is not None else None

    async def transition(
        self,
        *,
        publication_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        state: TranscriptPublicationState | None = None,
        projection_state: TranscriptProjectionState | None = None,
    ) -> TranscriptPublication:
        return await self.transition_in_scope(
            context=self._context_factory(workspace_id, environment_id),
            publication_id=publication_id,
            state=state,
            projection_state=projection_state,
        )

    async def transition_in_scope(
        self,
        *,
        context: TrustedPersistenceContext,
        publication_id: UUID,
        state: TranscriptPublicationState | None = None,
        projection_state: TranscriptProjectionState | None = None,
    ) -> TranscriptPublication:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidPersistenceContextError("transcript publication scope is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(
                    session, context.scope.workspace_id, context.scope.environment_id
                )
                row = (
                    await session.execute(
                        select(transcript_publications)
                        .where(
                            transcript_publications.c.workspace_id == context.scope.workspace_id,
                            transcript_publications.c.environment_id == context.scope.environment_id,
                            transcript_publications.c.publication_id == publication_id,
                            transcript_publications.c.is_current.is_(True),
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise KeyError("transcript publication is unavailable")
                current = _publication_from_row(row)
                updated = current.successor_state(
                    state=state or current.state,
                    projection_state=projection_state or current.projection_state,
                )
                changed = await session.execute(
                    update(transcript_publications)
                    .where(
                        transcript_publications.c.workspace_id == context.scope.workspace_id,
                        transcript_publications.c.environment_id == context.scope.environment_id,
                        transcript_publications.c.publication_id == publication_id,
                        transcript_publications.c.is_current.is_(True),
                    )
                    .values(is_current=False)
                )
                if changed.rowcount != 1:
                    raise ValueError("transcript publication transition lost its owner")
                await session.execute(
                    transcript_publications.insert().values(_publication_values(updated))
                )
                return updated

    async def rebuild_projection(
        self,
        *,
        publication_id: UUID,
        projection_version: str,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> TranscriptPublication:
        return await self._replace_with_successor(
            publication_id=publication_id,
            projection_version=projection_version,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )

    async def rollback_projection(
        self,
        *,
        publication_id: UUID,
        projection_version: str,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> TranscriptPublication:
        return await self._replace_with_successor(
            publication_id=publication_id,
            projection_version=projection_version,
            workspace_id=workspace_id,
            environment_id=environment_id,
            rollback=True,
        )

    async def _replace_with_successor(
        self,
        *,
        publication_id: UUID,
        projection_version: str,
        workspace_id: UUID,
        environment_id: UUID,
        rollback: bool = False,
    ) -> TranscriptPublication:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    await session.execute(
                        select(transcript_publications).where(
                            transcript_publications.c.workspace_id == workspace_id,
                            transcript_publications.c.environment_id == environment_id,
                            transcript_publications.c.publication_id == publication_id,
                            transcript_publications.c.is_current.is_(True),
                        ).with_for_update()
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise KeyError("transcript publication is unavailable")
                current = _publication_from_row(row)
                source = current
                if rollback:
                    source_row = (
                        await session.execute(
                            select(transcript_publications)
                            .where(
                                transcript_publications.c.workspace_id == workspace_id,
                                transcript_publications.c.environment_id == environment_id,
                                transcript_publications.c.transcript_artifact_id
                                == current.transcript_artifact_id,
                                transcript_publications.c.projection_version
                                == projection_version,
                            )
                            .order_by(transcript_publications.c.published_at.desc())
                            .limit(1)
                        )
                    ).mappings().one_or_none()
                    if source_row is None:
                        raise KeyError("transcript projection version is unavailable")
                    source = _publication_from_row(source_row)
                superseded = current.successor_state(
                    state=TranscriptPublicationState.SUPERSEDED,
                    projection_state=current.projection_state,
                )
                successor = source.successor_projection(
                    projection_version=projection_version,
                    predecessor_publication_id=superseded.publication_id,
                )
                changed = await session.execute(
                    update(transcript_publications)
                    .where(
                        transcript_publications.c.workspace_id == current.workspace_id,
                        transcript_publications.c.environment_id == current.environment_id,
                        transcript_publications.c.publication_id == publication_id,
                        transcript_publications.c.is_current.is_(True),
                    )
                    .values(is_current=False)
                )
                if changed.rowcount != 1:
                    raise ValueError("transcript projection replacement lost its owner")
                await session.execute(
                    transcript_publications.insert().values(
                        _publication_values(superseded, is_current=False)
                    )
                )
                await session.execute(
                    transcript_publications.insert().values(_publication_values(successor))
                )
                return successor

    async def _bind_scope(
        self, session: Any, workspace_id: UUID, environment_id: UUID
    ) -> None:
        try:
            context = self._context_verifier.verify(
                self._context_factory(workspace_id, environment_id)
            )
        except Exception:
            raise InvalidPersistenceContextError(
                "Transcript publication persistence context is invalid."
            ) from None
        if (
            not isinstance(context.scope, EnvironmentScope)
            or context.scope.workspace_id != workspace_id
            or context.scope.environment_id != environment_id
        ):
            raise InvalidPersistenceContextError(
                "Transcript publication persistence scope does not match its tenant."
            )
        await session.execute(
            text(
                "SELECT "
                "set_config('spine.workspace_id', :workspace_id, true), "
                "set_config('spine.environment_id', :environment_id, true), "
                "set_config('spine.acting_subject_id', :acting_subject_id, true), "
                "set_config('spine.service_principal_id', :service_principal_id, true), "
                "set_config('spine.purpose', :purpose, true), "
                "set_config('spine.operation', :operation, true), "
                "set_config('spine.trace_id', :trace_id, true)"
            ),
            {
                "workspace_id": str(context.scope.workspace_id),
                "environment_id": str(context.scope.environment_id),
                "acting_subject_id": str(context.acting_subject_id or ""),
                "service_principal_id": str(context.service_principal_id or ""),
                "purpose": context.purpose.value,
                "operation": context.operation.value,
                "trace_id": str(context.trace_id),
            },
        )


def _publication_values(
    publication: TranscriptPublication, *, is_current: bool = True
) -> dict[str, Any]:
    return {
        "workspace_id": publication.workspace_id,
        "environment_id": publication.environment_id,
        "publication_id": publication.publication_id,
        "is_current": is_current,
        "publication_digest": publication.publication_digest,
        "predecessor_publication_id": publication.predecessor_publication_id,
        "source_object_id": publication.source_object_id,
        "source_revision_id": publication.source_revision_id,
        "source_revision_digest": publication.source_revision_digest,
        "transcript_artifact_id": publication.transcript_artifact_id,
        "transcript_artifact_digest": publication.transcript_artifact_digest,
        "transcript_artifact_schema_version": publication.transcript_artifact_schema_version,
        "policy_reference": publication.policy_reference,
        "approval_id": publication.approval_id,
        "partial_allowed": publication.partial_allowed,
        "projection_version": publication.projection_version,
        "projection_state": publication.projection_state.value,
        "state": publication.state.value,
        "segments": [segment.model_dump(mode="json") for segment in publication.segments],
        "published_at": publication.published_at,
    }


def _publication_from_row(row: Any) -> TranscriptPublication:
    return TranscriptPublication.model_validate(
        {
            "schema_version": "transcript-publication:v1",
            "publication_id": row["publication_id"],
            "publication_digest": row["publication_digest"],
            "predecessor_publication_id": row["predecessor_publication_id"],
            "workspace_id": row["workspace_id"],
            "environment_id": row["environment_id"],
            "source_object_id": row["source_object_id"],
            "source_revision_id": row["source_revision_id"],
            "source_revision_digest": row["source_revision_digest"],
            "transcript_artifact_id": row["transcript_artifact_id"],
            "transcript_artifact_digest": row["transcript_artifact_digest"],
            "transcript_artifact_schema_version": row["transcript_artifact_schema_version"],
            "policy_reference": row["policy_reference"],
            "approval_id": row["approval_id"],
            "partial_allowed": row["partial_allowed"],
            "projection_version": row["projection_version"],
            "projection_state": row["projection_state"],
            "state": row["state"],
            "segments": tuple(
                TranscriptProjectionSegment.model_validate(item) for item in row["segments"]
            ),
            "published_at": row["published_at"],
        }
    )


__all__ = ["PostgreSQLTranscriptPublicationRepository"]
