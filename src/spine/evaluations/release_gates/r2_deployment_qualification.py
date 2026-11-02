"""Independent fail-closed evaluation of a live R2 deployment artifact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from spine.domain.evaluations.r2_deployment_qualification import (
    R2CollectionFailureEvidence,
    R2DeploymentEvidence,
    R2DeploymentQualificationResult,
)


_EVIDENCE_ADAPTER = TypeAdapter(R2DeploymentEvidence | R2CollectionFailureEvidence)
_BLOCKED_COLLECTION_REASON_PREFIXES = (
    "collector_io_unavailable",
    "http_probe_unavailable",
    "provider_authorization_missing",
    "runtime_configuration_unavailable",
    "runtime_setting_missing:",
)
from spine.evaluations.release_gates.artifact_attestation import (
    ArtifactAttestationVerificationError,
    ArtifactAttestationVerifier,
)


_REQUIRED_COMPONENT_ROLES = frozenset(
    {
        "api",
        "web",
        "qa_worker",
        "outbox_dispatcher",
        "postgresql",
        "temporal",
        "original_storage",
        "selected_provider",
    }
)
_EXPECTED_DEPENDENCIES = {
    "api": frozenset(
        {"postgresql", "temporal", "original_storage", "selected_provider"}
    ),
    "web": frozenset({"api"}),
    "qa_worker": frozenset({"postgresql", "temporal", "selected_provider"}),
    "outbox_dispatcher": frozenset(
        {"postgresql", "temporal", "selected_provider"}
    ),
    "postgresql": frozenset(),
    "temporal": frozenset({"postgresql"}),
    "original_storage": frozenset(),
    "selected_provider": frozenset({"postgresql"}),
}


def r2_composition_digest(evidence: R2DeploymentEvidence) -> str:
    """Return the canonical digest of the observed component graph."""

    manifest = tuple(
        sorted(
            (
                item.role,
                item.component_id,
                item.version,
                item.artifact_digest,
                item.observation_digest,
                item.deployment_fingerprint,
                tuple(sorted(item.dependencies)),
            )
            for item in evidence.components
        )
    )
    payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _append_once(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def qualify_r2_deployment_artifact(
    *,
    artifact_path: Path,
    expected_digest: str,
    expected_head_sha: str,
    expected_deployment_fingerprint: str,
    attestation_verifier: ArtifactAttestationVerifier,
) -> R2DeploymentQualificationResult:
    """Recompute R2 from artifact bytes and the release owner's exact target."""

    artifact_bytes = artifact_path.read_bytes()
    observed_digest = hashlib.sha256(artifact_bytes).hexdigest()
    failure_reasons: list[str] = []
    blocked_reasons: list[str] = []

    if observed_digest != expected_digest:
        failure_reasons.append("artifact_digest_mismatch")

    try:
        evidence = _EVIDENCE_ADAPTER.validate_json(artifact_bytes)
    except ValidationError:
        return R2DeploymentQualificationResult(
            status="fail",
            reason_codes=tuple((*failure_reasons, "artifact_schema_invalid")),
            artifact_digest=observed_digest,
            head_sha=expected_head_sha,
            deployment_fingerprint=expected_deployment_fingerprint,
        )

    attestation_trusted = True
    try:
        attestation = attestation_verifier.verify(
            artifact_path=artifact_path,
            artifact_digest=observed_digest,
            expected_head_sha=expected_head_sha,
        )
    except ArtifactAttestationVerificationError:
        attestation_trusted = False
    else:
        if attestation.artifact_digest != observed_digest:
            failure_reasons.append("attestation_digest_mismatch")
        if attestation.head_sha != expected_head_sha:
            failure_reasons.append("attestation_commit_mismatch")

    if evidence.target_commit != expected_head_sha:
        failure_reasons.append("target_commit_mismatch")
    if evidence.deployment_fingerprint != expected_deployment_fingerprint:
        failure_reasons.append("deployment_fingerprint_mismatch")

    privacy = evidence.privacy
    if not privacy.metadata_only or any(
        (
            privacy.credentials_recorded,
            privacy.network_locations_recorded,
            privacy.protected_content_recorded,
            privacy.provider_payloads_recorded,
            privacy.raw_errors_recorded,
        )
    ):
        failure_reasons.append("unsafe_evidence_artifact")

    if isinstance(evidence, R2CollectionFailureEvidence):
        if failure_reasons:
            status = "fail"
            reasons = failure_reasons
        elif not attestation_trusted:
            status = "not_verified"
            reasons = ["artifact_attestation_untrusted"]
        elif evidence.reason_code.startswith(_BLOCKED_COLLECTION_REASON_PREFIXES):
            status = "blocked"
            reasons = [evidence.reason_code]
        else:
            status = "fail"
            reasons = [evidence.reason_code]
        return R2DeploymentQualificationResult(
            status=status,
            reason_codes=tuple(reasons),
            artifact_digest=observed_digest,
            head_sha=expected_head_sha,
            deployment_fingerprint=expected_deployment_fingerprint,
        )

    observed_roles = {item.role for item in evidence.components}
    if _REQUIRED_COMPONENT_ROLES - observed_roles:
        blocked_reasons.append("required_component_missing")
    for component in evidence.components:
        if component.deployment_fingerprint != evidence.deployment_fingerprint:
            _append_once(failure_reasons, "component_binding_mismatch")
        if component.fallback_selected:
            _append_once(failure_reasons, "component_fallback_selected")
        if frozenset(component.dependencies) != _EXPECTED_DEPENDENCIES[component.role]:
            _append_once(failure_reasons, "component_composition_mismatch")
        if not component.configured:
            _append_once(blocked_reasons, "component_unconfigured")
        if not component.reachable:
            _append_once(blocked_reasons, "component_unreachable")
    if r2_composition_digest(evidence) != evidence.composition_digest:
        failure_reasons.append("composition_digest_mismatch")

    startup = evidence.startup
    if startup.status == "failed":
        failure_reasons.append("startup_failed")
    elif startup.status == "unavailable":
        blocked_reasons.append("startup_observation_unavailable")
    if startup.authority_mutations:
        failure_reasons.append("startup_authority_mutation_observed")
    if startup.provider_calls:
        failure_reasons.append("startup_provider_call_observed")

    readiness = evidence.readiness
    if readiness.status == "failed":
        failure_reasons.append("readiness_failed")
    elif readiness.status == "unavailable":
        blocked_reasons.append("readiness_unavailable")
    else:
        if not readiness.authenticated:
            failure_reasons.append("readiness_authentication_failed")
        if not readiness.scope_verified:
            failure_reasons.append("readiness_scope_mismatch")
    if readiness.authority_mutations:
        failure_reasons.append("readiness_authority_mutation_observed")
    if readiness.provider_calls:
        failure_reasons.append("readiness_provider_call_observed")

    canary = evidence.canary
    if canary.status == "failed":
        failure_reasons.append("canary_failed")
    elif canary.status == "not_run":
        blocked_reasons.append("canary_not_run")
    else:
        if not canary.authenticated:
            failure_reasons.append("canary_authentication_failed")
        if not canary.scope_verified:
            failure_reasons.append("canary_scope_mismatch")

    if failure_reasons:
        status = "fail"
        reasons = failure_reasons
    elif not attestation_trusted:
        status = "not_verified"
        reasons = ["artifact_attestation_untrusted"]
    elif evidence.live_execution is None:
        status = "not_verified"
        reasons = ["live_report_unlinked"]
    elif blocked_reasons:
        status = "blocked"
        reasons = blocked_reasons
    else:
        status = "pass"
        reasons = []

    return R2DeploymentQualificationResult(
        status=status,
        reason_codes=tuple(reasons),
        artifact_digest=observed_digest,
        head_sha=expected_head_sha,
        deployment_fingerprint=expected_deployment_fingerprint,
    )
