"""Same-UoW verifier for governed CanonicalAcceptance v3 tombstones."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable

from sqlalchemy import and_, select, text

from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.errors import CanonicalEligibilityError
from spine.domain.sources.canonical_tombstone_selector import (
    CanonicalTombstoneWriterMode,
    TrustedCanonicalTombstoneVersionSelector,
)
from spine.domain.sources.deletion_authorization import (
    QualifiedDeletionEvidence,
    SourceDeletionPolicyApproval,
    TombstoneAuthorization,
    TombstoneAuthorizationDecision,
    TombstoneAuthorizationDisposition,
    TombstoneAuthorizationIssuer,
)
from spine.domain.sources.lifecycle import (
    LifecycleDecision,
    LifecycleEligibilityStatus,
)
from spine.domain.sources.retention import RetentionApplicability, RetentionArtifactClass
from spine.infrastructure.connections.postgresql_provider_management import (
    provider_connection_approvals,
    provider_connection_versions,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    current_lifecycle_authorities,
    current_retention_applicabilities,
    current_source_deletion_policy_approvals,
    current_tombstone_authorization_decisions,
    current_tombstone_authorization_issuers,
    current_trusted_canonical_tombstone_selectors,
    lifecycle_decisions,
    qualified_deletion_evidence,
    retention_applicabilities,
    source_deletion_policy_approvals,
    source_objects,
    tombstone_authorization_decisions,
    tombstone_authorization_issuers,
    tombstone_authorizations,
    trusted_canonical_tombstone_selector_versions,
)
from spine.infrastructure.persistence.postgresql_source_locks import (
    lock_source_lifecycle_shared,
)


class PostgreSQLCurrentTombstoneAuthorizationAuthority:
    """Lock and re-resolve every mutable authority used by one v3 commit."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._clock = clock

    async def revalidate(
        self, command: CanonicalAcceptanceCommand, transaction: object
    ) -> None:
        authorization = command.tombstone_authorization
        scope = getattr(transaction, "scope", None)
        execute = getattr(transaction, "execute_statement", None)
        if not callable(execute):
            execute = getattr(transaction, "_execute", None)
        now = self._clock()
        if (
            not isinstance(scope, EnvironmentScope)
            or (scope.workspace_id, scope.environment_id)
            != (command.source.workspace_id, command.source.environment_id)
            or not callable(execute)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise CanonicalEligibilityError("Tombstone authority is unavailable.")

        if authorization is None:
            await self._require_legacy_selector(execute, command, now)
            return
        if not authorization.issued_at <= now < authorization.expires_at:
            raise CanonicalEligibilityError("Tombstone authority is unavailable.")

        await lock_source_lifecycle_shared(
            execute,
            workspace_id=authorization.workspace_id,
            environment_id=authorization.environment_id,
            source_object_id=authorization.source_object_id,
        )
        try:
            selector = await self._lock_selector(execute, authorization, now)
            persisted = await self._lock_authorization(execute, authorization)
            evidence = await self._evidence(execute, authorization)
            approval = await self._lock_policy(execute, authorization)
            issuer = await self._lock_issuer(execute, authorization)
            decision = await self._lock_decision(execute, authorization)
            connection_ok = await self._lock_connection(execute, authorization)
            lifecycle_ok = await self._lock_lifecycle(execute, authorization)
            retention_ok = await self._lock_retention(execute, authorization, now)
            source_ok = await self._source_identity(execute, authorization)
            if any(
                value is None
                for value in (selector, persisted, evidence, approval, issuer, decision)
            ):
                raise ValueError
            if persisted != authorization:
                raise ValueError
            authorization.validate_bindings(
                evidence=evidence,
                approval=approval,
                issuer=issuer,
                evaluated_at=now,
            )
            if (
                decision.disposition is not TombstoneAuthorizationDisposition.ISSUED
                or not connection_ok
                or not lifecycle_ok
                or not retention_ok
                or not source_ok
            ):
                raise ValueError
        except CanonicalEligibilityError:
            raise
        except (TypeError, ValueError):
            raise CanonicalEligibilityError(
                "Tombstone authority is unavailable."
            ) from None

    @staticmethod
    async def _require_legacy_selector(
        execute: Any, command: CanonicalAcceptanceCommand, now: datetime
    ) -> None:
        """Permit an old writer only when one active selector says LEGACY.

        Legacy commands predate the contract/scope selector key, so ambiguity is
        deliberately denied.  A connection with more than one active selector
        must migrate its writer to v3 before it can emit another tombstone.
        """

        selector_connection_id = (
            command.source.connection_id or command.source.source_object_id
        )
        result = await execute(
            select(trusted_canonical_tombstone_selector_versions.c.record)
            .select_from(
                current_trusted_canonical_tombstone_selectors.join(
                    trusted_canonical_tombstone_selector_versions,
                    and_(
                        current_trusted_canonical_tombstone_selectors.c.workspace_id
                        == trusted_canonical_tombstone_selector_versions.c.workspace_id,
                        current_trusted_canonical_tombstone_selectors.c.environment_id
                        == trusted_canonical_tombstone_selector_versions.c.environment_id,
                        current_trusted_canonical_tombstone_selectors.c.selector_series_id
                        == trusted_canonical_tombstone_selector_versions.c.selector_series_id,
                        current_trusted_canonical_tombstone_selectors.c.current_selector_id
                        == trusted_canonical_tombstone_selector_versions.c.selector_id,
                    ),
                )
            )
            .where(
                current_trusted_canonical_tombstone_selectors.c.workspace_id
                == command.source.workspace_id,
                current_trusted_canonical_tombstone_selectors.c.environment_id
                == command.source.environment_id,
                trusted_canonical_tombstone_selector_versions.c.source_object_id
                == command.source.source_object_id,
                trusted_canonical_tombstone_selector_versions.c.connection_id
                == selector_connection_id,
            )
            .with_for_update(of=current_trusted_canonical_tombstone_selectors)
        )
        selectors = [
            TrustedCanonicalTombstoneVersionSelector.model_validate(record)
            for record in result.scalars().all()
        ]
        if (
            len(selectors) != 1
            or selectors[0].writer_mode_at(now)
            is not CanonicalTombstoneWriterMode.LEGACY_V1_ALLOWED
        ):
            raise CanonicalEligibilityError("Tombstone authority is unavailable.")

    @staticmethod
    async def _lock_selector(execute: Any, authorization: TombstoneAuthorization, now: datetime):
        result = await execute(
            select(trusted_canonical_tombstone_selector_versions.c.record)
            .select_from(
                current_trusted_canonical_tombstone_selectors.join(
                    trusted_canonical_tombstone_selector_versions,
                    and_(
                        current_trusted_canonical_tombstone_selectors.c.workspace_id
                        == trusted_canonical_tombstone_selector_versions.c.workspace_id,
                        current_trusted_canonical_tombstone_selectors.c.environment_id
                        == trusted_canonical_tombstone_selector_versions.c.environment_id,
                        current_trusted_canonical_tombstone_selectors.c.selector_series_id
                        == trusted_canonical_tombstone_selector_versions.c.selector_series_id,
                        current_trusted_canonical_tombstone_selectors.c.current_selector_id
                        == trusted_canonical_tombstone_selector_versions.c.selector_id,
                    ),
                )
            )
            .where(
                current_trusted_canonical_tombstone_selectors.c.workspace_id
                == authorization.workspace_id,
                current_trusted_canonical_tombstone_selectors.c.environment_id
                == authorization.environment_id,
                trusted_canonical_tombstone_selector_versions.c.source_object_id
                == authorization.source_object_id,
                trusted_canonical_tombstone_selector_versions.c.source_contract_id
                == authorization.source_contract_id,
                trusted_canonical_tombstone_selector_versions.c.source_contract_version
                == authorization.source_contract_version,
                trusted_canonical_tombstone_selector_versions.c.provider_kind
                == authorization.provider_kind,
                trusted_canonical_tombstone_selector_versions.c.connection_id
                == authorization.connection_id,
                trusted_canonical_tombstone_selector_versions.c.resource_scope_reference
                == authorization.resource_scope_reference,
            )
            .with_for_update(of=current_trusted_canonical_tombstone_selectors)
        )
        rows = result.scalars().all()
        if len(rows) != 1:
            return None
        selector = TrustedCanonicalTombstoneVersionSelector.model_validate(rows[0])
        return (
            selector
            if selector.writer_mode_at(now) is CanonicalTombstoneWriterMode.V3_REQUIRED
            else None
        )

    @staticmethod
    async def _lock_authorization(execute: Any, authorization: TombstoneAuthorization):
        record = (
            await execute(
                select(tombstone_authorizations.c.record)
                .where(
                    tombstone_authorizations.c.workspace_id == authorization.workspace_id,
                    tombstone_authorizations.c.environment_id == authorization.environment_id,
                    tombstone_authorizations.c.authorization_id == authorization.authorization_id,
                    tombstone_authorizations.c.source_object_id == authorization.source_object_id,
                    tombstone_authorizations.c.command_digest == authorization.command_digest,
                    tombstone_authorizations.c.transition_id == authorization.transition_id,
                )
            )
        ).scalar_one_or_none()
        return _model(TombstoneAuthorization, record)

    @staticmethod
    async def _evidence(execute: Any, authorization: TombstoneAuthorization):
        record = (
            await execute(
                select(qualified_deletion_evidence.c.record).where(
                    qualified_deletion_evidence.c.workspace_id == authorization.workspace_id,
                    qualified_deletion_evidence.c.environment_id == authorization.environment_id,
                    qualified_deletion_evidence.c.evidence_id == authorization.qualified_evidence_id,
                    qualified_deletion_evidence.c.source_object_id == authorization.source_object_id,
                    qualified_deletion_evidence.c.evidence_digest == authorization.qualified_evidence_digest,
                )
            )
        ).scalar_one_or_none()
        return _model(QualifiedDeletionEvidence, record)

    @staticmethod
    async def _lock_policy(execute: Any, authorization: TombstoneAuthorization):
        result = await execute(
            select(source_deletion_policy_approvals.c.record)
            .select_from(
                current_source_deletion_policy_approvals.join(
                    source_deletion_policy_approvals,
                    and_(
                        source_deletion_policy_approvals.c.workspace_id
                        == current_source_deletion_policy_approvals.c.workspace_id,
                        source_deletion_policy_approvals.c.environment_id
                        == current_source_deletion_policy_approvals.c.environment_id,
                        source_deletion_policy_approvals.c.approval_id
                        == current_source_deletion_policy_approvals.c.current_approval_id,
                    ),
                )
            )
            .where(
                current_source_deletion_policy_approvals.c.workspace_id == authorization.workspace_id,
                current_source_deletion_policy_approvals.c.environment_id == authorization.environment_id,
                current_source_deletion_policy_approvals.c.source_contract_id == authorization.source_contract_id,
                current_source_deletion_policy_approvals.c.source_contract_version == authorization.source_contract_version,
                current_source_deletion_policy_approvals.c.connection_id == authorization.connection_id,
                current_source_deletion_policy_approvals.c.resource_scope_reference == authorization.resource_scope_reference,
                current_source_deletion_policy_approvals.c.current_approval_id == authorization.deletion_policy_approval_id,
                current_source_deletion_policy_approvals.c.generation == authorization.deletion_policy_authority_generation,
            )
            .with_for_update(of=current_source_deletion_policy_approvals)
        )
        return _model(SourceDeletionPolicyApproval, result.scalar_one_or_none())

    @staticmethod
    async def _lock_issuer(execute: Any, authorization: TombstoneAuthorization):
        result = await execute(
            select(tombstone_authorization_issuers.c.record)
            .select_from(
                current_tombstone_authorization_issuers.join(
                    tombstone_authorization_issuers,
                    and_(
                        tombstone_authorization_issuers.c.workspace_id
                        == current_tombstone_authorization_issuers.c.workspace_id,
                        tombstone_authorization_issuers.c.environment_id
                        == current_tombstone_authorization_issuers.c.environment_id,
                        tombstone_authorization_issuers.c.issuer_authority_id
                        == current_tombstone_authorization_issuers.c.current_issuer_authority_id,
                    ),
                )
            )
            .where(
                current_tombstone_authorization_issuers.c.workspace_id == authorization.workspace_id,
                current_tombstone_authorization_issuers.c.environment_id == authorization.environment_id,
                current_tombstone_authorization_issuers.c.current_issuer_authority_id == authorization.issuer_authority_id,
                current_tombstone_authorization_issuers.c.generation == authorization.issuer_authority_generation,
            )
            .with_for_update(of=current_tombstone_authorization_issuers)
        )
        return _model(TombstoneAuthorizationIssuer, result.scalar_one_or_none())

    @staticmethod
    async def _lock_decision(execute: Any, authorization: TombstoneAuthorization):
        result = await execute(
            select(tombstone_authorization_decisions.c.record)
            .select_from(
                current_tombstone_authorization_decisions.join(
                    tombstone_authorization_decisions,
                    and_(
                        tombstone_authorization_decisions.c.workspace_id
                        == current_tombstone_authorization_decisions.c.workspace_id,
                        tombstone_authorization_decisions.c.environment_id
                        == current_tombstone_authorization_decisions.c.environment_id,
                        tombstone_authorization_decisions.c.authorization_id
                        == current_tombstone_authorization_decisions.c.authorization_id,
                        tombstone_authorization_decisions.c.decision_id
                        == current_tombstone_authorization_decisions.c.current_decision_id,
                    ),
                )
            )
            .where(
                current_tombstone_authorization_decisions.c.workspace_id == authorization.workspace_id,
                current_tombstone_authorization_decisions.c.environment_id == authorization.environment_id,
                current_tombstone_authorization_decisions.c.authorization_id == authorization.authorization_id,
            )
            .with_for_update(of=current_tombstone_authorization_decisions)
        )
        return _model(TombstoneAuthorizationDecision, result.scalar_one_or_none())

    @staticmethod
    async def _lock_connection(execute: Any, authorization: TombstoneAuthorization) -> bool:
        await execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:lock, 0))"),
            {"lock": str(authorization.connection_id)},
        )
        row = (
            await execute(
                select(provider_connection_versions)
                .where(
                    provider_connection_versions.c.workspace_id == authorization.workspace_id,
                    provider_connection_versions.c.environment_id == authorization.environment_id,
                    provider_connection_versions.c.connection_id == authorization.connection_id,
                )
                .order_by(provider_connection_versions.c.version.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None:
            return False
        approval = (
            await execute(
                select(provider_connection_approvals)
                .where(
                    provider_connection_approvals.c.workspace_id == authorization.workspace_id,
                    provider_connection_approvals.c.environment_id == authorization.environment_id,
                    provider_connection_approvals.c.approval_id == row.approval_id,
                )
            )
        ).mappings().one_or_none()
        return bool(
            approval is not None
            and row.connection_version_id == authorization.connection_version_id
            and row.provider_kind == authorization.provider_kind
            and row.status == "active"
            and row.authorization_generation == authorization.authorization_generation
            and row.configuration_generation == authorization.configuration_generation
            and approval.status == "active"
            and authorization.resource_scope_reference in row.approved_resource_scope_references
            and authorization.resource_scope_reference in approval.approved_resource_scope_references
            and approval.authorization_generation == authorization.authorization_generation
            and approval.configuration_generation == authorization.configuration_generation
        )

    @staticmethod
    async def _lock_lifecycle(execute: Any, authorization: TombstoneAuthorization) -> bool:
        row = (
            await execute(
                select(lifecycle_decisions.c.record)
                .select_from(
                    current_lifecycle_authorities.join(
                        lifecycle_decisions,
                        and_(
                            lifecycle_decisions.c.workspace_id == current_lifecycle_authorities.c.workspace_id,
                            lifecycle_decisions.c.environment_id == current_lifecycle_authorities.c.environment_id,
                            lifecycle_decisions.c.source_object_id == current_lifecycle_authorities.c.source_object_id,
                            lifecycle_decisions.c.decision_id == current_lifecycle_authorities.c.current_decision_id,
                            lifecycle_decisions.c.generation == current_lifecycle_authorities.c.generation,
                        ),
                    )
                )
                .where(
                    current_lifecycle_authorities.c.workspace_id == authorization.workspace_id,
                    current_lifecycle_authorities.c.environment_id == authorization.environment_id,
                    current_lifecycle_authorities.c.source_object_id == authorization.source_object_id,
                    current_lifecycle_authorities.c.current_decision_id == authorization.legal_hold_check_reference,
                    current_lifecycle_authorities.c.generation == authorization.legal_hold_generation,
                )
                .with_for_update(of=current_lifecycle_authorities)
            )
        ).scalar_one_or_none()
        decision = _model(LifecycleDecision, row)
        return bool(
            decision is not None
            and not decision.legal_hold_active
            and decision.status is LifecycleEligibilityStatus.ELIGIBLE
        )

    @staticmethod
    async def _lock_retention(execute: Any, authorization: TombstoneAuthorization, now: datetime) -> bool:
        row = (
            await execute(
                select(retention_applicabilities.c.record)
                .select_from(
                    current_retention_applicabilities.join(
                        retention_applicabilities,
                        and_(
                            retention_applicabilities.c.workspace_id == current_retention_applicabilities.c.workspace_id,
                            retention_applicabilities.c.environment_id == current_retention_applicabilities.c.environment_id,
                            retention_applicabilities.c.target_kind == current_retention_applicabilities.c.target_kind,
                            retention_applicabilities.c.target_reference == current_retention_applicabilities.c.target_reference,
                            retention_applicabilities.c.applicability_id == current_retention_applicabilities.c.current_applicability_id,
                            retention_applicabilities.c.generation == current_retention_applicabilities.c.generation,
                        ),
                    )
                )
                .where(
                    current_retention_applicabilities.c.workspace_id == authorization.workspace_id,
                    current_retention_applicabilities.c.environment_id == authorization.environment_id,
                    current_retention_applicabilities.c.target_kind == "exact_source",
                    current_retention_applicabilities.c.target_reference == authorization.source_object_id,
                    current_retention_applicabilities.c.current_applicability_id == authorization.retention_eligibility_reference,
                    current_retention_applicabilities.c.generation == authorization.retention_eligibility_generation,
                )
                .with_for_update(of=current_retention_applicabilities)
            )
        ).scalar_one_or_none()
        binding = _model(RetentionApplicability, row)
        return bool(
            binding is not None
            and RetentionArtifactClass.ORIGINAL in binding.artifact_classes
            and not binding.legal_hold_references
            and binding.effective_from <= now
            and (binding.effective_until is None or now < binding.effective_until)
        )

    @staticmethod
    async def _source_identity(execute: Any, authorization: TombstoneAuthorization) -> bool:
        row = (
            await execute(
                select(source_objects).where(
                    source_objects.c.workspace_id == authorization.workspace_id,
                    source_objects.c.environment_id == authorization.environment_id,
                    source_objects.c.source_object_id == authorization.source_object_id,
                )
            )
        ).mappings().one_or_none()
        return bool(
            row is not None
            and row.connection_id == authorization.connection_id
            and row.external_namespace == authorization.object_namespace
            and row.external_object_id == authorization.object_identity
        )


def _model(model: Any, value: object):
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return model.model_validate(value)


__all__ = ["PostgreSQLCurrentTombstoneAuthorizationAuthority"]
