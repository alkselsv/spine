"""PostgreSQL persistence primitives for CRM relationship evidence v2.

Callers supply an already scoped async connection and own its transaction. The
methods keep history appends, idempotency receipts and mutable-head CAS in that
same transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID, uuid5

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
    EnvironmentScope,
)
from spine.application.persistence.outbox import OutboxEventRegistry, OutboxIntent
from spine.application.persistence.errors import IdempotencyConflictError, OptimisticConflictError
from spine.domain.audit.models import AuditEvent, ContextOrigin
from spine.domain.sources.crm_relationship_v2 import (
    CrmEligibilityOutcome,
    CrmRelationshipClaim,
    CrmRelationshipEligibility,
    CrmRelationshipEligibilityHead,
    CrmRelationshipIdentity,
    CrmRelationshipLifecycleEvent,
    CrmRelationshipMappingProfile,
    CrmRelationshipReceiptAssessment,
    CrmRelationshipObservationReceipt,
)

TRecord = TypeVar("TRecord", bound=BaseModel)
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events,
    crm_relationship_authority_heads,
    crm_relationship_claims,
    crm_relationship_eligibility,
    crm_relationship_eligibility_heads,
    crm_relationship_idempotency,
    crm_relationship_identities,
    crm_relationship_profiles,
    crm_relationship_receipts,
    current_lifecycle_authorities,
    current_source_heads,
    lifecycle_decisions,
    outbox_intents,
)


class CrmRelationshipV2Store:
    """Append immutable v2 evidence and advance current eligibility safely."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @staticmethod
    def _profile_matches_identity(
        profile: CrmRelationshipMappingProfile,
        identity: CrmRelationshipIdentity,
    ) -> bool:
        return (
            profile.profile_id == identity.mapping_profile_id
            and profile.workspace_id == identity.workspace_id
            and profile.environment_id == identity.environment_id
            and profile.provider_instance == identity.provider_instance
            and profile.connection_id == identity.connection_id
            and profile.activity_namespace == identity.activity_namespace
            and identity.activity_type_id in profile.activity_type_ids
            and identity.raw_entity_type_id in profile.raw_entity_type_ids
            and profile.entity_namespace == identity.entity_namespace
            and profile.files_namespace == identity.files_namespace
            and profile.normalizer_id == identity.normalizer_id
            and profile.normalizer_version == identity.normalizer_version
        )

    async def _execute(self, statement: Any) -> Any:
        executor = getattr(self._connection, "_execute", None)
        if executor is not None:
            return await executor(statement)
        return await self._connection.execute(statement)

    async def _scalar(self, statement: Any) -> Any:
        return (await self._execute(statement)).scalar_one_or_none()

    async def current_eligibility(
        self, *, workspace_id: UUID, environment_id: UUID, claim_id: UUID,
        actor_id: UUID,
    ) -> CrmRelationshipEligibilityHead | None:
        self._assert_current_human(
            workspace_id=workspace_id, environment_id=environment_id, actor_id=actor_id,
        )
        result = await self._execute(select(crm_relationship_eligibility_heads).where(
            crm_relationship_eligibility_heads.c.workspace_id == workspace_id,
            crm_relationship_eligibility_heads.c.environment_id == environment_id,
            crm_relationship_eligibility_heads.c.claim_id == claim_id,
        ))
        row = result.mappings().one_or_none()
        if row is None:
            return None
        current = CrmRelationshipEligibilityHead.model_validate(dict(row))
        claim = (await self._execute(select(
            crm_relationship_claims.c.source_object_id,
            crm_relationship_claims.c.source_revision_id,
            crm_relationship_claims.c.connection_id,
            crm_relationship_claims.c.connection_version_id,
            crm_relationship_claims.c.mapping_profile_id,
            crm_relationship_claims.c.mapping_profile_version,
            crm_relationship_claims.c.authorization_generation,
            crm_relationship_claims.c.configuration_generation,
            crm_relationship_claims.c.relationship_key,
            crm_relationship_claims.c.record,
            crm_relationship_claims.c.relationship_key,
        ).where(
            crm_relationship_claims.c.workspace_id == workspace_id,
            crm_relationship_claims.c.environment_id == environment_id,
            crm_relationship_claims.c.claim_id == claim_id,
        ))).one_or_none()
        if claim is None:
            return None
        authority = (await self._execute(select(
            crm_relationship_authority_heads.c.withdrawn,
        ).where(
            crm_relationship_authority_heads.c.workspace_id == workspace_id,
            crm_relationship_authority_heads.c.environment_id == environment_id,
            crm_relationship_authority_heads.c.relationship_key == claim.relationship_key,
        ).with_for_update(read=True))).scalar_one_or_none()
        if authority is None:
            return current.model_copy(update={"outcome": CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE})
        if authority:
            return current.model_copy(update={"outcome": CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN})
        if current.outcome is CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN:
            return current
        authorization_generation = await self._scalar(text(
            "SELECT generation FROM spine.authorization_generations "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id FOR SHARE"
        ).bindparams(workspace_id=workspace_id, environment_id=environment_id))
        source = (await self._execute(select(
            current_source_heads.c.current_revision_id,
            current_source_heads.c.current_revision_kind,
            lifecycle_decisions.c.status.label("lifecycle_status"),
        ).select_from(
            current_source_heads.outerjoin(
                current_lifecycle_authorities,
                (current_lifecycle_authorities.c.workspace_id == current_source_heads.c.workspace_id)
                & (current_lifecycle_authorities.c.environment_id == current_source_heads.c.environment_id)
                & (current_lifecycle_authorities.c.source_object_id == current_source_heads.c.source_object_id),
            ).outerjoin(
                lifecycle_decisions,
                (lifecycle_decisions.c.workspace_id == current_lifecycle_authorities.c.workspace_id)
                & (lifecycle_decisions.c.environment_id == current_lifecycle_authorities.c.environment_id)
                & (lifecycle_decisions.c.source_object_id == current_lifecycle_authorities.c.source_object_id)
                & (lifecycle_decisions.c.decision_id == current_lifecycle_authorities.c.current_decision_id)
                & (lifecycle_decisions.c.generation == current_lifecycle_authorities.c.generation),
            ),
        ).where(
            current_source_heads.c.workspace_id == workspace_id,
            current_source_heads.c.environment_id == environment_id,
            current_source_heads.c.source_object_id == claim.source_object_id,
        ).with_for_update(read=True, of=current_source_heads))).mappings().one_or_none()
        connection = (await self._execute(text(
            "SELECT version_record.approval_id, version_record.authorization_generation, "
            "version_record.configuration_generation, "
            "version_record.status, approval.status AS approval_status, version_record.version, "
            "version_record.approved_resource_scope_references "
            "FROM spine.provider_connection_versions AS version_record "
            "JOIN spine.provider_connection_approvals AS approval "
            "ON approval.approval_id = version_record.approval_id "
            "AND approval.workspace_id = version_record.workspace_id "
            "AND approval.environment_id = version_record.environment_id "
            "WHERE version_record.workspace_id = :workspace_id "
            "AND version_record.environment_id = :environment_id "
            "AND version_record.connection_id = :connection_id "
            "AND version_record.connection_version_id = :connection_version_id "
            "FOR SHARE OF version_record, approval"
        ).bindparams(
            workspace_id=workspace_id, environment_id=environment_id,
            connection_id=claim.connection_id,
            connection_version_id=claim.connection_version_id,
        ))).one_or_none()
        latest_connection_version = await self._scalar(text(
            "SELECT max(version) FROM spine.provider_connection_versions "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
            "AND connection_id = :connection_id"
        ).bindparams(
            workspace_id=workspace_id, environment_id=environment_id,
            connection_id=claim.connection_id,
        ))
        current_profile = (await self._execute(select(
            crm_relationship_profiles.c.profile_version,
            crm_relationship_profiles.c.record,
        ).where(
            crm_relationship_profiles.c.workspace_id == workspace_id,
            crm_relationship_profiles.c.environment_id == environment_id,
            crm_relationship_profiles.c.profile_id == claim.mapping_profile_id,
        ).order_by(crm_relationship_profiles.c.profile_version.desc()).limit(1)
        .with_for_update(read=True))).one_or_none()
        identity_record = await self._scalar(select(
            crm_relationship_identities.c.natural_identity,
        ).where(
            crm_relationship_identities.c.workspace_id == workspace_id,
            crm_relationship_identities.c.environment_id == environment_id,
            crm_relationship_identities.c.relationship_key == claim.relationship_key,
        ))
        try:
            identity = CrmRelationshipIdentity.model_validate(identity_record)
            profile = CrmRelationshipMappingProfile.model_validate(
                current_profile.record if current_profile is not None else None
            )
        except ValidationError:
            identity = None
            profile = None
        profile_is_current = (
            current_profile is not None
            and current_profile.profile_version == claim.mapping_profile_version
            and profile is not None
            and profile.profile_version == claim.mapping_profile_version
            and connection is not None
            and profile.connection_approval_id == connection.approval_id
            and profile.qualification_reference in connection.approved_resource_scope_references
            and profile.approval_reference in connection.approved_resource_scope_references
            and identity is not None
            and self._profile_matches_identity(profile, identity)
        )
        if source is None or source["current_revision_kind"] != "content":
            return current.model_copy(update={"outcome": CrmEligibilityOutcome.SOURCE_INELIGIBLE})
        if source["current_revision_id"] != claim.source_revision_id:
            return current.model_copy(update={"outcome": CrmEligibilityOutcome.STALE_CONTEXT})
        if source["lifecycle_status"] != "eligible":
            return current.model_copy(update={"outcome": CrmEligibilityOutcome.SOURCE_INELIGIBLE})
        if (
            authorization_generation != claim.authorization_generation
            or connection is None
            or connection.authorization_generation != claim.authorization_generation
            or connection.configuration_generation != claim.configuration_generation
            or connection.status != "active"
            or connection.approval_status != "active"
            or connection.version != latest_connection_version
            or not profile_is_current
        ):
            return current.model_copy(update={"outcome": CrmEligibilityOutcome.STALE_CONTEXT})
        return current

    async def _assert_enabled(self) -> None:
        enabled = await self._scalar(
            text("SELECT spine.crm_relationship_v2_writes_enabled()")
        )
        if enabled is not True:
            raise OptimisticConflictError("CRM relationship v2 writes are disabled by migration rollback.")

    def _assert_current_human(
        self, *, workspace_id: UUID, environment_id: UUID, actor_id: UUID
    ) -> None:
        context_reader = getattr(self._connection, "_context_snapshot_active", None)
        if not callable(context_reader):
            raise OptimisticConflictError("CRM v2 writes require a verified interactive human context.")
        context = context_reader()
        if (
            context.origin is not ContextOrigin.INTERACTIVE
            or context.acting_subject_id != actor_id
            or context.service_principal_id is not None
            or context.scope != EnvironmentScope(workspace_id, environment_id)
        ):
            raise OptimisticConflictError("CRM v2 writes require the exact current interactive human scope.")

    async def _persist_audit_outbox(
        self, *, audit_event: AuditEvent, outbox_intent: OutboxIntent,
        audit_registry: AuditEventRegistry, outbox_registry: OutboxEventRegistry,
        workspace_id: UUID, environment_id: UUID, actor_id: UUID,
        created_at: datetime, record_id: UUID,
    ) -> tuple[UUID, UUID]:
        audit = audit_registry.validate(audit_event)
        outbox = outbox_registry.validate(outbox_intent)
        if (
            audit.origin is not ContextOrigin.INTERACTIVE
            or audit.acting_subject_id != actor_id
            or audit.service_principal_id is not None
            or audit.workspace_id != workspace_id
            or audit.environment_id != environment_id
            or audit.audit_event_id is None
            or audit.target is None
            or audit.target.object_id != record_id
            or audit.trace_id != outbox.trace_id
            or outbox.workspace_id != workspace_id
            or outbox.environment_id != environment_id
            or outbox.event_id is None
            or outbox.aggregate is None
            or outbox.aggregate.object_id != record_id
        ):
            raise OptimisticConflictError("CRM write requires matching acting-human audit and outbox records.")
        aggregate = outbox.aggregate
        await self._execute(insert(audit_events).values(
            audit_event_id=audit.audit_event_id, workspace_id=audit.workspace_id,
            environment_id=audit.environment_id, event_type=str(audit.event_type),
            schema_version=audit.schema_version, origin=audit.origin.value,
            acting_subject_id=audit.acting_subject_id, service_principal_id=None,
            trace_id=audit.trace_id, correlation_id=audit.correlation_id,
            causation_id=audit.causation_id, occurred_at=audit.occurred_at,
            appended_at=audit.appended_at or created_at,
            target_type=audit.target.object_type if audit.target else None,
            target_id=audit.target.object_id if audit.target else None,
            target_schema_version=audit.target.schema_version if audit.target else None,
            outcome=audit.outcome.value, reason=str(audit.reason),
            producer_deduplication_id=str(audit.producer_deduplication_id) if audit.producer_deduplication_id else None,
            payload=audit.payload_json(),
        ))
        await self._execute(insert(outbox_intents).values(
            event_id=outbox.event_id, workspace_id=outbox.workspace_id,
            environment_id=outbox.environment_id, event_type=outbox.event_type,
            event_schema_version=outbox.schema_version,
            aggregate_type=aggregate.object_type if aggregate else None,
            aggregate_id=aggregate.object_id if aggregate else None,
            aggregate_schema_version=aggregate.schema_version if aggregate else None,
            producer_deduplication_id=outbox.producer_deduplication_id,
            payload=outbox.payload_json(), trace_id=outbox.trace_id,
            correlation_id=outbox.correlation_id, causation_id=outbox.causation_id,
            created_at=created_at,
        ))
        return audit.audit_event_id, outbox.event_id

    async def _reserve_idempotency(
        self, *, workspace_id: UUID, environment_id: UUID,
        idempotency_key: UUID, payload_digest: str, record_kind: str,
        record_id: UUID, result: TRecord, result_type: type[TRecord],
        created_at: datetime,
    ) -> TRecord | None:
        table = crm_relationship_idempotency
        inserted = await self._execute(insert(table).values(
            workspace_id=workspace_id, environment_id=environment_id,
            idempotency_key=idempotency_key, payload_digest=payload_digest,
            record_kind=record_kind, record_id=record_id,
            result=result.model_dump(mode="json"),
            created_at=created_at,
        ).on_conflict_do_nothing(index_elements=[
            table.c.workspace_id, table.c.environment_id, table.c.idempotency_key,
        ]).returning(table.c.idempotency_key))
        if inserted.scalar_one_or_none() is not None:
            return None
        prior = (await self._execute(select(
            table.c.payload_digest, table.c.record_kind, table.c.result,
        ).where(
            table.c.workspace_id == workspace_id,
            table.c.environment_id == environment_id,
            table.c.idempotency_key == idempotency_key,
        ))).one()
        if prior.payload_digest != payload_digest or prior.record_kind != record_kind:
            raise IdempotencyConflictError("CRM idempotency key conflicts with a different payload.")
        return result_type.model_validate(prior.result)

    async def register_profile(
        self, *, profile: CrmRelationshipMappingProfile, digest: str,
        created_at: datetime, actor_id: UUID,
    ) -> None:
        self._assert_current_human(
            workspace_id=profile.workspace_id, environment_id=profile.environment_id,
            actor_id=actor_id,
        )
        await self._assert_enabled()
        if (profile.workspace_id.int == 0 or profile.environment_id.int == 0):
            raise OptimisticConflictError("CRM mapping profile has an invalid tenant scope.")
        record = profile.model_dump(mode="json")
        table = crm_relationship_profiles
        values = {
            "profile_id": profile.profile_id, "workspace_id": profile.workspace_id,
            "environment_id": profile.environment_id,
            "profile_version": profile.profile_version,
            "profile_digest": digest, "record": record, "created_at": created_at,
        }
        result = await self._execute(
            insert(table).values(**values).on_conflict_do_nothing(
                index_elements=[table.c.workspace_id, table.c.environment_id,
                                table.c.profile_id, table.c.profile_version]
            ).returning(table.c.profile_id)
        )
        if result.scalar_one_or_none() is None:
            existing = (await self._execute(select(table.c.profile_digest, table.c.record).where(
                table.c.workspace_id == profile.workspace_id,
                table.c.environment_id == profile.environment_id,
                table.c.profile_id == profile.profile_id,
                table.c.profile_version == profile.profile_version,
            ))).one()
            if existing.profile_digest != digest or existing.record != record:
                raise IdempotencyConflictError("CRM mapping profile version already has different content.")

    async def register_identity(self, identity: CrmRelationshipIdentity, *, actor_id: UUID) -> None:
        self._assert_current_human(
            workspace_id=identity.workspace_id, environment_id=identity.environment_id,
            actor_id=actor_id,
        )
        await self._assert_enabled()
        table = crm_relationship_identities
        record = identity.model_dump(mode="json")
        values = {
            "relationship_key": identity.relationship_key,
            "workspace_id": identity.workspace_id,
            "environment_id": identity.environment_id,
            "natural_identity": record,
            "schema_version": identity.schema_version,
            "record_kind": identity.record_kind,
        }
        result = await self._execute(
            insert(table).values(**values).on_conflict_do_nothing(
                index_elements=[table.c.workspace_id, table.c.environment_id, table.c.relationship_key]
            ).returning(table.c.relationship_key)
        )
        if result.scalar_one_or_none() is None:
            existing = (await self._execute(select(table.c.natural_identity).where(
                table.c.workspace_id == identity.workspace_id,
                table.c.environment_id == identity.environment_id,
                table.c.relationship_key == identity.relationship_key,
            ))).scalar_one()
            if existing != record:
                raise IdempotencyConflictError("CRM relationship digest conflicts with natural identity.")
        await self._execute(insert(crm_relationship_authority_heads).values(
            workspace_id=identity.workspace_id,
            environment_id=identity.environment_id,
            relationship_key=identity.relationship_key,
            lifecycle_generation=0,
            withdrawn=False,
        ).on_conflict_do_nothing(index_elements=[
            crm_relationship_authority_heads.c.workspace_id,
            crm_relationship_authority_heads.c.environment_id,
            crm_relationship_authority_heads.c.relationship_key,
        ]))

    async def append_claim(
        self, claim: CrmRelationshipClaim, *,
        audit_event: AuditEvent, outbox_intent: OutboxIntent,
        audit_registry: AuditEventRegistry, outbox_registry: OutboxEventRegistry,
    ) -> CrmRelationshipClaim:
        self._assert_current_human(
            workspace_id=claim.workspace_id, environment_id=claim.environment_id,
            actor_id=claim.acting_human_id,
        )
        await self._assert_enabled()
        table = crm_relationship_claims
        record = claim.model_dump(mode="json")
        prior = await self._reserve_idempotency(
            workspace_id=claim.workspace_id, environment_id=claim.environment_id,
            idempotency_key=claim.idempotency_key, payload_digest=claim.payload_digest,
            record_kind="claim", record_id=claim.claim_id,
            result=claim, result_type=CrmRelationshipClaim,
            created_at=claim.created_at,
        )
        if prior is not None:
            return prior
        await self._execute(text(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(:lock_key, 0))"
        ).bindparams(lock_key=(
            f"{claim.workspace_id}:{claim.environment_id}:{claim.connection_id}"
        )))
        identity_record = await self._scalar(select(
            crm_relationship_identities.c.natural_identity,
        ).where(
            crm_relationship_identities.c.workspace_id == claim.workspace_id,
            crm_relationship_identities.c.environment_id == claim.environment_id,
            crm_relationship_identities.c.relationship_key == claim.relationship_key,
        ))
        try:
            identity = CrmRelationshipIdentity.model_validate(identity_record)
        except ValidationError as error:
            raise OptimisticConflictError("CRM logical relationship identity is unsupported.") from error
        if identity.connection_id != claim.connection_id:
            raise OptimisticConflictError("CRM claim connection does not match its logical relationship identity.")
        authority = (await self._execute(select(
            crm_relationship_authority_heads.c.lifecycle_generation,
            crm_relationship_authority_heads.c.withdrawn,
        ).where(
            crm_relationship_authority_heads.c.workspace_id == claim.workspace_id,
            crm_relationship_authority_heads.c.environment_id == claim.environment_id,
            crm_relationship_authority_heads.c.relationship_key == claim.relationship_key,
        ).with_for_update())).one_or_none()
        if authority is None:
            raise OptimisticConflictError("CRM relationship authority head is unavailable.")
        source_current = (await self._execute(select(
            current_source_heads.c.current_revision_id,
            current_source_heads.c.current_revision_kind,
            lifecycle_decisions.c.status.label("lifecycle_status"),
        ).select_from(
            current_source_heads.outerjoin(
                current_lifecycle_authorities,
                (current_lifecycle_authorities.c.workspace_id == current_source_heads.c.workspace_id)
                & (current_lifecycle_authorities.c.environment_id == current_source_heads.c.environment_id)
                & (current_lifecycle_authorities.c.source_object_id == current_source_heads.c.source_object_id),
            ).outerjoin(
                lifecycle_decisions,
                (lifecycle_decisions.c.workspace_id == current_lifecycle_authorities.c.workspace_id)
                & (lifecycle_decisions.c.environment_id == current_lifecycle_authorities.c.environment_id)
                & (lifecycle_decisions.c.source_object_id == current_lifecycle_authorities.c.source_object_id)
                & (lifecycle_decisions.c.decision_id == current_lifecycle_authorities.c.current_decision_id)
                & (lifecycle_decisions.c.generation == current_lifecycle_authorities.c.generation),
            ),
        ).where(
                current_source_heads.c.workspace_id == claim.workspace_id,
                current_source_heads.c.environment_id == claim.environment_id,
                current_source_heads.c.source_object_id == claim.source_object_id,
            ).with_for_update(of=current_source_heads))).mappings().one_or_none()
        if (
            source_current is None
            or source_current["current_revision_id"] != claim.source_revision_id
            or source_current["current_revision_kind"] != "content"
            or source_current["lifecycle_status"] != "eligible"
        ):
            raise OptimisticConflictError("CRM claim must reference the current eligible canonical source revision.")
        authorization_result = await self._execute(text(
            "SELECT generation FROM spine.authorization_generations "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id FOR SHARE"
        ).bindparams(
            workspace_id=claim.workspace_id, environment_id=claim.environment_id,
        ))
        authorization_generation = authorization_result.scalar_one_or_none()
        if authorization_generation != claim.authorization_generation:
            raise OptimisticConflictError("CRM claim authorization generation is stale.")
        connection_version = (await self._execute(text(
            "SELECT version_record.approval_id, version_record.authorization_generation, "
            "version_record.configuration_generation, version_record.status, "
            "approval.status AS approval_status, version_record.version, "
            "version_record.approved_resource_scope_references "
            "FROM spine.provider_connection_versions AS version_record "
            "JOIN spine.provider_connection_approvals AS approval "
            "ON approval.approval_id = version_record.approval_id "
            "AND approval.workspace_id = version_record.workspace_id "
            "AND approval.environment_id = version_record.environment_id "
            "WHERE version_record.workspace_id = :workspace_id "
            "AND version_record.environment_id = :environment_id "
            "AND version_record.connection_id = :connection_id "
            "AND version_record.connection_version_id = :connection_version_id "
            "FOR SHARE OF version_record, approval"
        ).bindparams(
            workspace_id=claim.workspace_id,
            environment_id=claim.environment_id,
            connection_id=claim.connection_id,
            connection_version_id=claim.connection_version_id,
        ))).one_or_none()
        if (
            connection_version is None
            or connection_version.authorization_generation != claim.authorization_generation
            or connection_version.configuration_generation != claim.configuration_generation
            or connection_version.status != "active"
            or connection_version.approval_status != "active"
        ):
            raise OptimisticConflictError("CRM claim connection authorization/configuration is stale or inactive.")
        current_connection_version = await self._scalar(text(
            "SELECT max(version) FROM spine.provider_connection_versions "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
            "AND connection_id = :connection_id"
        ).bindparams(
            workspace_id=claim.workspace_id,
            environment_id=claim.environment_id,
            connection_id=claim.connection_id,
        ))
        if current_connection_version != connection_version.version:
            raise OptimisticConflictError("CRM claim connection version is no longer current.")
        current_profile = (await self._execute(select(
            crm_relationship_profiles.c.profile_version,
            crm_relationship_profiles.c.record,
        ).where(
            crm_relationship_profiles.c.workspace_id == claim.workspace_id,
            crm_relationship_profiles.c.environment_id == claim.environment_id,
            crm_relationship_profiles.c.profile_id == claim.mapping_profile_id,
        ).order_by(crm_relationship_profiles.c.profile_version.desc()).limit(1).with_for_update())).one_or_none()
        if current_profile is None or current_profile.profile_version != claim.mapping_profile_version:
            raise OptimisticConflictError("CRM claim mapping profile version is no longer current.")
        try:
            profile = CrmRelationshipMappingProfile.model_validate(current_profile.record)
        except ValidationError as error:
            raise OptimisticConflictError("CRM mapping profile is unsupported.") from error
        if (
            profile.profile_version != claim.mapping_profile_version
            or profile.connection_approval_id != connection_version.approval_id
            or profile.qualification_reference not in connection_version.approved_resource_scope_references
            or profile.approval_reference not in connection_version.approved_resource_scope_references
            or not self._profile_matches_identity(profile, identity)
        ):
            raise OptimisticConflictError("CRM mapping profile approval or exact relationship scope is stale.")
        audit_id, outbox_id = await self._persist_audit_outbox(
            audit_event=audit_event, outbox_intent=outbox_intent,
            audit_registry=audit_registry, outbox_registry=outbox_registry,
            workspace_id=claim.workspace_id, environment_id=claim.environment_id,
            actor_id=claim.acting_human_id, created_at=claim.created_at,
            record_id=claim.claim_id,
        )
        await self._execute(insert(table).values(
            claim_id=claim.claim_id, workspace_id=claim.workspace_id,
            environment_id=claim.environment_id, relationship_key=claim.relationship_key,
            source_object_id=claim.source_object_id, source_revision_id=claim.source_revision_id,
            connection_id=claim.connection_id,
            connection_version_id=claim.connection_version_id,
            audit_event_id=audit_id, outbox_event_id=outbox_id,
            mapping_profile_id=claim.mapping_profile_id,
            mapping_profile_version=claim.mapping_profile_version,
            authorization_generation=claim.authorization_generation,
            configuration_generation=claim.configuration_generation,
            verification_generation=1, idempotency_key=claim.idempotency_key,
            payload_digest=claim.payload_digest, record=record, created_at=claim.created_at,
        ))
        await self._execute(insert(crm_relationship_eligibility_heads).values(
            workspace_id=claim.workspace_id, environment_id=claim.environment_id,
            claim_id=claim.claim_id, verification_generation=0,
            lifecycle_generation=authority.lifecycle_generation,
            outcome=("GOVERNANCE_WITHDRAWN" if authority.withdrawn else "NOT_VERIFIED"), receipt_id=None,
            updated_at=claim.created_at,
        ))
        return claim

    async def append_receipt(
        self, receipt: CrmRelationshipObservationReceipt, *,
        eligibility: CrmRelationshipEligibility,
        audit_event: AuditEvent, outbox_intent: OutboxIntent,
        audit_registry: AuditEventRegistry, outbox_registry: OutboxEventRegistry,
        expected_verification_generation: int, lifecycle_generation: int,
    ) -> CrmRelationshipObservationReceipt:
        self._assert_current_human(
            workspace_id=receipt.workspace_id, environment_id=receipt.environment_id,
            actor_id=receipt.acting_human_id,
        )
        await self._assert_enabled()
        head = crm_relationship_eligibility_heads
        record = receipt.model_dump(mode="json")
        if receipt.verification_generation != expected_verification_generation + 1:
            raise OptimisticConflictError("CRM receipt generation is not the next local verification generation.")
        if (
            eligibility.claim_id != receipt.claim_id
            or eligibility.workspace_id != receipt.workspace_id
            or eligibility.environment_id != receipt.environment_id
            or eligibility.verification_generation != receipt.verification_generation
            or eligibility.lifecycle_generation != lifecycle_generation
        ):
            raise OptimisticConflictError("CRM eligibility must pin this receipt and current lifecycle generation.")
        try:
            CrmRelationshipReceiptAssessment(receipt=receipt, eligibility=eligibility)
        except ValidationError as error:
            raise OptimisticConflictError(
                "CRM eligibility outcome must match its exact observation receipt."
            ) from error
        claim_scope = (await self._execute(select(
            crm_relationship_claims.c.connection_id,
            crm_relationship_claims.c.connection_version_id,
            crm_relationship_claims.c.source_object_id,
            crm_relationship_claims.c.source_revision_id,
            crm_relationship_claims.c.mapping_profile_id,
            crm_relationship_claims.c.mapping_profile_version,
            crm_relationship_claims.c.authorization_generation,
            crm_relationship_claims.c.configuration_generation,
        ).where(
            crm_relationship_claims.c.workspace_id == receipt.workspace_id,
            crm_relationship_claims.c.environment_id == receipt.environment_id,
            crm_relationship_claims.c.claim_id == receipt.claim_id,
        ))).one_or_none()
        if (
            claim_scope is None
            or claim_scope.connection_id != receipt.connection_id
            or claim_scope.connection_version_id != receipt.connection_version_id
            or claim_scope.mapping_profile_id != receipt.mapping_profile_id
            or claim_scope.mapping_profile_version != receipt.mapping_profile_version
            or claim_scope.authorization_generation != receipt.authorization_generation
            or claim_scope.configuration_generation != receipt.configuration_generation
        ):
            raise OptimisticConflictError("CRM receipt does not pin the exact claim configuration and human authority.")
        try:
            claim_record = CrmRelationshipClaim.model_validate(claim_scope.record)
            identity_record = await self._scalar(select(
                crm_relationship_identities.c.natural_identity,
            ).where(
                crm_relationship_identities.c.workspace_id == receipt.workspace_id,
                crm_relationship_identities.c.environment_id == receipt.environment_id,
                crm_relationship_identities.c.relationship_key == claim_scope.relationship_key,
            ))
            identity = CrmRelationshipIdentity.model_validate(identity_record)
        except ValidationError as error:
            raise OptimisticConflictError("CRM receipt identity evidence is unavailable or unsupported.") from error
        if (
            receipt.source_object_id != claim_scope.source_object_id
            or receipt.source_revision_id != claim_scope.source_revision_id
            or receipt.disk_namespace != claim_record.disk_namespace
            or receipt.disk_object_id != claim_record.disk_object_id
            or receipt.disk_version_evidence != claim_record.disk_version_evidence
            or receipt.provider_instance != identity.provider_instance
            or receipt.activity_namespace != identity.activity_namespace
            or receipt.activity_type_id != identity.activity_type_id
            or receipt.activity_id != identity.activity_id
            or receipt.raw_entity_type_id != identity.raw_entity_type_id
            or receipt.normalizer_id != identity.normalizer_id
            or receipt.normalizer_version != identity.normalizer_version
            or receipt.entity_namespace != identity.entity_namespace
            or receipt.entity_id != identity.entity_id
            or receipt.files_namespace != identity.files_namespace
            or receipt.files_item_id != identity.files_item_id
        ):
            raise OptimisticConflictError("CRM receipt evidence does not pin the exact claimed relationship and source.")
        prior = await self._reserve_idempotency(
            workspace_id=receipt.workspace_id, environment_id=receipt.environment_id,
            idempotency_key=receipt.idempotency_key, payload_digest=receipt.payload_digest,
            record_kind="observation_receipt", record_id=receipt.receipt_id,
            result=receipt, result_type=CrmRelationshipObservationReceipt,
            created_at=receipt.observed_at,
        )
        if prior is not None:
            return prior
        relationship_key = await self._scalar(select(
            crm_relationship_claims.c.relationship_key,
        ).where(
            crm_relationship_claims.c.workspace_id == receipt.workspace_id,
            crm_relationship_claims.c.environment_id == receipt.environment_id,
            crm_relationship_claims.c.claim_id == receipt.claim_id,
        ))
        authority_result = await self._execute(select(
            crm_relationship_authority_heads.c.lifecycle_generation,
            crm_relationship_authority_heads.c.withdrawn,
        ).where(
            crm_relationship_authority_heads.c.workspace_id == receipt.workspace_id,
            crm_relationship_authority_heads.c.environment_id == receipt.environment_id,
            crm_relationship_authority_heads.c.relationship_key == relationship_key,
        ).with_for_update())
        authority = authority_result.one_or_none()
        if authority is None or authority.lifecycle_generation != lifecycle_generation:
            raise OptimisticConflictError("CRM observation uses a stale lifecycle generation.")

        source_result = await self._execute(select(
            current_source_heads.c.current_revision_id,
            current_source_heads.c.current_revision_kind,
            lifecycle_decisions.c.status.label("lifecycle_status"),
        ).select_from(
            current_source_heads.outerjoin(
                current_lifecycle_authorities,
                (current_lifecycle_authorities.c.workspace_id == current_source_heads.c.workspace_id)
                & (current_lifecycle_authorities.c.environment_id == current_source_heads.c.environment_id)
                & (current_lifecycle_authorities.c.source_object_id == current_source_heads.c.source_object_id),
            ).outerjoin(
                lifecycle_decisions,
                (lifecycle_decisions.c.workspace_id == current_lifecycle_authorities.c.workspace_id)
                & (lifecycle_decisions.c.environment_id == current_lifecycle_authorities.c.environment_id)
                & (lifecycle_decisions.c.source_object_id == current_lifecycle_authorities.c.source_object_id)
                & (lifecycle_decisions.c.decision_id == current_lifecycle_authorities.c.current_decision_id)
                & (lifecycle_decisions.c.generation == current_lifecycle_authorities.c.generation),
            ),
        ).where(
            current_source_heads.c.workspace_id == receipt.workspace_id,
            current_source_heads.c.environment_id == receipt.environment_id,
            current_source_heads.c.source_object_id == claim_scope.source_object_id,
        ).with_for_update(of=current_source_heads)).mappings().one_or_none()
        authorization_generation = await self._scalar(text(
            "SELECT generation FROM spine.authorization_generations "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id FOR SHARE"
        ).bindparams(
            workspace_id=receipt.workspace_id, environment_id=receipt.environment_id,
        ))
        current_connection = (await self._execute(text(
            "SELECT version_record.approval_id, version_record.authorization_generation, "
            "version_record.configuration_generation, "
            "version_record.status, approval.status AS approval_status, version_record.version, "
            "version_record.approved_resource_scope_references "
            "FROM spine.provider_connection_versions AS version_record "
            "JOIN spine.provider_connection_approvals AS approval "
            "ON approval.approval_id = version_record.approval_id "
            "AND approval.workspace_id = version_record.workspace_id "
            "AND approval.environment_id = version_record.environment_id "
            "WHERE version_record.workspace_id = :workspace_id "
            "AND version_record.environment_id = :environment_id "
            "AND version_record.connection_id = :connection_id "
            "AND version_record.connection_version_id = :connection_version_id "
            "FOR SHARE OF version_record, approval"
        ).bindparams(
            workspace_id=receipt.workspace_id,
            environment_id=receipt.environment_id,
            connection_id=receipt.connection_id,
            connection_version_id=receipt.connection_version_id,
        ))).one_or_none()
        latest_connection_version = await self._scalar(text(
            "SELECT max(version) FROM spine.provider_connection_versions "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
            "AND connection_id = :connection_id"
        ).bindparams(
            workspace_id=receipt.workspace_id,
            environment_id=receipt.environment_id,
            connection_id=receipt.connection_id,
        ))
        current_profile = (await self._execute(select(
            crm_relationship_profiles.c.profile_version,
            crm_relationship_profiles.c.record,
        ).where(
            crm_relationship_profiles.c.workspace_id == receipt.workspace_id,
            crm_relationship_profiles.c.environment_id == receipt.environment_id,
            crm_relationship_profiles.c.profile_id == claim_scope.mapping_profile_id,
        ).order_by(crm_relationship_profiles.c.profile_version.desc()).limit(1)
        .with_for_update())).one_or_none()
        source_is_current = (
            source_result is not None
            and source_result["current_revision_id"] == claim_scope.source_revision_id
            and source_result["current_revision_kind"] == "content"
        )
        source_is_eligible = (
            source_result is not None
            and source_result["lifecycle_status"] == "eligible"
        )
        connection_is_current = (
            current_connection is not None
            and current_connection.authorization_generation == receipt.authorization_generation
            and current_connection.configuration_generation == receipt.configuration_generation
            and current_connection.status == "active"
            and current_connection.approval_status == "active"
            and current_connection.version == latest_connection_version
            and authorization_generation == receipt.authorization_generation
        )
        try:
            profile = CrmRelationshipMappingProfile.model_validate(
                current_profile.record if current_profile is not None else None
            )
        except ValidationError:
            profile = None
        profile_is_current = (
            current_profile is not None
            and current_profile.profile_version == claim_scope.mapping_profile_version
            and profile is not None
            and profile.profile_version == claim_scope.mapping_profile_version
            and current_connection is not None
            and profile.connection_approval_id == current_connection.approval_id
            and profile.qualification_reference in current_connection.approved_resource_scope_references
            and profile.approval_reference in current_connection.approved_resource_scope_references
            and self._profile_matches_identity(profile, identity)
            and (
                profile.mapping_strategy != "attached_object"
                or receipt.ownership_proof_reference is not None
            )
        )
        current_head = (await self._execute(select(
            head.c.outcome,
        ).where(
            head.c.workspace_id == receipt.workspace_id,
            head.c.environment_id == receipt.environment_id,
            head.c.claim_id == receipt.claim_id,
        ).with_for_update())).scalar_one_or_none()
        effective_outcome = eligibility.outcome
        if authority.withdrawn or current_head == "GOVERNANCE_WITHDRAWN":
            effective_outcome = CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN
        elif not source_is_current:
            effective_outcome = CrmEligibilityOutcome.STALE_CONTEXT
        elif not source_is_eligible:
            effective_outcome = CrmEligibilityOutcome.SOURCE_INELIGIBLE
        elif not connection_is_current or not profile_is_current:
            effective_outcome = CrmEligibilityOutcome.STALE_CONTEXT
        derived_eligibility = eligibility.model_copy(update={"outcome": effective_outcome})
        head_outcome = effective_outcome.value
        audit_id, outbox_id = await self._persist_audit_outbox(
            audit_event=audit_event, outbox_intent=outbox_intent,
            audit_registry=audit_registry, outbox_registry=outbox_registry,
            workspace_id=receipt.workspace_id, environment_id=receipt.environment_id,
            actor_id=receipt.acting_human_id, created_at=receipt.observed_at,
            record_id=receipt.receipt_id,
        )
        await self._execute(insert(crm_relationship_receipts).values(
            receipt_id=receipt.receipt_id, claim_id=receipt.claim_id,
            workspace_id=receipt.workspace_id, environment_id=receipt.environment_id,
            verification_generation=receipt.verification_generation,
            connection_id=receipt.connection_id,
            connection_version_id=receipt.connection_version_id,
            mapping_profile_id=receipt.mapping_profile_id,
            mapping_profile_version=receipt.mapping_profile_version,
            acting_human_id=receipt.acting_human_id,
            authorization_generation=receipt.authorization_generation,
            configuration_generation=receipt.configuration_generation,
            outcome=receipt.outcome.value, idempotency_key=receipt.idempotency_key,
            payload_digest=receipt.payload_digest, record=record,
            audit_event_id=audit_id, outbox_event_id=outbox_id,
            observed_at=receipt.observed_at,
        ))
        await self._execute(insert(crm_relationship_eligibility).values(
            eligibility_id=eligibility.eligibility_id,
            claim_id=eligibility.claim_id,
            workspace_id=eligibility.workspace_id,
            environment_id=eligibility.environment_id,
            receipt_id=receipt.receipt_id,
            verification_generation=eligibility.verification_generation,
            lifecycle_generation=eligibility.lifecycle_generation,
            outcome=derived_eligibility.outcome.value,
            record=derived_eligibility.model_dump(mode="json"),
            evaluated_at=eligibility.evaluated_at,
        ))
        changed = await self._execute(update(head).where(
            head.c.workspace_id == receipt.workspace_id,
            head.c.environment_id == receipt.environment_id,
            head.c.claim_id == receipt.claim_id,
            head.c.verification_generation == expected_verification_generation,
            head.c.lifecycle_generation == lifecycle_generation,
        ).values(
            verification_generation=receipt.verification_generation,
            outcome=head_outcome, receipt_id=receipt.receipt_id,
            eligibility_id=eligibility.eligibility_id,
            updated_at=receipt.observed_at,
        ).returning(head.c.claim_id))
        if changed.scalar_one_or_none() != receipt.claim_id:
            raise OptimisticConflictError("CRM eligibility head changed during verification.")
        return receipt

    async def append_lifecycle_event(
        self,
        event: CrmRelationshipLifecycleEvent,
        *,
        audit_event: AuditEvent,
        outbox_intent: OutboxIntent,
        audit_registry: AuditEventRegistry,
        outbox_registry: OutboxEventRegistry,
    ) -> CrmRelationshipLifecycleEvent:
        """Append a human governance event and advance its lifecycle CAS atomically."""
        self._assert_current_human(
            workspace_id=event.workspace_id, environment_id=event.environment_id,
            actor_id=event.actor_id,
        )
        await self._assert_enabled()
        if event.generation != event.expected_generation + 1:
            raise OptimisticConflictError("CRM lifecycle generation is not the next expected value.")
        auth_result = await self._execute(text(
            "SELECT generation FROM spine.authorization_generations "
            "WHERE workspace_id = :workspace_id AND environment_id = :environment_id FOR SHARE"
        ).bindparams(
            workspace_id=event.workspace_id, environment_id=event.environment_id,
        ))
        if auth_result.scalar_one_or_none() != event.authorization_generation:
            raise OptimisticConflictError("CRM lifecycle actor authority generation is stale.")

        head = crm_relationship_eligibility_heads
        record = event.model_dump(mode="json")
        prior = await self._reserve_idempotency(
            workspace_id=event.workspace_id, environment_id=event.environment_id,
            idempotency_key=event.idempotency_key, payload_digest=event.payload_digest,
            record_kind="lifecycle_event", record_id=event.event_id,
            result=event, result_type=CrmRelationshipLifecycleEvent,
            created_at=event.created_at,
        )
        if prior is not None:
            return prior

        authority = (await self._execute(select(
            crm_relationship_authority_heads.c.lifecycle_generation,
            crm_relationship_authority_heads.c.withdrawn,
            crm_relationship_authority_heads.c.last_event_id,
        ).where(
            crm_relationship_authority_heads.c.workspace_id == event.workspace_id,
            crm_relationship_authority_heads.c.environment_id == event.environment_id,
            crm_relationship_authority_heads.c.relationship_key == event.relationship_key,
        ).with_for_update())).one_or_none()
        if authority is None or authority.lifecycle_generation != event.expected_generation:
            raise OptimisticConflictError("CRM lifecycle authority generation is stale or unavailable.")
        if event.action == "withdraw" and authority.withdrawn:
            raise OptimisticConflictError("CRM relationship is already withdrawn.")
        if event.action == "reapprove":
            if authority.last_event_id != event.predecessor_event_id:
                raise OptimisticConflictError("CRM reapproval must name the current withdrawal event.")
            evidence = (await self._execute(select(
                crm_relationship_receipts.c.receipt_id,
                crm_relationship_receipts.c.outcome,
                crm_relationship_receipts.c.verification_generation,
                crm_relationship_receipts.c.connection_id,
                crm_relationship_receipts.c.connection_version_id,
                crm_relationship_receipts.c.mapping_profile_id,
                crm_relationship_receipts.c.mapping_profile_version,
                crm_relationship_receipts.c.authorization_generation,
                crm_relationship_receipts.c.configuration_generation,
                crm_relationship_claims.c.source_object_id,
                crm_relationship_claims.c.source_revision_id,
                crm_relationship_claims.c.relationship_key,
                crm_relationship_eligibility_heads.c.receipt_id.label("head_receipt_id"),
                crm_relationship_eligibility_heads.c.verification_generation.label("head_generation"),
                crm_relationship_eligibility_heads.c.outcome.label("head_outcome"),
            ).select_from(
                crm_relationship_receipts.join(
                    crm_relationship_claims,
                    (crm_relationship_claims.c.claim_id == crm_relationship_receipts.c.claim_id)
                    & (crm_relationship_claims.c.workspace_id == crm_relationship_receipts.c.workspace_id)
                    & (crm_relationship_claims.c.environment_id == crm_relationship_receipts.c.environment_id),
                ).join(
                    crm_relationship_eligibility_heads,
                    (crm_relationship_eligibility_heads.c.claim_id == crm_relationship_receipts.c.claim_id)
                    & (crm_relationship_eligibility_heads.c.workspace_id == crm_relationship_receipts.c.workspace_id)
                    & (crm_relationship_eligibility_heads.c.environment_id == crm_relationship_receipts.c.environment_id),
                )
            ).where(
                crm_relationship_receipts.c.workspace_id == event.workspace_id,
                crm_relationship_receipts.c.environment_id == event.environment_id,
                crm_relationship_receipts.c.receipt_id == event.reapproval_receipt_id,
                crm_relationship_receipts.c.claim_id == event.claim_id,
                crm_relationship_claims.c.relationship_key == event.relationship_key,
            ))).one_or_none()
            if (
                evidence is None or evidence.outcome != "ELIGIBLE_VERIFIED"
                or evidence.receipt_id != evidence.head_receipt_id
                or evidence.verification_generation != evidence.head_generation
                or evidence.head_outcome != "GOVERNANCE_WITHDRAWN"
            ):
                raise OptimisticConflictError("CRM reapproval requires the current exact positive verification receipt.")
            current_source = (await self._execute(text(
                "SELECT head.current_revision_id, head.current_revision_kind, decision.status "
                "FROM spine.current_source_heads AS head "
                "LEFT JOIN spine.current_lifecycle_authorities AS authority "
                "ON authority.workspace_id = head.workspace_id "
                "AND authority.environment_id = head.environment_id "
                "AND authority.source_object_id = head.source_object_id "
                "LEFT JOIN spine.lifecycle_decisions AS decision "
                "ON decision.workspace_id = authority.workspace_id "
                "AND decision.environment_id = authority.environment_id "
                "AND decision.source_object_id = authority.source_object_id "
                "AND decision.decision_id = authority.current_decision_id "
                "AND decision.generation = authority.generation "
                "WHERE head.workspace_id = :workspace_id AND head.environment_id = :environment_id "
                "AND head.source_object_id = :source_object_id FOR SHARE OF head"
            ).bindparams(
                workspace_id=event.workspace_id, environment_id=event.environment_id,
                source_object_id=evidence.source_object_id,
            ))).one_or_none()
            current_profile = (await self._execute(select(
                crm_relationship_profiles.c.profile_version,
                crm_relationship_profiles.c.record,
            ).where(
                crm_relationship_profiles.c.workspace_id == event.workspace_id,
                crm_relationship_profiles.c.environment_id == event.environment_id,
                crm_relationship_profiles.c.profile_id == evidence.mapping_profile_id,
            ).order_by(crm_relationship_profiles.c.profile_version.desc()).limit(1)
            .with_for_update())).one_or_none()
            latest_connection_version = await self._scalar(text(
                "SELECT max(version) FROM spine.provider_connection_versions "
                "WHERE workspace_id = :workspace_id AND environment_id = :environment_id "
                "AND connection_id = :connection_id"
            ).bindparams(
                workspace_id=event.workspace_id, environment_id=event.environment_id,
                connection_id=evidence.connection_id,
            ))
            if (
                current_source is None
                or current_source.current_revision_id != evidence.source_revision_id
                or current_source.current_revision_kind != "content"
                or current_source.status != "eligible"
                or current_profile is None
                or current_profile.profile_version != evidence.mapping_profile_version
                or evidence.authorization_generation != event.authorization_generation
            ):
                raise OptimisticConflictError("CRM reapproval authority, source, or profile is no longer current.")
            current_connection = (await self._execute(text(
                "SELECT version_record.approval_id, version_record.authorization_generation, "
                "version_record.configuration_generation, version_record.status, "
                "approval.status AS approval_status, version_record.version, "
                "version_record.approved_resource_scope_references "
                "FROM spine.provider_connection_versions AS version_record "
                "JOIN spine.provider_connection_approvals AS approval "
                "ON approval.approval_id = version_record.approval_id "
                "AND approval.workspace_id = version_record.workspace_id "
                "AND approval.environment_id = version_record.environment_id "
                "WHERE version_record.workspace_id = :workspace_id "
                "AND version_record.environment_id = :environment_id "
                "AND version_record.connection_id = :connection_id "
                "AND version_record.connection_version_id = :connection_version_id "
                "FOR SHARE OF version_record, approval"
            ).bindparams(
                workspace_id=event.workspace_id, environment_id=event.environment_id,
                connection_id=evidence.connection_id,
                connection_version_id=evidence.connection_version_id,
            ))).one_or_none()
            try:
                profile = CrmRelationshipMappingProfile.model_validate(current_profile.record)
                identity_record = await self._scalar(select(
                    crm_relationship_identities.c.natural_identity,
                ).where(
                    crm_relationship_identities.c.workspace_id == event.workspace_id,
                    crm_relationship_identities.c.environment_id == event.environment_id,
                    crm_relationship_identities.c.relationship_key == event.relationship_key,
                ))
                identity = CrmRelationshipIdentity.model_validate(identity_record)
            except ValidationError as error:
                raise OptimisticConflictError("CRM reapproval profile or identity is unsupported.") from error
            if (
                current_connection is None
                or current_connection.authorization_generation != event.authorization_generation
                or current_connection.configuration_generation != evidence.configuration_generation
                or current_connection.status != "active"
                or current_connection.approval_status != "active"
                or current_connection.version != latest_connection_version
                or current_connection.approval_id != profile.connection_approval_id
                or profile.qualification_reference not in current_connection.approved_resource_scope_references
                or profile.approval_reference not in current_connection.approved_resource_scope_references
                or not self._profile_matches_identity(profile, identity)
            ):
                raise OptimisticConflictError("CRM reapproval connection authority is no longer current.")
            reapproved_eligibility = CrmRelationshipEligibility(
                eligibility_id=event.reapproval_eligibility_id,
                claim_id=event.claim_id,
                workspace_id=event.workspace_id,
                environment_id=event.environment_id,
                verification_generation=evidence.verification_generation,
                lifecycle_generation=event.generation,
                outcome="ELIGIBLE_VERIFIED",
                evaluated_at=event.created_at,
            )
            await self._execute(insert(crm_relationship_eligibility).values(
                eligibility_id=reapproved_eligibility.eligibility_id,
                claim_id=reapproved_eligibility.claim_id,
                workspace_id=reapproved_eligibility.workspace_id,
                environment_id=reapproved_eligibility.environment_id,
                receipt_id=event.reapproval_receipt_id,
                verification_generation=reapproved_eligibility.verification_generation,
                lifecycle_generation=reapproved_eligibility.lifecycle_generation,
                outcome=reapproved_eligibility.outcome.value,
                record=reapproved_eligibility.model_dump(mode="json"),
                evaluated_at=reapproved_eligibility.evaluated_at,
            ))

        next_withdrawn = event.action == "withdraw" and event.claim_id is None
        changed = await self._execute(update(crm_relationship_authority_heads).where(
            crm_relationship_authority_heads.c.workspace_id == event.workspace_id,
            crm_relationship_authority_heads.c.environment_id == event.environment_id,
            crm_relationship_authority_heads.c.relationship_key == event.relationship_key,
            crm_relationship_authority_heads.c.lifecycle_generation == event.expected_generation,
            crm_relationship_authority_heads.c.withdrawn.is_(authority.withdrawn),
        ).values(
            lifecycle_generation=event.generation,
            withdrawn=next_withdrawn,
            last_event_id=event.event_id,
            updated_at=event.created_at,
        ).returning(crm_relationship_authority_heads.c.relationship_key))
        if changed.scalar_one_or_none() != event.relationship_key:
            raise OptimisticConflictError("CRM lifecycle head changed or claim is unavailable.")
        eligible_claims = select(crm_relationship_claims.c.claim_id).where(
            crm_relationship_claims.c.workspace_id == event.workspace_id,
            crm_relationship_claims.c.environment_id == event.environment_id,
            crm_relationship_claims.c.relationship_key == event.relationship_key,
        )
        if event.claim_id is not None:
            eligible_claims = eligible_claims.where(
                crm_relationship_claims.c.claim_id == event.claim_id
            )
        if event.action == "reapprove":
            await self._execute(update(head).where(
                head.c.workspace_id == event.workspace_id,
                head.c.environment_id == event.environment_id,
                head.c.claim_id == event.claim_id,
            ).values(
                lifecycle_generation=event.generation,
                outcome="ELIGIBLE_VERIFIED",
                eligibility_id=event.reapproval_eligibility_id,
                updated_at=event.created_at,
            ))
        else:
            rows = (await self._execute(select(head).where(
                head.c.workspace_id == event.workspace_id,
                head.c.environment_id == event.environment_id,
                head.c.claim_id.in_(eligible_claims),
            ).with_for_update())).mappings().all()
            for current in rows:
                new_eligibility_id = current["eligibility_id"]
                if current["receipt_id"] is not None:
                    derived = CrmRelationshipEligibility(
                        eligibility_id=uuid5(event.event_id, f"eligibility:{current['claim_id']}"),
                        claim_id=current["claim_id"],
                        workspace_id=event.workspace_id,
                        environment_id=event.environment_id,
                        verification_generation=current["verification_generation"],
                        lifecycle_generation=event.generation,
                        outcome=CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN,
                        evaluated_at=event.created_at,
                    )
                    await self._execute(insert(crm_relationship_eligibility).values(
                        eligibility_id=derived.eligibility_id,
                        claim_id=derived.claim_id,
                        workspace_id=derived.workspace_id,
                        environment_id=derived.environment_id,
                        receipt_id=current["receipt_id"],
                        verification_generation=derived.verification_generation,
                        lifecycle_generation=derived.lifecycle_generation,
                        outcome=derived.outcome.value,
                        record=derived.model_dump(mode="json"),
                        evaluated_at=derived.evaluated_at,
                    ))
                    new_eligibility_id = derived.eligibility_id
                await self._execute(update(head).where(
                    head.c.workspace_id == event.workspace_id,
                    head.c.environment_id == event.environment_id,
                    head.c.claim_id == current["claim_id"],
                    head.c.verification_generation == current["verification_generation"],
                    head.c.lifecycle_generation == current["lifecycle_generation"],
                ).values(
                    lifecycle_generation=event.generation,
                    outcome="GOVERNANCE_WITHDRAWN",
                    eligibility_id=new_eligibility_id,
                    updated_at=event.created_at,
                ))

        audit_id, outbox_id = await self._persist_audit_outbox(
            audit_event=audit_event, outbox_intent=outbox_intent,
            audit_registry=audit_registry, outbox_registry=outbox_registry,
            workspace_id=event.workspace_id, environment_id=event.environment_id,
            actor_id=event.actor_id, created_at=event.created_at,
            record_id=event.event_id,
        )
        await self._execute(insert(crm_relationship_lifecycle_events).values(
            event_id=event.event_id, workspace_id=event.workspace_id,
            environment_id=event.environment_id, relationship_key=event.relationship_key,
            claim_id=event.claim_id, generation=event.generation, action=event.action,
            expected_generation=event.expected_generation, actor_id=event.actor_id,
            authority_id=event.authority_id, authority_version_id=event.authority_version_id,
            authorization_generation=event.authorization_generation,
            predecessor_event_id=event.predecessor_event_id,
            reapproval_receipt_id=event.reapproval_receipt_id,
            reapproval_eligibility_id=event.reapproval_eligibility_id,
            idempotency_key=event.idempotency_key, payload_digest=event.payload_digest,
            audit_event_id=audit_id, outbox_event_id=outbox_id,
            record=record, created_at=event.created_at,
        ))
        return event


__all__ = ["CrmRelationshipV2Store"]


class PostgreSQLCrmRelationshipV2Repository:
    """Tenant-authorized UoW facade for the v2 PostgreSQL store."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], Any],
        context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def current_eligibility(
        self, *, workspace_id: UUID, environment_id: UUID, claim_id: UUID,
        actor_id: UUID,
    ) -> CrmRelationshipEligibilityHead | None:
        async with self._uow(
            "crm_relationship_v2_read", claim_id, actor_id=actor_id,
            workspace_id=workspace_id, environment_id=environment_id,
        ) as uow:
            result = await CrmRelationshipV2Store(uow).current_eligibility(
                workspace_id=workspace_id, environment_id=environment_id,
                claim_id=claim_id, actor_id=actor_id,
            )
            await uow.rollback()
            return result

    async def register_identity(self, identity: CrmRelationshipIdentity, *, actor_id: UUID) -> None:
        async with self._uow(
            "crm_relationship_v2_write", identity.workspace_id, actor_id=actor_id,
            workspace_id=identity.workspace_id, environment_id=identity.environment_id,
        ) as uow:
            await CrmRelationshipV2Store(uow).register_identity(identity, actor_id=actor_id)
            await uow.commit()

    async def register_profile(
        self, *, profile: CrmRelationshipMappingProfile, digest: str,
        created_at: datetime, actor_id: UUID,
    ) -> None:
        async with self._uow(
            "crm_relationship_v2_write", profile.profile_id, actor_id=actor_id,
            workspace_id=profile.workspace_id, environment_id=profile.environment_id,
        ) as uow:
            await CrmRelationshipV2Store(uow).register_profile(
                profile=profile, digest=digest, created_at=created_at,
                actor_id=actor_id,
            )
            await uow.commit()

    async def append_claim(
        self, claim: CrmRelationshipClaim, *,
        audit_event: AuditEvent, outbox_intent: OutboxIntent,
        audit_registry: AuditEventRegistry, outbox_registry: OutboxEventRegistry,
    ) -> CrmRelationshipClaim:
        async with self._uow(
            "crm_relationship_v2_write", claim.claim_id, actor_id=claim.acting_human_id,
            workspace_id=claim.workspace_id, environment_id=claim.environment_id,
        ) as uow:
            result = await CrmRelationshipV2Store(uow).append_claim(
                claim, audit_event=audit_event, outbox_intent=outbox_intent,
                audit_registry=audit_registry, outbox_registry=outbox_registry,
            )
            await uow.commit()
            return result

    async def append_receipt(
        self, receipt: CrmRelationshipObservationReceipt, *,
        eligibility: CrmRelationshipEligibility,
        audit_event: AuditEvent, outbox_intent: OutboxIntent,
        audit_registry: AuditEventRegistry, outbox_registry: OutboxEventRegistry,
        expected_verification_generation: int, lifecycle_generation: int,
    ) -> CrmRelationshipObservationReceipt:
        async with self._uow(
            "crm_relationship_v2_write", receipt.receipt_id,
            actor_id=receipt.acting_human_id,
            workspace_id=receipt.workspace_id, environment_id=receipt.environment_id,
        ) as uow:
            result = await CrmRelationshipV2Store(uow).append_receipt(
                receipt, eligibility=eligibility,
                audit_event=audit_event, outbox_intent=outbox_intent,
                audit_registry=audit_registry, outbox_registry=outbox_registry,
                expected_verification_generation=expected_verification_generation,
                lifecycle_generation=lifecycle_generation,
            )
            await uow.commit()
            return result

    async def append_lifecycle_event(
        self, event: CrmRelationshipLifecycleEvent, *,
        audit_event: AuditEvent, outbox_intent: OutboxIntent,
        audit_registry: AuditEventRegistry, outbox_registry: OutboxEventRegistry,
    ) -> CrmRelationshipLifecycleEvent:
        async with self._uow(
            "crm_relationship_v2_write", event.event_id, actor_id=event.actor_id,
            workspace_id=event.workspace_id, environment_id=event.environment_id,
        ) as uow:
            result = await CrmRelationshipV2Store(uow).append_lifecycle_event(
                event, audit_event=audit_event, outbox_intent=outbox_intent,
                audit_registry=audit_registry, outbox_registry=outbox_registry,
            )
            await uow.commit()
            return result

    def _uow(
        self, operation: str, identity: UUID, *,
        actor_id: UUID | None = None,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ):
        context = self._context_factory(PersistenceOperation(operation), identity)
        if actor_id is not None and (
            context.acting_subject_id != actor_id
            or context.service_principal_id is not None
            or context.origin is not ContextOrigin.INTERACTIVE
            or context.scope != EnvironmentScope(workspace_id, environment_id)
        ):
            raise OptimisticConflictError("CRM v2 writes require the exact current interactive human scope.")
        return self._uow_factory(context)


__all__.append("PostgreSQLCrmRelationshipV2Repository")
