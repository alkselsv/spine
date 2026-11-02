"""Deterministic metadata-only gate for the final R0/R1 release decision.

The gate consumes bounded evidence references.  It never reads qualification
payloads, runs private scenarios, or upgrades synthetic evidence into a
production claim.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    Field,
    PrivateAttr,
    StringConstraints,
    field_validator,
    model_validator,
)

from spine.domain.common import DefinitionModel, VersionIdentifier

CriterionStatus = Literal["pass", "fail", "blocked", "not_verified"]
EvidenceStatus = Literal["pass", "fail", "blocked", "not_verified", "not_applicable"]
EvidenceBoundary = Literal["physical", "live", "repository", "synthetic"]
EvidenceKind = Literal[
    "migration_graph",
    "repository_gate",
    "deployment_profile",
    "authorization",
    "ingestion_lifecycle",
    "physical_provider",
    "projection_live",
    "projection_failure_injection",
    "grounded_qa",
    "safe_outcomes",
    "durable_session",
    "browser_journey",
    "diagnostics",
    "privacy_scan",
    "backup_restore",
    "real_baseline",
    "threshold_approval",
    "threshold_gate",
    "legacy_disposition",
    "metric",
]
Decision = Literal["go", "no_go"]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]

_CRITERION_IDS = tuple(f"R{number}" for number in range(1, 17))
_REQUIRED_EVIDENCE_KINDS: dict[str, frozenset[EvidenceKind]] = {
    "R1": frozenset({"migration_graph", "repository_gate"}),
    "R2": frozenset({"deployment_profile"}),
    "R3": frozenset({"authorization"}),
    "R4": frozenset({"ingestion_lifecycle"}),
    "R5": frozenset({"physical_provider"}),
    "R6": frozenset({"projection_live", "projection_failure_injection"}),
    "R7": frozenset({"grounded_qa"}),
    "R8": frozenset({"safe_outcomes"}),
    "R9": frozenset({"durable_session"}),
    "R10": frozenset({"browser_journey"}),
    "R11": frozenset({"diagnostics"}),
    "R12": frozenset({"privacy_scan"}),
    "R13": frozenset({"backup_restore"}),
    "R14": frozenset({"real_baseline"}),
    "R15": frozenset({"threshold_approval", "threshold_gate"}),
    "R16": frozenset({"legacy_disposition"}),
}
_REQUIRED_BOUNDARIES: dict[EvidenceKind, frozenset[EvidenceBoundary]] = {
    "migration_graph": frozenset({"repository"}),
    "repository_gate": frozenset({"repository"}),
    "deployment_profile": frozenset({"live"}),
    "authorization": frozenset({"live"}),
    "ingestion_lifecycle": frozenset({"live"}),
    "physical_provider": frozenset({"physical"}),
    "projection_live": frozenset({"live", "physical"}),
    "projection_failure_injection": frozenset({"repository"}),
    "grounded_qa": frozenset({"live"}),
    "safe_outcomes": frozenset({"live"}),
    "durable_session": frozenset({"live"}),
    "browser_journey": frozenset({"live"}),
    "diagnostics": frozenset({"live"}),
    "privacy_scan": frozenset({"live"}),
    "backup_restore": frozenset({"physical"}),
    "real_baseline": frozenset({"live"}),
    "threshold_approval": frozenset({"repository"}),
    "threshold_gate": frozenset({"live", "repository"}),
    "legacy_disposition": frozenset({"live", "repository"}),
    "metric": frozenset({"live", "repository"}),
}
_EXPECTED_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "R1": (),
    "R2": ("R1",),
    "R3": ("R2",),
    "R4": ("R2",),
    "R5": ("R2", "R4"),
    "R6": ("R4", "R5"),
    "R7": ("R2", "R3", "R5", "R6"),
    "R8": ("R7",),
    "R9": ("R2", "R3"),
    "R10": ("R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"),
    "R11": ("R10",),
    "R12": ("R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11"),
    "R13": ("R2", "R4", "R5", "R6"),
    "R14": ("R2", "R3", "R4", "R5", "R6", "R7"),
    "R15": ("R14",),
    "R16": ("R1",),
}
_FORBIDDEN_IDENTIFIER_FRAGMENTS = (
    "answer",
    "chain_of_thought",
    "credential",
    "excerpt",
    "filename",
    "question",
    "raw_payload",
    "source_text",
)


def _safe_identifier(value: str) -> str:
    lowered = value.lower()
    if any(fragment in lowered for fragment in _FORBIDDEN_IDENTIFIER_FRAGMENTS):
        raise ValueError(
            "qualification metadata identifier contains a protected field name"
        )
    return value


def _safe_identifiers(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        _safe_identifier(value)
    return values


class CriterionEvidence(DefinitionModel):
    """Immutable reference to one disclosure-safe evidence artifact."""

    evidence_id: VersionIdentifier
    evidence_kind: EvidenceKind
    digest: Sha256Digest
    status: EvidenceStatus
    mandatory: bool = True
    boundary: EvidenceBoundary
    target_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    deployment_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    policy_generation: VersionIdentifier | None = None
    captured_at: datetime | None = None
    eligible_count: int | None = Field(default=None, ge=0)

    _identifiers_are_safe = field_validator("evidence_id")(_safe_identifier)

    @model_validator(mode="after")
    def validate_applicability(self) -> "CriterionEvidence":
        if self.status == "not_applicable":
            if self.mandatory or self.eligible_count != 0:
                raise ValueError(
                    "not_applicable evidence must be optional with zero eligible count"
                )
        elif self.eligible_count is not None:
            raise ValueError("eligible count is reserved for applicability evidence")
        return self


class EvidenceArtifact(DefinitionModel):
    """Repository-local artifact resolved before the aggregate can run."""

    evidence_id: VersionIdentifier
    artifact_path: str = Field(min_length=1, max_length=512)
    digest: Sha256Digest

    @field_validator("artifact_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "evidence artifact path must remain under the evidence root"
            )
        return value


class EvidenceIndex(DefinitionModel):
    """Paths used only by the resolver; paths never enter the final report."""

    schema_version: Literal["r0-r1-evidence-index:v1"] = "r0-r1-evidence-index:v1"
    entries: tuple[EvidenceArtifact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "EvidenceIndex":
        identifiers = [item.evidence_id for item in self.entries]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("evidence index identifiers must be unique")
        return self


_VERIFIED_INDEX_ISSUER = object()


class VerifiedEvidenceIndex(DefinitionModel):
    """Digest bindings issued only after repository artifacts were read."""

    digest: Sha256Digest
    entries: tuple[tuple[VersionIdentifier, str], ...]
    _issuer: object | None = PrivateAttr(default=None)

    def is_verified(self, evidence_id: str, digest: str) -> bool:
        return (
            self._issuer is _VERIFIED_INDEX_ISSUER
            and (evidence_id, digest) in self.entries
        )


def verify_evidence_index(index: EvidenceIndex, *, root: Path) -> VerifiedEvidenceIndex:
    """Resolve every artifact and issue a path-free verified digest index."""

    resolved_root = root.resolve(strict=True)
    verified: list[tuple[str, str]] = []
    for entry in index.entries:
        artifact_path = (resolved_root / entry.artifact_path).resolve(strict=True)
        if not artifact_path.is_relative_to(resolved_root):
            raise ValueError("evidence artifact escaped the evidence root")
        observed = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if observed != entry.digest:
            raise ValueError("evidence artifact digest mismatch")
        verified.append((entry.evidence_id, entry.digest))
    entries = tuple(sorted(verified))
    payload = json.dumps(entries, separators=(",", ":"), ensure_ascii=True)
    result = VerifiedEvidenceIndex(
        digest=hashlib.sha256(payload.encode("ascii")).hexdigest(),
        entries=entries,
    )
    result._issuer = _VERIFIED_INDEX_ISSUER
    return result


class CriterionInput(DefinitionModel):
    """Release-owner assessment for one mandatory R1-R16 criterion."""

    criterion_id: Literal[
        "R1",
        "R2",
        "R3",
        "R4",
        "R5",
        "R6",
        "R7",
        "R8",
        "R9",
        "R10",
        "R11",
        "R12",
        "R13",
        "R14",
        "R15",
        "R16",
    ]
    owner_function: VersionIdentifier
    responsible_principal_id: VersionIdentifier | None = None
    ticket_ids: tuple[int, ...] = Field(min_length=1)
    prerequisite_ids: tuple[VersionIdentifier, ...] = ()
    status: CriterionStatus
    reason_codes: tuple[VersionIdentifier, ...] = ()
    evidence: tuple[CriterionEvidence, ...] = Field(min_length=1)

    _owner_is_safe = field_validator("owner_function")(_safe_identifier)
    _principal_is_safe = field_validator("responsible_principal_id")(
        lambda value: None if value is None else _safe_identifier(value)
    )
    _identifier_lists_are_safe = field_validator("prerequisite_ids", "reason_codes")(
        _safe_identifiers
    )

    @model_validator(mode="after")
    def validate_assessment(self) -> "CriterionInput":
        if len(self.ticket_ids) != len(set(self.ticket_ids)):
            raise ValueError("criterion ticket identifiers must be unique")
        if self.status == "pass" and self.reason_codes:
            raise ValueError("passing criterion cannot carry failure reason codes")
        if self.status == "pass" and self.responsible_principal_id is None:
            raise ValueError("passing criterion requires an attributable principal")
        if self.status != "pass" and not self.reason_codes:
            raise ValueError("non-passing criterion requires a bounded reason code")
        return self


class FinalQualificationPackage(DefinitionModel):
    """Exact release target and all mandatory criterion assessments."""

    schema_version: Literal["r0-r1-final-qualification-input:v1"] = (
        "r0-r1-final-qualification-input:v1"
    )
    package_id: VersionIdentifier
    target_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_generation: VersionIdentifier
    started_at: datetime
    evaluated_at: datetime
    evidence_valid_after: datetime
    release_owner_function: VersionIdentifier
    release_owner_principal_id: VersionIdentifier
    component_versions: tuple[tuple[VersionIdentifier, VersionIdentifier], ...] = Field(
        min_length=1
    )
    configuration_digests: tuple[tuple[VersionIdentifier, Sha256Digest], ...] = Field(
        min_length=1
    )
    scope_references: tuple[tuple[VersionIdentifier, VersionIdentifier], ...] = Field(
        min_length=1
    )
    known_limitations: tuple[VersionIdentifier, ...] = Field(min_length=1)
    criteria: tuple[CriterionInput, ...]

    _identifiers_are_safe = field_validator(
        "package_id",
        "policy_generation",
        "release_owner_function",
        "release_owner_principal_id",
    )(_safe_identifier)

    @model_validator(mode="after")
    def validate_release_matrix(self) -> "FinalQualificationPackage":
        criterion_ids = tuple(item.criterion_id for item in self.criteria)
        if criterion_ids != _CRITERION_IDS:
            raise ValueError(
                "criteria must contain R1 through R16 exactly once in order"
            )
        if self.evidence_valid_after > self.evaluated_at:
            raise ValueError("evidence validity boundary cannot follow evaluation time")
        if self.evaluated_at.tzinfo is None or self.evidence_valid_after.tzinfo is None:
            raise ValueError("qualification times must include a timezone")
        if self.started_at.tzinfo is None or self.started_at > self.evaluated_at:
            raise ValueError(
                "qualification start must be timezone-aware and not after end"
            )
        for criterion in self.criteria:
            if (
                criterion.prerequisite_ids
                != _EXPECTED_PREREQUISITES[criterion.criterion_id]
            ):
                raise ValueError(
                    f"{criterion.criterion_id} prerequisite map does not match the release contract"
                )
            observed_kinds = {item.evidence_kind for item in criterion.evidence}
            if not _REQUIRED_EVIDENCE_KINDS[criterion.criterion_id] <= observed_kinds:
                raise ValueError(
                    f"{criterion.criterion_id} is missing its required evidence kind"
                )
            for evidence in criterion.evidence:
                if evidence.status == "not_applicable" and not (
                    criterion.criterion_id == "R15"
                    and evidence.evidence_kind == "metric"
                ):
                    raise ValueError(
                        "not_applicable is not permitted for this release criterion"
                    )
        return self


class CriterionResult(DefinitionModel):
    """Effective result after compatibility and freshness checks."""

    criterion_id: VersionIdentifier
    owner_function: VersionIdentifier
    responsible_principal_id: VersionIdentifier | None
    ticket_ids: tuple[int, ...]
    prerequisite_ids: tuple[VersionIdentifier, ...]
    status: CriterionStatus
    reason_codes: tuple[VersionIdentifier, ...]
    evidence: tuple[CriterionEvidence, ...]


class ReleaseBlocker(DefinitionModel):
    """Attributable reason that prevents GO."""

    criterion_id: VersionIdentifier
    owner_function: VersionIdentifier
    status: Literal["fail", "blocked", "not_verified"]
    reason_codes: tuple[VersionIdentifier, ...] = Field(min_length=1)


class FinalQualificationReport(DefinitionModel):
    """Reproducible metadata-only R1-R17 decision record."""

    schema_version: Literal["r0-r1-final-qualification-report:v1"] = (
        "r0-r1-final-qualification-report:v1"
    )
    package_id: VersionIdentifier
    target_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_generation: VersionIdentifier
    release_owner_principal_id: VersionIdentifier
    started_at: datetime
    evaluated_at: datetime
    component_versions: tuple[tuple[VersionIdentifier, VersionIdentifier], ...]
    configuration_digests: tuple[tuple[VersionIdentifier, Sha256Digest], ...]
    scope_references: tuple[tuple[VersionIdentifier, VersionIdentifier], ...]
    known_limitations: tuple[VersionIdentifier, ...]
    decision: Decision
    input_digest: Sha256Digest
    evidence_index_digest: Sha256Digest
    criteria: tuple[CriterionResult, ...] = Field(min_length=17, max_length=17)
    counts: "QualificationCounts"
    blockers: tuple[ReleaseBlocker, ...]


class QualificationCounts(DefinitionModel):
    """Closed aggregate schema for criterion and applicability outcomes."""

    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    not_verified: int = Field(ge=0)
    not_applicable: int = Field(ge=0)


def _input_digest(
    package: FinalQualificationPackage, verified_index: VerifiedEvidenceIndex
) -> str:
    payload = json.dumps(
        {
            "package": package.model_dump(mode="json"),
            "evidence_index_digest": verified_index.digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _evaluate_criterion(
    package: FinalQualificationPackage,
    criterion: CriterionInput,
    verified_index: VerifiedEvidenceIndex,
) -> CriterionResult:
    status = criterion.status
    reasons = list(criterion.reason_codes)
    evidence_by_id: dict[str, CriterionEvidence] = {}

    for evidence in criterion.evidence:
        previous = evidence_by_id.get(evidence.evidence_id)
        if previous is not None and previous != evidence:
            status = "not_verified"
            _append_reason(reasons, "evidence_conflict")
        evidence_by_id[evidence.evidence_id] = evidence
        if not verified_index.is_verified(evidence.evidence_id, evidence.digest):
            status = "not_verified"
            _append_reason(reasons, "evidence_digest_unverified")

    if status == "pass":
        mandatory_evidence = tuple(
            item for item in criterion.evidence if item.mandatory
        )
        if not mandatory_evidence:
            status = "not_verified"
            _append_reason(reasons, "mandatory_evidence_missing")

        for required_kind in _REQUIRED_EVIDENCE_KINDS[criterion.criterion_id]:
            matching = tuple(
                item
                for item in criterion.evidence
                if item.evidence_kind == required_kind and item.mandatory
            )
            if not matching or not any(item.status == "pass" for item in matching):
                status = "not_verified"
                _append_reason(reasons, "required_evidence_kind_not_passed")
            if any(
                item.boundary not in _REQUIRED_BOUNDARIES[required_kind]
                for item in matching
            ):
                status = "not_verified"
                _append_reason(reasons, "evidence_boundary_mismatch")

        for evidence in criterion.evidence:
            missing_binding = (
                evidence.target_commit is None
                or evidence.deployment_fingerprint is None
                or evidence.policy_generation is None
                or evidence.captured_at is None
            )
            if missing_binding:
                status = "not_verified"
                _append_reason(reasons, "evidence_binding_missing")
                continue
            if evidence.target_commit != package.target_commit:
                status = "not_verified"
                _append_reason(reasons, "evidence_commit_mismatch")
            if evidence.deployment_fingerprint != package.deployment_fingerprint:
                status = "not_verified"
                _append_reason(reasons, "evidence_deployment_mismatch")
            if evidence.policy_generation != package.policy_generation:
                status = "not_verified"
                _append_reason(reasons, "evidence_policy_mismatch")
            if (
                not package.evidence_valid_after
                <= evidence.captured_at
                <= package.evaluated_at
            ):
                status = "not_verified"
                _append_reason(reasons, "evidence_stale")
            if evidence.boundary == "synthetic":
                status = "not_verified"
                _append_reason(reasons, "synthetic_evidence_cannot_pass")
            if evidence.mandatory and evidence.status != "pass":
                status = (
                    evidence.status
                    if evidence.status in {"fail", "blocked", "not_verified"}
                    else "not_verified"
                )
                _append_reason(reasons, "mandatory_evidence_not_passed")

    return CriterionResult(
        criterion_id=criterion.criterion_id,
        owner_function=criterion.owner_function,
        responsible_principal_id=criterion.responsible_principal_id,
        ticket_ids=criterion.ticket_ids,
        prerequisite_ids=criterion.prerequisite_ids,
        status=status,
        reason_codes=tuple(reasons),
        evidence=criterion.evidence,
    )


def evaluate_final_qualification(
    package: FinalQualificationPackage,
    verified_index: VerifiedEvidenceIndex,
) -> FinalQualificationReport:
    """Evaluate the exact R1-R16 matrix and append the derived R17 decision."""

    criterion_results = tuple(
        _evaluate_criterion(package, criterion, verified_index)
        for criterion in package.criteria
    )
    blockers = tuple(
        ReleaseBlocker(
            criterion_id=result.criterion_id,
            owner_function=result.owner_function,
            status=result.status,
            reason_codes=result.reason_codes,
        )
        for result in criterion_results
        if result.status != "pass"
    )
    decision: Decision = "go" if not blockers else "no_go"
    final_reason_codes = (
        ()
        if decision == "go"
        else tuple(f"mandatory_{item.status}" for item in blockers)
    )
    final_result = CriterionResult(
        criterion_id="R17",
        owner_function=package.release_owner_function,
        responsible_principal_id=package.release_owner_principal_id,
        ticket_ids=(16, 511),
        prerequisite_ids=_CRITERION_IDS,
        status="pass" if decision == "go" else "fail",
        reason_codes=tuple(dict.fromkeys(final_reason_codes)),
        evidence=(),
    )

    counts: Counter[str] = Counter(
        result.status for result in (*criterion_results, final_result)
    )
    counts["not_applicable"] = sum(
        evidence.status == "not_applicable"
        for result in criterion_results
        for evidence in result.evidence
    )
    normalized_counts = QualificationCounts(
        passed=counts["pass"],
        failed=counts["fail"],
        blocked=counts["blocked"],
        not_verified=counts["not_verified"],
        not_applicable=counts["not_applicable"],
    )

    return FinalQualificationReport(
        package_id=package.package_id,
        target_commit=package.target_commit,
        deployment_fingerprint=package.deployment_fingerprint,
        policy_generation=package.policy_generation,
        release_owner_principal_id=package.release_owner_principal_id,
        started_at=package.started_at,
        evaluated_at=package.evaluated_at,
        component_versions=package.component_versions,
        configuration_digests=package.configuration_digests,
        scope_references=package.scope_references,
        known_limitations=package.known_limitations,
        decision=decision,
        input_digest=_input_digest(package, verified_index),
        evidence_index_digest=verified_index.digest,
        criteria=(*criterion_results, final_result),
        counts=normalized_counts,
        blockers=blockers,
    )
