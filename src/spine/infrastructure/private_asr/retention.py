"""Transaction-aware reuse of Spine's canonical retention applicability.

No ASR retention registry lives here. The adapter locks Spine's current
applicability pointer, restores the immutable binding, delegates approval and
policy-term resolution through typed ports, then derives enforcement expiry.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import and_, event, select

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.commit_fence import (
    CommitFenceBinding,
    CommitFenceOutcomeKind,
    canonical_commit_fence_lineage_digest,
)
from spine.application.private_asr_corpus import PrivateArtifactSourceLineage
from spine.application.retention import (
    ApprovedRetentionTerms,
    ApprovedRetentionTermsProvider,
    RetentionCommitApprovalVerifier,
    RetentionStartingEvent,
)
from spine.domain.common import DefinitionModel
from spine.domain.evaluations.private_asr_corpus import PrivateReference
from spine.domain.sources.models import SourceObject, SourceRevision
from spine.domain.sources.retention import (
    RetentionApplicability,
    RetentionArtifactClass,
    RetentionTargetKind,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    current_retention_applicabilities,
    retention_applicabilities,
    source_objects,
    source_revisions,
)
from spine.infrastructure.persistence.postgresql_commit_fence import (
    PostgreSQLCommitFence,
    PostgreSQLFenceOwner,
    postgresql_f1_issuer_id,
)


class PrivateASRRetentionDenied(Exception):
    """Disclosure-safe failure to resolve current retention authority."""


class PrivateASRRetentionDisposition(DefinitionModel):
    """Verified binding and term provenance used to enforce an artifact deadline."""

    applicability_id: UUID
    applicability_generation: int = Field(ge=1)
    policy_id: UUID
    policy_version_id: UUID
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    terms_id: UUID
    terms_version: str
    terms_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_class: RetentionArtifactClass
    starting_event: RetentionStartingEvent
    starting_at: datetime
    approved_evidence_reference: UUID
    deletion_obligation_reference: UUID
    backup_obligation_reference: UUID
    expires_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> "PrivateASRRetentionDisposition":
        if (
            self.starting_at.tzinfo is None
            or self.starting_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.expires_at <= self.starting_at
        ):
            raise ValueError("retention disposition timestamps are invalid")
        return self


class PostgreSQLPrivateASRRetentionResolver:
    """Resolve exact-source applicability under the caller's DB transaction.

    Exact-source applicability is required for this first adapter. Supporting
    source-class matching needs an additional shared lock for class-pointer
    insertion and is deliberately not inferred from a source-kind string.
    """

    def __init__(
        self,
        *,
        approvals: RetentionCommitApprovalVerifier | None,
        terms: ApprovedRetentionTermsProvider | None,
        test_only_allow_synthetic_sources: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._approvals = approvals
        self._terms = terms
        self._test_only_allow_synthetic_sources = test_only_allow_synthetic_sources
        self._clock = clock

    async def resolve(
        self,
        transaction: object,
        *,
        context: TrustedPersistenceContext,
        profile_ref: PrivateReference,
        source: SourceObject,
        revision: SourceRevision,
        artifact_class: RetentionArtifactClass,
        starting_event: RetentionStartingEvent,
        starting_at: datetime,
        evaluated_at: datetime,
        allow_expired: bool = False,
    ) -> PrivateASRRetentionDisposition:
        try:
            return await self._resolve(
                transaction,
                context=context,
                profile_ref=profile_ref,
                source=source,
                revision=revision,
                artifact_class=artifact_class,
                starting_event=starting_event,
                starting_at=starting_at,
                evaluated_at=evaluated_at,
                allow_expired=allow_expired,
            )
        except PrivateASRRetentionDenied:
            raise
        except Exception:
            raise PrivateASRRetentionDenied() from None

    async def _resolve(
        self,
        transaction: object,
        *,
        context: TrustedPersistenceContext,
        profile_ref: PrivateReference,
        source: SourceObject,
        revision: SourceRevision,
        artifact_class: RetentionArtifactClass,
        starting_event: RetentionStartingEvent,
        starting_at: datetime,
        evaluated_at: datetime,
        allow_expired: bool,
    ) -> PrivateASRRetentionDisposition:
        scope = context.scope
        if (
            self._approvals is None
            or self._terms is None
            or not self._sources_are_fenceable()
            or not isinstance(scope, EnvironmentScope)
            or context.purpose.value != "asr_model_evaluation"
            or (scope.workspace_id, scope.environment_id)
            != (source.workspace_id, source.environment_id)
            or (source.workspace_id, source.environment_id)
            != (revision.workspace_id, revision.environment_id)
            or source.source_object_id != revision.source_object_id
            or profile_ref.reference_kind != "retention_applicability"
            or starting_at.tzinfo is None
            or starting_at.utcoffset() is None
            or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() is None
            or starting_at > evaluated_at
        ):
            raise PrivateASRRetentionDenied()

        lineage = await _execute(
            transaction,
            select(
                source_objects.c.source_object_id,
                source_revisions.c.revision_id,
                source_revisions.c.revision_digest,
                source_revisions.c.original_sha256,
                source_revisions.c.byte_length,
                source_revisions.c.kind,
            )
            .select_from(
                source_objects.join(
                    source_revisions,
                    and_(
                        source_revisions.c.workspace_id == source_objects.c.workspace_id,
                        source_revisions.c.environment_id == source_objects.c.environment_id,
                        source_revisions.c.source_object_id == source_objects.c.source_object_id,
                    ),
                )
            )
            .where(
                source_objects.c.workspace_id == scope.workspace_id,
                source_objects.c.environment_id == scope.environment_id,
                source_objects.c.source_object_id == source.source_object_id,
                source_revisions.c.revision_id == revision.revision_id,
                source_revisions.c.revision_digest == revision.revision_digest,
                source_revisions.c.original_sha256 == revision.original_sha256,
                source_revisions.c.byte_length == revision.byte_length,
                source_revisions.c.kind == "content",
            ),
        )
        if lineage.mappings().one_or_none() is None:
            raise PrivateASRRetentionDenied()

        result = await _execute(
            transaction,
            select(
                current_retention_applicabilities.c.current_applicability_id,
                current_retention_applicabilities.c.generation.label("current_generation"),
                retention_applicabilities,
            )
            .select_from(
                current_retention_applicabilities.join(
                    retention_applicabilities,
                    and_(
                        retention_applicabilities.c.workspace_id
                        == current_retention_applicabilities.c.workspace_id,
                        retention_applicabilities.c.environment_id
                        == current_retention_applicabilities.c.environment_id,
                        retention_applicabilities.c.target_kind
                        == current_retention_applicabilities.c.target_kind,
                        retention_applicabilities.c.target_reference
                        == current_retention_applicabilities.c.target_reference,
                        retention_applicabilities.c.applicability_id
                        == current_retention_applicabilities.c.current_applicability_id,
                        retention_applicabilities.c.generation
                        == current_retention_applicabilities.c.generation,
                    ),
                )
            )
            .where(
                current_retention_applicabilities.c.workspace_id == scope.workspace_id,
                current_retention_applicabilities.c.environment_id == scope.environment_id,
                current_retention_applicabilities.c.target_kind
                == RetentionTargetKind.EXACT_SOURCE.value,
                current_retention_applicabilities.c.target_reference
                == source.source_object_id,
                current_retention_applicabilities.c.current_applicability_id
                == profile_ref.reference_id,
            )
            .with_for_update(of=current_retention_applicabilities),
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise PrivateASRRetentionDenied()
        binding = RetentionApplicability.model_validate_json(
            json.dumps(row["record"])
        )
        if (
            binding.applicability_id != row["current_applicability_id"]
            or binding.generation != row["current_generation"]
            or binding.applicability_id != profile_ref.reference_id
            or binding.workspace_id != scope.workspace_id
            or binding.environment_id != scope.environment_id
            or binding.target_kind is not RetentionTargetKind.EXACT_SOURCE
            or binding.source_object_id != source.source_object_id
            or binding.retention_policy_id != row["retention_policy_id"]
            or binding.retention_policy_version_id != row["retention_policy_version_id"]
            or binding.retention_policy_digest != row["retention_policy_digest"]
            or artifact_class not in binding.artifact_classes
            or evaluated_at < binding.effective_from
            or (
                binding.effective_until is not None
                and evaluated_at >= binding.effective_until
            )
        ):
            raise PrivateASRRetentionDenied()
        if not await self._approvals.verify_current(binding, context, transaction):
            raise PrivateASRRetentionDenied()
        terms = await self._terms.resolve(
            binding, artifact_class, context, transaction
        )
        if not _terms_match(terms, binding, artifact_class, starting_event):
            raise PrivateASRRetentionDenied()
        assert terms is not None
        if (
            terms.resolved_exception_references != binding.exception_references
            or terms.resolved_legal_hold_references != binding.legal_hold_references
            or terms.active_legal_hold_references
        ):
            raise PrivateASRRetentionDenied()
        expires_at = starting_at + timedelta(seconds=terms.duration_seconds)
        if expires_at <= evaluated_at and not allow_expired:
            raise PrivateASRRetentionDenied()
        await self._acquire_f1_commit_fence(
            transaction,
            context=context,
            source=source,
            revision=revision,
            binding=binding,
            record=row["record"],
            terms=terms,
            artifact_class=artifact_class,
            operation=context.operation.value,
            evaluated_at=evaluated_at,
        )
        return PrivateASRRetentionDisposition(
            applicability_id=binding.applicability_id,
            applicability_generation=binding.generation,
            policy_id=binding.retention_policy_id,
            policy_version_id=binding.retention_policy_version_id,
            policy_digest=binding.retention_policy_digest,
            terms_id=terms.terms_id,
            terms_version=terms.terms_version,
            terms_digest=terms.terms_digest,
            artifact_class=artifact_class,
            starting_event=starting_event,
            starting_at=starting_at,
            approved_evidence_reference=terms.approval_evidence_reference,
            deletion_obligation_reference=terms.deletion_obligation_reference,
            backup_obligation_reference=terms.backup_obligation_reference,
            expires_at=expires_at,
        )

    def _sources_are_fenceable(self) -> bool:
        """Require explicit PostgreSQL ownership for approval and terms reads.

        Synthetic test authorities are an opt-in test seam. An external or
        unidentified source needs a provider-specific F2 adapter; until one is
        configured it fails closed.
        """

        if self._test_only_allow_synthetic_sources:
            return True
        return bool(
            getattr(self._approvals, "retention_fence_source", None) == "postgresql"
            and getattr(self._terms, "retention_fence_source", None) == "postgresql"
        )

    async def _acquire_f1_commit_fence(
        self,
        transaction: object,
        *,
        context: TrustedPersistenceContext,
        source: SourceObject,
        revision: SourceRevision,
        binding: RetentionApplicability,
        record: object,
        terms: ApprovedRetentionTerms,
        artifact_class: RetentionArtifactClass,
        operation: str,
        evaluated_at: datetime,
    ) -> None:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise PrivateASRRetentionDenied()
        record_digest = hashlib.sha256(
            _canonical_json(_json_object(record))
        ).hexdigest()
        state_digest = _retention_state_digest(
            binding=binding,
            terms=terms,
            artifact_class=artifact_class,
            operation=operation,
        )
        lineage = (
            PrivateArtifactSourceLineage(
                source_object_id=source.source_object_id,
                source_revision_id=revision.revision_id,
                source_revision_digest=revision.revision_digest,
            ),
        )
        valid_until = binding.effective_until or datetime.max.replace(
            tzinfo=evaluated_at.tzinfo
        )
        if evaluated_at >= valid_until:
            raise PrivateASRRetentionDenied()
        fence_binding = CommitFenceBinding(
            issuer_id=postgresql_f1_issuer_id(PostgreSQLFenceOwner.RETENTION_APPLICABILITY),
            approval_version=str(binding.applicability_id),
            approval_digest=record_digest,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            purpose=context.purpose.value,
            action=operation,
            subject_id=source.source_object_id,
            state_digest=state_digest,
            source_lineage=lineage,
            lineage_digest=canonical_commit_fence_lineage_digest(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                source_lineage=lineage,
            ),
            generation=binding.generation,
            revocation_epoch=binding.generation,
            operation_id=context.trace_id,
            idempotency_key=(
                f"retention:{context.trace_id}:{artifact_class.value}:"
                f"{binding.applicability_id}:{terms.terms_id}:{operation}"
            )[:128],
            valid_from=evaluated_at,
            valid_until=valid_until,
        )
        adapter = (
            transaction
            if callable(getattr(transaction, "register_commit_guard", None))
            and getattr(transaction, "scope", None) is not None
            else _SessionFenceUnitOfWork(transaction, scope)
        )
        outcome = await PostgreSQLCommitFence(adapter, clock=self._clock).acquire(
            fence_binding,
            owner=PostgreSQLFenceOwner.RETENTION_APPLICABILITY,
            now=evaluated_at,
        )
        if outcome.outcome is not CommitFenceOutcomeKind.ACQUIRED:
            raise PrivateASRRetentionDenied()


class _SessionFenceUnitOfWork:
    """Narrow F1 view over an active SQLAlchemy AsyncSession transaction."""

    def __init__(self, transaction: object, scope: EnvironmentScope) -> None:
        self._transaction = transaction
        self.scope = scope

    async def execute_statement(
        self, statement: object, parameters: object | None = None
    ) -> Any:
        execute = getattr(self._transaction, "execute_statement", None)
        if execute is not None:
            return await execute(statement, parameters)
        return await self._transaction.execute(statement, parameters)  # type: ignore[attr-defined,no-any-return]

    def register_commit_guard(self, guard: Callable[[], bool]) -> None:
        sync_session = getattr(self._transaction, "sync_session", None)
        if sync_session is None:
            raise PrivateASRRetentionDenied()

        def check_before_commit(session) -> None:
            del session
            if not guard():
                raise PrivateASRRetentionDenied()

        def cleanup(session) -> None:
            if event.contains(sync_session, "before_commit", check_before_commit):
                event.remove(sync_session, "before_commit", check_before_commit)
            del session

        event.listen(sync_session, "before_commit", check_before_commit, once=True)
        event.listen(sync_session, "after_commit", cleanup, once=True)
        event.listen(sync_session, "after_rollback", cleanup, once=True)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, dict) else {}


def _retention_state_digest(
    *,
    binding: RetentionApplicability,
    terms: ApprovedRetentionTerms,
    artifact_class: RetentionArtifactClass,
    operation: str,
) -> str:
    state = {
        "schema_version": "retention-consumer-state:v1",
        "retention_policy_id": str(binding.retention_policy_id),
        "retention_policy_version_id": str(binding.retention_policy_version_id),
        "retention_policy_digest": binding.retention_policy_digest,
        "applicability_id": str(binding.applicability_id),
        "applicability_generation": binding.generation,
        "applicability_evidence_reference": str(binding.applicability_evidence_reference),
        "applicability_evidence_digest": binding.applicability_evidence_digest,
        "approving_authority_reference": str(binding.approving_authority_reference),
        "approving_authority_version_id": str(binding.approving_authority_version_id),
        "approving_authority_digest": binding.approving_authority_digest,
        "terms_id": str(terms.terms_id),
        "terms_version": terms.terms_version,
        "terms_digest": terms.terms_digest,
        "starting_event": terms.starting_event.value,
        "duration_seconds": terms.duration_seconds,
        "approval_evidence_reference": str(terms.approval_evidence_reference),
        "deletion_obligation_reference": str(terms.deletion_obligation_reference),
        "backup_obligation_reference": str(terms.backup_obligation_reference),
        "approved": terms.approved,
        "revoked": terms.revoked,
        "artifact_class": artifact_class.value,
        "resolved_exceptions": sorted(map(str, terms.resolved_exception_references)),
        "resolved_holds": sorted(map(str, terms.resolved_legal_hold_references)),
        "active_holds": sorted(map(str, terms.active_legal_hold_references)),
        "operation": operation,
    }
    return hashlib.sha256(
        b"spine.retention-consumer-state:v1\0" + _canonical_json(state)
    ).hexdigest()


async def _execute(transaction: object, statement: object) -> Any:
    execute_statement = getattr(transaction, "execute_statement", None)
    if execute_statement is not None:
        return await execute_statement(statement)
    return await transaction.execute(statement)  # type: ignore[attr-defined,no-any-return]


def _terms_match(
    terms: ApprovedRetentionTerms | None,
    binding: RetentionApplicability,
    artifact_class: RetentionArtifactClass,
    starting_event: RetentionStartingEvent,
) -> bool:
    return bool(
        terms is not None
        and terms.approved
        and not terms.revoked
        and terms.retention_policy_id == binding.retention_policy_id
        and terms.retention_policy_version_id == binding.retention_policy_version_id
        and terms.retention_policy_digest == binding.retention_policy_digest
        and terms.artifact_class is artifact_class
        and terms.starting_event is starting_event
    )


__all__ = [
    "PostgreSQLPrivateASRRetentionResolver",
    "PrivateASRRetentionDenied",
    "PrivateASRRetentionDisposition",
]
