"""Tenant-scoped append-only persistence for email attachment originals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from spine.application.email_attachments import (
    EmailAttachmentConflict,
    EmailAttachmentPersistenceResult,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.domain.artifacts.email_attachment import EmailAttachmentArtifact
from spine.infrastructure.persistence.postgresql_mappings import email_attachment_artifacts


class InMemoryEmailAttachmentRepository:
    """Atomic fake repository for deterministic offline application tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[tuple[UUID, UUID, UUID], EmailAttachmentArtifact] = {}

    async def persist_many(
        self, artifacts: tuple[EmailAttachmentArtifact, ...]
    ) -> tuple[EmailAttachmentPersistenceResult, ...]:
        async with self._lock:
            results: list[EmailAttachmentPersistenceResult] = []
            pending: dict[tuple[UUID, UUID, UUID], EmailAttachmentArtifact] = {}
            for artifact in artifacts:
                key = (artifact.workspace_id, artifact.environment_id, artifact.artifact_id)
                prior = self._records.get(key) or pending.get(key)
                if prior is not None:
                    if prior.artifact_sha256 != artifact.artifact_sha256:
                        raise EmailAttachmentConflict()
                    results.append(EmailAttachmentPersistenceResult(prior, replay=True))
                else:
                    pending[key] = artifact
                    results.append(EmailAttachmentPersistenceResult(artifact, replay=False))
            self._records.update(pending)
            return tuple(results)

    async def persist(
        self, artifact: EmailAttachmentArtifact
    ) -> EmailAttachmentPersistenceResult:
        return (await self.persist_many((artifact,)))[0]

    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailAttachmentArtifact | None:
        async with self._lock:
            return self._records.get((workspace_id, environment_id, artifact_id))

    async def list_for_revision(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID
    ) -> tuple[EmailAttachmentArtifact, ...]:
        async with self._lock:
            return tuple(
                artifact for (workspace, environment, _), artifact in self._records.items()
                if (workspace, environment) == (workspace_id, environment_id)
                and artifact.source_revision_id == source_revision_id
            )


class PostgreSQLEmailAttachmentRepository:
    """Append-only JSONB records with trusted context and tenant row scope."""

    def __init__(
        self, session_factory: Callable[[], Any], *,
        context_factory: Callable[[UUID, UUID], TrustedPersistenceContext],
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_factory = context_factory
        self._context_verifier = context_verifier

    async def persist_many(
        self, artifacts: tuple[EmailAttachmentArtifact, ...]
    ) -> tuple[EmailAttachmentPersistenceResult, ...]:
        if not artifacts:
            return ()
        tenant = (artifacts[0].workspace_id, artifacts[0].environment_id)
        if any((item.workspace_id, item.environment_id) != tenant for item in artifacts):
            raise EmailAttachmentConflict()
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_tenant_scope(session, *tenant)
                results: list[EmailAttachmentPersistenceResult] = []
                for artifact in artifacts:
                    inserted = await session.execute(
                        insert(email_attachment_artifacts)
                        .values(
                            workspace_id=artifact.workspace_id,
                            environment_id=artifact.environment_id,
                            artifact_id=artifact.artifact_id,
                            source_object_id=artifact.source_object_id,
                            source_revision_id=artifact.source_revision_id,
                            canonical_acceptance_id=artifact.canonical_acceptance_id,
                            mime_part_path=artifact.mime_part_path,
                            content_sha256=artifact.content_sha256,
                            extraction_profile=artifact.extraction_profile,
                            artifact_digest=artifact.artifact_sha256,
                            record=artifact.model_dump(mode="json"),
                            created_at=datetime.now(timezone.utc),
                        )
                        .on_conflict_do_nothing()
                        .returning(email_attachment_artifacts.c.artifact_id)
                    )
                    if inserted.scalar_one_or_none() is not None:
                        results.append(EmailAttachmentPersistenceResult(artifact, replay=False))
                        continue
                    conflict = (await session.execute(select(email_attachment_artifacts.c.record).where(
                        email_attachment_artifacts.c.workspace_id == artifact.workspace_id,
                        email_attachment_artifacts.c.environment_id == artifact.environment_id,
                        email_attachment_artifacts.c.source_revision_id == artifact.source_revision_id,
                        email_attachment_artifacts.c.mime_part_path == artifact.mime_part_path,
                        email_attachment_artifacts.c.extraction_profile == artifact.extraction_profile,
                    ))).scalar_one_or_none()
                    if conflict is None:
                        raise EmailAttachmentConflict()
                    prior = EmailAttachmentArtifact.model_validate_json(json.dumps(conflict))
                    if prior.artifact_id != artifact.artifact_id or prior.artifact_sha256 != artifact.artifact_sha256:
                        raise EmailAttachmentConflict()
                    results.append(EmailAttachmentPersistenceResult(prior, replay=True))
                return tuple(results)

    async def persist(
        self, artifact: EmailAttachmentArtifact
    ) -> EmailAttachmentPersistenceResult:
        return (await self.persist_many((artifact,)))[0]

    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailAttachmentArtifact | None:
        async with self._session_factory() as session:
            await self._bind_tenant_scope(session, workspace_id, environment_id)
            row = (await session.execute(select(email_attachment_artifacts.c.record).where(
                email_attachment_artifacts.c.workspace_id == workspace_id,
                email_attachment_artifacts.c.environment_id == environment_id,
                email_attachment_artifacts.c.artifact_id == artifact_id,
            ))).scalar_one_or_none()
            return EmailAttachmentArtifact.model_validate_json(json.dumps(row)) if row is not None else None

    async def list_for_revision(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID
    ) -> tuple[EmailAttachmentArtifact, ...]:
        async with self._session_factory() as session:
            await self._bind_tenant_scope(session, workspace_id, environment_id)
            rows = (await session.execute(select(email_attachment_artifacts.c.record).where(
                email_attachment_artifacts.c.workspace_id == workspace_id,
                email_attachment_artifacts.c.environment_id == environment_id,
                email_attachment_artifacts.c.source_revision_id == source_revision_id,
            ).order_by(email_attachment_artifacts.c.mime_part_path))).scalars().all()
            return tuple(
                EmailAttachmentArtifact.model_validate_json(json.dumps(row)) for row in rows
            )

    async def _bind_tenant_scope(self, session: Any, workspace_id: UUID, environment_id: UUID) -> None:
        try:
            context = self._context_verifier.verify(self._context_factory(workspace_id, environment_id))
        except Exception:
            raise InvalidPersistenceContextError("Email attachment persistence context is invalid.") from None
        if not isinstance(context.scope, EnvironmentScope) or (
            context.scope.workspace_id, context.scope.environment_id
        ) != (workspace_id, environment_id):
            raise InvalidPersistenceContextError("Email attachment persistence scope does not match its tenant.")
        await session.execute(text(
            "SELECT set_config('spine.workspace_id', :workspace_id, true), "
            "set_config('spine.environment_id', :environment_id, true), "
            "set_config('spine.acting_subject_id', :acting_subject_id, true), "
            "set_config('spine.service_principal_id', :service_principal_id, true), "
            "set_config('spine.purpose', :purpose, true), "
            "set_config('spine.operation', :operation, true), "
            "set_config('spine.trace_id', :trace_id, true)"
        ), {
            "workspace_id": str(workspace_id), "environment_id": str(environment_id),
            "acting_subject_id": str(context.acting_subject_id or ""),
            "service_principal_id": str(context.service_principal_id or ""),
            "purpose": context.purpose.value, "operation": context.operation.value,
            "trace_id": str(context.trace_id),
        })
