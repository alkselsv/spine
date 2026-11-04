"""Content-free lifecycle and lineage receipts for governed email scans."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel


class EmailLifecycleCoverage(str, Enum):
    COMPLETE_AUTHORIZED = "complete_authorized"
    INCOMPLETE_SCAN = "incomplete_scan"
    PERMISSION_LOSS = "permission_loss"
    PROVIDER_OUTAGE = "provider_outage"


class EmailLifecycleOutcome(str, Enum):
    OBSERVED = "observed"
    ABSENCE_EVIDENCE = "absence_evidence"
    MOVE_OR_COPY_CANDIDATE = "move_or_copy_candidate"
    REAPPEARED = "reappeared"
    INCOMPLETE_SCAN = "incomplete_scan"
    PERMISSION_LOSS = "permission_loss"
    PROVIDER_OUTAGE = "provider_outage"


class EmailLifecycleLineage(str, Enum):
    NONE = "none"
    PRESERVED_IDENTITY = "preserved_identity"
    DISTINCT_IDENTITY_CANDIDATE = "distinct_identity_candidate"


class EmailLifecycleReceipt(DefinitionModel):
    """Immutable evidence receipt; it intentionally carries no deletion authority."""

    schema_version: Literal["email-lifecycle-receipt:v1"] = "email-lifecycle-receipt:v1"
    receipt_id: UUID
    workspace_id: UUID
    environment_id: UUID
    scan_id: UUID
    outcome: EmailLifecycleOutcome
    coverage: EmailLifecycleCoverage
    lineage: EmailLifecycleLineage = EmailLifecycleLineage.NONE
    provider_identity: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+:[a-z2-7]{52}$", max_length=96
    )
    candidate_provider_identity: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+:[a-z2-7]{52}$", max_length=96
    )
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    candidate_source_object_id: UUID | None = None
    candidate_source_revision_id: UUID | None = None
    predecessor_receipt_id: UUID | None = None
    similarity_basis: Literal["message_id", "content_sha256"] | None = None
    similarity_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    identity_continuity_proven: bool = False
    authoritative_absence: bool = False
    tombstone_authorized: Literal[False] = False
    observed_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> "EmailLifecycleReceipt":
        for field_name in (
            "receipt_id",
            "workspace_id",
            "environment_id",
            "scan_id",
        ):
            if getattr(self, field_name).int == 0:
                raise ValueError(f"{field_name} must be non-zero")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("email lifecycle timestamp must be timezone-aware")

        source_fields = (
            self.provider_identity,
            self.source_object_id,
            self.source_revision_id,
        )
        candidate_fields = (
            self.candidate_provider_identity,
            self.candidate_source_object_id,
            self.candidate_source_revision_id,
            self.similarity_basis,
            self.similarity_digest,
        )
        if self.outcome in {
            EmailLifecycleOutcome.INCOMPLETE_SCAN,
            EmailLifecycleOutcome.PERMISSION_LOSS,
            EmailLifecycleOutcome.PROVIDER_OUTAGE,
        }:
            expected_coverage = {
                EmailLifecycleOutcome.INCOMPLETE_SCAN: (
                    EmailLifecycleCoverage.INCOMPLETE_SCAN
                ),
                EmailLifecycleOutcome.PERMISSION_LOSS: (
                    EmailLifecycleCoverage.PERMISSION_LOSS
                ),
                EmailLifecycleOutcome.PROVIDER_OUTAGE: (
                    EmailLifecycleCoverage.PROVIDER_OUTAGE
                ),
            }[self.outcome]
            if self.coverage is not expected_coverage:
                raise ValueError("scan lifecycle outcome must match its coverage")
            if any(value is not None for value in (*source_fields, *candidate_fields)):
                raise ValueError(
                    "scan-level lifecycle outcomes cannot identify content"
                )
            if (
                self.lineage is not EmailLifecycleLineage.NONE
                or self.identity_continuity_proven
                or self.authoritative_absence
                or self.predecessor_receipt_id is not None
            ):
                raise ValueError("unusable coverage cannot assert lifecycle authority")
            return self

        if self.coverage is not EmailLifecycleCoverage.COMPLETE_AUTHORIZED:
            raise ValueError(
                "message lifecycle evidence requires complete authorized coverage"
            )
        if any(value is None for value in source_fields):
            raise ValueError("message lifecycle evidence requires exact source lineage")

        if self.outcome is EmailLifecycleOutcome.ABSENCE_EVIDENCE:
            if (
                not self.authoritative_absence
                or self.lineage is not EmailLifecycleLineage.NONE
                or self.identity_continuity_proven
                or any(value is not None for value in candidate_fields)
            ):
                raise ValueError("absence evidence cannot assert identity or deletion")
        elif self.outcome is EmailLifecycleOutcome.MOVE_OR_COPY_CANDIDATE:
            if (
                any(value is None for value in candidate_fields)
                or self.provider_identity == self.candidate_provider_identity
                or self.lineage is not EmailLifecycleLineage.DISTINCT_IDENTITY_CANDIDATE
                or self.identity_continuity_proven
                or self.authoritative_absence
            ):
                raise ValueError(
                    "move/copy evidence must retain distinct candidate identities"
                )
        elif self.outcome is EmailLifecycleOutcome.REAPPEARED:
            if (
                self.predecessor_receipt_id is None
                or self.lineage is not EmailLifecycleLineage.PRESERVED_IDENTITY
                or not self.identity_continuity_proven
                or self.authoritative_absence
                or any(value is not None for value in candidate_fields)
            ):
                raise ValueError("reappearance requires exact identity lineage")
        elif (
            self.lineage is not EmailLifecycleLineage.NONE
            or self.identity_continuity_proven
            or self.authoritative_absence
            or self.predecessor_receipt_id is not None
            or any(value is not None for value in candidate_fields)
        ):
            raise ValueError("observed email cannot carry inferred lifecycle authority")
        return self


__all__ = [
    "EmailLifecycleCoverage",
    "EmailLifecycleLineage",
    "EmailLifecycleOutcome",
    "EmailLifecycleReceipt",
]
