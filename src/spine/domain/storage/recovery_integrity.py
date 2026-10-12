"""Hash-linked recovery evidence and fail-closed trust decisions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, model_validator

from .recovery import StorageRecoveryManifest, _Contract


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RecoveryEvidenceEventKind(str, Enum):
    """The only events that can change recovery-evidence trust."""

    INVALIDATED = "invalidated"
    REPLACED = "replaced"


class RecoveryEvidenceFailure(str, Enum):
    """Disclosure-safe reason for an indeterminate or unavailable chain."""

    NO_COHERENT_MANIFEST = "no_coherent_manifest"
    MIXED_DEPLOYMENT_SCOPE = "mixed_deployment_scope"
    DUPLICATE_MANIFEST = "duplicate_manifest"
    DUPLICATE_SEQUENCE = "duplicate_sequence"
    NON_CONTIGUOUS_SEQUENCE = "non_contiguous_sequence"
    WRONG_PREVIOUS_DIGEST = "wrong_previous_digest"
    INVALID_EVENT_DIGEST = "invalid_event_digest"
    UNKNOWN_MANIFEST = "unknown_manifest"
    MANIFEST_DIGEST_MISMATCH = "manifest_digest_mismatch"
    INVALID_REPLACEMENT = "invalid_replacement"
    INVALID_EVENT = "invalid_event"
    MIXED_GENERATION = "mixed_generation"
    TRUNCATED_CHAIN = "truncated_chain"


class RecoveryTrustState(str, Enum):
    """Derived trust state; it is never persisted as mutable authority."""

    TRUSTED = "trusted"
    INVALIDATED = "invalidated"
    INDETERMINATE = "indeterminate"
    UNAVAILABLE = "unavailable"


class RecoveryEvidenceInvalidation(_Contract):
    """One immutable, append-only event in a deployment recovery chain."""

    schema_version: PositiveInt = 1
    deployment_scope_id: str = Field(min_length=1, max_length=256)
    sequence: PositiveInt
    recovery_set_id: str = Field(min_length=1, max_length=256)
    manifest_digest: str = Field(min_length=64, max_length=64)
    event_kind: RecoveryEvidenceEventKind
    reason_code: str = Field(min_length=1, max_length=128)
    replacement_recovery_set_id: str | None = Field(default=None, min_length=1, max_length=256)
    replacement_manifest_digest: str | None = Field(default=None, min_length=64, max_length=64)
    replacement_validation_reference: str | None = Field(
        default=None, min_length=1, max_length=256
    )
    replacement_approval_reference: str | None = Field(
        default=None, min_length=1, max_length=256
    )
    previous_event_digest: str | None = Field(default=None, min_length=64, max_length=64)
    event_digest: str = Field(min_length=64, max_length=64)
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_event(self) -> "RecoveryEvidenceInvalidation":
        for name in (
            "deployment_scope_id",
            "recovery_set_id",
            "reason_code",
            "replacement_recovery_set_id",
            "replacement_validation_reference",
            "replacement_approval_reference",
        ):
            value = getattr(self, name)
            if value is not None:
                self._validate_safe_identifier(value, name)
        self._validate_timestamp(self.occurred_at, "occurred_at")
        for name in (
            "manifest_digest",
            "replacement_manifest_digest",
            "previous_event_digest",
            "event_digest",
        ):
            value = getattr(self, name)
            if value is not None and not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be lowercase sha256")
        if self.sequence == 1 and self.previous_event_digest is not None:
            raise ValueError("first recovery evidence event cannot have a predecessor")
        if self.sequence > 1 and self.previous_event_digest is None:
            raise ValueError("non-first recovery evidence event requires a predecessor")
        replacement_values = (
            self.replacement_recovery_set_id,
            self.replacement_manifest_digest,
            self.replacement_validation_reference,
            self.replacement_approval_reference,
        )
        if self.event_kind is RecoveryEvidenceEventKind.INVALIDATED and any(
            value is not None for value in replacement_values
        ):
            raise ValueError("invalidated event cannot carry replacement references")
        if self.event_kind is RecoveryEvidenceEventKind.REPLACED:
            if any(value is None for value in replacement_values):
                raise ValueError("replacement validation and approval references are required")
            if self.replacement_recovery_set_id == self.recovery_set_id:
                raise ValueError("replacement must reference a different recovery set")
            if self.replacement_manifest_digest == self.manifest_digest:
                raise ValueError("replacement must reference a different manifest digest")
        if self.event_digest != _event_digest(self):
            raise ValueError("event_digest does not match immutable event fields")
        return self

    @classmethod
    def build(cls, **values: object) -> "RecoveryEvidenceInvalidation":
        values.setdefault("schema_version", 1)
        values["event_digest"] = "0" * 64
        draft = cls.model_construct(**values)
        values["event_digest"] = _event_digest(draft)
        return cls.model_validate(values)


class RecoveryEvidenceVerification(BaseModel):
    """Purely derived verification result for one restored recovery scope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    deployment_scope_id: str
    trust_state: RecoveryTrustState
    chain_complete: bool
    current_recovery_set_id: str | None = None
    current_manifest_digest: str | None = None
    last_sequence: int | None = None
    last_event_digest: str | None = None
    failure: RecoveryEvidenceFailure | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "RecoveryEvidenceVerification":
        if self.chain_complete and self.failure is not None:
            raise ValueError("complete chain cannot have a failure")
        if self.trust_state is RecoveryTrustState.TRUSTED and not self.chain_complete:
            raise ValueError("trusted recovery evidence requires a complete chain")
        if self.last_sequence is None and self.last_event_digest is not None:
            raise ValueError("last event digest requires a sequence")
        return self


class RecoveryReadiness(BaseModel):
    """Disclosure-safe process readiness derived from recovery trust."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ready: bool
    code: str = Field(min_length=1, max_length=128)
    trust_state: RecoveryTrustState


def derive_recovery_readiness(
    verification: RecoveryEvidenceVerification,
) -> RecoveryReadiness:
    """Allow readiness only for a fully verified, currently trusted chain."""

    if verification.trust_state is RecoveryTrustState.TRUSTED:
        return RecoveryReadiness(
            ready=True,
            code="recovery.ready",
            trust_state=verification.trust_state,
        )
    if verification.trust_state is RecoveryTrustState.INVALIDATED:
        code = "recovery.evidence_invalidated"
    elif verification.trust_state is RecoveryTrustState.INDETERMINATE:
        code = "recovery.evidence_indeterminate"
    else:
        code = "recovery.evidence_unavailable"
    return RecoveryReadiness(
        ready=False,
        code=code,
        trust_state=verification.trust_state,
    )


def verify_recovery_evidence_chain(
    deployment_scope_id: str,
    *,
    manifests: Iterable[StorageRecoveryManifest],
    invalidations: Iterable[RecoveryEvidenceInvalidation],
    expected_last_sequence: int | None = None,
    expected_last_event_digest: str | None = None,
) -> RecoveryEvidenceVerification:
    """Verify a complete immutable chain without mutating restored state."""

    manifest_values = tuple(manifests)
    event_values = tuple(invalidations)
    try:
        validated_manifests = tuple(
            StorageRecoveryManifest.model_validate(
                value.model_dump(mode="python"), strict=False
            )
            for value in manifest_values
        )
        validated_events = tuple(
            RecoveryEvidenceInvalidation.model_validate(
                value.model_dump(mode="python"), strict=False
            )
            for value in event_values
        )
    except (AttributeError, ValidationError):
        return _failure(
            deployment_scope_id,
            RecoveryTrustState.INDETERMINATE,
            RecoveryEvidenceFailure.INVALID_EVENT,
        )

    if any(value.deployment_scope_id != deployment_scope_id for value in validated_manifests):
        return _failure(
            deployment_scope_id,
            RecoveryTrustState.INDETERMINATE,
            RecoveryEvidenceFailure.MIXED_DEPLOYMENT_SCOPE,
        )
    if any(value.deployment_scope_id != deployment_scope_id for value in validated_events):
        return _failure(
            deployment_scope_id,
            RecoveryTrustState.INDETERMINATE,
            RecoveryEvidenceFailure.MIXED_DEPLOYMENT_SCOPE,
        )

    by_id: dict[str, StorageRecoveryManifest] = {}
    for value in validated_manifests:
        if value.recovery_set_id in by_id:
            return _failure(
                deployment_scope_id,
                RecoveryTrustState.INDETERMINATE,
                RecoveryEvidenceFailure.DUPLICATE_MANIFEST,
            )
        by_id[value.recovery_set_id] = value
    coherent = tuple(
        value
        for value in validated_manifests
        if value.manifest_status.value == "coherent"
    )
    if not coherent:
        return _failure(
            deployment_scope_id,
            RecoveryTrustState.UNAVAILABLE,
            RecoveryEvidenceFailure.NO_COHERENT_MANIFEST,
        )
    ordered_events = sorted(validated_events, key=lambda value: value.sequence)
    sequences = tuple(value.sequence for value in ordered_events)
    if len(set(sequences)) != len(sequences):
        return _failure(
            deployment_scope_id,
            RecoveryTrustState.INDETERMINATE,
            RecoveryEvidenceFailure.DUPLICATE_SEQUENCE,
        )
    if sequences != tuple(range(1, len(ordered_events) + 1)):
        return _failure(
            deployment_scope_id,
            RecoveryTrustState.INDETERMINATE,
            RecoveryEvidenceFailure.NON_CONTIGUOUS_SEQUENCE,
        )
    actual_last_sequence = ordered_events[-1].sequence if ordered_events else 0
    actual_last_digest = ordered_events[-1].event_digest if ordered_events else None
    invalid_expected_head = (
        expected_last_sequence is not None
        and (
            expected_last_sequence != actual_last_sequence
            or (
                expected_last_sequence == 0
                and expected_last_event_digest is not None
            )
            or (
                expected_last_sequence > 0
                and expected_last_event_digest != actual_last_digest
            )
        )
    ) or (expected_last_sequence is None and expected_last_event_digest is not None)
    if invalid_expected_head:
        return _failure(
            deployment_scope_id,
            RecoveryTrustState.INDETERMINATE,
            RecoveryEvidenceFailure.TRUNCATED_CHAIN,
            last_sequence=actual_last_sequence or None,
        )

    if ordered_events:
        initial = by_id.get(ordered_events[0].recovery_set_id)
        if (
            initial is None
            or initial.manifest_digest != ordered_events[0].manifest_digest
            or initial.manifest_status.value != "coherent"
        ):
            return _failure(
                deployment_scope_id,
                RecoveryTrustState.INDETERMINATE,
                RecoveryEvidenceFailure.UNKNOWN_MANIFEST,
                last_sequence=ordered_events[0].sequence,
            )
        current_id = initial.recovery_set_id
        current_digest = initial.manifest_digest
    else:
        latest = max(coherent, key=lambda value: (value.created_at, value.recovery_set_id))
        current_id = latest.recovery_set_id
        current_digest = latest.manifest_digest

    previous_digest: str | None = None
    trust_state = RecoveryTrustState.TRUSTED
    for event in ordered_events:
        if event.previous_event_digest != previous_digest:
            return _failure(
                deployment_scope_id,
                RecoveryTrustState.INDETERMINATE,
                RecoveryEvidenceFailure.WRONG_PREVIOUS_DIGEST,
                last_sequence=event.sequence,
            )
        target = by_id.get(event.recovery_set_id)
        if target is None:
            return _failure(
                deployment_scope_id,
                RecoveryTrustState.INDETERMINATE,
                RecoveryEvidenceFailure.UNKNOWN_MANIFEST,
                last_sequence=event.sequence,
            )
        if target.manifest_digest != event.manifest_digest:
            return _failure(
                deployment_scope_id,
                RecoveryTrustState.INDETERMINATE,
                RecoveryEvidenceFailure.MANIFEST_DIGEST_MISMATCH,
                last_sequence=event.sequence,
            )
        if event.recovery_set_id != current_id or event.manifest_digest != current_digest:
            return _failure(
                deployment_scope_id,
                RecoveryTrustState.INDETERMINATE,
                RecoveryEvidenceFailure.MIXED_GENERATION,
                last_sequence=event.sequence,
            )
        if event.event_kind is RecoveryEvidenceEventKind.INVALIDATED:
            trust_state = RecoveryTrustState.INVALIDATED
        else:
            replacement_id = event.replacement_recovery_set_id
            replacement_digest = event.replacement_manifest_digest
            replacement = by_id.get(replacement_id or "")
            if (
                replacement is None
                or replacement.manifest_status.value != "coherent"
                or replacement.manifest_digest != replacement_digest
                or event.replacement_validation_reference is None
                or event.replacement_approval_reference is None
            ):
                return _failure(
                    deployment_scope_id,
                    RecoveryTrustState.INDETERMINATE,
                    RecoveryEvidenceFailure.INVALID_REPLACEMENT,
                    last_sequence=event.sequence,
                )
            current_id = replacement.recovery_set_id
            current_digest = replacement.manifest_digest
            trust_state = RecoveryTrustState.TRUSTED
        previous_digest = event.event_digest

    return RecoveryEvidenceVerification(
        deployment_scope_id=deployment_scope_id,
        trust_state=trust_state,
        chain_complete=True,
        current_recovery_set_id=current_id,
        current_manifest_digest=current_digest,
        last_sequence=(ordered_events[-1].sequence if ordered_events else None),
        last_event_digest=(ordered_events[-1].event_digest if ordered_events else None),
    )


def _failure(
    deployment_scope_id: str,
    trust_state: RecoveryTrustState,
    failure: RecoveryEvidenceFailure,
    *,
    last_sequence: int | None = None,
) -> RecoveryEvidenceVerification:
    return RecoveryEvidenceVerification(
        deployment_scope_id=deployment_scope_id,
        trust_state=trust_state,
        chain_complete=False,
        last_sequence=last_sequence,
        failure=failure,
    )


def _event_digest(event: BaseModel) -> str:
    payload = event.model_dump(mode="json", exclude={"event_digest"})
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "RecoveryEvidenceEventKind",
    "RecoveryEvidenceFailure",
    "RecoveryEvidenceInvalidation",
    "RecoveryEvidenceVerification",
    "RecoveryReadiness",
    "RecoveryTrustState",
    "derive_recovery_readiness",
    "verify_recovery_evidence_chain",
]
