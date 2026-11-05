"""Transaction-bound F1 commit fences for PostgreSQL-owned current state.

This adapter does not create an authority registry. It locks and resolves the
canonical owner row through the caller's active PostgreSQL Unit of Work; the
database transaction retains that lock until commit or rollback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, select

from spine.application.commit_fence import (
    CommitFenceBinding,
    CommitFenceOutcome,
    CommitFenceOutcomeKind,
)
from spine.application.persistence.context import EnvironmentScope
from spine.infrastructure.persistence.postgresql_mappings import (
    access_policy_versions,
    current_access_policies,
    current_lifecycle_authorities,
    current_retention_applicabilities,
    lifecycle_decisions,
    retention_applicabilities,
    source_revisions,
)
from spine.infrastructure.persistence.postgresql_source_locks import (
    lock_source_lifecycle_shared,
)


class PostgreSQLFenceOwner(str, Enum):
    """Canonical PostgreSQL-owned state this narrow adapter can fence."""

    SOURCE_LIFECYCLE = "source_lifecycle"
    ACCESS_POLICY = "access_policy"
    RETENTION_APPLICABILITY = "retention_applicability"


def postgresql_f1_issuer_id(owner: PostgreSQLFenceOwner) -> UUID:
    """Stable code identity for a canonical PG owner, not an approval registry."""

    return uuid5(NAMESPACE_URL, f"spine.postgresql-f1-owner:v1:{owner.value}")


class PostgreSQLCommitFence:
    """Acquire an F1 fence inside an already active PostgreSQL UoW."""

    def __init__(
        self,
        uow: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow = uow
        self._clock = clock

    async def acquire(
        self,
        binding: CommitFenceBinding,
        *,
        owner: PostgreSQLFenceOwner,
        now: datetime,
    ) -> CommitFenceOutcome:
        """Lock and match current owner state, returning a transaction-bound permit.

        The permit is meaningful only while the caller's UoW remains open. The
        owner state is re-read after its canonical lock is acquired, and the
        caller must perform its protected write and commit through that same UoW.
        """

        if not binding.is_valid_at(now):
            return _outcome(binding, CommitFenceOutcomeKind.EXPIRED, now)
        try:
            scope = self._uow.scope
            if not isinstance(scope, EnvironmentScope) or (
                scope.workspace_id,
                scope.environment_id,
            ) != (binding.workspace_id, binding.environment_id):
                return _outcome(binding, CommitFenceOutcomeKind.DENIED, now)
            if not await self._lineage_is_current(binding):
                return _outcome(binding, CommitFenceOutcomeKind.STALE, now)
            row = await self._resolve_owner(binding, owner)
        except Exception:
            # An absent UoW, unknown owner state or adapter/query error never
            # becomes evidence of approval.
            return _outcome(binding, CommitFenceOutcomeKind.DENIED, now)
        if row is None:
            return _outcome(binding, CommitFenceOutcomeKind.DENIED, now)
        if not self._matches(binding, owner, row):
            return _outcome(binding, CommitFenceOutcomeKind.STALE, now)
        register_guard = getattr(self._uow, "register_commit_guard", None)
        if not callable(register_guard):
            return _outcome(binding, CommitFenceOutcomeKind.DENIED, now)
        register_guard(lambda: binding.is_valid_at(self._clock()))
        return CommitFenceOutcome(
            binding=binding,
            outcome=CommitFenceOutcomeKind.ACQUIRED,
            evaluated_at=now,
            permit_reference=uuid4(),
            permit_valid_until=binding.valid_until,
        )

    async def _lineage_is_current(self, binding: CommitFenceBinding) -> bool:
        for source in binding.source_lineage:
            await lock_source_lifecycle_shared(
                self._uow.execute_statement,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
                source_object_id=source.source_object_id,
            )
            result = await self._uow.execute_statement(
                select(source_revisions.c.revision_digest)
                .where(
                    source_revisions.c.workspace_id == binding.workspace_id,
                    source_revisions.c.environment_id == binding.environment_id,
                    source_revisions.c.source_object_id == source.source_object_id,
                    source_revisions.c.revision_id == source.source_revision_id,
                )
            )
            digest = result.scalar_one_or_none()
            if digest != source.source_revision_digest:
                return False
        return True

    async def _resolve_owner(
        self, binding: CommitFenceBinding, owner: PostgreSQLFenceOwner
    ) -> dict[str, Any] | None:
        if owner is PostgreSQLFenceOwner.SOURCE_LIFECYCLE:
            if binding.subject_id not in {
                item.source_object_id for item in binding.source_lineage
            }:
                return None
            await lock_source_lifecycle_shared(
                self._uow.execute_statement,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
                source_object_id=binding.subject_id,
            )
            result = await self._uow.execute_statement(
                select(
                    current_lifecycle_authorities.c.current_decision_id,
                    current_lifecycle_authorities.c.generation,
                    lifecycle_decisions.c.status,
                    lifecycle_decisions.c.record,
                )
                .select_from(
                    current_lifecycle_authorities.join(
                        lifecycle_decisions,
                        and_(
                            lifecycle_decisions.c.workspace_id
                            == current_lifecycle_authorities.c.workspace_id,
                            lifecycle_decisions.c.environment_id
                            == current_lifecycle_authorities.c.environment_id,
                            lifecycle_decisions.c.source_object_id
                            == current_lifecycle_authorities.c.source_object_id,
                            lifecycle_decisions.c.decision_id
                            == current_lifecycle_authorities.c.current_decision_id,
                            lifecycle_decisions.c.generation
                            == current_lifecycle_authorities.c.generation,
                        ),
                    )
                )
                .where(
                    current_lifecycle_authorities.c.workspace_id
                    == binding.workspace_id,
                    current_lifecycle_authorities.c.environment_id
                    == binding.environment_id,
                    current_lifecycle_authorities.c.source_object_id
                    == binding.subject_id,
                )
            )
            return result.mappings().one_or_none()

        if owner is PostgreSQLFenceOwner.ACCESS_POLICY:
            result = await self._uow.execute_statement(
                select(
                    current_access_policies.c.access_policy_version_id,
                    current_access_policies.c.policy_generation,
                    current_access_policies.c.cas_version,
                    access_policy_versions.c.document_hash,
                )
                .select_from(
                    current_access_policies.join(
                        access_policy_versions,
                        and_(
                            access_policy_versions.c.workspace_id
                            == current_access_policies.c.workspace_id,
                            access_policy_versions.c.environment_id
                            == current_access_policies.c.environment_id,
                            access_policy_versions.c.source_object_id
                            == current_access_policies.c.source_object_id,
                            access_policy_versions.c.access_policy_version_id
                            == current_access_policies.c.access_policy_version_id,
                        ),
                    )
                )
                .where(
                    current_access_policies.c.workspace_id == binding.workspace_id,
                    current_access_policies.c.environment_id == binding.environment_id,
                    current_access_policies.c.source_object_id == binding.subject_id,
                )
                .with_for_update(of=current_access_policies)
            )
            return result.mappings().one_or_none()

        if owner is PostgreSQLFenceOwner.RETENTION_APPLICABILITY:
            result = await self._uow.execute_statement(
                select(
                    current_retention_applicabilities.c.current_applicability_id,
                    current_retention_applicabilities.c.generation,
                    retention_applicabilities.c.record,
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
                    current_retention_applicabilities.c.workspace_id
                    == binding.workspace_id,
                    current_retention_applicabilities.c.environment_id
                    == binding.environment_id,
                    current_retention_applicabilities.c.target_kind == "exact_source",
                    current_retention_applicabilities.c.target_reference
                    == binding.subject_id,
                )
                .with_for_update(of=current_retention_applicabilities)
            )
            return result.mappings().one_or_none()
        return None

    @staticmethod
    def _matches(
        binding: CommitFenceBinding,
        owner: PostgreSQLFenceOwner,
        row: dict[str, Any],
    ) -> bool:
        if owner is PostgreSQLFenceOwner.SOURCE_LIFECYCLE:
            return (
                row["current_decision_id"] is not None
                and row["status"] == "eligible"
                and binding.issuer_id == postgresql_f1_issuer_id(owner)
                and str(row["current_decision_id"]) == binding.approval_version
                and hashlib.sha256(
                    _canonical_json(_json_object(row["record"]))
                ).hexdigest()
                == binding.approval_digest
                and hashlib.sha256(
                    _canonical_json(_json_object(row["record"]))
                ).hexdigest()
                == binding.state_digest
                and row["generation"] == binding.generation
                and row["generation"] == binding.revocation_epoch
            )
        if owner is PostgreSQLFenceOwner.ACCESS_POLICY:
            return (
                binding.issuer_id == postgresql_f1_issuer_id(owner)
                and str(row["access_policy_version_id"]) == binding.approval_version
                and row["document_hash"] == binding.approval_digest
                and row["document_hash"] == binding.state_digest
                and row["policy_generation"] == binding.generation
                and row["cas_version"] == binding.revocation_epoch
            )
        if owner is PostgreSQLFenceOwner.RETENTION_APPLICABILITY:
            record = _json_object(row["record"])
            return (
                binding.issuer_id == postgresql_f1_issuer_id(owner)
                and str(row["current_applicability_id"]) == binding.approval_version
                and hashlib.sha256(_canonical_json(record)).hexdigest()
                == binding.approval_digest
                and hashlib.sha256(_canonical_json(record)).hexdigest()
                == binding.state_digest
                and row["generation"] == binding.generation
                and row["generation"] == binding.revocation_epoch
            )
        return False


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, dict) else {}


def _outcome(
    binding: CommitFenceBinding,
    outcome: CommitFenceOutcomeKind,
    now: datetime,
) -> CommitFenceOutcome:
    return CommitFenceOutcome(
        binding=binding,
        outcome=outcome,
        evaluated_at=now,
    )


__all__ = [
    "PostgreSQLCommitFence",
    "PostgreSQLFenceOwner",
    "postgresql_f1_issuer_id",
]
