"""Tenant-scoped persistence for immutable processed email artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from spine.application.email_artifacts import (
    AcceptedEmailParent,
    EmailArtifactCommand,
    EmailArtifactConflict,
    EmailArtifactPersistenceResult,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.application.storage.authorized_read import (
    OriginalReadLifecycle,
    OriginalReadRetention,
    OriginalReadTargetResolver,
)
from spine.domain.sources import AdmissionDisposition
from spine.domain.artifacts.email_message import EmailMessageArtifact
from spine.infrastructure.persistence.postgresql_mappings import (
    email_message_artifacts,
    outbox_intents,
)


class AuthorizedReadEmailParentReader:
    """Resolve immutable source metadata from the authorized-read authority."""

    def __init__(self, resolver: OriginalReadTargetResolver) -> None:
        self._resolver = resolver

    async def resolve_accepted_email_parent(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID,
    ) -> AcceptedEmailParent | None:
        try:
            target = await self._resolver.resolve_original_read_target(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_revision_id=source_revision_id,
            )
        except Exception:
            return None
        if target is None or (
            target.workspace_id != workspace_id
            or target.environment_id != environment_id
            or target.source_object_id != source_object_id
            or target.source_revision_id != source_revision_id
            or target.current_revision_id != source_revision_id
            or target.lifecycle is not OriginalReadLifecycle.CURRENT
            or target.retention is not OriginalReadRetention.AVAILABLE
            or target.disposition is not AdmissionDisposition.ADMITTED
            or target.revision.media_type != "message/rfc822"
            or target.revision.original_sha256 is None
            or target.revision.byte_length is None
        ):
            return None
        return AcceptedEmailParent(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            source_revision_digest=target.revision.revision_digest,
            original_sha256=target.association.original_sha256,
            raw_byte_length=target.association.byte_length,
        )


class InMemoryEmailArtifactRepository:
    """Atomic, tenant-scoped repository for offline application compositions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[tuple[UUID, UUID, UUID], EmailMessageArtifact] = {}
        self._commands: dict[tuple[UUID, UUID, str], tuple[str, UUID]] = {}
        self._intents: dict[UUID, object] = {}

    async def persist(self, command: EmailArtifactCommand) -> EmailArtifactPersistenceResult:
        artifact = command.artifact
        tenant = (artifact.workspace_id, artifact.environment_id)
        async with self._lock:
            prior_command = self._commands.get((*tenant, command.command_identity))
            if prior_command is not None:
                digest, artifact_id = prior_command
                if digest != command.command_digest:
                    raise EmailArtifactConflict()
                prior = self._records.get((*tenant, artifact_id))
                if prior is None or prior.artifact_sha256 != artifact.artifact_sha256:
                    raise EmailArtifactConflict()
                return EmailArtifactPersistenceResult(
                    prior, command.projection_intent_id, replay=True
                )
            key = (*tenant, artifact.artifact_id)
            prior = self._records.get(key)
            if prior is not None:
                if prior.artifact_sha256 != artifact.artifact_sha256:
                    raise EmailArtifactConflict()
                self._commands[(*tenant, command.command_identity)] = (command.command_digest, artifact.artifact_id)
                return EmailArtifactPersistenceResult(
                    prior, command.projection_intent_id, replay=True
                )
            existing_intent = self._intents.get(command.projection_intent_id)
            if existing_intent is not None and existing_intent != command.projection_intent:
                raise EmailArtifactConflict()
            self._records[key] = artifact
            self._commands[(*tenant, command.command_identity)] = (command.command_digest, artifact.artifact_id)
            self._intents[command.projection_intent_id] = command.projection_intent
            return EmailArtifactPersistenceResult(
                artifact, command.projection_intent_id, replay=False
            )

    async def list_for_revision(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID
    ) -> tuple[EmailMessageArtifact, ...]:
        async with self._lock:
            return tuple(
                value for (workspace, environment, _), value in self._records.items()
                if workspace == workspace_id and environment == environment_id
                and value.source_revision_id == source_revision_id
            )

    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailMessageArtifact | None:
        async with self._lock:
            return self._records.get((workspace_id, environment_id, artifact_id))

    async def projection_intent_count(self) -> int:
        async with self._lock:
            return len(self._intents)


class PostgreSQLEmailArtifactRepository:
    """Append-only JSONB artifact persistence guarded by verified tenant context."""

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

    async def persist(self, command: EmailArtifactCommand) -> EmailArtifactPersistenceResult:
        artifact = command.artifact
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_tenant_scope(session, artifact.workspace_id, artifact.environment_id)
                existing = (await session.execute(select(email_message_artifacts).where(
                    email_message_artifacts.c.workspace_id == artifact.workspace_id,
                    email_message_artifacts.c.environment_id == artifact.environment_id,
                    email_message_artifacts.c.command_identity == command.command_identity,
                ))).mappings().one_or_none()
                if existing is not None:
                    if existing["command_digest"] != command.command_digest:
                        raise EmailArtifactConflict()
                    prior = EmailMessageArtifact.model_validate(existing["record"])
                    if prior.artifact_sha256 != artifact.artifact_sha256:
                        raise EmailArtifactConflict()
                    return EmailArtifactPersistenceResult(
                        prior, command.projection_intent_id, replay=True
                    )
                inserted = await session.execute(
                    insert(email_message_artifacts)
                    .values(
                        workspace_id=artifact.workspace_id,
                        environment_id=artifact.environment_id,
                        artifact_id=artifact.artifact_id,
                        source_object_id=artifact.source_object_id,
                        source_revision_id=artifact.source_revision_id,
                        canonical_acceptance_id=artifact.canonical_acceptance_id,
                        artifact_digest=artifact.artifact_sha256,
                        command_identity=command.command_identity,
                        command_digest=command.command_digest,
                        record=artifact.model_dump(mode="json"),
                        created_at=datetime.now(timezone.utc),
                    )
                    .on_conflict_do_nothing()
                    .returning(email_message_artifacts.c.artifact_id)
                )
                if inserted.scalar_one_or_none() is not None:
                    intent = command.projection_intent
                    aggregate = intent.aggregate
                    await session.execute(insert(outbox_intents).values(
                        event_id=intent.event_id,
                        workspace_id=intent.workspace_id,
                        environment_id=intent.environment_id,
                        event_type=intent.event_type,
                        event_schema_version=intent.schema_version,
                        aggregate_type=aggregate.object_type if aggregate else None,
                        aggregate_id=aggregate.object_id if aggregate else None,
                        aggregate_schema_version=aggregate.schema_version if aggregate else None,
                        producer_deduplication_id=intent.producer_deduplication_id,
                        payload=intent.payload_json(),
                        trace_id=intent.trace_id,
                        correlation_id=intent.correlation_id,
                        causation_id=intent.causation_id,
                    ))
                    return EmailArtifactPersistenceResult(
                        artifact, intent.event_id, replay=False
                    )
                conflict = (await session.execute(select(email_message_artifacts).where(
                    email_message_artifacts.c.workspace_id == artifact.workspace_id,
                    email_message_artifacts.c.environment_id == artifact.environment_id,
                    email_message_artifacts.c.command_identity == command.command_identity,
                ))).mappings().one_or_none()
                if conflict is None or conflict["command_digest"] != command.command_digest:
                    raise EmailArtifactConflict()
                prior = EmailMessageArtifact.model_validate(conflict["record"])
                if prior.artifact_sha256 != artifact.artifact_sha256:
                    raise EmailArtifactConflict()
                return EmailArtifactPersistenceResult(
                    prior, command.projection_intent_id, replay=True
                )

    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailMessageArtifact | None:
        async with self._session_factory() as session:
            await self._bind_tenant_scope(session, workspace_id, environment_id)
            row = (await session.execute(select(email_message_artifacts.c.record).where(
                email_message_artifacts.c.workspace_id == workspace_id,
                email_message_artifacts.c.environment_id == environment_id,
                email_message_artifacts.c.artifact_id == artifact_id,
            ))).scalar_one_or_none()
            return EmailMessageArtifact.model_validate(row) if row is not None else None

    async def _bind_tenant_scope(self, session: Any, workspace_id: UUID, environment_id: UUID) -> None:
        try:
            context = self._context_verifier.verify(self._context_factory(workspace_id, environment_id))
        except Exception:
            raise InvalidPersistenceContextError("Email artifact persistence context is invalid.") from None
        if not isinstance(context.scope, EnvironmentScope) or (
            context.scope.workspace_id, context.scope.environment_id
        ) != (workspace_id, environment_id):
            raise InvalidPersistenceContextError("Email artifact persistence scope does not match its tenant.")
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
