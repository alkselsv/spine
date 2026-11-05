"""PostgreSQL authority store for qualified deletion and tombstone issuance."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.tombstone_authorization import (
    TombstoneAuthorizationOutcome,
    TombstoneAuthorizationResult,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources.canonical import CurrentSourceHead
from spine.domain.sources.deletion_authorization import (
    QualifiedDeletionEvidence,
    SourceDeletionPolicyApproval,
    TombstoneAuthorization,
    TombstoneAuthorizationDecision,
    TombstoneAuthorizationDisposition,
    TombstoneAuthorizationIssuer,
)
from spine.domain.sources.models import RevisionKind
from spine.infrastructure.persistence.postgresql_mappings import (
    current_source_deletion_policy_approvals,
    current_source_heads,
    current_tombstone_authorization_decisions,
    current_tombstone_authorization_issuers,
    qualified_deletion_evidence,
    source_deletion_policy_approvals,
    tombstone_authorization_decisions,
    tombstone_authorization_issuers,
    tombstone_authorizations,
)
class TombstoneAuthorizationEffectsWriter(Protocol):
    async def persist_policy_approval(
        self,
        approval: SourceDeletionPolicyApproval,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> UUID: ...

    async def persist_issuer(
        self,
        issuer: TombstoneAuthorizationIssuer,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> UUID: ...

    async def persist_authorization(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> UUID: ...

    async def persist_decision(
        self,
        decision: TombstoneAuthorizationDecision,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> UUID: ...


class TombstoneAuthorizationCommitVerifier(Protocol):
    """Verify connection/scope, lifecycle, retention and legal authority in-UoW."""

    async def verify(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> bool: ...


class PostgreSQLTombstoneAuthorizationRepository:
    def __init__(
        self,
        *,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        effects: TombstoneAuthorizationEffectsWriter,
        commit_verifier: TombstoneAuthorizationCommitVerifier,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._effects = effects
        self._commit_verifier = commit_verifier
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def record_evidence(
        self, evidence: QualifiedDeletionEvidence, context: TrustedPersistenceContext
    ) -> QualifiedDeletionEvidence:
        if not (
            _worker_scope(context, evidence.workspace_id, evidence.environment_id)
            and context.service_principal_id
            == evidence.qualifier_service_principal_id
        ):
            raise ValueError("qualified deletion evidence authority is unavailable")
        try:
            async with self._uow_factory(context) as uow:
                existing = await _evidence_by_id(uow, evidence.evidence_id)
                if existing is not None:
                    await uow.rollback()
                    if existing != evidence:
                        raise ValueError("qualified deletion evidence conflicts")
                    return existing
                authority = await _current_qualification_authority(
                    uow, evidence, context
                )
                if authority is None:
                    raise ValueError(
                        "qualified deletion evidence authority is unavailable"
                    )
                await uow._execute(
                    insert(qualified_deletion_evidence).values(
                        **_evidence_columns(evidence)
                    )
                )
                await uow.commit()
                return evidence
        except (IntegrityError, ConstraintConflictError):
            raise ValueError(
                "qualified deletion evidence authority is unavailable"
            ) from None

    async def record_policy_approval(
        self, approval: SourceDeletionPolicyApproval, context: TrustedPersistenceContext
    ) -> SourceDeletionPolicyApproval:
        if not _interactive_scope(
            context,
            approval.workspace_id,
            approval.environment_id,
            approval.approver_human_id,
        ):
            raise ValueError("source deletion policy approval is unavailable")
        try:
            async with self._uow_factory(context) as uow:
                existing = await _approval_by_id(uow, approval.approval_id)
                if existing is not None:
                    await uow.rollback()
                    if existing != approval:
                        raise ValueError("source deletion policy approval conflicts")
                    return existing
                pointer_result = await uow._execute(
                    select(current_source_deletion_policy_approvals)
                    .where(*_policy_series_predicates(approval))
                    .with_for_update()
                )
                pointer = pointer_result.mappings().one_or_none()
                current_generation = pointer["generation"] if pointer else 0
                current_approval_id = (
                    pointer["current_approval_id"] if pointer else None
                )
                human_generation = await _current_human_authority_generation(
                    uow,
                    approval.approver_human_id,
                    approval.workspace_id,
                    approval.environment_id,
                )
                if (
                    approval.authority_generation != current_generation + 1
                    or approval.predecessor_approval_id != current_approval_id
                    or approval.approver_authority_reference
                    != approval.approver_human_id
                    or approval.approver_authority_generation != human_generation
                ):
                    raise ValueError("source deletion policy approval head conflicts")
                audit_id = await self._effects.persist_policy_approval(
                    approval, context, uow
                )
                if audit_id != approval.audit_event_id:
                    raise ValueError(
                        "source deletion policy approval audit is unavailable"
                    )
                await uow._execute(
                    insert(source_deletion_policy_approvals).values(
                        **_approval_columns(approval)
                    )
                )
                await _write_policy_pointer(uow, approval, pointer is None)
                await uow.commit()
                return approval
        except (IntegrityError, ConstraintConflictError):
            raise ValueError("source deletion policy approval is unavailable") from None

    async def record_issuer(
        self, issuer: TombstoneAuthorizationIssuer, context: TrustedPersistenceContext
    ) -> TombstoneAuthorizationIssuer:
        if not _interactive_scope(
            context,
            issuer.workspace_id,
            issuer.environment_id,
            issuer.authorized_by_human_id,
        ):
            raise ValueError("trusted tombstone issuer authority is unavailable")
        try:
            async with self._uow_factory(context) as uow:
                existing = await _issuer_by_id(uow, issuer.issuer_authority_id)
                if existing is not None:
                    await uow.rollback()
                    if existing != issuer:
                        raise ValueError("trusted tombstone issuer authority conflicts")
                    return existing
                pointer_result = await uow._execute(
                    select(current_tombstone_authorization_issuers)
                    .where(
                        current_tombstone_authorization_issuers.c.workspace_id
                        == issuer.workspace_id,
                        current_tombstone_authorization_issuers.c.environment_id
                        == issuer.environment_id,
                    )
                    .with_for_update()
                )
                pointer = pointer_result.mappings().one_or_none()
                current_generation = pointer["generation"] if pointer else 0
                current_issuer_id = (
                    pointer["current_issuer_authority_id"] if pointer else None
                )
                human_generation = await _current_human_authority_generation(
                    uow,
                    issuer.authorized_by_human_id,
                    issuer.workspace_id,
                    issuer.environment_id,
                )
                if (
                    issuer.authority_generation != current_generation + 1
                    or issuer.predecessor_issuer_authority_id != current_issuer_id
                    or issuer.authorized_by_authority_generation != human_generation
                ):
                    raise ValueError("trusted tombstone issuer head conflicts")
                audit_id = await self._effects.persist_issuer(issuer, context, uow)
                if audit_id != issuer.audit_event_id:
                    raise ValueError("trusted tombstone issuer audit is unavailable")
                await uow._execute(
                    insert(tombstone_authorization_issuers).values(
                        **_issuer_columns(issuer)
                    )
                )
                await _write_issuer_pointer(uow, issuer, pointer is None)
                await uow.commit()
                return issuer
        except (IntegrityError, ConstraintConflictError):
            raise ValueError(
                "trusted tombstone issuer authority is unavailable"
            ) from None

    async def replay(
        self, authorization: TombstoneAuthorization, context: TrustedPersistenceContext
    ) -> TombstoneAuthorizationResult | None:
        if not _scope(
            context, authorization.workspace_id, authorization.environment_id
        ):
            return _denied()
        async with self._uow_factory(context) as uow:
            result = await _authorization_by_command(uow, authorization.idempotency_key)
            await uow.rollback()
        return _replay_result(result, authorization)

    async def issue(
        self, authorization: TombstoneAuthorization, context: TrustedPersistenceContext
    ) -> TombstoneAuthorizationResult:
        if not _trusted_issuer(context, authorization):
            return _denied()
        try:
            async with self._uow_factory(context) as uow:
                existing = await _authorization_by_command(
                    uow, authorization.idempotency_key
                )
                replay = _replay_result(existing, authorization)
                if replay is not None:
                    await uow.rollback()
                    return replay
                head_result = await uow._execute(
                    select(current_source_heads)
                    .where(
                        current_source_heads.c.workspace_id
                        == authorization.workspace_id,
                        current_source_heads.c.environment_id
                        == authorization.environment_id,
                        current_source_heads.c.source_object_id
                        == authorization.source_object_id,
                    )
                    .with_for_update()
                )
                head_row = head_result.mappings().one_or_none()
                evidence = await _evidence_by_id(
                    uow, authorization.qualified_evidence_id
                )
                approval = await _current_approval(uow, authorization)
                issuer = await _current_issuer(
                    uow, authorization.workspace_id, authorization.environment_id
                )
                now = self._clock()
                if (
                    head_row is None
                    or evidence is None
                    or approval is None
                    or issuer is None
                ):
                    return await _rollback_denied(uow)
                head = CurrentSourceHead.model_validate(dict(head_row))
                authorization.validate_bindings(
                    evidence=evidence,
                    approval=approval,
                    issuer=issuer,
                    evaluated_at=now,
                )
                authorization.validate_expected_head(
                    revision_id=head.current_revision_id,
                    acceptance_id=head.acceptance_id,
                    cas_version=head.cas_version,
                )
                if (
                    head.current_revision_kind is RevisionKind.TOMBSTONE
                    or not authorization.issued_at <= now < authorization.expires_at
                    or not await self._commit_verifier.verify(
                        authorization, context, uow
                    )
                ):
                    return await _rollback_denied(uow)
                audit_id = await self._effects.persist_authorization(
                    authorization, context, uow
                )
                if audit_id != authorization.audit_event_id:
                    return await _rollback_denied(uow)
                baseline = TombstoneAuthorizationDecision.issued(authorization)
                await uow._execute(
                    insert(tombstone_authorizations).values(
                        **_authorization_columns(authorization)
                    )
                )
                await uow._execute(
                    insert(tombstone_authorization_decisions).values(
                        **_decision_columns(baseline)
                    )
                )
                await uow._execute(
                    insert(current_tombstone_authorization_decisions).values(
                        workspace_id=authorization.workspace_id,
                        environment_id=authorization.environment_id,
                        authorization_id=authorization.authorization_id,
                        current_decision_id=baseline.decision_id,
                        generation=baseline.generation,
                        updated_at=baseline.decided_at,
                    )
                )
                await uow.commit()
                return TombstoneAuthorizationResult(
                    TombstoneAuthorizationOutcome.ISSUED, authorization
                )
        except Exception:
            try:
                replay = await self.replay(authorization, context)
                return replay if replay is not None else _denied()
            except Exception:
                return _denied()

    async def record_decision(
        self,
        decision: TombstoneAuthorizationDecision,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationDecision:
        if not _scope(context, decision.workspace_id, decision.environment_id):
            raise ValueError(
                "tombstone authorization decision authority is unavailable"
            )
        try:
            async with self._uow_factory(context) as uow:
                existing = await _decision_by_id(uow, decision.decision_id)
                if existing is not None:
                    await uow.rollback()
                    if existing != decision:
                        raise ValueError("tombstone authorization decision conflicts")
                    return existing
                authorization = await _authorization_by_id(
                    uow, decision.authorization_id
                )
                issuer = await _current_issuer(
                    uow, decision.workspace_id, decision.environment_id
                )
                pointer_result = await uow._execute(
                    select(current_tombstone_authorization_decisions)
                    .where(
                        current_tombstone_authorization_decisions.c.workspace_id
                        == decision.workspace_id,
                        current_tombstone_authorization_decisions.c.environment_id
                        == decision.environment_id,
                        current_tombstone_authorization_decisions.c.authorization_id
                        == decision.authorization_id,
                    )
                    .with_for_update()
                )
                pointer = pointer_result.mappings().one_or_none()
                current = (
                    None
                    if pointer is None
                    else await _decision_by_id(uow, pointer["current_decision_id"])
                )
                if (
                    authorization is None
                    or current is None
                    or issuer is None
                    or not _worker_scope(
                        context, decision.workspace_id, decision.environment_id
                    )
                    or context.service_principal_id != issuer.service_principal_id
                    or (
                        decision.source_object_id,
                        decision.expected_current_decision_id,
                        decision.expected_current_generation,
                        decision.authority_reference,
                        decision.authority_generation,
                    )
                    != (
                        authorization.source_object_id,
                        current.decision_id,
                        current.generation,
                        issuer.issuer_authority_id,
                        issuer.authority_generation,
                    )
                    or current.disposition
                    is not TombstoneAuthorizationDisposition.ISSUED
                ):
                    raise ValueError("tombstone authorization decision head conflicts")
                audit_id = await self._effects.persist_decision(decision, context, uow)
                if audit_id != decision.audit_event_id:
                    raise ValueError(
                        "tombstone authorization decision audit is unavailable"
                    )
                await uow._execute(
                    insert(tombstone_authorization_decisions).values(
                        **_decision_columns(decision)
                    )
                )
                updated = await uow._execute(
                    update(current_tombstone_authorization_decisions)
                    .where(
                        current_tombstone_authorization_decisions.c.workspace_id
                        == decision.workspace_id,
                        current_tombstone_authorization_decisions.c.environment_id
                        == decision.environment_id,
                        current_tombstone_authorization_decisions.c.authorization_id
                        == decision.authorization_id,
                        current_tombstone_authorization_decisions.c.current_decision_id
                        == decision.expected_current_decision_id,
                        current_tombstone_authorization_decisions.c.generation
                        == decision.expected_current_generation,
                    )
                    .values(
                        current_decision_id=decision.decision_id,
                        generation=decision.generation,
                        updated_at=decision.decided_at,
                    )
                    .returning(
                        current_tombstone_authorization_decisions.c.current_decision_id
                    )
                )
                if updated.scalar_one_or_none() != decision.decision_id:
                    raise ValueError("tombstone authorization decision head conflicts")
                await uow.commit()
                return decision
        except (IntegrityError, ConstraintConflictError):
            raise ValueError(
                "tombstone authorization decision authority is unavailable"
            ) from None

    async def current_decision(
        self, authorization_id: UUID, context: TrustedPersistenceContext
    ) -> TombstoneAuthorizationDecision | None:
        if not isinstance(context.scope, EnvironmentScope):
            return None
        try:
            context.validate_shape()
            async with self._uow_factory(context) as uow:
                pointer_result = await uow._execute(
                    select(
                        current_tombstone_authorization_decisions.c.current_decision_id
                    ).where(
                        current_tombstone_authorization_decisions.c.workspace_id
                        == context.scope.workspace_id,
                        current_tombstone_authorization_decisions.c.environment_id
                        == context.scope.environment_id,
                        current_tombstone_authorization_decisions.c.authorization_id
                        == authorization_id,
                    )
                )
                decision_id = pointer_result.scalar_one_or_none()
                decision = (
                    None
                    if decision_id is None
                    else await _decision_by_id(uow, decision_id)
                )
                await uow.rollback()
                return decision
        except Exception:
            return None

    async def is_current(
        self, authorization_id: UUID, context: TrustedPersistenceContext
    ) -> bool:
        decision = await self.current_decision(authorization_id, context)
        return (
            decision is not None
            and decision.disposition is TombstoneAuthorizationDisposition.ISSUED
        )

    async def resolve_evidence(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> QualifiedDeletionEvidence | None:
        if not _scope(
            context, authorization.workspace_id, authorization.environment_id
        ):
            return None
        async with self._uow_factory(context) as uow:
            value = await _evidence_by_id(uow, authorization.qualified_evidence_id)
            await uow.rollback()
            return value

    async def resolve_current_policy_approval(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> SourceDeletionPolicyApproval | None:
        if not _scope(
            context, authorization.workspace_id, authorization.environment_id
        ):
            return None
        async with self._uow_factory(context) as uow:
            value = await _current_approval(uow, authorization)
            await uow.rollback()
            return value

    async def resolve_current_issuer(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> TombstoneAuthorizationIssuer | None:
        if not _scope(
            context, authorization.workspace_id, authorization.environment_id
        ):
            return None
        async with self._uow_factory(context) as uow:
            value = await _current_issuer(
                uow, authorization.workspace_id, authorization.environment_id
            )
            await uow.rollback()
            return value

    async def resolve_current_head(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> CurrentSourceHead | None:
        if not _scope(
            context, authorization.workspace_id, authorization.environment_id
        ):
            return None
        async with self._uow_factory(context) as uow:
            result = await uow._execute(
                select(current_source_heads).where(
                    current_source_heads.c.workspace_id == authorization.workspace_id,
                    current_source_heads.c.environment_id
                    == authorization.environment_id,
                    current_source_heads.c.source_object_id
                    == authorization.source_object_id,
                )
            )
            row = result.mappings().one_or_none()
            await uow.rollback()
            return None if row is None else CurrentSourceHead.model_validate(dict(row))

    async def retention_and_legal_eligible(
        self,
        authorization: TombstoneAuthorization,
        context: TrustedPersistenceContext,
    ) -> bool:
        if not _scope(
            context, authorization.workspace_id, authorization.environment_id
        ):
            return False
        try:
            async with self._uow_factory(context) as uow:
                eligible = await self._commit_verifier.verify(
                    authorization, context, uow
                )
                await uow.rollback()
                return bool(eligible)
        except Exception:
            return False


async def _write_policy_pointer(uow, approval, create: bool) -> None:
    values = dict(
        workspace_id=approval.workspace_id,
        environment_id=approval.environment_id,
        source_contract_id=approval.source_contract_id,
        source_contract_version=approval.source_contract_version,
        connection_id=approval.connection_id,
        resource_scope_reference=approval.resource_scope_reference,
        current_approval_id=approval.approval_id,
        generation=approval.authority_generation,
        updated_at=approval.approved_at,
    )
    statement = (
        insert(current_source_deletion_policy_approvals).values(**values)
        if create
        else (
            update(current_source_deletion_policy_approvals)
            .where(
                *_policy_series_predicates(approval),
                current_source_deletion_policy_approvals.c.generation
                == approval.authority_generation - 1,
            )
            .values(
                current_approval_id=approval.approval_id,
                generation=approval.authority_generation,
                updated_at=approval.approved_at,
            )
        )
    )
    await uow._execute(statement)


async def _write_issuer_pointer(uow, issuer, create: bool) -> None:
    values = dict(
        workspace_id=issuer.workspace_id,
        environment_id=issuer.environment_id,
        current_issuer_authority_id=issuer.issuer_authority_id,
        generation=issuer.authority_generation,
        updated_at=issuer.valid_from,
    )
    statement = (
        insert(current_tombstone_authorization_issuers).values(**values)
        if create
        else (
            update(current_tombstone_authorization_issuers)
            .where(
                current_tombstone_authorization_issuers.c.workspace_id
                == issuer.workspace_id,
                current_tombstone_authorization_issuers.c.environment_id
                == issuer.environment_id,
                current_tombstone_authorization_issuers.c.generation
                == issuer.authority_generation - 1,
            )
            .values(
                current_issuer_authority_id=issuer.issuer_authority_id,
                generation=issuer.authority_generation,
                updated_at=issuer.valid_from,
            )
        )
    )
    await uow._execute(statement)


def _policy_series_predicates(value):
    return (
        current_source_deletion_policy_approvals.c.workspace_id == value.workspace_id,
        current_source_deletion_policy_approvals.c.environment_id
        == value.environment_id,
        current_source_deletion_policy_approvals.c.source_contract_id
        == value.source_contract_id,
        current_source_deletion_policy_approvals.c.source_contract_version
        == value.source_contract_version,
        current_source_deletion_policy_approvals.c.connection_id == value.connection_id,
        current_source_deletion_policy_approvals.c.resource_scope_reference
        == value.resource_scope_reference,
    )


async def _evidence_by_id(uow, evidence_id):
    result = await uow._execute(
        select(qualified_deletion_evidence.c.record).where(
            qualified_deletion_evidence.c.evidence_id == evidence_id
        )
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else QualifiedDeletionEvidence.model_validate_json(json.dumps(record))
    )


async def _current_qualification_authority(
    uow: object,
    evidence: QualifiedDeletionEvidence,
    context: TrustedPersistenceContext,
) -> object | None:
    result = await uow._execute(
        text(
            "SELECT binding_id, version "
            "FROM spine.resolve_current_deletion_qualification_authority("
            ":principal_id, :workspace_id, :environment_id, :purpose, :operation)"
        ),
        {
            "principal_id": str(evidence.qualifier_service_principal_id),
            "workspace_id": str(evidence.workspace_id),
            "environment_id": str(evidence.environment_id),
            "purpose": context.purpose.value,
            "operation": context.operation.value,
        },
    )
    row = result.one_or_none()
    if row is None or tuple(row) != (
        evidence.qualification_authority_reference,
        evidence.qualification_authority_generation,
    ):
        return None
    return row


async def _current_human_authority_generation(
    uow: object,
    human_id: UUID,
    workspace_id: UUID,
    environment_id: UUID,
) -> int | None:
    result = await uow._execute(
        text(
            "SELECT spine.resolve_current_human_authority("
            ":human_id, :workspace_id, :environment_id)"
        ),
        {
            "human_id": str(human_id),
            "workspace_id": str(workspace_id),
            "environment_id": str(environment_id),
        },
    )
    return result.scalar_one_or_none()


async def _approval_by_id(uow, approval_id):
    result = await uow._execute(
        select(source_deletion_policy_approvals.c.record).where(
            source_deletion_policy_approvals.c.approval_id == approval_id
        )
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else SourceDeletionPolicyApproval.model_validate_json(json.dumps(record))
    )


async def _issuer_by_id(uow, issuer_id):
    result = await uow._execute(
        select(tombstone_authorization_issuers.c.record).where(
            tombstone_authorization_issuers.c.issuer_authority_id == issuer_id
        )
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else TombstoneAuthorizationIssuer.model_validate_json(json.dumps(record))
    )


async def _current_approval(uow, authorization):
    result = await uow._execute(
        select(source_deletion_policy_approvals.c.record)
        .select_from(
            current_source_deletion_policy_approvals.join(
                source_deletion_policy_approvals,
                (
                    source_deletion_policy_approvals.c.workspace_id
                    == current_source_deletion_policy_approvals.c.workspace_id
                )
                & (
                    source_deletion_policy_approvals.c.environment_id
                    == current_source_deletion_policy_approvals.c.environment_id
                )
                & (
                    source_deletion_policy_approvals.c.approval_id
                    == current_source_deletion_policy_approvals.c.current_approval_id
                ),
            )
        )
        .where(
            *_policy_series_predicates(authorization),
            current_source_deletion_policy_approvals.c.generation
            == authorization.deletion_policy_authority_generation,
        )
        .with_for_update(of=current_source_deletion_policy_approvals)
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else SourceDeletionPolicyApproval.model_validate_json(json.dumps(record))
    )


async def _current_issuer(uow, workspace_id, environment_id):
    result = await uow._execute(
        select(tombstone_authorization_issuers.c.record)
        .select_from(
            current_tombstone_authorization_issuers.join(
                tombstone_authorization_issuers,
                (
                    tombstone_authorization_issuers.c.workspace_id
                    == current_tombstone_authorization_issuers.c.workspace_id
                )
                & (
                    tombstone_authorization_issuers.c.environment_id
                    == current_tombstone_authorization_issuers.c.environment_id
                )
                & (
                    tombstone_authorization_issuers.c.issuer_authority_id
                    == current_tombstone_authorization_issuers.c.current_issuer_authority_id
                ),
            )
        )
        .where(
            current_tombstone_authorization_issuers.c.workspace_id == workspace_id,
            current_tombstone_authorization_issuers.c.environment_id == environment_id,
        )
        .with_for_update(of=current_tombstone_authorization_issuers)
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else TombstoneAuthorizationIssuer.model_validate_json(json.dumps(record))
    )


async def _authorization_by_command(uow, idempotency_key):
    result = await uow._execute(
        select(tombstone_authorizations.c.record).where(
            tombstone_authorizations.c.idempotency_key == idempotency_key
        )
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else TombstoneAuthorization.model_validate_json(json.dumps(record))
    )


async def _authorization_by_id(uow, authorization_id):
    result = await uow._execute(
        select(tombstone_authorizations.c.record).where(
            tombstone_authorizations.c.authorization_id == authorization_id
        )
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else TombstoneAuthorization.model_validate_json(json.dumps(record))
    )


async def _decision_by_id(uow, decision_id):
    result = await uow._execute(
        select(tombstone_authorization_decisions.c.record).where(
            tombstone_authorization_decisions.c.decision_id == decision_id
        )
    )
    record = result.scalar_one_or_none()
    return (
        None
        if record is None
        else TombstoneAuthorizationDecision.model_validate_json(json.dumps(record))
    )


def _evidence_columns(value):
    return dict(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
        evidence_id=value.evidence_id,
        source_object_id=value.source_object_id,
        connection_id=value.connection_id,
        connection_version_id=value.connection_version_id,
        resource_scope_reference=value.resource_scope_reference,
        scope_approval_version_id=value.scope_approval_version_id,
        scope_approval_digest=value.scope_approval_digest,
        qualifier_service_principal_id=value.qualifier_service_principal_id,
        qualification_authority_reference=value.qualification_authority_reference,
        qualification_authority_generation=value.qualification_authority_generation,
        observation_id=value.observation_id,
        profile_id=value.profile_id,
        profile_version=value.profile_version,
        evidence_digest=value.evidence_digest,
        outcome=value.outcome.value,
        fresh_until=value.fresh_until,
        record=value.model_dump(mode="json"),
        verified_at=value.verified_at,
    )


def _approval_columns(value):
    return dict(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
        approval_id=value.approval_id,
        policy_id=value.policy_id,
        policy_version_id=value.policy_version_id,
        policy_digest=value.policy_digest,
        source_contract_id=value.source_contract_id,
        source_contract_version=value.source_contract_version,
        connection_id=value.connection_id,
        resource_scope_reference=value.resource_scope_reference,
        approver_human_id=value.approver_human_id,
        authority_generation=value.authority_generation,
        audit_event_id=value.audit_event_id,
        record=value.model_dump(mode="json"),
        approved_at=value.approved_at,
    )


def _issuer_columns(value):
    return dict(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
        issuer_authority_id=value.issuer_authority_id,
        service_principal_id=value.service_principal_id,
        authorized_by_human_id=value.authorized_by_human_id,
        authority_generation=value.authority_generation,
        authority_digest=value.authority_digest,
        audit_event_id=value.audit_event_id,
        record=value.model_dump(mode="json"),
        valid_from=value.valid_from,
    )


def _authorization_columns(value):
    return dict(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
        authorization_id=value.authorization_id,
        source_object_id=value.source_object_id,
        qualified_evidence_id=value.qualified_evidence_id,
        qualified_evidence_digest=value.qualified_evidence_digest,
        deletion_policy_approval_id=value.deletion_policy_approval_id,
        deletion_policy_version_id=value.deletion_policy_version_id,
        deletion_policy_digest=value.deletion_policy_digest,
        issuer_authority_id=value.issuer_authority_id,
        issuer_service_principal_id=value.issuer_service_principal_id,
        issuer_authority_generation=value.issuer_authority_generation,
        expected_current_revision_id=value.expected_current_revision_id,
        expected_current_acceptance_id=value.expected_current_acceptance_id,
        expected_current_cas_version=value.expected_current_cas_version,
        tombstone_revision_id=value.tombstone_revision_id,
        transition_id=value.transition_id,
        idempotency_key=value.idempotency_key,
        command_digest=value.command_digest,
        audit_event_id=value.audit_event_id,
        record=value.model_dump(mode="json"),
        issued_at=value.issued_at,
    )


def _decision_columns(value):
    return dict(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
        decision_id=value.decision_id,
        authorization_id=value.authorization_id,
        source_object_id=value.source_object_id,
        disposition=value.disposition.value,
        generation=value.generation,
        audit_event_id=value.audit_event_id,
        record=value.model_dump(mode="json"),
        decided_at=value.decided_at,
    )


def _replay_result(existing, requested):
    if existing is None:
        return None
    if existing == requested and existing.command_digest == requested.command_digest:
        return TombstoneAuthorizationResult(
            TombstoneAuthorizationOutcome.REPLAYED, existing
        )
    return TombstoneAuthorizationResult(
        TombstoneAuthorizationOutcome.IDEMPOTENCY_CONFLICT
    )


async def _rollback_denied(uow):
    await uow.rollback()
    return _denied()


def _denied():
    return TombstoneAuthorizationResult(
        TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED
    )


def _scope(context, workspace_id, environment_id):
    try:
        context.validate_shape()
    except Exception:
        return False
    return isinstance(context.scope, EnvironmentScope) and (
        context.scope.workspace_id,
        context.scope.environment_id,
    ) == (workspace_id, environment_id)


def _worker_scope(context, workspace_id, environment_id):
    return context.origin is ContextOrigin.WORKER and _scope(
        context, workspace_id, environment_id
    )


def _interactive_scope(context, workspace_id, environment_id, actor_id):
    return (
        context.origin is ContextOrigin.INTERACTIVE
        and _scope(context, workspace_id, environment_id)
        and (actor_id is None or context.acting_subject_id == actor_id)
    )


def _trusted_issuer(context, authorization):
    return (
        _worker_scope(context, authorization.workspace_id, authorization.environment_id)
        and context.service_principal_id == authorization.issuer_service_principal_id
    )


__all__ = [
    "PostgreSQLTombstoneAuthorizationRepository",
    "TombstoneAuthorizationCommitVerifier",
    "TombstoneAuthorizationEffectsWriter",
]
