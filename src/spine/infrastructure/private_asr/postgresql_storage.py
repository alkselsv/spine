"""Restricted PostgreSQL storage for private ASR source and report artifacts.

All bytes, manifests, receipts, tombstones and source fences share PostgreSQL's
transaction and RLS scope. No filesystem path or public URL is created.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Index, LargeBinary, MetaData, String, Table, Text, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from spine.application.persistence.context import EnvironmentScope, TrustedContextVerifier, TrustedPersistenceContext
from spine.application.persistence.retry import RetryEligibility, run_with_transaction_retry
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.private_asr_corpus import (
    AccessPolicyPort,
    AdmittedSourceRevision,
    AdmissionAuthorityPort,
    PrivateArtifactDeletionAuthority,
    PrivateArtifactDeletionAuthorization,
    PrivateArtifactDeletionRequest,
    PrivateArtifactSourceLineage,
    PrivateAggregatePublicationAuthorityPort,
    PrivateASRManifestLineageVerifier,
    ScopedAuditReceiptPort,
    PrivateReportReceipt,
    PrivateReportSourceBinding,
)
from spine.domain.evaluations.private_asr_corpus import (
    CorpusSplit,
    PrivateReference,
    SanitizedCorpusReport,
)
from spine.domain.sources.models import SourceObject
from spine.domain.sources.retention import RetentionArtifactClass
from spine.application.retention import RetentionStartingEvent
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events, canonical_generation_counters, current_access_policies,
    current_lifecycle_authorities, current_source_heads, lifecycle_decisions,
    source_objects, source_revisions,
)
from spine.infrastructure.persistence.postgresql_source_locks import (
    lock_admission_revision,
    lock_source_lifecycle_shared,
)
from spine.infrastructure.private_asr.retention import PostgreSQLPrivateASRRetentionResolver


_SCHEMA = "spine"
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_RETENTION_CLASS_BY_KIND = {
    "staging": RetentionArtifactClass.PRIVATE_ASR_STAGING,
    "original": RetentionArtifactClass.PRIVATE_ASR_SOURCE_ORIGINAL,
    "annotation": RetentionArtifactClass.PRIVATE_ASR_ANNOTATION,
    "report": RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
}
_ASR_METADATA = MetaData(schema=_SCHEMA)
private_asr_artifacts = Table(
    "private_asr_artifacts", _ASR_METADATA,
    Column("artifact_id", PGUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PGUUID(as_uuid=True), primary_key=True),
    Column("environment_id", PGUUID(as_uuid=True), primary_key=True),
    Column("purpose", Text, nullable=False), Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False), Column("report_id", PGUUID(as_uuid=True)),
    Column("idempotency_key", Text), Column("source_object_id", PGUUID(as_uuid=True)),
    Column("source_revision_id", PGUUID(as_uuid=True)), Column("source_revision_digest", String(64)),
    Column("content_sha256", String(64)), Column("byte_length", BigInteger, nullable=False),
    Column("payload", LargeBinary, nullable=False),
    Column("retention_profile_ref", PGUUID(as_uuid=True), nullable=False),
    Column("retention_disposition", JSONB),
    Column("audit_receipt_ref", PGUUID(as_uuid=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("dataset_id", PGUUID(as_uuid=True)), Column("manifest_version", Text),
    Column("manifest_sha256", String(64)),
    Column("admitted_record", JSONB), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("tombstoned_at", DateTime(timezone=True)),
)
private_asr_source_fences = Table(
    "private_asr_source_fences", _ASR_METADATA,
    Column("workspace_id", PGUUID(as_uuid=True), primary_key=True),
    Column("environment_id", PGUUID(as_uuid=True), primary_key=True),
    Column("purpose", Text, nullable=False),
    Column("source_revision_id", PGUUID(as_uuid=True), primary_key=True),
    Column("revoked", Boolean, nullable=False), Column("epoch", BigInteger, nullable=False),
    Column("reason", Text, nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False),
)
private_asr_report_sources = Table(
    "private_asr_report_sources", _ASR_METADATA,
    Column("workspace_id", PGUUID(as_uuid=True), primary_key=True),
    Column("environment_id", PGUUID(as_uuid=True), primary_key=True),
    Column("purpose", Text, nullable=False),
    Column("report_id", PGUUID(as_uuid=True), primary_key=True),
    Column("source_record_id", PGUUID(as_uuid=True), primary_key=True),
    Column("source_revision_id", PGUUID(as_uuid=True), nullable=False),
    Column("source_object_id", PGUUID(as_uuid=True)),
    Column("source_revision_digest", String(64)), Column("split", Text),
)
private_asr_aggregate_exports = Table(
    "private_asr_aggregate_exports", _ASR_METADATA,
    Column("export_id", PGUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PGUUID(as_uuid=True), primary_key=True),
    Column("environment_id", PGUUID(as_uuid=True), primary_key=True),
    Column("purpose", Text, nullable=False),
    Column("report_artifact_id", PGUUID(as_uuid=True), nullable=False),
    Column("report_id", PGUUID(as_uuid=True), nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("privacy_policy_version", Text, nullable=False),
    Column("privacy_policy_approval_version", Text, nullable=False),
    Column("retention_profile_ref", PGUUID(as_uuid=True), nullable=False),
    Column("retention_disposition", JSONB, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("audit_receipt_id", PGUUID(as_uuid=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "uq_private_asr_aggregate_exports_idempotency",
    private_asr_aggregate_exports.c.workspace_id,
    private_asr_aggregate_exports.c.environment_id,
    private_asr_aggregate_exports.c.idempotency_key,
    unique=True,
)


class PrivateASRStorageDenied(Exception):
    """Generic denial that never includes private source metadata."""


def _retention_class_for_kind(kind: str) -> RetentionArtifactClass | None:
    return _RETENTION_CLASS_BY_KIND.get(kind)


def _retention_starting_event_for_kind(kind: str) -> RetentionStartingEvent:
    return (
        RetentionStartingEvent.REPORT_GENERATED
        if kind == "report"
        else RetentionStartingEvent.ARTIFACT_CREATED
    )


def _admitted_from_jsonb(value: object) -> AdmittedSourceRevision:
    try:
        return AdmittedSourceRevision.model_validate_json(json.dumps(value))
    except Exception:
        raise PrivateASRStorageDenied("Private ASR storage metadata is invalid.") from None


class PostgreSQLPrivateASRStorage:
    """Scoped #771/#772 adapter backed by one PostgreSQL database.

    The session factory must use the restricted runtime role. Each operation
    verifies trusted context and sets tenant-local RLS variables before querying.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        contexts: TrustedContextVerifier,
        authority: AdmissionAuthorityPort,
        access_policies: AccessPolicyPort,
        audit_receipts: ScopedAuditReceiptPort,
        retention_resolver: PostgreSQLPrivateASRRetentionResolver | None = None,
        manifest_lineage: PrivateASRManifestLineageVerifier | None = None,
        deletion_authority: PrivateArtifactDeletionAuthority | None = None,
        aggregate_authority: PrivateAggregatePublicationAuthorityPort | None = None,
        uow_factory: UnitOfWorkFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        max_payload_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_payload_bytes <= 0:
            raise ValueError("storage limit must be positive")
        self._sessions = sessions
        self._contexts = contexts
        self._authority = authority
        self._access_policies = access_policies
        self._audit_receipts = audit_receipts
        self._retention_resolver = retention_resolver
        self._manifest_lineage = manifest_lineage
        self._deletion_authority = deletion_authority
        self._aggregate_authority = aggregate_authority
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory
        self._max_payload_bytes = max_payload_bytes

    def _context(self, context: TrustedPersistenceContext) -> TrustedPersistenceContext:
        try:
            context.validate_shape()
            trusted = self._contexts.verify(context)
            trusted.validate_shape()
            if not isinstance(trusted.scope, EnvironmentScope):
                raise ValueError
            if trusted.purpose.value != "asr_model_evaluation":
                raise ValueError
            return trusted
        except Exception:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.") from None

    async def _execute(self, transaction: object, statement: object, parameters: object | None = None):
        execute_statement = getattr(transaction, "execute_statement", None)
        if execute_statement is not None:
            return await execute_statement(statement, parameters)
        return await transaction.execute(statement, parameters)  # type: ignore[attr-defined,no-any-return]

    async def _set_scope(self, session: object, context: TrustedPersistenceContext) -> None:
        scope = context.scope
        await self._execute(session, text("select set_config('spine.workspace_id', :workspace_id, true)"),
                            {"workspace_id": str(scope.workspace_id)})
        await self._execute(session, text("select set_config('spine.environment_id', :environment_id, true)"),
                            {"environment_id": str(scope.environment_id)})
        await self._execute(session, text("select set_config('spine.purpose', :purpose, true)"),
                            {"purpose": context.purpose.value})

    def _scope(self, context: TrustedPersistenceContext) -> tuple[UUID, UUID]:
        trusted = self._context(context)
        assert isinstance(trusted.scope, EnvironmentScope)
        return trusted.scope.workspace_id, trusted.scope.environment_id

    def _require_worker_operation(
        self, context: TrustedPersistenceContext, expected: str
    ) -> tuple[UUID, UUID]:
        trusted = self._context(context)
        if trusted.operation.value != expected or trusted.service_principal_id is None:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        return self._scope(trusted)

    async def _check_admitted(
        self, admitted: AdmittedSourceRevision, context: TrustedPersistenceContext
    ) -> tuple[UUID, UUID]:
        trusted = self._context(context)
        workspace_id, environment_id = self._scope(trusted)
        actor_id = trusted.acting_subject_id or trusted.service_principal_id
        source, revision = admitted.source_object, admitted.source_revision
        if (
            (workspace_id, environment_id) != (admitted.workspace_id, admitted.environment_id)
            or (source.workspace_id, source.environment_id) != (workspace_id, environment_id)
            or (revision.workspace_id, revision.environment_id) != (workspace_id, environment_id)
            or source.source_object_id != revision.source_object_id
            or actor_id != admitted.actor_id
            or admitted.authority.purpose != "asr_model_evaluation"
            or not admitted.authority.current or admitted.authority.revoked
            or self._clock() >= admitted.authority.expires_at
        ):
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        try:
            current = await self._authority.recheck(admitted, evaluated_at=self._clock())
            policy = await self._access_policies.current_for(source, context=context)
            if (
                not current
                or policy.access_policy_version_id != admitted.access_policy_version
                or policy.source_object_id != source.source_object_id
                or policy.workspace_id != workspace_id
                or policy.environment_id != environment_id
            ):
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        except PrivateASRStorageDenied:
            raise
        except Exception:
            raise PrivateASRStorageDenied("Private ASR storage authorization unavailable.") from None
        return workspace_id, environment_id

    async def _current_retention(
        self, transaction: object, profile_ref: PrivateReference, *, context: TrustedPersistenceContext,
        admitted: AdmittedSourceRevision, artifact_class: RetentionArtifactClass,
        starting_event: RetentionStartingEvent, starting_at: datetime,
        allow_expired: bool = False,
    ):
        if self._retention_resolver is None:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        return await self._retention_resolver.resolve(
            transaction, context=context, profile_ref=profile_ref,
            source=admitted.source_object, revision=admitted.source_revision,
            artifact_class=artifact_class, starting_event=starting_event,
            starting_at=starting_at, evaluated_at=self._clock(),
            allow_expired=allow_expired,
        )

    async def _lock_source_admission_path(
        self,
        session: object,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        revision_id: UUID,
    ) -> None:
        await self._lock_source_admission_paths(
            session,
            ((workspace_id, environment_id, source_object_id, revision_id),),
        )

    async def _lock_source_admission_paths(
        self,
        session: object,
        source_paths: tuple[tuple[UUID, UUID, UUID, UUID], ...],
    ) -> None:
        """Acquire lifecycle, generation, and exact-revision locks in canonical order."""
        if not source_paths:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        scopes = {(workspace_id, environment_id) for workspace_id, environment_id, _, _ in source_paths}
        if len(scopes) != 1:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        workspace_id, environment_id = next(iter(scopes))
        source_ids = sorted({source_id for _, _, source_id, _ in source_paths}, key=str)
        execute = lambda statement, parameters=None: self._execute(session, statement, parameters)
        for source_object_id in source_ids:
            await lock_source_lifecycle_shared(
                execute,
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
            )
        counter = (await self._execute(session, select(
            canonical_generation_counters.c.workspace_id,
        ).where(
            canonical_generation_counters.c.workspace_id == workspace_id,
            canonical_generation_counters.c.environment_id == environment_id,
        ).with_for_update())).scalar_one_or_none()
        if counter is None:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        for _, _, source_object_id, revision_id in sorted(
            set(source_paths), key=lambda item: (str(item[2]), str(item[3])),
        ):
            await lock_admission_revision(
                execute,
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
                revision_id=revision_id,
            )

    async def _locked_source(
        self, session: object, admitted: AdmittedSourceRevision, *,
        retention_profile_ref: PrivateReference | None = None,
        artifact_class: RetentionArtifactClass | None = None,
        starting_event: RetentionStartingEvent | None = None,
        starting_at: datetime | None = None,
        retention_allow_expired: bool = False,
        context: TrustedPersistenceContext | None = None,
        admission_path_locked: bool = False,
    ):
        source = admitted.source_object
        revision = admitted.source_revision
        if not admission_path_locked:
            await self._lock_source_admission_path(
                session,
                workspace_id=source.workspace_id,
                environment_id=source.environment_id,
                source_object_id=source.source_object_id,
                revision_id=revision.revision_id,
            )
        source_result = await self._execute(session, select(source_objects.c.source_object_id).where(
            source_objects.c.workspace_id == source.workspace_id,
            source_objects.c.environment_id == source.environment_id,
            source_objects.c.source_object_id == source.source_object_id,
        ))
        revision_result = await self._execute(session, select(
            source_revisions.c.revision_id,
        ).where(
            source_revisions.c.workspace_id == source.workspace_id,
            source_revisions.c.environment_id == source.environment_id,
            source_revisions.c.source_object_id == source.source_object_id,
            source_revisions.c.revision_id == revision.revision_id,
            source_revisions.c.kind == "content",
            source_revisions.c.revision_digest == revision.revision_digest,
            source_revisions.c.original_sha256 == revision.original_sha256,
            source_revisions.c.byte_length == revision.byte_length,
        ))
        head = (await self._execute(session, select(
            current_source_heads.c.current_revision_id,
            current_source_heads.c.current_revision_kind,
        ).where(
            current_source_heads.c.workspace_id == source.workspace_id,
            current_source_heads.c.environment_id == source.environment_id,
            current_source_heads.c.source_object_id == source.source_object_id,
        ).with_for_update())).mappings().one_or_none()
        lifecycle = (await self._execute(session, select(
            lifecycle_decisions.c.status,
        ).select_from(current_lifecycle_authorities.join(
            lifecycle_decisions,
            (lifecycle_decisions.c.workspace_id == current_lifecycle_authorities.c.workspace_id)
            & (lifecycle_decisions.c.environment_id == current_lifecycle_authorities.c.environment_id)
            & (lifecycle_decisions.c.source_object_id == current_lifecycle_authorities.c.source_object_id)
            & (lifecycle_decisions.c.decision_id == current_lifecycle_authorities.c.current_decision_id)
            & (lifecycle_decisions.c.generation == current_lifecycle_authorities.c.generation),
        )).where(
            current_lifecycle_authorities.c.workspace_id == source.workspace_id,
            current_lifecycle_authorities.c.environment_id == source.environment_id,
            current_lifecycle_authorities.c.source_object_id == source.source_object_id,
        ).with_for_update(of=current_lifecycle_authorities))).scalar_one_or_none()
        retention_disposition = None
        if retention_profile_ref is not None:
            if context is None or artifact_class is None or starting_event is None or starting_at is None:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            retention_disposition = await self._current_retention(
                session, retention_profile_ref, context=context, admitted=admitted,
                artifact_class=artifact_class, starting_event=starting_event,
                starting_at=starting_at, allow_expired=retention_allow_expired,
            )
        policy_result = await self._execute(session, select(current_access_policies.c.access_policy_version_id).where(
            current_access_policies.c.workspace_id == source.workspace_id,
            current_access_policies.c.environment_id == source.environment_id,
            current_access_policies.c.source_object_id == source.source_object_id,
        ).with_for_update())
        current_policy_id = policy_result.scalar_one_or_none()
        if (
            source_result.scalar_one_or_none() is None
            or revision_result.scalar_one_or_none() is None
            or head is None
            or head["current_revision_id"] != revision.revision_id
            or head["current_revision_kind"] != "content"
            or lifecycle not in (None, "eligible")
            or current_policy_id != admitted.access_policy_version
        ):
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        fence = (await self._execute(session, select(private_asr_source_fences).where(
            private_asr_source_fences.c.workspace_id == source.workspace_id,
            private_asr_source_fences.c.environment_id == source.environment_id,
            private_asr_source_fences.c.source_revision_id == admitted.source_revision.revision_id,
        ).with_for_update())).mappings().one_or_none()
        if fence is not None and fence["revoked"]:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        return retention_disposition

    def _ref(self, artifact_id: UUID, kind: str) -> PrivateReference:
        return PrivateReference(reference_id=artifact_id, reference_kind=f"private_asr_{kind}")

    async def begin(self, admitted: AdmittedSourceRevision, *, context: TrustedPersistenceContext) -> PrivateReference:
        workspace_id, environment_id = await self._check_admitted(admitted, context)
        revision = admitted.source_revision
        artifact_id = uuid5(NAMESPACE_URL, f"spine-private-asr-staging:v1:{workspace_id}:{environment_id}:{revision.revision_id}:{revision.revision_digest}")
        async with self._sessions() as session, session.begin():
            await self._set_scope(session, context)
            disposition = await self._locked_source(
                session, admitted, retention_profile_ref=admitted.retention_profile_ref,
                artifact_class=RetentionArtifactClass.PRIVATE_ASR_STAGING,
                starting_event=RetentionStartingEvent.ARTIFACT_CREATED,
                starting_at=self._clock(), context=context,
            )
            await self._check_admitted(admitted, context)
            assert disposition is not None
            retention_expires_at = disposition.expires_at
            existing = (await session.execute(select(private_asr_artifacts).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == artifact_id,
            ).with_for_update())).mappings().one_or_none()
            if existing is None:
                await session.execute(insert(private_asr_artifacts).values(
                    artifact_id=artifact_id, workspace_id=workspace_id, environment_id=environment_id,
                    purpose="asr_model_evaluation", kind="staging", status="staging",
                    source_object_id=admitted.source_object.source_object_id,
                    source_revision_id=revision.revision_id,
                    source_revision_digest=revision.revision_digest, byte_length=0, payload=b"",
                    retention_profile_ref=admitted.retention_profile_ref.reference_id,
                    audit_receipt_ref=admitted.audit_receipt_ref.reference_id,
                    expires_at=retention_expires_at,
                    admitted_record=admitted.model_dump(mode="json"), created_at=self._clock(),
                ))
            elif (
                existing["expires_at"] <= self._clock()
                or existing["status"] == "tombstoned"
                or existing["kind"] != "staging"
                or existing["purpose"] != "asr_model_evaluation"
                or existing["source_object_id"] != admitted.source_object.source_object_id
                or existing["source_revision_id"] != revision.revision_id
                or existing["source_revision_digest"] != revision.revision_digest
                or existing["retention_profile_ref"] != admitted.retention_profile_ref.reference_id
            ):
                raise PrivateASRStorageDenied("Private ASR storage unavailable.")
            else:
                # A retry restarts the source stream from byte zero. Clear both
                # interrupted and finalized bytes so at-least-once retries never
                # concatenate a second copy onto the first attempt.
                await session.execute(update(private_asr_artifacts).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.artifact_id == artifact_id,
                ).values(status="staging", payload=b"", byte_length=0,
                         content_sha256=None, expires_at=retention_expires_at))
        return self._ref(artifact_id, "staging")

    async def append(self, staging_ref: PrivateReference, chunk: bytes, *, context: TrustedPersistenceContext) -> None:
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > self._max_payload_bytes:
            raise PrivateASRStorageDenied("Private ASR storage write rejected.")
        workspace_id, environment_id = self._scope(context)
        async with self._sessions() as session, session.begin():
            await self._set_scope(session, context)
            row = (await session.execute(select(
                private_asr_artifacts.c.admitted_record,
                private_asr_artifacts.c.expires_at,
                private_asr_artifacts.c.source_revision_id,
                private_asr_artifacts.c.source_revision_digest,
                private_asr_artifacts.c.content_sha256,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == staging_ref.reference_id,
                private_asr_artifacts.c.kind == "staging",
                private_asr_artifacts.c.status == "staging",
                private_asr_artifacts.c.tombstoned_at.is_(None),
                private_asr_artifacts.c.expires_at > self._clock(),
            ).with_for_update())).mappings().one_or_none()
            if row is None or staging_ref.reference_kind != "private_asr_staging":
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            if row["content_sha256"] is not None:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            admitted = _admitted_from_jsonb(row["admitted_record"])
            await self._check_admitted(admitted, context)
            await self._locked_source(
                session, admitted, retention_profile_ref=admitted.retention_profile_ref,
                artifact_class=RetentionArtifactClass.PRIVATE_ASR_STAGING,
                starting_event=RetentionStartingEvent.ARTIFACT_CREATED,
                starting_at=self._clock(), context=context,
            )
            await self._check_admitted(admitted, context)
            if (row["source_revision_id"] != admitted.source_revision.revision_id
                or row["source_revision_digest"] != admitted.source_revision.revision_digest):
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            result = await session.execute(update(private_asr_artifacts).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == staging_ref.reference_id,
                private_asr_artifacts.c.byte_length <= self._max_payload_bytes - len(chunk),
            ).values(payload=private_asr_artifacts.c.payload.op("||")(chunk),
                     byte_length=private_asr_artifacts.c.byte_length + len(chunk)))
            if result.rowcount != 1:
                raise PrivateASRStorageDenied("Private ASR storage limit exceeded.")

    async def finalize(self, staging_ref: PrivateReference, *, sha256: str, byte_length: int,
                       context: TrustedPersistenceContext) -> PrivateReference:
        workspace_id, environment_id = self._scope(context)
        async with self._sessions() as session, session.begin():
            await self._set_scope(session, context)
            row = (await session.execute(select(
                private_asr_artifacts.c.admitted_record,
                private_asr_artifacts.c.expires_at,
                private_asr_artifacts.c.byte_length,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == staging_ref.reference_id,
                private_asr_artifacts.c.kind == "staging",
                private_asr_artifacts.c.status == "staging",
                private_asr_artifacts.c.tombstoned_at.is_(None),
                private_asr_artifacts.c.expires_at > self._clock(),
            ).with_for_update())).mappings().one_or_none()
            if row is None or staging_ref.reference_kind != "private_asr_staging":
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            admitted = _admitted_from_jsonb(row["admitted_record"])
            await self._check_admitted(admitted, context)
            # Finalization and revocation share the revision advisory lock. This
            # prevents staging from becoming committed after its source is fenced.
            await self._locked_source(
                session, admitted, retention_profile_ref=admitted.retention_profile_ref,
                artifact_class=RetentionArtifactClass.PRIVATE_ASR_STAGING,
                starting_event=RetentionStartingEvent.ARTIFACT_CREATED,
                starting_at=self._clock(), context=context,
            )
            await self._check_admitted(admitted, context)
            if sha256 != admitted.source_revision.original_sha256:
                raise PrivateASRStorageDenied("Private ASR content verification failed.")
            payload_result = await session.execute(select(private_asr_artifacts.c.payload).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == staging_ref.reference_id,
            ))
            payload = bytes(payload_result.scalar_one())
            if len(payload) != byte_length or byte_length != row["byte_length"] or hashlib.sha256(payload).hexdigest() != sha256:
                raise PrivateASRStorageDenied("Private ASR content verification failed.")
            await session.execute(update(private_asr_artifacts).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == staging_ref.reference_id,
            ).values(content_sha256=sha256))
        return self._ref(staging_ref.reference_id, "staging")

    async def discard(self, staging_ref: PrivateReference, *, context: TrustedPersistenceContext) -> None:
        await self.delete_private_reference(staging_ref, context=context)

    async def bind_immutable_original(self, admitted: AdmittedSourceRevision, *, sha256: str,
                                      byte_length: int, context: TrustedPersistenceContext) -> PrivateReference:
        workspace_id, environment_id = await self._check_admitted(admitted, context)
        revision = admitted.source_revision
        if sha256 != revision.original_sha256:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        original_id = uuid5(NAMESPACE_URL, f"spine-private-asr-original:v1:{workspace_id}:{environment_id}:{revision.revision_id}:{sha256}")
        async with self._sessions() as session, session.begin():
            await self._set_scope(session, context)
            retention_disposition = await self._locked_source(
                session, admitted, retention_profile_ref=admitted.retention_profile_ref,
                artifact_class=RetentionArtifactClass.PRIVATE_ASR_SOURCE_ORIGINAL,
                starting_event=RetentionStartingEvent.ARTIFACT_CREATED,
                starting_at=self._clock(), context=context,
            )
            await self._check_admitted(admitted, context)
            assert retention_disposition is not None
            retention_expires_at = retention_disposition.expires_at
            audit = (await session.execute(select(audit_events.c.audit_event_id).where(
                audit_events.c.workspace_id == workspace_id,
                audit_events.c.environment_id == environment_id,
                audit_events.c.audit_event_id == admitted.audit_receipt_ref.reference_id,
            ))).scalar_one_or_none()
            if audit is None:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            existing = (await session.execute(select(private_asr_artifacts.c.artifact_id).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == original_id,
                private_asr_artifacts.c.status == "committed",
            ))).scalar_one_or_none()
            if existing is None:
                source = (await session.execute(select(private_asr_artifacts).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.source_revision_id == revision.revision_id,
                    private_asr_artifacts.c.source_revision_digest == revision.revision_digest,
                    private_asr_artifacts.c.kind == "staging",
                    private_asr_artifacts.c.status == "staging",
                    private_asr_artifacts.c.content_sha256 == sha256,
                    private_asr_artifacts.c.byte_length == byte_length,
                    private_asr_artifacts.c.tombstoned_at.is_(None),
                ).with_for_update())).mappings().first()
                if source is None or hashlib.sha256(bytes(source["payload"])).hexdigest() != sha256:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                await session.execute(insert(private_asr_artifacts).values(
                    artifact_id=original_id, workspace_id=workspace_id, environment_id=environment_id,
                    purpose="asr_model_evaluation", kind="original", status="committed",
                    source_object_id=admitted.source_object.source_object_id,
                    source_revision_id=revision.revision_id, source_revision_digest=revision.revision_digest,
                    content_sha256=sha256, byte_length=byte_length, payload=bytes(source["payload"]),
                    retention_profile_ref=admitted.retention_profile_ref.reference_id,
                    audit_receipt_ref=admitted.audit_receipt_ref.reference_id,
                    expires_at=retention_expires_at,
                    admitted_record=admitted.model_dump(mode="json"), created_at=self._clock(),
                ))
        return self._ref(original_id, "original")

    async def store_private_report(self, payload: bytes, *, report_id: UUID, idempotency_key: str,
                                   source_bindings: tuple[PrivateReportSourceBinding, ...],
                                   retention_profile_ref: PrivateReference,
                                   audit_receipt_ref: PrivateReference, context: TrustedPersistenceContext,
                                   authorize_commit: Callable[[], Any]) -> PrivateReportReceipt:
        """Retry a reproducible report transaction after canonical PG aborts.

        Every attempt creates a fresh UoW and re-resolves source, policy,
        retention, manifest, and commit authorization. The caller's idempotency
        key keeps publication identity stable across attempts.
        """
        if not idempotency_key:
            raise PrivateASRStorageDenied("Private ASR report rejected.")

        async def attempt(_: int) -> PrivateReportReceipt:
            return await self._store_private_report_once(
                payload,
                report_id=report_id,
                idempotency_key=idempotency_key,
                source_bindings=source_bindings,
                retention_profile_ref=retention_profile_ref,
                audit_receipt_ref=audit_receipt_ref,
                context=context,
                authorize_commit=authorize_commit,
            )

        return await run_with_transaction_retry(
            attempt,
            eligibility=RetryEligibility(
                idempotent=True,
                has_idempotency_key=True,
                reproducible=True,
            ),
        )

    async def _store_private_report_once(self, payload: bytes, *, report_id: UUID, idempotency_key: str,
                                   source_bindings: tuple[PrivateReportSourceBinding, ...],
                                   retention_profile_ref: PrivateReference,
                                   audit_receipt_ref: PrivateReference, context: TrustedPersistenceContext,
                                   authorize_commit: Callable[[], Any]) -> PrivateReportReceipt:
        from spine.domain.evaluations.private_asr_qualification import PrivateRealASREvaluationReport
        workspace_id, environment_id = self._scope(context)
        if self._uow_factory is None:
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
        if not payload or len(payload) > self._max_payload_bytes:
            raise PrivateASRStorageDenied("Private ASR report rejected.")
        try:
            report = PrivateRealASREvaluationReport.model_validate_json(payload)
        except Exception:
            raise PrivateASRStorageDenied("Private ASR report rejected.") from None
        if (report.report_id != report_id or report.workspace_id != workspace_id
            or report.environment_id != environment_id or report.retention_profile_ref != retention_profile_ref
            or report.audit_receipt_ref != audit_receipt_ref or not idempotency_key
            or len(idempotency_key) > 256):
            raise PrivateASRStorageDenied("Private ASR report rejected.")
        if (
            len({item.record_id for item in source_bindings}) != len(source_bindings)
            or {item.record_id for item in source_bindings}
            != {item.record_ref.reference_id for item in report.outcomes}
            or any(
                item.split != next(
                    outcome.split for outcome in report.outcomes
                    if outcome.record_ref.reference_id == item.record_id
                )
                for item in source_bindings
            )
            or any(item.dataset_id != report.dataset_id for item in source_bindings)
        ):
            raise PrivateASRStorageDenied("Private ASR report lineage rejected.")
        if self._manifest_lineage is None:
            raise PrivateASRStorageDenied("Private ASR report lineage rejected.")
        source_revision_ids = tuple(item.source_revision_id for item in source_bindings)
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = uuid5(NAMESPACE_URL, f"spine-private-asr-report:v1:{workspace_id}:{environment_id}:{idempotency_key}")
        now = self._clock()
        async with self._uow_factory(context) as session:
            await self._set_scope(session, context)
            await self._execute(session, select(func.pg_advisory_xact_lock(func.hashtextextended(
                f"report:{workspace_id}:{environment_id}:{idempotency_key}", 0
            ))))
            # Lock the approved source and policy pointers before the callback.
            admitted_records: list[AdmittedSourceRevision] = []
            retention_expiries: list[datetime] = []
            retention_dispositions: list[dict[str, object]] = []
            originals_by_revision: dict[UUID, Any] = {}
            admitted_by_revision: dict[UUID, AdmittedSourceRevision] = {}
            for revision_id in sorted(set(source_revision_ids), key=str):
                original = (await self._execute(session, select(
                    private_asr_artifacts.c.retention_profile_ref,
                    private_asr_artifacts.c.expires_at,
                    private_asr_artifacts.c.admitted_record,
                    private_asr_artifacts.c.source_object_id,
                    private_asr_artifacts.c.source_revision_digest,
                ).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.source_revision_id == revision_id,
                    private_asr_artifacts.c.kind == "original",
                    private_asr_artifacts.c.status == "committed",
                    private_asr_artifacts.c.tombstoned_at.is_(None),
                    private_asr_artifacts.c.expires_at > now,
                ))).mappings().first()
                if original is None or original["retention_profile_ref"] != retention_profile_ref.reference_id:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                admitted = _admitted_from_jsonb(original["admitted_record"])
                if (
                    admitted.source_object.workspace_id != workspace_id
                    or admitted.source_object.environment_id != environment_id
                    or admitted.source_revision.revision_id != revision_id
                ):
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                originals_by_revision[revision_id] = original
                admitted_by_revision[revision_id] = admitted

            if not source_revision_ids:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            await self._lock_source_admission_paths(
                session,
                tuple(
                    (
                        workspace_id,
                        environment_id,
                        admitted.source_object.source_object_id,
                        admitted.source_revision.revision_id,
                    )
                    for admitted in admitted_by_revision.values()
                ),
            )
            ordered_admissions = sorted(
                admitted_by_revision.values(),
                key=lambda item: (
                    str(item.source_object.source_object_id),
                    str(item.source_revision.revision_id),
                ),
            )
            for admitted in ordered_admissions:
                original = originals_by_revision[admitted.source_revision.revision_id]
                retention_disposition = await self._locked_source(
                    session, admitted, retention_profile_ref=retention_profile_ref,
                    artifact_class=RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
                    starting_event=RetentionStartingEvent.REPORT_GENERATED,
                    starting_at=report.generated_at, context=context,
                    admission_path_locked=True,
                )
                await self._check_admitted(admitted, context)
                if admitted.retention_profile_ref != retention_profile_ref:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                matching_bindings = tuple(
                    item for item in source_bindings
                    if item.source_revision_id == admitted.source_revision.revision_id
                )
                if any(
                    item.source_object_id != admitted.source_object.source_object_id
                    or item.source_revision_digest != admitted.source_revision.revision_digest
                    or original["source_object_id"] != item.source_object_id
                    or original["source_revision_digest"] != item.source_revision_digest
                    for item in matching_bindings
                ) or not matching_bindings:
                    raise PrivateASRStorageDenied("Private ASR report lineage rejected.")
                assert retention_disposition is not None
                retention_expiry = retention_disposition.expires_at
                if retention_expiry <= now:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                retention_expiries.append(retention_expiry)
                retention_dispositions.append({
                    "source_revision_id": str(admitted.source_revision.revision_id),
                    "disposition": retention_disposition.model_dump(mode="json"),
                })
                admitted_records.append(admitted)
            retention_dispositions.sort(key=lambda item: str(item["source_revision_id"]))
            if not await self._manifest_lineage.verify_current(
                transaction=session,
                context=context,
                dataset_id=report.dataset_id,
                manifest_version=report.manifest_version,
                manifest_sha256=report.manifest_sha256,
                retention_profile_ref=retention_profile_ref,
                source_bindings=source_bindings,
            ):
                raise PrivateASRStorageDenied("Private ASR report lineage rejected.")
            existing = (await self._execute(session, select(
                private_asr_artifacts.c.status,
                private_asr_artifacts.c.content_sha256,
                private_asr_artifacts.c.expires_at,
                private_asr_artifacts.c.retention_disposition,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == artifact_id,
            ).with_for_update())).mappings().one_or_none()
            if existing is not None:
                if (
                    existing["status"] != "committed"
                    or existing["content_sha256"] != digest
                    or existing["expires_at"] != min(retention_expiries)
                    or existing["retention_disposition"] != retention_dispositions
                ):
                    raise PrivateASRStorageDenied("Private ASR idempotency conflict.")
                linked = {
                    (
                        row["source_record_id"], row["source_revision_id"],
                        row["source_object_id"], row["source_revision_digest"], row["split"],
                    )
                    for row in (await self._execute(session, select(
                        private_asr_report_sources.c.source_record_id,
                        private_asr_report_sources.c.source_revision_id,
                        private_asr_report_sources.c.source_object_id,
                        private_asr_report_sources.c.source_revision_digest,
                        private_asr_report_sources.c.split,
                    ).where(
                    private_asr_report_sources.c.workspace_id == workspace_id,
                    private_asr_report_sources.c.environment_id == environment_id,
                    private_asr_report_sources.c.report_id == report_id,
                    ))).mappings()
                }
                if linked != {
                    (
                        item.record_id, item.source_revision_id, item.source_object_id,
                        item.source_revision_digest, item.split.value,
                    ) for item in source_bindings
                }:
                    raise PrivateASRStorageDenied("Private ASR idempotency conflict.")
            else:
                await self._execute(session, insert(private_asr_artifacts).values(
                    artifact_id=artifact_id, workspace_id=workspace_id, environment_id=environment_id,
                    purpose="asr_model_evaluation", kind="report", status="staging", report_id=report_id,
                    idempotency_key=idempotency_key, content_sha256=digest,
                    byte_length=len(payload), payload=payload,
                    retention_profile_ref=retention_profile_ref.reference_id,
                    retention_disposition=retention_dispositions,
                    audit_receipt_ref=admitted_records[0].audit_receipt_ref.reference_id,
                    expires_at=min(retention_expiries, default=now),
                    dataset_id=report.dataset_id,
                    manifest_version=report.manifest_version,
                    manifest_sha256=report.manifest_sha256,
                    created_at=report.generated_at,
                ))
                for binding in sorted(source_bindings, key=lambda item: str(item.record_id)):
                    await self._execute(session, insert(private_asr_report_sources).values(
                        workspace_id=workspace_id, environment_id=environment_id,
                        purpose="asr_model_evaluation",
                        report_id=report_id, source_record_id=binding.record_id,
                        source_revision_id=binding.source_revision_id,
                        source_object_id=binding.source_object_id,
                        source_revision_digest=binding.source_revision_digest,
                        split=binding.split.value,
                    ))
            if hashlib.sha256(payload).hexdigest() != digest:
                raise PrivateASRStorageDenied("Private ASR report verification failed.")
            # The callback runs while source, current-policy and revocation rows
            # are locked. Callback failure/cancellation rolls this transaction back.
            try:
                await authorize_commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise PrivateASRStorageDenied(
                    "Private ASR report authorization denied."
                ) from None
            append_publication = getattr(self._audit_receipts, "append_report_publication", None)
            if append_publication is None:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            try:
                await append_publication(
                    session, report_id=report_id, receipt_ref=audit_receipt_ref, context=context
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise PrivateASRStorageDenied(
                    "Private ASR report audit authorization unavailable."
                ) from None
            if existing is None:
                await self._execute(session, update(private_asr_artifacts).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.artifact_id == artifact_id,
                ).values(
                    status="committed",
                    audit_receipt_ref=audit_receipt_ref.reference_id,
                ))
            await session.commit()
        return PrivateReportReceipt(
            report_ref=self._ref(artifact_id, "report"),
            manifest_ref=self._ref(artifact_id, "manifest"),
            audit_receipt_ref=audit_receipt_ref,
            retention_profile_ref=retention_profile_ref,
        )

    async def store_immutable_annotation(
        self, admitted: AdmittedSourceRevision, record_id: UUID, annotation_version: int,
        payload: bytes, *, retention_profile_ref: PrivateReference,
        audit_receipt_ref: PrivateReference, context: TrustedPersistenceContext,
    ) -> PrivateReference:
        from spine.domain.evaluations.private_asr_corpus import GroundTruthAnnotation
        workspace_id, environment_id = await self._check_admitted(admitted, context)
        if not payload or len(payload) > self._max_payload_bytes or annotation_version <= 0:
            raise PrivateASRStorageDenied("Private ASR annotation rejected.")
        try:
            annotation = GroundTruthAnnotation.model_validate_json(payload)
        except Exception:
            raise PrivateASRStorageDenied("Private ASR annotation rejected.") from None
        if annotation.source_record_id != record_id or annotation.version != annotation_version:
            raise PrivateASRStorageDenied("Private ASR annotation rejected.")
        if retention_profile_ref != admitted.retention_profile_ref:
            raise PrivateASRStorageDenied("Private ASR annotation rejected.")
        artifact_id = uuid5(NAMESPACE_URL,
            f"spine-private-asr-annotation:v1:{workspace_id}:{environment_id}:{record_id}:{annotation_version}")
        digest = hashlib.sha256(payload).hexdigest()
        async with self._sessions() as session, session.begin():
            await self._set_scope(session, context)
            retention_disposition = await self._locked_source(
                session, admitted, retention_profile_ref=retention_profile_ref,
                artifact_class=RetentionArtifactClass.PRIVATE_ASR_ANNOTATION,
                starting_event=RetentionStartingEvent.ARTIFACT_CREATED,
                starting_at=self._clock(), context=context,
            )
            assert retention_disposition is not None
            audit = (await session.execute(select(audit_events.c.audit_event_id).where(
                audit_events.c.workspace_id == workspace_id,
                audit_events.c.environment_id == environment_id,
                audit_events.c.audit_event_id == audit_receipt_ref.reference_id,
            ))).scalar_one_or_none()
            if audit is None or not await self._authority.recheck(admitted, evaluated_at=self._clock()):
                raise PrivateASRStorageDenied("Private ASR annotation authorization denied.")
            existing = (await session.execute(select(private_asr_artifacts).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == artifact_id,
            ).with_for_update())).mappings().one_or_none()
            if existing is not None:
                if existing["status"] != "committed" or existing["content_sha256"] != digest:
                    raise PrivateASRStorageDenied("Private ASR annotation conflict.")
            else:
                await session.execute(insert(private_asr_artifacts).values(
                    artifact_id=artifact_id, workspace_id=workspace_id, environment_id=environment_id,
                    purpose="asr_model_evaluation", kind="annotation", status="committed",
                    report_id=record_id, idempotency_key=f"annotation:{record_id}:{annotation_version}",
                    source_object_id=admitted.source_object.source_object_id,
                    source_revision_id=admitted.source_revision.revision_id,
                    source_revision_digest=admitted.source_revision.revision_digest,
                    content_sha256=digest, byte_length=len(payload), payload=payload,
                    retention_profile_ref=retention_profile_ref.reference_id,
                    audit_receipt_ref=audit_receipt_ref.reference_id,
                    expires_at=retention_disposition.expires_at,
                    admitted_record=admitted.model_dump(mode="json"), created_at=self._clock(),
                ))
        return self._ref(artifact_id, "annotation")

    async def expire(self, reference: PrivateReference, *, policy_ref: PrivateReference,
                     context: TrustedPersistenceContext) -> None:
        await self._delete_authorized_artifact(
            reference,
            policy_ref=policy_ref,
            context=context,
            action="retention_expiration",
            expected_operation="private_asr_retention_expiration",
        )

    async def export_sanitized(self, payload: bytes, *, context: TrustedPersistenceContext,
                               authorize_export: Callable[[], Any]) -> None:
        """Fail closed until aggregate authorization and audit share a commit."""
        del payload, authorize_export
        self._scope(context)
        raise PrivateASRStorageDenied("Sanitized aggregate export is not enabled.")

    async def publish_sanitized_aggregate(
        self,
        sanitized_report: SanitizedCorpusReport,
        *,
        report_id: UUID,
        idempotency_key: str,
        context: TrustedPersistenceContext,
        authorize_export: Callable[[], Any],
    ) -> PrivateReference:
        """Commit a validated aggregate and its canonical audit in one UoW.

        Production remains disabled without a current authority implementation
        that participates in this transaction. The synthetic integration suite
        supplies a fixture-only authority; it is never production wiring.
        """
        from spine.application.private_asr_corpus import _sanitize_aggregate_report
        from spine.application.private_asr_qualification import (
            PrivateRealASREvaluationReport,
            _aggregate_measurements,
        )

        workspace_id, environment_id = self._scope(context)
        if (
            self._uow_factory is None
            or self._manifest_lineage is None
            or self._retention_resolver is None
            or self._aggregate_authority is None
            or context.operation.value != "sanitize_private_asr_report"
            or not idempotency_key
            or len(idempotency_key) > 256
        ):
            raise PrivateASRStorageDenied("Private ASR aggregate authorization denied.")
        try:
            sanitized = SanitizedCorpusReport(
                schema_version=sanitized_report.schema_version,
                privacy_policy_version=sanitized_report.privacy_policy_version,
                privacy_policy_approval_version=sanitized_report.privacy_policy_approval_version,
                generated_at=sanitized_report.generated_at,
                privacy_policy_approval=sanitized_report.privacy_policy_approval,
                workspace_id=sanitized_report.workspace_id,
                environment_id=sanitized_report.environment_id,
                privacy_policy=sanitized_report.privacy_policy,
                status=sanitized_report.status,
                sufficiency=sanitized_report.sufficiency,
                aggregates=sanitized_report.aggregates,
                metric_names=sanitized_report.metric_names,
            )
        except Exception:
            raise PrivateASRStorageDenied("Private ASR aggregate rejected.") from None
        approval = sanitized.privacy_policy_approval
        if (
            sanitized.workspace_id != workspace_id
            or sanitized.environment_id != environment_id
            or approval.workspace_id != workspace_id
            or approval.environment_id != environment_id
            or approval.policy.policy_version != sanitized.privacy_policy_version
        ):
            raise PrivateASRStorageDenied("Private ASR aggregate authorization denied.")

        public_payload = json.loads(sanitized.model_dump_json())
        payload_bytes = json.dumps(
            public_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        payload_digest = hashlib.sha256(payload_bytes).hexdigest()
        export_id = uuid5(
            NAMESPACE_URL,
            f"spine:private-asr-aggregate:v1:{workspace_id}:{environment_id}:{idempotency_key}",
        )
        audit_receipt_id = uuid5(
            NAMESPACE_URL,
            f"spine:private-asr-aggregate-audit:v1:{workspace_id}:{environment_id}:{export_id}",
        )
        now = self._clock()
        async with self._uow_factory(context) as session:
            await self._set_scope(session, context)
            await self._execute(session, select(func.pg_advisory_xact_lock(func.hashtextextended(
                f"aggregate:{workspace_id}:{environment_id}:{idempotency_key}", 0
            ))))
            reports = (await self._execute(session, select(
                private_asr_artifacts.c.artifact_id,
                private_asr_artifacts.c.report_id,
                private_asr_artifacts.c.status,
                private_asr_artifacts.c.content_sha256,
                private_asr_artifacts.c.expires_at,
                private_asr_artifacts.c.retention_profile_ref,
                private_asr_artifacts.c.retention_disposition,
                private_asr_artifacts.c.audit_receipt_ref,
                private_asr_artifacts.c.created_at,
                private_asr_artifacts.c.dataset_id,
                private_asr_artifacts.c.manifest_version,
                private_asr_artifacts.c.manifest_sha256,
                private_asr_artifacts.c.tombstoned_at,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.purpose == "asr_model_evaluation",
                private_asr_artifacts.c.kind == "report",
                private_asr_artifacts.c.report_id == report_id,
                private_asr_artifacts.c.status == "committed",
                private_asr_artifacts.c.tombstoned_at.is_(None),
                private_asr_artifacts.c.expires_at > now,
            ).with_for_update())).mappings().all()
            if len(reports) != 1:
                raise PrivateASRStorageDenied("Private ASR aggregate authorization denied.")
            report_row = reports[0]
            retention_profile_ref = PrivateReference(
                reference_id=report_row["retention_profile_ref"],
                reference_kind="retention_applicability",
            )
            source_rows = (await self._execute(session, select(
                private_asr_report_sources.c.source_record_id,
                private_asr_report_sources.c.source_revision_id,
                private_asr_report_sources.c.source_object_id,
                private_asr_report_sources.c.source_revision_digest,
                private_asr_report_sources.c.split,
            ).where(
                private_asr_report_sources.c.workspace_id == workspace_id,
                private_asr_report_sources.c.environment_id == environment_id,
                private_asr_report_sources.c.purpose == "asr_model_evaluation",
                private_asr_report_sources.c.report_id == report_id,
            ))).mappings().all()
            if not source_rows or len({row["source_record_id"] for row in source_rows}) != len(source_rows):
                raise PrivateASRStorageDenied("Private ASR aggregate lineage denied.")
            bindings = tuple(sorted((
                PrivateReportSourceBinding(
                    record_id=row["source_record_id"],
                    dataset_id=report_row["dataset_id"],
                    source_object_id=row["source_object_id"],
                    source_revision_id=row["source_revision_id"],
                    source_revision_digest=row["source_revision_digest"],
                    split=CorpusSplit(row["split"]),
                )
                for row in source_rows
            ), key=lambda item: str(item.record_id)))
            if any(
                item.source_object_id is None or item.source_revision_digest is None
                for item in bindings
            ):
                raise PrivateASRStorageDenied("Private ASR aggregate lineage denied.")

            originals_by_revision: dict[UUID, Any] = {}
            admitted_by_revision: dict[UUID, AdmittedSourceRevision] = {}
            source_paths: list[tuple[UUID, UUID, UUID, UUID]] = []
            for binding in bindings:
                originals = (await self._execute(session, select(
                    private_asr_artifacts.c.admitted_record,
                    private_asr_artifacts.c.source_object_id,
                    private_asr_artifacts.c.source_revision_digest,
                    private_asr_artifacts.c.retention_profile_ref,
                    private_asr_artifacts.c.expires_at,
                ).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.source_revision_id == binding.source_revision_id,
                    private_asr_artifacts.c.kind == "original",
                    private_asr_artifacts.c.status == "committed",
                    private_asr_artifacts.c.tombstoned_at.is_(None),
                    private_asr_artifacts.c.expires_at > now,
                ))).mappings().all()
                if len(originals) != 1:
                    raise PrivateASRStorageDenied("Private ASR aggregate authorization denied.")
                original = originals[0]
                admitted = _admitted_from_jsonb(original["admitted_record"])
                if (
                    original["source_object_id"] != binding.source_object_id
                    or original["source_revision_digest"] != binding.source_revision_digest
                    or original["retention_profile_ref"] != retention_profile_ref.reference_id
                    or admitted.source_revision.revision_id != binding.source_revision_id
                    or admitted.source_object.source_object_id != binding.source_object_id
                    or admitted.source_revision.revision_digest != binding.source_revision_digest
                ):
                    raise PrivateASRStorageDenied("Private ASR aggregate lineage denied.")
                originals_by_revision[binding.source_revision_id] = original
                admitted_by_revision[binding.source_revision_id] = admitted
                source_paths.append((
                    workspace_id, environment_id, binding.source_object_id,
                    binding.source_revision_id,
                ))
            await self._lock_source_admission_paths(session, tuple(source_paths))

            stored_report_dispositions = {
                UUID(item["source_revision_id"]): item["disposition"]
                for item in report_row["retention_disposition"]
            }
            aggregate_dispositions: list[dict[str, object]] = []
            aggregate_expiries: list[datetime] = []
            for admitted in sorted(
                admitted_by_revision.values(),
                key=lambda item: (
                    str(item.source_object.source_object_id),
                    str(item.source_revision.revision_id),
                ),
            ):
                revision_id = admitted.source_revision.revision_id
                if originals_by_revision[revision_id]["expires_at"] <= now:
                    raise PrivateASRStorageDenied("Private ASR aggregate authorization denied.")
                report_disposition = await self._locked_source(
                    session,
                    admitted,
                    retention_profile_ref=retention_profile_ref,
                    artifact_class=RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
                    starting_event=RetentionStartingEvent.REPORT_GENERATED,
                    starting_at=report_row["created_at"],
                    context=context,
                    admission_path_locked=True,
                )
                if (
                    report_disposition is None
                    or report_row["expires_at"] <= now
                    or stored_report_dispositions.get(revision_id)
                    != report_disposition.model_dump(mode="json")
                    or not await self._authority.recheck(admitted, evaluated_at=now)
                ):
                    raise PrivateASRStorageDenied("Private ASR aggregate authorization denied.")
                aggregate_disposition = await self._current_retention(
                    session,
                    retention_profile_ref,
                    context=context,
                    admitted=admitted,
                    artifact_class=RetentionArtifactClass.PRIVATE_ASR_SANITIZED_AGGREGATE,
                    starting_event=RetentionStartingEvent.ARTIFACT_CREATED,
                    starting_at=now,
                    allow_expired=False,
                )
                if aggregate_disposition.expires_at <= now:
                    raise PrivateASRStorageDenied("Private ASR aggregate authorization denied.")
                aggregate_expiries.append(aggregate_disposition.expires_at)
                aggregate_dispositions.append({
                    "source_revision_id": str(revision_id),
                    "disposition": aggregate_disposition.model_dump(mode="json"),
                })

            if not await self._manifest_lineage.verify_current(
                transaction=session,
                context=context,
                dataset_id=report_row["dataset_id"],
                manifest_version=report_row["manifest_version"],
                manifest_sha256=report_row["manifest_sha256"],
                retention_profile_ref=retention_profile_ref,
                source_bindings=bindings,
            ):
                raise PrivateASRStorageDenied("Private ASR aggregate lineage denied.")

            async def verify_publication_authority() -> None:
                try:
                    approved = await self._aggregate_authority.verify_current_publication(
                        session,
                        report_id=report_id,
                        sanitized_report=sanitized,
                        context=context,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise PrivateASRStorageDenied(
                        "Private ASR aggregate authorization unavailable."
                    ) from None
                if not approved:
                    raise PrivateASRStorageDenied(
                        "Private ASR aggregate authorization denied."
                    )

            async def verify_export_callback() -> None:
                try:
                    await authorize_export()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise PrivateASRStorageDenied(
                        "Private ASR aggregate authorization denied."
                    ) from None

            # Refuse release before selecting detailed report bytes. Repeat the
            # current checks immediately before audit/publication so a slow
            # payload validation does not extend the callback's freshness.
            await verify_publication_authority()
            await verify_export_callback()

            report_payload = (await self._execute(session, select(
                private_asr_artifacts.c.payload,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == report_row["artifact_id"],
            ))).scalar_one()
            if hashlib.sha256(bytes(report_payload)).hexdigest() != report_row["content_sha256"]:
                raise PrivateASRStorageDenied("Private ASR aggregate lineage denied.")
            try:
                report = PrivateRealASREvaluationReport.model_validate_json(bytes(report_payload))
                expected_sanitized = _sanitize_aggregate_report(
                    policy=sanitized.privacy_policy,
                    approval=approval,
                    approval_version=sanitized.privacy_policy_approval_version,
                    generated_at=sanitized.generated_at,
                    measurements=_aggregate_measurements(report),
                )
            except Exception:
                raise PrivateASRStorageDenied("Private ASR aggregate rejected.") from None
            if (
                report.report_id != report_id
                or report.workspace_id != workspace_id
                or report.environment_id != environment_id
                or report.dataset_id != report_row["dataset_id"]
                or report.manifest_version != report_row["manifest_version"]
                or report.manifest_sha256 != report_row["manifest_sha256"]
                or report.audit_receipt_ref.reference_id != report_row["audit_receipt_ref"]
                or report.retention_profile_ref.reference_id != retention_profile_ref.reference_id
                or {item.record_ref.reference_id for item in report.outcomes}
                != {item.record_id for item in bindings}
                or expected_sanitized != sanitized
            ):
                raise PrivateASRStorageDenied("Private ASR aggregate lineage denied.")

            existing = (await self._execute(session, select(
                private_asr_aggregate_exports.c.export_id,
                private_asr_aggregate_exports.c.report_id,
                private_asr_aggregate_exports.c.privacy_policy_version,
                private_asr_aggregate_exports.c.privacy_policy_approval_version,
                private_asr_aggregate_exports.c.payload_sha256,
                private_asr_aggregate_exports.c.audit_receipt_id,
            ).where(
                private_asr_aggregate_exports.c.workspace_id == workspace_id,
                private_asr_aggregate_exports.c.environment_id == environment_id,
                private_asr_aggregate_exports.c.idempotency_key == idempotency_key,
            ))).mappings().one_or_none()
            if existing is not None and (
                existing["export_id"] != export_id
                or existing["report_id"] != report_id
                or existing["privacy_policy_version"] != sanitized.privacy_policy_version
                or existing["privacy_policy_approval_version"]
                != sanitized.privacy_policy_approval_version
                or existing["payload_sha256"] != payload_digest
                or existing["audit_receipt_id"] != audit_receipt_id
            ):
                raise PrivateASRStorageDenied("Private ASR aggregate idempotency conflict.")

            await verify_publication_authority()
            await verify_export_callback()

            if existing is None:
                append_aggregate = getattr(
                    self._audit_receipts, "append_aggregate_publication", None,
                )
                if append_aggregate is None:
                    raise PrivateASRStorageDenied("Private ASR aggregate audit unavailable.")
                try:
                    await append_aggregate(
                        session,
                        export_id=export_id,
                        report_id=report_id,
                        receipt_id=audit_receipt_id,
                        context=context,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise PrivateASRStorageDenied("Private ASR aggregate audit unavailable.") from None
                await self._execute(session, insert(private_asr_aggregate_exports).values(
                    export_id=export_id,
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    purpose="asr_model_evaluation",
                    report_artifact_id=report_row["artifact_id"],
                    report_id=report_id,
                    idempotency_key=idempotency_key,
                    privacy_policy_version=sanitized.privacy_policy_version,
                    privacy_policy_approval_version=sanitized.privacy_policy_approval_version,
                    retention_profile_ref=retention_profile_ref.reference_id,
                    retention_disposition=aggregate_dispositions,
                    expires_at=min(aggregate_expiries),
                    payload_sha256=payload_digest,
                    audit_receipt_id=audit_receipt_id,
                    payload=public_payload,
                    created_at=now,
                ))
            else:
                audit = (await self._execute(session, select(
                    audit_events.c.audit_event_id,
                ).where(
                    audit_events.c.workspace_id == workspace_id,
                    audit_events.c.environment_id == environment_id,
                    audit_events.c.audit_event_id == audit_receipt_id,
                ))).scalar_one_or_none()
                if audit != audit_receipt_id:
                    raise PrivateASRStorageDenied("Private ASR aggregate audit unavailable.")
            await session.commit()
        return PrivateReference(
            reference_id=export_id,
            reference_kind="private_asr_aggregate",
        )

    async def fence(self, source_revision_id: UUID, *, reason: str,
                    context: TrustedPersistenceContext) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        workspace_id, environment_id = self._require_worker_operation(
            context, "private_asr_revocation"
        )
        if (
            not _SAFE_REASON.fullmatch(reason)
            or self._uow_factory is None
            or getattr(self._audit_receipts, "append_source_revocation_tombstones", None) is None
        ):
            raise PrivateASRStorageDenied("Private ASR revocation rejected.")
        async with self._uow_factory(context) as transaction:
            await self._set_scope(transaction, context)
            source_object_id = (await self._execute(transaction, select(
                source_revisions.c.source_object_id,
            ).where(
                source_revisions.c.workspace_id == workspace_id,
                source_revisions.c.environment_id == environment_id,
                source_revisions.c.revision_id == source_revision_id,
            ))).scalar_one_or_none()
            if source_object_id is None:
                raise PrivateASRStorageDenied("Private ASR revocation rejected.")
            await self._lock_source_admission_path(
                transaction,
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
                revision_id=source_revision_id,
            )
            linked_reports = select(private_asr_report_sources.c.report_id).where(
                private_asr_report_sources.c.workspace_id == workspace_id,
                private_asr_report_sources.c.environment_id == environment_id,
                private_asr_report_sources.c.source_revision_id == source_revision_id,
            )
            affected_artifacts = (await self._execute(transaction, select(
                private_asr_artifacts.c.artifact_id,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.status.in_(("staging", "committed")),
                (private_asr_artifacts.c.source_revision_id == source_revision_id)
                | (
                    (private_asr_artifacts.c.kind == "report")
                    & private_asr_artifacts.c.report_id.in_(linked_reports)
                ),
            ).with_for_update())).scalars().all()
            statement = pg_insert(private_asr_source_fences).values(
                workspace_id=workspace_id, environment_id=environment_id,
                purpose="asr_model_evaluation",
                source_revision_id=source_revision_id, revoked=True, epoch=1,
                reason=reason, updated_at=self._clock(),
            ).on_conflict_do_update(
                index_elements=["workspace_id", "environment_id", "source_revision_id"],
                set_={"revoked": True, "epoch": private_asr_source_fences.c.epoch + 1,
                      "reason": reason, "updated_at": self._clock()},
            )
            await self._execute(transaction, statement)
            if affected_artifacts:
                await self._audit_receipts.append_source_revocation_tombstones(
                    transaction,
                    source_revision_id=source_revision_id,
                    artifact_ids=tuple(affected_artifacts),
                    context=context,
                )
            await self._execute(transaction, update(private_asr_artifacts).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.source_revision_id == source_revision_id,
                private_asr_artifacts.c.status.in_(("staging", "committed")),
            ).values(status="tombstoned", tombstoned_at=self._clock(), payload=b""))
            await self._execute(transaction, update(private_asr_artifacts).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.report_id.in_(linked_reports),
                private_asr_artifacts.c.kind == "report",
                private_asr_artifacts.c.status == "committed",
            ).values(status="tombstoned", tombstoned_at=self._clock(), payload=b""))
            await transaction.commit()

    async def delete_private_reference(self, reference: PrivateReference, *,
                                       context: TrustedPersistenceContext) -> None:
        trusted = self._context(context)
        if trusted.operation.value not in {
            "run_private_asr_evaluation", "private_asr_cleanup"
        } or reference.reference_kind != "private_asr_staging":
            raise PrivateASRStorageDenied("Private ASR cleanup authorization denied.")
        try:
            await self._delete_authorized_artifact(
                reference,
                policy_ref=None,
                context=context,
                action="staging_cleanup",
                expected_operation=trusted.operation.value,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PrivateASRStorageDenied("Private ASR cleanup authorization denied.") from None

    async def _delete_authorized_artifact(
        self,
        reference: PrivateReference,
        *,
        policy_ref: PrivateReference | None,
        context: TrustedPersistenceContext,
        action: str,
        expected_operation: str,
    ) -> None:
        workspace_id, environment_id = self._require_worker_operation(context, expected_operation)
        if self._uow_factory is None or self._deletion_authority is None:
            raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
        expected_kinds = {
            "retention_expiration": {
                "private_asr_original", "private_asr_annotation", "private_asr_report"
            },
            "staging_cleanup": {"private_asr_staging"},
        }
        if reference.reference_kind not in expected_kinds.get(action, set()):
            raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
        now = self._clock()
        async with self._uow_factory(context) as transaction:
            await self._set_scope(transaction, context)
            initial = (await self._execute(transaction, select(
                private_asr_artifacts.c.artifact_id,
                private_asr_artifacts.c.kind,
                private_asr_artifacts.c.report_id,
                private_asr_artifacts.c.status,
                private_asr_artifacts.c.source_object_id,
                private_asr_artifacts.c.source_revision_id,
                private_asr_artifacts.c.source_revision_digest,
                private_asr_artifacts.c.content_sha256,
                private_asr_artifacts.c.byte_length,
                private_asr_artifacts.c.retention_profile_ref,
                private_asr_artifacts.c.retention_disposition,
                private_asr_artifacts.c.audit_receipt_ref,
                private_asr_artifacts.c.expires_at,
                private_asr_artifacts.c.admitted_record,
                private_asr_artifacts.c.created_at,
                private_asr_artifacts.c.dataset_id,
                private_asr_artifacts.c.manifest_version,
                private_asr_artifacts.c.manifest_sha256,
                private_asr_artifacts.c.tombstoned_at,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == reference.reference_id,
            ))).mappings().one_or_none()
            if initial is None:
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            if initial["status"] == "tombstoned":
                await transaction.commit()
                return
            prior_receipt = (await self._execute(transaction, select(
                audit_events.c.audit_event_id,
            ).where(
                audit_events.c.workspace_id == workspace_id,
                audit_events.c.environment_id == environment_id,
                audit_events.c.audit_event_id == initial["audit_receipt_ref"],
            ))).scalar_one_or_none()
            if prior_receipt is None:
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            kind = initial["kind"]
            if reference.reference_kind != f"private_asr_{kind}" or kind not in {
                "staging", "original", "annotation", "report"
            }:
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            if (action == "retention_expiration") != (kind != "staging"):
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            stored_profile_ref = PrivateReference(
                reference_id=initial["retention_profile_ref"],
                reference_kind="retention_applicability",
            )
            if policy_ref is not None and policy_ref != stored_profile_ref:
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            if policy_ref is None:
                policy_ref = stored_profile_ref

            admitted_by_revision: dict[UUID, AdmittedSourceRevision] = {}
            manifest_bindings: dict[UUID, tuple[UUID, UUID, CorpusSplit] | None] = {}
            if kind == "report":
                linked = (await self._execute(transaction, select(
                    private_asr_report_sources.c.source_record_id,
                    private_asr_report_sources.c.source_revision_id,
                    private_asr_report_sources.c.source_object_id,
                    private_asr_report_sources.c.source_revision_digest,
                    private_asr_report_sources.c.split,
                ).where(
                    private_asr_report_sources.c.workspace_id == workspace_id,
                    private_asr_report_sources.c.environment_id == environment_id,
                    private_asr_report_sources.c.report_id == initial["report_id"],
                ))).mappings().all()
                if not linked or len({row["source_record_id"] for row in linked}) != len(linked):
                    raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
                for link in linked:
                    original = (await self._execute(transaction, select(
                        private_asr_artifacts.c.admitted_record,
                        private_asr_artifacts.c.source_object_id,
                        private_asr_artifacts.c.source_revision_digest,
                        private_asr_artifacts.c.retention_profile_ref,
                    ).where(
                        private_asr_artifacts.c.workspace_id == workspace_id,
                        private_asr_artifacts.c.environment_id == environment_id,
                        private_asr_artifacts.c.source_revision_id == link["source_revision_id"],
                        private_asr_artifacts.c.kind == "original",
                        private_asr_artifacts.c.status.in_(("committed", "tombstoned")),
                    ))).mappings().one_or_none()
                    if original is None:
                        raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
                    admitted = _admitted_from_jsonb(original["admitted_record"])
                    if (
                        original["retention_profile_ref"] != policy_ref.reference_id
                        or link["source_object_id"] != admitted.source_object.source_object_id
                        or link["source_revision_digest"] != admitted.source_revision.revision_digest
                        or link["source_revision_id"] != admitted.source_revision.revision_id
                    ):
                        raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
                    admitted_by_revision[admitted.source_revision.revision_id] = admitted
                    manifest_bindings[admitted.source_revision.revision_id] = (
                        link["source_record_id"], initial["dataset_id"], CorpusSplit(link["split"])
                    )
            else:
                if initial["admitted_record"] is None:
                    raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
                admitted = _admitted_from_jsonb(initial["admitted_record"])
                if (
                    initial["source_object_id"] != admitted.source_object.source_object_id
                    or initial["source_revision_id"] != admitted.source_revision.revision_id
                    or initial["source_revision_digest"] != admitted.source_revision.revision_digest
                ):
                    raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
                admitted_by_revision[admitted.source_revision.revision_id] = admitted
                manifest_bindings[admitted.source_revision.revision_id] = None

            source_lineage: list[PrivateArtifactSourceLineage] = []
            dispositions = []
            await self._lock_source_admission_paths(
                transaction,
                tuple(
                    (
                        workspace_id,
                        environment_id,
                        admitted.source_object.source_object_id,
                        revision_id,
                    )
                    for revision_id, admitted in admitted_by_revision.items()
                ),
            )
            for revision_id, admitted in sorted(
                admitted_by_revision.items(),
                key=lambda pair: (str(pair[1].source_object.source_object_id), str(pair[0])),
            ):
                artifact_class = _retention_class_for_kind(kind)
                if artifact_class is None:
                    raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
                starting_event = _retention_starting_event_for_kind(kind)
                disposition = await self._locked_source(
                    transaction,
                    admitted,
                    retention_profile_ref=policy_ref,
                    artifact_class=artifact_class,
                    starting_event=starting_event,
                    starting_at=initial["created_at"],
                    retention_allow_expired=True,
                    context=context,
                    admission_path_locked=True,
                )
                assert disposition is not None
                dispositions.append(disposition)
                binding = manifest_bindings[revision_id]
                source_lineage.append(PrivateArtifactSourceLineage(
                    source_object_id=admitted.source_object.source_object_id,
                    source_revision_id=revision_id,
                    source_revision_digest=admitted.source_revision.revision_digest,
                    record_id=binding[0] if binding else None,
                    dataset_id=binding[1] if binding else None,
                    split=binding[2] if binding else None,
                ))
            current_deadline = min(item.expires_at for item in dispositions)
            if (
                initial["expires_at"] != current_deadline
                or (action == "retention_expiration" and current_deadline > now)
                or (action == "staging_cleanup" and initial["status"] != "staging")
            ):
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            state = {
                "artifact_id": str(initial["artifact_id"]), "kind": kind,
                "status": initial["status"], "source_object_id": str(initial["source_object_id"]),
                "source_revision_id": str(initial["source_revision_id"]),
                "source_revision_digest": initial["source_revision_digest"],
                "content_sha256": initial["content_sha256"], "byte_length": initial["byte_length"],
                "retention_profile_ref": str(initial["retention_profile_ref"]),
                "audit_receipt_ref": str(initial["audit_receipt_ref"]),
                "dataset_id": str(initial["dataset_id"]) if initial["dataset_id"] else None,
                "manifest_version": initial["manifest_version"],
                "manifest_sha256": initial["manifest_sha256"],
                "retention_disposition": initial["retention_disposition"],
                "expires_at": initial["expires_at"].isoformat(),
                "created_at": initial["created_at"].isoformat(),
            }
            request = PrivateArtifactDeletionRequest(
                artifact_id=reference.reference_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                artifact_kind=kind,
                action=action,
                expected_state_digest=hashlib.sha256(json.dumps(
                    state, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest(),
                retention_profile_ref=policy_ref,
                retention_deadline=current_deadline,
                requested_at=now,
                source_lineage=tuple(source_lineage),
                manifest_version=initial["manifest_version"],
                manifest_sha256=initial["manifest_sha256"],
            )
            try:
                authz = await self._deletion_authority.authorize_deletion(
                    request, context=context, transaction=transaction
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.") from None
            if not isinstance(authz, PrivateArtifactDeletionAuthorization) or (
                not authz.authorized
                or authz.request_digest != request.calculate_digest()
                or authz.valid_until <= now
            ):
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            current = (await self._execute(transaction, select(
                private_asr_artifacts.c.status,
                private_asr_artifacts.c.content_sha256,
                private_asr_artifacts.c.byte_length,
                private_asr_artifacts.c.expires_at,
                private_asr_artifacts.c.retention_profile_ref,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == reference.reference_id,
            ).with_for_update())).mappings().one_or_none()
            if current is None or (
                current["status"], current["content_sha256"], current["byte_length"],
                current["expires_at"], current["retention_profile_ref"],
            ) != (
                initial["status"], initial["content_sha256"], initial["byte_length"],
                initial["expires_at"], initial["retention_profile_ref"],
            ):
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            audit_writer = getattr(self._audit_receipts, "append_artifact_deletion", None)
            if audit_writer is None:
                raise PrivateASRStorageDenied("Private ASR deletion authorization denied.")
            try:
                await audit_writer(
                    transaction,
                    request=request,
                    authorization=authz,
                    context=context,
                )
                await self._execute(transaction, update(private_asr_artifacts).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.artifact_id == reference.reference_id,
                    private_asr_artifacts.c.status != "tombstoned",
                ).values(status="tombstoned", tombstoned_at=now, payload=b""))
                await transaction.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise PrivateASRStorageDenied("Private ASR deletion transaction failed.") from None

    async def read_private_reference(self, reference: PrivateReference, *,
                                     context: TrustedPersistenceContext) -> bytes:
        try:
            return await self._read_private_reference(reference, context=context)
        except Exception:
            # Metadata existence, malformed lineage, and unavailable authority
            # are deliberately indistinguishable to the caller.
            raise PrivateASRStorageDenied("Private ASR storage authorization denied.") from None

    async def _read_private_reference(self, reference: PrivateReference, *,
                                      context: TrustedPersistenceContext) -> bytes:
        workspace_id, environment_id = self._scope(context)
        async with self._sessions() as session, session.begin():
            await self._set_scope(session, context)
            row = (await session.execute(select(
                private_asr_artifacts.c.kind,
                private_asr_artifacts.c.source_object_id,
                private_asr_artifacts.c.source_revision_id,
                private_asr_artifacts.c.retention_profile_ref,
                private_asr_artifacts.c.retention_disposition,
                private_asr_artifacts.c.audit_receipt_ref,
                private_asr_artifacts.c.expires_at,
                private_asr_artifacts.c.admitted_record,
                private_asr_artifacts.c.report_id,
                private_asr_artifacts.c.content_sha256,
                private_asr_artifacts.c.created_at,
                private_asr_artifacts.c.dataset_id,
                private_asr_artifacts.c.manifest_version,
                private_asr_artifacts.c.manifest_sha256,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == reference.reference_id,
                private_asr_artifacts.c.status == "committed",
                private_asr_artifacts.c.tombstoned_at.is_(None),
                private_asr_artifacts.c.expires_at > self._clock(),
            ))).mappings().one_or_none()
            valid_reference_kinds = {f"private_asr_{row['kind']}", "private_asr_manifest"} if row and row["kind"] == "report" else ({f"private_asr_{row['kind']}"} if row else set())
            if row is None or reference.reference_kind not in valid_reference_kinds:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            audit = (await session.execute(select(audit_events.c.audit_event_id).where(
                audit_events.c.workspace_id == workspace_id,
                audit_events.c.environment_id == environment_id,
                audit_events.c.audit_event_id == row["audit_receipt_ref"],
            ))).scalar_one_or_none()
            if audit is None:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            if row["kind"] == "report":
                binding_rows = (await session.execute(select(
                    private_asr_report_sources.c.source_record_id,
                    private_asr_report_sources.c.source_revision_id,
                    private_asr_report_sources.c.source_object_id,
                    private_asr_report_sources.c.source_revision_digest,
                    private_asr_report_sources.c.split,
                ).where(
                    private_asr_report_sources.c.workspace_id == workspace_id,
                    private_asr_report_sources.c.environment_id == environment_id,
                    private_asr_report_sources.c.report_id == row["report_id"],
                ))).mappings().all()
                if not binding_rows:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                if (
                    row["dataset_id"] is None or row["manifest_version"] is None
                    or row["manifest_sha256"] is None
                    or any(
                        item["source_object_id"] is None
                        or item["source_revision_digest"] is None
                        or item["split"] not in {"development", "qualification"}
                        for item in binding_rows
                    )
                ):
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                source_ids = {item["source_revision_id"] for item in binding_rows}
                report_bindings = tuple(
                    PrivateReportSourceBinding(
                        record_id=item["source_record_id"], dataset_id=row["dataset_id"],
                        source_object_id=item["source_object_id"],
                        source_revision_id=item["source_revision_id"],
                        source_revision_digest=item["source_revision_digest"],
                        split=CorpusSplit(item["split"]),
                    ) for item in binding_rows
                )
                if self._manifest_lineage is None or not await self._manifest_lineage.verify_current(
                    transaction=session, context=context, dataset_id=row["dataset_id"],
                    manifest_version=row["manifest_version"],
                    manifest_sha256=row["manifest_sha256"],
                    retention_profile_ref=PrivateReference(
                        reference_id=row["retention_profile_ref"],
                        reference_kind="retention_applicability",
                    ),
                    source_bindings=report_bindings,
                ):
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                source_rows = (await session.execute(select(
                    private_asr_artifacts.c.admitted_record,
                    private_asr_artifacts.c.source_object_id,
                    private_asr_artifacts.c.source_revision_digest,
                ).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.source_revision_id.in_(source_ids),
                    private_asr_artifacts.c.kind == "original",
                    private_asr_artifacts.c.status == "committed",
                    private_asr_artifacts.c.expires_at > self._clock(),
                    private_asr_artifacts.c.tombstoned_at.is_(None),
                ).order_by(private_asr_artifacts.c.source_revision_id))).mappings().all()
                if len(source_rows) != len(source_ids):
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                admitted_values = [item["admitted_record"] for item in source_rows]
                for source_row in source_rows:
                    admitted_source = _admitted_from_jsonb(source_row["admitted_record"])
                    matched = [
                        item for item in report_bindings
                        if item.source_revision_id == admitted_source.source_revision.revision_id
                    ]
                    if not matched or any(
                        item.source_object_id != source_row["source_object_id"]
                        or item.source_revision_digest != source_row["source_revision_digest"]
                        for item in matched
                    ):
                        raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            elif row["admitted_record"] is not None:
                admitted_values = [row["admitted_record"]]
            else:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            current_retention_expiries: list[datetime] = []
            current_retention_dispositions: list[dict[str, object]] = []
            admitted_records = sorted(
                (_admitted_from_jsonb(value) for value in admitted_values),
                key=lambda item: (
                    str(item.source_object.source_object_id),
                    str(item.source_revision.revision_id),
                ),
            )
            await self._lock_source_admission_paths(
                session,
                tuple(
                    (
                        workspace_id,
                        environment_id,
                        admitted.source_object.source_object_id,
                        admitted.source_revision.revision_id,
                    )
                    for admitted in admitted_records
                ),
            )
            for admitted in admitted_records:
                artifact_class = _retention_class_for_kind(row["kind"])
                if artifact_class is None:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                disposition = await self._locked_source(
                    session, admitted,
                    retention_profile_ref=PrivateReference(
                        reference_id=row["retention_profile_ref"],
                        reference_kind="retention_applicability",
                    ),
                    artifact_class=artifact_class,
                    starting_event=_retention_starting_event_for_kind(row["kind"]),
                    starting_at=row["created_at"], context=context,
                    admission_path_locked=True,
                )
                if disposition is None:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
                current_retention_expiries.append(disposition.expires_at)
                current_retention_dispositions.append({
                    "source_revision_id": str(admitted.source_revision.revision_id),
                    "disposition": disposition.model_dump(mode="json"),
                })
                await self._check_admitted(admitted, context)
            if min(current_retention_expiries) != row["expires_at"]:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            if row["kind"] == "report":
                current_retention_dispositions.sort(
                    key=lambda item: str(item["source_revision_id"])
                )
                if row["retention_disposition"] != current_retention_dispositions:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            if row["source_revision_id"] is not None:
                fence = (await session.execute(select(private_asr_source_fences.c.revoked).where(
                    private_asr_source_fences.c.workspace_id == workspace_id,
                    private_asr_source_fences.c.environment_id == environment_id,
                    private_asr_source_fences.c.source_revision_id == row["source_revision_id"],
                ).with_for_update())).scalar_one_or_none()
                if fence:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            if row["kind"] == "report":
                locked_originals = (await session.execute(select(
                    private_asr_artifacts.c.source_revision_id,
                ).where(
                    private_asr_artifacts.c.workspace_id == workspace_id,
                    private_asr_artifacts.c.environment_id == environment_id,
                    private_asr_artifacts.c.source_revision_id.in_(source_ids),
                    private_asr_artifacts.c.kind == "original",
                    private_asr_artifacts.c.status == "committed",
                    private_asr_artifacts.c.tombstoned_at.is_(None),
                    private_asr_artifacts.c.expires_at > self._clock(),
                ).order_by(private_asr_artifacts.c.source_revision_id).with_for_update(read=True))).scalars().all()
                if set(locked_originals) != source_ids:
                    raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            locked_artifact = (await session.execute(select(
                private_asr_artifacts.c.artifact_id,
            ).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == reference.reference_id,
                private_asr_artifacts.c.status == "committed",
                private_asr_artifacts.c.tombstoned_at.is_(None),
                private_asr_artifacts.c.expires_at > self._clock(),
            ).with_for_update(read=True))).scalar_one_or_none()
            if locked_artifact is None:
                raise PrivateASRStorageDenied("Private ASR storage authorization denied.")
            payload_result = await session.execute(select(private_asr_artifacts.c.payload).where(
                private_asr_artifacts.c.workspace_id == workspace_id,
                private_asr_artifacts.c.environment_id == environment_id,
                private_asr_artifacts.c.artifact_id == reference.reference_id,
                private_asr_artifacts.c.status == "committed",
                private_asr_artifacts.c.tombstoned_at.is_(None),
                private_asr_artifacts.c.expires_at > self._clock(),
            ))
            payload = bytes(payload_result.scalar_one())
            if row["content_sha256"] is None or hashlib.sha256(payload).hexdigest() != row["content_sha256"]:
                raise PrivateASRStorageDenied("Private ASR storage unavailable.")
            return payload
