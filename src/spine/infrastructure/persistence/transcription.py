"""PostgreSQL persistence for governed transcription jobs and artifacts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.transcription import (
    TranscriptionAdmission,
    TranscriptionConflictError,
)
from spine.domain.artifacts.transcription import (
    AudioMediaInspection,
    TranscriptSegment,
    TranscriptionArtifact,
    TranscriptionChunkReceipt,
    TranscriptionFailureProvenance,
    TranscriptionJob,
    TranscriptionJobState,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    transcript_segments,
    transcription_artifacts,
    transcription_jobs,
)


class PostgreSQLTranscriptionRepository:
    """Tenant-scoped durable store with atomic terminal artifact persistence."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        context_factory: Callable[[UUID, UUID], TrustedPersistenceContext],
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_factory = context_factory
        self._context_verifier = context_verifier

    async def admit(self, job: TranscriptionJob, *, now: Any) -> TranscriptionAdmission:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, job.workspace_id, job.environment_id)
                inserted = await session.execute(
                    postgresql_insert(transcription_jobs)
                    .values(_job_values(job))
                    .on_conflict_do_nothing()
                    .returning(transcription_jobs.c.job_id)
                )
                if inserted.scalar_one_or_none() is not None:
                    return TranscriptionAdmission(
                        job=job,
                        replay=False,
                        execution_owner=True,
                        lease_id=job.execution_lease_id,
                    )
                row = (
                    await session.execute(
                        select(transcription_jobs).where(
                            transcription_jobs.c.workspace_id == job.workspace_id,
                            transcription_jobs.c.environment_id == job.environment_id,
                            transcription_jobs.c.idempotency_key == job.idempotency_key,
                        )
                    )
                ).mappings().one_or_none()
                if row is None:
                    row = (
                        await session.execute(
                            select(transcription_jobs).where(
                                transcription_jobs.c.workspace_id == job.workspace_id,
                                transcription_jobs.c.environment_id == job.environment_id,
                                transcription_jobs.c.job_id == job.job_id,
                            )
                        )
                    ).mappings().one_or_none()
                if row is None or row["command_digest"] != job.command_digest:
                    raise TranscriptionConflictError(
                        "transcription command conflicts with history"
                    )
                current = _job_from_row(row)
                if (
                    current.state is TranscriptionJobState.PENDING
                    and current.execution_lease_expires_at <= now
                ):
                    reclaimed = current.reclaim(
                        lease_id=job.execution_lease_id,
                        lease_expires_at=job.execution_lease_expires_at,
                        now=now,
                    )
                    claimed = await session.execute(
                        update(transcription_jobs)
                        .where(
                            transcription_jobs.c.workspace_id == job.workspace_id,
                            transcription_jobs.c.environment_id == job.environment_id,
                            transcription_jobs.c.job_id == current.job_id,
                            transcription_jobs.c.state == TranscriptionJobState.PENDING.value,
                            transcription_jobs.c.execution_lease_id == current.execution_lease_id,
                        )
                        .values(
                            execution_lease_id=reclaimed.execution_lease_id,
                            execution_lease_expires_at=reclaimed.execution_lease_expires_at,
                            execution_attempt=reclaimed.execution_attempt,
                        )
                        .returning(transcription_jobs)
                    )
                    claimed_row = claimed.mappings().one_or_none()
                    if claimed_row is not None:
                        current = _job_from_row(claimed_row)
                        return TranscriptionAdmission(
                            current, True, True, current.execution_lease_id
                        )
                return TranscriptionAdmission(current, True, False, current.execution_lease_id)

    async def finish(
        self,
        *,
        job_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        state: TranscriptionJobState,
        completed_at: Any,
        artifact: TranscriptionArtifact | None,
        terminal_code: str,
        retryable: bool | None,
        lease_id: UUID,
    ) -> TranscriptionJob:
        if state is TranscriptionJobState.PENDING:
            raise TranscriptionConflictError("finish requires a terminal state")
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    await session.execute(
                        select(transcription_jobs)
                        .where(
                            transcription_jobs.c.workspace_id == workspace_id,
                            transcription_jobs.c.environment_id == environment_id,
                            transcription_jobs.c.job_id == job_id,
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise TranscriptionConflictError("transcription job is unavailable")
                current = _job_from_row(row)
                if (
                    current.state is TranscriptionJobState.PENDING
                    and current.execution_lease_id != lease_id
                ):
                    raise TranscriptionConflictError("transcription execution lease is stale")
                if (
                    current.state is TranscriptionJobState.PENDING
                    and current.cancellation_requested_at is not None
                    and state is not TranscriptionJobState.CANCELLED
                ):
                    state = TranscriptionJobState.CANCELLED
                    artifact = None
                    terminal_code = "cancelled"
                    retryable = False
                try:
                    terminal = current.terminal(
                        state=state,
                        completed_at=completed_at,
                        artifact=artifact,
                        terminal_code=terminal_code,
                        retryable=retryable,
                    )
                except ValueError as error:
                    raise TranscriptionConflictError(str(error)) from None
                if current.state is not TranscriptionJobState.PENDING:
                    return terminal
                if artifact is not None:
                    await session.execute(
                        insert(transcription_artifacts).values(_artifact_values(artifact))
                    )
                    if artifact.segments:
                        await session.execute(
                            insert(transcript_segments),
                            [_segment_values(artifact, segment) for segment in artifact.segments],
                        )
                await session.execute(
                    update(transcription_jobs)
                    .where(
                        transcription_jobs.c.workspace_id == workspace_id,
                        transcription_jobs.c.environment_id == environment_id,
                        transcription_jobs.c.job_id == job_id,
                        transcription_jobs.c.state == TranscriptionJobState.PENDING.value,
                        transcription_jobs.c.execution_lease_id == lease_id,
                    )
                    .values(
                        state=state.value,
                        artifact_id=artifact.artifact_id if artifact is not None else None,
                        terminal_code=terminal_code,
                        retryable=retryable,
                        completed_at=completed_at,
                    )
                )
                return terminal

    async def get_job(
        self, *, workspace_id: UUID, environment_id: UUID, job_id: UUID
    ) -> TranscriptionJob | None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    await session.execute(
                        select(transcription_jobs).where(
                            transcription_jobs.c.workspace_id == workspace_id,
                            transcription_jobs.c.environment_id == environment_id,
                            transcription_jobs.c.job_id == job_id,
                        )
                    )
                ).mappings().one_or_none()
                return _job_from_row(row) if row is not None else None

    async def get_artifact(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> TranscriptionArtifact | None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    await session.execute(
                        select(transcription_artifacts).where(
                            transcription_artifacts.c.workspace_id == workspace_id,
                            transcription_artifacts.c.environment_id == environment_id,
                            transcription_artifacts.c.artifact_id == artifact_id,
                        )
                    )
                ).mappings().one_or_none()
                if row is None:
                    return None
                segment_rows = (
                    await session.execute(
                        select(transcript_segments)
                        .where(
                            transcript_segments.c.workspace_id == workspace_id,
                            transcript_segments.c.environment_id == environment_id,
                            transcript_segments.c.artifact_id == artifact_id,
                        )
                        .order_by(transcript_segments.c.ordinal)
                    )
                ).mappings().all()
                return _artifact_from_rows(row, segment_rows)

    async def request_cancellation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        job_id: UUID,
        requested_at: Any,
    ) -> TranscriptionJob:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                row = (
                    await session.execute(
                        select(transcription_jobs)
                        .where(
                            transcription_jobs.c.workspace_id == workspace_id,
                            transcription_jobs.c.environment_id == environment_id,
                            transcription_jobs.c.job_id == job_id,
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise TranscriptionConflictError("transcription job is unavailable")
                current = _job_from_row(row)
                updated = current.request_cancellation(requested_at=requested_at)
                if updated == current:
                    return current
                await session.execute(
                    update(transcription_jobs)
                    .where(
                        transcription_jobs.c.workspace_id == workspace_id,
                        transcription_jobs.c.environment_id == environment_id,
                        transcription_jobs.c.job_id == job_id,
                        transcription_jobs.c.state == TranscriptionJobState.PENDING.value,
                        transcription_jobs.c.cancellation_requested_at.is_(None),
                    )
                    .values(cancellation_requested_at=requested_at)
                )
                return updated

    async def _bind_scope(
        self, session: Any, workspace_id: UUID, environment_id: UUID
    ) -> None:
        try:
            context = self._context_verifier.verify(
                self._context_factory(workspace_id, environment_id)
            )
        except Exception:
            raise InvalidPersistenceContextError(
                "Transcription persistence context is invalid."
            ) from None
        if (
            not isinstance(context.scope, EnvironmentScope)
            or context.scope.workspace_id != workspace_id
            or context.scope.environment_id != environment_id
        ):
            raise InvalidPersistenceContextError(
                "Transcription persistence scope does not match its tenant."
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


def _job_values(job: TranscriptionJob) -> dict[str, Any]:
    return {
        "workspace_id": job.workspace_id,
        "environment_id": job.environment_id,
        "job_id": job.job_id,
        "schema_version": job.schema_version,
        "source_object_id": job.source_object_id,
        "source_revision_id": job.source_revision_id,
        "source_revision_digest": job.source_revision_digest,
        "original_sha256": job.original_sha256,
        "processing_profile_reference": job.processing_profile_reference,
        "adapter_reference": job.adapter_reference,
        "model_config_reference": job.model_config_reference,
        "authorization_decision_id": job.authorization_decision_id,
        "authorization_policy_reference": job.authorization_policy_reference,
        "idempotency_key": job.idempotency_key,
        "command_digest": job.command_digest,
        "execution_lease_id": job.execution_lease_id,
        "execution_lease_expires_at": job.execution_lease_expires_at,
        "execution_attempt": job.execution_attempt,
        "state": job.state.value,
        "artifact_id": job.artifact_id,
        "terminal_code": job.terminal_code,
        "retryable": job.retryable,
        "cancellation_requested_at": job.cancellation_requested_at,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
    }


def _job_from_row(row: Any) -> TranscriptionJob:
    return TranscriptionJob(
        schema_version=row["schema_version"],
        job_id=row["job_id"],
        workspace_id=row["workspace_id"],
        environment_id=row["environment_id"],
        source_object_id=row["source_object_id"],
        source_revision_id=row["source_revision_id"],
        source_revision_digest=row["source_revision_digest"],
        original_sha256=row["original_sha256"],
        processing_profile_reference=row["processing_profile_reference"],
        adapter_reference=row["adapter_reference"],
        model_config_reference=row["model_config_reference"],
        authorization_decision_id=row["authorization_decision_id"],
        authorization_policy_reference=row["authorization_policy_reference"],
        idempotency_key=row["idempotency_key"],
        command_digest=row["command_digest"],
        execution_lease_id=row["execution_lease_id"],
        execution_lease_expires_at=row["execution_lease_expires_at"],
        execution_attempt=row["execution_attempt"],
        state=row["state"],
        artifact_id=row["artifact_id"],
        terminal_code=row["terminal_code"],
        retryable=row["retryable"],
        cancellation_requested_at=row["cancellation_requested_at"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _artifact_values(artifact: TranscriptionArtifact) -> dict[str, Any]:
    return {
        "workspace_id": artifact.workspace_id,
        "environment_id": artifact.environment_id,
        "artifact_id": artifact.artifact_id,
        "artifact_digest": artifact.artifact_digest,
        "source_object_id": artifact.source_object_id,
        "source_revision_id": artifact.source_revision_id,
        "source_revision_digest": artifact.source_revision_digest,
        "original_sha256": artifact.original_sha256,
        "processing_job_id": artifact.processing_job_id,
        "processing_profile_reference": artifact.processing_profile_reference,
        "adapter_reference": artifact.adapter_reference,
        "model_config_reference": artifact.model_config_reference,
        "recording_duration_ms": artifact.recording_duration_ms,
        "language": artifact.language,
        "media_inspection": (
            artifact.media_inspection.model_dump(mode="json")
            if artifact.media_inspection is not None
            else None
        ),
        "decoder_reference": artifact.decoder_reference,
        "decoded_sha256": artifact.decoded_sha256,
        "reassembly_reference": artifact.reassembly_reference,
        "chunk_receipts": [
            receipt.model_dump(mode="json") for receipt in artifact.chunk_receipts
        ],
        "outcome": artifact.outcome.value,
        "failure": (
            artifact.failure.model_dump(mode="json")
            if artifact.failure is not None
            else None
        ),
        "schema_version": artifact.schema_version,
        "created_at": artifact.created_at,
    }


def _segment_values(
    artifact: TranscriptionArtifact, segment: TranscriptSegment
) -> dict[str, Any]:
    return {
        "workspace_id": artifact.workspace_id,
        "environment_id": artifact.environment_id,
        "segment_id": segment.segment_id,
        "artifact_id": artifact.artifact_id,
        "ordinal": segment.ordinal,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.text,
        "anonymous_speaker_label": segment.anonymous_speaker_label,
        "signals": [signal.model_dump(mode="json") for signal in segment.signals],
        "schema_version": segment.schema_version,
    }


def _artifact_from_rows(row: Any, segment_rows: list[Any]) -> TranscriptionArtifact:
    return TranscriptionArtifact(
        artifact_id=row["artifact_id"],
        artifact_digest=row["artifact_digest"],
        workspace_id=row["workspace_id"],
        environment_id=row["environment_id"],
        source_object_id=row["source_object_id"],
        source_revision_id=row["source_revision_id"],
        source_revision_digest=row["source_revision_digest"],
        original_sha256=row["original_sha256"],
        processing_job_id=row["processing_job_id"],
        processing_profile_reference=row["processing_profile_reference"],
        adapter_reference=row["adapter_reference"],
        model_config_reference=row["model_config_reference"],
        recording_duration_ms=row["recording_duration_ms"],
        language=row["language"],
        media_inspection=(
            AudioMediaInspection.model_validate(row["media_inspection"])
            if row["media_inspection"] is not None
            else None
        ),
        decoder_reference=row["decoder_reference"],
        decoded_sha256=row["decoded_sha256"],
        reassembly_reference=row["reassembly_reference"],
        chunk_receipts=tuple(
            TranscriptionChunkReceipt.model_validate(item)
            for item in row["chunk_receipts"]
        ),
        outcome=row["outcome"],
        failure=(
            TranscriptionFailureProvenance.model_validate(row["failure"])
            if row["failure"] is not None
            else None
        ),
        segments=tuple(
            TranscriptSegment(
                segment_id=item["segment_id"],
                ordinal=item["ordinal"],
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                text=item["text"],
                anonymous_speaker_label=item["anonymous_speaker_label"],
                signals=tuple(item["signals"]),
                schema_version=item["schema_version"],
            )
            for item in segment_rows
        ),
        schema_version=row["schema_version"],
        created_at=row["created_at"],
    )


__all__ = ["PostgreSQLTranscriptionRepository"]
