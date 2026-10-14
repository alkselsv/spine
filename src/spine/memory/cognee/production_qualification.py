"""Fail-closed decision model for the Cognee production profile qualification.

The model consumes only bounded observations produced by an operator-controlled
live suite.  It cannot turn the deterministic fake evidence from Issue #211
into production approval and it never carries provider exception text or source
content into the qualification report.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel


class GateStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class QualificationMode(str, Enum):
    LIVE_REAL_BACKENDS = "live_real_backends"
    OFFLINE_FAKE = "offline_fake"


class QualificationDecision(str, Enum):
    GO = "go"
    NO_GO = "no_go"


GateId = Literal[
    "public_sdk",
    "structured_round_trip",
    "unsafe_evidence",
    "workspace_isolation",
    "namespace_isolation",
    "explicit_retrieval_context",
    "update_remove_tombstone",
    "rebuild_recovery_validation_diagnostics",
    "concurrency_failure_cleanup",
    "canonical_lifecycle_separation",
]
SafeCode = Literal[
    "physical_failure",
    "incomplete_evidence",
    "isolation_failure",
    "cleanup_failure",
    "concurrency_failure",
    "configuration_mismatch",
    "live_prerequisite_unavailable",
]
DecisionReason = Literal[
    "all_required_gates_passed",
    "gate_failure_or_missing_evidence",
    "live_real_backend_evidence_required",
    "production_profile_rejected",
    "invalid_gate_evidence",
]


_REQUIRED_GATES: tuple[GateId, ...] = (
    "public_sdk",
    "structured_round_trip",
    "unsafe_evidence",
    "workspace_isolation",
    "namespace_isolation",
    "explicit_retrieval_context",
    "update_remove_tombstone",
    "rebuild_recovery_validation_diagnostics",
    "concurrency_failure_cleanup",
    "canonical_lifecycle_separation",
)

_FAILURE_CODES: dict[GateId, SafeCode] = {
    "public_sdk": "configuration_mismatch",
    "structured_round_trip": "physical_failure",
    "unsafe_evidence": "incomplete_evidence",
    "workspace_isolation": "isolation_failure",
    "namespace_isolation": "isolation_failure",
    "explicit_retrieval_context": "isolation_failure",
    "update_remove_tombstone": "physical_failure",
    "rebuild_recovery_validation_diagnostics": "physical_failure",
    "concurrency_failure_cleanup": "concurrency_failure",
    "canonical_lifecycle_separation": "configuration_mismatch",
}


class ProductionProfile(DefinitionModel):
    """Backend identity only; credentials and endpoint URLs are never retained."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relational: str = Field(min_length=1, max_length=32)
    graph: str = Field(min_length=1, max_length=32)
    vector: str = Field(min_length=1, max_length=32)

    @classmethod
    def approved_candidate(cls) -> "ProductionProfile":
        return cls(relational="postgresql", graph="neo4j", vector="pgvector")


class GateObservation(DefinitionModel):
    """One content-free result from the live qualification harness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str = Field(min_length=1, max_length=64)
    status: GateStatus
    safe_code: SafeCode | None = None

    @model_validator(mode="after")
    def validate_safe_code(self) -> "GateObservation":
        if self.status is GateStatus.PASSED and self.safe_code is not None:
            raise ValueError("passed gate observation cannot contain a failure code")
        if self.status is not GateStatus.PASSED and self.safe_code is None:
            raise ValueError("non-passing gate observation requires a safe code")
        return self


class ProductionQualificationResult(DefinitionModel):
    """Machine-readable production decision with no free-form provider output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cognee-production-qualification:v1"] = (
        "cognee-production-qualification:v1"
    )
    profile: ProductionProfile
    mode: QualificationMode
    decision: QualificationDecision
    production_approval: bool
    safe_reason_code: DecisionReason
    observations: tuple[GateObservation, ...]
    failed_gate_ids: tuple[str, ...] = ()
    not_run_gate_ids: tuple[str, ...] = ()
    fallback: Literal["owned_postgresql_projection_mapping"]

    @model_validator(mode="after")
    def validate_decision(self) -> "ProductionQualificationResult":
        reason, failed, not_run = _derive_decision(
            profile=self.profile,
            mode=self.mode,
            observations=self.observations,
        )
        approved = reason == "all_required_gates_passed"
        if (
            self.production_approval is not approved
            or (self.decision is QualificationDecision.GO) is not approved
            or self.safe_reason_code != reason
            or self.failed_gate_ids != failed
            or self.not_run_gate_ids != not_run
        ):
            raise ValueError("qualification decision does not match gate evidence")
        return self


class Issue212QualificationReport(ProductionQualificationResult):
    """Versioned, closed schema for the committed Issue #212 report."""

    ticket: Literal[212] = 212
    qualification_run_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^(not-run|[a-z0-9][a-z0-9-]{0,31})$",
    )
    issue_211_fake_qualification: Literal["not_production_evidence"]
    postgres_demo: Literal["rejected"]
    canonical_lifecycle_owner: Literal["postgresql"]
    physical_calls_inside_postgresql_unit_of_work: Literal[False]


def required_gate_ids() -> tuple[GateId, ...]:
    """Return the fixed gate inventory for report and harness traceability."""

    return _REQUIRED_GATES


QualificationGate = Callable[[], Awaitable[None]]


async def run_qualification_gates(
    gates: Mapping[str, QualificationGate],
) -> tuple[GateObservation, ...]:
    """Run every required gate and retain only bounded observations.

    A failed gate does not prevent later independent gates from running.  Raw
    provider exceptions are deliberately discarded at this boundary.
    """

    observations: list[GateObservation] = []
    for gate_id in _REQUIRED_GATES:
        gate = gates.get(gate_id)
        if gate is None:
            observations.append(
                GateObservation(
                    gate_id=gate_id,
                    status=GateStatus.NOT_RUN,
                    safe_code="live_prerequisite_unavailable",
                )
            )
            continue
        try:
            await gate()
        except Exception:
            observations.append(
                GateObservation(
                    gate_id=gate_id,
                    status=GateStatus.FAILED,
                    safe_code=_FAILURE_CODES[gate_id],
                )
            )
        else:
            observations.append(
                GateObservation(gate_id=gate_id, status=GateStatus.PASSED)
            )
    return tuple(observations)


def _derive_decision(
    *,
    profile: ProductionProfile,
    mode: QualificationMode,
    observations: tuple[GateObservation, ...],
) -> tuple[DecisionReason, tuple[str, ...], tuple[str, ...]]:
    observed_ids = tuple(observation.gate_id for observation in observations)
    valid_inventory = (
        len(observed_ids) == len(set(observed_ids))
        and set(observed_ids) == set(_REQUIRED_GATES)
    )
    failed = tuple(
        observation.gate_id
        for observation in observations
        if observation.status is GateStatus.FAILED
    )
    not_run = tuple(
        observation.gate_id
        for observation in observations
        if observation.status is GateStatus.NOT_RUN
    )
    if profile != ProductionProfile.approved_candidate():
        reason: DecisionReason = "production_profile_rejected"
    elif mode is not QualificationMode.LIVE_REAL_BACKENDS:
        reason = "live_real_backend_evidence_required"
    elif not valid_inventory:
        reason = "invalid_gate_evidence"
    elif failed or not_run:
        reason = "gate_failure_or_missing_evidence"
    else:
        reason = "all_required_gates_passed"
    return reason, failed, not_run


def evaluate_production_profile(
    *,
    profile: ProductionProfile,
    mode: QualificationMode,
    observations: tuple[GateObservation, ...],
) -> ProductionQualificationResult:
    """Issue a production decision from bounded live observations.

    Missing, duplicate and unknown observations are invalid evidence rather
    than implicit passes.  The fallback is retained even for a go decision so
    an operator has an explicit recovery recommendation if a later gate drifts.
    """

    reason, failed, not_run = _derive_decision(
        profile=profile,
        mode=mode,
        observations=observations,
    )
    approved = reason == "all_required_gates_passed"
    return ProductionQualificationResult(
        profile=profile,
        mode=mode,
        decision=(
            QualificationDecision.GO if approved else QualificationDecision.NO_GO
        ),
        production_approval=approved,
        safe_reason_code=reason,
        observations=observations,
        failed_gate_ids=failed,
        not_run_gate_ids=not_run,
        fallback="owned_postgresql_projection_mapping",
    )


def build_issue212_report(
    *,
    qualification_run_id: str,
    observations: tuple[GateObservation, ...],
) -> Issue212QualificationReport:
    """Build the ticket report from one bounded live-run observation set."""

    result = evaluate_production_profile(
        profile=ProductionProfile.approved_candidate(),
        mode=QualificationMode.LIVE_REAL_BACKENDS,
        observations=observations,
    )
    return Issue212QualificationReport(
        **result.model_dump(),
        qualification_run_id=qualification_run_id,
        issue_211_fake_qualification="not_production_evidence",
        postgres_demo="rejected",
        canonical_lifecycle_owner="postgresql",
        physical_calls_inside_postgresql_unit_of_work=False,
    )


def write_issue212_report(
    path: Path,
    *,
    qualification_run_id: str,
    observations: tuple[GateObservation, ...],
) -> Issue212QualificationReport:
    """Write one validated, content-free report to an operator-selected path."""

    report = build_issue212_report(
        qualification_run_id=qualification_run_id,
        observations=observations,
    )
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return report


__all__ = [
    "GateObservation",
    "GateStatus",
    "build_issue212_report",
    "Issue212QualificationReport",
    "ProductionProfile",
    "ProductionQualificationResult",
    "QualificationDecision",
    "QualificationMode",
    "evaluate_production_profile",
    "required_gate_ids",
    "run_qualification_gates",
    "write_issue212_report",
]
