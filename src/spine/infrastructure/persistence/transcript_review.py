"""PostgreSQL append-only storage for transcript reviews and corrections."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.transcript_review import (
    TranscriptReviewConflictError,
    TranscriptReviewRepository,
    TranscriptReviewReplayConflictError,
)
from spine.domain.artifacts import (
    CorrectedTranscriptArtifact,
    TranscriptReviewRecord,
    TranscriptionArtifact,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    corrected_transcript_artifacts,
    transcript_reviews,
)
from spine.infrastructure.persistence.transcription import (
    PostgreSQLTranscriptionRepository,
)


class PostgreSQLTranscriptReviewRepository(TranscriptReviewRepository):
    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        artifact_repository: PostgreSQLTranscriptionRepository,
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

    async def get_review(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptReviewRecord | None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    (
                        await session.execute(
                            select(transcript_reviews).where(
                                transcript_reviews.c.workspace_id == workspace_id,
                                transcript_reviews.c.environment_id == environment_id,
                                transcript_reviews.c.input_artifact_id == artifact_id,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        return _review_from_row(row) if row is not None else None

    async def get_corrected_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> CorrectedTranscriptArtifact | None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    await session.execute(
                        select(corrected_transcript_artifacts.c.artifact).where(
                            corrected_transcript_artifacts.c.workspace_id == workspace_id,
                            corrected_transcript_artifacts.c.environment_id == environment_id,
                            corrected_transcript_artifacts.c.artifact_id == artifact_id,
                        )
                    )
                ).scalar_one_or_none()
        return CorrectedTranscriptArtifact.model_validate(row) if row is not None else None

    async def create_or_replay(
        self,
        *,
        review: TranscriptReviewRecord,
        corrected_artifact: CorrectedTranscriptArtifact | None,
        command_digest: str,
    ) -> tuple[TranscriptReviewRecord, CorrectedTranscriptArtifact | None, bool]:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(
                    session, review.workspace_id, review.environment_id
                )
                by_key = (
                    (
                        await session.execute(
                            select(transcript_reviews)
                            .where(
                                transcript_reviews.c.workspace_id
                                == review.workspace_id,
                                transcript_reviews.c.environment_id
                                == review.environment_id,
                                transcript_reviews.c.actor_id == review.actor_id,
                                transcript_reviews.c.idempotency_key
                                == review.idempotency_key,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if by_key is not None:
                    if by_key["command_digest"] != command_digest:
                        raise TranscriptReviewReplayConflictError(
                            "review idempotency key conflicts"
                        )
                    return await self._replay(session, by_key)

                inserted = await session.execute(
                    postgresql_insert(transcript_reviews)
                    .values(_review_values(review, command_digest=command_digest))
                    .on_conflict_do_nothing()
                    .returning(transcript_reviews.c.review_id)
                )
                if inserted.scalar_one_or_none() is None:
                    by_key = (
                        (
                            await session.execute(
                                select(transcript_reviews).where(
                                    transcript_reviews.c.workspace_id
                                    == review.workspace_id,
                                    transcript_reviews.c.environment_id
                                    == review.environment_id,
                                    transcript_reviews.c.actor_id == review.actor_id,
                                    transcript_reviews.c.idempotency_key
                                    == review.idempotency_key,
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if by_key is not None:
                        if by_key["command_digest"] != command_digest:
                            raise TranscriptReviewReplayConflictError(
                                "review idempotency key conflicts"
                            )
                        return await self._replay(session, by_key)
                    raise TranscriptReviewConflictError(
                        "transcript artifact has already been reviewed"
                    )

                if corrected_artifact is not None:
                    await session.execute(
                        postgresql_insert(corrected_transcript_artifacts).values(
                            _corrected_values(corrected_artifact)
                        )
                    )
                return review, corrected_artifact, False

    async def _replay(self, session: Any, review_row: Any):
        review = _review_from_row(review_row)
        corrected = None
        if review.corrected_artifact_id is not None:
            row = (
                (
                    await session.execute(
                        select(corrected_transcript_artifacts).where(
                            corrected_transcript_artifacts.c.workspace_id
                            == review.workspace_id,
                            corrected_transcript_artifacts.c.environment_id
                            == review.environment_id,
                            corrected_transcript_artifacts.c.artifact_id
                            == review.corrected_artifact_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise RuntimeError(
                    "corrected transcript review artifact is unavailable"
                )
            corrected = CorrectedTranscriptArtifact.model_validate(row["artifact"])
        return review, corrected, True

    async def _bind_scope(
        self, session: Any, workspace_id: UUID, environment_id: UUID
    ) -> None:
        context = self._context_factory(workspace_id, environment_id)
        self._context_verifier.verify(context)
        if (
            not isinstance(context.scope, EnvironmentScope)
            or context.scope.workspace_id != workspace_id
            or context.scope.environment_id != environment_id
        ):
            raise InvalidPersistenceContextError(
                "Transcript review persistence scope is invalid."
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


def _review_values(
    review: TranscriptReviewRecord, *, command_digest: str
) -> dict[str, Any]:
    return {
        "workspace_id": review.workspace_id,
        "environment_id": review.environment_id,
        "review_id": review.review_id,
        "review_digest": review.review_digest,
        "idempotency_key": review.idempotency_key,
        "command_digest": command_digest,
        "source_object_id": review.source_object_id,
        "source_revision_id": review.source_revision_id,
        "source_revision_digest": review.source_revision_digest,
        "input_artifact_id": review.input_artifact_id,
        "input_artifact_digest": review.input_artifact_digest,
        "input_artifact_schema_version": review.input_artifact_schema_version,
        "decision": review.decision.value,
        "rationale": review.rationale,
        "actor_id": review.actor_id,
        "actor_authority": review.actor_authority,
        "policy_reference": review.policy_reference,
        "publication_eligible": review.publication_eligible,
        "correlation_id": review.correlation_id,
        "corrected_artifact_id": review.corrected_artifact_id,
        "decided_at": review.decided_at,
    }


def _review_from_row(row: Any) -> TranscriptReviewRecord:
    return TranscriptReviewRecord.model_validate(
        {
            key: row[key]
            for key in (
                "review_id",
                "review_digest",
                "idempotency_key",
                "workspace_id",
                "environment_id",
                "source_object_id",
                "source_revision_id",
                "source_revision_digest",
                "input_artifact_id",
                "input_artifact_digest",
                "input_artifact_schema_version",
                "decision",
                "rationale",
                "actor_id",
                "actor_authority",
                "policy_reference",
                "publication_eligible",
                "correlation_id",
                "corrected_artifact_id",
                "decided_at",
            )
        }
    )


def _corrected_values(artifact: CorrectedTranscriptArtifact) -> dict[str, Any]:
    return {
        "workspace_id": artifact.workspace_id,
        "environment_id": artifact.environment_id,
        "artifact_id": artifact.artifact_id,
        "artifact_digest": artifact.artifact_digest,
        "original_artifact_id": artifact.original_artifact_id,
        "review_id": artifact.review_id,
        "artifact": artifact.model_dump(mode="json"),
        "created_at": artifact.created_at,
    }


__all__ = ["PostgreSQLTranscriptReviewRepository"]
