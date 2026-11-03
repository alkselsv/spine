"""Tenant-scoped PostgreSQL ledger for Gmail message state evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from spine.application.gmail_message_state import (
    GmailCurrentLabelState,
    GmailLabelDisposition,
    GmailLabelObservation,
    GmailLabelObservationResult,
    GmailLabelState,
    GmailMessageEvidenceBinding,
    GmailRelationshipEvidence,
)
from spine.application.persistence.context import PersistenceOperation, TrustedPersistenceContext
from spine.domain.sources.gmail import GmailMessageKey, gmail_source_identity
from spine.domain.sources.models import EmailRevisionMetadata
from spine.infrastructure.persistence.postgresql_mappings import (
    current_source_heads,
    gmail_message_label_heads,
    gmail_message_label_observations,
    gmail_message_relationship_evidence,
    source_objects,
    source_revisions,
)


class PostgreSQLGmailMessageStateRepository:
    """Append evidence and advance label heads by provider history ordering."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def resolve_current_binding(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        message_key: GmailMessageKey,
    ) -> GmailMessageEvidenceBinding | None:
        identity = gmail_source_identity(message_key)
        async with self._uow("gmail_message_state_resolve", workspace_id) as uow:
            result = await uow._execute(
                select(
                    source_objects.c.source_object_id,
                    current_source_heads.c.current_revision_id,
                )
                .select_from(
                    source_objects.join(
                        current_source_heads,
                        and_(
                            current_source_heads.c.workspace_id
                            == source_objects.c.workspace_id,
                            current_source_heads.c.environment_id
                            == source_objects.c.environment_id,
                            current_source_heads.c.source_object_id
                            == source_objects.c.source_object_id,
                        ),
                    )
                )
                .where(
                    source_objects.c.workspace_id == workspace_id,
                    source_objects.c.environment_id == environment_id,
                    source_objects.c.connection_id == message_key.connection_id,
                    source_objects.c.external_namespace
                    == identity.external_namespace,
                    source_objects.c.external_generation
                    == identity.external_generation,
                    source_objects.c.external_object_id == identity.external_object_id,
                )
            )
            row = result.mappings().one_or_none()
            await uow.rollback()
            if row is None:
                return None
            return GmailMessageEvidenceBinding(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=row["source_object_id"],
                source_revision_id=row["current_revision_id"],
                message_key=message_key,
            )

    async def record_label(
        self, observation: GmailLabelObservation
    ) -> GmailLabelObservationResult:
        binding = observation.binding
        async with self._uow("gmail_message_state_write", observation.observation_id) as uow:
            existing_row = await self._label_row(uow, observation.observation_id)
            await self._assert_target(uow, binding, require_current=existing_row is None)
            inserted = await uow._execute(
                insert(gmail_message_label_observations)
                .values(
                    observation_id=observation.observation_id,
                    workspace_id=binding.workspace_id,
                    environment_id=binding.environment_id,
                    source_object_id=binding.source_object_id,
                    source_revision_id=binding.source_revision_id,
                    history_id=int(observation.history_id),
                    event_identity=observation.event_identity,
                    label_id=observation.label_id,
                    state=observation.state.value,
                    record=observation.model_dump(mode="json"),
                    received_at=observation.received_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[gmail_message_label_observations.c.observation_id]
                )
                .returning(gmail_message_label_observations.c.observation_id)
            )
            created = inserted.scalar_one_or_none() is not None
            persisted = observation
            if not created:
                if existing_row is None:
                    existing_row = await self._label_row(uow, observation.observation_id)
                if existing_row is None:
                    raise ValueError("Gmail label observation was not persisted")
                persisted = _label_from_row(existing_row)
                if persisted.semantic_identity() != observation.semantic_identity():
                    raise ValueError("Gmail label observation identity conflicts")

            head_values = {
                "workspace_id": binding.workspace_id,
                "environment_id": binding.environment_id,
                "source_object_id": binding.source_object_id,
                "label_id": observation.label_id,
                "observation_id": persisted.observation_id,
                "history_id": int(persisted.history_id),
                "event_identity": persisted.event_identity,
                "state": persisted.state.value,
            }
            advanced = await uow._execute(
                insert(gmail_message_label_heads)
                .values(**head_values)
                .on_conflict_do_update(
                    index_elements=[
                        gmail_message_label_heads.c.workspace_id,
                        gmail_message_label_heads.c.environment_id,
                        gmail_message_label_heads.c.source_object_id,
                        gmail_message_label_heads.c.label_id,
                    ],
                    set_={
                        "observation_id": persisted.observation_id,
                        "history_id": int(persisted.history_id),
                        "event_identity": persisted.event_identity,
                        "state": persisted.state.value,
                    },
                    where=tuple_(
                        gmail_message_label_heads.c.history_id,
                        gmail_message_label_heads.c.event_identity,
                        gmail_message_label_heads.c.observation_id,
                    )
                    < tuple_(
                        int(persisted.history_id),
                        persisted.event_identity,
                        persisted.observation_id,
                    ),
                )
                .returning(gmail_message_label_heads.c.observation_id)
            )
            changed_head = advanced.scalar_one_or_none() is not None
            head = await self._head(uow, binding, observation.label_id)
            if head is None:
                raise ValueError("Gmail label head was not persisted")
            current_row = await self._label_row(uow, head["observation_id"])
            if current_row is None:
                raise ValueError("Gmail label head evidence is unavailable")
            current = _label_from_row(current_row)
            await uow.commit()
            disposition = GmailLabelDisposition.DUPLICATE
            if created:
                disposition = (
                    GmailLabelDisposition.APPLIED
                    if changed_head
                    else GmailLabelDisposition.OUT_OF_ORDER
                )
            return GmailLabelObservationResult(
                disposition=disposition,
                persisted=persisted,
                current=GmailCurrentLabelState(
                    observation=current,
                    present=current.state is GmailLabelState.PRESENT,
                ),
            )

    async def record_relationship(
        self, evidence: GmailRelationshipEvidence
    ) -> GmailRelationshipEvidence:
        binding = evidence.binding
        async with self._uow("gmail_message_relationship_write", evidence.evidence_id) as uow:
            existing_row = await self._relationship_row(uow, evidence.evidence_id)
            revision_evidence = await self._assert_target(
                uow, binding, require_current=existing_row is None
            )
            if not evidence.is_claimed_by(revision_evidence):
                raise ValueError("Gmail relationship is absent from the bound revision")
            if existing_row is None:
                await uow._execute(
                    insert(gmail_message_relationship_evidence)
                    .values(
                        evidence_id=evidence.evidence_id,
                        workspace_id=binding.workspace_id,
                        environment_id=binding.environment_id,
                        source_object_id=binding.source_object_id,
                        source_revision_id=binding.source_revision_id,
                        kind=evidence.kind.value,
                        authority=evidence.authority.value,
                        record=evidence.model_dump(mode="json"),
                        received_at=evidence.received_at,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            gmail_message_relationship_evidence.c.evidence_id
                        ]
                    )
                )
            row = existing_row or await self._relationship_row(uow, evidence.evidence_id)
            if row is None:
                raise ValueError("Gmail relationship evidence was not persisted")
            persisted = _relationship_from_row(row)
            if persisted.semantic_identity() != evidence.semantic_identity():
                raise ValueError("Gmail relationship evidence identity conflicts")
            await uow.commit()
            return persisted

    async def list_label_observations(
        self, binding: GmailMessageEvidenceBinding
    ) -> tuple[GmailLabelObservation, ...]:
        async with self._uow("gmail_message_state_read", binding.source_object_id) as uow:
            await self._assert_target(uow, binding, require_current=False)
            result = await uow._execute(
                select(gmail_message_label_observations)
                .where(*self._binding_predicates(gmail_message_label_observations, binding))
                .order_by(
                    gmail_message_label_observations.c.history_id,
                    gmail_message_label_observations.c.event_identity,
                    gmail_message_label_observations.c.observation_id,
                )
            )
            records = tuple(_label_from_row(row) for row in result.mappings().all())
            await uow.rollback()
            return records

    async def list_relationships(
        self, binding: GmailMessageEvidenceBinding
    ) -> tuple[GmailRelationshipEvidence, ...]:
        async with self._uow("gmail_message_relationship_read", binding.source_object_id) as uow:
            await self._assert_target(uow, binding, require_current=False)
            result = await uow._execute(
                select(gmail_message_relationship_evidence)
                .where(*self._binding_predicates(gmail_message_relationship_evidence, binding))
                .order_by(
                    gmail_message_relationship_evidence.c.kind.desc(),
                    gmail_message_relationship_evidence.c.evidence_id,
                )
            )
            records = tuple(
                sorted(
                    (
                        _relationship_from_row(row)
                        for row in result.mappings().all()
                    ),
                    key=lambda item: item.sort_key(),
                )
            )
            await uow.rollback()
            return records

    def _uow(self, operation: str, identity: UUID):
        return self._uow_factory(
            self._context_factory(PersistenceOperation(operation), identity)
        )

    @staticmethod
    def _binding_predicates(table: Any, binding: GmailMessageEvidenceBinding) -> tuple[Any, ...]:
        return (
            table.c.workspace_id == binding.workspace_id,
            table.c.environment_id == binding.environment_id,
            table.c.source_object_id == binding.source_object_id,
            table.c.source_revision_id == binding.source_revision_id,
        )

    @staticmethod
    async def _label_row(uow: Any, observation_id: UUID) -> Any | None:
        result = await uow._execute(
            select(gmail_message_label_observations).where(
                gmail_message_label_observations.c.observation_id == observation_id
            )
        )
        return result.mappings().one_or_none()

    @staticmethod
    async def _relationship_row(uow: Any, evidence_id: UUID) -> Any | None:
        result = await uow._execute(
            select(gmail_message_relationship_evidence).where(
                gmail_message_relationship_evidence.c.evidence_id == evidence_id
            )
        )
        return result.mappings().one_or_none()

    @staticmethod
    async def _head(
        uow: Any, binding: GmailMessageEvidenceBinding, label_id: str
    ) -> Any | None:
        result = await uow._execute(
            select(gmail_message_label_heads).where(
                gmail_message_label_heads.c.workspace_id == binding.workspace_id,
                gmail_message_label_heads.c.environment_id == binding.environment_id,
                gmail_message_label_heads.c.source_object_id == binding.source_object_id,
                gmail_message_label_heads.c.label_id == label_id,
            )
        )
        return result.mappings().one_or_none()

    @staticmethod
    async def _assert_target(
        uow: Any,
        binding: GmailMessageEvidenceBinding,
        *,
        require_current: bool,
    ) -> EmailRevisionMetadata:
        identity = gmail_source_identity(binding.message_key)
        result = await uow._execute(
            select(
                source_objects.c.connection_id,
                source_objects.c.external_namespace,
                source_objects.c.external_generation,
                source_objects.c.external_object_id,
                source_revisions.c.revision_metadata,
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
                source_objects.c.workspace_id == binding.workspace_id,
                source_objects.c.environment_id == binding.environment_id,
                source_objects.c.source_object_id == binding.source_object_id,
                source_revisions.c.revision_id == binding.source_revision_id,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ValueError("Gmail message evidence target is unavailable in this tenant")
        metadata = row["revision_metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        email_payload = (
            metadata.get("email_revision") if isinstance(metadata, dict) else None
        )
        try:
            email = EmailRevisionMetadata.model_validate(email_payload)
        except Exception:
            email = None
        if (
            row["connection_id"] != binding.message_key.connection_id
            or row["external_namespace"] != identity.external_namespace
            or row["external_generation"] != identity.external_generation
            or row["external_object_id"] != identity.external_object_id
            or email is None
            or email.provider_kind != "google"
            or email.provider_message_id != binding.message_key.message_id
        ):
            raise ValueError("Gmail message evidence target identity does not match")
        if require_current:
            head = await uow._execute(
                select(current_source_heads.c.current_revision_id).where(
                    current_source_heads.c.workspace_id == binding.workspace_id,
                    current_source_heads.c.environment_id == binding.environment_id,
                    current_source_heads.c.source_object_id == binding.source_object_id,
                    current_source_heads.c.current_revision_id
                    == binding.source_revision_id,
                )
            )
            if head.scalar_one_or_none() is None:
                raise ValueError("Gmail message evidence target is not the current revision")
        return email


def _label_from_row(row: Any) -> GmailLabelObservation:
    return GmailLabelObservation.model_validate(row["record"])


def _relationship_from_row(row: Any) -> GmailRelationshipEvidence:
    return GmailRelationshipEvidence.model_validate(row["record"])


__all__ = ["PostgreSQLGmailMessageStateRepository"]
