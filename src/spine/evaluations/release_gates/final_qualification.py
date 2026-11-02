"""Executable final R0/R1 evidence resolver and aggregate gate."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, field_validator, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.evaluations.final_qualification import (
    CriterionEvidence,
    CriterionInput,
    CriterionResult,
    Decision,
    FinalQualificationPackage,
    FinalQualificationReport,
    QualificationCounts,
    ReleaseBlocker,
    Sha256Digest,
    _CRITERION_IDS,
    _REQUIRED_BOUNDARIES,
    _REQUIRED_EVIDENCE_KINDS,
)

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
