"""Closed contract for the Issue #369 operational qualification package."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier


REPORT_SCHEMA_VERSION = "r1-operational-foundation-qualification:v1"
REPORT_ID = "issue-369:qualification:v1"
_GATE_IDS = tuple(f"O{index:02d}" for index in range(1, 10))
_REFERENCE = re.compile(r"^(?P<path>[^:]+)::(?P<node>test_[A-Za-z0-9_]+)$")
EvidenceReference = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=512,
        pattern=r"^[^:]+::test_[A-Za-z0-9_]+$",
    ),
]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
GateId = Annotated[str, StringConstraints(pattern=r"^O0[1-9]$")]


class QualificationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    UNVERIFIED_PROVIDER = "unverified_provider"


class QualificationEnvironment(str, Enum):
    OFFLINE = "offline_deterministic_fake"
    DISPOSABLE = "disposable_integration"
    BROWSER = "browser_fixture"
    SYNTHETIC_AND_DISPOSABLE = "synthetic_and_disposable"


class EvidenceTimestampKind(str, Enum):
    COMMIT = "commit"
    QUALIFICATION_RECORD = "qualification_record"


class BlockerCode(str, Enum):
    DEPLOYMENT_BINDINGS = "deployment_bindings_unverified"
    PHYSICAL_RESTORE = "physical_restore_unverified"
    PROJECTION_PROVIDER = "projection_provider_unverified"
    RUNBOOK_EXERCISES = "runbook_deployment_exercises_incomplete"
    RETENTION_AUTHORIZATION = "retention_and_authorization_bindings_unverified"


class CrossCuttingKind(str, Enum):
    WORKSPACE_ISOLATION = "workspace_isolation"
    AUTHORIZATION_DENIAL = "authorization_denial"
    IDEMPOTENCY = "idempotency"
    RETRY_FAILURE_ROLLBACK = "retry_failure_rollback"
    STALE_DEGRADED_UNAVAILABLE = "stale_degraded_unavailable"
    NO_DISCLOSURE = "no_content_or_secret_disclosure"


class GateDecision(DefinitionModel):
    status: Literal[QualificationStatus.BLOCKED]
    production_qualified: Literal[False]
    downstream_ticket: Literal[16]
    reason_codes: tuple[BlockerCode, ...]


class GateEvidence(DefinitionModel):
    gate_id: GateId
    ticket: int = Field(gt=0)
    source_commit: CommitSha
    source_schema_version: VersionIdentifier
    environment: QualificationEnvironment
    observed_at: datetime
    timestamp_kind: EvidenceTimestampKind
    source_record: str | None = Field(default=None, min_length=1, max_length=256)
    verification_status: QualificationStatus
    evidence_references: tuple[EvidenceReference, ...] = Field(min_length=1)
    independently_verified_physical_evidence: bool | None = None
    blocker: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("observed_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("evidence timestamp must be aware UTC")
        return value

    @model_validator(mode="after")
    def require_fail_closed_detail(self) -> "GateEvidence":
        if self.verification_status is QualificationStatus.PASS and self.blocker:
            raise ValueError("passing evidence cannot carry a blocker")
        if self.verification_status is not QualificationStatus.PASS and not self.blocker:
            raise ValueError("non-passing evidence requires a blocker")
        if self.gate_id in {"O03", "O04"} and self.independently_verified_physical_evidence is not False:
            raise ValueError("O03/O04 cannot claim unrecorded physical verification")
        expects_record = self.timestamp_kind is EvidenceTimestampKind.QUALIFICATION_RECORD
        if expects_record != (self.source_record is not None):
            raise ValueError("qualification-record timestamps require one source record")
        return self


class OperationalPathStage(DefinitionModel):
    stage: VersionIdentifier
    gate_ids: tuple[GateId, ...] = Field(min_length=1)


class QualificationExecution(DefinitionModel):
    execution_id: VersionIdentifier
    environment: QualificationEnvironment
    executed_at: datetime
    status: Literal[QualificationStatus.PASS]
    working_directory: Literal[".", "web"]
    command: str = Field(min_length=1, max_length=512)
    source_artifact: str | None = Field(default=None, min_length=1, max_length=256)
    gate_ids: tuple[GateId, ...] = Field(min_length=1)

    @field_validator("executed_at")
    @classmethod
    def require_executed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("execution timestamp must be aware UTC")
        return value


class UnsupportedClaims(DefinitionModel):
    rpo_rto: Literal[False]
    pitr: Literal[False]
    ha: Literal[False]
    quality_threshold: Literal[False]


class OperationalFoundationQualificationReport(DefinitionModel):
    schema_version: Literal[REPORT_SCHEMA_VERSION]
    report_id: Literal[REPORT_ID]
    ticket: Literal[369]
    parent_ticket: Literal[18]
    generated_at: datetime
    immutable: Literal[True]
    content_free: Literal[True]
    server_logs_used: Literal[False]
    undocumented_claims_used: Literal[False]
    approved_contracts: tuple[VersionIdentifier, ...]
    outcome_semantics: dict[QualificationStatus, str]
    gate_decision: GateDecision
    evidence: tuple[GateEvidence, ...]
    execution_evidence: tuple[QualificationExecution, ...] = Field(min_length=1)
    operational_path: tuple[OperationalPathStage, ...]
    cross_cutting_verification: dict[
        CrossCuttingKind, tuple[EvidenceReference, ...]
    ]
    unresolved_conditions: tuple[BlockerCode, ...]
    unsupported_claims: UnsupportedClaims
    authority_boundaries: dict[VersionIdentifier, VersionIdentifier]

    @field_validator("generated_at")
    @classmethod
    def require_generated_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("report timestamp must be aware UTC")
        return value

    @model_validator(mode="after")
    def validate_complete_fail_closed_package(
        self,
    ) -> "OperationalFoundationQualificationReport":
        if tuple(sorted(item.gate_id for item in self.evidence)) != _GATE_IDS:
            raise ValueError("qualification must contain exactly O01 through O09")
        if len({item.ticket for item in self.evidence}) != len(_GATE_IDS):
            raise ValueError("each operational gate must identify one owning ticket")
        if set(self.outcome_semantics) != set(QualificationStatus):
            raise ValueError("qualification outcome vocabulary is incomplete")
        if set(self.cross_cutting_verification) != set(CrossCuttingKind):
            raise ValueError("cross-cutting verification matrix is incomplete")
        unresolved = set(self.unresolved_conditions)
        if set(self.gate_decision.reason_codes) != unresolved:
            raise ValueError("gate decision must retain every unresolved condition")
        if all(
            item.verification_status is QualificationStatus.PASS
            for item in self.evidence
        ):
            raise ValueError("blocked qualification requires fail-closed evidence")
        covered_gates = {
            gate_id
            for execution in self.execution_evidence
            for gate_id in execution.gate_ids
        }
        if covered_gates != set(_GATE_IDS):
            raise ValueError("execution evidence must cover O01 through O09")
        return self


def load_operational_foundation_report(
    path: Path,
) -> OperationalFoundationQualificationReport:
    """Read and close-validate one immutable qualification package."""

    return OperationalFoundationQualificationReport.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def split_evidence_reference(reference: str) -> tuple[str, str]:
    """Return the committed path and opaque test-node identity."""

    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError("invalid evidence reference")
    return match.group("path"), match.group("node")


__all__ = [
    "BlockerCode",
    "CrossCuttingKind",
    "GateEvidence",
    "EvidenceTimestampKind",
    "OperationalFoundationQualificationReport",
    "QualificationEnvironment",
    "QualificationStatus",
    "load_operational_foundation_report",
    "split_evidence_reference",
]
