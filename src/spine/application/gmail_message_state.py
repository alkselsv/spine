"""Immutable Gmail message state and relationship evidence.

Label observations are deliberately separate from ``SourceRevision``.  They bind
to the exact raw-message revision that was current when the provider state was
observed, while duplicate and late history delivery only changes the materialized
label head when its provider ordering wins.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import json
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.sources.gmail import GmailMessageKey
from spine.domain.sources.models import EmailRevisionMetadata


class GmailLabelState(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"


class GmailLabelDisposition(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"


class GmailRelationshipKind(str, Enum):
    PROVIDER_THREAD = "provider_thread"
    REFERENCES = "references"
    IN_REPLY_TO = "in_reply_to"


class GmailRelationshipAuthority(str, Enum):
    PROVIDER = "provider_reported_thread_id"
    HEADER_INFERRED = "header_inferred"


class GmailMessageEvidenceBinding(DefinitionModel):
    """Exact tenant, provider message and immutable content-revision binding."""

    schema_version: Literal["gmail-message-evidence-binding:v1"] = (
        "gmail-message-evidence-binding:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    message_key: GmailMessageKey

    @model_validator(mode="after")
    def validate_nonzero_ids(self) -> "GmailMessageEvidenceBinding":
        for name in (
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        return self


class GmailLabelObservation(DefinitionModel):
    """One immutable provider-history statement about one Gmail label."""

    schema_version: Literal["gmail-label-observation:v1"] = (
        "gmail-label-observation:v1"
    )
    observation_id: UUID
    binding: GmailMessageEvidenceBinding
    history_id: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    event_identity: str = Field(min_length=1, max_length=4096)
    label_id: str = Field(min_length=1, max_length=1024, pattern=r"^[!-~]+$")
    state: GmailLabelState
    received_at: datetime

    @classmethod
    def create(
        cls,
        *,
        binding: GmailMessageEvidenceBinding,
        history_id: str,
        event_identity: str,
        label_id: str,
        state: GmailLabelState,
        received_at: datetime,
    ) -> "GmailLabelObservation":
        return cls(
            observation_id=_label_observation_id(
                binding,
                history_id=history_id,
                event_identity=event_identity,
                label_id=label_id,
                state=state,
            ),
            binding=binding,
            history_id=history_id,
            event_identity=event_identity,
            label_id=label_id,
            state=state,
            received_at=received_at,
        )

    @model_validator(mode="after")
    def validate_observation(self) -> "GmailLabelObservation":
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        if self.observation_id != _label_observation_id(
            self.binding,
            history_id=self.history_id,
            event_identity=self.event_identity,
            label_id=self.label_id,
            state=self.state,
        ):
            raise ValueError("observation_id does not match Gmail label evidence")
        return self

    def semantic_identity(self) -> tuple[object, ...]:
        """Return replay-relevant fields, excluding first-receipt time."""

        return (
            self.binding,
            self.history_id,
            self.event_identity,
            self.label_id,
            self.state,
        )


class GmailCurrentLabelState(DefinitionModel):
    schema_version: Literal["gmail-current-label-state:v1"] = (
        "gmail-current-label-state:v1"
    )
    observation: GmailLabelObservation
    present: bool

    @model_validator(mode="after")
    def validate_state(self) -> "GmailCurrentLabelState":
        if self.present != (self.observation.state is GmailLabelState.PRESENT):
            raise ValueError("current label state conflicts with its observation")
        return self


class GmailLabelObservationResult(DefinitionModel):
    """Safe convergence diagnostic for one content-free label observation."""

    schema_version: Literal["gmail-label-observation-result:v1"] = (
        "gmail-label-observation-result:v1"
    )
    disposition: GmailLabelDisposition
    persisted: GmailLabelObservation
    current: GmailCurrentLabelState


class GmailRelationshipEvidence(DefinitionModel):
    """Relationship evidence whose authority is fixed by relationship kind."""

    schema_version: Literal["gmail-relationship-evidence:v1"] = (
        "gmail-relationship-evidence:v1"
    )
    evidence_id: UUID
    binding: GmailMessageEvidenceBinding
    kind: GmailRelationshipKind
    authority: GmailRelationshipAuthority
    relationship_reference: str = Field(min_length=1, max_length=4096)
    received_at: datetime

    @model_validator(mode="after")
    def validate_authority(self) -> "GmailRelationshipEvidence":
        expected = (
            GmailRelationshipAuthority.PROVIDER
            if self.kind is GmailRelationshipKind.PROVIDER_THREAD
            else GmailRelationshipAuthority.HEADER_INFERRED
        )
        if self.authority is not expected:
            raise ValueError("relationship kind cannot be upgraded to another authority")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        if self.evidence_id != _relationship_evidence_id(
            self.binding,
            kind=self.kind,
            authority=self.authority,
            reference=self.relationship_reference,
        ):
            raise ValueError("evidence_id does not match Gmail relationship evidence")
        return self

    def semantic_identity(self) -> tuple[object, ...]:
        """Return replay-relevant fields, excluding first-receipt time."""

        return (
            self.binding,
            self.kind,
            self.authority,
            self.relationship_reference,
        )

    def is_claimed_by(self, revision: EmailRevisionMetadata) -> bool:
        """Check that this relationship exists in the exact raw revision."""

        if self.kind is GmailRelationshipKind.PROVIDER_THREAD:
            return self.relationship_reference == revision.provider_thread_reference
        if self.kind is GmailRelationshipKind.REFERENCES:
            return self.relationship_reference in revision.references_claims
        return self.relationship_reference in revision.in_reply_to_claims

    def sort_key(self) -> tuple[int, str, str]:
        rank = {
            GmailRelationshipKind.PROVIDER_THREAD: 0,
            GmailRelationshipKind.REFERENCES: 1,
            GmailRelationshipKind.IN_REPLY_TO: 2,
        }
        return (rank[self.kind], self.relationship_reference, str(self.evidence_id))


class GmailMessageStateRepository(Protocol):
    async def resolve_current_binding(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        message_key: GmailMessageKey,
    ) -> GmailMessageEvidenceBinding | None: ...

    async def record_label(
        self, observation: GmailLabelObservation
    ) -> GmailLabelObservationResult: ...

    async def record_relationship(
        self, evidence: GmailRelationshipEvidence
    ) -> GmailRelationshipEvidence: ...

    async def list_label_observations(
        self, binding: GmailMessageEvidenceBinding
    ) -> tuple[GmailLabelObservation, ...]: ...

    async def list_relationships(
        self, binding: GmailMessageEvidenceBinding
    ) -> tuple[GmailRelationshipEvidence, ...]: ...


class GmailMessageStateUnavailableError(RuntimeError):
    """A disclosure-safe failure to bind state evidence to a tenant source."""

    def __init__(self) -> None:
        super().__init__("Gmail message state target is unavailable in this tenant")


class InMemoryGmailMessageStateRepository:
    """Offline reference implementation with the same convergence semantics."""

    def __init__(
        self,
        allowed_bindings: tuple[GmailMessageEvidenceBinding, ...] = (),
        revision_evidence: tuple[
            tuple[GmailMessageEvidenceBinding, EmailRevisionMetadata], ...
        ] = (),
        current_bindings: tuple[GmailMessageEvidenceBinding, ...] | None = None,
    ) -> None:
        self._targets = {_binding_key(item) for item in allowed_bindings}
        self._current_targets = {
            _source_key(item.workspace_id, item.environment_id, item.message_key): item
            for item in (
                allowed_bindings if current_bindings is None else current_bindings
            )
        }
        self._revision_evidence = {
            _binding_key(item): evidence for item, evidence in revision_evidence
        }
        self._labels: dict[UUID, GmailLabelObservation] = {}
        self._heads: dict[tuple[str, str], GmailLabelObservation] = {}
        self._relationships: dict[UUID, GmailRelationshipEvidence] = {}

    async def resolve_current_binding(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        message_key: GmailMessageKey,
    ) -> GmailMessageEvidenceBinding | None:
        return self._current_targets.get(
            _source_key(workspace_id, environment_id, message_key)
        )

    def set_current_binding(self, binding: GmailMessageEvidenceBinding) -> None:
        """Advance the offline fake's source head for lifecycle scenarios."""

        self._require_target(binding)
        self._current_targets[
            _source_key(binding.workspace_id, binding.environment_id, binding.message_key)
        ] = binding

    def clear_current_binding(self, binding: GmailMessageEvidenceBinding) -> None:
        """Model a tombstoned source with no current content revision."""

        self._current_targets.pop(
            _source_key(binding.workspace_id, binding.environment_id, binding.message_key),
            None,
        )

    async def record_label(
        self, observation: GmailLabelObservation
    ) -> GmailLabelObservationResult:
        self._require_target(observation.binding)
        existing = self._labels.get(observation.observation_id)
        if existing is not None:
            if existing.semantic_identity() != observation.semantic_identity():
                raise ValueError("Gmail label observation identity conflicts")
            persisted = existing
            disposition = GmailLabelDisposition.DUPLICATE
        else:
            self._require_current_target(observation.binding)
            self._labels[observation.observation_id] = observation
            persisted = observation
            disposition = GmailLabelDisposition.APPLIED

        head_key = (_source_binding_key(observation.binding), observation.label_id)
        current = self._heads.get(head_key)
        if current is None or _label_order(observation) > _label_order(current):
            self._heads[head_key] = persisted
            current = persisted
        elif existing is None:
            disposition = GmailLabelDisposition.OUT_OF_ORDER
        assert current is not None
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
        self._require_target(evidence.binding)
        existing = self._relationships.get(evidence.evidence_id)
        if existing is None:
            self._require_current_target(evidence.binding)
        revision = self._revision_evidence.get(_binding_key(evidence.binding))
        if revision is None or not evidence.is_claimed_by(revision):
            raise ValueError("Gmail relationship is absent from the bound revision")
        if existing is not None:
            if existing.semantic_identity() != evidence.semantic_identity():
                raise ValueError("Gmail relationship evidence identity conflicts")
            return existing
        self._relationships[evidence.evidence_id] = evidence
        return evidence

    async def list_label_observations(
        self, binding: GmailMessageEvidenceBinding
    ) -> tuple[GmailLabelObservation, ...]:
        self._require_target(binding)
        return tuple(
            sorted(
                (
                    item
                    for item in self._labels.values()
                    if item.binding == binding
                ),
                key=_label_order,
            )
        )

    async def list_relationships(
        self, binding: GmailMessageEvidenceBinding
    ) -> tuple[GmailRelationshipEvidence, ...]:
        self._require_target(binding)
        return tuple(
            sorted(
                (
                    item
                    for item in self._relationships.values()
                    if item.binding == binding
                ),
                key=lambda item: item.sort_key(),
            )
        )

    def _require_target(self, binding: GmailMessageEvidenceBinding) -> None:
        if _binding_key(binding) not in self._targets:
            raise GmailMessageStateUnavailableError()

    def _require_current_target(self, binding: GmailMessageEvidenceBinding) -> None:
        current = self._current_targets.get(
            _source_key(binding.workspace_id, binding.environment_id, binding.message_key)
        )
        if current != binding:
            raise GmailMessageStateUnavailableError()


def gmail_relationship_observations(
    binding: GmailMessageEvidenceBinding,
    *,
    revision_evidence: EmailRevisionMetadata,
    received_at: datetime,
) -> tuple[GmailRelationshipEvidence, ...]:
    """Bind provider and header claims without conflating their authority."""

    if revision_evidence.provider_kind != "google" or (
        revision_evidence.provider_message_id != binding.message_key.message_id
    ):
        raise ValueError("email revision evidence does not match the Gmail message")
    values: list[tuple[GmailRelationshipKind, GmailRelationshipAuthority, str]] = []
    if revision_evidence.provider_thread_reference is not None:
        values.append(
            (
                GmailRelationshipKind.PROVIDER_THREAD,
                GmailRelationshipAuthority.PROVIDER,
                revision_evidence.provider_thread_reference,
            )
        )
    values.extend(
        (GmailRelationshipKind.REFERENCES, GmailRelationshipAuthority.HEADER_INFERRED, value)
        for value in revision_evidence.references_claims
    )
    values.extend(
        (GmailRelationshipKind.IN_REPLY_TO, GmailRelationshipAuthority.HEADER_INFERRED, value)
        for value in revision_evidence.in_reply_to_claims
    )
    return tuple(
        GmailRelationshipEvidence(
            evidence_id=_relationship_evidence_id(
                binding, kind=kind, authority=authority, reference=reference
            ),
            binding=binding,
            kind=kind,
            authority=authority,
            relationship_reference=reference,
            received_at=received_at,
        )
        for kind, authority, reference in values
    )


def _canonical_identity(kind: str, payload: dict[str, object]) -> UUID:
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return uuid5(NAMESPACE_URL, f"{kind}:{serialized}")


def _label_observation_id(
    binding: GmailMessageEvidenceBinding,
    *,
    history_id: str,
    event_identity: str,
    label_id: str,
    state: GmailLabelState,
) -> UUID:
    return _canonical_identity(
        "gmail-label-observation:v1",
        {
            "binding": binding.model_dump(mode="json"),
            "history_id": history_id,
            "event_identity": event_identity,
            "label_id": label_id,
            "state": state.value,
        },
    )


def _relationship_evidence_id(
    binding: GmailMessageEvidenceBinding,
    *,
    kind: GmailRelationshipKind,
    authority: GmailRelationshipAuthority,
    reference: str,
) -> UUID:
    return _canonical_identity(
        "gmail-relationship-evidence:v1",
        {
            "binding": binding.model_dump(mode="json"),
            "kind": kind.value,
            "authority": authority.value,
            "reference": reference,
        },
    )


def _binding_key(binding: GmailMessageEvidenceBinding) -> str:
    return binding.model_dump_json()


def _source_binding_key(binding: GmailMessageEvidenceBinding) -> str:
    return json.dumps(
        {
            "workspace_id": str(binding.workspace_id),
            "environment_id": str(binding.environment_id),
            "source_object_id": str(binding.source_object_id),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _source_key(
    workspace_id: UUID, environment_id: UUID, message_key: GmailMessageKey
) -> str:
    return json.dumps(
        {
            "workspace_id": str(workspace_id),
            "environment_id": str(environment_id),
            "message_key": message_key.model_dump(mode="json"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _label_order(observation: GmailLabelObservation) -> tuple[int, str, str]:
    return (
        int(observation.history_id),
        observation.event_identity,
        str(observation.observation_id),
    )


__all__ = [
    "GmailCurrentLabelState",
    "GmailLabelDisposition",
    "GmailLabelObservation",
    "GmailLabelObservationResult",
    "GmailLabelState",
    "GmailMessageEvidenceBinding",
    "GmailMessageStateRepository",
    "GmailMessageStateUnavailableError",
    "GmailRelationshipAuthority",
    "GmailRelationshipEvidence",
    "GmailRelationshipKind",
    "InMemoryGmailMessageStateRepository",
    "gmail_relationship_observations",
]
